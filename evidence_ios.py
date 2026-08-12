"""iOS-only Evidence provider registry and capture orchestration."""

import json
from dataclasses import dataclass
from pathlib import Path

from testcases.ios_signal_testcases import BID, IOS_DEVICE_CONTEXT, IOS_LIFECYCLE_SEQUENCE


@dataclass(frozen=True)
class EvidenceProvider:
    before_bid: object = None
    after_bid: object = None


def materialize_ios_device_context(folder):
    folder = Path(folder)
    summary = json.loads((folder / "summary.json").read_text())
    (folder / "ios-device-context.json").write_text(json.dumps({
        "source": "ideviceinfo / XCUITest capture metadata",
        "device": summary.get("device", {}),
    }, ensure_ascii=False, indent=2) + "\n")


def mark_lifecycle_sequence_pending(folder):
    folder = Path(folder)
    (folder / "ios-lifecycle-sequence.json").write_text(json.dumps({
        "status": "NOT_IMPLEMENTED",
        "reason": "The independent iOS foreground/background/termination sequence is not connected yet.",
    }, ensure_ascii=False, indent=2) + "\n")


EVIDENCE_CAPTURES = {
    BID: EvidenceProvider(),
    IOS_DEVICE_CONTEXT: EvidenceProvider(after_bid=materialize_ios_device_context),
    IOS_LIFECYCLE_SEQUENCE: EvidenceProvider(after_bid=mark_lifecycle_sequence_pending),
}


def collect(config, required, capture_bid):
    keys = tuple(dict.fromkeys(required))
    unknown = [key for key in keys if key not in EVIDENCE_CAPTURES]
    if unknown:
        raise RuntimeError(f"Unknown iOS Evidence keys: {', '.join(unknown)}")
    if BID not in keys:
        raise RuntimeError("Current iOS Evidence bundle requires the shared 'bid' capture")
    errors = {}

    def before_bid(_config=None):
        for key in keys:
            provider = EVIDENCE_CAPTURES[key]
            if provider.before_bid:
                try:
                    provider.before_bid(config)
                except Exception as exc:
                    errors[key] = {"phase": "before_bid", "error": str(exc)}

    folder = Path(capture_bid(before_bid))
    for key in keys:
        provider = EVIDENCE_CAPTURES[key]
        if provider.after_bid:
            try:
                provider.after_bid(folder)
            except Exception as exc:
                errors[key] = {"phase": "after_bid", "error": str(exc)}
    if errors:
        (folder / "evidence-errors.json").write_text(json.dumps({"providers": errors}, ensure_ascii=False, indent=2) + "\n")
    return folder

