import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
