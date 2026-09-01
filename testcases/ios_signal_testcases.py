"""iOS-owned Signal TestCases, Round membership, and comparisons.

The keys intentionally match the shared Catalog, but no Android validator or
Round definition is imported.  Device-state validators compare the wire value
with independent iOS Evidence when the testcase claims the state itself.  A
testcase may instead explicitly validate only the SDK's wire contract, without
claiming that the payload is an independent integrity attestation.
"""

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from verdict import blocked, evaluate


BID = "bid"
IOS_DEVICE_CONTEXT = "ios-device-context"
IOS_IDFA_VISIBLE = "ios-idfa-visible"
IOS_IDFV_PAYLOAD = "ios-idfv-payload"
IOS_IAP_PAYLOAD = "ios-iap-payload"
IOS_BOOT_PAYLOAD = "ios-boot-payload"
IOS_RAM_PAYLOAD = "ios-ram-payload"
IOS_BATTERY_VISIBLE = "ios-battery-visible"
IOS_CHARGING_VISIBLE = "ios-charging-visible"
IOS_LOW_POWER_VISIBLE = "ios-low-power-visible"
IOS_DISPLAY_STATUS = "ios-display-status"
IOS_DEVICE_IDENTITY = "ios-device-identity"
IOS_BRIGHTNESS_VISIBLE = "ios-brightness-visible"
IOS_FONT_SIZE_VISIBLE = "ios-font-size-visible"
IOS_DARK_MODE_VISIBLE = "ios-dark-mode-visible"
IOS_OUTPUT_VOLUME_VISIBLE = "ios-output-volume-visible"
IOS_SYSTEM_CONTEXT_VISIBLE = "ios-system-context-visible"
IOS_REVIEW_CONTEXT = "ios-review-context"
IOS_SETTINGS_STATE = "ios-settings-state"
IOS_QA_EVIDENCE = "ios-qa-evidence"
IOS_LIFECYCLE_SEQUENCE = "ios-lifecycle-sequence"


@dataclass(frozen=True)
class TestCase:
    key: str
    title: str
    description: str
    evidence: tuple
    validate: object


@dataclass(frozen=True)
class Round:
    capture_name: str
    testcase_keys: tuple
    warmup_ads: int = 0
    strategy: str = "standard"


