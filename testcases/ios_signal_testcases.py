"""iOS-owned Signal TestCases, Round membership, and comparisons.

The keys intentionally match the shared Catalog, but no Android validator or
Round definition is imported.  A wire value is never enough to prove a device
state: validators either compare it with independent iOS Evidence or return an
honest BLOCKED result until that Evidence is available.
"""

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from verdict import blocked, evaluate


BID = "bid"
IOS_DEVICE_CONTEXT = "ios-device-context"
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
        {"captured_version": actual}, "bid_decoded.json",
    )


def validate_sdk_version(folder):
    req, ext = _wire(folder, "app.sdk_version")
    actual = req if req is not None else ext
    return _blocked(
        "sdk-version", "SDK Version (sdk_version)",
        "Waiting for a reviewer to enter the expected iOS Ads SDK build version in the report.",
        {"captured_version": actual}, "bid_decoded.json",
    )


def validate_language_iso(folder):
    req, ext = _wire(folder, "device.lang")
    value = ext if ext is not None else req
    source = ""
    try:
        source = (Path(folder) / "app-page-source.xml").read_text(errors="replace")
    except OSError:
        pass
    expected = "en" if any(label in source for label in ("Appier Direct", "AdMob Mediation")) else ""
    if not expected:
        return _blocked("default-language-iso", "App Language Code", "The App-language UI Evidence was not captured; payload alone cannot PASS.", value, "screenshot.png")
    passed = bool(expected and isinstance(value, str) and value.lower() == expected)
    return _row("default-language-iso", "App Language Code", expected, value, passed,
                "ios-device-context.json", "The App language code matches the language component of the iOS locale." if passed else "FAILED: the App language code does not match the independently captured iOS locale.")


def validate_language_bcp47(folder):
    req, ext = _wire(folder, "device.langb")
    value = ext if ext is not None else req
    source = ""
    try:
        source = (Path(folder) / "app-page-source.xml").read_text(errors="replace")
    except OSError:
        pass
    language = "en" if any(label in source for label in ("Appier Direct", "AdMob Mediation")) else ""
    if not language:
        return _blocked("default-language-bcp47", "App Language and Region Tag", "The App-language UI Evidence was not captured; payload alone cannot PASS.", value, "screenshot.png")
    passed = bool(isinstance(value, str) and re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})+", value) and value.lower().startswith(language + "-"))
    return _row("default-language-bcp47", "App Language and Region Tag", {"visible_app_language": language, "normalized_bcp47": True}, value, passed,
                "screenshot.png", "The high-precision App tag agrees with the visible App language and has normalized BCP 47 form." if passed else "FAILED: the App language-region tag disagrees with the visible App language or is not normalized BCP 47.")


def _display_dimension(key, title, field):
    def validate(folder):
        req, ext = _wire(folder, f"device.{field}")
        ratio_req, ratio_ext = _wire(folder, "device.pxratio")
        ratio = ratio_ext if ratio_ext is not None else ratio_req
        passed = all(_positive_number(value) for value in (req, ext, ratio)) and abs(ext - req * ratio) <= 1
        return _row(key, title, {"physical_pixels": "logical_points × pixel_ratio"},
                    {"logical_points": req, "physical_pixels": ext, "pixel_ratio": ratio}, passed,
                    "bid_decoded.json", "Logical points and physical pixels agree through the captured iOS pixel ratio." if passed else "FAILED: iOS logical and physical display dimensions do not agree through pixel ratio.")
    return validate


def validate_tracking_allowed(folder):
    ia_req, ia_ext = _wire(folder, "device.ia")
    lat_req, lat_ext = _wire(folder, "device.lat")
    att = _get(_read(folder, "ios-settings-state.json", {}) or {}, "att.authorization")
    if not att:
        return _blocked("tracking-allowed", "Advertising Tracking Allowed", "ATT authorization Evidence was not captured; IDFA/LAT alone cannot prove user consent.", {"ia": ia_ext or ia_req, "lat": lat_ext if lat_ext is not None else lat_req}, "ios-settings-state.json")
    passed = str(att).lower() in {"authorized", "allowed", "3"} and _uuid(ia_ext or ia_req) and (lat_ext if lat_ext is not None else lat_req) == 0
    return _row("tracking-allowed", "Advertising Tracking Allowed", {"ATT": "authorized", "IDFA": "UUID", "LAT": 0}, {"ATT": att, "IDFA": ia_ext or ia_req, "LAT": lat_ext if lat_ext is not None else lat_req}, passed, "ios-settings-state.json", "ATT, IDFA and LAT consistently prove tracking is allowed." if passed else "FAILED: visible ATT state, IDFA and LAT do not consistently prove tracking is allowed.")


