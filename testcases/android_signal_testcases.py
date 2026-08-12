"""Reviewed Android Signal TestCases, validators, and Round registry."""

import json
import ipaddress
import math
import re
from dataclasses import dataclass
from pathlib import Path

from evidence_aos import (
    ADS_SETTINGS,
    ADS_TRACKING_DENIED,
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
    VOLUME_STATUS,
    TIMEZONE_STATUS,
    LOCATION_PERMISSION_STATUS,
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
    warmup_ads: int = 0
    strategy: str = "standard"


def _decoded(folder):
    return json.loads((Path(folder) / "bid_decoded.json").read_text())


def _ads_state(folder):
    return json.loads((Path(folder) / "ads-settings-state.json").read_text())


def _decoded_device_value(document, section, field):
    plaintext = document.get(section, {}).get("plaintext", {})
    device = plaintext.get("device") if isinstance(plaintext, dict) else None
    return device.get(field) if isinstance(device, dict) else None


def _decoded_user_value(document, field):
    plaintext = document.get("ext", {}).get("plaintext", {})
    user = plaintext.get("user") if isinstance(plaintext, dict) else None
    return user.get(field) if isinstance(user, dict) else None


def _decoded_compliance_value(document, field):
    plaintext = document.get("req", {}).get("plaintext", {})
    compliance = plaintext.get("compliance") if isinstance(plaintext, dict) else None
    return compliance.get(field) if isinstance(compliance, dict) else None


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
        "default-language-iso": ("Primary Android system language code", expected.get("lang"), "SDK ext", actual.get("lang"), "="),
        "default-language-bcp47": ("Primary Android system language and region", expected.get("langb"), "SDK req/ext", f'{actual.get("req_langb")} / {actual.get("langb")}', "="),
        "keyboard-languages": ("Enabled Gboard languages", expected.get("input_lang"), "SDK Payload", actual.get("input_lang"), "="),
        "root-status": ("Android root probe", expected.get("jailbreak"), "SDK jailbreak", actual.get("jailbreak"), "="),
        "emulator-detection": ("Android hardware probe", expected.get("emulator"), "SDK emulator", actual.get("emulator"), "="),
        "connection-type": ("Active Android network", expected.get("conntype"), "SDK req/ext", f'{actual.get("req_conntype")} / {actual.get("conntype")}', "="),
        "tracking-allowed": ("Settings: tracking allowed", actual.get("visible_tracking_allowed"), "LAT inverse flag · req/ext", f'{actual.get("req_limit_ad_tracking")} / {actual.get("ext_limit_ad_tracking")}', "↔"),
        "app-initialization-time": ("Requests 1–3 · same PID", actual.get("stable_app_init_time"), "Request 4 · new PID", actual.get("restarted_app_init_time"), "≠"),
        "app-duration-today": ("Request 3 · before restart", actual.get("before_restart_ms"), "Request 4 · after restart", actual.get("after_restart_ms"), "≤"),
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
        "default-language-iso": "The extended ISO-639-1 code must equal the language component of the primary Android system locale.",
        "default-language-bcp47": "Request and extended BCP 47 tags must equal the primary Android system language and region.",
        "keyboard-languages": "Payload list must exactly match the enabled Gboard language tags.",
        "root-status": "Payload jailbreak boolean must match an independent Android root probe.",
        "emulator-detection": "Payload emulator boolean must match Android hardware properties.",
        "connection-type": "Request and extended connection types must match the active Android network transport.",
        "tracking-allowed": "Android Ads settings visibly show an active advertising ID; because LAT means Limit Ad Tracking, its inverse flag must be 0 or absent.",
        "app-initialization-time": "Initialization time stays fixed within one process and is renewed after process restart.",
        "app-duration-today": "Today's foreground usage is monotonic and persists across process restart.",
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
    }.get(key, "The decoded Bid Request must satisfy the reviewed TestCase rule.")
    return {
        "kind": "rule",
        "criterion": rule,
        "actual": {"label": "Decoded Bid Request", "value": rule_actual},
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
    if state.get("tracking_allowed") is not True:
        failures.append("Advertising ID settings do not visibly show tracking as allowed")
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
        {"tracking_allowed": True, "format": "lowercase UUID 8-4-4-4-12", "non_zero": True, "settings_equals_req_equals_ext": True},
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
    title = "Advertising Tracking Allowed"
    description = "The user allows tracking; device.lat is the inverse Limit Ad Tracking flag."
    state = _ads_state(folder)
    decoded = _decoded(folder)
    req_present, req_value = _lat_value(decoded, "req")
    ext_present, ext_value = _lat_value(decoded, "ext")
    actual = {
        "visible_tracking_allowed": state.get("tracking_allowed") is True,
        "req_limit_ad_tracking": req_value if req_present else ABSENT,
        "ext_limit_ad_tracking": ext_value if ext_present else ABSENT,
    }
    failures = []
    if actual["visible_tracking_allowed"] is not True:
        failures.append("Ads settings do not visibly allow advertising tracking")
    if not _allowed_lat(req_present, req_value):
        failures.append(f"req.device.lat must be integer 0 or absent, got {req_value!r}")
    if not _allowed_lat(ext_present, ext_value):
        failures.append(f"ext.device.lat must be integer 0 or absent, got {ext_value!r}")
    return _verdict(
        key,
        title,
        description,
        {"visible_tracking_allowed": True, "req_limit_ad_tracking": "0 (not limited) or ABSENT", "ext_limit_ad_tracking": "0 (not limited) or ABSENT"},
        actual,
        "tracking-allowed.png",
        failures,
    )


def validate_advertising_id_opt_out(folder):
    key = "advertising-id-opt-out"
    decoded = _decoded(folder)
    state = json.loads((Path(folder) / "tracking-denied-state.json").read_text())
    req = _decoded_device_value(decoded, "req", "ia")
    ext = _decoded_device_value(decoded, "ext", "ia")
    protected = lambda value: value is None or value == "" or value == ZERO_GAID
    failures = []
    if state.get("tracking_allowed") is not False:
        failures.append("Advertising ID settings do not visibly show tracking as denied")
    if state.get("visual_contract") != "advertising-id-disabled-visible-v3":
        failures.append("privacy screenshot does not prove a disabled Advertising ID state")
    if not protected(req) or not protected(ext):
        failures.append("req/ext device.ia must be absent, empty, or the zero advertising ID when tracking is denied")
    return _verdict(
        key, "Advertising ID — Tracking Denied",
        "A visibly disabled Advertising ID must prevent the SDK from sending a usable advertising ID.",
        {"visible_tracking_denied": True, "req_ext_device_ia": "ABSENT, empty, or zero UUID"},
        {"visible_tracking_denied": state.get("tracking_allowed") is False, "req_device_ia": req if req is not None else ABSENT, "ext_device_ia": ext if ext is not None else ABSENT},
        "advertising-id-opt-out.png", failures,
    )


def validate_tracking_denied(folder):
    key = "tracking-denied"
    state = json.loads((Path(folder) / "tracking-denied-state.json").read_text())
    decoded = _decoded(folder)
    req_present, req = _lat_value(decoded, "req")
    ext_present, ext = _lat_value(decoded, "ext")
    failures = []
    if state.get("tracking_allowed") is not False:
        failures.append("Advertising ID settings do not visibly show tracking as denied")
    if state.get("visual_contract") != "advertising-id-disabled-visible-v3":
        failures.append("privacy screenshot does not prove a disabled Advertising ID state")
    if not req_present or type(req) is not int or req != 1:
        failures.append(f"req.device.lat must be integer 1, got {req!r}")
    if not ext_present or type(ext) is not int or ext != 1:
        failures.append(f"ext.device.lat must be integer 1, got {ext!r}")
    return _verdict(
        key, "Advertising Tracking Denied",
        "A visibly disabled Advertising ID means tracking is denied and the inverse LAT flag is enabled.",
        {"visible_tracking_denied": True, "req_device_lat": 1, "ext_device_lat": 1},
        {"visible_tracking_denied": state.get("tracking_allowed") is False, "req_device_lat": req if req_present else ABSENT, "ext_device_lat": ext if ext_present else ABSENT},
        "tracking-denied.png", failures,
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
    if not isinstance(expected_version, str) or not expected_version:
        row = blocked(key, "Waiting for a reviewer to enter the expected SDK version in the report").to_dict()
        row.update({
            "layer": "Signal",
            "title": title,
            "description": "The request version was captured; comparison waits for an independently entered expected version.",
            "actual": actual,
            "evidence": "sdk-build-info.json",
            "comparison_view": {
                "kind": "manual-expected",
                "criterion": "Enter the intended build SDK version; an exact match passes and a mismatch fails.",
                "actual": {"label": "Decoded Bid Request", "value": actual_version},
            },
        })
        return row
    failures = []
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


def validate_dark_mode_enabled(folder):
    row = _validate_display_value(folder, "dark-mode-enabled", "Dark Mode — Enabled", "dark_mode", "darkmode")
    if row["actual"].get("darkmode") is not True:
        row["status"] = "FAILED"; row["reason"] = "R5 mutation did not produce darkmode=true"
    row["evidence"] = "dark-mode-evidence.png"
    return row


def validate_font_scale_maximum(folder):
    row = _validate_display_value(folder, "font-scale-maximum", "Font Scale — Maximum", "font_scale", "fontscale", 1e-6)
    info = _status_info(folder, "display-status.json")
    if info.get("font_scale_ui_maximum") is not True:
        row["status"] = "FAILED"; row["reason"] = "R5 mutation did not reach the rightmost native Font size position"
    row["evidence"] = "font-scale-evidence.png"
    return row


def validate_screen_brightness_minimum(folder):
    row = _validate_display_value(folder, "screen-brightness-minimum", "Screen Brightness — Minimum", "screen_brightness", "screen_bright", 1 / 255 + 1e-8)
    info = _status_info(folder, "display-status.json")
    if info.get("brightness_raw") != 1:
        row["status"] = "FAILED"; row["reason"] = "R5 mutation did not produce Android minimum brightness raw 1"
    elif not info.get("visual_evidence", {}).get("quick_settings") and not info.get("visual_evidence", {}).get("display_page"):
        row["status"] = "FAILED"; row["reason"] = "Android minimum brightness matched, but no native visual brightness evidence was captured"
    row["evidence"] = "screen-brightness-evidence.png"
    return row


def validate_screen_brightness_maximum(folder):
    row = _validate_display_value(folder, "screen-brightness-maximum", "Screen Brightness — Maximum", "screen_brightness", "screen_bright", 1 / 255 + 1e-8)
    info = _status_info(folder, "display-status.json")
    if info.get("brightness_ui_percent") != "100%":
        row["status"] = "FAILED"; row["reason"] = "R5 mutation did not produce Android Display brightness 100%"
    row["evidence"] = "screen-brightness-evidence.png"
    return row


def _context_info(folder):
    info = _status_info(folder, "device-context.json")

    # Evidence captured before the system-locale contract was finalized used
    # `locale`/`langb_system_hint`; later captures used `device_locale` but did
    # not yet persist `langb_system`. Normalize those historical schemas so an
    # archived report is always re-evaluated against the same current rule.
    device_locale = info.get("device_locale") or info.get("locale")
    if device_locale:
        normalized = str(device_locale).replace("_", "-")
        parts = normalized.split("-", 1)
        normalized = parts[0].lower() + (f"-{parts[1].upper()}" if len(parts) == 2 else "")
        info.setdefault("device_locale", normalized)
        info.setdefault("lang", parts[0].lower())
        info.setdefault("langb_system", info.get("langb_system_hint") or normalized)
    return info


def validate_output_volume(folder):
    info = _context_info(folder); expected = info["volume_normalized"]; actual = info["actual"]; value = actual.get("volume")
    failures = [] if type(value) in (int, float) and abs(value - expected) <= 1 / info["volume_max"] + 1e-8 else ["volume does not match normalized Android Media volume"]
    return _verdict("output-volume", "Output Volume", "Output volume matches Android Media volume.", {"volume_normalized": expected, "current": info["volume_current"], "max": info["volume_max"]}, actual, "volume-evidence.png", failures)


def validate_output_volume_muted(folder):
    info = _status_info(folder, "volume-status.json"); value = info.get("actual")
    failures = []
    if info.get("current") != 0:
        failures.append("R5 mutation did not mute Android media volume")
    if type(value) not in (int, float) or value != 0:
        failures.append(f"device.ext.volume must be 0 while muted, got {value!r}")
    return _verdict("output-volume-muted", "Output Volume — Muted", "Muted Android media volume must produce zero.", {"volume": 0}, {"volume": value}, "volume-evidence.png", failures)


def validate_output_volume_maximum(folder):
    info = _status_info(folder, "volume-status.json"); value = info.get("actual")
    failures = []
    if info.get("current") != info.get("max"):
        failures.append("R5 mutation did not set Android media volume to its maximum")
    if type(value) not in (int, float) or abs(value - 1) > 1e-8:
        failures.append(f"device.ext.volume must be 1 at maximum volume, got {value!r}")
    return _verdict("output-volume-maximum", "Output Volume — Maximum", "Maximum Android media volume must normalize to one.", {"volume": 1, "current_equals_max": True}, {"volume": value, "current": info.get("current"), "max": info.get("max")}, "volume-evidence.png", failures)


def validate_timezone_changed(folder):
    info = _status_info(folder, "timezone-status.json"); actual = info.get("actual", {})
    failures = []
    if info.get("timezone") != "America/New_York":
        failures.append("R5 mutation did not set timezone to America/New_York")
    for name in ("req_utcoffset", "ext_utcoffset"):
        if actual.get(name) != info.get("utcoffset"):
            failures.append(f"{name} does not match the current Android UTC offset")
    return _verdict("timezone-changed", "Timezone — Changed", "A changed Android timezone must update both request offsets.", {"timezone": "America/New_York", "utcoffset": info.get("utcoffset")}, actual, "timezone-changed.png", failures)


def validate_location_permission_denied(folder):
    info = _status_info(folder, "location-permission-status.json"); actual = info.get("actual", {})
    failures = []
    if info.get("denied") is not True:
        failures.append("Android location permission is not denied")
    for section in ("req", "ext"):
        values = actual.get(section, {})
        if values.get("geo_lat_present") or values.get("geo_lon_present"):
            failures.append(f"{section}.device must not contain geo_lat or geo_lon when location permission is denied")
    return _verdict("location-permission-denied", "Location Permission — Denied", "Denied location permission must suppress precise coordinates.", {"permission_denied": True, "geo_fields_absent": True}, actual, "location-permission-denied.png", failures)


def validate_battery_saver_enabled(folder):
    info = _status_info(folder, "battery-status.json"); actual = info.get("actual", {}).get("battery_saver")
    failures = [] if info.get("battery_saver") is True and actual is True else ["Battery Saver ON must produce device.ext.battery_saver=true"]
    return _verdict("battery-saver-enabled", "Battery Saver — Enabled", "Visible Battery Saver ON matches the payload boolean.", {"battery_saver": True}, {"battery_saver": actual}, "battery-saver-settings.png", failures)


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
    return _verdict(
        "default-language-iso",
        "System Language Code",
        "Extended device.lang contains the ISO-639-1 component of the primary Android system language.",
        {"lang": expected},
        {"lang": actual.get("lang")},
        "lang-evidence.png",
        [] if actual.get("lang") == expected else ["lang does not match the primary Android system language code"],
    )


def validate_default_language_bcp47(folder):
    info = _context_info(folder)
    actual = info["actual"]
    expected = info["langb_system"]
    failures = [name for name in ("req_langb", "langb") if actual.get(name) != expected]
    return _verdict(
        "default-language-bcp47",
        "System Language and Region Tag",
        "Request and extended device.langb contain the complete BCP 47 tag of the primary Android system language and region.",
        {"langb": expected},
        {"req_langb": actual.get("req_langb"), "langb": actual.get("langb")},
        "langb-evidence.png",
        [f"{', '.join(failures)} do not match the primary Android system language tag"] if failures else [],
    )


def _validate_context_exact(folder, key, title, expected_key, actual_names, evidence):
    info = _context_info(folder); actual = info["actual"]; expected = info[expected_key]
    failures = [name for name in actual_names if actual.get(name) != expected]
    return _verdict(key, title, f"{title} matches the direct Android source.", {expected_key: expected}, actual, evidence, [f"{', '.join(failures)} do not match Android"] if failures else [])


def validate_keyboard_languages(folder): return _validate_context_exact(folder, "keyboard-languages", "Installed Keyboard Languages", "input_lang", ("input_lang",), "input_lang-evidence.png")
def validate_root_status(folder): return _validate_context_exact(folder, "root-status", "Root Status", "jailbreak", ("jailbreak",), "jailbreak-evidence.png")
def validate_emulator_detection(folder): return _validate_context_exact(folder, "emulator-detection", "Emulator Detection", "emulator", ("emulator",), "emulator-evidence.png")
def validate_connection_type(folder): return _validate_context_exact(folder, "connection-type", "Connection Type", "conntype", ("req_conntype", "conntype"), "conntype-evidence.png")


def validate_connection_type_cellular(_folder):
    return _round_blocked("connection-type-cellular", "Connection Type (Cellular)", "Hardware limitation: QA device has no active SIM; 4G/5G transport cannot be established")


def _validate_cellular_identity(folder, key, title):
    info = _context_info(folder)
    if info.get("no_active_sim"):
        reason = f"Hardware limitation: QA device has no active SIM; {title} cannot be captured or verified"
    else:
        reason = f"Round limitation: an independent Android {title} reference is not captured yet"
    row = blocked(key, reason).to_dict()
    row.update({"layer": "Signal", "title": title, "description": reason})
    return row


def validate_carrier(folder): return _validate_cellular_identity(folder, "carrier", "Carrier")
def validate_mcc_mnc(folder): return _validate_cellular_identity(folder, "mcc-mnc", "MCC/MNC")


def _round_blocked(key, title, reason, *, actual=None, evidence=None):
    row = blocked(key, reason).to_dict()
    if actual is not None:
        row["actual"] = actual
    if evidence:
        row["evidence"] = evidence
    row.update({"layer": "Signal", "title": title, "description": reason})
    return row


def validate_ipv6(folder):
    folder = Path(folder)
    decoded = _decoded(folder)
    value = _decoded_device_value(decoded, "ext", "ipv6")
    response_path = folder / "ipv6-net-probe-response.json"
    events_path = folder / "proxy-events.jsonl"
    response = json.loads(response_path.read_text()) if response_path.exists() else {}
    probe_ipv6 = response.get("ipv6") if isinstance(response, dict) else None
    events = []
    if events_path.exists():
        for line in events_path.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("kind") == "ipv6-net-probe":
                events.append(event)
    successful_probe = any(event.get("phase") == "response" and event.get("status") == 200 for event in events)
    if not successful_probe or not response_path.exists():
        return _round_blocked(
            "ipv6-address", "IPv6 Address",
            "Environment prerequisite unavailable: no successful Appier adx6 IPv6 probe was captured",
        )
    failures = []
    try:
        if not probe_ipv6 or ipaddress.ip_address(probe_ipv6).version != 6:
            failures.append("Appier net probe response must contain a valid IPv6 address")
    except ValueError:
        failures.append("Appier net probe response contains an invalid IPv6 address")
    try:
        if not value or ipaddress.ip_address(value).version != 6:
            failures.append("ext device.ipv6 must contain a valid IPv6 address")
    except ValueError:
        failures.append("ext device.ipv6 contains an invalid IPv6 address")
    if probe_ipv6 != value:
        failures.append("ext device.ipv6 does not equal the Appier net probe response")
    return _verdict(
        "ipv6-address", "IPv6 Address",
        "AOS obtains public IPv6 from the Appier adx6 net endpoint.",
        {"endpoint": "https://adx6.apx.appier.net/v2/sdk/net", "http_status": 200, "ipv6": probe_ipv6},
        {"probe_events": len(events), "ext_device_ipv6": value},
        "ipv6-net-probe-response.json", failures,
    )
def _gps_distance_m(expected_lat, expected_lon, actual_lat, actual_lon):
    radius = 6_371_000
    lat1, lat2 = math.radians(expected_lat), math.radians(actual_lat)
    dlat = lat2 - lat1
    dlon = math.radians(actual_lon - expected_lon)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


def _validate_precise_location(folder, key, title, field):
    info = _context_info(folder)
    expected_lat = info.get("location_latitude")
    expected_lon = info.get("location_longitude")
    accuracy = info.get("location_accuracy_m")
    actual_lat = info.get("actual", {}).get("geo_lat")
    actual_lon = info.get("actual", {}).get("geo_lon")
    failures = []
    if not all(type(value) in (int, float) for value in (expected_lat, expected_lon, actual_lat, actual_lon)):
        failures.append("Android fused location and ext.device geo_lat/geo_lon must all be numeric")
        distance = None
    else:
        distance = _gps_distance_m(expected_lat, expected_lon, actual_lat, actual_lon)
        tolerance = max(float(accuracy or 0), 200.0)
        if distance > tolerance:
            failures.append(f"payload location differs from Android fused location by {distance:.1f} m (tolerance {tolerance:.1f} m)")
    return _verdict(
        key, title,
        "The decoded coordinate must agree with Android fused last-known location within its reviewed accuracy tolerance.",
        {"geo_lat": expected_lat, "geo_lon": expected_lon, "accuracy_m": accuracy},
        {"geo_lat": actual_lat, "geo_lon": actual_lon, "distance_m": distance, "checked_field": field},
        f"{field}-evidence.png", failures,
    )


def validate_precise_latitude(folder):
    return _validate_precise_location(folder, "precise-gps-latitude", "Precise GPS Latitude", "geo_lat")


def validate_precise_longitude(folder):
    return _validate_precise_location(folder, "precise-gps-longitude", "Precise GPS Longitude", "geo_lon")
def _session_sequence(folder):
    return json.loads((Path(folder) / "session-duration-sequence.json").read_text())


def _session_pair_verdict(folder, key, title, before_index, after_index, relation, evidence):
    document = _session_sequence(folder)
    steps = document.get("steps", [])
    failures = []
    if len(steps) != 4:
        failures.append(f"R3 must contain exactly four requests, got {len(steps)}")
        before = after = {}
    else:
        before, after = steps[before_index], steps[after_index]
    before_value = before.get("session_duration")
    after_value = after.get("session_duration")
    if type(before_value) is not int or before_value < 0:
        failures.append("before session_duration must be a non-negative integer")
    if type(after_value) is not int or after_value < 0:
        failures.append("after session_duration must be a non-negative integer")
    if relation == "increase":
        if before.get("pid") != after.get("pid"):
            failures.append("App PID changed during a session that must remain alive")
        if not failures and after_value <= before_value:
            failures.append("session_duration did not increase")
        criterion = "Session duration increases while the same App process remains alive."
    else:
        actual = {
            "before_ms": before_value,
            "after_ms": after_value,
            "before_pid": before.get("pid"),
            "after_pid": after.get("pid"),
            "immediate_pid_exit_observed": bool(document.get("terminated_pid_confirmed")),
        }
        if before.get("pid") == after.get("pid"):
            return _round_blocked(
                key,
                title,
                "R3 termination setup did not produce a new App process; the termination-dependent comparison was not executed",
                actual=actual,
                evidence=evidence,
            )
        if not failures and after_value >= before_value:
            failures.append("session_duration did not reset after termination")
        criterion = "Session duration resets after the old App process exits and a new process starts."
    actual = {
        "before_ms": before_value,
        "after_ms": after_value,
        "before_pid": before.get("pid"),
        "after_pid": after.get("pid"),
    }
    if relation != "increase":
        actual["immediate_pid_exit_observed"] = bool(document.get("terminated_pid_confirmed"))
    return _verdict(
        key, title, criterion,
        {"relation": ">" if relation == "increase" else "<", "process_requirement": "same PID" if relation == "increase" else "new PID"},
        actual,
        evidence, failures,
    )


def validate_session_duration_continuous(folder):
    return _session_pair_verdict(folder, "session-duration-continuous", "Session Duration — Continuous App Session", 0, 1, "increase", "02-continuous.png")


def validate_session_duration_background(folder):
    return _session_pair_verdict(folder, "session-duration-background", "Session Duration — Resume from Background", 1, 2, "increase", "03-after-background.png")


def validate_session_duration_termination(folder):
    return _session_pair_verdict(folder, "session-duration-termination", "Session Duration — Reset after Termination", 2, 3, "reset", "04-after-termination.png")


def validate_app_initialization_time(folder):
    key = "app-initialization-time"
    title = "App Initialization Time"
    document = _session_sequence(folder)
    steps = document.get("steps", [])
    failures = []
    values = [step.get("app_init_time") for step in steps]
    if len(steps) != 4:
        failures.append(f"R3 must contain exactly four requests, got {len(steps)}")
    if len(values) != 4 or any(type(value) is not int or value <= 0 for value in values):
        failures.append("all app_init_time values must be positive Unix epoch milliseconds")
    elif len(set(values[:3])) != 1:
        failures.append("app_init_time changed while the same App process remained alive")
    elif values[3] <= values[2]:
        failures.append("app_init_time was not renewed after process restart")
    if len(steps) == 4:
        if len({step.get("pid") for step in steps[:3]}) != 1:
            failures.append("Requests 1–3 do not share one App PID")
        if steps[3].get("pid") == steps[2].get("pid"):
            failures.append("Request 4 does not use a new App PID")
    return _verdict(
        key, title, "Argus initialization time is stable per process and renewed at process restart.",
        {"requests_1_to_3": "same timestamp and PID", "request_4": "newer timestamp and new PID"},
        {
            "stable_app_init_time": values[0] if values else None,
            "restarted_app_init_time": values[3] if len(values) == 4 else None,
            "values": values,
        },
        "04-after-termination.png", failures,
    )


def validate_app_duration_today(folder):
    key = "app-duration-today"
    title = "Total App Usage Time Today"
    document = _session_sequence(folder)
    steps = document.get("steps", [])
    values = [step.get("app_duration") for step in steps]
    failures = []
    if len(steps) != 4:
        failures.append(f"R3 must contain exactly four requests, got {len(steps)}")
    if len(values) != 4 or any(type(value) is not int or value < 0 for value in values):
        failures.append("all app_duration values must be non-negative integer milliseconds")
    elif any(before > after for before, after in zip(values, values[1:])):
        failures.append("app_duration decreased within the same calendar-day R3 sequence")
    if len(steps) == 4:
        if steps[3].get("pid") == steps[2].get("pid"):
            failures.append("Request 4 does not use a new App PID")
    return _verdict(
        key, title, "Today's foreground usage remains monotonic across background and process restart.",
        {"unit": "milliseconds", "requests_1_to_4": "monotonic non-decreasing", "restart_behavior": "must persist"},
        {
            "before_restart_ms": values[2] if len(values) == 4 else None,
            "after_restart_ms": values[3] if len(values) == 4 else None,
            "values": values,
        },
        "04-after-termination.png", failures,
    )


def _validate_epoch_history(folder, key, title, field, allow_empty):
    value = _decoded_user_value(_decoded(folder), field)
    failures = []
    if not isinstance(value, list):
        failures.append(f"ext.user.{field} must be an array")
    else:
        if not allow_empty and not value:
            failures.append(f"ext.user.{field} must contain the current lifecycle timestamp")
        if any(type(item) is not int or item <= 0 for item in value):
            failures.append(f"ext.user.{field} must contain positive Unix epoch milliseconds")
        if any(left >= right for left, right in zip(value, value[1:])):
            failures.append(f"ext.user.{field} must be strictly increasing")
    expected = {
        "type": "array of strictly increasing Unix epoch milliseconds",
        "empty_allowed": allow_empty,
        "cross_platform_contract": True,
    }
    actual = {"timestamp_count": len(value) if isinstance(value, list) else None, "timestamps": value}
    return _verdict(key, title, f"{title} follows the shared Android/iOS lifecycle-history contract.", expected, actual, "bid_decoded.json", failures)


def validate_last_foreground_times(folder):
    return _validate_epoch_history(folder, "last-foreground-times", "Last Foreground Times", "last_foreground_time", False)


def validate_last_background_times(folder):
    return _validate_epoch_history(folder, "last-background-times", "Last Background Times", "last_background_time", True)


def validate_impression_history(folder):
    key = "impression-history"
    title = "Impression History"
    value = _decoded_user_value(_decoded(folder), "impression_history")
    failures = []
    previous = Path(folder) / "previous-impression.json"
    if not previous.is_file():
        failures.append("first-ad impression evidence is missing")
    if not isinstance(value, list):
        failures.append("ext.user.impression_history must be an array")
    elif not value:
        failures.append("second-ad capture must contain the previous impression history")
    return _verdict(
        key, title, "The second ad request must carry history after the first ad impression.",
        {"capture": "second ad request", "previous_impression_confirmed": True, "history_non_empty": True},
        {"previous_impression_confirmed": previous.is_file(), "impression_history": value}, "previous-impression.json", failures,
    )


def validate_network_latency(folder):
    folder = Path(folder)
    device_ext = _decoded_device_value(_decoded(folder), "ext", "ext")
    value = device_ext.get("latency") if isinstance(device_ext, dict) else None
    events = []
    event_paths = [folder / "proxy-events.jsonl"]
    summary_path = folder / "summary.json"
    if summary_path.is_file():
        try:
            run_id = json.loads(summary_path.read_text()).get("test_run_id")
        except json.JSONDecodeError:
            run_id = None
        if run_id:
            # The SDK probe runs asynchronously and may finish in an earlier
            # Round. Capture retries deliberately clear their local proxy
            # window, while the measured value remains in later bid requests.
            # Reuse only evidence carrying the exact same automation run id.
            evidence_root = folder.parent.parent
            for sibling_summary in evidence_root.glob("AOS_*/**/summary.json"):
                try:
                    sibling = json.loads(sibling_summary.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                if sibling.get("test_run_id") == run_id:
                    candidate = sibling_summary.parent / "proxy-events.jsonl"
                    if candidate not in event_paths:
                        event_paths.append(candidate)
    for path in event_paths:
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("method") == "HEAD" and event.get("url") == "https://cr.adsappier.com/4QGDNtuHG/icon/Info.svg":
                events.append({**event, "evidence_file": str(path.relative_to(folder.parent.parent))})
    successful_probe = next(
        (event for event in events if event.get("phase") == "response" and event.get("status") == 200),
        None,
    )
    probe_ok = successful_probe is not None
    failures = []
    if type(value) is not int or value <= 0:
        failures.append("ext.device.ext.latency must be a positive integer in milliseconds")
    if not probe_ok:
        failures.append("the SDK latency HEAD endpoint must return HTTP 200 in the same automation run")
    return _verdict(
        "network-latency", "Connection Latency",
        "The SDK HEAD latency probe must succeed during the same automation run and ext.device.ext.latency must contain its positive millisecond result.",
        {"endpoint": "https://cr.adsappier.com/4QGDNtuHG/icon/Info.svg", "method": "HEAD", "http_status": 200, "positive_ms": True, "scope": "same automation run"},
        {"latency_ms": value, "probe_response": successful_probe},
        "proxy-events.jsonl", failures,
    )


def validate_force_gdpr_override(_folder):
    return _round_blocked("force-gdpr-override", "Force GDPR Override", "Sample App limitation: setForceGDPRApplies(true) is not exposed or invoked")


def validate_coppa_applies(folder):
    value = _decoded_compliance_value(_decoded(folder), "coppa_applies")
    failures = [] if type(value) is int and value == 1 else ["req.compliance.coppa_applies must be integer 1"]
    return _verdict(
        "coppa-applies", "COPPA Applicability Flag",
        "Sample App calls setCoppaApplies(true), so the request flag must equal integer 1.",
        {"coppa_applies": 1}, {"coppa_applies": value}, "bid_decoded.json", failures,
    )


def validate_vpn_status(_folder):
    return _round_blocked("vpn-status", "VPN Status", "Not In Scope: this Android payload has no reviewed device.ext.vpn field")


def validate_argus_sdk_version(folder):
    actual = _decoded_device_value(_decoded(folder), "ext", "argus_ver")
    row = blocked("argus-sdk-version", "Waiting for a reviewer to enter the expected Argus SDK version in the report").to_dict()
    row.update({
        "layer": "Signal",
        "title": "Argus SDK Version",
        "description": "The Argus version was captured; comparison waits for an independently entered expected version.",
        "actual": {"argus_ver": actual},
        "evidence": "bid_decoded.json",
        "comparison_view": {
            "kind": "manual-expected",
            "criterion": "Enter the intended Argus SDK version; an exact match passes and a mismatch fails.",
            "actual": {"label": "Decoded Bid Request", "value": actual},
        },
    })
    return row


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
        "Advertising Tracking Allowed",
        "An active Advertising ID means tracking is allowed; device.lat is the inverse limit-tracking flag.",
        (ADS_SETTINGS, BID),
        validate_tracking_allowed,
    ),
    "advertising-id-opt-out": TestCase("advertising-id-opt-out", "Advertising ID — Tracking Denied", "Opt out prevents a usable advertising ID.", (ADS_TRACKING_DENIED, BID), validate_advertising_id_opt_out),
    "tracking-denied": TestCase("tracking-denied", "Advertising Tracking Denied", "Opt out ON produces LAT=1.", (ADS_TRACKING_DENIED, BID), validate_tracking_denied),
    "dark-mode-enabled": TestCase("dark-mode-enabled", "Dark Mode — Enabled", "Alternate dark-mode state follows Android.", (DISPLAY_STATUS, BID), validate_dark_mode_enabled),
    "font-scale-maximum": TestCase("font-scale-maximum", "Font Scale — Maximum", "Alternate font scale follows Android.", (DISPLAY_STATUS, BID), validate_font_scale_maximum),
    "screen-brightness-minimum": TestCase("screen-brightness-minimum", "Screen Brightness — Minimum", "Minimum brightness follows Android.", (DISPLAY_STATUS, BID), validate_screen_brightness_minimum),
    "output-volume-muted": TestCase("output-volume-muted", "Output Volume — Muted", "Muted media volume produces zero.", (VOLUME_STATUS, BID), validate_output_volume_muted),
    "screen-brightness-maximum": TestCase("screen-brightness-maximum", "Screen Brightness — Maximum", "Maximum brightness follows Android.", (DISPLAY_STATUS, BID), validate_screen_brightness_maximum),
    "output-volume-maximum": TestCase("output-volume-maximum", "Output Volume — Maximum", "Maximum media volume normalizes to one.", (VOLUME_STATUS, BID), validate_output_volume_maximum),
    "timezone-changed": TestCase("timezone-changed", "Timezone — Changed", "Alternate timezone updates UTC offset.", (TIMEZONE_STATUS, BID), validate_timezone_changed),
    "location-permission-denied": TestCase("location-permission-denied", "Location Permission — Denied", "Denied location permission suppresses coordinates.", (LOCATION_PERMISSION_STATUS, BID), validate_location_permission_denied),
    "battery-saver-enabled": TestCase("battery-saver-enabled", "Battery Saver — Enabled", "Battery Saver ON follows Android.", (BATTERY_STATUS, BID), validate_battery_saver_enabled),
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
    "default-language-iso": TestCase("default-language-iso", "System Language Code", "ISO-639-1 language component matches the primary Android system language.", (DEVICE_CONTEXT, BID), validate_default_language_iso),
    "default-language-bcp47": TestCase("default-language-bcp47", "System Language and Region Tag", "Complete BCP 47 tag matches the primary Android system language and region.", (DEVICE_CONTEXT, BID), validate_default_language_bcp47),
    "keyboard-languages": TestCase("keyboard-languages", "Installed Keyboard Languages", "Enabled keyboard languages match Android.", (DEVICE_CONTEXT, BID), validate_keyboard_languages),
    "root-status": TestCase("root-status", "Root Status", "Root detection matches Android.", (DEVICE_CONTEXT, BID), validate_root_status),
    "emulator-detection": TestCase("emulator-detection", "Emulator Detection", "Emulator detection matches Android.", (DEVICE_CONTEXT, BID), validate_emulator_detection),
    "connection-type": TestCase("connection-type", "Connection Type", "Connection transport matches Android.", (DEVICE_CONTEXT, BID), validate_connection_type),
    "connection-type-cellular": TestCase("connection-type-cellular", "Connection Type (Cellular)", "Cellular transport requires an active SIM.", (BID,), validate_connection_type_cellular),
    "carrier": TestCase("carrier", "Carrier", "Carrier reflects SIM state.", (DEVICE_CONTEXT, BID), validate_carrier),
    "mcc-mnc": TestCase("mcc-mnc", "MCC/MNC", "MCC/MNC reflects SIM state.", (DEVICE_CONTEXT, BID), validate_mcc_mnc),
    "precise-gps-latitude": TestCase("precise-gps-latitude", "Precise GPS Latitude", "Latitude matches Android fused location within accuracy tolerance.", (DEVICE_CONTEXT, BID), validate_precise_latitude),
    "precise-gps-longitude": TestCase("precise-gps-longitude", "Precise GPS Longitude", "Longitude matches Android fused location within accuracy tolerance.", (DEVICE_CONTEXT, BID), validate_precise_longitude),
    "session-duration-continuous": TestCase("session-duration-continuous", "Session Duration — Continuous App Session", "Session duration increases without leaving the App.", (BID,), validate_session_duration_continuous),
    "session-duration-background": TestCase("session-duration-background", "Session Duration — Resume from Background", "Session duration increases after background and resume.", (BID,), validate_session_duration_background),
    "session-duration-termination": TestCase("session-duration-termination", "Session Duration — Reset after Termination", "Session duration resets after process termination.", (BID,), validate_session_duration_termination),
    "app-initialization-time": TestCase("app-initialization-time", "App Initialization Time", "Argus initialization timestamp is stable per process and renewed after restart.", (BID,), validate_app_initialization_time),
    "app-duration-today": TestCase("app-duration-today", "Total App Usage Time Today", "Today's foreground usage persists across process restart.", (BID,), validate_app_duration_today),
    "last-foreground-times": TestCase("last-foreground-times", "Last Foreground Times", "Foreground history follows the shared Android/iOS contract.", (BID,), validate_last_foreground_times),
    "last-background-times": TestCase("last-background-times", "Last Background Times", "Background history follows the shared Android/iOS contract.", (BID,), validate_last_background_times),
    "impression-history": TestCase("impression-history", "Impression History", "The second ad request carries the first impression history.", (BID,), validate_impression_history),
    "network-latency": TestCase("network-latency", "Connection Latency", "The SDK latency probe and its positive millisecond result are required in R2 after the App has had time to initialize.", (BID,), validate_network_latency),
    "force-gdpr-override": TestCase("force-gdpr-override", "Force GDPR Override", "Force GDPR requires a Sample App trigger.", (BID,), validate_force_gdpr_override),
    "coppa-applies": TestCase("coppa-applies", "COPPA Applicability Flag", "COPPA flag reflects the Sample App setter.", (BID,), validate_coppa_applies),
    "vpn-status": TestCase("vpn-status", "VPN Status", "VPN is outside this round scope.", (BID,), validate_vpn_status),
    "argus-sdk-version": TestCase("argus-sdk-version", "Argus SDK Version", "Captured Argus version waits for a reviewer-supplied expected version.", (BID,), validate_argus_sdk_version),
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
            "connection-type",
            "connection-type-cellular",
            "carrier",
            "mcc-mnc",
            "precise-gps-latitude",
            "precise-gps-longitude",
            "last-foreground-times",
            "last-background-times",
            "vpn-status",
            "force-gdpr-override",
            "coppa-applies",
            "argus-sdk-version",
            "tracking-allowed",
            "sdk-version",
        ),
    ),
    "R2": Round(
        "SECOND-AD-HISTORY",
        ("impression-history", "network-latency"),
        warmup_ads=1,
    ),
    "R3": Round(
        "SESSION-DURATION",
        ("session-duration-continuous", "session-duration-background", "session-duration-termination", "app-initialization-time", "app-duration-today"),
        strategy="session-duration",
    ),
    "R5": Round(
        "ALTERNATE-STATE",
        ("advertising-id-opt-out", "tracking-denied", "dark-mode-enabled", "font-scale-maximum", "screen-brightness-minimum", "output-volume-muted", "battery-saver-enabled", "screen-brightness-maximum", "output-volume-maximum", "timezone-changed", "location-permission-denied"),
        strategy="r5-scenarios",
    ),
}