def _read(folder, name, default=None):
    try:
        return json.loads((Path(folder) / name).read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _get(document, path):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _wire(folder, path):
    decoded = _read(folder, "bid_decoded.json", {}) or {}
    req = _get(decoded, f"req.plaintext.{path}")
    ext = _get(decoded, f"ext.plaintext.{path}")
    return req, ext


def _row(key, title, expected, actual, passed, evidence, reason):
    row = evaluate(
        key, expected=expected, actual=actual, evidence=evidence,
        compare=lambda _expected, _actual: passed, reason=reason,
    ).to_dict()
    row.update({"layer": "Signal", "title": title, "description": reason})
    return row


def _blocked(key, title, reason, actual=None, evidence="ios-device-context.json", *, not_executable=False):
    row = blocked(key, reason).to_dict()
    row.update({
        "layer": "Signal", "title": title, "description": reason,
        "actual": actual, "evidence": evidence,
    })
    if not_executable:
        row["execution_state"] = "NOT_EXECUTABLE"
    return row


def _present(value):
    return value is not None and value != "" and value != [] and value != {}


def _same(req, ext):
    return req is None or ext is None or req == ext


def _wire_validator(key, title, path, predicate=_present, expected="non-empty value", *, evidence="bid_decoded.json"):
    def validate(folder):
        req, ext = _wire(folder, path)
        values = [value for value in (req, ext) if value is not None]
        passed = bool(values) and all(predicate(value) for value in values) and _same(req, ext)
        return _row(
            key, title, {"field": path, "rule": expected, "req_ext_consistent": True},
            {"request": req, "extended": ext}, passed, evidence,
            f"iOS request and extended {path} satisfy the reviewed wire contract." if passed else
            f"FAILED: iOS {path} is missing, invalid, or inconsistent between request and extended payloads.",
        )
    return validate


def _context_validator(key, title, path, context_key, normalize=lambda value: value):
    def validate(folder):
        req, ext = _wire(folder, path)
        context = _read(folder, "ios-device-context.json", {}) or {}
        expected = _get(context, f"device.{context_key}")
        captured = ext if ext is not None else req
        if expected in (None, ""):
            return _blocked(
                key, title,
                f"Independent iOS Evidence for {context_key} was not captured; payload alone cannot PASS.",
                {"payload": captured, "context": expected},
            )
        try:
            passed = normalize(captured) == normalize(expected)
        except Exception:
            passed = False
        return _row(
            key, title, {"independent_ios_value": expected},
            {"payload": captured, "ios_device_context": expected}, passed,
            "ios-device-context.json",
            "The decoded value matches independent iOS device Evidence." if passed else
            "FAILED: the decoded value does not match independent iOS device Evidence.",
        )
    return validate


def _qa_evidence_validator(key, title, path, evidence_key, predicate=_present, normalize=lambda value: value):
    """Compare a wire value with a human-readable Sample App QA value."""
    def validate(folder):
        req, ext = _wire(folder, path)
        captured = ext if ext is not None else req
        document = _read(folder, "ios-qa-evidence.json", {}) or {}
        expected = _get(document, f"values.{evidence_key}")
        screenshot = Path(folder) / "ios-qa-evidence.png"
        if expected in (None, "") or not screenshot.is_file():
            return _blocked(
                key, title,
                f"Sample App QA Evidence for {evidence_key} is unavailable; the decoded Bid Request cannot verify itself.",
                {"payload": captured, "qa_evidence": expected}, "ios-qa-evidence.json",
            )
        try:
            passed = predicate(captured) and normalize(captured) == normalize(expected)
        except Exception:
            passed = False
        return _row(
            key, title, {"sample_app_qa_value": expected},
            {"payload": captured, "sample_app_qa_value": expected}, passed,
            "ios-qa-evidence.png",
            "The decoded value matches the visible Sample App QA Evidence." if passed else
            "FAILED: the decoded value does not match the visible Sample App QA Evidence.",
        )
    return validate


def _uuid(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}", value))


def validate_idfv(folder):
    _, ext_value = _wire(folder, "device.ifv")
    passed = _uuid(ext_value)
    return _row(
        "app-set-id", "Identifier for Vendor (IDFV)",
        {"ext_device_ifv": "non-empty UUID 8-4-4-4-12 (canonical iOS uppercase or lowercase)"},
        {"ext_device_ifv": ext_value}, passed, "app-set-id.json",
        "Extended device.ifv is a non-empty UUID in 8-4-4-4-12 form." if passed else
        "FAILED: Extended device.ifv is missing or is not a UUID in 8-4-4-4-12 form.",
    )


def validate_in_app_purchase_history(folder):
    _, value = _wire(folder, "device.ext.iaphistory")
    present = value is not None
    actual = {
        "field_present": present,
        "product_count": len(value) if isinstance(value, list) else 0,
        "product_ids": value,
    }
    if not present:
        return _blocked(
            "in-app-purchase-history", "In App Purchase History",
            "The Sample App has no observable purchase flow or independent expected product IDs, so purchase-history correctness cannot be judged.",
            actual, "in-app-purchase-history.json",
        )
    valid = (
        isinstance(value, list)
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
        and len(value) == len(set(value))
    )
    if not valid:
        return _row(
            "in-app-purchase-history", "In App Purchase History",
            {"field_present": True, "value": "array of unique non-empty product-ID strings"},
            actual, False, "in-app-purchase-history.json",
            "FAILED: Extended device.ext.iaphistory is present but is not an array of unique non-empty product-ID strings.",
        )
    return _blocked(
        "in-app-purchase-history", "In App Purchase History",
        "Sample App has no purchase flow or independent expected product IDs; the captured array cannot be verified for correctness.",
        actual, "in-app-purchase-history.json",
    )


def validate_boot_timestamps(folder):
    _, value = _wire(folder, "device.ext.pot")
    passed = (
        isinstance(value, list)
        and 1 <= len(value) <= 5
        and all(type(item) is int and item > 0 for item in value)
        and all(previous < current for previous, current in zip(value, value[1:]))
    )
    return _row(
        "boot-timestamps", "System Boot Timestamps",
        {"value": "1 to 5 strictly increasing positive epoch-millisecond integers"},
        {"timestamp_count": len(value) if isinstance(value, list) else 0, "pot": value},
        passed, "boot-timestamps.json",
        "device.ext.pot has the valid iOS wire format; human-visible Evidence is currently unavailable." if passed else
        "FAILED: device.ext.pot must contain 1 to 5 strictly increasing positive epoch-millisecond integers.",
    )


def validate_ram_total(folder):
    _, value = _wire(folder, "device.ext.mem_total")
    passed = type(value) is int and value > 0
    return _row(
        "ram-total", "RAM Status (Total)", {"mem_total": "positive integer bytes"},
        {"mem_total": value}, passed, "ram-total.json",
        "device.ext.mem_total is a positive integer byte value; human-visible Evidence is currently unavailable." if passed else
        "FAILED: device.ext.mem_total must be a positive integer byte value.",
    )


def validate_ram_available(folder):
    _, available = _wire(folder, "device.ext.mem_available")
    _, total = _wire(folder, "device.ext.mem_total")
    passed = (
        type(available) is int and available > 0
        and type(total) is int and total > 0
        and available <= total
    )
    return _row(
        "ram-available", "RAM Status (Available)",
        {"mem_available": "positive integer bytes not exceeding mem_total"},
        {"mem_available": available, "mem_total": total}, passed, "ram-available.json",
        "device.ext.mem_available is positive and does not exceed mem_total; human-visible Evidence is currently unavailable." if passed else
        "FAILED: device.ext.mem_available must be positive integer bytes not exceeding mem_total.",
    )


def validate_battery_level(folder):
    _, value = _wire(folder, "device.batterylevel")
    visible = _read(folder, "ios-battery-level.json", {}) or {}
    expected = visible.get("value") if visible.get("status") == "CAPTURED" else None
    if type(expected) not in (int, float):
        return _blocked(
            "battery-level", "Battery Level",
            "The visible iOS Control Center battery percentage was not captured.",
            {"payload": value, "visible_battery_level": expected},
            "ios-battery-level.png" if (Path(folder) / "ios-battery-level.png").is_file() else "ios-battery-level.json",
        )
    passed = (
        type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100
        and abs(value - expected) <= 2
    )
    return _row(
        "battery-level", "Battery Level", {"visible_battery_level": expected, "tolerance_percent": 2},
        {"payload_battery_level": value, "visible_battery_level": expected}, passed, "ios-battery-level.png",
        "The payload battery level matches the visible iOS Control Center percentage within 2%." if passed else
        "FAILED: device.batterylevel is invalid or differs from the visible iOS Control Center percentage by more than 2%.",
    )


def validate_charging_status(folder):
    req, ext = _wire(folder, "device.charging")
    visible = _read(folder, "ios-charging-status.json", {}) or {}
    expected = visible.get("charging") if visible.get("status") == "CAPTURED" else None
    screenshot = Path(folder) / "ios-charging-status.png"
    actual = {
        "request_device_charging": req,
        "extended_device_charging": ext,
        "visible_charging": expected,
        "accessibility_text": visible.get("accessibility_text"),
    }
    if type(expected) is not bool or not screenshot.is_file():
        return _blocked(
            "charging-status", "Charging Status",
            visible.get("reason") or "The iOS Control Center charging state was not captured with a visible screenshot.",
            actual, "ios-charging-status.png" if screenshot.is_file() else "ios-charging-status.json",
        )
    values = [value for value in (req, ext) if value is not None]
    passed = (
        bool(values)
        and all(value in (0, 1, False, True) for value in values)
        and _same(req, ext)
        and all(bool(value) is expected for value in values)
    )
    return _row(
        "charging-status", "Charging Status",
        {"visible_control_center_charging": expected, "wire": "boolean/0/1"},
        actual, passed, "ios-charging-status.png",
        "The payload charging state matches the visible iOS Control Center battery state." if passed else
        "FAILED: device.charging is missing, invalid, inconsistent, or does not match the visible iOS Control Center state.",
    )


def validate_battery_saver(folder):
    req, ext = _wire(folder, "device.ext.battery_saver")
    visible = _read(folder, "ios-low-power-mode.json", {}) or {}
    expected = visible.get("enabled") if visible.get("status") == "CAPTURED" else None
    screenshot = Path(folder) / "ios-low-power-mode.png"
    actual = {
        "request_battery_saver": req,
        "extended_battery_saver": ext,
        "visible_low_power_mode": expected,
        "switch_value": visible.get("switch_value"),
    }
    if type(expected) is not bool or not screenshot.is_file():
        return _blocked(
            "battery-saver", "Battery Saver (Low Power Mode)",
            visible.get("reason") or "The native iOS Low Power Mode switch was not captured with a visible screenshot.",
            actual, "ios-low-power-mode.json",
        )
    values = [value for value in (req, ext) if value is not None]
    passed = (
        bool(values)
        and all(type(value) is bool for value in values)
        and _same(req, ext)
        and all(value is expected for value in values)
    )
    return _row(
        "battery-saver", "Battery Saver (Low Power Mode)",
        {"visible_low_power_mode": expected, "wire": "JSON boolean"},
        actual, passed, "ios-low-power-mode.png",
        "The payload battery-saver flag matches the visible native iOS Low Power Mode switch." if passed else
        "FAILED: device.ext.battery_saver is missing, not boolean, inconsistent, or does not match the visible Low Power Mode switch.",
    )


def _positive_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _fraction(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _timestamp_array(value):
    return isinstance(value, list) and all(type(item) is int and item > 0 for item in value)


def validate_argus_sdk_version(folder):
    _, actual = _wire(folder, "device.argus_ver")
    return _blocked(
        "argus-sdk-version", "Argus SDK Version",
        "Waiting for a reviewer to enter the expected iOS Argus SDK version in the report.",
        {"captured_version": actual}, "argus-sdk-version-evidence.png",
    )


def validate_sdk_version(folder):
    req, ext = _wire(folder, "app.sdk_version")
    actual = req if req is not None else ext
    return _blocked(
        "sdk-version", "SDK Version (sdk_version)",
        "Waiting for a reviewer to enter the expected iOS Ads SDK build version in the report.",
        {"captured_version": actual}, "sdk-version-evidence.png",
    )


def _system_context(folder):
    return _read(folder, "ios-system-context.json", {}) or {}


def _system_page_ready(folder, info, page, image, card):
    return (
        info.get("status") == "CAPTURED"
        and _get(info, f"pages.{page}.status") == "CAPTURED"
        and (Path(folder) / image).is_file()
        and (Path(folder) / card).is_file()
    )


def validate_default_timezone(folder):
    req, ext = _wire(folder, "device.utcoffset")
    info = _system_context(folder); expected = info.get("timezone_offset_minutes")
    actual = {"timezone": info.get("timezone"), "expected_offset_minutes": expected, "request": req, "extended": ext}
    if type(expected) is not int or not _system_page_ready(folder, info, "date_time", "ios-date-time.png", "default-timezone-evidence.png"):
        return _blocked("default-timezone", "Default Timezone", info.get("timezone_reason") or "Native Date & Time and exact timezone Evidence is incomplete.", actual, "ios-system-context.json")
    passed = ext == expected and (req is None or req == expected)
    return _row("default-timezone", "Default Timezone", expected, actual, passed, "default-timezone-evidence.png", "The payload UTC offset matches the capture-time IANA timezone including DST." if passed else "FAILED: device.utcoffset does not match the independently calculated capture-time timezone offset.")


def _validate_system_language(folder, key, title, path, expected_key, card):
    req, ext = _wire(folder, path); info = _system_context(folder); expected = info.get(expected_key)
    actual = {"locale": info.get("locale"), "expected": expected, "request": req, "extended": ext}
    if not expected or not _system_page_ready(folder, info, "language_region", "ios-language-region.png", card):
        return _blocked(key, title, "Native Language & Region and exact locale Evidence is incomplete.", actual, "ios-system-context.json")
    passed = ext == expected and (req is None or req == expected)
    return _row(key, title, expected, actual, passed, card, "The payload language matches native Language & Region and ideviceinfo Locale." if passed else "FAILED: the payload language does not match the independently captured locale.")


def validate_language_iso(folder):
    return _validate_system_language(folder, "default-language-iso", "System Language Code", "device.lang", "language_code", "default-language-iso-evidence.png")


def validate_language_bcp47(folder):
    return _validate_system_language(folder, "default-language-bcp47", "System Language and Region Tag", "device.langb", "normalized_locale", "default-language-bcp47-evidence.png")


def validate_keyboard_languages(folder):
    req, ext = _wire(folder, "device.input_lang"); info = _system_context(folder)
    expected = _get(info, "pages.keyboards.keyboard_tags") or []
    actual = {"visible_mapped_keyboards": expected, "request": req, "extended": ext}
    if not expected or not _system_page_ready(folder, info, "keyboards", "ios-keyboards.png", "keyboard-languages-evidence.png"):
        return _blocked("keyboard-languages", "Installed Keyboard Languages", "Visible keyboard rows could not be mapped to one reviewed ordered tag list.", actual, "ios-system-context.json")
    passed = ext == expected and (req is None or req == expected)
    return _row("keyboard-languages", "Installed Keyboard Languages", expected, actual, passed, "keyboard-languages-evidence.png", "The payload keyboard tag list matches the visible native keyboard order." if passed else "FAILED: device.input_lang does not match the visible mapped keyboard list.")


def validate_root_status(folder):
    req, ext = _wire(folder, "device.ext.jailbreak"); info = _system_context(folder)
    actual = {"product_type": info.get("product_type"), "request": req, "extended": ext}
    passed = ext is False and (req is None or req is False)
    return _row(
        "root-status", "Jailbreak Status", False, actual, passed,
        "root-status-evidence.png",
        "Extended device.ext.jailbreak is JSON boolean false; Request is absent or also false."
        if passed else
        "FAILED: device.ext.jailbreak must be JSON boolean false in Extended and must be absent or false in Request.",
    )


def validate_emulator_detection(folder):
    req, ext = _wire(folder, "device.ext.emulator"); info = _system_context(folder)
    physical = str(info.get("product_type") or "").startswith(("iPhone", "iPad", "iPod"))
    actual = {"product_type": info.get("product_type"), "physical_libimobiledevice": physical, "request": req, "extended": ext}
    if not physical or not (Path(folder) / "emulator-detection-evidence.png").is_file():
        return _blocked("emulator-detection", "Simulator Detection", "Physical-device ProductType Evidence is incomplete.", actual, "ios-system-context.json")
    passed = ext is False and (req is None or req is False)
    return _row("emulator-detection", "Simulator Detection", False, actual, passed, "emulator-detection-evidence.png", "libimobiledevice and hardware ProductType establish a physical iOS device." if passed else "FAILED: device.ext.emulator is not false on the independently established physical device.")


def validate_connection_type(folder):
    req, ext = _wire(folder, "device.conntype"); info = _system_context(folder); connected = _get(info, "pages.wifi.connected")
    actual = {"visible_wifi_connected": connected, "request": req, "extended": ext}
    if connected is not True or not _system_page_ready(folder, info, "wifi", "ios-wifi.png", "connection-type-evidence.png"):
        return _blocked("connection-type", "Connection Type", "Native Wi-Fi does not expose one checked connected network.", actual, "ios-system-context.json")
    passed = ext == "wifi" and (req is None or req == "wifi")
    return _row("connection-type", "Connection Type", "wifi", actual, passed, "connection-type-evidence.png", "The payload transport matches the visibly connected Wi-Fi network." if passed else "FAILED: device.conntype does not match visible Wi-Fi connectivity.")


def _validate_no_sim_identity(folder, key, title, path, card):
    req, ext = _wire(folder, path); info = _system_context(folder); no_sim = _get(info, "pages.cellular.no_sim")
    actual = {"visible_no_sim": no_sim, "request": req, "extended": ext}
    if no_sim is not True or not _system_page_ready(folder, info, "cellular", "ios-cellular.png", card):
        return _blocked(key, title, "An active SIM requires a separate exact iOS carrier contract; visible No SIM Evidence is unavailable.", actual, card)
    passed = req in (None, "") and ext in (None, "")
    return _row(key, title, "empty/absent when No SIM", actual, passed, card, "The empty payload agrees with the visible No SIM state." if passed else f"FAILED: {path} must be empty or absent when native Settings visibly shows No SIM.")


def validate_carrier(folder): return _validate_no_sim_identity(folder, "carrier", "Carrier", "device.carrier", "carrier-evidence.png")
def validate_mcc_mnc(folder): return _validate_no_sim_identity(folder, "mcc-mnc", "MCC/MNC", "device.mccmnc", "mcc-mnc-evidence.png")


def _validate_precise_location_blocked(folder, key, title, path):
    req, ext = _wire(folder, path); info = _system_context(folder)
    return _blocked(key, title, "Location Services is visible, but it does not expose exact coordinates; the Sample App still needs an independent coordinate QA surface.", {"request": req, "extended": ext, "location_page": _get(info, "pages.location.status")}, f"{key}-evidence.png")


def validate_precise_latitude(folder): return _validate_precise_location_blocked(folder, "precise-gps-latitude", "Precise GPS Latitude", "device.geo_lat")
def validate_precise_longitude(folder): return _validate_precise_location_blocked(folder, "precise-gps-longitude", "Precise GPS Longitude", "device.geo_lon")


def validate_vpn_status(folder):
    req, ext = _wire(folder, "device.ext.vpn"); info = _system_context(folder); connected = _get(info, "pages.vpn.connected")
    expected = "1" if connected is True else ("0" if connected is False else None)
    actual = {"visible_vpn_connected": connected, "request": req, "extended": ext}
    if expected is None or not _system_page_ready(folder, info, "vpn", "ios-vpn.png", "vpn-status-evidence.png"):
        return _blocked("vpn-status", "VPN Status", "Native VPN connected state is not unambiguous.", actual, "ios-system-context.json")
    passed = ext == expected and (req is None or req == expected)
    return _row("vpn-status", "VPN Status", expected, actual, passed, "vpn-status-evidence.png", "The payload VPN flag matches the visible native VPN state." if passed else "FAILED: device.ext.vpn does not match native VPN state.")


def validate_connection_type_cellular(folder):
    req, ext = _wire(folder, "device.conntype"); info = _system_context(folder)
    return _blocked("connection-type-cellular", "Connection Type (Cellular)", "This scenario requires an active SIM and cellular data; the current R1 only observes native Cellular prerequisites.", {"visible_no_sim": _get(info, "pages.cellular.no_sim"), "request": req, "extended": ext}, "connection-type-cellular-evidence.png", not_executable=True)


def _review_context_blocked(folder, key, title, path, reason):
    req, ext = _wire(folder, path)
    return _blocked(key, title, reason, {"request": req, "extended": ext}, f"{key}-evidence.png")


def validate_last_foreground_times(folder):
    return _review_context_blocked(folder, "last-foreground-times", "Last Foreground Times", "user.last_foreground_time", "The payload array is observed, but R1 has no independent visible foreground-event timeline to verify each timestamp.")


def validate_last_background_times(folder):
    return _review_context_blocked(folder, "last-background-times", "Last Background Times", "user.last_background_time", "The payload array is observed, but R1 has no independent visible background-event timeline to verify each timestamp.")


def validate_force_gdpr_override(folder):
    return _review_context_blocked(folder, "force-gdpr-override", "Force GDPR Override", "compliance.force_gdpr_applies", "The Sample App does not visibly expose the configured Force GDPR input; the request cannot prove its own configuration.")


def validate_coppa_applies(folder):
    return _review_context_blocked(folder, "coppa-applies", "COPPA Applicability Flag", "compliance.coppa_applies", "The Sample App does not visibly expose the configured COPPA input; the request cannot prove its own configuration.")


def _ios_display_info(folder):
    return _read(folder, "ios-display-status.json", {}) or {}


def _display_evidence_ready(folder, info, evidence):
    captured = info.get("status") == "CAPTURED"
    portrait = str(info.get("orientation") or "").upper().startswith("PORTRAIT")
    screenshot = (Path(folder) / "ios-display-source.png").is_file()
    card = (Path(folder) / evidence).is_file()
    return captured and portrait and screenshot and card


def _validate_display_dimension(folder, key, title, axis, field):
    info = _ios_display_info(folder)
    evidence = f"{key}-evidence.png"
    req, ext = _wire(folder, f"device.{field}")
    logical = _get(info, f"logical_points.{axis}")
    native = _get(info, f"official_spec.native_{axis}")
    actual = {
        "request_points": req,
        "extended_pixels": ext,
        "captured_logical_points": logical,
        "official_native_pixels": native,
        "product_type": info.get("product_type"),
        "screenshot_dimensions": info.get("screenshot_dimensions"),
    }
    if not _display_evidence_ready(folder, info, evidence) or type(logical) is not int or type(native) is not int:
        return _blocked(
            key, title,
            info.get("reason") or "Independent iOS display Evidence is incomplete, not portrait, or the device model is not mapped to an Apple display specification.",
            actual, "ios-display-status.json",
        )
    passed = type(req) is int and type(ext) is int and req == logical and ext == native
    return _row(
        key, title,
        {"request_points": logical, "extended_native_pixels": native},
        actual, passed, evidence,
        f"Request {field} matches captured XCUITest points and Extended {field} matches the mapped Apple native pixels." if passed else
        f"FAILED: Request/Extended {field} do not match the independent logical-point and native-pixel sources.",
    )


def validate_screen_width(folder):
    return _validate_display_dimension(folder, "screen-width", "Screen Width", "width", "sw")


def validate_screen_height(folder):
    return _validate_display_dimension(folder, "screen-height", "Screen Height", "height", "sh")


def validate_screen_ppi(folder):
    info = _ios_display_info(folder)
    evidence = "screen-ppi-evidence.png"
    req, ext = _wire(folder, "device.ppi")
    expected = _get(info, "official_spec.physical_ppi")
    actual = {
        "request_ppi": req,
        "extended_ppi": ext,
        "official_physical_ppi": expected,
        "product_type": info.get("product_type"),
    }
    if not _display_evidence_ready(folder, info, evidence) or type(expected) not in (int, float):
        return _blocked(
            "screen-ppi", "Screen PPI",
            info.get("reason") or "Independent iOS display Evidence or the mapped Apple physical PPI is unavailable.",
            actual, "ios-display-status.json",
        )
    passed = type(ext) in (int, float) and ext == expected and (req is None or req == expected)
    return _row(
        "screen-ppi", "Screen PPI",
        {"official_physical_ppi": expected, "request_may_be_absent": True},
        actual, passed, evidence,
        "Extended device.ppi matches the Apple physical PPI mapped from the independently captured ProductType." if passed else
        "FAILED: device.ppi does not match the mapped Apple physical PPI.",
    )


def validate_pixel_ratio(folder):
    info = _ios_display_info(folder)
    evidence = "pixel-ratio-evidence.png"
    req, ext = _wire(folder, "device.pxratio")
    logical_width = _get(info, "logical_points.width")
    logical_height = _get(info, "logical_points.height")
    native_width = _get(info, "official_spec.native_width")
    native_height = _get(info, "official_spec.native_height")
    ratios = (
        native_width / logical_width
        if all(type(value) in (int, float) and value > 0 for value in (native_width, logical_width)) else None,
        native_height / logical_height
        if all(type(value) in (int, float) and value > 0 for value in (native_height, logical_height)) else None,
    )
    expected = ratios[0] if all(type(value) in (int, float) for value in ratios) and abs(ratios[0] - ratios[1]) <= 1e-6 else None
    actual = {
        "request_pixel_ratio": req,
        "extended_pixel_ratio": ext,
        "width_formula": f"{native_width} / {logical_width}" if expected is not None else None,
        "height_formula": f"{native_height} / {logical_height}" if expected is not None else None,
        "derived_pixel_ratio": expected,
    }
    if not _display_evidence_ready(folder, info, evidence) or expected is None:
        return _blocked(
            "pixel-ratio", "Pixel Ratio",
            info.get("reason") or "Independent iOS logical points and mapped native pixels cannot produce one unambiguous pixel ratio.",
            actual, "ios-display-status.json",
        )
    passed = all(
        type(value) in (int, float) and abs(value - expected) <= 1e-6
        for value in (req, ext)
    )
    return _row(
        "pixel-ratio", "Pixel Ratio",
        {"native_pixels_divided_by_logical_points": expected, "tolerance": 1e-6},
        actual, passed, evidence,
        "Request and Extended device.pxratio match native pixels divided by independently captured logical points." if passed else
        "FAILED: device.pxratio does not match the independently derived iOS pixel ratio.",
    )


def validate_screen_brightness(folder):
    req, ext = _wire(folder, "device.ext.screen_bright")
    info = _read(folder, "ios-brightness-status.json", {}) or {}
    expected = info.get("normalized_brightness") if info.get("status") == "CAPTURED" else None
    screenshot = Path(folder) / "ios-brightness-settings.png"
    card = Path(folder) / "screen-brightness-evidence.png"
    actual = {
        "request_screen_bright": req,
        "extended_screen_bright": ext,
        "visible_slider_percent": info.get("visible_percent"),
        "normalized_brightness": expected,
        "slider_accessibility_value": info.get("slider_accessibility_value"),
    }
    if (
        type(expected) not in (int, float)
        or info.get("slider_visible_in_screenshot") is not True
        or not screenshot.is_file()
        or not card.is_file()
    ):
        return _blocked(
            "screen-brightness", "Screen Brightness",
            info.get("reason") or "The native iOS Display & Brightness slider was not captured with complete visual Evidence.",
            actual, "ios-brightness-status.json",
        )
    tolerance = .01
    passed = (
        type(ext) in (int, float) and math.isfinite(ext) and 0 <= ext <= 1
        and abs(ext - expected) <= tolerance
        and (req is None or (
            type(req) in (int, float) and math.isfinite(req) and abs(req - expected) <= tolerance
        ))
    )
    return _row(
        "screen-brightness", "Screen Brightness",
        {"visible_slider_normalized": expected, "tolerance": tolerance},
        actual, passed, "screen-brightness-evidence.png",
        "The payload brightness matches the visible native iOS slider within one percentage point." if passed else
        "FAILED: device.ext.screen_bright is invalid or differs from the visible native iOS brightness slider by more than 0.01.",
    )


def validate_font_scale(folder):
    req, ext = _wire(folder, "device.ext.fontscale")
    info = _read(folder, "ios-font-size-status.json", {}) or {}
    screenshot = Path(folder) / "ios-font-size-settings.png"
    card = Path(folder) / "font-scale-evidence.png"
    actual = {
        "request_fontscale": req,
        "extended_fontscale": ext,
        "larger_text_slider_value": info.get("slider_accessibility_value"),
        "larger_text_slider_position": info.get("slider_position"),
        "increase_button_enabled": info.get("increase_button_enabled"),
    }
    if info.get("status") != "CAPTURED" or not screenshot.is_file() or not card.is_file():
        return _blocked(
            "font-scale", "Font Scale",
            info.get("reason") or "The native iOS Larger Text page was not captured with complete visual Evidence.",
            actual, "ios-font-size-status.json",
        )
    values = [value for value in (req, ext) if value is not None]
    if not values or not all(_positive_number(value) for value in values) or not _same(req, ext):
        return _row(
            "font-scale", "Font Scale",
            {"wire": "positive consistent numeric value"},
            actual, False, "font-scale-evidence.png",
            "FAILED: device.ext.fontscale is missing, non-positive, or inconsistent between Request and Extended payloads.",
        )
    return _blocked(
        "font-scale", "Font Scale",
        "The Larger Text page visibly proves the selected Dynamic Type state, but no reviewed iOS API bridge maps that slider state to the exact payload scale yet.",
        actual, "font-scale-evidence.png",
    )


def validate_dark_mode(folder):
    req, ext = _wire(folder, "device.ext.darkmode")
    info = _read(folder, "ios-dark-mode-status.json", {}) or {}
    expected = info.get("dark_mode") if info.get("status") == "CAPTURED" else None
    screenshot = Path(folder) / "ios-dark-mode-settings.png"
    card = Path(folder) / "dark-mode-evidence.png"
    actual = {
        "request_darkmode": req,
        "extended_darkmode": ext,
        "selected_appearance": info.get("selected_appearance"),
        "visible_dark_mode": expected,
        "appearance_controls": info.get("appearance_controls"),
    }
    if type(expected) is not bool or not screenshot.is_file() or not card.is_file():
        return _blocked(
            "dark-mode", "Dark Mode",
            info.get("reason") or "The selected Light/Dark appearance was not captured with complete native iOS visual Evidence.",
            actual, "ios-dark-mode-status.json",
        )
    passed = (
        type(ext) is bool and ext is expected
        and (req is None or (type(req) is bool and req is expected))
    )
    return _row(
        "dark-mode", "Dark Mode",
        {"visible_selected_appearance": info.get("selected_appearance"), "darkmode": expected},
        actual, passed, "dark-mode-evidence.png",
        "The payload dark-mode boolean matches the visibly selected native iOS appearance." if passed else
        "FAILED: device.ext.darkmode is missing, non-boolean, or differs from the visibly selected Light/Dark appearance.",
    )


def validate_output_volume(folder):
    req, ext = _wire(folder, "device.ext.volume")
    info = _read(folder, "ios-output-volume-status.json", {}) or {}
    expected = info.get("normalized_volume") if info.get("status") == "CAPTURED" else None
    screenshot = Path(folder) / "ios-output-volume-control-center.png"
    card = Path(folder) / "output-volume-evidence.png"
    actual = {
        "request_output_volume": req,
        "extended_output_volume": ext,
        "visible_percent": info.get("visible_percent"),
        "normalized_volume": expected,
        "accessibility_text": info.get("accessibility_text"),
    }
    if type(expected) not in (int, float) or not screenshot.is_file() or not card.is_file():
        return _blocked(
            "output-volume", "Output Volume",
            info.get("reason") or "The iOS Control Center media-volume slider was not captured with complete visual Evidence.",
            actual, "output-volume-evidence.png" if card.is_file() else (
                "ios-output-volume-control-center.png" if screenshot.is_file() else "ios-output-volume-status.json"
            ),
        )
    tolerance = .01
    passed = (
        type(ext) in (int, float) and math.isfinite(ext) and 0 <= ext <= 1
        and abs(ext - expected) <= tolerance
        and (req is None or (
            type(req) in (int, float) and math.isfinite(req) and 0 <= req <= 1
            and abs(req - expected) <= tolerance
        ))
    )
    return _row(
        "output-volume", "Output Volume",
        {"visible_control_center_volume": expected, "tolerance": tolerance},
        actual, passed, "output-volume-evidence.png",
        "The payload output volume matches the visible iOS Control Center media-volume slider within 0.01." if passed else
        "FAILED: device.ext.volume is missing, invalid, or differs from the visible Control Center media-volume slider by more than 0.01.",
    )


def validate_device_make(folder):
    req, ext = _wire(folder, "device.make")
    info = _read(folder, "ios-device-identity-status.json", {}) or {}
    screenshot = Path(folder) / "ios-device-about.png"
    card = Path(folder) / "device-make-evidence.png"
    expected = info.get("official_make") if info.get("status") == "CAPTURED" else None
    actual = {
        "request_device_make": req,
        "extended_device_make": ext,
        "product_type": info.get("product_type"),
        "visible_model_name": info.get("visible_model_name"),
        "official_model": _get(info, "official_spec.model"),
        "official_make": expected,
    }
    if expected != "Apple" or not screenshot.is_file() or not card.is_file():
        return _blocked(
            "device-make", "Device Make",
            info.get("reason") or "Native About and Apple ProductType mapping Evidence is incomplete.",
            actual, "ios-device-identity-status.json",
        )
    passed = ext == expected and (req is None or req == expected)
    return _row(
        "device-make", "Device Make",
        {"official_product_manufacturer": expected},
        actual, passed, "device-make-evidence.png",
        "The payload manufacturer matches the Apple device established by native About and the official ProductType mapping." if passed else
        "FAILED: device.make does not equal Apple after native About and the official ProductType mapping establish the manufacturer.",
    )


def validate_device_model(folder):
    model_req, model_ext = _wire(folder, "device.model")
    hwv_req, hwv_ext = _wire(folder, "device.hwv")
    info = _read(folder, "ios-device-identity-status.json", {}) or {}
    screenshot = Path(folder) / "ios-device-about.png"
    card = Path(folder) / "device-model-evidence.png"
    expected_model = _get(info, "official_spec.model") if info.get("status") == "CAPTURED" else None
    expected_hwv = info.get("product_type") if info.get("status") == "CAPTURED" else None
    actual = {
        "visible_model_name": info.get("visible_model_name"),
        "official_model": expected_model,
        "product_type": expected_hwv,
        "request_device_model": model_req,
        "extended_device_model": model_ext,
        "request_device_hwv": hwv_req,
        "extended_device_hwv": hwv_ext,
    }
    if not expected_model or not expected_hwv or not screenshot.is_file() or not card.is_file():
        return _blocked(
            "device-model", "Device Model",
            info.get("reason") or "Native About and Apple ProductType mapping Evidence is incomplete.",
            actual, "ios-device-identity-status.json",
        )
    passed = (
        model_ext == expected_model and (model_req is None or model_req == expected_model)
        and hwv_ext == expected_hwv and (hwv_req is None or hwv_req == expected_hwv)
    )
    return _row(
        "device-model", "Device Model",
        {"official_model": expected_model, "product_type": expected_hwv},
        actual, passed, "device-model-evidence.png",
        "The payload model and hardware version match native About and the Apple ProductType mapping." if passed else
        "FAILED: device.model or device.hwv does not match native About and the Apple ProductType mapping.",
    )


def validate_tracking_allowed(folder):
    ia_req, ia_ext = _wire(folder, "device.ia")
    lat_req, lat_ext = _wire(folder, "device.lat")
    info = _read(folder, "ios-tracking-allowed-status.json", {}) or {}
    att = _get(info, "att.authorization")
    visible_idfa = info.get("visible_idfa")
    screenshot = Path(folder) / "tracking-allowed.png"
    idfa_screenshot = Path(folder) / "ios-idfa.png"
    card = Path(folder) / "tracking-allowed-evidence.png"
    actual = {
        "visible_app_switch": info.get("app_switch"),
        "att_authorization": att,
        "visible_idfa": visible_idfa,
        "request_device_ia": ia_req,
        "extended_device_ia": ia_ext,
        "request_device_lat": lat_req if lat_req is not None else "ABSENT",
        "extended_device_lat": lat_ext if lat_ext is not None else "ABSENT",
    }
    if (
        info.get("status") != "CAPTURED" or not screenshot.is_file()
        or not idfa_screenshot.is_file() or not card.is_file()
        or info.get("visible_idfa_status") != "CAPTURED" or not _uuid(visible_idfa)
    ):
        return _blocked(
            "tracking-allowed", "Advertising Tracking Allowed",
            info.get("reason") or "Complete native Tracking and visible non-zero IDFA Evidence was not captured before the Bid.",
            actual, "ios-tracking-allowed-status.json",
        )
    if str(att).lower() not in {"authorized", "allowed", "3"}:
        return _blocked(
            "tracking-allowed", "Advertising Tracking Allowed",
            "The visible Sample App tracking switch is not enabled, so the tracking-allowed precondition is not established; R1 does not mutate this privacy setting.",
            actual, "tracking-allowed-evidence.png",
        )
    lat_allowed = lambda value: value is None or (type(value) is int and value == 0)
    expected_idfa = str(visible_idfa).lower()
    passed = (
        _uuid(ia_req) and _uuid(ia_ext)
        and str(ia_req).lower() == expected_idfa and str(ia_ext).lower() == expected_idfa
        and lat_allowed(lat_req) and lat_allowed(lat_ext)
    )
    return _row(
        "tracking-allowed", "Advertising Tracking Allowed",
        {
            "visible_sample_app_tracking": "authorized",
            "visible_non_zero_idfa_matches_req_ext": True,
            "req_ext_device_lat": "integer 0 or ABSENT",
        },
        actual, passed, "tracking-allowed-evidence.png",
        "The visible Sample App tracking switch, visible IDFA, Request/Extended IDFA, and inverse LAT flag consistently prove tracking is allowed." if passed else
        "FAILED: with complete allowed-state Evidence, Request/Extended device.ia must match the visible non-zero IDFA and each device.lat must be integer 0 or absent.",
    )


def validate_advertising_id(folder):
    title = "Advertising Identifier (IDFA)"
    visible = _read(folder, "ios-idfa-state.json", {}) or {}
    screenshot = Path(folder) / "ios-idfa.png"
    visible_value = visible.get("value")
    if visible.get("status") != "CAPTURED" or not _uuid(visible_value) or not screenshot.is_file():
        return _blocked(
            "advertising-id", title,
            visible.get("reason") or "GetMyIDFA did not provide complete visible IDFA Evidence.",
            visible, "ios-idfa-state.json",
        )
    req, ext = _wire(folder, "device.ia")
    settings = _read(folder, "ios-settings-state.json", {}) or {}
    att = _get(settings, "att.authorization")
    settings_screenshot = Path(folder) / "ios-settings-state.png"
    expected = str(visible_value).lower()
    actual = {
        "get_my_idfa": visible_value,
        "request_device_ia": req,
        "extended_device_ia": ext,
        "sample_app_att": att,
    }
    if str(att).lower() not in {"authorized", "allowed", "3"} or not settings_screenshot.is_file():
        return _blocked(
            "advertising-id", title,
            settings.get("reason") or "Sample App ATT authorization and native Tracking screenshot are incomplete.",
            actual, "ios-settings-state.json",
        )
    passed = bool(
        str(att).lower() in {"authorized", "allowed", "3"}
        and _uuid(req) and _uuid(ext)
        and str(req).lower() == expected
        and str(ext).lower() == expected
    )
    return _row(
        "advertising-id", title,
        {"visible GetMyIDFA IDFA": visible_value, "Sample App ATT": "authorized"},
        actual, passed, "ios-idfa.png",
        "The visible GetMyIDFA value exactly matches Request and Extended device.ia under authorized ATT."
        if passed else
        "FAILED: the independently visible IDFA, Sample App ATT state, Request device.ia, and Extended device.ia do not agree.",
    )


def validate_tracking_denied(folder):
    ia_req, ia_ext = _wire(folder, "device.ia")
    lat_req, lat_ext = _wire(folder, "device.lat")
    state = _read(folder, "ios-settings-state.json", {}) or {}
    operations = state.get("operations") if isinstance(state, dict) else None
    if isinstance(operations, dict):
        state = operations.get("tracking-denied") or operations.get("advertising-id-opt-out") or {}
    att = _get(state, "att.authorization")
    ia = ia_ext if ia_ext is not None else ia_req
    lat = lat_ext if lat_ext is not None else lat_req
    if not att:
        return _blocked("tracking-denied", "Advertising Tracking Denied", "ATT denied Evidence was not captured; payload alone cannot prove the privacy state.", {"ia": ia, "lat": lat}, "tracking-denied-evidence.png")
    unusable = ia in (None, "", "00000000-0000-0000-0000-000000000000")
    passed = str(att).lower() in {"denied", "restricted", "0", "1", "2"} and unusable and lat == 1
    return _row("tracking-denied", "Advertising Tracking Denied", {"ATT": "not authorized", "IDFA": "absent/zero", "LAT": 1}, {"ATT": att, "IDFA": ia, "LAT": lat}, passed, "tracking-denied-evidence.png", "ATT, IDFA and LAT consistently prove tracking is denied." if passed else "FAILED: ATT state, IDFA and LAT violate the denied-tracking contract.")


def validate_advertising_id_opt_out(folder):
    row = validate_tracking_denied(folder)
    row.update({
        "tc": "advertising-id-opt-out", "title": "Advertising ID — Tracking Denied",
        "evidence": "advertising-id-opt-out-evidence.png",
    })
    return row


def _lifecycle_value(folder, name):
    sequence = _read(folder, "ios-lifecycle-sequence.json", {}) or {}
    return sequence.get(name)


def _lifecycle_validator(key, title, rule):
    def validate(folder):
        value = _lifecycle_value(folder, key)
        if not isinstance(value, dict) or not value.get("executed"):
            return _blocked(key, title, "The required independent iOS lifecycle sequence was not executed.", value, "ios-lifecycle-sequence.json")
        passed = bool(value.get("passed"))
        return _row(key, title, rule, value, passed, "ios-lifecycle-sequence.json", value.get("reason") or ("The iOS lifecycle sequence passed." if passed else "FAILED: the iOS lifecycle sequence did not satisfy its contract."))
    return validate


def _lifecycle_pids(folder, value):
    pids = value.get("pids") if isinstance(value.get("pids"), list) else []
    if pids:
        return pids
    sequence = _read(folder, "ios-lifecycle-sequence.json", {}) or {}
    steps = sequence.get("steps") if isinstance(sequence.get("steps"), list) else []
    return [step.get("pid") for step in steps if isinstance(step, dict)]


def _lifecycle_blocked(key, title, _expected, reason, actual):
    return _blocked(key, title, reason, actual, "ios-lifecycle-sequence.json")


def _validate_session_duration_increase(folder, key, title, pid_indexes):
    expected = {"relation": "after > before", "process_requirement": "same PID"}
    value = _lifecycle_value(folder, key)
    if not isinstance(value, dict) or not value.get("executed"):
        return _lifecycle_blocked(
            key, title, expected,
            "The required independent iOS lifecycle sequence was not executed.", value,
        )
    values = value.get("values") if isinstance(value.get("values"), list) else []
    all_pids = _lifecycle_pids(folder, value)
    pair_pids = (
        [all_pids[index] for index in pid_indexes]
        if len(all_pids) > max(pid_indexes) else []
    )
    before_ms = values[0] if len(values) >= 1 else None
    after_ms = values[1] if len(values) >= 2 else None
    actual = {
        "before_ms": before_ms,
        "after_ms": after_ms,
        "before_pid": pair_pids[0] if len(pair_pids) == 2 else None,
        "after_pid": pair_pids[1] if len(pair_pids) == 2 else None,
        "pid_probe_error": value.get("pid_probe_error"),
    }
    same_process_proven = (
        len(pair_pids) == 2
        and all(type(pid) is int for pid in pair_pids)
        and pair_pids[0] == pair_pids[1]
    )
    if not same_process_proven:
        return _lifecycle_blocked(
            key, title, expected,
            "R3 did not prove that both requests used the same iOS App PID; the same-process session comparison was not executed.",
            actual,
        )
    values_valid = (
        type(before_ms) is int and before_ms >= 0
        and type(after_ms) is int and after_ms >= 0
    )
    passed = values_valid and after_ms > before_ms
    return _row(
        key, title,
        expected,
        actual, passed, f"{key}-evidence.png",
        "Session duration increases while the same App process remains alive." if passed else
        "FAILED: session_duration did not increase within the proven same App process.",
    )


def validate_session_duration_continuous(folder):
    return _validate_session_duration_increase(
        folder, "session-duration-continuous",
        "Session Duration — Continuous App Session", (0, 1),
    )


def validate_session_duration_background(folder):
    return _validate_session_duration_increase(
        folder, "session-duration-background",
        "Session Duration — Resume from Background", (1, 2),
    )


def validate_session_duration_termination(folder):
    key = "session-duration-termination"
    title = "Session Duration — Reset after Termination"
    expected = {"relation": "after < before", "process_requirement": "new PID"}
    value = _lifecycle_value(folder, key)
    if not isinstance(value, dict) or not value.get("executed"):
        return _lifecycle_blocked(
            key, title, expected,
            "The required independent iOS lifecycle sequence was not executed.", value,
        )
    pids = _lifecycle_pids(folder, value)
    before_pid = value.get("before_pid", pids[2] if len(pids) == 4 else None)
    after_pid = value.get("after_pid", pids[3] if len(pids) == 4 else None)
    legacy_values = value.get("values") if isinstance(value.get("values"), list) else []
    before_ms = value.get("before_ms", legacy_values[0] if len(legacy_values) >= 1 else None)
    after_ms = value.get("after_ms", legacy_values[1] if len(legacy_values) >= 2 else None)
    actual = {
        "before_ms": before_ms,
        "after_ms": after_ms,
        "before_pid": before_pid,
        "after_pid": after_pid,
        "immediate_pid_exit_observed": bool(value.get("immediate_pid_exit_observed")),
        "pid_probe_error": value.get("pid_probe_error"),
    }
    if type(before_pid) is not int or type(after_pid) is not int or before_pid == after_pid:
        return _lifecycle_blocked(
            key, title, expected,
            "R3 termination setup did not prove that the old iOS App process exited and Request 4 used a new PID; the termination-dependent comparison was not executed.",
            actual,
        )
    values_valid = (
        type(before_ms) is int and before_ms >= 0
        and type(after_ms) is int and after_ms >= 0
    )
    passed = values_valid and after_ms < before_ms
    return _row(
        key, title,
        expected,
        actual, passed, "session-duration-termination-evidence.png",
        "Session duration resets after the old App process exits and a new process starts." if passed else
        "FAILED: session_duration did not reset after a proven App process restart.",
    )


def validate_app_initialization_time(folder):
    key = "app-initialization-time"
    title = "App Initialization Time"
    expected = {"requests_1_to_3": "same timestamp and PID", "request_4": "newer timestamp and new PID"}
    value = _lifecycle_value(folder, key)
    if not isinstance(value, dict) or not value.get("executed"):
        return _lifecycle_blocked(
            key, title, expected,
            "The required independent iOS lifecycle sequence was not executed.", value,
        )
    values = value.get("values") if isinstance(value.get("values"), list) else []
    pids = _lifecycle_pids(folder, value)
    actual = {
        "values": values,
        "pids": pids,
        "stable_app_init_time": values[0] if values else None,
        "restarted_app_init_time": values[3] if len(values) == 4 else None,
        "pid_probe_error": value.get("pid_probe_error"),
    }
    process_proven = (
        len(pids) == 4
        and all(type(pid) is int for pid in pids)
        and len(set(pids[:3])) == 1
        and pids[3] != pids[2]
    )
    if not process_proven:
        return _lifecycle_blocked(
            key, title, expected,
            "R3 did not prove that Requests 1–3 used one iOS App PID and Request 4 used a new PID; the process-scoped initialization comparison was not executed.",
            actual,
        )
    values_valid = len(values) == 4 and all(type(item) is int and item > 0 for item in values)
    passed = values_valid and len(set(values[:3])) == 1 and values[3] > values[2]
    return _row(
        key, title,
        expected,
        actual, passed, "app-initialization-time-evidence.png",
        "app_init_time remains stable in one process and renews after a proven process restart." if passed else
        "FAILED: app_init_time was not stable in one process or did not renew after a proven process restart.",
    )


def validate_app_duration_today(folder):
    key = "app-duration-today"
    title = "Total App Usage Time Today"
    expected = {"unit": "milliseconds", "requests_1_to_4": "monotonic non-decreasing", "restart_behavior": "must persist"}
    value = _lifecycle_value(folder, key)
    if not isinstance(value, dict) or not value.get("executed"):
        return _lifecycle_blocked(
            key, title, expected,
            "The required independent iOS lifecycle sequence was not executed.", value,
        )
    values = value.get("values") if isinstance(value.get("values"), list) else []
    pids = _lifecycle_pids(folder, value)
    actual = {
        "before_restart_ms": values[2] if len(values) == 4 else None,
        "after_restart_ms": values[3] if len(values) == 4 else None,
        "values": values,
        "pids": pids,
        "pid_probe_error": value.get("pid_probe_error"),
    }
    process_sequence_proven = (
        len(pids) == 4
        and all(type(pid) is int for pid in pids)
        and len(set(pids[:3])) == 1
        and pids[3] != pids[2]
    )
    if not process_sequence_proven:
        return _lifecycle_blocked(
            key, title, expected,
            "R3 did not prove one App PID through background/resume and a new PID after termination; the cross-process app-duration comparison was not executed.",
            actual,
        )
    values_valid = len(values) == 4 and all(type(item) is int and item >= 0 for item in values)
    passed = values_valid and all(before <= after for before, after in zip(values, values[1:]))
    return _row(
        key, title,
        expected,
        actual, passed, "app-duration-today-evidence.png",
        "Today's foreground usage remains monotonic across background and a proven process restart." if passed else
        "FAILED: app_duration decreased during the proven same-day lifecycle sequence.",
    )


def _platform_contract_pending(key, title, field):
    def validate(folder):
        req, ext = _wire(folder, field)
        return _blocked(
            key, title,
            f"The reviewed iOS contract for {field} is not defined yet; the TC remains visible but cannot PASS from an Android expectation.",
            {"request": req, "extended": ext}, "bid_decoded.json", not_executable=True,
        )
    return validate


def _sensor_out_of_scope(key, title):
    def validate(_folder):
        row = blocked(
            key, "Not In Scope: this round has no sensor motion setup or reviewed expected samples",
        ).to_dict()
        row.update({
            "layer": "Signal", "title": title,
            "description": "Sensor array is observed but not evaluated in this scope.",
            "evidence": "bid_decoded.json",
        })
        return row
    return validate


def validate_impression_history(folder):
    _, value = _wire(folder, "user.impression_history")
    passed = isinstance(value, list) and bool(value) and all(isinstance(item, dict) and item.get("bidobjid") and _positive_number(item.get("displaytime")) for item in value)
    return _row("impression-history", "Impression History", {"second_request": True, "non_empty_valid_history": True}, value, passed, "bid_decoded.json", "The second request contains a valid proven impression history." if passed else "FAILED: the second request has no valid first-impression history.")


def _tc(key, title, description, validate, evidence=(IOS_DEVICE_CONTEXT, BID)):
    return TestCase(key, title, description, evidence, validate)


TC_DEFINITIONS = {
    "advertising-id": _tc(
        "advertising-id", "Advertising Identifier (IDFA)",
        "The IDFA visibly displayed by GetMyIDFA must equal Request and Extended device.ia.",
        validate_advertising_id, (IOS_IDFA_VISIBLE, IOS_SETTINGS_STATE, BID),
    ),
    "app-set-id": _tc(
        "app-set-id", "Identifier for Vendor (IDFV)",
        "Extended device.ifv must be a non-empty lowercase UUID; visible source Evidence is not available yet.",
        validate_idfv, (BID, IOS_IDFV_PAYLOAD),
    ),
    "in-app-purchase-history": _tc(
        "in-app-purchase-history", "In App Purchase History",
        "Extended payload must contain a valid product-ID array; without an independent expected answer it remains BLOCKED.",
        validate_in_app_purchase_history, (BID, IOS_IAP_PAYLOAD),
    ),
    "boot-timestamps": _tc(
        "boot-timestamps", "System Boot Timestamps",
        "Power-on history must contain 1 to 5 strictly increasing positive epoch-millisecond integers; human-visible Evidence is currently unavailable.",
        validate_boot_timestamps, (BID, IOS_BOOT_PAYLOAD),
    ),
    "ram-total": _tc(
        "ram-total", "RAM Status (Total)",
        "Total RAM must be positive integer bytes; human-visible Evidence is currently unavailable.",
        validate_ram_total, (BID, IOS_RAM_PAYLOAD),
    ),
    "ram-available": _tc(
        "ram-available", "RAM Status (Available)",
        "Available RAM must be positive integer bytes not exceeding total RAM; human-visible Evidence is currently unavailable.",
        validate_ram_available, (BID, IOS_RAM_PAYLOAD),
    ),
    "battery-level": _tc(
        "battery-level", "Battery Level",
        "Battery percentage must be within 0 to 100; human-visible Evidence is currently unavailable.",
        validate_battery_level, (IOS_BATTERY_VISIBLE, BID),
    ),
    "charging-status": _tc(
        "charging-status", "Charging Status",
        "The boolean-compatible payload state must match the visible iOS Control Center charging state.",
        validate_charging_status, (IOS_CHARGING_VISIBLE, BID),
    ),
    "battery-saver": _tc(
        "battery-saver", "Battery Saver (Low Power Mode)",
        "The boolean payload flag must match the visible native iOS Low Power Mode switch.",
        validate_battery_saver, (IOS_LOW_POWER_VISIBLE, BID),
    ),
    "screen-width": _tc("screen-width", "Screen Width", "Request points and Extended native pixels match independent iOS display sources.", validate_screen_width, (IOS_DISPLAY_STATUS, BID)),
    "screen-height": _tc("screen-height", "Screen Height", "Request points and Extended native pixels match independent iOS display sources.", validate_screen_height, (IOS_DISPLAY_STATUS, BID)),
    "screen-ppi": _tc("screen-ppi", "Screen PPI", "Physical PPI matches the Apple specification mapped from ProductType.", validate_screen_ppi, (IOS_DISPLAY_STATUS, BID)),
    "pixel-ratio": _tc("pixel-ratio", "Pixel Ratio", "Native pixels divided by logical points match Request and Extended pxratio.", validate_pixel_ratio, (IOS_DISPLAY_STATUS, BID)),
    "screen-brightness": _tc("screen-brightness", "Screen Brightness", "The normalized payload value matches the visible native iOS brightness slider.", validate_screen_brightness, (IOS_BRIGHTNESS_VISIBLE, BID)),
    "font-scale": _tc("font-scale", "Font Scale", "The native Larger Text state is visible; exact numeric mapping remains blocked pending a reviewed API bridge.", validate_font_scale, (IOS_FONT_SIZE_VISIBLE, BID)),
    "dark-mode": _tc("dark-mode", "Dark Mode", "The boolean payload matches the visibly selected native iOS Light/Dark appearance.", validate_dark_mode, (IOS_DARK_MODE_VISIBLE, BID)),
    "gyroscope": _tc("gyroscope", "Gyroscope", "Sensor samples are outside this round scope.", _sensor_out_of_scope("gyroscope", "Gyroscope")),
    "accelerometer": _tc("accelerometer", "Accelerometer", "Sensor samples are outside this round scope.", _sensor_out_of_scope("accelerometer", "Accelerometer")),
    "output-volume": _tc("output-volume", "Output Volume", "The normalized payload value matches the visible iOS Control Center media-volume slider.", validate_output_volume, (IOS_OUTPUT_VOLUME_VISIBLE, BID)),
    "device-make": _tc("device-make", "Device Make", "Native About and the Apple ProductType mapping establish the manufacturer.", validate_device_make, (IOS_DEVICE_IDENTITY, BID)),
    "device-model": _tc("device-model", "Device Model", "Native About and Apple mapping establish model and hardware version.", validate_device_model, (IOS_DEVICE_IDENTITY, BID)),
    "default-timezone": _tc("default-timezone", "Default Timezone", "Capture-time IANA timezone establishes the exact UTC offset.", validate_default_timezone, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "default-language-iso": _tc("default-language-iso", "App Language Code", "Native Language & Region and Locale establish the language code.", validate_language_iso, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "default-language-bcp47": _tc("default-language-bcp47", "App Language and Region Tag", "Native Language & Region and Locale establish the BCP 47 tag.", validate_language_bcp47, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "keyboard-languages": _tc("keyboard-languages", "Installed Keyboard Languages", "Visible keyboard rows map to the ordered payload tags.", validate_keyboard_languages, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "root-status": _tc("root-status", "Jailbreak Status", "Validate the SDK jailbreak wire value as false without treating it as an independent integrity attestation.", validate_root_status, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "emulator-detection": _tc("emulator-detection", "Simulator Detection", "libimobiledevice ProductType establishes a physical device.", validate_emulator_detection, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "connection-type": _tc("connection-type", "Connection Type", "A checked native Wi-Fi network establishes the transport.", validate_connection_type, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "connection-type-cellular": _tc("connection-type-cellular", "Connection Type (Cellular)", "Requires an active SIM and cellular-data scenario.", validate_connection_type_cellular, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "carrier": _tc("carrier", "Carrier", "Visible No SIM establishes empty carrier identity.", validate_carrier, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "mcc-mnc": _tc("mcc-mnc", "MCC/MNC", "Visible No SIM establishes empty MCC/MNC identity.", validate_mcc_mnc, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "precise-gps-latitude": _tc("precise-gps-latitude", "Precise GPS Latitude", "Exact coordinate Evidence requires a Sample App QA surface.", validate_precise_latitude, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "precise-gps-longitude": _tc("precise-gps-longitude", "Precise GPS Longitude", "Exact coordinate Evidence requires a Sample App QA surface.", validate_precise_longitude, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "last-foreground-times": _tc("last-foreground-times", "Last Foreground Times", "Independent event timeline is required for correctness.", validate_last_foreground_times, (IOS_REVIEW_CONTEXT, BID)),
    "last-background-times": _tc("last-background-times", "Last Background Times", "Independent event timeline is required for correctness.", validate_last_background_times, (IOS_REVIEW_CONTEXT, BID)),
    "vpn-status": _tc("vpn-status", "VPN Status", "Native VPN state maps to the payload string flag.", validate_vpn_status, (IOS_SYSTEM_CONTEXT_VISIBLE, BID)),
    "force-gdpr-override": _tc("force-gdpr-override", "Force GDPR Override", "Visible Sample App configuration is required.", validate_force_gdpr_override, (IOS_REVIEW_CONTEXT, BID)),
    "coppa-applies": _tc("coppa-applies", "COPPA Applicability Flag", "Visible Sample App configuration is required.", validate_coppa_applies, (IOS_REVIEW_CONTEXT, BID)),
    "argus-sdk-version": _tc("argus-sdk-version", "Argus SDK Version", "Reviewer-supplied build answer.", validate_argus_sdk_version, (IOS_REVIEW_CONTEXT, BID)),
    "tracking-allowed": _tc("tracking-allowed", "Advertising Tracking Allowed", "Visible Sample App ATT state and IDFA agree with the inverse LAT flag.", validate_tracking_allowed, (IOS_IDFA_VISIBLE, IOS_SETTINGS_STATE, BID)),
    "sdk-version": _tc("sdk-version", "SDK Version (sdk_version)", "Reviewer-supplied build answer.", validate_sdk_version, (IOS_REVIEW_CONTEXT, BID)),
    "impression-history": _tc("impression-history", "Impression History", "Second request carries the first proven impression.", validate_impression_history),
    "network-latency": _tc("network-latency", "Connection Latency", "SDK latency is available after initialization.", _wire_validator("network-latency", "Connection Latency", "device.ext.latency", _positive_number, "positive milliseconds")),
    "session-duration-continuous": _tc("session-duration-continuous", "Session Duration — Continuous App Session", "Duration increases in the same foreground App process.", validate_session_duration_continuous, (IOS_LIFECYCLE_SEQUENCE, BID)),
    "session-duration-background": _tc("session-duration-background", "Session Duration — Resume from Background", "Duration continues after background/resume in the same App process.", validate_session_duration_background, (IOS_LIFECYCLE_SEQUENCE, BID)),
    "session-duration-termination": _tc("session-duration-termination", "Session Duration — Reset after Termination", "Duration resets only after a proven process restart.", validate_session_duration_termination, (IOS_LIFECYCLE_SEQUENCE, BID)),
    "app-initialization-time": _tc("app-initialization-time", "App Initialization Time", "Init time is stable per process and renews after a proven restart.", validate_app_initialization_time, (IOS_LIFECYCLE_SEQUENCE, BID)),
    "app-duration-today": _tc("app-duration-today", "Total App Usage Time Today", "Daily foreground duration persists monotonically across a proven process restart.", validate_app_duration_today, (IOS_LIFECYCLE_SEQUENCE, BID)),
    "advertising-id-opt-out": _tc("advertising-id-opt-out", "Advertising ID — Tracking Denied", "ATT denial suppresses usable IDFA.", validate_advertising_id_opt_out, (IOS_SETTINGS_STATE, BID)),
    "tracking-denied": _tc("tracking-denied", "Advertising Tracking Denied", "ATT denial produces LAT=1.", validate_tracking_denied, (IOS_SETTINGS_STATE, BID)),
}


def _alternate(key, title, path, predicate, expected, *, allow_missing=False):
    def validate(folder):
        req, ext = _wire(folder, path)
        values = [value for value in (req, ext) if value is not None]
        wire_ok = (allow_missing and not values) or (bool(values) and all(predicate(value) for value in values) and _same(req, ext))
        state_document = _read(folder, "ios-settings-state.json", {}) or {}
        operations = state_document.get("operations") if isinstance(state_document, dict) else None
        state = operations.get(key, {}) if isinstance(operations, dict) else state_document
        mutated_screenshot = _get(state, "stages.mutated.screenshot") or "ios-settings-state.png"
        visible = bool((state.get("confirmed_by_operator") or state.get("automation")) and state.get("screenshot_saved")
                       and (Path(folder) / mutated_screenshot).is_file())
        passed = visible and wire_ok
        return _row(
            key, title, {"visible_native_settings_state": True, "wire_rule": expected},
            {"settings": state, "request": req, "extended": ext}, passed,
            f"{key}-evidence.png",
            "The visible native iOS state and decoded Bid value agree." if passed else
            "FAILED: visible native iOS Settings Evidence is missing or the decoded Bid does not match that state.",
        )
    return _tc(key, title, "Alternate iOS state must match visible Settings Evidence.", validate, (IOS_SETTINGS_STATE, BID))


TC_DEFINITIONS.update({
    "dark-mode-enabled": _alternate("dark-mode-enabled", "Dark Mode — Enabled", "device.ext.darkmode", lambda v: v is True, "true"),
    "font-scale-maximum": _alternate("font-scale-maximum", "Font Scale — Maximum", "device.ext.fontscale", _positive_number, "matches maximum Dynamic Type state"),
    "screen-brightness-minimum": _alternate("screen-brightness-minimum", "Screen Brightness — Minimum", "device.ext.screen_bright", _fraction, "matches minimum visible slider"),
    "output-volume-muted": _alternate("output-volume-muted", "Output Volume — Muted", "device.ext.volume", lambda v: v == 0, "0"),
    "battery-saver-enabled": _alternate("battery-saver-enabled", "Low Power Mode — Enabled", "device.ext.battery_saver", lambda v: v is True, "true"),
    "screen-brightness-maximum": _alternate("screen-brightness-maximum", "Screen Brightness — Maximum", "device.ext.screen_bright", lambda v: type(v) in (int, float) and v >= .99, "approximately 1"),
    "output-volume-maximum": _alternate("output-volume-maximum", "Output Volume — Maximum", "device.ext.volume", lambda v: type(v) in (int, float) and v >= .99, "approximately 1"),
    "timezone-changed": _alternate("timezone-changed", "Timezone — Changed", "device.utcoffset", lambda v: type(v) is int, "matches changed timezone offset"),
    "location-permission-denied": _alternate("location-permission-denied", "Location Permission — Denied", "device.geo_lat", lambda v: v == 0, "coordinates absent/zero", allow_missing=True),
})


ROUND_DEFINITIONS = {
    "R1": Round("HAPPY-PATH", tuple(key for key in (
        "advertising-id", "app-set-id", "in-app-purchase-history",
        "boot-timestamps", "ram-total", "ram-available",
        "battery-level", "charging-status", "battery-saver", "screen-width", "screen-height",
        "screen-ppi", "pixel-ratio", "screen-brightness", "font-scale", "dark-mode",
        "gyroscope", "accelerometer",
        "output-volume", "device-make", "device-model", "default-timezone",
        "default-language-iso", "default-language-bcp47", "keyboard-languages", "root-status",
        "emulator-detection", "connection-type", "connection-type-cellular", "carrier", "mcc-mnc",
        "precise-gps-latitude", "precise-gps-longitude", "last-foreground-times",
        "last-background-times", "vpn-status", "force-gdpr-override", "coppa-applies",
        "argus-sdk-version", "tracking-allowed", "sdk-version",
    ) if key in TC_DEFINITIONS)),
    "R2": Round("SECOND-AD-HISTORY", ("impression-history", "network-latency"), warmup_ads=1),
    "R3": Round("LIFECYCLE-SEQUENCE", (
        "session-duration-continuous", "session-duration-background",
        "session-duration-termination", "app-initialization-time", "app-duration-today",
    ), strategy="lifecycle-sequence"),
    "R5": Round("ALTERNATE-STATE", (
        "dark-mode-enabled", "font-scale-maximum", "screen-brightness-maximum",
        "output-volume-maximum", "screen-brightness-minimum", "output-volume-muted",
        "battery-saver-enabled", "timezone-changed", "location-permission-denied",
        "advertising-id-opt-out", "tracking-denied",
    ), strategy="r5-scenarios"),
}

R5_SCENARIOS = (
    ("DISPLAY-HIGH", (
        "dark-mode-enabled", "font-scale-maximum",
        "screen-brightness-maximum", "output-volume-maximum",
    )),
    ("DISPLAY-LOW", ("screen-brightness-minimum", "output-volume-muted")),
    ("SYSTEM-ALT", (
        "battery-saver-enabled", "timezone-changed", "location-permission-denied",
    )),
    ("PRIVACY-DENIED", ("advertising-id-opt-out", "tracking-denied")),
)

R5_OPERATION_LABELS = {
    "dark-mode-enabled": "DISPLAY-DARK",
    "font-scale-maximum": "TEXT-MAX",
    "screen-brightness-minimum": "DISPLAY-LOW",
    "output-volume-muted": "AUDIO-MUTED",
    "battery-saver-enabled": "LOW-POWER",
    "screen-brightness-maximum": "DISPLAY-HIGH",
    "output-volume-maximum": "AUDIO-HIGH",
    "timezone-changed": "TIMEZONE-ALT",
    "location-permission-denied": "LOCATION-DENIED",
    "advertising-id-opt-out": "PRIVACY-DENIED",
    "tracking-denied": "PRIVACY-DENIED",
}
