"""Reviewed Android Signal TestCases, validators, and Round registry."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from evidence_aos import ADS_SETTINGS, APP_SET_ID, BID, SDK_BUILD_INFO
from verdict import evaluate


UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
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
        ("advertising-id", "app-set-id", "tracking-allowed", "sdk-version"),
    ),
}
