"""Reviewed iOS Signal registry.

This module intentionally does not import the Android registry.  Stable
TestCase keys may be shared through the Catalog, while iOS Round setup,
Evidence requirements, and comparisons remain platform-owned.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from verdict import blocked, evaluate


BID = "bid"
IOS_DEVICE_CONTEXT = "ios-device-context"
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


def _decoded(folder):
    return json.loads((Path(folder) / "bid_decoded.json").read_text())


def _value(document, section, group, field):
    plaintext = document.get(section, {}).get("plaintext", {})
    container = plaintext.get(group, {}) if isinstance(plaintext, dict) else {}
    return container.get(field) if isinstance(container, dict) else None


def _verdict(key, title, description, expected, actual, evidence, failures):
    row = evaluate(
        key, expected=expected, actual=actual, evidence=evidence,
        compare=lambda _expected, _actual: not failures,
        reason="; ".join(failures),
    ).to_dict()
    row.update({"layer": "Signal", "title": title, "description": description})
    return row


def _review_blocked(key, title, reason):
    row = blocked(key, reason).to_dict()
    row.update({
        "layer": "Signal", "title": title,
        "description": "iOS uses an independent platform comparison contract.",
    })
    return row


def validate_argus_sdk_version(_folder):
    return _review_blocked(
        "argus-sdk-version", "Argus SDK Version",
        "Waiting for a reviewer to enter the expected iOS Argus SDK version in the report",
    )


def _timestamp_array(folder, testcase_key, field, title):
    value = _value(_decoded(folder), "ext", "user", field)
    failures = []
    if not isinstance(value, list) or any(type(item) is not int or item <= 0 for item in value):
        failures.append(f"ext.user.{field} must be an array of positive epoch-millisecond integers")
    return _verdict(
        testcase_key, title,
        "The iOS lifecycle timestamp array must have the reviewed wire format.",
        {"type": "array", "items": "positive epoch milliseconds"},
        {field: value}, "bid_decoded.json", failures,
    )


def validate_last_foreground_times(folder):
    return _timestamp_array(folder, "last-foreground-times", "last_foreground_time", "Last Foreground Times")


def validate_last_background_times(folder):
    return _timestamp_array(folder, "last-background-times", "last_background_time", "Last Background Times")


def validate_impression_history(folder):
    value = _value(_decoded(folder), "ext", "user", "impression_history")
    failures = [] if isinstance(value, list) and value else [
        "ext.user.impression_history must be non-empty after a proven first impression"
    ]
    return _verdict(
        "impression-history", "Impression History",
        "The second iOS request must contain the first proven impression.",
        {"capture": "second request", "history_non_empty": True},
        {"impression_history": value}, "bid_decoded.json", failures,
    )


def _lifecycle_pending(key, title):
    return lambda _folder: _review_blocked(
        key, title,
        "iOS lifecycle sequence Evidence is not implemented yet; no comparison is claimed",
    )


TC_DEFINITIONS = {
    "argus-sdk-version": TestCase("argus-sdk-version", "Argus SDK Version", "Reviewer-supplied iOS build answer.", (BID,), validate_argus_sdk_version),
    "last-foreground-times": TestCase("last-foreground-times", "Last Foreground Times", "iOS foreground lifecycle timestamps.", (BID,), validate_last_foreground_times),
    "last-background-times": TestCase("last-background-times", "Last Background Times", "iOS background lifecycle timestamps.", (BID,), validate_last_background_times),
    "impression-history": TestCase("impression-history", "Impression History", "Second-request impression history.", (BID,), validate_impression_history),
    "session-duration-continuous": TestCase("session-duration-continuous", "Session Duration — Continuous", "iOS lifecycle sequence.", (BID, IOS_LIFECYCLE_SEQUENCE), _lifecycle_pending("session-duration-continuous", "Session Duration — Continuous")),
    "session-duration-background": TestCase("session-duration-background", "Session Duration — Background", "iOS lifecycle sequence.", (BID, IOS_LIFECYCLE_SEQUENCE), _lifecycle_pending("session-duration-background", "Session Duration — Background")),
    "session-duration-termination": TestCase("session-duration-termination", "Session Duration — Termination", "iOS lifecycle sequence.", (BID, IOS_LIFECYCLE_SEQUENCE), _lifecycle_pending("session-duration-termination", "Session Duration — Termination")),
    "app-initialization-time": TestCase("app-initialization-time", "App Initialization Time", "iOS process initialization sequence.", (BID, IOS_LIFECYCLE_SEQUENCE), _lifecycle_pending("app-initialization-time", "App Initialization Time")),
    "app-duration-today": TestCase("app-duration-today", "Total App Usage Time Today", "iOS daily foreground duration sequence.", (BID, IOS_LIFECYCLE_SEQUENCE), _lifecycle_pending("app-duration-today", "Total App Usage Time Today")),
}


ROUND_DEFINITIONS = {
    "R1": Round("BASELINE", ("argus-sdk-version", "last-foreground-times", "last-background-times")),
    "R2": Round("SECOND-AD-HISTORY", ("impression-history",), warmup_ads=1),
    "R3": Round("LIFECYCLE-SEQUENCE", (
        "session-duration-continuous", "session-duration-background",
        "session-duration-termination", "app-initialization-time", "app-duration-today",
    ), strategy="lifecycle-sequence"),
}
