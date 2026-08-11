import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import evidence_aos
import page
import qa_aos
import run_aos_test_suite
import run_reen_test_suite
from campaign_profiles import CAMPAIGN_PROFILES
from campaign_testcases import CAMPAIGN_TESTCASES
from testcases import android_signal_testcases


ROOT = Path(__file__).resolve().parents[1]


def round_args(name, test_type="aibid", mode="standalone"):
    return types.SimpleNamespace(
        command="round",
        name=name,
        test_mode=mode,
        test_type=test_type,
    )


class CampaignContractTests(unittest.TestCase):
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
        self.assertIn('data-slot="aos:standalone:reen-static"', document)
        reen_start = document.index('<section class="slot-detail" data-slot="aos:standalone:reen-static"')
        reen_end = document.find('<section class="slot-detail"', reen_start + 1)
        if reen_end < 0:
            reen_end = len(document)
        reen_detail = document[reen_start:reen_end]
        self.assertNotIn('data-tc="advertising-id-opt-out"', reen_detail)
        self.assertNotIn('data-tc="tracking-denied"', reen_detail)

    def test_campaign_skip_is_labeled_cannot_run(self):
        card = page._unexecuted_card(
            self.catalog_by_key["tracking-denied"],
            "aos",
            "Campaign capability does not support this scenario",
        )
        self.assertIn("CANNOT RUN", card)
        self.assertIn("不可執行", card)

    def test_r5_state_testcases_are_independent_scenarios(self):
        plan = qa_aos.resolve_execution_plan(round_args("R5"))
        state_keys = {
            "dark-mode-enabled", "font-scale-maximum", "screen-brightness-minimum",
            "output-volume-muted", "battery-saver-enabled",
            "screen-brightness-maximum", "output-volume-maximum",
        }
        owners = {
            key: scenario.label
            for scenario in plan.scenarios
            for key in scenario.testcase_keys
            if key in state_keys
        }
        self.assertEqual(state_keys, set(owners))
        self.assertEqual(len(state_keys), len(set(owners.values())))

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


if __name__ == "__main__":
    unittest.main()