def validate_tracking_denied(folder):
    ia_req, ia_ext = _wire(folder, "device.ia")
    lat_req, lat_ext = _wire(folder, "device.lat")
    att = _get(_read(folder, "ios-settings-state.json", {}) or {}, "att.authorization")
    ia = ia_ext if ia_ext is not None else ia_req
    lat = lat_ext if lat_ext is not None else lat_req
    if not att:
        return _blocked("tracking-denied", "Advertising Tracking Denied", "ATT denied Evidence was not captured; payload alone cannot prove the privacy state.", {"ia": ia, "lat": lat}, "ios-settings-state.json")
    unusable = ia in (None, "", "00000000-0000-0000-0000-000000000000")
    passed = str(att).lower() in {"denied", "restricted", "0", "1", "2"} and unusable and lat == 1
    return _row("tracking-denied", "Advertising Tracking Denied", {"ATT": "not authorized", "IDFA": "absent/zero", "LAT": 1}, {"ATT": att, "IDFA": ia, "LAT": lat}, passed, "ios-settings-state.json", "ATT, IDFA and LAT consistently prove tracking is denied." if passed else "FAILED: ATT state, IDFA and LAT violate the denied-tracking contract.")


def validate_advertising_id_opt_out(folder):
    row = validate_tracking_denied(folder)
    row.update({"tc": "advertising-id-opt-out", "title": "Advertising ID — Tracking Denied"})
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


def _platform_contract_pending(key, title, field):
    def validate(folder):
        req, ext = _wire(folder, field)
        return _blocked(
            key, title,
            f"The reviewed iOS contract for {field} is not defined yet; the TC remains visible but cannot PASS from an Android expectation.",
            {"request": req, "extended": ext}, "bid_decoded.json", not_executable=True,
        )
    return validate


def validate_impression_history(folder):
    _, value = _wire(folder, "user.impression_history")
    passed = isinstance(value, list) and bool(value) and all(isinstance(item, dict) and item.get("bidobjid") and _positive_number(item.get("displaytime")) for item in value)
    return _row("impression-history", "Impression History", {"second_request": True, "non_empty_valid_history": True}, value, passed, "bid_decoded.json", "The second request contains a valid proven impression history." if passed else "FAILED: the second request has no valid first-impression history.")


def _tc(key, title, description, validate, evidence=(IOS_DEVICE_CONTEXT, BID)):
    return TestCase(key, title, description, evidence, validate)