R5_PRIVACY_KEYS = ("advertising-id-opt-out", "tracking-denied")
R5_DARK_MODE_KEYS = ("dark-mode-enabled",)
R5_FONT_SCALE_KEYS = ("font-scale-maximum",)
R5_BRIGHTNESS_MINIMUM_KEYS = ("screen-brightness-minimum",)
R5_VOLUME_MUTED_KEYS = ("output-volume-muted",)
R5_BATTERY_SAVER_KEYS = ("battery-saver-enabled",)
R5_BRIGHTNESS_MAXIMUM_KEYS = ("screen-brightness-maximum",)
R5_VOLUME_MAXIMUM_KEYS = ("output-volume-maximum",)
R5_TIMEZONE_KEYS = ("timezone-changed",)
R5_LOCATION_DENIED_KEYS = ("location-permission-denied",)
R5_DISPLAY_HIGH_KEYS = R5_DARK_MODE_KEYS + R5_FONT_SCALE_KEYS + R5_BRIGHTNESS_MAXIMUM_KEYS + R5_VOLUME_MAXIMUM_KEYS
R5_DISPLAY_LOW_KEYS = R5_BRIGHTNESS_MINIMUM_KEYS + R5_VOLUME_MUTED_KEYS
R5_SYSTEM_ALT_KEYS = R5_BATTERY_SAVER_KEYS + R5_TIMEZONE_KEYS + R5_LOCATION_DENIED_KEYS
