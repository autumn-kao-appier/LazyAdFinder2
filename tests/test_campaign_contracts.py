import json
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import evidence_aos
import page
import qa_aos
import qa_ios
import run_aos_test_suite
import run_ios_test_suite
import run_reen_test_suite
from campaign_profiles import CAMPAIGN_PROFILES
from campaign_testcases import CAMPAIGN_TESTCASES
from testcases import android_signal_testcases
from testcases import ios_signal_testcases
from testcases.e2e.android_e2e_baseline import validate_bundle as validate_android_e2e


ROOT = Path(__file__).resolve().parents[1]


def round_args(name, test_type="aibid", mode="standalone"):
    return types.SimpleNamespace(
        command="round",
        name=name,
        test_mode=mode,
        test_type=test_type,
    )


class CampaignContractTests(unittest.TestCase):
    def test_ios_registry_is_platform_owned_and_does_not_alias_android(self):
        self.assertIsNot(ios_signal_testcases.TC_DEFINITIONS, android_signal_testcases.TC_DEFINITIONS)
        self.assertIsNot(ios_signal_testcases.ROUND_DEFINITIONS, android_signal_testcases.ROUND_DEFINITIONS)
        self.assertEqual("HAPPY-PATH", ios_signal_testcases.ROUND_DEFINITIONS["R1"].capture_name)
        self.assertIn("advertising-id", ios_signal_testcases.TC_DEFINITIONS)
        self.assertGreaterEqual(len(ios_signal_testcases.ROUND_DEFINITIONS["R1"].testcase_keys), 35)
        self.assertEqual("IDFA", ios_signal_testcases.TC_DEFINITIONS["advertising-id"].title.rsplit("(", 1)[-1].rstrip(")"))
        self.assertIn(qa_ios.EVENTS_FILE, qa_ios.DETECTOR_FILES)
        self.assertTrue(set(qa_ios.ADMOB_RAW_FILES).issubset(qa_ios.DETECTOR_FILES))

    def test_ios_suite_plan_runs_all_reviewed_platform_rounds(self):
        plan = run_ios_test_suite.execution_plan("aibid", "standalone")
        by_name = {item.name: item for item in plan}
        for name in ("R1", "R2", "R3", "R5", "E2E-STANDALONE"):
            self.assertEqual("RUN", by_name[name].decision)
            self.assertTrue(by_name[name].testcase_keys)
        self.assertEqual("NOT_EXECUTABLE", by_name["R4"].decision)
        ipv6 = {item.name: item for item in run_ios_test_suite.execution_plan(
            "aibid", "standalone", ipv6_ready=True,
        )}
        self.assertEqual("RUN", ipv6["R4"].decision)
        mediation = {item.name: item for item in run_ios_test_suite.execution_plan("aibid", "mediation")}
        self.assertEqual("RUN", mediation["E2E-ADMOB"].decision)
        self.assertIn("admob-pubsetting", mediation["E2E-ADMOB"].testcase_keys)

    def test_ios_suite_round_selection_is_decided_before_execution(self):
        plan = run_ios_test_suite.execution_plan(
            "aibid", "standalone", selected_rounds=("R1", "E2E-STANDALONE"),
        )
        by_name = {item.name: item for item in plan}
        self.assertEqual("RUN", by_name["R1"].decision)
        self.assertEqual("RUN", by_name["E2E-STANDALONE"].decision)
        for name in ("R2", "R3", "R4", "R5"):
            self.assertEqual("SKIP", by_name[name].decision)
            self.assertIn("Not selected", by_name[name].reason)

    def test_ios_mediation_has_an_independent_test_device_gate(self):
        opened = []
        self.assertFalse(run_ios_test_suite.confirm_mediation_test_device(
            "mediation", input_fn=lambda _prompt: "no", open_page=opened.append,
        ))
        self.assertTrue(opened and "admob.google.com" in opened[0])
        self.assertTrue(run_ios_test_suite.confirm_mediation_test_device(
            "mediation", input_fn=lambda _prompt: "yes", open_page=lambda _url: None,
        ))
        self.assertTrue(run_ios_test_suite.confirm_mediation_test_device(
            "standalone", input_fn=lambda _prompt: self.fail("Standalone must not prompt"),
        ))

    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / "testcases/testcase_catalog.json").read_text())["testcases"]
        cls.catalog_by_key = {row["key"]: row for row in cls.catalog}

    def test_original_aibid_catalog_and_rounds_remain_available(self):
        self.assertEqual(84, len(self.catalog))
        for round_name in ("R1", "R2", "R3", "R4", "R5"):
            plan = qa_aos.resolve_execution_plan(round_args(round_name))
            self.assertTrue(plan.scenarios)
            self.assertTrue(all(scenario.testcase_keys for scenario in plan.scenarios))
        privacy = next(
            scenario for scenario in qa_aos.resolve_execution_plan(round_args("R5")).scenarios
            if scenario.label == "PRIVACY-DENIED"
        )
        self.assertEqual(tuple(qa_aos.R5_PRIVACY_KEYS), privacy.testcase_keys)

    def test_reen_identity_denied_scenario_is_absent_from_execution_plan(self):
        for test_type in ("reen-static", "reen-dynamic"):
            plan = qa_aos.resolve_execution_plan(round_args("R5", test_type))
            self.assertNotIn("PRIVACY-DENIED", {scenario.label for scenario in plan.scenarios})
        aibid = qa_aos.resolve_execution_plan(round_args("R5", "aibid"))
        self.assertIn("PRIVACY-DENIED", {scenario.label for scenario in aibid.scenarios})

    def test_r5_1_is_aibid_standalone_only(self):
        plan = qa_aos.resolve_execution_plan(round_args("R5-1", "aibid", "standalone"))
        self.assertEqual("R5-1", plan.round_name)
        self.assertEqual(("PRIVACY-DENIED",), tuple(item.label for item in plan.scenarios))
        with self.assertRaises(qa_aos.CaptureError):
            qa_aos.resolve_execution_plan(round_args("R5-1", "aibid", "admob-mediation"))
        with self.assertRaises(qa_aos.CaptureError):
            qa_aos.resolve_execution_plan(round_args("R5-1", "reen-static", "standalone"))

    def test_mediation_r5_blocks_privacy_without_deleting_gaid(self):
        plan = qa_aos.resolve_execution_plan(round_args("R5", "aibid", "admob-mediation"))
        with patch.object(qa_aos, "location_permission_preflight", return_value=(True, "ready", {})):
            resolved = qa_aos.preflight_execution_plan(plan, types.SimpleNamespace(test_type="aibid"))
        privacy = next(item for item in resolved.scenarios if item.label == "PRIVACY-DENIED")
        self.assertEqual("BLOCK", privacy.decision)
        self.assertIn("TestDevice", privacy.reason)

    def test_same_run_standalone_privacy_can_cover_mediation_block(self):
        common = {
            "tc": "tracking-denied", "platform": "aos", "test_type": "aibid",
            "run_group": "run-1", "captured_at": "2026-08-11T10:00:00+08:00",
            "expected": None, "actual": None, "comparison_view": None, "evidence": None,
            "source": ROOT / "blocked-verdicts.json", "coverage_only": False,
        }
        mediation = {**common, "mode_group": "mediation", "status": "BLOCKED", "reason": "safe default"}
        standalone = {
            **common, "mode_group": "standalone", "status": "PASS", "reason": "matched",
            "expected": {"lat": 1}, "actual": {"lat": 1}, "evidence": "bid_decoded.json",
            "source": ROOT / "standalone-verdicts.json", "coverage_only": True,
        }
        linked = page._apply_standalone_privacy_coverage([mediation, standalone])
        result = next(row for row in linked if row["mode_group"] == "mediation")
        self.assertEqual("PASS", result["status"])
        self.assertIn("Standalone R5-1", result["coverage_source"])

    def test_reen_report_excludes_aibid_only_r5_privacy(self):
        for test_type in ("reen-static", "reen-dynamic"):
            for mode in ("standalone", "admob-mediation"):
                applicable = {
                    row["key"] for row in self.catalog
                    if page._catalog_applicable(row, "aos", mode, test_type)
                }
                self.assertNotIn("advertising-id-opt-out", applicable)
                self.assertNotIn("tracking-denied", applicable)
                self.assertIn("dark-mode-enabled", applicable)
        for mode in ("standalone", "admob-mediation"):
            aibid = {
                row["key"] for row in self.catalog
                if page._catalog_applicable(row, "aos", mode, "aibid")
            }
            self.assertIn("advertising-id-opt-out", aibid)
            self.assertIn("tracking-denied", aibid)

    def test_s14_to_s16_are_shared_by_aibid_and_reen(self):
        expected = {
            "standalone-landing": "E2E-S14",
            "standalone-install-attribution": "E2E-S15",
            "standalone-attribution-reconciliation": "E2E-S16",
        }
        for key, display_id in expected.items():
            testcase = self.catalog_by_key[key]
            self.assertEqual(display_id, testcase["display_id"])
            self.assertTrue(all(key in keys for keys in CAMPAIGN_TESTCASES.values()))
        for test_type in CAMPAIGN_PROFILES:
            keys = {
                key for scenario in qa_aos.resolve_execution_plan(
                    round_args("E2E-STANDALONE", test_type)
                ).scenarios for key in scenario.testcase_keys
            }
            self.assertTrue(set(expected).issubset(keys))

    def test_campaign_dictionary_covers_catalog_without_orphans(self):
        catalog_keys = set(self.catalog_by_key)
        assigned_keys = set().union(*CAMPAIGN_TESTCASES.values())
        self.assertEqual(catalog_keys, assigned_keys)

    def test_reen_static_and_dynamic_have_identical_testcases(self):
        self.assertEqual(
            CAMPAIGN_TESTCASES["reen-static"],
            CAMPAIGN_TESTCASES["reen-dynamic"],
        )

    def test_reen_entry_maps_creative_to_separate_report_type(self):
        parser = run_reen_test_suite.build_parser()
        common = [
            "--mode", "standalone", "--cid", "cid",
            "--target-app-package", "target.app",
        ]
        static = run_reen_test_suite.build_runner_arguments(parser.parse_args(["static", *common]))
        dynamic = run_reen_test_suite.build_runner_arguments(parser.parse_args(["dynamic", *common]))
        self.assertIn("reen-static", static)
        self.assertIn("reen-dynamic", dynamic)

    def test_single_integration_entry_maps_mediation_to_admob_e2e(self):
        self.assertEqual(
            "admob-mediation", run_aos_test_suite.INTEGRATION_MODE_ALIASES["mediation"],
        )
        self.assertEqual(
            ["R1", "R2", "R3", "R4", "R5", "E2E-STANDALONE"],
            run_aos_test_suite.suite_rounds("standalone"),
        )
        self.assertEqual(
            ["R1", "R2", "R3", "R4", "R5", "E2E-ADMOB"],
            run_aos_test_suite.suite_rounds("admob-mediation"),
        )
        parser = run_reen_test_suite.build_parser()
        args = parser.parse_args([
            "static", "--mode", "mediation", "--cid", "cid",
            "--target-app-package", "target.app",
        ])
        command = run_reen_test_suite.build_runner_arguments(args)
        self.assertIn("--integration-mode", command)
        self.assertIn("mediation", command)

    def test_report_has_latest_and_catalog_only_and_renders_reen_planned_cards(self):
        document = page.render([], [], [], [], self.catalog)
        self.assertNotIn('id="history-page"', document)
        self.assertNotIn('data-page="history"', document)
        self.assertIn('" TC・已執行 "+executed+"・未執行 "', document)
        self.assertIn('" TC · Executed "+executed+" · Not run "', document)
        self.assertIn('data-manual-override-summary', document)
        self.assertIn('data-manual-summary-reason', document)
        self.assertIn('data-slot="aos:standalone:reen-static"', document)
        reen_start = document.index('<section class="slot-detail" data-slot="aos:standalone:reen-static"')
        reen_end = document.find('<section class="slot-detail"', reen_start + 1)
        if reen_end < 0:
            reen_end = len(document)
        reen_detail = document[reen_start:reen_end]
        self.assertNotIn('data-tc="advertising-id-opt-out"', reen_detail)
        self.assertNotIn('data-tc="tracking-denied"', reen_detail)

    def test_current_e2e_result_reasons_have_chinese_translations(self):
        reasons = (
            "At least one response-specified creative asset was not captured or failed its transport contract.",
            "The response-specified creative assets either loaded successfully in traffic or were proven as rendered cached views in the saved screenshot.",
            "The CTA interaction emitted an xclk whose correlation IDs match the visible impression, and preserved its response.",
            "The traffic lookup key was captured automatically. MMP install-click verification still requires the MMP action query.",
            "The traffic lookup key was captured automatically. MMP re-engagement-click verification still requires the MMP action query.",
            "The traffic lookup key was captured automatically. install attribution recognition still requires Spark/MMP reconciliation.",
            "The traffic lookup key was captured automatically. re-engagement attribution recognition still requires Spark/MMP reconciliation.",
        )
        for reason in reasons:
            self.assertIn(reason, page.DYNAMIC_ZH)

    def test_capture_limit_reasons_have_parameterized_chinese_translations(self):
        interrupted = page._dynamic_bi(
            "Standalone R5-1 was stopped by the user after 21 attempts without capturing "
            "an eligible bid for CID target-cid. The current runner had no attempt or phase "
            "timeout limit, so no comparison result is claimed."
        )
        limited = page._dynamic_bi(
            "No eligible bid for CID target-cid after 20 attempts "
            "(NETWORK_ERROR=1, NO_BID=17, WRONG_CID=2)"
        )
        server = page._dynamic_bi(
            "Appier Server error: 3 consecutive 5xx responses; No eligible bid for CID "
            "target-cid after 3 attempts (SERVER_ERROR=3)"
        )
        self.assertIn("嘗試 21 次後由使用者中止", interrupted)
        self.assertIn("20 次嘗試內未取得有效 Bid", limited)
        self.assertIn("連續 3 次回傳 5xx", server)

    def test_campaign_skip_is_labeled_cannot_run(self):
        card = page._unexecuted_card(
            self.catalog_by_key["tracking-denied"],
            "aos",
            "Campaign capability does not support this scenario",
        )
        self.assertIn("CANNOT RUN", card)
        self.assertIn("不可執行", card)

    def test_execution_plan_skip_is_labeled_not_run(self):
        card = page._unexecuted_card(
            self.catalog_by_key["tracking-denied"],
            "aos",
            {"decision": "SKIP", "reason": "Not selected in this suite's Test Scope"},
        )
        self.assertIn("NOT RUN", card)
        self.assertIn("未執行", card)
        self.assertNotIn("CANNOT RUN", card)

    def test_r5_uses_four_transactional_scenarios(self):
        plan = qa_aos.resolve_execution_plan(round_args("R5"))
        scenarios = {scenario.label: set(scenario.testcase_keys) for scenario in plan.scenarios}
        self.assertEqual(
            {"DISPLAY-HIGH", "DISPLAY-LOW", "SYSTEM-ALT", "PRIVACY-DENIED"},
            set(scenarios),
        )
        self.assertEqual({
            "dark-mode-enabled", "font-scale-maximum",
            "screen-brightness-maximum", "output-volume-maximum",
        }, scenarios["DISPLAY-HIGH"])
        self.assertEqual({
            "screen-brightness-minimum", "output-volume-muted",
        }, scenarios["DISPLAY-LOW"])
        self.assertEqual({
            "battery-saver-enabled", "timezone-changed", "location-permission-denied",
        }, scenarios["SYSTEM-ALT"])
        self.assertFalse(any(
            "battery-saver-enabled" in keys
            and ({"screen-brightness-minimum", "screen-brightness-maximum"} & keys)
            for keys in scenarios.values()
        ))

    def test_r5_missing_location_permission_skips_only_location_testcase(self):
        plan = qa_aos.resolve_execution_plan(round_args("R5"))
        config = types.SimpleNamespace(test_type="aibid")
        with patch.object(
            qa_aos,
            "location_permission_preflight",
            return_value=(False, "Sample App declares no location permission to revoke", {"declared_permissions": []}),
        ), patch.object(
            qa_aos,
            "privacy_scenario_preflight",
            return_value=(True, "ready", {}),
        ):
            resolved = qa_aos.preflight_execution_plan(plan, config)

        system_alt = next(item for item in resolved.scenarios if item.label == "SYSTEM-ALT")
        self.assertEqual("RUN", system_alt.decision)
        self.assertEqual(
            {"location-permission-denied": "Sample App declares no location permission to revoke"},
            system_alt.checks["skipped_testcases"],
        )
        self.assertIn("battery-saver-enabled", system_alt.testcase_keys)
        self.assertIn("timezone-changed", system_alt.testcase_keys)

    def test_network_latency_is_evaluated_from_r2_second_request(self):
        self.assertNotIn(
            "network-latency",
            android_signal_testcases.ROUND_DEFINITIONS["R1"].testcase_keys,
        )
        self.assertIn(
            "network-latency",
            android_signal_testcases.ROUND_DEFINITIONS["R2"].testcase_keys,
        )
        self.assertEqual("R2", self.catalog_by_key["network-latency"]["round"])

    def test_display_numeric_status_survives_settings_navigation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            paths = {
                "SETUP_DISPLAY_SCREENSHOT": directory / "display.png",
                "SETUP_FONT_SCALE_SCREENSHOT": directory / "font.png",
                "SETUP_QUICK_BRIGHTNESS_SCREENSHOT": directory / "quick.png",
                "SETUP_DISPLAY_STATUS": directory / "status.json",
            }

            def fake_adb(_udid, *args, binary=False, **_kwargs):
                command = " ".join(args)
                if command.endswith("wm size"):
                    return "Physical size: 1080x2424"
                if command.endswith("wm density"):
                    return "Physical density: 420"
                if "ro.product.model" in command:
                    return "Pixel 10a"
                if "screen_brightness" in command:
                    return "1"
                if command.endswith("dumpsys display"):
                    return "mLastUserSetScreenBrightness=0.003921569 mLatestIntBrightness=1 mLatestFloatBrightness=0.003921569"
                if "font_scale" in command:
                    return "1.0"
                if "uimode night" in command:
                    return "Night mode: no"
                if binary and "screencap" in command:
                    return b"x" * 1500
                return ""

            patches = [patch.object(evidence_aos, name, value) for name, value in paths.items()]
            with patches[0], patches[1], patches[2], patches[3], \
                    patch.object(evidence_aos, "_open_settings_screenshot", return_value=""), \
                    patch.object(evidence_aos, "_adb", side_effect=fake_adb), \
                    patch.object(evidence_aos.time, "sleep", return_value=None):
                evidence_aos.capture_display_status(types.SimpleNamespace(udid="device"))

            status = json.loads(paths["SETUP_DISPLAY_STATUS"].read_text())
            self.assertEqual(1, status["brightness_raw"])
            self.assertAlmostEqual(1 / 255, status["screen_brightness"])
            self.assertFalse(status["visual_evidence"]["display_page"])
            self.assertTrue(status["visual_evidence"]["quick_settings"])
            self.assertTrue(paths["SETUP_DISPLAY_SCREENSHOT"].is_file())

    def test_evidence_provider_failure_is_isolated(self):
        def broken(_config):
            raise RuntimeError("one screenshot failed")

        def good_after(folder):
            (Path(folder) / "good.txt").write_text("ok")

        providers = {
            evidence_aos.BID: evidence_aos.EvidenceProvider(),
            "broken": evidence_aos.EvidenceProvider(before_bid=broken),
            "good": evidence_aos.EvidenceProvider(after_bid=good_after),
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            evidence_aos.EVIDENCE_CAPTURES, providers, clear=True,
        ):
            folder = evidence_aos.collect(
                object(), (evidence_aos.BID, "broken", "good"),
                lambda setup: (setup(), Path(directory))[1],
            )
            self.assertEqual("ok", (folder / "good.txt").read_text())
            errors = json.loads((folder / "evidence-errors.json").read_text())
            self.assertEqual("before_bid", errors["providers"]["broken"]["phase"])

    def test_e2e_recording_requires_complete_mp4_moov_atom(self):
        def box(kind, payload=b""):
            return (8 + len(payload)).to_bytes(4, "big") + kind + payload

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            valid = directory / "valid.mp4"
            valid.write_bytes(box(b"ftyp", b"isom") + box(b"mdat", b"video") + box(b"moov", b"index"))
            incomplete = directory / "incomplete.mp4"
            incomplete.write_bytes(box(b"ftyp", b"isom") + box(b"mdat", b"video"))
            truncated = directory / "truncated.mp4"
            truncated.write_bytes((100).to_bytes(4, "big") + b"mdat" + b"short")

            self.assertTrue(qa_aos._mp4_has_moov_atom(valid))
            self.assertFalse(qa_aos._mp4_has_moov_atom(incomplete))
            self.assertFalse(qa_aos._mp4_has_moov_atom(truncated))

    def test_mediation_requires_explicit_test_device_confirmation(self):
        environment = {}
        self.assertFalse(qa_aos.confirm_mediation_test_device(
            "admob-mediation", environment=environment, input_fn=lambda _prompt: "no",
            advertising_id="11111111-2222-3333-4444-555555555555", open_page=lambda _url: True,
        ))
        self.assertNotIn(qa_aos.MEDIATION_TEST_DEVICE_CONFIRMED, environment)
        self.assertTrue(qa_aos.confirm_mediation_test_device(
            "admob-mediation", environment=environment, input_fn=lambda _prompt: "yes",
            advertising_id="11111111-2222-3333-4444-555555555555", open_page=lambda _url: True,
        ))
        self.assertEqual("1", environment[qa_aos.MEDIATION_TEST_DEVICE_CONFIRMED])
        self.assertIn("developers.google.com/admob", qa_aos.ADMOB_TEST_DEVICE_GUIDE)
        self.assertEqual("https://appier.atlassian.net/wiki/x/l4LbNwE", qa_aos.APPIER_ADMOB_LOGIN_GUIDE)
        self.assertIn("admob.google.com/v2/settings/test-devices/list", qa_aos.ADMOB_TEST_DEVICE_PAGE)
        self.assertTrue(qa_aos.confirm_mediation_test_device(
            "admob-mediation", environment=environment,
            input_fn=lambda _prompt: self.fail("suite confirmation must not prompt each child Round"),
        ))

    def test_standalone_does_not_prompt_for_mediation_confirmation(self):
        self.assertTrue(qa_aos.confirm_mediation_test_device(
            "standalone", environment={},
            input_fn=lambda _prompt: self.fail("Standalone must not show the Mediation warning"),
        ))

    def test_default_language_uses_primary_android_system_locale(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "device-context.json").write_text(json.dumps({
                "device_locale": "en-JP",
                "app_locale": "en-US",
                "lang": "en",
                "langb_system": "en-JP",
                "actual": {"lang": "en", "req_langb": "en-US", "langb": "en-US"},
            }))
            iso = android_signal_testcases.validate_default_language_iso(folder)
            bcp47 = android_signal_testcases.validate_default_language_bcp47(folder)
            self.assertEqual("PASS", iso["status"])
            self.assertEqual("FAILED", bcp47["status"])
            self.assertEqual({"langb": "en-JP"}, bcp47["expected"])

    def test_network_latency_accepts_probe_from_earlier_round_in_same_run(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory)
            r1 = evidence / "AOS_STANDALONE_AIBID_CID_x_R1_run-one" / "TRACKING"
            r2 = evidence / "AOS_STANDALONE_AIBID_CID_x_R2_run-one" / "SECOND"
            r1.mkdir(parents=True)
            r2.mkdir(parents=True)
            for folder, round_name in ((r1, "R1"), (r2, "R2")):
                (folder / "summary.json").write_text(json.dumps({
                    "test_run_id": "run-one", "test_round": round_name,
                }))
            (r1 / "proxy-events.jsonl").write_text(json.dumps({
                "phase": "response", "method": "HEAD", "status": 200,
                "url": "https://cr.adsappier.com/4QGDNtuHG/icon/Info.svg",
            }) + "\n")
            (r2 / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"latency": 38}}}},
            }))

            verdict = android_signal_testcases.validate_network_latency(r2)

            self.assertEqual("PASS", verdict["status"])
            self.assertEqual(38, verdict["actual"]["latency_ms"])
            self.assertIn("R1_run-one", verdict["actual"]["probe_response"]["evidence_file"])
            self.assertEqual("R1", verdict["actual"]["probe_response"]["source_round"])
            self.assertFalse(verdict["actual"]["probe_response"]["same_round"])
            self.assertTrue(verdict["actual"]["probe_response"]["same_test_run"])
            self.assertIn("same TEST_RUN_ID", verdict["actual"]["probe_response"]["reuse_reason"])

    def test_report_summarizes_and_translates_missing_evidence_errors(self):
        rendered = page._dynamic_bi(
            "Validator error after execution: [Errno 2] No such file or directory: "
            "'/private/evidence/SYSTEM-ALT/location-permission-status.json'"
        )
        self.assertIn("Evidence capture did not produce location-permission-status.json", rendered)
        self.assertIn("Evidence 擷取未產生 location-permission-status.json", rendered)
        self.assertNotIn("/private/evidence", rendered)

    def test_aos_missing_signal_evidence_is_blocked(self):
        testcase = types.SimpleNamespace(
            key="location-permission-denied",
            title="Location Permission Denied",
            description="Captured permission state matches the payload.",
        )
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "summary.json").write_text("{}")
            missing = folder / "location-permission-status.json"
            error = FileNotFoundError(2, "No such file or directory", str(missing))

            verdict = qa_aos.blocked_validator_verdict(testcase, error, folder)

        self.assertEqual("BLOCKED", verdict["status"])
        self.assertIsNone(verdict.get("expected"))
        self.assertEqual("location-permission-status.json", verdict["actual"]["missing_artifact"])

    def test_aos_e2e_missing_proxy_evidence_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "summary.json").write_text(json.dumps({
                "test_type": "aibid", "cid": "target-cid",
                "app_package": "com.example.app",
            }))

            rows = {row["tc"]: row for row in validate_android_e2e(folder)}

        self.assertEqual("BLOCKED", rows["standalone-appier-ad-request"]["status"])
        self.assertEqual("BLOCKED", rows["standalone-creative-assets"]["status"])
        self.assertEqual("BLOCKED", rows["standalone-native-render"]["status"])

    def test_aos_e2e_preserved_bad_response_is_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "summary.json").write_text(json.dumps({
                "test_type": "aibid", "cid": "target-cid",
                "app_package": "com.example.app",
            }))
            (folder / "bid_raw.json").write_text(json.dumps({"zone_id": "zone"}))
            (folder / "bid_response.json").write_text(json.dumps({"adUnits": []}))
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"app": {
                    "bundle": "com.example.app", "sdk_version": "1.0",
                }}},
            }))
            events = (
                {"kind": "bid", "phase": "request", "method": "POST", "flow_id": "flow-1", "url": "https://adx.apx.appier.net/v2/sdk/aos/ad"},
                {"kind": "bid", "phase": "response", "flow_id": "flow-1", "status": 200},
            )
            (folder / "proxy-events.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events)
            )

            rows = {row["tc"]: row for row in validate_android_e2e(folder)}

        self.assertEqual("FAILED", rows["standalone-appier-ad-request"]["status"])

    def test_report_translates_common_execution_failures(self):
        appium = page._dynamic_bi(
            "R5 SYSTEM-ALT failed at Evidence capture: Cannot launch Appium session: connection refused"
        )
        self.assertIn("因 Appium 無法使用", appium)
        self.assertNotIn("connection refused", appium)
        self.assertIn(
            "R3 冷啟動後沒有取得可用的 Bid／曝光",
            page._dynamic_bi("R3 cold-start: no eligible bid/impression"),
        )

    def test_ad_capture_defaults_to_twenty_attempts(self):
        args = qa_aos.build_parser().parse_args(["capture"])
        self.assertEqual(20, args.max_attempts)

    def test_ineligible_bid_reasons_are_operator_facing(self):
        target = "target-cid"
        self.assertEqual("NO_BID", qa_aos.classify_ineligible_bid(None, "204", None, target))
        self.assertEqual("SERVER_ERROR", qa_aos.classify_ineligible_bid(None, "503", None, target))
        self.assertEqual("REQUEST_REJECTED", qa_aos.classify_ineligible_bid(None, "401", None, target))
        self.assertEqual("NETWORK_ERROR", qa_aos.classify_ineligible_bid(None, None, None, target))
        self.assertEqual(
            "WRONG_CID",
            qa_aos.classify_ineligible_bid({}, "200", {"cid": "another-cid"}, target),
        )
        self.assertEqual("INVALID_RESPONSE", qa_aos.classify_ineligible_bid({}, "200", None, target))

    def _capture_config(self, evidence_dir, run_id="run-one", max_attempts=20):
        return qa_aos.CaptureConfig(
            app_package="com.example.app", app_activity="com.example.app.MainActivity",
            test_mode="standalone", test_type="aibid", test_cid="target-cid",
            target_app_package="", test_round="R1", trigger_text="Native",
            tab_text="Appier SDK", udid="device", executor="test",
            evidence_dir=Path(evidence_dir), bid_timeout=0.01, retry_delay=0,
            max_attempts=max_attempts, phase_timeout=0, accept_request=False,
            test_run_id=run_id, test_run_started_at="2026-08-11T10:00:00+08:00",
        )

    def _capture_patches(self, responses):
        driver = types.SimpleNamespace(
            activate_app=lambda _package: None, back=lambda: None, quit=lambda: None,
        )
        return (
            patch.object(qa_aos, "adb", return_value=""),
            patch.object(qa_aos, "LogcatRecorder", side_effect=lambda _udid: nullcontext()),
            patch.object(qa_aos, "create_driver", return_value=driver),
            patch.object(qa_aos, "select_tab"),
            patch.object(qa_aos, "tap_placement", return_value=True),
            patch.object(qa_aos, "wait_for_bid", side_effect=responses),
            patch.object(qa_aos, "clear_detector_state"),
            patch.object(qa_aos.time, "sleep"),
        )

    def test_capture_stops_at_twenty_and_writes_classified_summary(self):
        responses = [(None, "204", None, None)] * 17 + [
            ({}, "200", {"cid": "other"}, "proxy"),
            (None, None, None, None),
            ({}, "200", None, "proxy"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = self._capture_config(directory)
            patches = self._capture_patches(responses)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
                with self.assertRaises(qa_aos.CaptureError) as caught:
                    qa_aos.capture(config, "LIMIT")
            folder = Path(caught.exception.evidence_folder)
            summary = json.loads((folder / "capture-attempt-summary.json").read_text())
            self.assertEqual(20, summary["attempts"])
            self.assertEqual({
                "NO_BID": 17, "WRONG_CID": 1,
                "NETWORK_ERROR": 1, "INVALID_RESPONSE": 1,
            }, summary["counts"])

    def test_capture_stops_after_three_consecutive_server_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._capture_config(directory)
            patches = self._capture_patches([(None, "503", None, None)] * 3)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
                with self.assertRaisesRegex(qa_aos.CaptureError, "3 consecutive 5xx") as caught:
                    qa_aos.capture(config, "SERVER")
            summary = json.loads(
                (Path(caught.exception.evidence_folder) / "capture-attempt-summary.json").read_text()
            )
            self.assertEqual(3, summary["attempts"])
            self.assertEqual({"SERVER_ERROR": 3}, summary["counts"])

    def test_missing_ui_trigger_is_counted_and_stops_at_twenty(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._capture_config(directory)
            patches = self._capture_patches([])
            with patches[0], patches[1], patches[2], patches[3], patch.object(
                qa_aos, "tap_placement", return_value=False,
            ), patches[6], patches[7]:
                with self.assertRaises(qa_aos.CaptureError) as caught:
                    qa_aos.capture(config, "NO-TRIGGER")
            summary = json.loads(
                (Path(caught.exception.evidence_folder) / "capture-attempt-summary.json").read_text()
            )
            self.assertEqual(20, summary["attempts"])
            self.assertEqual({"UI_TRIGGER_MISSING": 20}, summary["counts"])

    def test_keyboard_interrupt_is_preserved_as_user_interrupted_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._capture_config(directory)
            patches = self._capture_patches([KeyboardInterrupt()])
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
                with self.assertRaises(qa_aos.UserInterrupted) as caught:
                    qa_aos.capture(config, "INTERRUPTED")
            summary = json.loads((Path(caught.exception.evidence_folder) / "summary.json").read_text())
            self.assertEqual("INTERRUPTED", summary["result"])
            self.assertEqual("Stopped by user", summary["error"])
            plan = types.SimpleNamespace(scenarios=(
                qa_aos.ScenarioPlan("TRACKING-ALLOWED", ("advertising-id",)),
            ))
            qa_aos.record_interrupted_verdicts([caught.exception.evidence_folder], plan, str(caught.exception))
            verdict = json.loads(
                (Path(caught.exception.evidence_folder) / "verdicts.json").read_text()
            )["verdicts"][0]
            self.assertEqual("BLOCKED", verdict["status"])
            self.assertIn("Stopped by user", verdict["reason"])

    def test_round_directories_are_isolated_by_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            one = qa_aos.round_directory(self._capture_config(directory, "run-one"))
            two = qa_aos.round_directory(self._capture_config(directory, "run-two"))
            self.assertNotEqual(one, two)
            self.assertIn("run-one", one.name)
            self.assertIn("run-two", two.name)

    def test_launcher_activity_is_resolved_and_wrong_internal_activity_is_rejected(self):
        output = "priority=0 preferredOrder=0\ncom.example.app/.MainActivity"
        with patch.object(qa_aos, "adb", return_value=output):
            self.assertEqual(
                "com.example.app.MainActivity",
                qa_aos.resolve_launcher_activity("device", "com.example.app"),
            )
            with self.assertRaises(qa_aos.InfrastructureError):
                qa_aos.resolve_launcher_activity(
                    "device", "com.example.app", "com.example.app.InternalActivity",
                )

    def _suite_config(self):
        return {
            "app_package": "com.example.app", "app_activity": "com.example.app.MainActivity",
            "test_mode": "standalone", "test_type": "aibid", "test_cid": "target-cid",
            "target_app_package": "", "trigger_text": "Native", "tab_text": "", "udid": "device",
        }

    def test_suite_stops_later_rounds_after_infrastructure_exit(self):
        plans = [
            (types.SimpleNamespace(round_name="R1"), None, "standalone"),
            (types.SimpleNamespace(round_name="R2"), None, "standalone"),
        ]
        processes = []
        class Process:
            def wait(self, timeout=None): return 2
        def factory(*_args, **_kwargs):
            process = Process(); processes.append(process); return process
        failures, interrupted = run_aos_test_suite.execute_plans(
            plans, self._suite_config(), {"TEST_RUN_ID": "run-one"}, factory,
        )
        self.assertEqual(["R1"], failures)
        self.assertFalse(interrupted)
        self.assertEqual(1, len(processes))

    def test_suite_ctrl_c_signals_child_and_publish_still_runs(self):
        plan = [(types.SimpleNamespace(round_name="R1"), None, "standalone")]
        class Process:
            def __init__(self): self.waits = 0; self.signal = None
            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1: raise KeyboardInterrupt()
                return 130
            def send_signal(self, value): self.signal = value
            def terminate(self): self.terminated = True
        process = Process()
        failures, interrupted = run_aos_test_suite.execute_plans(
            plan, self._suite_config(), {"TEST_RUN_ID": "run-one"},
            lambda *_args, **_kwargs: process,
        )
        self.assertTrue(interrupted)
        self.assertEqual(["R1"], failures)
        self.assertIsNone(process.signal)
        publish_calls = []
        code = run_aos_test_suite.finalize_suite(
            "run-one", failures, interrupted, publish=True,
            runner=lambda *args, **kwargs: publish_calls.append((args, kwargs)),
        )
        self.assertEqual(130, code)
        self.assertEqual(1, len(publish_calls))
        self.assertIn("page.py", " ".join(publish_calls[0][0][0]))


if __name__ == "__main__":
    unittest.main()
