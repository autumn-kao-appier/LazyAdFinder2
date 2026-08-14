"""iOS-only Evidence provider registry and capture orchestration."""

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from testcases.ios_signal_testcases import (
    BID, IOS_DEVICE_CONTEXT, IOS_LIFECYCLE_SEQUENCE, IOS_QA_EVIDENCE,
    IOS_SETTINGS_STATE,
)


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


def materialize_ios_settings_state(folder):
    folder = Path(folder)
    source = Path(os.environ.get("IOS_SETTINGS_STATE_FILE", "/tmp/laf2-ios-settings-state.json"))
    screenshot = Path(os.environ.get("IOS_SETTINGS_SCREENSHOT", "/tmp/laf2-ios-settings-state.png"))
    before_screenshot = Path(os.environ.get("IOS_SETTINGS_BEFORE_SCREENSHOT", "/tmp/laf2-ios-settings-before.png"))
    if source.is_file():
        shutil.copy2(source, folder / "ios-settings-state.json")
    else:
        (folder / "ios-settings-state.json").write_text(json.dumps({
            "status": "MISSING",
            "reason": "No independent Settings/ATT state was captured before the bid.",
        }, ensure_ascii=False, indent=2) + "\n")
    if screenshot.is_file() and screenshot.stat().st_size:
        shutil.copy2(screenshot, folder / "ios-settings-state.png")
    if before_screenshot.is_file() and before_screenshot.stat().st_size:
        shutil.copy2(before_screenshot, folder / "ios-settings-before.png")


def materialize_ios_qa_evidence(folder):
    """Preserve the optional, human-readable Sample App QA Evidence surface.

    Payload values are deliberately never synthesized into this document.  A
    missing Sample App surface remains explicit so validators cannot use the
    Bid Request as its own ground truth.
    """
    folder = Path(folder)
    source = Path(os.environ.get("IOS_QA_EVIDENCE_FILE", "/tmp/laf2-ios-qa-evidence.json"))
    screenshot = Path(os.environ.get("IOS_QA_EVIDENCE_SCREENSHOT", "/tmp/laf2-ios-qa-evidence.png"))
    if source.is_file():
        shutil.copy2(source, folder / "ios-qa-evidence.json")
    else:
        (folder / "ios-qa-evidence.json").write_text(json.dumps({
            "status": "UNAVAILABLE",
            "source": "Sample App QA Evidence page",
            "reason": (
                "The Sample App does not expose the requested independent value yet; "
                "the decoded Bid Request is not accepted as its own Evidence."
            ),
        }, ensure_ascii=False, indent=2) + "\n")
    if screenshot.is_file() and screenshot.stat().st_size:
        shutil.copy2(screenshot, folder / "ios-qa-evidence.png")


EVIDENCE_CAPTURES = {
    BID: EvidenceProvider(),
    IOS_DEVICE_CONTEXT: EvidenceProvider(after_bid=materialize_ios_device_context),
    IOS_SETTINGS_STATE: EvidenceProvider(after_bid=materialize_ios_settings_state),
    IOS_QA_EVIDENCE: EvidenceProvider(after_bid=materialize_ios_qa_evidence),
    # R3 writes the real multi-capture comparison after all lifecycle actions.
    IOS_LIFECYCLE_SEQUENCE: EvidenceProvider(),
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
