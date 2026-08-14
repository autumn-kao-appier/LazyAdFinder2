import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import evidence_ios
import qa_ios
from testcases.e2e.ios_e2e_baseline import validate_bundle as validate_ios_e2e
from testcases.ios_signal_testcases import TC_DEFINITIONS


class IOSEvidenceContractTests(unittest.TestCase):
    def test_missing_sample_app_surface_is_explicit_and_never_uses_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch.dict("os.environ", {
                "IOS_QA_EVIDENCE_FILE": str(folder / "absent.json"),
                "IOS_QA_EVIDENCE_SCREENSHOT": str(folder / "absent.png"),
            }):
                evidence_ios.materialize_ios_qa_evidence(folder)
            document = json.loads((folder / "ios-qa-evidence.json").read_text())
            self.assertEqual(document["status"], "UNAVAILABLE")
            self.assertNotIn("values", document)

    def test_idfv_cannot_pass_without_visible_sample_app_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = "82bd86b3-8f29-0da1-fc71-d24ce7c15f77"
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ifv": value}}},
                "ext": {"plaintext": {"device": {"ifv": value}}},
            }))
            (folder / "ios-qa-evidence.json").write_text(json.dumps({
                "status": "UNAVAILABLE",
            }))
            verdict = TC_DEFINITIONS["app-set-id"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")
            self.assertIn("cannot verify itself", verdict["reason"])

    def test_idfv_passes_only_with_matching_visible_sample_app_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = "82bd86b3-8f29-0da1-fc71-d24ce7c15f77"
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ifv": value}}},
                "ext": {"plaintext": {"device": {"ifv": value}}},
            }))
            (folder / "ios-qa-evidence.json").write_text(json.dumps({
                "status": "CAPTURED", "values": {"idfv": value.upper()},
            }))
            (folder / "ios-qa-evidence.png").write_bytes(b"visible")
            verdict = TC_DEFINITIONS["app-set-id"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")

    def test_settings_provider_preserves_before_and_mutated_screenshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "bundle"
            folder.mkdir()
            state = root / "state.json"
            before = root / "before.png"
            mutated = root / "mutated.png"
            state.write_text(json.dumps({
                "scenario": "DISPLAY-DARK",
                "stages": {
                    "before": {"value": "Light"},
                    "mutated": {"value": "Dark"},
                    "restored": {"status": "PENDING"},
                },
            }))
            before.write_bytes(b"before")
            mutated.write_bytes(b"mutated")
            with patch.dict("os.environ", {
                "IOS_SETTINGS_STATE_FILE": str(state),
                "IOS_SETTINGS_BEFORE_SCREENSHOT": str(before),
                "IOS_SETTINGS_SCREENSHOT": str(mutated),
            }):
                evidence_ios.materialize_ios_settings_state(folder)
            self.assertEqual((folder / "ios-settings-before.png").read_bytes(), b"before")
            self.assertEqual((folder / "ios-settings-state.png").read_bytes(), b"mutated")

    def test_e2e_cannot_pass_request_or_render_without_complete_session_and_valid_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "summary.json").write_text(json.dumps({"test_type": "aibid", "cid": "cid"}))
            (folder / "bid_response.json").write_text(json.dumps({
                "adUnits": [{"ad": {"native": {"title": "Ad"}}}],
            }))
            (folder / "bid_raw.json").write_text(json.dumps({"zone_id": "zone"}))
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"app": {"bundle": "app", "sdk_version": "1"}}},
            }))
            (folder / "e2e-interactions.json").write_text(json.dumps({
                "recording": {"saved": True, "valid_mp4": False},
                "timeline": [{"stage": "rendered-ad", "outcome": "CAPTURED"}],
            }))
            (folder / "visual-review.json").write_text(json.dumps({"passed": True}))
            (folder / "ad-before-interactions.png").write_bytes(b"image")
            rows = {row["tc"]: row for row in validate_ios_e2e(folder)}
            self.assertEqual(rows["standalone-appier-ad-request"]["status"], "FAILED")
            self.assertEqual(rows["standalone-native-render"]["status"], "FAILED")

    def test_e2e_proxy_preflight_rejects_missing_charles_before_ui(self):
        with patch.object(qa_ios, "_tcp_listening", return_value=False):
            with self.assertRaisesRegex(qa_ios.CaptureError, "Charles is not listening"):
                qa_ios.ensure_e2e_proxy_ready()

    def test_e2e_cold_launch_terminates_before_reactivating_sample_app(self):
        driver = MagicMock()
        with patch.object(qa_ios.time, "sleep") as sleep:
            qa_ios._cold_launch_for_e2e(driver, "com.appier.Random")
        self.assertEqual(
            [
                call.terminate_app("com.appier.Random"),
                call.activate_app("com.appier.Random"),
            ],
            driver.mock_calls,
        )
        self.assertEqual([call(1), call(2)], sleep.mock_calls)

    def test_e2e_cta_locator_uses_response_text_in_any_language(self):
        chinese_cta = MagicMock()
        chinese_cta.is_displayed.return_value = True
        chinese_cta.is_enabled.return_value = True
        chinese_cta.get_attribute.side_effect = lambda name: "立即下載" if name in {"name", "label"} else None
        driver = MagicMock()
        driver.find_elements.return_value = [chinese_cta]
        self.assertIs(chinese_cta, qa_ios._button_with_text(driver, "立即下載"))

    def test_e2e_privacy_locator_accepts_unlabeled_top_right_icon(self):
        app_icon = MagicMock()
        app_icon.is_displayed.return_value = True
        app_icon.rect = {"x": 20, "y": 146, "width": 40, "height": 41}
        privacy = MagicMock()
        privacy.is_displayed.return_value = True
        privacy.rect = {"x": 335, "y": 156, "width": 20, "height": 21}
        driver = MagicMock()
        driver.get_window_size.return_value = {"width": 375, "height": 812}
        driver.find_elements.return_value = [app_icon, privacy]
        self.assertIs(privacy, qa_ios._privacy_icon(driver))

    def test_e2e_privacy_return_closes_in_app_browser_instead_of_history_back(self):
        driver = MagicMock()
        driver.find_elements.return_value = []
        driver.get_window_size.return_value = {"width": 375, "height": 812}
        with patch.object(qa_ios, "_active_app", return_value={"bundle_id": "com.appier.Random"}):
            method = qa_ios._close_privacy_destination(driver, "com.appier.Random")
        self.assertEqual("safari-close-coordinate", method)
        driver.execute_script.assert_called_once_with(
            "mobile: tap", {"x": 375 * 0.69, "y": 812 - 48},
        )
        driver.back.assert_not_called()

    def test_e2e_privacy_return_reactivates_sample_app_after_external_safari(self):
        driver = MagicMock()
        with patch.object(qa_ios, "_active_app", return_value={"bundle_id": "com.apple.mobilesafari"}):
            method = qa_ios._close_privacy_destination(driver, "com.appier.Random")
        self.assertEqual("reactivate-sample-app", method)
        driver.activate_app.assert_called_once_with("com.appier.Random")


if __name__ == "__main__":
    unittest.main()
