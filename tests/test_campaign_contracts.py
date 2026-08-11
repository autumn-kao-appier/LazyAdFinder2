import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import evidence_aos
import page
import qa_aos
import run_reen_test_suite
from campaign_profiles import CAMPAIGN_PROFILES
from campaign_testcases import CAMPAIGN_TESTCASES


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

    def test_only_reen_identity_denied_scenario_is_skipped_by_campaign_capability(self):
        for test_type in ("reen-static", "reen-dynamic"):
            ready, _reason, checks = qa_aos.privacy_scenario_preflight(
                types.SimpleNamespace(test_type=test_type)
            )
            self.assertFalse(ready)
            self.assertFalse(checks["privacy_denied_identity"])
        ready, _reason, _checks = qa_aos.privacy_scenario_preflight(
            types.SimpleNamespace(test_type="aibid")
        )
        self.assertTrue(ready)

    def test_reen_report_plans_all_shared_signal_cards_including_r5_privacy(self):
        for test_type in CAMPAIGN_PROFILES:
            applicable = {
                row["key"] for row in self.catalog
                if page._catalog_applicable(row, "aos", "standalone", test_type)
            }
            self.assertIn("advertising-id-opt-out", applicable)
            self.assertIn("tracking-denied", applicable)
            self.assertIn("dark-mode-enabled", applicable)

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
        self.assertIn('data-tc="advertising-id-opt-out"', reen_detail)
        self.assertIn('data-tc="tracking-denied"', reen_detail)

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


if __name__ == "__main__":
    unittest.main()