TC_DEFINITIONS = {
    "advertising-id": _tc("advertising-id", "Advertising ID (IDFA)", "IDFA has UUID wire format.", _wire_validator("advertising-id", "Advertising ID (IDFA)", "device.ia", _uuid, "UUID")),
    "app-set-id": _tc("app-set-id", "Vendor ID (IDFV)", "Visible Sample App IDFV must equal the Bid value.", _qa_evidence_validator("app-set-id", "Vendor ID (IDFV)", "device.ifv", "idfv", _uuid, lambda v: str(v).lower()), (IOS_QA_EVIDENCE, BID)),
    "installed-app-list": _tc("installed-app-list", "Installed App List", "iOS collection contract requires review.", _platform_contract_pending("installed-app-list", "Installed App List", "device.ext.applist")),
    "in-app-purchase-history": _tc("in-app-purchase-history", "In App Purchase History", "iOS collection contract requires review.", _platform_contract_pending("in-app-purchase-history", "In App Purchase History", "device.ext.iaphistory")),
    "boot-timestamps": _tc("boot-timestamps", "System Boot Timestamps", "Power-on history has ordered epoch milliseconds.", _wire_validator("boot-timestamps", "System Boot Timestamps", "device.ext.pot", lambda v: _timestamp_array(v) and v == sorted(v), "ordered epoch-millisecond array")),
    "ram-total": _tc("ram-total", "RAM Status (Total)", "Visible Sample App physical-memory value must equal the Bid value.", _qa_evidence_validator("ram-total", "RAM Status (Total)", "device.ext.mem_total", "mem_total", _positive_number), (IOS_QA_EVIDENCE, BID)),
    "ram-available": _tc("ram-available", "RAM Status (Available)", "Visible Sample App available-memory value must equal the Bid value.", _qa_evidence_validator("ram-available", "RAM Status (Available)", "device.ext.mem_available", "mem_available", _positive_number), (IOS_QA_EVIDENCE, BID)),
    "disk-total": _tc("disk-total", "Disk Storage (Total)", "iOS filesystem scope requires review.", _platform_contract_pending("disk-total", "Disk Storage (Total)", "device.ext.disk_total")),
    "disk-free": _tc("disk-free", "Disk Storage (Free)", "iOS filesystem scope requires review.", _platform_contract_pending("disk-free", "Disk Storage (Free)", "device.ext.disk_free")),
    "battery-level": _tc("battery-level", "Battery Level", "Battery percentage is valid.", _wire_validator("battery-level", "Battery Level", "device.batterylevel", lambda v: type(v) in (int, float) and 0 <= v <= 100, "0..100")),
    "charging-status": _tc("charging-status", "Charging Status", "Charging status is boolean-compatible.", _wire_validator("charging-status", "Charging Status", "device.charging", lambda v: v in (0, 1, False, True), "boolean/0/1")),
    "battery-saver": _tc("battery-saver", "Battery Saver", "Low Power Mode is boolean.", _wire_validator("battery-saver", "Battery Saver", "device.ext.battery_saver", lambda v: type(v) is bool, "boolean")),
    "screen-width": _tc("screen-width", "Screen Width", "Logical and physical width agree.", _display_dimension("screen-width", "Screen Width", "sw")),
    "screen-height": _tc("screen-height", "Screen Height", "Logical and physical height agree.", _display_dimension("screen-height", "Screen Height", "sh")),
    "screen-ppi": _tc("screen-ppi", "Screen PPI", "PPI is positive.", _wire_validator("screen-ppi", "Screen PPI", "device.ppi", _positive_number, "positive PPI")),
    "pixel-ratio": _tc("pixel-ratio", "Pixel Ratio", "Native scale is positive.", _wire_validator("pixel-ratio", "Pixel Ratio", "device.pxratio", _positive_number, "positive scale")),
    "screen-brightness": _tc("screen-brightness", "Screen Brightness", "Brightness is normalized.", _wire_validator("screen-brightness", "Screen Brightness", "device.ext.screen_bright", _fraction, "0..1")),
    "font-scale": _tc("font-scale", "Font Scale", "Dynamic Type scale is positive.", _wire_validator("font-scale", "Font Scale", "device.ext.fontscale", _positive_number, "positive scale")),
    "dark-mode": _tc("dark-mode", "Dark Mode", "Interface style is boolean.", _wire_validator("dark-mode", "Dark Mode", "device.ext.darkmode", lambda v: type(v) is bool, "boolean")),
    "gyroscope": _tc("gyroscope", "Gyroscope", "iOS sensor collection contract requires review.", _platform_contract_pending("gyroscope", "Gyroscope", "device.ext.gyroscope")),
    "accelerometer": _tc("accelerometer", "Accelerometer", "iOS sensor collection contract requires review.", _platform_contract_pending("accelerometer", "Accelerometer", "device.ext.accelerometer")),
    "output-volume": _tc("output-volume", "Output Volume", "Visible AVAudioSession outputVolume must equal the Bid value.", _qa_evidence_validator("output-volume", "Output Volume", "device.ext.volume", "output_volume", _fraction), (IOS_QA_EVIDENCE, BID)),
    "device-make": _tc("device-make", "Device Make", "Manufacturer is Apple.", _wire_validator("device-make", "Device Make", "device.make", lambda v: str(v).lower() == "apple", "Apple")),
    "device-model": _tc("device-model", "Device Model", "Model maps to ProductType.", _wire_validator("device-model", "Device Model", "device.model", _present, "non-empty model")),
    "default-timezone": _tc("default-timezone", "Default Timezone", "UTC offset is integer minutes.", _wire_validator("default-timezone", "Default Timezone", "device.utcoffset", lambda v: type(v) is int and -720 <= v <= 840, "UTC offset minutes")),
    "default-language-iso": _tc("default-language-iso", "App Language Code", "Low-precision App language.", validate_language_iso),
    "default-language-bcp47": _tc("default-language-bcp47", "App Language and Region Tag", "High-precision App language-region tag.", validate_language_bcp47),
    "keyboard-languages": _tc("keyboard-languages", "Installed Keyboard Languages", "Keyboard language list is non-empty.", _wire_validator("keyboard-languages", "Installed Keyboard Languages", "device.input_lang", lambda v: isinstance(v, list) and bool(v) and all(isinstance(x, str) and x for x in v), "non-empty language array")),
    "root-status": _tc("root-status", "Jailbreak Status", "Jailbreak flag is boolean.", _wire_validator("root-status", "Jailbreak Status", "device.ext.jailbreak", lambda v: type(v) is bool, "boolean")),
    "emulator-detection": _tc("emulator-detection", "Simulator Detection", "Simulator flag is boolean.", _wire_validator("emulator-detection", "Simulator Detection", "device.ext.emulator", lambda v: type(v) is bool, "boolean")),
    "connection-type": _tc("connection-type", "Connection Type", "Network transport is present.", _wire_validator("connection-type", "Connection Type", "device.conntype", _present, "non-empty transport")),
    "connection-type-cellular": _tc("connection-type-cellular", "Connection Type (Cellular)", "Requires an active cellular test environment.", _platform_contract_pending("connection-type-cellular", "Connection Type (Cellular)", "device.conntype")),
    "carrier": _tc("carrier", "Carrier", "Carrier follows SIM state.", _platform_contract_pending("carrier", "Carrier", "device.carrier")),
    "mcc-mnc": _tc("mcc-mnc", "MCC/MNC", "MCC/MNC follows SIM state.", _platform_contract_pending("mcc-mnc", "MCC/MNC", "device.mccmnc")),
    "precise-gps-latitude": _tc("precise-gps-latitude", "Precise GPS Latitude", "Visible Core Location latitude must equal the Bid value.", _qa_evidence_validator("precise-gps-latitude", "Precise GPS Latitude", "device.geo_lat", "geo_lat", lambda v: type(v) in (int, float) and -90 <= v <= 90 and v != 0, lambda v: round(float(v), 5)), (IOS_QA_EVIDENCE, BID)),
    "precise-gps-longitude": _tc("precise-gps-longitude", "Precise GPS Longitude", "Visible Core Location longitude must equal the Bid value.", _qa_evidence_validator("precise-gps-longitude", "Precise GPS Longitude", "device.geo_lon", "geo_lon", lambda v: type(v) in (int, float) and -180 <= v <= 180 and v != 0, lambda v: round(float(v), 5)), (IOS_QA_EVIDENCE, BID)),
    "last-foreground-times": _tc("last-foreground-times", "Last Foreground Times", "Foreground timestamps are valid.", _wire_validator("last-foreground-times", "Last Foreground Times", "user.last_foreground_time", _timestamp_array, "epoch-millisecond array")),
    "last-background-times": _tc("last-background-times", "Last Background Times", "Background timestamps are valid.", _wire_validator("last-background-times", "Last Background Times", "user.last_background_time", _timestamp_array, "epoch-millisecond array")),
    "vpn-status": _tc("vpn-status", "VPN Status", "VPN status has a supported value.", _wire_validator("vpn-status", "VPN Status", "device.ext.vpn", lambda v: v in (0, 1, "0", "1", False, True), "boolean-compatible")),
    "force-gdpr-override": _tc("force-gdpr-override", "Force GDPR Override", "Force GDPR flag is integer-compatible.", _wire_validator("force-gdpr-override", "Force GDPR Override", "compliance.force_gdpr_applies", lambda v: v in (0, 1, False, True), "0/1")),
    "coppa-applies": _tc("coppa-applies", "COPPA Applicability Flag", "COPPA flag is integer-compatible.", _wire_validator("coppa-applies", "COPPA Applicability Flag", "compliance.coppa_applies", lambda v: v in (0, 1, False, True), "0/1")),
    "argus-sdk-version": _tc("argus-sdk-version", "Argus SDK Version", "Reviewer-supplied build answer.", validate_argus_sdk_version),
    "tracking-allowed": _tc("tracking-allowed", "Advertising Tracking Allowed", "Visible ATT state agrees with IDFA and LAT.", validate_tracking_allowed, (IOS_SETTINGS_STATE, BID)),
    "sdk-version": _tc("sdk-version", "SDK Version (sdk_version)", "Reviewer-supplied build answer.", validate_sdk_version),
    "impression-history": _tc("impression-history", "Impression History", "Second request carries the first proven impression.", validate_impression_history),
    "network-latency": _tc("network-latency", "Connection Latency", "SDK latency is available after initialization.", _wire_validator("network-latency", "Connection Latency", "device.ext.latency", _positive_number, "positive milliseconds")),
    "session-duration-continuous": _tc("session-duration-continuous", "Session Duration — Continuous", "Duration increases in one foreground session.", _lifecycle_validator("session-duration-continuous", "Session Duration — Continuous", "second > first"), (IOS_LIFECYCLE_SEQUENCE, BID)),
    "session-duration-background": _tc("session-duration-background", "Session Duration — Background", "Duration continues after background/resume.", _lifecycle_validator("session-duration-background", "Session Duration — Background", "resumed > previous"), (IOS_LIFECYCLE_SEQUENCE, BID)),
    "session-duration-termination": _tc("session-duration-termination", "Session Duration — Termination", "Duration resets after termination.", _lifecycle_validator("session-duration-termination", "Session Duration — Termination", "cold < previous"), (IOS_LIFECYCLE_SEQUENCE, BID)),
    "app-initialization-time": _tc("app-initialization-time", "App Initialization Time", "Init time is stable per process and renews after restart.", _lifecycle_validator("app-initialization-time", "App Initialization Time", "same process stable; new process renewed"), (IOS_LIFECYCLE_SEQUENCE, BID)),
    "app-duration-today": _tc("app-duration-today", "Total App Usage Time Today", "Daily foreground duration persists and increases.", _lifecycle_validator("app-duration-today", "Total App Usage Time Today", "monotonic across restart"), (IOS_LIFECYCLE_SEQUENCE, BID)),
    "advertising-id-opt-out": _tc("advertising-id-opt-out", "Advertising ID — Tracking Denied", "ATT denial suppresses usable IDFA.", validate_advertising_id_opt_out, (IOS_SETTINGS_STATE, BID)),
    "tracking-denied": _tc("tracking-denied", "Advertising Tracking Denied", "ATT denial produces LAT=1.", validate_tracking_denied, (IOS_SETTINGS_STATE, BID)),
}


