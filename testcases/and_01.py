"""AND-01 — visible Android GAID must match both decoded device.ia values."""

import json
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from verdict import evaluate


TC_ID = "AND-01"
TITLE = "Advertising ID (GAID)"
DESCRIPTION = "Visible Android advertising ID matches req/ext device.ia."
ADS_SETTINGS_ACTION = "com.google.android.gms.settings.ADS_PRIVACY"
SETUP_SCREENSHOT = Path("/tmp/laf2_and01_gaid_settings.png")
SETUP_STATE = Path("/tmp/laf2_and01_gaid_state.json")
UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
VISIBLE_GAID_RE = re.compile(
    r"Your advertising ID:\s*([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
)
ZERO_GAID = "00000000-0000-0000-0000-000000000000"


class SetupError(RuntimeError):
    pass


def _adb(udid, *args, binary=False, check=True):
    command = ["adb", "-s", udid, *args]
    result = subprocess.run(command, capture_output=True, text=not binary)
    if check and result.returncode:
        stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
        raise SetupError(f"{' '.join(command)} failed: {stderr.strip()}")
    return result.stdout


def _bounds_center(value):
    values = [int(part) for part in re.findall(r"\d+", value or "")]
    if len(values) != 4:
        raise SetupError(f"invalid UI bounds: {value!r}")
    return (values[0] + values[2]) // 2, (values[1] + values[3]) // 2


def _visible_state(udid):
    _adb(udid, "shell", "uiautomator", "dump", "/sdcard/laf2_and01.xml")
    document = _adb(udid, "exec-out", "cat", "/sdcard/laf2_and01.xml", binary=True)
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


def setup(config):
    """Open the human-readable Ads page, enforce opt-out off, and photograph GAID."""
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
        gaid, opt_out, switch_center = _visible_state(config.udid)
        if gaid and opt_out is not None:
            break
        _adb(config.udid, "shell", "input", "swipe", "540", "1900", "540", "500", "450")
        time.sleep(0.5)
    if not gaid:
        raise SetupError("Ads page did not visibly show 'Your advertising ID'")
    if opt_out is None or switch_center is None:
        raise SetupError("Cannot read the visible 'Opt out of Ads Personalization' switch")
    if opt_out:
        _adb(config.udid, "shell", "input", "tap", str(switch_center[0]), str(switch_center[1]))
        time.sleep(1)
        gaid, opt_out, _ = _visible_state(config.udid)
        if opt_out:
            raise SetupError("Opt out of Ads Personalization remained enabled after tap")

    SETUP_SCREENSHOT.write_bytes(_adb(config.udid, "exec-out", "screencap", "-p", binary=True))
    SETUP_STATE.write_text(json.dumps({"gaid": gaid, "opt_out": opt_out}, indent=2) + "\n")


def _decoded_value(document, section):
    value = document.get(section, {}).get("plaintext", {})
    return value.get("device", {}).get("ia") if isinstance(value, dict) else None


def validate(folder):
    """Create the reviewed Signal verdict and attach the visible settings screenshot."""
    folder = Path(folder)
    visible_evidence = folder / "gaid-settings.png"
    if not SETUP_SCREENSHOT.exists() or not SETUP_STATE.exists():
        raise SetupError("AND-01 visible GAID setup evidence is missing")
    shutil.copy2(SETUP_SCREENSHOT, visible_evidence)
    state = json.loads(SETUP_STATE.read_text())
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    actual = {
        "settings_gaid": state.get("gaid"),
        "opt_out": state.get("opt_out"),
        "req_device_ia": _decoded_value(decoded, "req"),
        "ext_device_ia": _decoded_value(decoded, "ext"),
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
    expected = {
        "opt_out": False,
        "format": "lowercase UUID 8-4-4-4-12",
        "non_zero": True,
        "settings_equals_req_equals_ext": True,
    }
    verdict = evaluate(
        TC_ID,
        expected=expected,
        actual=actual,
        evidence=visible_evidence.name,
        compare=lambda _expected, _actual: not failures,
        reason="; ".join(failures),
    )
    row = verdict.to_dict()
    row.update({"layer": "Signal", "title": TITLE, "description": DESCRIPTION})
    return row
