"""Android Evidence providers used by declared TestCases.

TestCases declare Evidence keys.  This module resolves and de-duplicates those
keys, performs device work before the shared bid capture, and materializes the
derived human-readable Evidence after capture.
"""

import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


ADS_SETTINGS = "ads-settings"
APP_SET_ID = "app-set-id"
BID = "bid"
SDK_BUILD_INFO = "sdk-build-info"
ADS_SETTINGS_ACTION = "com.google.android.gms.settings.ADS_PRIVACY"
SETUP_SCREENSHOT = Path("/tmp/laf2_ads_settings.png")
SETUP_STATE = Path("/tmp/laf2_ads_settings_state.json")
DEFAULT_EXPECTED_SDK_VERSION = "2.2.0"
VISIBLE_GAID_RE = re.compile(
    r"Your advertising ID:\s*([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
)


class EvidenceCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvidenceProvider:
    before_bid: object = None
    after_bid: object = None


def _adb(udid, *args, binary=False, check=True):
    command = ["adb", "-s", udid, *args]
    result = subprocess.run(command, capture_output=True, text=not binary)
    if check and result.returncode:
        stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
        raise EvidenceCaptureError(f"{' '.join(command)} failed: {stderr.strip()}")
    return result.stdout


def _bounds_center(value):
    values = [int(part) for part in re.findall(r"\d+", value or "")]
    if len(values) != 4:
        raise EvidenceCaptureError(f"invalid UI bounds: {value!r}")
    return (values[0] + values[2]) // 2, (values[1] + values[3]) // 2


def _visible_ads_state(udid):
    _adb(udid, "shell", "uiautomator", "dump", "/sdcard/laf2_ads_settings.xml")
    document = _adb(udid, "exec-out", "cat", "/sdcard/laf2_ads_settings.xml", binary=True)
    root = ET.fromstring(document)
    nodes = list(root.iter("node"))
    visible_text = "\n".join(node.attrib.get("text", "") for node in nodes)
    match = VISIBLE_GAID_RE.search(visible_text)
    gaid = match.group(1) if match else ""
    title = next(
        (node for node in nodes if node.attrib.get("text") == "Opt out of Ads Personalization"),
        None,
    )
    switches = [node for node in nodes if node.attrib.get("class") == "android.widget.Switch"]
    opt_out = None
    switch_center = None
    if title is not None and switches:
        _, title_y = _bounds_center(title.attrib.get("bounds"))
        selected = min(switches, key=lambda node: abs(_bounds_center(node.attrib.get("bounds"))[1] - title_y))
        opt_out = selected.attrib.get("checked") == "true"
        switch_center = _bounds_center(selected.attrib.get("bounds"))
    return gaid, opt_out, switch_center


def capture_ads_settings(config):
    """Open the human-readable Ads page, enforce tracking allowed, and photograph it."""
    for path in (SETUP_SCREENSHOT, SETUP_STATE):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    _adb(config.udid, "shell", "am", "start", "-a", ADS_SETTINGS_ACTION)
    time.sleep(2)
    gaid = ""
    opt_out = None
    switch_center = None
    for _ in range(5):
        gaid, opt_out, switch_center = _visible_ads_state(config.udid)
        if gaid and opt_out is not None:
            break
        _adb(config.udid, "shell", "input", "swipe", "540", "1900", "540", "500", "450")
        time.sleep(0.5)
    if not gaid:
        raise EvidenceCaptureError("Ads page did not visibly show 'Your advertising ID'")
    if opt_out is None or switch_center is None:
        raise EvidenceCaptureError("Cannot read the visible 'Opt out of Ads Personalization' switch")
    if opt_out:
        _adb(config.udid, "shell", "input", "tap", str(switch_center[0]), str(switch_center[1]))
        time.sleep(1)
        gaid, opt_out, _ = _visible_ads_state(config.udid)
        if opt_out:
            raise EvidenceCaptureError("Opt out of Ads Personalization remained enabled after tap")
    SETUP_SCREENSHOT.write_bytes(_adb(config.udid, "exec-out", "screencap", "-p", binary=True))
    SETUP_STATE.write_text(json.dumps({"gaid": gaid, "opt_out": opt_out}, indent=2) + "\n")