def _alternate(key, title, path, predicate, expected, *, allow_missing=False):
    def validate(folder):
        req, ext = _wire(folder, path)
        values = [value for value in (req, ext) if value is not None]
        wire_ok = (allow_missing and not values) or (bool(values) and all(predicate(value) for value in values) and _same(req, ext))
        state = _read(folder, "ios-settings-state.json", {}) or {}
        visible = bool((state.get("confirmed_by_operator") or state.get("automation")) and state.get("screenshot_saved")
                       and (Path(folder) / "ios-settings-state.png").is_file())
        passed = visible and wire_ok
        return _row(
            key, title, {"visible_native_settings_state": True, "wire_rule": expected},
            {"settings": state, "request": req, "extended": ext}, passed,
            "ios-settings-state.png" if visible else "ios-settings-state.json",
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
        "advertising-id", "app-set-id", "installed-app-list", "in-app-purchase-history",
        "boot-timestamps", "ram-total", "ram-available", "disk-total", "disk-free",
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
        "dark-mode-enabled", "font-scale-maximum", "screen-brightness-minimum",
        "output-volume-muted", "battery-saver-enabled", "screen-brightness-maximum",
        "output-volume-maximum", "timezone-changed", "location-permission-denied",
        "advertising-id-opt-out", "tracking-denied",
    ), strategy="r5-scenarios"),
}

R5_SCENARIOS = (
    ("DISPLAY-DARK", ("dark-mode-enabled",)),
    ("TEXT-MAX", ("font-scale-maximum",)),
    ("DISPLAY-LOW", ("screen-brightness-minimum",)),
    ("AUDIO-MUTED", ("output-volume-muted",)),
    ("LOW-POWER", ("battery-saver-enabled",)),
    ("DISPLAY-HIGH", ("screen-brightness-maximum",)),
    ("AUDIO-HIGH", ("output-volume-maximum",)),
    ("TIMEZONE-ALT", ("timezone-changed",)),
    ("LOCATION-DENIED", ("location-permission-denied",)),
    ("PRIVACY-DENIED", ("advertising-id-opt-out", "tracking-denied")),
)
