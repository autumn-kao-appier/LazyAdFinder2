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
    row.update({"layer": "Signal", "title": title, "description": description})
    return row


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
            "tracking-allowed",
            "sdk-version",
        ),
    ),
}
