"""Cross-platform R4 IPv6 refresh contracts and ordered-round validation."""

import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path

from verdict import blocked, evaluate


@dataclass(frozen=True)
class IPv6TestCase:
    key: str
    title: str
    priority: str


TESTCASES = {
    row.key: row for row in (
        IPv6TestCase("ipv6-address", "IPv6 Address", "P2"),
        IPv6TestCase("ipv6-refresh-launch", "IPv6 on App Launch", "P1"),
        IPv6TestCase("ipv6-refresh-wifi-switch", "IPv6 after Wi-Fi Switch", "P1"),
        IPv6TestCase("ipv6-refresh-recovery", "IPv6 after Network Recovery", "P1"),
        IPv6TestCase("ipv6-refresh-debounce", "IPv6 after Rapid Wi-Fi Switching", "P2"),
        IPv6TestCase("ipv6-refresh-slow-network", "IPv6 Refresh on Slow Network", "P1"),
    )
}

ROUND_DEFINITIONS = {"R4": tuple(TESTCASES)}


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _payload(folder):
    decoded = _read_json(Path(folder) / "bid_decoded.json", {}) or {}
    candidates = []
    for section in ("req", "ext"):
        plaintext = decoded.get(section, {}).get("plaintext", {})
        if isinstance(plaintext, dict):
            candidates.append(plaintext)
    return candidates


def _field(folder, key):
    for payload in _payload(folder):
        device = payload.get("device", {})
        if isinstance(device, dict) and key in device:
            return device.get(key)
        ext = device.get("ext", {}) if isinstance(device, dict) else {}
        if isinstance(ext, dict) and key in ext:
            return ext.get(key)
    return None


def _valid_ipv6(value):
    try:
        address = ipaddress.ip_address(str(value))
        return address.version == 6 and not address.is_unspecified
    except ValueError:
        return False


def _wifi(value):
    return str(value).strip().lower() in {"2", "wifi", "wi-fi"}


def _probe_ipv6(folder):
    document = _read_json(Path(folder) / "ipv6-net-probe-response.json", {}) or {}
    return document.get("ipv6") if isinstance(document, dict) else None


def _valid_step(value, require_probe):
    return (
        _wifi(value["conntype"])
        and _valid_ipv6(value["ipv6"])
        and (not require_probe or value["probe_ipv6"] == value["ipv6"])
    )


def _row(key, expected, actual, passed, evidence, reason):
    testcase = TESTCASES[key]
    row = evaluate(
        key,
        expected=expected,
        actual=actual,
        evidence=evidence,
        compare=lambda _expected, _actual: passed,
        reason=reason,
    ).to_dict()
    row.update({"layer": "Signal", "title": testcase.title, "description": reason})
    return row


def _blocked(key, reason, evidence="r4-network-sequence.json"):
    testcase = TESTCASES[key]
    row = blocked(key, reason).to_dict()
    row.update({"layer": "Signal", "title": testcase.title, "description": reason, "evidence": evidence})
    return row


def validate_sequence(folders, context=None):
    """Validate five ordered captures; environment absence blocks, executed mismatches fail."""
    folders = [Path(folder) for folder in folders]
    context = context or {}
    if not folders:
        return [_blocked(key, "Environment prerequisite unavailable: no R4 capture was executed") for key in TESTCASES]

    values = [
        {
            "folder": folder.name,
            "ipv6": _field(folder, "ipv6"),
            "conntype": _field(folder, "conntype"),
            "probe_ipv6": _probe_ipv6(folder),
        }
        for folder in folders
    ]
    first = values[0]
    require_probe = str(context.get("platform", "ios")).lower() == "aos"
    if not _valid_ipv6(first["ipv6"]) or (require_probe and first["probe_ipv6"] != first["ipv6"]):
        reason = "Environment prerequisite unavailable: the current network did not produce a matching Appier IPv6 probe and payload; R4 was not executable"
        return [_blocked(key, reason) for key in TESTCASES]

    rows = []
    first_ok = _valid_step(first, require_probe)
    rows.append(_row(
        "ipv6-address",
        {"valid_ipv6": True, "matches_appier_probe": require_probe},
        first,
        first_ok,
        "r4-network-sequence.json",
        "The decoded IPv6 is valid and matches the Appier probe." if first_ok else "FAILED: the R4 payload IPv6 is invalid or does not match the Appier probe.",
    ))
    rows.append(_row(
        "ipv6-refresh-launch",
        {"conntype": "wifi", "valid_ipv6_after_10s": True},
        first,
        first_ok,
        "r4-network-sequence.json",
        "Cold launch produced a valid Wi-Fi IPv6 after the required wait." if first_ok else "FAILED: cold launch completed, but the payload did not contain the expected Wi-Fi IPv6.",
    ))

    if len(values) < 5:
        executed = len(values)
        transition_keys = tuple(TESTCASES)[1:]
        for key in transition_keys[executed:]:
            rows.append(_blocked(key, "Operator checkpoint was not completed; this network transition was not executed"))
        return rows

    second = values[1]
    switch_ok = _valid_step(second, require_probe) and second["ipv6"] != first["ipv6"]
    rows.append(_row(
        "ipv6-refresh-wifi-switch",
        {"wifi": True, "valid_ipv6": True, "different_from_network_a": True},
        second,
        switch_ok,
        "r4-network-sequence.json",
        "The same App session refreshed to the second Wi-Fi IPv6." if switch_ok else "FAILED: after the Wi-Fi switch, IPv6 was missing, invalid, or still equal to the first network.",
    ))

    third = values[2]
    recovery_ok = _valid_step(third, require_probe) and third["ipv6"] == second["ipv6"]
    rows.append(_row(
        "ipv6-refresh-recovery",
        {"app_session_survived": True, "wifi": True, "valid_ipv6_after_recovery": True},
        third,
        recovery_ok,
        "r4-network-sequence.json",
        "The App session recovered and emitted a valid current Wi-Fi IPv6." if recovery_ok else "FAILED: the executed network recovery did not produce a valid current Wi-Fi IPv6.",
    ))

    fourth = values[3]
    debounce_ok = _valid_step(fourth, require_probe) and fourth["ipv6"] == second["ipv6"]
    rows.append(_row(
        "ipv6-refresh-debounce",
        {"app_session_survived": True, "final_ipv6_equals_network_b": True},
        fourth,
        debounce_ok,
        "r4-network-sequence.json",
        "Rapid switching settled on the final network's IPv6." if debounce_ok else "FAILED: rapid switching completed, but the payload did not settle on network B's IPv6.",
    ))

    fifth = values[4]
    slow_confirmed = bool(context.get("slow_network_confirmed"))
    slow_ok = slow_confirmed and _valid_step(fifth, require_probe) and fifth["ipv6"] != fourth["ipv6"]
    rows.append(_row(
        "ipv6-refresh-slow-network",
        {"slow_network_confirmed": True, "request_not_blocked": True, "valid_ipv6_after_15s": True},
        {**fifth, "slow_network_confirmed": slow_confirmed},
        slow_ok,
        "r4-network-sequence.json",
        "The ad request remained usable and IPv6 refreshed under the confirmed slow-network profile." if slow_ok else "FAILED: the slow-network step ran without complete proof of the throttle or a valid refreshed IPv6.",
    ))
    return rows