def materialize_ads_settings(folder):
    folder = Path(folder)
    screenshot = folder / "ads-settings.png"
    state = folder / "ads-settings-state.json"
    if SETUP_SCREENSHOT.exists() and SETUP_STATE.exists():
        shutil.copy2(SETUP_SCREENSHOT, screenshot)
        shutil.copy2(SETUP_STATE, state)
    if not screenshot.exists() or not state.exists():
        raise EvidenceCaptureError("visible Ads setting evidence is missing")


def _request_sdk_version(decoded):
    plaintext = decoded.get("req", {}).get("plaintext", {})
    app = plaintext.get("app") if isinstance(plaintext, dict) else None
    return app.get("sdk_version") if isinstance(app, dict) else None


def _expected_sdk_version(folder):
    configured = os.environ.get("EXPECTED_SDK_VERSION")
    if configured is not None:
        return configured.strip(), "EXPECTED_SDK_VERSION"
    existing = Path(folder) / "sdk-build-info.json"
    if existing.exists():
        document = json.loads(existing.read_text())
        value = document.get("expected", {}).get("build_sdk_version")
        if isinstance(value, str):
            return value, "saved sdk-build-info.json"
    return DEFAULT_EXPECTED_SDK_VERSION, "reviewed project default"


def capture_sdk_build_info(folder):
    folder = Path(folder)
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    expected, source = _expected_sdk_version(folder)
    (folder / "sdk-build-info.json").write_text(
        json.dumps(
            {
                "expected": {"build_sdk_version": expected, "source": source},
                "actual": {"req_app_sdk_version": _request_sdk_version(decoded)},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def capture_app_set_id_info(folder):
    """Materialize the decoded App Set ID as a small, human-readable artifact."""
    folder = Path(folder)
    decoded = json.loads((folder / "bid_decoded.json").read_text())

    plaintext = decoded.get("ext", {}).get("plaintext", {})
    device = plaintext.get("device") if isinstance(plaintext, dict) else None
    ext_value = device.get("ifv") if isinstance(device, dict) else None
    (folder / "app-set-id.json").write_text(
        json.dumps(
            {
                "source": "ext.plaintext.device.ifv",
                "actual": {"ext_device_ifv": ext_value},
                "note": (
                    "目前是單純抓包並解密 device.ifv。若需要可截圖的人眼 Evidence，"
                    "需請 RD 在 Sample App 增加顯示 App Set ID 的測試入口。"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


EVIDENCE_CAPTURES = {
    ADS_SETTINGS: EvidenceProvider(capture_ads_settings, materialize_ads_settings),
    APP_SET_ID: EvidenceProvider(after_bid=capture_app_set_id_info),
    BID: EvidenceProvider(),
    SDK_BUILD_INFO: EvidenceProvider(after_bid=capture_sdk_build_info),
}


def collect(config, required, capture_bid):
    """Collect each requested Evidence key once and return the shared bundle folder."""
    keys = tuple(dict.fromkeys(required))
    unknown = [key for key in keys if key not in EVIDENCE_CAPTURES]
    if unknown:
        raise EvidenceCaptureError(f"Unknown AOS Evidence keys: {', '.join(unknown)}")
    if BID not in keys:
        raise EvidenceCaptureError("Current AOS Evidence bundle requires the shared 'bid' capture")
    def before_bid(_capture_config=None):
        for key in keys:
            provider = EVIDENCE_CAPTURES[key]
            if provider.before_bid:
                provider.before_bid(config)

    folder = capture_bid(before_bid)
    try:
        for key in keys:
            provider = EVIDENCE_CAPTURES[key]
            if provider.after_bid:
                provider.after_bid(folder)
    except Exception as exc:
        error = EvidenceCaptureError(str(exc))
        error.evidence_folder = folder
        raise error from exc
    return folder
