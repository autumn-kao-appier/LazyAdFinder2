import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

import evidence_ios
import qa_ios
from testcases.e2e.ios_e2e_baseline import validate_bundle as validate_ios_e2e
from testcases.ios_signal_testcases import TC_DEFINITIONS
from testcases.ipv6_refresh_testcases import validate_sequence as validate_ipv6_sequence


class IOSEvidenceContractTests(unittest.TestCase):
    def test_r3_proves_old_process_absent_before_first_request(self):
        config = MagicMock()
        tokens = ("com.appier.random",)
        termination = {"terminated_pid": 41, "terminated_pid_confirmed": True}
        with patch.object(qa_ios, "_ios_app_pid", return_value=41) as pid, \
                patch.object(qa_ios, "_terminate_app", return_value=termination) as terminate:
            result = qa_ios._prepare_r3_cold_start(config, tokens)
        pid.assert_called_once_with(config, tokens)
        terminate.assert_called_once_with(config, tokens, 41)
        self.assertEqual(termination, result)

    def test_r3_refuses_to_label_unproven_launch_as_cold_start(self):
        with self.assertRaisesRegex(qa_ios.CaptureError, "process identity"):
            qa_ios._prepare_r3_cold_start(MagicMock(), ())

    def test_ios_r4_requires_matching_appier_ipv6_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ipv6": "2001:db8::1", "conntype": 2}}},
            }))
            without_probe = validate_ipv6_sequence([folder], {"platform": "ios"})
            self.assertTrue(all(row["status"] == "BLOCKED" for row in without_probe))
            (folder / "ipv6-net-probe-response.json").write_text(json.dumps({"ipv6": "2001:db8::1"}))
            with_probe = validate_ipv6_sequence([folder], {"platform": "ios"})
            self.assertEqual("PASS", with_probe[0]["status"])
            self.assertEqual("PASS", with_probe[1]["status"])

    def test_r5_group_uses_one_shared_bid_and_keeps_per_tc_mutation_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            state = {
                "automation": "Appium", "screenshot_saved": True,
                "before": .5, "after": 0, "desired": 0,
                "stages": {"before": {}, "mutated": {}, "restored": {}},
            }
            brightness = MagicMock()
            brightness.key = "screen-brightness-minimum"
            brightness.evidence = ("bid",)
            brightness.validate.return_value = {"tc": "screen-brightness-minimum", "status": "PASS"}
            volume = MagicMock()
            volume.key = "output-volume-muted"
            volume.evidence = ("bid",)
            volume.title = "Output Volume — Muted"
            volume.description = "muted"
            config = MagicMock(
                selected_scenarios=("DISPLAY-LOW",), test_type="reen-static",
                test_mode="standalone",
            )
            with patch.dict(qa_ios.TC_DEFINITIONS, {
                    "screen-brightness-minimum": brightness,
                    "output-volume-muted": volume,
                }, clear=True), patch.object(
                    qa_ios, "_mutate_ios_state",
                    side_effect=[state, qa_ios.CaptureError("volume unavailable")],
                ) as mutate, patch.object(
                    qa_ios, "collect_evidence", return_value=folder,
                ) as collect, patch.object(
                    qa_ios, "_restore_ios_state", return_value=(True, ""),
                ), patch.object(qa_ios, "_render_r5_cards"):
                result = qa_ios.run_r5_round(config)
            self.assertEqual([folder], result)
            self.assertEqual(2, mutate.call_count)
            collect.assert_called_once()
            document = json.loads((folder / "ios-settings-state.json").read_text())
            self.assertTrue(document["shared_bid"])
            self.assertIn("screen-brightness-minimum", document["operations"])
            self.assertIn("output-volume-muted", document["mutation_errors"])

    def test_ios_recording_uses_appium_camel_case_h264_contract(self):
        driver = MagicMock()
        qa_ios._start_screen_recording(driver)
        driver.start_recording_screen.assert_called_once_with(
            videoType="libx264",
            videoQuality="medium",
            videoFps=10,
            pixelFormat="yuv420p",
        )

    def test_mjpeg_recording_is_preserved_and_transcoded_for_browser(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary) / "e2e-interactions.mp4"
            raw.write_bytes(b"raw-mjpeg")
            browser = raw.with_name("e2e-interactions-browser.mp4")

            def probe(path):
                if Path(path) == raw:
                    return {"path": raw.name, "codec": "mjpeg", "pixel_format": "yuvj420p", "duration_seconds": 4.0}
                return {"path": browser.name, "codec": "h264", "pixel_format": "yuv420p", "duration_seconds": 10.0}

            def transcode(command, **_kwargs):
                browser.write_bytes(b"derived-h264")
                result = MagicMock()
                result.returncode = 0
                result.stderr = ""
                result.stdout = ""
                return result

            with patch.object(qa_ios, "_recording_metadata", side_effect=probe), \
                    patch.object(qa_ios.shutil, "which", return_value="/usr/local/bin/ffmpeg"), \
                    patch.object(qa_ios.subprocess, "run", side_effect=transcode) as run:
                metadata = qa_ios.materialize_browser_recording(raw, expected_duration=10.0)

            self.assertEqual(raw.read_bytes(), b"raw-mjpeg")
            self.assertEqual(browser.read_bytes(), b"derived-h264")
            self.assertTrue(metadata["browser_compatible"])
            self.assertTrue(metadata["transcoded"])
            self.assertEqual(metadata["browser_path"], browser.name)
            command = run.call_args.args[0]
            self.assertIn("libx264", command)
            self.assertIn("yuv420p", command)
            self.assertIn("setpts=2.50000000*PTS", " ".join(command))

    def test_idfa_cannot_pass_without_visible_get_my_idfa_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = "82bd86b3-8f29-0da1-fc71-d24ce7c15f77"
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ia": value}}},
                "ext": {"plaintext": {"device": {"ia": value}}},
            }))
            (folder / "ios-idfa-state.json").write_text(json.dumps({"status": "UNAVAILABLE"}))
            verdict = TC_DEFINITIONS["advertising-id"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")

    def test_idfa_passes_with_matching_visible_value_and_authorized_att(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = "82bd86b3-8f29-0da1-fc71-d24ce7c15f77"
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ia": value}}},
                "ext": {"plaintext": {"device": {"ia": value}}},
            }))
            (folder / "ios-idfa-state.json").write_text(json.dumps({
                "status": "CAPTURED", "value": value.upper(),
            }))
            (folder / "ios-idfa.png").write_bytes(b"visible")
            (folder / "ios-settings-state.json").write_text(json.dumps({
                "att": {"authorization": "authorized"},
            }))
            (folder / "ios-settings-state.png").write_bytes(b"visible tracking")
            verdict = TC_DEFINITIONS["advertising-id"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")

    def test_idfa_blocks_when_native_att_evidence_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = "82bd86b3-8f29-0da1-fc71-d24ce7c15f77"
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ia": value}}},
                "ext": {"plaintext": {"device": {"ia": value}}},
            }))
            (folder / "ios-idfa-state.json").write_text(json.dumps({"status": "CAPTURED", "value": value}))
            (folder / "ios-idfa.png").write_bytes(b"visible")
            (folder / "ios-settings-state.json").write_text(json.dumps({
                "status": "UNAVAILABLE", "att": {"authorization": None},
                "reason": "Tracking page unavailable",
            }))
            verdict = TC_DEFINITIONS["advertising-id"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")

    def test_get_my_idfa_zero_value_is_unavailable_not_product_failure(self):
        config = MagicMock()
        driver = MagicMock()
        driver.page_source = '<XCUIElementTypeStaticText value="00000000-0000-0000-0000-000000000000"/>'
        driver.find_elements.return_value = []
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            screenshot = Path(temporary) / "idfa.png"
            driver.save_screenshot.side_effect = lambda path: Path(path).write_bytes(b"image") or True
            with patch.dict("os.environ", {
                "IOS_IDFA_STATE_FILE": str(state),
                "IOS_IDFA_SCREENSHOT": str(screenshot),
            }), patch.object(qa_ios, "create_driver", return_value=driver), patch.object(qa_ios.time, "sleep"):
                document = qa_ios.capture_visible_idfa(config)
            self.assertEqual(document["status"], "UNAVAILABLE")
            self.assertIn("zero IDFA", document["reason"])

    def test_get_my_idfa_permission_alert_is_not_auto_accepted(self):
        config = MagicMock()
        driver = MagicMock()
        driver.page_source = '<XCUIElementTypeStaticText value="82bd86b3-8f29-0da1-fc71-d24ce7c15f77"/>'
        driver.find_elements.return_value = [MagicMock()]
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            screenshot = Path(temporary) / "idfa.png"
            driver.save_screenshot.side_effect = lambda path: Path(path).write_bytes(b"image") or True
            with patch.dict("os.environ", {
                "IOS_IDFA_STATE_FILE": str(state),
                "IOS_IDFA_SCREENSHOT": str(screenshot),
            }), patch.object(qa_ios, "create_driver", return_value=driver) as create, patch.object(qa_ios.time, "sleep"):
                document = qa_ios.capture_visible_idfa(config)
            self.assertEqual(document["status"], "UNAVAILABLE")
            self.assertIn("visible permission/system alert", document["reason"])
            create.assert_called_once_with(
                config, bundle_id="com.pag3dev.GetMyIDFA", auto_accept_alerts=False,
            )

    def test_tracking_allowed_passes_with_visible_switch_idfa_and_inverse_lat(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = "82bd86b3-8f29-0da1-fc71-d24ce7c15f77"
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ia": value, "lat": 0}}},
                "ext": {"plaintext": {"device": {"ia": value}}},
            }))
            (folder / "ios-tracking-allowed-status.json").write_text(json.dumps({
                "status": "CAPTURED", "screenshot_saved": True,
                "att": {"authorization": "authorized"},
                "app_switch": {"name": "Random", "value": "1"},
                "visible_idfa_status": "CAPTURED", "visible_idfa": value,
            }))
            for name in ("tracking-allowed.png", "ios-idfa.png", "tracking-allowed-evidence.png"):
                (folder / name).write_bytes(b"visible")
            verdict = TC_DEFINITIONS["tracking-allowed"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")

    def test_tracking_allowed_rejects_boolean_lat_after_complete_visual_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = "82bd86b3-8f29-0da1-fc71-d24ce7c15f77"
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ia": value, "lat": False}}},
                "ext": {"plaintext": {"device": {"ia": value, "lat": 0}}},
            }))
            (folder / "ios-tracking-allowed-status.json").write_text(json.dumps({
                "status": "CAPTURED", "screenshot_saved": True,
                "att": {"authorization": "authorized"},
                "app_switch": {"name": "Random", "value": "1"},
                "visible_idfa_status": "CAPTURED", "visible_idfa": value,
            }))
            for name in ("tracking-allowed.png", "ios-idfa.png", "tracking-allowed-evidence.png"):
                (folder / name).write_bytes(b"visible")
            verdict = TC_DEFINITIONS["tracking-allowed"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_tracking_allowed_is_blocked_when_visible_app_switch_is_off(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = "82bd86b3-8f29-0da1-fc71-d24ce7c15f77"
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ia": value, "lat": 0}}},
                "ext": {"plaintext": {"device": {"ia": value, "lat": 0}}},
            }))
            (folder / "ios-tracking-allowed-status.json").write_text(json.dumps({
                "status": "CAPTURED", "screenshot_saved": True,
                "att": {"authorization": "denied"},
                "app_switch": {"name": "Random", "value": "0"},
                "visible_idfa_status": "CAPTURED", "visible_idfa": value,
            }))
            for name in ("tracking-allowed.png", "ios-idfa.png", "tracking-allowed-evidence.png"):
                (folder / name).write_bytes(b"visible")
            verdict = TC_DEFINITIONS["tracking-allowed"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")
            self.assertIn("does not mutate", verdict["reason"])

    def test_tracking_settings_capture_reads_sample_app_switch_without_mutation(self):
        config = MagicMock()
        config.bundle_id = "com.appier.Random"
        driver = MagicMock()
        privacy = MagicMock()
        tracking = MagicMock()
        app_switch = MagicMock()
        app_switch.get_attribute.side_effect = lambda name: {"name": "Random", "value": "1"}.get(name)
        driver.find_elements.return_value = [app_switch]
        driver.save_screenshot.side_effect = lambda path: Path(path).write_bytes(b"image") or True
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "settings.json"
            screenshot = Path(temporary) / "tracking.png"
            with patch.dict("os.environ", {
                "IOS_SETTINGS_STATE_FILE": str(state),
                "IOS_SETTINGS_SCREENSHOT": str(screenshot),
            }), patch.object(qa_ios, "_first_element", side_effect=[privacy, tracking]), \
                    patch.object(qa_ios.time, "sleep"):
                document = qa_ios.capture_tracking_settings(driver, config)
            self.assertEqual(document["status"], "CAPTURED")
            self.assertEqual(document["att"]["authorization"], "authorized")
            self.assertEqual(document["app_switch"]["name"], "Random")
            app_switch.click.assert_not_called()

    def test_tracking_allowed_materializer_builds_comparison_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "bundle"
            folder.mkdir()
            value = "82bd86b3-8f29-0da1-fc71-d24ce7c15f77"
            settings_state = root / "settings.json"
            settings_image = root / "settings.png"
            idfa_state = root / "idfa.json"
            idfa_image = root / "idfa.png"
            settings_state.write_text(json.dumps({
                "status": "CAPTURED", "scenario": "TRACKING-ALLOWED", "screenshot_saved": True,
                "att": {"authorization": "authorized"},
                "app_switch": {"name": "Random", "value": "1"},
            }))
            settings_image.write_bytes(b"settings")
            idfa_state.write_text(json.dumps({"status": "CAPTURED", "value": value}))
            idfa_image.write_bytes(b"idfa")
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ia": value, "lat": 0}}},
                "ext": {"plaintext": {"device": {"ia": value, "lat": 0}}},
            }))

            def render(_document, target, width=1400, height=1000):
                target.write_bytes(b"card")

            with patch.dict("os.environ", {
                "IOS_SETTINGS_STATE_FILE": str(settings_state),
                "IOS_SETTINGS_SCREENSHOT": str(settings_image),
                "IOS_IDFA_STATE_FILE": str(idfa_state),
                "IOS_IDFA_SCREENSHOT": str(idfa_image),
                "IOS_SETTINGS_BEFORE_SCREENSHOT": str(root / "missing-before.png"),
            }), patch.object(evidence_ios, "_write_html_screenshot", side_effect=render):
                evidence_ios.materialize_ios_settings_state(folder)
            status = json.loads((folder / "ios-tracking-allowed-status.json").read_text())
            self.assertEqual(status["visible_idfa"], value)
            self.assertEqual(status["actual"]["request_lat"], 0)
            self.assertTrue((folder / "tracking-allowed-evidence.png").is_file())

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

    def test_idfv_current_scope_passes_from_valid_consistent_wire_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = "82bd86b3-8f29-0da1-fc71-d24ce7c15f77"
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ifv": value}}},
                "ext": {"plaintext": {"device": {"ifv": value}}},
            }))
            verdict = TC_DEFINITIONS["app-set-id"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")
            self.assertEqual(verdict["evidence"], "app-set-id.json")
            self.assertEqual(verdict["title"], "Identifier for Vendor (IDFV)")

    def test_idfv_uppercase_wire_value_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = "82BD86B3-8F29-0DA1-FC71-D24CE7C15F77"
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ifv": value}}},
            }))
            verdict = TC_DEFINITIONS["app-set-id"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")

    def test_idfv_payload_evidence_uses_extended_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = "82bd86b3-8f29-0da1-fc71-d24ce7c15f77"
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ifv": value}}},
            }))
            evidence_ios.materialize_ios_idfv_payload(folder)
            document = json.loads((folder / "app-set-id.json").read_text())
            self.assertEqual(document["source"], "ext.plaintext.device.ifv")
            self.assertEqual(document["actual"]["ext_device_ifv"], value)
            self.assertIn("IDFV", document["note"])

    def test_idfv_invalid_wire_value_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ifv": "not-a-uuid"}}},
                "ext": {"plaintext": {"device": {"ifv": "not-a-uuid"}}},
            }))
            verdict = TC_DEFINITIONS["app-set-id"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_iap_valid_array_is_blocked_without_independent_answer(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = ["com.example.product"]
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"iaphistory": value}}}},
            }))
            evidence_ios.materialize_ios_iap_payload(folder)
            verdict = TC_DEFINITIONS["in-app-purchase-history"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")
            self.assertEqual(verdict["evidence"], "in-app-purchase-history.json")
            document = json.loads((folder / "in-app-purchase-history.json").read_text())
            self.assertEqual(document["actual"]["product_ids"], value)

    def test_iap_malformed_array_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"iaphistory": [""]}}}},
            }))
            verdict = TC_DEFINITIONS["in-app-purchase-history"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_iap_missing_field_is_blocked_without_observable_purchase_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {}}}},
            }))
            verdict = TC_DEFINITIONS["in-app-purchase-history"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")
            self.assertFalse(verdict["actual"]["field_present"])

    def test_boot_timestamps_valid_format_passes_without_visible_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = [1700000000000, 1710000000000]
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"pot": value}}}},
            }))
            evidence_ios.materialize_ios_boot_payload(folder)
            verdict = TC_DEFINITIONS["boot-timestamps"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")
            self.assertEqual(verdict["evidence"], "boot-timestamps.json")
            document = json.loads((folder / "boot-timestamps.json").read_text())
            self.assertIn("肉眼可見 Evidence", document["note"])

    def test_boot_timestamps_invalid_format_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"pot": [2, 2]}}}},
            }))
            verdict = TC_DEFINITIONS["boot-timestamps"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_ram_payload_values_pass_and_preserve_evidence_note(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {
                    "mem_total": 6_000_000_000,
                    "mem_available": 2_000_000_000,
                }}}},
            }))
            evidence_ios.materialize_ios_ram_payload(folder)
            self.assertEqual(TC_DEFINITIONS["ram-total"].validate(folder)["status"], "PASS")
            self.assertEqual(TC_DEFINITIONS["ram-available"].validate(folder)["status"], "PASS")
            document = json.loads((folder / "ram-available.json").read_text())
            self.assertIn("肉眼可見 Evidence", document["note"])

    def test_ram_available_above_total_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {
                    "mem_total": 100,
                    "mem_available": 101,
                }}}},
            }))
            verdict = TC_DEFINITIONS["ram-available"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_battery_level_matches_visible_control_center_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"batterylevel": 67}}},
            }))
            (folder / "ios-battery-level.json").write_text(json.dumps({
                "status": "CAPTURED", "value": 68,
            }))
            verdict = TC_DEFINITIONS["battery-level"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")
            self.assertEqual(verdict["evidence"], "ios-battery-level.png")

    def test_battery_level_out_of_range_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"batterylevel": 101}}},
            }))
            (folder / "ios-battery-level.json").write_text(json.dumps({
                "status": "CAPTURED", "value": 68,
            }))
            verdict = TC_DEFINITIONS["battery-level"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_battery_level_without_visible_evidence_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"batterylevel": 67}}},
            }))
            verdict = TC_DEFINITIONS["battery-level"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")

    def test_control_center_parser_scopes_battery_away_from_other_percentages(self):
        source = """
        <XCUIElementTypeSlider name="Brightness" value="50%"/>
        <XCUIElementTypeSlider name="Volume" value="25%"/>
        <XCUIElementTypeOther name="Battery Power" value="67%, Charging"/>
        """
        level, charging, text = qa_ios._control_center_battery_state(source)
        self.assertEqual(level, 67)
        self.assertIs(charging, True)
        self.assertIn("Battery Power", text)

    def test_control_center_battery_ocr_accepts_one_visible_percentage(self):
        result = MagicMock(returncode=0, stdout="No Service\n100%\n", stderr="")
        with tempfile.TemporaryDirectory() as temporary:
            screenshot = Path(temporary) / "control-center.png"
            screenshot.write_bytes(b"image")
            with patch.object(qa_ios.subprocess, "run", return_value=result):
                level, text = qa_ios._visible_battery_level_from_screenshot(screenshot)
        self.assertEqual(level, 100)
        self.assertIn("100%", text)

    def test_control_center_battery_ocr_rejects_ambiguous_percentages(self):
        result = MagicMock(returncode=0, stdout="100%\n50%\n", stderr="")
        with tempfile.TemporaryDirectory() as temporary:
            screenshot = Path(temporary) / "control-center.png"
            screenshot.write_bytes(b"image")
            with patch.object(qa_ios.subprocess, "run", return_value=result):
                level, _ = qa_ios._visible_battery_level_from_screenshot(screenshot)
        self.assertIsNone(level)

    def test_control_center_screenshot_detects_green_battery_and_white_lightning(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            screenshot = Path(temporary) / "control-center.png"
            image = Image.new("RGB", (100, 100), (40, 40, 40))
            for x in range(70, 90):
                for y in range(5, 15):
                    image.putpixel((x, y), (30, 210, 70))
            for x in range(77, 83):
                for y in range(8, 12):
                    image.putpixel((x, y), (255, 255, 255))
            image.save(screenshot)
            charging, metrics = qa_ios._visible_charging_indicator_from_screenshot(screenshot)
        self.assertIs(charging, True)
        self.assertGreater(metrics["white_glyph_pixels"], 0)

    def test_control_center_volume_parser_scopes_media_slider_away_from_brightness(self):
        source = """
        <XCUIElementTypeSlider name="Brightness" value="50%"/>
        <XCUIElementTypeSlider name="Volume" value="25%"/>
        <XCUIElementTypeOther name="Battery Power" value="67%, Charging"/>
        """
        percent, text = qa_ios._control_center_volume_state(source)
        self.assertEqual(percent, 25)
        self.assertIn("Volume", text)

    def test_output_volume_passes_against_visible_control_center_slider(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"volume": .25}}}},
            }))
            (folder / "ios-output-volume-status.json").write_text(json.dumps({
                "status": "CAPTURED", "visible_percent": 25,
                "normalized_volume": .25, "accessibility_text": "Volume | 25%",
            }))
            (folder / "ios-output-volume-control-center.png").write_bytes(b"visible")
            (folder / "output-volume-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["output-volume"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")

    def test_output_volume_payload_mismatch_fails_with_complete_visual_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"volume": .8}}}},
            }))
            (folder / "ios-output-volume-status.json").write_text(json.dumps({
                "status": "CAPTURED", "visible_percent": 25,
                "normalized_volume": .25, "accessibility_text": "Volume | 25%",
            }))
            (folder / "ios-output-volume-control-center.png").write_bytes(b"visible")
            (folder / "output-volume-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["output-volume"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_output_volume_materializer_joins_control_center_and_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "bundle"
            folder.mkdir()
            state = root / "volume.json"
            screenshot = root / "volume.png"
            state.write_text(json.dumps({
                "status": "CAPTURED", "visible_percent": 25,
                "normalized_volume": .25, "accessibility_text": "Volume | 25%",
            }))
            screenshot.write_bytes(b"image")
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"volume": .25}}}},
            }))

            def render(_document, target, width=1400, height=1000):
                target.write_bytes(b"card")

            with patch.dict("os.environ", {
                "IOS_OUTPUT_VOLUME_STATE_FILE": str(state),
                "IOS_OUTPUT_VOLUME_SCREENSHOT": str(screenshot),
            }), patch.object(evidence_ios, "_write_html_screenshot", side_effect=render):
                evidence_ios.materialize_ios_output_volume_visible(folder)
            document = json.loads((folder / "ios-output-volume-status.json").read_text())
            self.assertEqual(document["actual"]["extended"], .25)
            self.assertTrue((folder / "output-volume-evidence.png").is_file())

    def test_system_context_parsers_extract_keyboard_wifi_and_vpn_state(self):
        self.assertEqual(
            qa_ios._visible_keyboard_tags(["English (US)", "Chinese, Traditional – Zhuyin", "Emoji"]),
            ["en-US", "zh-Hant", "emoji"],
        )
        wifi_source = '<XCUIElementTypeSwitch label="Wi‑Fi" value="1"/><XCUIElementTypeCell name="QA WiFi"/><XCUIElementTypeImage name="checkmark"/>'
        self.assertIs(qa_ios._visible_wifi_connected(wifi_source), True)
        self.assertIs(qa_ios._visible_vpn_connected(["VPN", "Not Connected"]), False)

    def test_ideviceinfo_falls_back_to_coredevice_inventory(self):
        config = MagicMock(udid="physical-udid")
        qa_ios._COREDEVICE_DETAILS.clear()
        details = {"result": {
            "hardwareProperties": {"productType": "iPhone12,3"},
            "deviceProperties": {
                "name": "QA iPhone", "osVersionNumber": "26.3", "osBuildUpdate": "23D127",
            },
        }}
        with patch.object(qa_ios.shutil, "which", return_value="available"), \
                patch.object(qa_ios, "_run", return_value=""), \
                patch.object(qa_ios, "_read_json", return_value=details):
            self.assertEqual(qa_ios.ideviceinfo(config, "ProductType"), "iPhone12,3")
            self.assertEqual(qa_ios.ideviceinfo(config, "DeviceName"), "QA iPhone")
            self.assertEqual(qa_ios.ideviceinfo(config, "ProductVersion"), "26.3")

    def test_system_context_materializer_and_validators_use_independent_native_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "bundle"
            folder.mkdir()
            state = root / "system.json"
            pages = {
                "date_time": {"status": "CAPTURED"},
                "language_region": {"status": "CAPTURED"},
                "keyboards": {"status": "CAPTURED", "keyboard_tags": ["en-US", "zh-Hant", "emoji"]},
                "wifi": {"status": "CAPTURED", "connected": True},
                "cellular": {"status": "CAPTURED", "no_sim": True},
                "vpn": {"status": "CAPTURED", "connected": False},
                "location": {"status": "CAPTURED"},
            }
            state.write_text(json.dumps({
                "status": "CAPTURED", "locale": "en_TW", "timezone": "Asia/Taipei",
                "timezone_offset_minutes": 480, "product_type": "iPhone12,3", "pages": pages,
            }))
            screenshot_env = {}
            for key, (env_key, _default, _target) in evidence_ios.IOS_SYSTEM_SCREENSHOTS.items():
                image = root / f"{key}.png"
                image.write_bytes(b"image")
                screenshot_env[env_key] = str(image)
            screenshot_env["IOS_SYSTEM_CONTEXT_STATE_FILE"] = str(state)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {
                    "utcoffset": 480, "lang": "en", "langb": "en-TW", "conntype": "wifi",
                    "input_lang": ["en-US", "zh-Hant", "emoji"], "carrier": "", "mccmnc": "",
                    "ext": {"emulator": False, "jailbreak": False, "vpn": "0"},
                }}},
                "ext": {"plaintext": {"device": {
                    "utcoffset": 480, "lang": "en", "langb": "en-TW", "conntype": "wifi",
                    "input_lang": ["en-US", "zh-Hant", "emoji"], "carrier": "", "mccmnc": "",
                    "geo_lat": 25.0, "geo_lon": 121.5,
                    "ext": {"emulator": False, "jailbreak": False, "vpn": "0"},
                }}},
            }))

            def render(_document, target, width=1400, height=1000):
                target.write_bytes(b"card")

            with patch.dict("os.environ", screenshot_env), patch.object(
                evidence_ios, "_write_html_screenshot", side_effect=render,
            ):
                evidence_ios.materialize_ios_system_context(folder)
            root_card = (folder / "root-status-evidence.html").read_text()
            self.assertIn("NO INDEPENDENT SCREEN", root_card)
            self.assertIn("Extended device.ext.jailbreak", root_card)
            for key in (
                "default-timezone", "default-language-iso", "default-language-bcp47",
                "keyboard-languages", "connection-type", "carrier", "mcc-mnc", "vpn-status",
                "emulator-detection", "root-status",
            ):
                self.assertEqual(TC_DEFINITIONS[key].validate(folder)["status"], "PASS", key)
            for key in ("precise-gps-latitude", "precise-gps-longitude", "connection-type-cellular"):
                self.assertEqual(TC_DEFINITIONS[key].validate(folder)["status"], "BLOCKED", key)

    def test_root_status_requires_strict_false_wire_value(self):
        cases = (
            (None, False, "PASS"),
            (False, False, "PASS"),
            (None, True, "FAILED"),
            (None, 0, "FAILED"),
            (True, False, "FAILED"),
            (None, None, "FAILED"),
        )
        for request_value, extended_value, expected_status in cases:
            with self.subTest(request=request_value, extended=extended_value):
                with tempfile.TemporaryDirectory() as temporary:
                    folder = Path(temporary)
                    request_ext = {} if request_value is None else {"jailbreak": request_value}
                    extended_ext = {} if extended_value is None else {"jailbreak": extended_value}
                    (folder / "bid_decoded.json").write_text(json.dumps({
                        "req": {"plaintext": {"device": {"ext": request_ext}}},
                        "ext": {"plaintext": {"device": {"ext": extended_ext}}},
                    }))
                    verdict = TC_DEFINITIONS["root-status"].validate(folder)
                    self.assertEqual(verdict["status"], expected_status)

    def test_review_context_materializer_keeps_unverifiable_values_visible_and_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "screenshot.png").write_bytes(b"sample-app")
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {
                    "app": {"sdk_version": "9.9.9"},
                    "device": {"argus_ver": "1.2.3"},
                    "user": {"last_foreground_time": [1000], "last_background_time": []},
                    "compliance": {"force_gdpr_applies": 0, "coppa_applies": 1},
                }},
            }))

            def render(_document, target, width=1400, height=1000):
                target.write_bytes(b"card")

            with patch.object(evidence_ios, "_write_html_screenshot", side_effect=render):
                evidence_ios.materialize_ios_review_context(folder)

            context = json.loads((folder / "ios-review-context.json").read_text())
            self.assertEqual(context["status"], "REVIEW_REQUIRED")
            for key in (
                "sdk-version", "argus-sdk-version", "last-foreground-times",
                "last-background-times", "force-gdpr-override", "coppa-applies",
            ):
                self.assertTrue((folder / f"{key}-evidence.png").is_file(), key)
                self.assertEqual(TC_DEFINITIONS[key].validate(folder)["status"], "BLOCKED", key)

    def test_control_center_parser_treats_scoped_battery_without_qualifier_as_not_charging(self):
        level, charging, _ = qa_ios._control_center_battery_state(
            '<XCUIElementTypeOther name="Battery" value="67%"/>'
        )
        self.assertEqual(level, 67)
        self.assertIs(charging, False)

    def test_charging_status_matches_visible_control_center_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"charging": 1}}},
                "ext": {"plaintext": {"device": {"charging": 1}}},
            }))
            (folder / "ios-charging-status.json").write_text(json.dumps({
                "status": "CAPTURED", "charging": True,
                "accessibility_text": "Battery Power | 67%, Charging",
            }))
            (folder / "ios-charging-status.png").write_bytes(b"visible")
            verdict = TC_DEFINITIONS["charging-status"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")
            self.assertEqual(verdict["evidence"], "ios-charging-status.png")

    def test_charging_status_without_visible_screenshot_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"charging": 0}}},
            }))
            (folder / "ios-charging-status.json").write_text(json.dumps({
                "status": "CAPTURED", "charging": False,
            }))
            verdict = TC_DEFINITIONS["charging-status"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")

    def test_active_sim_carrier_block_points_to_existing_visual_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"carrier": "Orange France"}}},
                "ext": {"plaintext": {"device": {"carrier": "Orange France"}}},
            }))
            (folder / "ios-system-context.json").write_text(json.dumps({
                "pages": {"cellular": {"status": "CAPTURED", "no_sim": False}},
            }))
            (folder / "ios-cellular.png").write_bytes(b"settings")
            (folder / "carrier-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["carrier"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")
            self.assertEqual(verdict["evidence"], "carrier-evidence.png")

    def test_battery_saver_matches_visible_low_power_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"battery_saver": False}}}},
            }))
            (folder / "ios-low-power-mode.json").write_text(json.dumps({
                "status": "CAPTURED", "enabled": False, "switch_value": "0",
            }))
            (folder / "ios-low-power-mode.png").write_bytes(b"visible")
            verdict = TC_DEFINITIONS["battery-saver"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")
            self.assertEqual(verdict["evidence"], "ios-low-power-mode.png")

    def test_battery_saver_payload_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"battery_saver": True}}}},
            }))
            (folder / "ios-low-power-mode.json").write_text(json.dumps({
                "status": "CAPTURED", "enabled": False, "switch_value": "0",
            }))
            (folder / "ios-low-power-mode.png").write_bytes(b"visible")
            verdict = TC_DEFINITIONS["battery-saver"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_low_power_capture_preserves_visible_switch_without_mutating_it(self):
        config = MagicMock()
        driver = MagicMock()
        control = MagicMock()
        control.get_attribute.side_effect = lambda name: "1" if name == "value" else None
        driver.save_screenshot.side_effect = lambda path: Path(path).write_bytes(b"image") or True
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            screenshot = Path(temporary) / "low-power.png"
            with patch.dict("os.environ", {
                "IOS_LOW_POWER_STATE_FILE": str(state),
                "IOS_LOW_POWER_SCREENSHOT": str(screenshot),
            }), patch.object(qa_ios, "create_driver", return_value=driver), \
                    patch.object(qa_ios, "_settings_search_open"), \
                    patch.object(qa_ios, "_setting_element", return_value=control):
                document = qa_ios.capture_visible_low_power_mode(config)
            self.assertEqual(document["status"], "CAPTURED")
            self.assertIs(document["enabled"], True)
            control.click.assert_not_called()

    def test_ios_display_validators_match_points_native_pixels_ppi_and_ratio(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {
                    "sw": 375, "sh": 812, "pxratio": 3,
                }}},
                "ext": {"plaintext": {"device": {
                    "sw": 1125, "sh": 2436, "ppi": 458, "pxratio": 3,
                }}},
            }))
            (folder / "ios-display-status.json").write_text(json.dumps({
                "status": "CAPTURED", "orientation": "PORTRAIT",
                "product_type": "iPhone12,3",
                "logical_points": {"width": 375, "height": 812},
                "official_spec": {
                    "native_width": 1125, "native_height": 2436, "physical_ppi": 458,
                },
                "screenshot_dimensions": {"width": 1124, "height": 2436},
            }))
            (folder / "ios-display-source.png").write_bytes(b"visible")
            for key in ("screen-width", "screen-height", "screen-ppi", "pixel-ratio"):
                (folder / f"{key}-evidence.png").write_bytes(b"card")
                verdict = TC_DEFINITIONS[key].validate(folder)
                self.assertEqual(verdict["status"], "PASS", key)

    def test_ios_display_unknown_product_type_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"sw": 375}}},
                "ext": {"plaintext": {"device": {"sw": 1125}}},
            }))
            (folder / "ios-display-status.json").write_text(json.dumps({
                "status": "CAPTURED", "orientation": "PORTRAIT",
                "product_type": "iPhone99,9",
                "logical_points": {"width": 375, "height": 812},
                "official_spec": None,
                "reason": "ProductType iPhone99,9 is not mapped",
            }))
            (folder / "ios-display-source.png").write_bytes(b"visible")
            (folder / "screen-width-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["screen-width"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")

    def test_ios_pixel_ratio_mismatch_fails_with_complete_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"pxratio": 2}}},
                "ext": {"plaintext": {"device": {"pxratio": 2}}},
            }))
            (folder / "ios-display-status.json").write_text(json.dumps({
                "status": "CAPTURED", "orientation": "PORTRAIT",
                "logical_points": {"width": 375, "height": 812},
                "official_spec": {"native_width": 1125, "native_height": 2436},
            }))
            (folder / "ios-display-source.png").write_bytes(b"visible")
            (folder / "pixel-ratio-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["pixel-ratio"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_ios_display_materializer_maps_official_spec_and_keeps_screenshot_dimensions_supporting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "bundle"
            folder.mkdir()
            state = root / "state.json"
            screenshot = root / "source.png"
            state.write_text(json.dumps({
                "status": "CAPTURED", "orientation": "PORTRAIT",
                "product_type": "iPhone12,3",
                "logical_points": {"width": 375, "height": 812},
            }))
            screenshot.write_bytes(
                b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" +
                (1124).to_bytes(4, "big") + (2436).to_bytes(4, "big")
            )
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"sw": 375, "sh": 812, "pxratio": 3}}},
                "ext": {"plaintext": {"device": {"sw": 1125, "sh": 2436, "ppi": 458, "pxratio": 3}}},
            }))

            def render_cards(target, _info, _source):
                for key in ("screen-width", "screen-height", "screen-ppi", "pixel-ratio"):
                    (Path(target) / f"{key}-evidence.png").write_bytes(b"card")

            with patch.dict("os.environ", {
                "IOS_DISPLAY_STATE_FILE": str(state),
                "IOS_DISPLAY_SCREENSHOT": str(screenshot),
            }), patch.object(evidence_ios, "_render_ios_display_evidence", side_effect=render_cards):
                evidence_ios.materialize_ios_display_status(folder)
            document = json.loads((folder / "ios-display-status.json").read_text())
            self.assertEqual(document["official_spec"]["physical_ppi"], 458)
            self.assertEqual(document["actual"]["request"]["sw"], 375)
            self.assertEqual(document["actual"]["extended"]["sw"], 1125)
            self.assertEqual(document["screenshot_dimensions"], {"width": 1124, "height": 2436})
            self.assertEqual(TC_DEFINITIONS["screen-width"].validate(folder)["status"], "PASS")

    def test_ios_display_capture_preserves_points_product_type_and_visible_screen(self):
        config = MagicMock()
        config.bundle_id = "com.appier.Random"
        driver = MagicMock()
        driver.orientation = "PORTRAIT"
        driver.get_window_size.return_value = {"width": 375, "height": 812}
        driver.save_screenshot.side_effect = lambda path: Path(path).write_bytes(b"image") or True
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            screenshot = Path(temporary) / "display.png"

            def device_info(_config, key):
                return {"ProductType": "iPhone12,3", "DeviceName": "QA iPhone"}.get(key, "")

            with patch.dict("os.environ", {
                "IOS_DISPLAY_STATE_FILE": str(state),
                "IOS_DISPLAY_SCREENSHOT": str(screenshot),
            }), patch.object(qa_ios, "create_driver", return_value=driver), \
                    patch.object(qa_ios, "_settings_search_open"), \
                    patch.object(qa_ios, "ideviceinfo", side_effect=device_info):
                document = qa_ios.capture_visible_display_status(config)
            self.assertEqual(document["status"], "CAPTURED")
            self.assertEqual(document["logical_points"], {"width": 375, "height": 812})
            self.assertEqual(document["product_type"], "iPhone12,3")

    def test_about_parser_extracts_visible_model_name_from_native_cell(self):
        source = """
        <XCUIElementTypeCell name="Model Name, iPhone 11 Pro">
          <XCUIElementTypeStaticText name="Model Name"/>
          <XCUIElementTypeStaticText name="iPhone 11 Pro"/>
        </XCUIElementTypeCell>
        """
        self.assertEqual(qa_ios._about_visible_model_name(source), "iPhone 11 Pro")

    def test_device_make_passes_from_about_and_official_product_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"make": "Apple"}}},
            }))
            (folder / "ios-device-identity-status.json").write_text(json.dumps({
                "status": "CAPTURED", "product_type": "iPhone12,3",
                "visible_model_name": "iPhone 11 Pro", "official_make": "Apple",
                "official_spec": {"model": "iPhone 11 Pro"},
            }))
            (folder / "ios-device-about.png").write_bytes(b"visible")
            (folder / "device-make-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["device-make"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")

    def test_device_make_payload_mismatch_fails_with_complete_identity_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"make": "apple"}}},
            }))
            (folder / "ios-device-identity-status.json").write_text(json.dumps({
                "status": "CAPTURED", "product_type": "iPhone12,3",
                "visible_model_name": "iPhone 11 Pro", "official_make": "Apple",
                "official_spec": {"model": "iPhone 11 Pro"},
            }))
            (folder / "ios-device-about.png").write_bytes(b"visible")
            (folder / "device-make-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["device-make"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_device_model_passes_from_about_model_and_product_type(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"hwv": "iPhone12,3"}}},
                "ext": {"plaintext": {"device": {
                    "model": "iPhone 11 Pro", "hwv": "iPhone12,3",
                }}},
            }))
            (folder / "ios-device-identity-status.json").write_text(json.dumps({
                "status": "CAPTURED", "product_type": "iPhone12,3",
                "visible_model_name": "iPhone 11 Pro", "official_make": "Apple",
                "official_spec": {"model": "iPhone 11 Pro"},
            }))
            (folder / "ios-device-about.png").write_bytes(b"visible")
            (folder / "device-model-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["device-model"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")

    def test_device_identity_materializer_joins_about_product_type_and_make(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "bundle"
            folder.mkdir()
            state = root / "display.json"
            screenshot = root / "about.png"
            state.write_text(json.dumps({
                "status": "CAPTURED", "product_type": "iPhone12,3",
                "visible_model_name": "iPhone 11 Pro",
                "visual_source": "native Settings > General > About",
            }))
            screenshot.write_bytes(b"image")
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"make": "Apple"}}},
            }))

            def render(_document, target, width=1400, height=1000):
                target.write_bytes(b"card")

            with patch.dict("os.environ", {
                "IOS_DISPLAY_STATE_FILE": str(state),
                "IOS_DISPLAY_SCREENSHOT": str(screenshot),
            }), patch.object(evidence_ios, "_write_html_screenshot", side_effect=render):
                evidence_ios.materialize_ios_device_identity(folder)
            document = json.loads((folder / "ios-device-identity-status.json").read_text())
            self.assertEqual(document["official_make"], "Apple")
            self.assertEqual(document["actual"]["extended_make"], "Apple")
            self.assertTrue((folder / "device-make-evidence.png").is_file())

    def test_screen_brightness_passes_against_visible_native_slider(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ext": {"screen_bright": .45}}}},
                "ext": {"plaintext": {"device": {"ext": {"screen_bright": .45}}}},
            }))
            (folder / "ios-brightness-status.json").write_text(json.dumps({
                "status": "CAPTURED", "slider_accessibility_value": "45%",
                "visible_percent": 45, "normalized_brightness": .45,
                "slider_visible_in_screenshot": True,
            }))
            (folder / "ios-brightness-settings.png").write_bytes(b"visible")
            (folder / "screen-brightness-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["screen-brightness"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")

    def test_screen_brightness_fails_when_payload_differs_from_visible_slider(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"screen_bright": .8}}}},
            }))
            (folder / "ios-brightness-status.json").write_text(json.dumps({
                "status": "CAPTURED", "slider_accessibility_value": "45%",
                "visible_percent": 45, "normalized_brightness": .45,
                "slider_visible_in_screenshot": True,
            }))
            (folder / "ios-brightness-settings.png").write_bytes(b"visible")
            (folder / "screen-brightness-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["screen-brightness"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_screen_brightness_blocks_when_slider_is_not_confirmed_in_screenshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"screen_bright": .45}}}},
            }))
            (folder / "ios-brightness-status.json").write_text(json.dumps({
                "status": "CAPTURED", "slider_accessibility_value": "45%",
                "visible_percent": 45, "normalized_brightness": .45,
            }))
            (folder / "ios-brightness-settings.png").write_bytes(b"missing-slider")
            (folder / "screen-brightness-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["screen-brightness"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")

    def test_incomplete_brightness_card_does_not_claim_slider_is_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            screenshot = Path(temporary) / "brightness.png"
            screenshot.write_bytes(b"image")
            document = evidence_ios._settings_slider_evidence_document(
                "brightness",
                {
                    "status": "CAPTURED",
                    "slider_accessibility_value": "53%",
                    "visible_percent": 53,
                    "normalized_brightness": .53,
                    "actual": {"request": None, "extended": .55},
                },
                screenshot,
            )
            self.assertIn("INCOMPLETE CAPTURE · SLIDER NOT VISIBLE", document)
            self.assertIn("Accessibility metadata (supporting only)", document)
            self.assertNotIn("53% VISIBLE BRIGHTNESS", document)

    def test_visible_brightness_capture_is_read_only(self):
        config = MagicMock()
        driver = MagicMock()
        slider = MagicMock()
        slider.get_attribute.side_effect = lambda name: {"name": "Brightness", "value": "45%"}.get(name)
        slider.rect = {"x": 30, "y": 810, "width": 330, "height": 44}
        driver.get_window_size.return_value = {"width": 390, "height": 844}

        def scroll(_name, _arguments):
            slider.rect = {"x": 30, "y": 600, "width": 330, "height": 44}

        driver.execute_script.side_effect = scroll
        driver.find_elements.return_value = [slider]
        driver.save_screenshot.side_effect = lambda path: Path(path).write_bytes(b"image") or True
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "brightness.json"
            screenshot = Path(temporary) / "brightness.png"
            with patch.dict("os.environ", {
                "IOS_BRIGHTNESS_STATE_FILE": str(state),
                "IOS_BRIGHTNESS_SCREENSHOT": str(screenshot),
            }), patch.object(qa_ios, "create_driver", return_value=driver), \
                    patch.object(qa_ios, "_settings_search_open"):
                document = qa_ios.capture_visible_brightness(config)
            self.assertEqual(document["status"], "CAPTURED")
            self.assertEqual(document["normalized_brightness"], .45)
            self.assertTrue(document["slider_visible_in_screenshot"])
            driver.execute_script.assert_called()
            slider.click.assert_not_called()

    def test_font_scale_visible_page_remains_blocked_without_numeric_bridge(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"fontscale": 1.24}}}},
            }))
            (folder / "ios-font-size-status.json").write_text(json.dumps({
                "status": "CAPTURED", "slider_accessibility_value": "62%",
                "slider_position": .62, "increase_button_enabled": True,
            }))
            (folder / "ios-font-size-settings.png").write_bytes(b"visible")
            (folder / "font-scale-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["font-scale"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")
            self.assertIn("no reviewed iOS API bridge", verdict["reason"])

    def test_font_scale_invalid_payload_fails_with_visible_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"fontscale": 0}}}},
            }))
            (folder / "ios-font-size-status.json").write_text(json.dumps({
                "status": "CAPTURED", "slider_accessibility_value": "62%",
                "slider_position": .62,
            }))
            (folder / "ios-font-size-settings.png").write_bytes(b"visible")
            (folder / "font-scale-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["font-scale"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_visible_font_size_capture_opens_larger_text_without_mutation(self):
        config = MagicMock()
        driver = MagicMock()
        slider = MagicMock()
        slider.get_attribute.side_effect = lambda name: "62%" if name == "value" else None
        driver.find_elements.return_value = [slider]
        driver.save_screenshot.side_effect = lambda path: Path(path).write_bytes(b"image") or True
        increase = MagicMock()
        increase.is_enabled.return_value = True
        decrease = MagicMock()
        decrease.is_enabled.return_value = True
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "font-size.json"
            screenshot = Path(temporary) / "font-size.png"
            with patch.dict("os.environ", {
                "IOS_FONT_SIZE_STATE_FILE": str(state),
                "IOS_FONT_SIZE_SCREENSHOT": str(screenshot),
            }), patch.object(qa_ios, "create_driver", return_value=driver), \
                    patch.object(qa_ios, "_settings_search_open") as search, \
                    patch.object(qa_ios, "_setting_element", side_effect=[increase, decrease]):
                document = qa_ios.capture_visible_font_size(config)
            self.assertEqual(document["status"], "CAPTURED")
            self.assertEqual(document["slider_position"], .62)
            search.assert_called_once_with(driver, "Larger Text")
            slider.click.assert_not_called()

    def test_brightness_materializer_joins_capture_and_payload_into_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "bundle"
            folder.mkdir()
            state = root / "state.json"
            screenshot = root / "brightness.png"
            state.write_text(json.dumps({
                "status": "CAPTURED", "slider_accessibility_value": "45%",
                "visible_percent": 45, "normalized_brightness": .45,
                "slider_visible_in_screenshot": True,
            }))
            screenshot.write_bytes(b"image")
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"screen_bright": .45}}}},
            }))

            def render(_document, target, width=1400, height=1000):
                target.write_bytes(b"card")

            with patch.dict("os.environ", {
                "IOS_BRIGHTNESS_STATE_FILE": str(state),
                "IOS_BRIGHTNESS_SCREENSHOT": str(screenshot),
            }), patch.object(evidence_ios, "_write_html_screenshot", side_effect=render):
                evidence_ios.materialize_ios_brightness_visible(folder)
            document = json.loads((folder / "ios-brightness-status.json").read_text())
            self.assertEqual(document["actual"]["extended"], .45)
            self.assertTrue((folder / "screen-brightness-evidence.png").is_file())

    def test_dark_mode_passes_against_visible_dark_appearance(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ext": {"darkmode": True}}}},
                "ext": {"plaintext": {"device": {"ext": {"darkmode": True}}}},
            }))
            (folder / "ios-dark-mode-status.json").write_text(json.dumps({
                "status": "CAPTURED", "selected_appearance": "Dark", "dark_mode": True,
                "appearance_controls": {
                    "Light": {"selected": False}, "Dark": {"selected": True},
                },
            }))
            (folder / "ios-dark-mode-settings.png").write_bytes(b"visible")
            (folder / "dark-mode-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["dark-mode"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")

    def test_dark_mode_fails_when_payload_differs_from_visible_light_appearance(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"darkmode": True}}}},
            }))
            (folder / "ios-dark-mode-status.json").write_text(json.dumps({
                "status": "CAPTURED", "selected_appearance": "Light", "dark_mode": False,
                "appearance_controls": {
                    "Light": {"selected": True}, "Dark": {"selected": False},
                },
            }))
            (folder / "ios-dark-mode-settings.png").write_bytes(b"visible")
            (folder / "dark-mode-evidence.png").write_bytes(b"card")
            verdict = TC_DEFINITIONS["dark-mode"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_visible_dark_mode_capture_reads_selected_appearance_without_mutation(self):
        config = MagicMock()
        driver = MagicMock()
        driver.save_screenshot.side_effect = lambda path: Path(path).write_bytes(b"image") or True
        light = MagicMock()
        dark = MagicMock()
        light.get_attribute.side_effect = lambda name: {"selected": "false", "traits": "Button", "value": ""}.get(name)
        dark.get_attribute.side_effect = lambda name: {"selected": "true", "traits": "Button, Selected", "value": "1"}.get(name)
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "dark-mode.json"
            screenshot = Path(temporary) / "dark-mode.png"
            with patch.dict("os.environ", {
                "IOS_DARK_MODE_STATE_FILE": str(state),
                "IOS_DARK_MODE_SCREENSHOT": str(screenshot),
            }), patch.object(qa_ios, "create_driver", return_value=driver), \
                    patch.object(qa_ios, "_settings_search_open") as search, \
                    patch.object(qa_ios, "_setting_element", side_effect=[light, dark]):
                document = qa_ios.capture_visible_dark_mode(config)
            self.assertEqual(document["status"], "CAPTURED")
            self.assertEqual(document["selected_appearance"], "Dark")
            self.assertIs(document["dark_mode"], True)
            search.assert_called_once_with(driver, "Display & Brightness")
            light.click.assert_not_called()
            dark.click.assert_not_called()

    def test_dark_mode_materializer_joins_visible_state_and_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "bundle"
            folder.mkdir()
            state = root / "dark-mode.json"
            screenshot = root / "dark-mode.png"
            state.write_text(json.dumps({
                "status": "CAPTURED", "selected_appearance": "Light", "dark_mode": False,
                "appearance_controls": {
                    "Light": {"selected": True}, "Dark": {"selected": False},
                },
            }))
            screenshot.write_bytes(b"image")
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ext": {"darkmode": False}}}},
            }))

            def render(_document, target, width=1400, height=1000):
                target.write_bytes(b"card")

            with patch.dict("os.environ", {
                "IOS_DARK_MODE_STATE_FILE": str(state),
                "IOS_DARK_MODE_SCREENSHOT": str(screenshot),
            }), patch.object(evidence_ios, "_write_html_screenshot", side_effect=render):
                evidence_ios.materialize_ios_dark_mode_visible(folder)
            document = json.loads((folder / "ios-dark-mode-status.json").read_text())
            self.assertIs(document["actual"]["extended"], False)
            self.assertTrue((folder / "dark-mode-evidence.png").is_file())

    def test_ios_sensors_are_blocked_as_not_in_scope_for_empty_or_populated_payloads(self):
        for payload in ([], [{"x": 0.1, "y": 0.2, "z": 0.3}]):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                (folder / "bid_decoded.json").write_text(json.dumps({
                    "ext": {"plaintext": {"device": {"ext": {
                        "gyroscope": payload, "accelerometer": payload,
                    }}}},
                }))
                for key in ("gyroscope", "accelerometer"):
                    verdict = TC_DEFINITIONS[key].validate(folder)
                    self.assertEqual(verdict["status"], "BLOCKED")
                    self.assertIn("Not In Scope", verdict["reason"])
                    self.assertEqual(
                        verdict["description"],
                        "Sensor array is observed but not evaluated in this scope.",
                    )

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
            flow = json.loads((folder / "appier-ad-flow.json").read_text())
            self.assertEqual(flow["expected"]["path"], "/v2/sdk/ios/ad")
            self.assertIn("actual", flow)
            self.assertIn("note", flow)

    def test_ios_sdk_init_requires_same_flow_request_and_http_200_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "summary.json").write_text(json.dumps({"test_type": "aibid", "cid": "cid"}))
            (folder / "bid_response.json").write_text(json.dumps({}))
            (folder / "bid_raw.json").write_text(json.dumps({}))
            (folder / "bid_decoded.json").write_text(json.dumps({}))
            events = [
                {"flow_id": "init-1", "timestamp": "2026-08-26T01:00:00Z", "phase": "request",
                 "kind": "sdk-init", "method": "GET", "url": "https://adx.apx.appier.net/v1/sdk/ios/init?"},
                {"flow_id": "init-1", "timestamp": "2026-08-26T01:00:00.1Z", "phase": "response",
                 "kind": "sdk-init", "method": "GET", "url": "https://adx.apx.appier.net/v1/sdk/ios/init?",
                 "status": 200, "content_type": "application/json", "content_length": 76},
            ]
            (folder / "proxy-events.jsonl").write_text("\n".join(json.dumps(row) for row in events) + "\n")

            rows = {row["tc"]: row for row in validate_ios_e2e(folder)}

            init = rows["standalone-sdk-init"]
            self.assertEqual(init["status"], "PASS")
            self.assertEqual(init["evidence"], "sdk-init-flow.json")
            self.assertEqual(init["actual"]["successful_transaction_count"], 1)
            flow = json.loads((folder / "sdk-init-flow.json").read_text())
            self.assertEqual(flow["actual"]["transactions"][0]["request"]["flow_id"], "init-1")
            self.assertEqual(flow["actual"]["transactions"][0]["response"]["status"], 200)

    def test_ios_sdk_init_does_not_pass_from_unpaired_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "summary.json").write_text(json.dumps({"test_type": "aibid", "cid": "cid"}))
            (folder / "bid_response.json").write_text(json.dumps({}))
            (folder / "bid_raw.json").write_text(json.dumps({}))
            (folder / "bid_decoded.json").write_text(json.dumps({}))
            (folder / "proxy-events.jsonl").write_text(json.dumps({
                "flow_id": "response-only", "phase": "response", "kind": "sdk-init", "method": "GET",
                "url": "https://adx.apx.appier.net/v1/sdk/ios/init?", "status": 200,
            }) + "\n")

            rows = {row["tc"]: row for row in validate_ios_e2e(folder)}

            self.assertEqual(rows["standalone-sdk-init"]["status"], "FAILED")
            self.assertEqual(rows["standalone-sdk-init"]["actual"]["successful_transaction_count"], 0)

    def test_r5_materializer_builds_three_stage_visual_comparison_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ext": {"darkmode": True}}}},
                "ext": {"plaintext": {"device": {"ext": {"darkmode": True}}}},
            }))
            (folder / "ios-settings-state.json").write_text(json.dumps({
                "scenario": "DISPLAY-DARK", "automation": "Appium XCUITest native Settings UI",
                "before": "Light", "desired": "Dark", "after": {"value": "selected"},
                "stages": {
                    "before": {"value": "Light", "screenshot": "ios-settings-before.png"},
                    "mutated": {"value": "Dark", "screenshot": "ios-settings-state.png"},
                    "restored": {"status": "VERIFIED", "value": "Light", "screenshot": "ios-settings-restored.png"},
                },
            }))
            for name in ("ios-settings-before.png", "ios-settings-state.png", "ios-settings-restored.png"):
                (folder / name).write_bytes(name.encode())
            (folder / "verdicts.json").write_text(json.dumps({"verdicts": [{
                "tc": "dark-mode-enabled", "status": "PASS",
                "reason": "The visible native iOS state and decoded Bid value agree.",
                "evidence": "ios-settings-state.png",
            }]}))

            def render(_document, target, width=1400, height=1000):
                target.write_bytes(b"card")

            with patch.object(evidence_ios, "_write_html_screenshot", side_effect=render):
                rendered = evidence_ios.materialize_ios_r5_visual_evidence(folder)
            self.assertEqual(rendered, ["dark-mode-enabled-evidence.png"])
            html = (folder / "dark-mode-enabled-evidence.html").read_text()
            self.assertIn("BEFORE", html)
            self.assertIn("NEGATIVE STATE", html)
            self.assertIn("RESTORED", html)
            self.assertIn("VERIFIED", html)
            self.assertIn("Request device.ext.darkmode", html)
            self.assertIn("PASS", html)
            verdict = json.loads((folder / "verdicts.json").read_text())["verdicts"][0]
            self.assertEqual(verdict["evidence"], "dark-mode-enabled-evidence.png")

    def test_r5_blocked_case_still_gets_a_visual_reason_card_without_screenshot_or_bid(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "verdicts.json").write_text(json.dumps({"verdicts": [{
                "tc": "output-volume-muted", "status": "BLOCKED",
                "reason": "iOS cannot independently read back and restore media volume.",
            }]}))

            def render(_document, target, width=1400, height=1000):
                target.write_bytes(b"card")

            with patch.object(evidence_ios, "_write_html_screenshot", side_effect=render):
                evidence_ios.materialize_ios_r5_visual_evidence(folder)
            html = (folder / "output-volume-muted-evidence.html").read_text()
            self.assertIn("NO SCREENSHOT", html)
            self.assertIn("NOT CAPTURED", html)
            self.assertIn("BLOCKED", html)
            self.assertIn("independently read back", html)
            self.assertTrue((folder / "output-volume-muted-evidence.png").is_file())

    def test_r5_validator_points_report_to_case_specific_visual_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "bid_decoded.json").write_text(json.dumps({
                "req": {"plaintext": {"device": {"ext": {"battery_saver": True}}}},
                "ext": {"plaintext": {"device": {"ext": {"battery_saver": True}}}},
            }))
            (folder / "ios-settings-state.json").write_text(json.dumps({
                "scenario": "LOW-POWER", "automation": "Appium XCUITest native Settings UI",
                "screenshot_saved": True,
            }))
            (folder / "ios-settings-state.png").write_bytes(b"visible")
            verdict = TC_DEFINITIONS["battery-saver-enabled"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")
            self.assertEqual(verdict["evidence"], "battery-saver-enabled-evidence.png")

    def test_aos_aligned_payload_card_is_explicitly_not_independent_screen_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            value = "82BD86B3-8F29-0DA1-FC71-D24CE7C15F77"
            (folder / "bid_decoded.json").write_text(json.dumps({
                "ext": {"plaintext": {"device": {"ifv": value}}},
            }))
            (folder / "verdicts.json").write_text(json.dumps({"verdicts": [{
                "tc": "app-set-id", "status": "PASS", "expected": "canonical UUID",
                "actual": {"ext_device_ifv": value}, "reason": "Wire-format contract passed.",
                "evidence": "app-set-id.json",
            }]}))

            def render(_document, target, width=1400, height=1000):
                target.write_bytes(b"card")

            with patch.object(evidence_ios, "_write_html_screenshot", side_effect=render):
                rendered = evidence_ios.materialize_ios_aos_aligned_visual_evidence(folder)
            self.assertEqual(rendered, ["app-set-id-evidence.png"])
            document = (folder / "app-set-id-evidence.html").read_text()
            self.assertIn("payload-only contract", document)
            self.assertIn("NO INDEPENDENT SCREEN", document)
            self.assertIn("device.ifv", document)
            verdict = json.loads((folder / "verdicts.json").read_text())["verdicts"][0]
            self.assertEqual(verdict["evidence"], "app-set-id-evidence.png")

    def test_aos_aligned_sensor_card_records_not_in_scope_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "verdicts.json").write_text(json.dumps({"verdicts": [{
                "tc": "gyroscope", "status": "BLOCKED", "expected": "Not In Scope",
                "actual": {"value": []}, "reason": "No reviewed sensor action was executed.",
            }]}))
            with patch.object(evidence_ios, "_write_html_screenshot", side_effect=lambda _d, target, **_k: target.write_bytes(b"card")):
                evidence_ios.materialize_ios_aos_aligned_visual_evidence(folder)
            document = (folder / "gyroscope-evidence.html").read_text()
            self.assertIn("NOT IN SCOPE", document)
            self.assertNotIn("AOS", document)
            self.assertIn("BLOCKED", document)

    def test_aos_aligned_lifecycle_card_shows_all_four_captures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            captures = []
            for index in range(4):
                capture = root / f"capture-{index}"
                capture.mkdir()
                (capture / "screenshot.png").write_bytes(f"image-{index}".encode())
                captures.append(str(capture))
            result = Path(captures[-1])
            (result / "ios-lifecycle-sequence.json").write_text(json.dumps({
                "captures": captures,
                "session-duration-continuous": {
                    "values": [1, 11], "passed": True,
                    "reason": "Continuous foreground session_duration must increase.",
                },
            }))
            (result / "verdicts.json").write_text(json.dumps({"verdicts": [{
                "tc": "session-duration-continuous", "status": "PASS",
                "expected": "second > first", "actual": {"values": [1, 11]},
                "reason": "Lifecycle rule passed.",
            }]}))
            with patch.object(evidence_ios, "_write_html_screenshot", side_effect=lambda _d, target, **_k: target.write_bytes(b"card")):
                evidence_ios.materialize_ios_aos_aligned_visual_evidence(result)
            document = (result / "session-duration-continuous-evidence.html").read_text()
            for label in ("START", "CONTINUOUS", "BACKGROUND", "TERMINATED"):
                self.assertIn(label, document)
            self.assertIn("[1,11]", document)

    def test_ios_process_pid_is_resolved_from_devicectl_json(self):
        config = MagicMock()
        config.bundle_id = "com.appier.Random"
        config.udid = "device"
        applications = {
            "result": {"apps": [{
                "bundleIdentifier": "com.appier.Random",
                "name": "AppierAdsSwiftSample",
                "executable": "file:///private/AppierAdsSwiftSample.app/AppierAdsSwiftSample",
            }]},
        }
        processes = {
            "result": {"runningProcesses": [{
                "processIdentifier": "4321",
                "executable": "file:///private/AppierAdsSwiftSample.app/AppierAdsSwiftSample",
            }]},
        }
        with patch.object(qa_ios, "_devicectl_json", side_effect=[applications, processes]):
            tokens = qa_ios._ios_app_process_tokens(config)
            pid = qa_ios._ios_app_pid(config, tokens)
        self.assertEqual(pid, 4321)

    def test_session_duration_increase_cases_require_same_pid_and_increase(self):
        cases = (
            ("session-duration-continuous", [111, 111, 111, 222], [1000, 2000], "PASS"),
            ("session-duration-continuous", [111, 111, 111, 222], [2000, 1000], "FAILED"),
            ("session-duration-continuous", [], [1000, 2000], "BLOCKED"),
            ("session-duration-background", [111, 111, 111, 222], [2000, 3000], "PASS"),
            ("session-duration-background", [111, 111, 333, 444], [2000, 3000], "BLOCKED"),
        )
        for key, pids, values, expected in cases:
            with self.subTest(key=key, pids=pids, values=values), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                (folder / "ios-lifecycle-sequence.json").write_text(json.dumps({
                    key: {"executed": True, "pids": pids, "values": values},
                }))
                verdict = TC_DEFINITIONS[key].validate(folder)
                self.assertEqual(verdict["status"], expected)

    def test_session_duration_termination_passes_only_with_new_pid_and_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "ios-lifecycle-sequence.json").write_text(json.dumps({
                "session-duration-termination": {
                    "executed": True,
                    "before_ms": 9000,
                    "after_ms": 1200,
                    "before_pid": 111,
                    "after_pid": 222,
                    "immediate_pid_exit_observed": True,
                },
            }))
            verdict = TC_DEFINITIONS["session-duration-termination"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")
            self.assertEqual(verdict["actual"]["before_pid"], 111)
            self.assertEqual(verdict["actual"]["after_pid"], 222)

    def test_session_duration_termination_blocks_without_new_pid_proof(self):
        for before_pid, after_pid in ((None, None), (111, 111)):
            with self.subTest(before_pid=before_pid, after_pid=after_pid), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                (folder / "ios-lifecycle-sequence.json").write_text(json.dumps({
                    "session-duration-termination": {
                        "executed": True,
                        "before_ms": 9000,
                        "after_ms": 1200,
                        "before_pid": before_pid,
                        "after_pid": after_pid,
                        "immediate_pid_exit_observed": False,
                    },
                }))
                verdict = TC_DEFINITIONS["session-duration-termination"].validate(folder)
                self.assertEqual(verdict["status"], "BLOCKED")

    def test_session_duration_termination_preserves_legacy_values_when_pid_is_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "ios-lifecycle-sequence.json").write_text(json.dumps({
                "session-duration-termination": {
                    "executed": True,
                    "values": [166370, 183897],
                },
            }))
            verdict = TC_DEFINITIONS["session-duration-termination"].validate(folder)
            self.assertEqual(verdict["status"], "BLOCKED")
            self.assertEqual(verdict["actual"]["before_ms"], 166370)
            self.assertEqual(verdict["actual"]["after_ms"], 183897)

    def test_session_duration_termination_fails_after_proven_restart_without_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "ios-lifecycle-sequence.json").write_text(json.dumps({
                "session-duration-termination": {
                    "executed": True,
                    "before_ms": 9000,
                    "after_ms": 9500,
                    "before_pid": 111,
                    "after_pid": 222,
                    "immediate_pid_exit_observed": True,
                },
            }))
            verdict = TC_DEFINITIONS["session-duration-termination"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_app_initialization_time_passes_with_stable_then_new_process_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "ios-lifecycle-sequence.json").write_text(json.dumps({
                "app-initialization-time": {
                    "executed": True,
                    "pids": [111, 111, 111, 222],
                    "values": [1000, 1000, 1000, 2000],
                },
            }))
            verdict = TC_DEFINITIONS["app-initialization-time"].validate(folder)
            self.assertEqual(verdict["status"], "PASS")

    def test_app_initialization_time_blocks_without_process_generation_proof(self):
        for pids in ([None, None, None, None], [111, 111, 111, 111]):
            with self.subTest(pids=pids), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                (folder / "ios-lifecycle-sequence.json").write_text(json.dumps({
                    "app-initialization-time": {
                        "executed": True,
                        "pids": pids,
                        "values": [1000, 1000, 1000, 2000],
                    },
                }))
                verdict = TC_DEFINITIONS["app-initialization-time"].validate(folder)
                self.assertEqual(verdict["status"], "BLOCKED")

    def test_app_initialization_time_fails_after_proven_restart_without_renewal(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "ios-lifecycle-sequence.json").write_text(json.dumps({
                "app-initialization-time": {
                    "executed": True,
                    "pids": [111, 111, 111, 222],
                    "values": [1000, 1000, 1000, 1000],
                },
            }))
            verdict = TC_DEFINITIONS["app-initialization-time"].validate(folder)
            self.assertEqual(verdict["status"], "FAILED")

    def test_app_duration_today_requires_proven_restart_and_monotonic_values(self):
        cases = (
            ([111, 111, 111, 222], [1000, 2000, 3000, 4000], "PASS"),
            ([111, 111, 111, 222], [1000, 2000, 3000, 2500], "FAILED"),
            ([], [1000, 2000, 3000, 4000], "BLOCKED"),
            ([111, 111, 111, 111], [1000, 2000, 3000, 4000], "BLOCKED"),
        )
        for pids, values, expected in cases:
            with self.subTest(pids=pids, values=values), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                (folder / "ios-lifecycle-sequence.json").write_text(json.dumps({
                    "app-duration-today": {
                        "executed": True,
                        "pids": pids,
                        "values": values,
                    },
                }))
                verdict = TC_DEFINITIONS["app-duration-today"].validate(folder)
                self.assertEqual(verdict["status"], expected)

    def test_aos_aligned_network_card_lists_each_decoded_transition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            captures = []
            for index in range(2):
                capture = root / f"net-{index}"
                capture.mkdir()
                (capture / "screenshot.png").write_bytes(f"image-{index}".encode())
                (capture / "bid_decoded.json").write_text(json.dumps({
                    "ext": {"plaintext": {"device": {
                        "ipv6": f"2001:db8::{index + 1}", "conntype": index + 1,
                    }}},
                }))
                captures.append(str(capture))
            result = Path(captures[-1])
            (result / "r4-network-sequence.json").write_text(json.dumps({"captures": captures}))
            (result / "verdicts.json").write_text(json.dumps({"verdicts": [{
                "tc": "ipv6-refresh-wifi-switch", "status": "PASS",
                "expected": "IPv6 refreshes", "actual": {"changed": True}, "reason": "Refreshed.",
            }]}))
            with patch.object(evidence_ios, "_write_html_screenshot", side_effect=lambda _d, target, **_k: target.write_bytes(b"card")):
                evidence_ios.materialize_ios_aos_aligned_visual_evidence(result)
            document = (result / "ipv6-refresh-wifi-switch-evidence.html").read_text()
            self.assertIn("LAUNCH", document)
            self.assertIn("WI-FI SWITCH", document)
            self.assertIn("2001:db8::1", document)
            self.assertIn("2001:db8::2", document)

    def test_all_ios_e2e_verdicts_keep_validator_evidence_without_materialized_png(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            evidence_by_tc = {
                "standalone-sdk-init": "sdk-init-flow.json",
                "standalone-appier-ad-request": "appier-ad-flow.json",
                "standalone-creative-assets": "e2e-network-evidence.json",
                "standalone-native-render": "ad-before-interactions.png",
                "standalone-impression": "e2e-network-evidence.json",
                "standalone-click": "e2e-network-evidence.json",
                "standalone-landing": "click-landing.png",
                "standalone-privacy": "privacy-landing.png",
                "standalone-install-attribution": "attribution-query.json",
                "standalone-attribution-reconciliation": "attribution-query.json",
                "admob-pubsetting": "mediation-network-evidence.json",
                "admob-gma-request": "mediation-network-evidence.json",
                "admob-appier-ad-request": "mediation-network-evidence.json",
                "admob-impression": "mediation-network-evidence.json",
                "admob-fill-result": "mediation-network-evidence.json",
                "admob-click": "mediation-network-evidence.json",
            }
            verdicts = [
                {"tc": key, "status": "PASS", "evidence": evidence}
                for key, evidence in evidence_by_tc.items()
            ]
            (folder / "verdicts.json").write_text(json.dumps({"verdicts": verdicts}))

            rendered = evidence_ios.materialize_ios_aos_aligned_visual_evidence(folder)

            self.assertEqual(rendered, [])
            document = json.loads((folder / "verdicts.json").read_text())
            self.assertEqual(
                {row["tc"]: row["evidence"] for row in document["verdicts"]},
                evidence_by_tc,
            )
            for key in evidence_by_tc:
                self.assertFalse((folder / f"{key}-evidence.png").exists(), key)

    def test_e2e_proxy_preflight_rejects_missing_charles_before_ui(self):
        with patch.object(qa_ios, "_tcp_listening", return_value=False):
            with self.assertRaisesRegex(qa_ios.CaptureError, "Charles is not listening"):
                qa_ios.ensure_e2e_proxy_ready()

    def test_shared_proxy_preflight_starts_repo_mitmdump_when_missing(self):
        addon = str(Path(qa_ios.__file__).with_name("mitmdump_addon.py").resolve())
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(qa_ios, "MITMDUMP_LOG", Path(temporary) / "mitmdump.log"), \
                patch.object(qa_ios, "_tcp_listening", side_effect=[True, False, True, True]), \
                patch.object(
                    qa_ios, "_listener_commands",
                    side_effect=[["/Applications/Charles.app/Contents/MacOS/Charles"], [f"mitmdump -s {addon}"]],
                ), patch.object(qa_ios.shutil, "which", return_value="/opt/homebrew/bin/mitmdump"), \
                patch.object(qa_ios.subprocess, "Popen") as start:
            qa_ios.ensure_proxy_ready()
        start.assert_called_once_with(
            ["/opt/homebrew/bin/mitmdump", "-s", addon, "--listen-port", "8081"],
            cwd=Path(addon).parent,
            stdout=ANY,
            stderr=qa_ios.subprocess.STDOUT,
            start_new_session=True,
        )

    def test_ios_automation_preflight_requires_device_appium_app_and_proxy(self):
        config = MagicMock(udid="device-1", bundle_id="com.appier.Random")
        with patch.object(qa_ios, "connected_udids", return_value=["device-1"]), \
                patch.object(qa_ios, "_appium_ready", return_value=True), \
                patch.object(qa_ios, "_bundle_installed", return_value=True), \
                patch.object(qa_ios, "ensure_proxy_ready") as proxy:
            qa_ios.ensure_ios_automation_ready(config)
        proxy.assert_called_once_with()

    def test_ios_automation_preflight_rejects_missing_scope_helper_app(self):
        config = MagicMock(udid="device-1", bundle_id="com.appier.Random")
        with patch.object(qa_ios, "connected_udids", return_value=["device-1"]), \
                patch.object(qa_ios, "_appium_ready", return_value=True), \
                patch.object(qa_ios, "_bundle_installed", side_effect=[True, False]) as installed, \
                patch.object(qa_ios, "ensure_proxy_ready") as proxy:
            with self.assertRaisesRegex(qa_ios.CaptureError, "com.pag3dev.GetMyIDFA"):
                qa_ios.ensure_ios_automation_ready(config, ("com.pag3dev.GetMyIDFA",))
        self.assertEqual(2, installed.call_count)
        proxy.assert_not_called()

    def test_ios_automation_preflight_stops_before_proxy_when_appium_is_missing(self):
        config = MagicMock(udid="device-1", bundle_id="com.appier.Random")
        with patch.object(qa_ios, "connected_udids", return_value=["device-1"]), \
                patch.object(qa_ios, "_appium_ready", return_value=False), \
                patch.object(qa_ios, "_bundle_installed") as installed, \
                patch.object(qa_ios, "ensure_proxy_ready") as proxy:
            with self.assertRaisesRegex(qa_ios.CaptureError, "Appium is not ready"):
                qa_ios.ensure_ios_automation_ready(config)
        installed.assert_not_called()
        proxy.assert_not_called()

    def test_r1_main_runs_preflight_before_round_or_phone_interaction(self):
        config = MagicMock(
            udid="device-1", bundle_id="com.appier.Random", test_mode="standalone",
            tab_name="Appier Direct", test_type="aibid", test_cid="cid", test_round="R1",
        )
        arguments = [
            "round", "R1", "--bundle-id", "com.appier.Random",
            "--test-mode", "standalone", "--test-type", "aibid",
            "--test-cid", "cid", "--udid", "device-1",
        ]
        with patch.object(qa_ios, "config_from_args", return_value=config), \
                patch.object(
                    qa_ios, "ensure_ios_automation_ready",
                    side_effect=qa_ios.CaptureError("preflight stopped"),
                ) as preflight, patch.object(qa_ios, "run_round") as run:
            with self.assertRaisesRegex(qa_ios.CaptureError, "preflight stopped"):
                qa_ios.main(arguments)
        preflight.assert_called_once_with(config)
        run.assert_not_called()

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
