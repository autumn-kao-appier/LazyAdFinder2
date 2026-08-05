"""Reviewed Android Signal TestCases, validators, and Round registry."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from evidence_aos import (
    ADS_SETTINGS,
    APP_SET_ID,
    BID,
    BOOT_TIMESTAMPS,
    BATTERY_STATUS,
    DISPLAY_STATUS,
    DEVICE_CONTEXT,
    IN_APP_PURCHASE_HISTORY,
    INSTALLED_APP_LIST,
    RESOURCE_STATUS,
    SDK_BUILD_INFO,
)
from verdict import blocked, evaluate


UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
ZERO_GAID = "00000000-0000-0000-0000-000000000000"
ABSENT = "ABSENT"


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


def _decoded(folder):
    return json.loads((Path(folder) / "bid_decoded.json").read_text())


def _ads_state(folder):
    return json.loads((Path(folder) / "ads-settings-state.json").read_text())


def _decoded_device_value(document, section, field):
    plaintext = document.get(section, {}).get("plaintext", {})
    device = plaintext.get("device") if isinstance(plaintext, dict) else None
    return device.get(field) if isinstance(device, dict) else None


def _verdict(key, title, description, expected, actual, evidence, failures):
    row = evaluate(
        key,
        expected=expected,
        actual=actual,
        evidence=evidence,
        compare=lambda _expected, _actual: not failures,
        reason="; ".join(failures),
    ).to_dict()
    row.update({
        "layer": "Signal",
        "title": title,
        "description": description,
        "comparison_view": _comparison_view(key, expected, actual),
    })
    return row


def _comparison_view(key, expected, actual):
    """Describe what Page should show without asking Page to reinterpret a TC."""
    compare = {
        "advertising-id": ("Visible Android GAID", actual.get("settings_gaid"), "SDK Payload", actual.get("ext_device_ia"), "="),
        "boot-timestamps": ("Calculated boot time", actual.get("current_boot_reference_ms"), "SDK Payload", (actual.get("pot") or [None])[-1], "≈"),
        "ram-total": ("Captured OS bytes", expected.get("system_reference_bytes"), "SDK Payload", actual.get("payload_bytes"), "≈"),
        "ram-available": ("Captured OS bytes", expected.get("system_reference_bytes"), "SDK Payload", actual.get("payload_bytes"), "≈"),
        "disk-total": ("Captured OS bytes", expected.get("system_reference_bytes"), "SDK Payload", actual.get("payload_bytes"), "≈"),
        "disk-free": ("Captured OS bytes", expected.get("system_reference_bytes"), "SDK Payload", actual.get("payload_bytes"), "≈"),
        "battery-level": ("Visible battery level", expected.get("level"), "SDK Payload", actual.get("batterylevel"), "≈"),
        "charging-status": ("Captured power state", expected.get("charging"), "SDK Payload", actual.get("charging"), "="),
        "battery-saver": ("Visible Battery Saver", expected.get("battery_saver"), "SDK Payload", actual.get("battery_saver"), "="),
        "screen-width": ("Captured screen width", expected.get("width"), "SDK Payload", actual.get("sw"), "="),
        "screen-height": ("Captured screen height", expected.get("height"), "SDK Payload", actual.get("sh"), "="),
        "screen-ppi": ("Android logical density", expected.get("logical_density_dpi"), "SDK Payload", actual.get("ppi"), "="),
        "pixel-ratio": ("Density ÷ 160", expected.get("pixel_ratio"), "SDK Payload", actual.get("pxratio"), "="),
        "screen-brightness": ("Captured brightness", expected.get("screen_brightness"), "SDK Payload", actual.get("screen_bright"), "≈"),
        "font-scale": ("Android font scale", expected.get("font_scale"), "SDK Payload", actual.get("fontscale"), "="),
        "dark-mode": ("Visible Dark theme", expected.get("dark_mode"), "SDK Payload", actual.get("darkmode"), "="),
        "output-volume": ("Android Media volume", expected.get("volume_normalized"), "SDK Payload", actual.get("volume"), "≈"),
        "device-make": ("Android manufacturer", expected.get("make"), "SDK req/ext", f'{actual.get("req_make")} / {actual.get("make")}', "="),
        "device-model": ("Android product model", expected.get("model"), "SDK model / hwv", f'{actual.get("model")} / {actual.get("hwv")}', "="),
        "default-timezone": ("Android UTC offset", expected.get("utcoffset"), "SDK req/ext", f'{actual.get("req_utcoffset")} / {actual.get("utcoffset")}', "="),
        "default-language-iso": ("Android language", expected.get("lang"), "SDK Payload", actual.get("lang"), "="),
        "default-language-bcp47": ("Android locale tag", expected.get("langb"), "SDK req/ext", f'{actual.get("req_langb")} / {actual.get("langb")}', "="),
        "keyboard-languages": ("Enabled Gboard languages", expected.get("input_lang"), "SDK Payload", actual.get("input_lang"), "="),
        "root-status": ("Android root probe", expected.get("jailbreak"), "SDK jailbreak", actual.get("jailbreak"), "="),
        "emulator-detection": ("Android hardware probe", expected.get("emulator"), "SDK emulator", actual.get("emulator"), "="),
        "connection-type": ("Active Android network", expected.get("conntype"), "SDK req/ext", f'{actual.get("req_conntype")} / {actual.get("conntype")}', "="),
        "carrier": ("Android SIM state", expected.get("carrier"), "SDK Payload", actual.get("carrier"), "="),
        "mcc-mnc": ("Android SIM state", expected.get("mccmnc"), "SDK Payload", actual.get("mccmnc"), "="),
        "tracking-allowed": ("Visible opt-out state", actual.get("visible_opt_out"), "SDK Payload · req/ext", f'{actual.get("req_device_lat")} / {actual.get("ext_device_lat")}', "↔"),
    }
    criteria = {
        "advertising-id": "Visible GAID and SDK req/ext values must be the same valid lowercase UUID.",
        "boot-timestamps": "Latest timestamp must match device time minus uptime within 120 seconds.",
        "ram-total": "Payload must be within 2% of the captured OS value.",
        "ram-available": "Dynamic payload must remain within the reviewed capture tolerance.",
        "disk-total": "Payload must be within 2% of the captured /data filesystem value.",
        "disk-free": "Dynamic payload must remain within the reviewed capture tolerance.",
        "battery-level": "Payload must be within 2 percentage points of the visible battery level.",
        "charging-status": "Payload must equal the captured Android power state.",
        "battery-saver": "Payload must equal the visible Battery Saver switch.",
        "screen-width": "Payload must equal the captured screen width in pixels.",
        "screen-height": "Payload must equal the captured screen height in pixels.",
        "screen-ppi": "Payload must equal wm density; mapped physical PPI must be within ±5%.",
        "pixel-ratio": "Payload must equal Android logical density divided by 160.",
        "screen-brightness": "Payload must match Android brightness normalized from 0–255 to 0–1.",
        "font-scale": "Payload must equal the current Android font scale.",
        "dark-mode": "Payload must equal the visible Android Dark theme state.",
        "output-volume": "Payload must equal Android Media volume normalized by its maximum level.",
        "device-make": "Request and extended payload manufacturer must equal Android ro.product.manufacturer.",
        "device-model": "Payload model and hardware version must equal Android ro.product.model.",
        "default-timezone": "Request and extended UTC offset minutes must equal the device timezone at capture time.",
        "default-language-iso": "ISO-639-1 language must equal the language component of the Android system locale.",
        "default-language-bcp47": "Request and extended BCP 47 tags must equal the normalized Android system locale.",
        "keyboard-languages": "Payload list must exactly match the enabled Gboard language tags.",
        "root-status": "Payload jailbreak boolean must match an independent Android root probe.",
        "emulator-detection": "Payload emulator boolean must match Android hardware properties.",
        "connection-type": "Request and extended connection types must match the active Android network transport.",
        "carrier": "With no active SIM, carrier must be an empty string.",
        "mcc-mnc": "With no active SIM, MCC/MNC must be an empty string.",
        "tracking-allowed": "Visible opt-out OFF must agree with SDK tracking-allowed flags.",
    }
    if key in compare:
        captured_label, captured, payload_label, payload, operator = compare[key]
        view = {
            "kind": "compare",
            "criterion": criteria[key],
            "captured": {"label": captured_label, "value": captured},
            "actual": {"label": payload_label, "value": payload},
            "operator": operator,
        }
        if key == "screen-ppi":
            view["supporting"] = f'Official physical PPI: {expected.get("official_physical_ppi")} (supporting check)'
        return view
    if key == "sdk-version":
        required = expected.get("build_sdk_version")
        return {
            "kind": "compare",
            "criterion": "Decoded app.sdk_version must match the reviewed build target.",
            "captured": {"label": "Reviewed Build Target", "value": required},
            "actual": {"label": "Decoded Bid Request", "value": actual.get("req_app_sdk_version")},
            "operator": "=",
        }
    rule_actual = {
        "app-set-id": actual.get("ext_device_ifv"),
        "installed-app-list": actual.get("packages"),
    }.get(key, actual)
    rule = {
        "app-set-id": "SDK value must be a non-empty lowercase UUID; no independent Sample App display exists yet.",
        "installed-app-list": "Unavailable, empty, or a valid unique package list is allowed; no exact fixed list is required.",
    }.get(key, "Actual SDK payload must satisfy the reviewed TestCase rule.")
    return {
        "kind": "rule",
        "criterion": rule,
        "actual": {"label": "Actual SDK Payload", "value": rule_actual},
    }


def validate_advertising_id(folder):
    key = "advertising-id"
    title = "Advertising ID (GAID)"
    description = "Visible Android advertising ID matches req/ext device.ia."
    state = _ads_state(folder)
    decoded = _decoded(folder)
    actual = {
        "settings_gaid": state.get("gaid"),
        "opt_out": state.get("opt_out"),
        "req_device_ia": _decoded_device_value(decoded, "req", "ia"),
        "ext_device_ia": _decoded_device_value(decoded, "ext", "ia"),
    }
    values = (actual["settings_gaid"], actual["req_device_ia"], actual["ext_device_ia"])
    failures = []
    if actual["opt_out"] is not False:
        failures.append("Opt out of Ads Personalization is not visibly off")
    if not all(isinstance(value, str) and UUID_RE.fullmatch(value) for value in values):
        failures.append("settings/req/ext GAID is missing or not a lowercase UUID")
    if any(value == ZERO_GAID for value in values):
        failures.append("GAID is all zeros")
    if len(set(values)) != 1:
        failures.append("visible GAID, req.device.ia, and ext.device.ia do not match")
    return _verdict(
        key,
        title,
        description,
        {"opt_out": False, "format": "lowercase UUID 8-4-4-4-12", "non_zero": True, "settings_equals_req_equals_ext": True},
        actual,
        "ads-settings.png",
        failures,
    )


def _lat_value(document, section):
    plaintext = document.get(section, {}).get("plaintext", {})
    device = plaintext.get("device") if isinstance(plaintext, dict) else None
    if not isinstance(device, dict) or "lat" not in device:
        return False, None
    return True, device["lat"]


def _allowed_lat(present, value):
    return not present or (type(value) is int and value == 0)


def validate_tracking_allowed(folder):
    key = "tracking-allowed"
    title = "Limit Ad Tracking Flag (tracking allowed)"
    description = "Visible opt-out OFF agrees with req/ext device.lat."
    state = _ads_state(folder)
    decoded = _decoded(folder)
    req_present, req_value = _lat_value(decoded, "req")
    ext_present, ext_value = _lat_value(decoded, "ext")
    actual = {
        "visible_opt_out": state.get("opt_out"),
        "req_device_lat": req_value if req_present else ABSENT,
        "ext_device_lat": ext_value if ext_present else ABSENT,
    }
    failures = []
    if actual["visible_opt_out"] is not False:
        failures.append("Opt out of Ads Personalization is not visibly off")
    if not _allowed_lat(req_present, req_value):
        failures.append(f"req.device.lat must be integer 0 or absent, got {req_value!r}")
    if not _allowed_lat(ext_present, ext_value):
        failures.append(f"ext.device.lat must be integer 0 or absent, got {ext_value!r}")
    return _verdict(
        key,
        title,
        description,
        {"visible_opt_out": False, "req_device_lat": "integer 0 or ABSENT", "ext_device_lat": "integer 0 or ABSENT"},
        actual,
        "ads-settings.png",
        failures,
    )


def validate_sdk_version(folder):
    key = "sdk-version"
    title = "SDK Version (sdk_version)"
    description = "Request app.sdk_version matches the independently declared build version."
    info = json.loads((Path(folder) / "sdk-build-info.json").read_text())
    expected = info.get("expected", {})
    actual = info.get("actual", {})
    expected_version = expected.get("build_sdk_version")
    actual_version = actual.get("req_app_sdk_version")
    failures = []
    if not isinstance(expected_version, str) or not expected_version:
        failures.append("expected build SDK version is empty")
    if not isinstance(actual_version, str) or not actual_version:
        failures.append("req.app.sdk_version is missing or empty")
    elif actual_version != expected_version:
        failures.append(f"req.app.sdk_version {actual_version!r} does not match build {expected_version!r}")
    return _verdict(key, title, description, expected, actual, "sdk-build-info.json", failures)


def validate_app_set_id(folder):
    key = "app-set-id"
    title = "Vendor ID (App Set ID)"
    description = "Extended payload contains a non-empty lowercase App Set ID in device.ifv."
    decoded = _decoded(folder)
    ext_value = _decoded_device_value(decoded, "ext", "ifv")
    failures = []
    if not isinstance(ext_value, str) or not ext_value:
        failures.append("ext.device.ifv is missing or empty")
    elif not UUID_RE.fullmatch(ext_value):
        failures.append("ext.device.ifv is not a lowercase UUID in 8-4-4-4-12 form")
    return _verdict(
        key,
        title,
        description,
        {"ext_device_ifv": "non-empty lowercase UUID 8-4-4-4-12"},
        {"ext_device_ifv": ext_value},
        "app-set-id.json",
        failures,
    )


def validate_installed_app_list(folder):
    key = "installed-app-list"
    title = "Installed App List"
    description = "Extended payload carries a valid installed-app list or an allowed empty/unavailable state."
    decoded = _decoded(folder)
    plaintext = decoded.get("ext", {}).get("plaintext", {})
    if not isinstance(plaintext, dict):
        raise ValueError("ext plaintext is unavailable; installed-app-list was not executed")
    device = plaintext.get("device")
    if not isinstance(device, dict):
        raise ValueError("ext.device is unavailable; installed-app-list was not executed")
    device_ext = device.get("ext") if isinstance(device, dict) else None
    present = isinstance(device_ext, dict) and "applist" in device_ext
    value = device_ext.get("applist") if present else None
    failures = []
    if not present:
        state = "UNAVAILABLE"
        packages = None
    elif not isinstance(value, list):
        state = "INVALID"
        packages = value
        failures.append(f"ext.device.ext.applist must be an array or absent, got {type(value).__name__}")
    elif not value:
        state = "EMPTY"
        packages = value
    else:
        state = "CAPTURED"
        packages = value
        invalid = [
            item
            for item in value
            if not isinstance(item, str) or not PACKAGE_NAME_RE.fullmatch(item)
        ]
        if invalid:
            failures.append("ext.device.ext.applist contains an invalid package name")
        valid_strings = [item for item in value if isinstance(item, str)]
        if len(valid_strings) != len(set(valid_strings)):
            failures.append("ext.device.ext.applist contains duplicate package names")
    return _verdict(
        key,
        title,
        description,
        {
            "allowed_states": ["UNAVAILABLE", "EMPTY", "CAPTURED"],
            "captured_items": "unique syntactically valid package-name strings",
        },
        {
            "collection_status": state,
            "package_count": len(value) if isinstance(value, list) else 0,
            "packages": packages,
        },
        "installed-apps-settings.png",
        failures,
    )


def _device_ext_field(decoded, field):
    plaintext = decoded.get("ext", {}).get("plaintext", {})
    if not isinstance(plaintext, dict):
        raise ValueError(f"ext plaintext is unavailable; {field} was not executed")
    device = plaintext.get("device")
    if not isinstance(device, dict):
        raise ValueError(f"ext.device is unavailable; {field} was not executed")
    device_ext = device.get("ext")
    if not isinstance(device_ext, dict):
        return False, None
    return field in device_ext, device_ext.get(field)


def validate_in_app_purchase_history(folder):
    key = "in-app-purchase-history"
    title = "In App Purchase History"
    description = "Extended payload sends a valid product-ID array; an empty Sample App result is allowed."
    present, value = _device_ext_field(_decoded(folder), "iaphistory")
    failures = []
    if not present:
        failures.append("ext.device.ext.iaphistory is missing")
    elif not isinstance(value, list):
        failures.append("ext.device.ext.iaphistory must be an array")
    else:
        invalid = [item for item in value if not isinstance(item, str) or not item.strip()]
        if invalid:
            failures.append("ext.device.ext.iaphistory contains a non-string or empty product ID")
        valid_strings = [item for item in value if isinstance(item, str)]
        if len(valid_strings) != len(set(valid_strings)):
            failures.append("ext.device.ext.iaphistory contains duplicate product IDs")
    if failures:
        return _verdict(
            key,
            title,
            description,
            {"field_present": True, "value": "array of unique non-empty product-ID strings"},
            {
                "field_present": present,
                "product_count": len(value) if isinstance(value, list) else 0,
                "product_ids": value,
            },
            "in-app-purchase-history.json",
            failures,
        )
    row = blocked(
        key,
        "Sample App has no purchase flow or independent expected product IDs; "
        "the captured array cannot be verified for correctness",
    ).to_dict()
    row.update({"layer": "Signal", "title": title, "description": description})
    return row


def validate_boot_timestamps(folder):
    key = "boot-timestamps"
    title = "System Boot Timestamps"
    description = "Power-on timestamps are ordered and the latest matches device clock minus uptime."
    info = json.loads((Path(folder) / "boot-timestamps.json").read_text())
    expected_boot = info.get("current_boot_time_ms")
    captured_epoch = info.get("captured_epoch_ms")
    present, value = _device_ext_field(_decoded(folder), "pot")
    failures = []
    if not present:
        failures.append("ext.device.ext.pot is missing")
    elif not isinstance(value, list):
        failures.append("ext.device.ext.pot must be an array")
    else:
        if not 1 <= len(value) <= 5:
            failures.append(f"ext.device.ext.pot must contain 1 to 5 timestamps, got {len(value)}")
        if any(type(item) is not int or item <= 0 for item in value):
            failures.append("ext.device.ext.pot must contain positive integer epoch milliseconds")
        elif any(left >= right for left, right in zip(value, value[1:])):
            failures.append("ext.device.ext.pot must be strictly increasing")
        elif isinstance(captured_epoch, int) and any(item > captured_epoch + 120_000 for item in value):
            failures.append("ext.device.ext.pot contains a timestamp later than the capture time")
        if value and isinstance(expected_boot, int) and type(value[-1]) is int:
            if abs(value[-1] - expected_boot) > 120_000:
                failures.append("latest pot does not match device clock minus uptime within 120 seconds")
    return _verdict(
        key,
        title,
        description,
        {
            "count": "1 to 5",
            "format": "strictly increasing positive epoch milliseconds",
            "latest": "device date - /proc/uptime ±120 seconds",
        },
        {
            "field_present": present,
            "timestamp_count": len(value) if isinstance(value, list) else 0,
            "pot": value,
            "current_boot_reference_ms": expected_boot,
        },
        "boot-time-calculation.png",
        failures,
    )


def _validate_resource_status(folder, key, title, field, dynamic=False):
    description = f"Extended payload {field} matches an independent Android system snapshot."
    info = json.loads((Path(folder) / "resource-status.json").read_text())
    expected = info.get("reference", {}).get(field)
    actual = info.get("actual", {}).get(field)
    comparison = info.get("comparisons", {}).get(field, {})
    failures = []
    if type(actual) is not int or actual <= 0:
        failures.append(f"ext.device.ext.{field} must be a positive integer byte count")
    if field.endswith("available") and type(actual) is int:
        total = info.get("actual", {}).get("mem_total")
        if type(total) is int and actual > total:
            failures.append("mem_available cannot exceed mem_total")
    if field.endswith("free") and type(actual) is int:
        total = info.get("actual", {}).get("disk_total")
        if type(total) is int and actual > total:
            failures.append("disk_free cannot exceed disk_total")
    if not comparison.get("within_tolerance"):
        failures.append(f"{field} differs from the independent system snapshot beyond tolerance")
    return _verdict(
        key,
        title,
        description,
        {
            "system_reference_bytes": expected,
            "tolerance_bytes": comparison.get("tolerance_bytes"),
            "policy": "dynamic capture tolerance" if dynamic else "within 2%",
        },
        {
            "payload_bytes": actual,
            "difference_bytes": comparison.get("difference_bytes"),
        },
        "resource-status-calculation.png",
        failures,
    )


def validate_ram_total(folder):
    row = _validate_resource_status(folder, "ram-total", "RAM Status (Total)", "mem_total")
    row["evidence"] = "mem-total-evidence.png"
    return row


def validate_ram_available(folder):
    row = _validate_resource_status(folder, "ram-available", "RAM Status (Available)", "mem_available", True)
    row["evidence"] = "mem-available-evidence.png"
    return row


def validate_disk_total(folder):
    row = _validate_resource_status(folder, "disk-total", "Disk Storage (Total)", "disk_total")
    row["evidence"] = "disk-total-evidence.png"
    return row


def validate_disk_free(folder):
    row = _validate_resource_status(folder, "disk-free", "Disk Storage (Free)", "disk_free", True)
    row["evidence"] = "disk-free-evidence.png"
    return row


def _status_info(folder, name):
    return json.loads((Path(folder) / name).read_text())


def _simple_status(folder, key, title, info_file, expected_key, actual_key, evidence, tolerance=0):
    info = _status_info(folder, info_file)
    expected = info.get(expected_key)
    actual = info.get("actual", {}).get(actual_key)
    failures = []
    if type(actual) is not type(expected):
        failures.append(f"{actual_key} has wrong type or is missing")
    elif isinstance(expected, int) and abs(actual - expected) > tolerance:
        failures.append(f"{actual_key} differs from Android source beyond tolerance")
    elif actual != expected:
        failures.append(f"{actual_key} does not match Android source")
    return _verdict(key, title, f"{title} matches the direct Android source.", {expected_key: expected, "tolerance": tolerance}, {actual_key: actual}, evidence, failures)


def validate_battery_level(folder): return _simple_status(folder, "battery-level", "Battery Level", "battery-status.json", "level", "batterylevel", "battery-settings.png", 2)
def validate_charging_status(folder): return _simple_status(folder, "charging-status", "Charging Status", "battery-status.json", "charging", "charging", "battery-settings.png")
def validate_battery_saver(folder): return _simple_status(folder, "battery-saver", "Battery Saver", "battery-status.json", "battery_saver", "battery_saver", "battery-saver-settings.png")
def validate_screen_width(folder): return _simple_status(folder, "screen-width", "Screen Width", "display-status.json", "width", "sw", "screen-width-evidence.png")
def validate_screen_height(folder): return _simple_status(folder, "screen-height", "Screen Height", "display-status.json", "height", "sh", "screen-height-evidence.png")


def validate_screen_ppi(folder):
    info = _status_info(folder, "display-status.json")
    expected = info.get("density_dpi")
    actual = info.get("actual", {}).get("ppi")
    official = info.get("official_spec") or {}
    physical = official.get("physical_ppi")
    difference = abs(actual - physical) if type(actual) is int and type(physical) in (int, float) else None
    within_five_percent = difference is None or difference / physical <= 0.05
    failures = []
    if type(actual) is not int or actual != expected:
        failures.append("ppi does not match Android logical density")
    if not within_five_percent:
        failures.append("logical density differs from the mapped official physical PPI by more than 5%")
    return _verdict(
        "screen-ppi",
        "Screen PPI",
        "Logical density matches Android and is reasonable against the mapped official panel PPI.",
        {"logical_density_dpi": expected, "official_physical_ppi": physical, "physical_tolerance": "±5%"},
        {"ppi": actual, "difference_from_physical_ppi": difference, "within_physical_tolerance": within_five_percent},
        "screen-ppi-evidence.png",
        failures,
    )


def _validate_display_value(folder, key, title, expected_key, actual_key, tolerance=0):
    info = _status_info(folder, "display-status.json")
    expected = info.get(expected_key)
    actual = info.get("actual", {}).get(actual_key)
    failures = []
    if expected_key == "dark_mode":
        if type(actual) is not bool or actual is not expected:
            failures.append(f"{actual_key} does not match Android Dark theme")
    elif type(actual) not in (int, float) or abs(actual - expected) > tolerance:
        failures.append(f"{actual_key} differs from the captured Android value beyond tolerance")
    return _verdict(key, title, f"{title} matches the direct Android source.", {expected_key: expected, "tolerance": tolerance}, {actual_key: actual}, f"{key}-evidence.png", failures)


def validate_pixel_ratio(folder): return _validate_display_value(folder, "pixel-ratio", "Pixel Ratio", "pixel_ratio", "pxratio", 1e-6)
def validate_screen_brightness(folder): return _validate_display_value(folder, "screen-brightness", "Screen Brightness", "screen_brightness", "screen_bright", 1 / 255 + 1e-8)
def validate_font_scale(folder): return _validate_display_value(folder, "font-scale", "Font Scale", "font_scale", "fontscale", 1e-6)
def validate_dark_mode(folder): return _validate_display_value(folder, "dark-mode", "Dark Mode", "dark_mode", "darkmode")


def _context_info(folder):
    return _status_info(folder, "device-context.json")


def validate_output_volume(folder):
    info = _context_info(folder); expected = info["volume_normalized"]; actual = info["actual"]; value = actual.get("volume")
    failures = [] if type(value) in (int, float) and abs(value - expected) <= 1 / info["volume_max"] + 1e-8 else ["volume does not match normalized Android Media volume"]
    return _verdict("output-volume", "Output Volume", "Output volume matches Android Media volume.", {"volume_normalized": expected, "current": info["volume_current"], "max": info["volume_max"]}, actual, "volume-evidence.png", failures)


def validate_device_make(folder):
    info = _context_info(folder); actual = info["actual"]; expected = info["make"]
    failures = [name for name in ("req_make", "make") if actual.get(name) != expected]
    return _verdict("device-make", "Device Make", "Manufacturer matches Android.", {"make": expected}, actual, "make-evidence.png", [f"{', '.join(failures)} do not match Android manufacturer"] if failures else [])


def validate_device_model(folder):
    info = _context_info(folder); actual = info["actual"]; expected = info["model"]
    names = ("req_model", "model", "req_hwv", "hwv"); failures = [name for name in names if actual.get(name) != expected]
    return _verdict("device-model", "Device Model", "Model and hardware version match Android.", {"model": expected}, actual, "model-evidence.png", [f"{', '.join(failures)} do not match Android model"] if failures else [])


def validate_default_timezone(folder):
    info = _context_info(folder); actual = info["actual"]; expected = info["utcoffset"]
    failures = [name for name in ("req_utcoffset", "utcoffset") if actual.get(name) != expected]
    return _verdict("default-timezone", "Default Timezone", "UTC offset minutes match Android timezone.", {"utcoffset": expected, "timezone": info["timezone"]}, actual, "utcoffset-evidence.png", [f"{', '.join(failures)} do not match Android UTC offset"] if failures else [])


def validate_default_language_iso(folder):
    info = _context_info(folder); actual = info["actual"]; expected = info["lang"]
    return _verdict("default-language-iso", "Default Language (ISO-639-1)", "Language matches Android locale.", {"lang": expected}, actual, "lang-evidence.png", [] if actual.get("lang") == expected else ["lang does not match Android locale language"])


def validate_default_language_bcp47(folder):
    info = _context_info(folder); actual = info["actual"]; expected = info["langb"]
    failures = [name for name in ("req_langb", "langb") if actual.get(name) != expected]
    return _verdict("default-language-bcp47", "Default Language (BCP 47)", "Language tag matches Android locale.", {"langb": expected}, actual, "langb-evidence.png", [f"{', '.join(failures)} do not match Android locale tag"] if failures else [])


def _validate_context_exact(folder, key, title, expected_key, actual_names, evidence):
    info = _context_info(folder); actual = info["actual"]; expected = info[expected_key]
    failures = [name for name in actual_names if actual.get(name) != expected]
    return _verdict(key, title, f"{title} matches the direct Android source.", {expected_key: expected}, actual, evidence, [f"{', '.join(failures)} do not match Android"] if failures else [])


def validate_keyboard_languages(folder): return _validate_context_exact(folder, "keyboard-languages", "Installed Keyboard Languages", "input_lang", ("input_lang",), "input_lang-evidence.png")
def validate_root_status(folder): return _validate_context_exact(folder, "root-status", "Root Status", "jailbreak", ("jailbreak",), "jailbreak-evidence.png")
def validate_emulator_detection(folder): return _validate_context_exact(folder, "emulator-detection", "Emulator Detection", "emulator", ("emulator",), "emulator-evidence.png")
def validate_connection_type(folder): return _validate_context_exact(folder, "connection-type", "Connection Type", "conntype", ("req_conntype", "conntype"), "conntype-evidence.png")


def _validate_no_sim_value(folder, key, title, actual_key, evidence):
    info = _context_info(folder); actual = info["actual"]; failures = []
    if not info.get("no_active_sim"): failures.append("device has an active SIM; populated carrier validation is not defined yet")
    elif actual.get(actual_key) != "": failures.append(f"{actual_key} must be empty when Android has no active SIM")
    return _verdict(key, title, f"{title} reflects Android subscription state.", {actual_key: "", "no_active_sim": info.get("no_active_sim")}, actual, evidence, failures)


def validate_carrier(folder): return _validate_no_sim_value(folder, "carrier", "Carrier", "carrier", "carrier-evidence.png")
def validate_mcc_mnc(folder): return _validate_no_sim_value(folder, "mcc-mnc", "MCC/MNC", "mccmnc", "mccmnc-evidence.png")


def _round_blocked(key, title, reason):
    row = blocked(key, reason).to_dict(); row.update({"layer": "Signal", "title": title, "description": reason}); return row


def validate_ipv6(_folder): return _round_blocked("ipv6-address", "IPv6 Address", "Round limitation: no reviewed IPv6 payload field is present in this capture")
def validate_precise_latitude(_folder): return _round_blocked("precise-gps-latitude", "Precise GPS Latitude", "Not In Scope: location ground-truth capture is not defined; device.lat is the tracking flag, not latitude")
def validate_precise_longitude(_folder): return _round_blocked("precise-gps-longitude", "Precise GPS Longitude", "Not In Scope: location ground-truth capture is not defined; the observed payload path is device.geo_lon")
def validate_session_duration(_folder): return _round_blocked("foreground-session-duration", "Current Foreground Session Duration", "Round limitation: SampleApp session start timestamp and field unit are not yet exposed")


def _sensor_out_of_scope(key, title):
    row = blocked(key, "Not In Scope: this round has no sensor motion setup or reviewed expected samples").to_dict()
    row.update({"layer": "Signal", "title": title, "description": "Sensor array is observed but not evaluated in this scope."})
    return row


def validate_gyroscope(_folder): return _sensor_out_of_scope("gyroscope", "Gyroscope")
def validate_accelerometer(_folder): return _sensor_out_of_scope("accelerometer", "Accelerometer")


TC_DEFINITIONS = {
    "advertising-id": TestCase(
        "advertising-id",
        "Advertising ID (GAID)",
        "Visible Android advertising ID matches req/ext device.ia.",
        (ADS_SETTINGS, BID),
        validate_advertising_id,
    ),
    "tracking-allowed": TestCase(
        "tracking-allowed",
        "Limit Ad Tracking Flag (tracking allowed)",
        "Visible opt-out OFF agrees with req/ext device.lat.",
        (ADS_SETTINGS, BID),
        validate_tracking_allowed,
    ),
    "app-set-id": TestCase(
        "app-set-id",
        "Vendor ID (App Set ID)",
        "Extended payload contains a non-empty lowercase App Set ID in device.ifv.",
        (APP_SET_ID, BID),
        validate_app_set_id,
    ),
    "installed-app-list": TestCase(
        "installed-app-list",
        "Installed App List",
        "Extended payload carries a valid installed-app list or an allowed empty/unavailable state.",
        (INSTALLED_APP_LIST, BID),
        validate_installed_app_list,
    ),
    "in-app-purchase-history": TestCase(
        "in-app-purchase-history",
        "In App Purchase History",
        "Extended payload sends a valid product-ID array; an empty Sample App result is allowed.",
        (IN_APP_PURCHASE_HISTORY, BID),
        validate_in_app_purchase_history,
    ),
    "boot-timestamps": TestCase(
        "boot-timestamps",
        "System Boot Timestamps",
        "Power-on timestamps are ordered and the latest matches device clock minus uptime.",
        (BOOT_TIMESTAMPS, BID),
        validate_boot_timestamps,
    ),
    "ram-total": TestCase("ram-total", "RAM Status (Total)", "Total RAM matches the Android system snapshot.", (RESOURCE_STATUS, BID), validate_ram_total),
    "ram-available": TestCase("ram-available", "RAM Status (Available)", "Available RAM is valid and matches the near-time system snapshot.", (RESOURCE_STATUS, BID), validate_ram_available),
    "disk-total": TestCase("disk-total", "Disk Storage (Total)", "Total app-data filesystem storage matches the Android system snapshot.", (RESOURCE_STATUS, BID), validate_disk_total),
    "disk-free": TestCase("disk-free", "Disk Storage (Free)", "Free app-data filesystem storage is valid and matches the near-time system snapshot.", (RESOURCE_STATUS, BID), validate_disk_free),
    "battery-level": TestCase("battery-level", "Battery Level", "Battery percentage matches Android.", (BATTERY_STATUS, BID), validate_battery_level),
    "charging-status": TestCase("charging-status", "Charging Status", "Charging flag matches Android power state.", (BATTERY_STATUS, BID), validate_charging_status),
    "battery-saver": TestCase("battery-saver", "Battery Saver", "Battery Saver matches the visible Android setting.", (BATTERY_STATUS, BID), validate_battery_saver),
    "screen-width": TestCase("screen-width", "Screen Width", "Screen width matches Android display pixels.", (DISPLAY_STATUS, BID), validate_screen_width),
    "screen-height": TestCase("screen-height", "Screen Height", "Screen height matches Android display pixels.", (DISPLAY_STATUS, BID), validate_screen_height),
    "screen-ppi": TestCase("screen-ppi", "Screen PPI", "Logical density DPI matches Android.", (DISPLAY_STATUS, BID), validate_screen_ppi),
    "pixel-ratio": TestCase("pixel-ratio", "Pixel Ratio", "Pixel ratio matches Android density scale.", (DISPLAY_STATUS, BID), validate_pixel_ratio),
    "screen-brightness": TestCase("screen-brightness", "Screen Brightness", "Normalized brightness matches Android.", (DISPLAY_STATUS, BID), validate_screen_brightness),
    "font-scale": TestCase("font-scale", "Font Scale", "Font scale matches Android.", (DISPLAY_STATUS, BID), validate_font_scale),
    "dark-mode": TestCase("dark-mode", "Dark Mode", "Dark mode matches Android UI mode.", (DISPLAY_STATUS, BID), validate_dark_mode),
    "gyroscope": TestCase("gyroscope", "Gyroscope", "Sensor samples are outside this round scope.", (BID,), validate_gyroscope),
    "accelerometer": TestCase("accelerometer", "Accelerometer", "Sensor samples are outside this round scope.", (BID,), validate_accelerometer),
    "output-volume": TestCase("output-volume", "Output Volume", "Normalized Media volume matches Android.", (DEVICE_CONTEXT, BID), validate_output_volume),
    "device-make": TestCase("device-make", "Device Make", "Manufacturer matches Android.", (DEVICE_CONTEXT, BID), validate_device_make),
    "device-model": TestCase("device-model", "Device Model", "Model and hardware version match Android.", (DEVICE_CONTEXT, BID), validate_device_model),
    "default-timezone": TestCase("default-timezone", "Default Timezone", "UTC offset matches Android.", (DEVICE_CONTEXT, BID), validate_default_timezone),
    "default-language-iso": TestCase("default-language-iso", "Default Language (ISO-639-1)", "Language code matches Android.", (DEVICE_CONTEXT, BID), validate_default_language_iso),
    "default-language-bcp47": TestCase("default-language-bcp47", "Default Language (BCP 47)", "Language tag matches Android.", (DEVICE_CONTEXT, BID), validate_default_language_bcp47),
    "keyboard-languages": TestCase("keyboard-languages", "Installed Keyboard Languages", "Enabled keyboard languages match Android.", (DEVICE_CONTEXT, BID), validate_keyboard_languages),
    "root-status": TestCase("root-status", "Root Status", "Root detection matches Android.", (DEVICE_CONTEXT, BID), validate_root_status),
    "emulator-detection": TestCase("emulator-detection", "Emulator Detection", "Emulator detection matches Android.", (DEVICE_CONTEXT, BID), validate_emulator_detection),
    "ipv6-address": TestCase("ipv6-address", "IPv6 Address", "IPv6 validation requires a reviewed payload field.", (BID,), validate_ipv6),
    "connection-type": TestCase("connection-type", "Connection Type", "Connection transport matches Android.", (DEVICE_CONTEXT, BID), validate_connection_type),
    "carrier": TestCase("carrier", "Carrier", "Carrier reflects SIM state.", (DEVICE_CONTEXT, BID), validate_carrier),
    "mcc-mnc": TestCase("mcc-mnc", "MCC/MNC", "MCC/MNC reflects SIM state.", (DEVICE_CONTEXT, BID), validate_mcc_mnc),
    "precise-gps-latitude": TestCase("precise-gps-latitude", "Precise GPS Latitude", "Precise location is outside this round scope.", (BID,), validate_precise_latitude),
    "precise-gps-longitude": TestCase("precise-gps-longitude", "Precise GPS Longitude", "Precise location is outside this round scope.", (BID,), validate_precise_longitude),
    "foreground-session-duration": TestCase("foreground-session-duration", "Current Foreground Session Duration", "Session timing requires app instrumentation.", (BID,), validate_session_duration),
    "sdk-version": TestCase(
        "sdk-version",
        "SDK Version (sdk_version)",
        "Request app.sdk_version matches the independently declared build version.",
        (SDK_BUILD_INFO, BID),
        validate_sdk_version,
    ),
}

ROUND_DEFINITIONS = {
    "R1": Round(
        "TRACKING-ALLOWED",
        (
            "advertising-id",
            "app-set-id",
            "installed-app-list",
            "in-app-purchase-history",
            "boot-timestamps",
            "ram-total",
            "ram-available",
            "disk-total",
            "disk-free",
            "battery-level",
            "charging-status",
            "battery-saver",
            "screen-width",
            "screen-height",
            "screen-ppi",
            "pixel-ratio",
            "screen-brightness",
            "font-scale",
            "dark-mode",
            "gyroscope",
            "accelerometer",
            "output-volume",
            "device-make",
            "device-model",
            "default-timezone",
            "default-language-iso",
            "default-language-bcp47",
            "keyboard-languages",
            "root-status",
            "emulator-detection",
            "ipv6-address",
            "connection-type",
            "carrier",
            "mcc-mnc",
            "precise-gps-latitude",
            "precise-gps-longitude",
            "foreground-session-duration",
            "tracking-allowed",
            "sdk-version",
        ),
    ),
}
