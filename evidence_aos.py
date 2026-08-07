"""Android Evidence providers used by declared TestCases.

TestCases declare Evidence keys.  This module resolves and de-duplicates those
keys, performs device work before the shared bid capture, and materializes the
derived human-readable Evidence after capture.
"""

import html
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


ADS_SETTINGS = "ads-settings"
ADS_TRACKING_DENIED = "ads-tracking-denied"
APP_SET_ID = "app-set-id"
BID = "bid"
BOOT_TIMESTAMPS = "boot-timestamps"
BATTERY_STATUS = "battery-status"
DISPLAY_STATUS = "display-status"
DEVICE_CONTEXT = "device-context"
IN_APP_PURCHASE_HISTORY = "in-app-purchase-history"
INSTALLED_APP_LIST = "installed-app-list"
RESOURCE_STATUS = "resource-status"
VOLUME_STATUS = "volume-status"
TIMEZONE_STATUS = "timezone-status"
LOCATION_PERMISSION_STATUS = "location-permission-status"
SDK_BUILD_INFO = "sdk-build-info"
ADS_SETTINGS_ACTION = "com.google.android.gms.settings.ADS_PRIVACY"
SETTINGS_COMPONENT = "com.android.settings/.Settings"
INSTALLED_APPS_SETTINGS_ACTION = "android.settings.MANAGE_APPLICATIONS_SETTINGS"
SETUP_SCREENSHOT = Path("/tmp/laf2_ads_settings.png")
SETUP_TRACKING_SCREENSHOT = Path("/tmp/laf2_tracking_allowed.png")
SETUP_TRACKING_DENIED_SCREENSHOT = Path("/tmp/laf2_tracking_denied.png")
SETUP_TRACKING_DENIED_STATE = Path("/tmp/laf2_tracking_denied_state.json")
SETUP_STATE = Path("/tmp/laf2_ads_settings_state.json")
SETUP_INSTALLED_APPS_SCREENSHOT = Path("/tmp/laf2_installed_apps_settings.png")
SETUP_BOOT_TIME_REFERENCE = Path("/tmp/laf2_boot_time_reference.json")
SETUP_UPTIME_SCREENSHOT = Path("/tmp/laf2_uptime_settings.png")
SETUP_RESOURCE_STATUS = Path("/tmp/laf2_resource_status.json")
SETUP_MEMORY_SCREENSHOT = Path("/tmp/laf2_memory_settings.png")
SETUP_STORAGE_SCREENSHOT = Path("/tmp/laf2_storage_settings.png")
SETUP_BATTERY_SCREENSHOT = Path("/tmp/laf2_battery_settings.png")
SETUP_BATTERY_SAVER_SCREENSHOT = Path("/tmp/laf2_battery_saver_settings.png")
SETUP_DISPLAY_SCREENSHOT = Path("/tmp/laf2_display_settings.png")
SETUP_FONT_SCALE_SCREENSHOT = Path("/tmp/laf2_font_scale_settings.png")
SETUP_QUICK_BRIGHTNESS_SCREENSHOT = Path("/tmp/laf2_quick_brightness.png")
SETUP_BATTERY_STATUS = Path("/tmp/laf2_battery_status.json")
SETUP_DISPLAY_STATUS = Path("/tmp/laf2_display_status.json")
SETUP_DEVICE_CONTEXT = Path("/tmp/laf2_device_context.json")
SETUP_SOUND_SCREENSHOT = Path("/tmp/laf2_sound_settings.png")
SETUP_VOLUME_STATUS = Path("/tmp/laf2_volume_status.json")
SETUP_TIMEZONE_STATUS = Path("/tmp/laf2_timezone_status.json")
SETUP_LOCATION_PERMISSION_STATUS = Path("/tmp/laf2_location_permission_status.json")
SETUP_LOCATION_PERMISSION_SCREENSHOT = Path("/tmp/laf2_location_permission.png")
SETUP_ABOUT_SCREENSHOT = Path("/tmp/laf2_about_settings.png")
SETUP_DATETIME_SCREENSHOT = Path("/tmp/laf2_datetime_settings.png")
SETUP_LANGUAGE_SCREENSHOT = Path("/tmp/laf2_language_settings.png")
SETUP_KEYBOARD_SCREENSHOT = Path("/tmp/laf2_keyboard_languages.png")
SETUP_ROOT_SCREENSHOT = Path("/tmp/laf2_root_status.png")
SETUP_NETWORK_SCREENSHOT = Path("/tmp/laf2_network_settings.png")
OFFICIAL_DISPLAY_SPECS = {
    "Pixel 10a": {
        "physical_ppi": 422.2,
        "source": "Google Pixel phone hardware tech specs",
        "url": "https://support.google.com/pixelphone/answer/7158570?hl=en",
    },
}
VISIBLE_GAID_RE = re.compile(
    r"(?:Your|This device(?:'|’)?s) advertising ID:\s*"
    r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
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
    delete_visible = any(node.attrib.get("text") == "Delete advertising ID" for node in nodes)
    renew_visible = any(node.attrib.get("text") == "Renew advertising ID" for node in nodes)
    reset_visible = any(node.attrib.get("text") == "Reset advertising ID" for node in nodes)
    get_new_visible = any(node.attrib.get("text") == "Get new advertising ID" for node in nodes)
    if delete_visible:
        ui_model = "delete-renew"
        tracking_allowed = True
    elif renew_visible:
        ui_model = "delete-renew"
        tracking_allowed = False
    elif get_new_visible:
        ui_model = "delete-get-new"
        tracking_allowed = False
    elif opt_out is not None:
        ui_model = "legacy-opt-out"
        tracking_allowed = not opt_out
    else:
        ui_model = "unknown"
        tracking_allowed = None
    return {
        "gaid": gaid,
        "opt_out": opt_out,
        "switch_center": switch_center,
        "delete_visible": delete_visible,
        "renew_visible": renew_visible,
        "reset_visible": reset_visible,
        "get_new_visible": get_new_visible,
        "ui_model": ui_model,
        "tracking_allowed": tracking_allowed,
    }


def _open_ads_settings_via_search(udid):
    """Navigate via Settings search: Ads → Privacy controls → Ads."""
    _adb(udid, "shell", "am", "force-stop", "com.android.settings")
    _adb(udid, "shell", "am", "start", "-n", SETTINGS_COMPONENT)
    time.sleep(1.5)
    _adb(udid, "shell", "uiautomator", "dump", "/sdcard/laf2_settings_home.xml")
    home = ET.fromstring(_adb(udid, "exec-out", "cat", "/sdcard/laf2_settings_home.xml", binary=True))
    search = next((node for node in home.iter("node") if node.attrib.get("text") == "Search Settings"), None)
    if search is None:
        raise EvidenceCaptureError("Settings home does not expose Search Settings")
    x, y = _bounds_center(search.attrib.get("bounds"))
    _adb(udid, "shell", "input", "tap", str(x), str(y))
    time.sleep(0.5)
    _adb(udid, "shell", "uiautomator", "dump", "/sdcard/laf2_settings_search.xml")
    search_page = ET.fromstring(_adb(udid, "exec-out", "cat", "/sdcard/laf2_settings_search.xml", binary=True))
    edit = next((node for node in search_page.iter("node") if node.attrib.get("class") == "android.widget.EditText"), None)
    if edit is None:
        raise EvidenceCaptureError("Settings search input is unavailable")
    x, y = _bounds_center(edit.attrib.get("bounds"))
    _adb(udid, "shell", "input", "tap", str(x), str(y))
    _adb(udid, "shell", "input", "text", "Ads")
    breadcrumb = None
    results = None
    for _ in range(6):
        time.sleep(0.5)
        _adb(udid, "shell", "uiautomator", "dump", "/sdcard/laf2_settings_search_results.xml")
        results = ET.fromstring(_adb(udid, "exec-out", "cat", "/sdcard/laf2_settings_search_results.xml", binary=True))
        breadcrumb = next(
            (node for node in results.iter("node") if "Privacy controls" in node.attrib.get("text", "")),
            None,
        )
        if breadcrumb is not None:
            break
    if breadcrumb is None:
        raise EvidenceCaptureError("Settings search for Ads did not return the Privacy controls result")
    parents = {child: parent for parent in results.iter("node") for child in parent}
    result_row = breadcrumb
    while result_row is not None and result_row.attrib.get("clickable") != "true":
        result_row = parents.get(result_row)
    if result_row is None:
        raise EvidenceCaptureError("Privacy controls search result is not clickable")
    x, y = _bounds_center(result_row.attrib.get("bounds"))
    _adb(udid, "shell", "input", "tap", str(x), str(y))
    time.sleep(1.5)
    _adb(udid, "shell", "uiautomator", "dump", "/sdcard/laf2_privacy_controls.xml")
    controls = ET.fromstring(_adb(udid, "exec-out", "cat", "/sdcard/laf2_privacy_controls.xml", binary=True))
    ads = next((node for node in controls.iter("node") if node.attrib.get("text") == "Ads"), None)
    if ads is None:
        raise EvidenceCaptureError("Privacy controls does not expose the visible Ads preference")
    x, y = _bounds_center(ads.attrib.get("bounds"))
    _adb(udid, "shell", "input", "tap", str(x), str(y))
    time.sleep(1.5)
    _adb(udid, "shell", "uiautomator", "dump", "/sdcard/laf2_ads_settings.xml")
    opened = ET.fromstring(_adb(udid, "exec-out", "cat", "/sdcard/laf2_ads_settings.xml", binary=True))
    actions = {node.attrib.get("text") for node in opened.iter("node")}
    has_ads_control = bool(
        {"Delete advertising ID", "Renew advertising ID", "Reset advertising ID", "Get new advertising ID"} & actions
    ) or "Opt out of Ads Personalization" in actions
    if not has_ads_control:
        raise EvidenceCaptureError("Privacy controls → Ads did not open the Advertising ID page")


def _position_visible_opt_out(udid):
    """Return the Ads page to the Opt-out row and reject clipped screenshots."""
    for _ in range(7):
        _adb(udid, "shell", "input", "swipe", "540", "650", "540", "1900", "300")
        time.sleep(0.2)
    state = _visible_ads_state(udid)
    document = _adb(udid, "exec-out", "cat", "/sdcard/laf2_ads_settings.xml", binary=True)
    root = ET.fromstring(document)
    title = next(
        (node for node in root.iter("node") if node.attrib.get("text") == "Opt out of Ads Personalization"),
        None,
    )
    if title is None or switch_center is None:
        return state
    bounds = [int(part) for part in re.findall(r"\d+", title.attrib.get("bounds", ""))]
    if len(bounds) != 4 or bounds[1] < 250 or bounds[3] > 2250:
        return state
    return state


def _tap_ads_action(udid, label):
    """Tap a visible Ads action and confirm its resulting real device state."""
    _adb(udid, "shell", "uiautomator", "dump", "/sdcard/laf2_ads_action.xml")
    root = ET.fromstring(_adb(udid, "exec-out", "cat", "/sdcard/laf2_ads_action.xml", binary=True))
    matches = [node for node in root.iter("node") if node.attrib.get("text") == label]
    if not matches:
        raise EvidenceCaptureError(f"Advertising ID page does not expose {label!r}")
    x, y = _bounds_center(matches[0].attrib.get("bounds"))
    _adb(udid, "shell", "input", "tap", str(x), str(y))
    time.sleep(0.7)

    # Pixel shows a confirmation dialog with the same action label. Prefer a
    # Button so the underlying preference row is not tapped a second time.
    _adb(udid, "shell", "uiautomator", "dump", "/sdcard/laf2_ads_confirm.xml")
    confirm = ET.fromstring(_adb(udid, "exec-out", "cat", "/sdcard/laf2_ads_confirm.xml", binary=True))
    buttons = [
        node for node in confirm.iter("node")
        if node.attrib.get("text") in {label, "Confirm", "OK"}
        and node.attrib.get("class") == "android.widget.Button"
    ]
    if buttons:
        x, y = _bounds_center(buttons[-1].attrib.get("bounds"))
        _adb(udid, "shell", "input", "tap", str(x), str(y))
    time.sleep(1.2)

    for _ in range(5):
        state = _visible_ads_state(udid)
        if label == "Delete advertising ID" and state["tracking_allowed"] is False:
            return state
        if label in {"Renew advertising ID", "Reset advertising ID", "Get new advertising ID"} and (
            state["tracking_allowed"] is True
        ):
            return state
        time.sleep(0.5)
    raise EvidenceCaptureError(f"{label} did not produce the expected Advertising ID state")


def capture_ads_settings(config):
    """Open Ads, support both legacy Opt-out and modern Delete/Renew UIs."""
    for path in (SETUP_SCREENSHOT, SETUP_TRACKING_SCREENSHOT, SETUP_STATE):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    _adb(config.udid, "shell", "cmd", "statusbar", "collapse", check=False)
    _adb(config.udid, "shell", "input", "keyevent", "4", check=False)
    _open_ads_settings_via_search(config.udid)
    time.sleep(2)
    state = _position_visible_opt_out(config.udid)
    if state["get_new_visible"]:
        state = _tap_ads_action(config.udid, "Get new advertising ID")
    elif state["ui_model"] == "delete-renew" and state["renew_visible"]:
        state = _tap_ads_action(config.udid, "Renew advertising ID")
    elif state["ui_model"] == "legacy-opt-out" and state["opt_out"]:
        x, y = state["switch_center"]
        _adb(config.udid, "shell", "input", "tap", str(x), str(y))
        time.sleep(1)
        state = _position_visible_opt_out(config.udid)
        if not state["tracking_allowed"]:
            raise EvidenceCaptureError("Opt out of Ads Personalization remained enabled after tap")
    elif state["ui_model"] == "unknown":
        raise EvidenceCaptureError("Advertising ID page exposes neither Delete/Renew nor legacy Opt out controls")
    SETUP_TRACKING_SCREENSHOT.write_bytes(
        _adb(config.udid, "exec-out", "screencap", "-p", binary=True)
    )
    gaid = ""
    for _ in range(5):
        state = _visible_ads_state(config.udid)
        gaid = state["gaid"]
        if gaid and state["tracking_allowed"] is True:
            break
        _adb(config.udid, "shell", "input", "swipe", "540", "1900", "540", "500", "450")
        time.sleep(0.5)
    if not gaid:
        raise EvidenceCaptureError("Ads page did not visibly show the device advertising ID")
    SETUP_SCREENSHOT.write_bytes(_adb(config.udid, "exec-out", "screencap", "-p", binary=True))
    SETUP_STATE.write_text(json.dumps({
        "gaid": gaid,
        "opt_out": state["opt_out"],
        "tracking_allowed": state["tracking_allowed"],
        "ui_model": state["ui_model"],
        "visible_action": "Delete advertising ID" if state["delete_visible"] else None,
    }, indent=2) + "\n")


def materialize_ads_settings(folder):
    folder = Path(folder)
    screenshot = folder / "ads-settings.png"
    tracking_screenshot = folder / "tracking-allowed.png"
    state = folder / "ads-settings-state.json"
    if SETUP_SCREENSHOT.exists() and SETUP_STATE.exists():
        shutil.copy2(SETUP_SCREENSHOT, screenshot)
        shutil.copy2(SETUP_TRACKING_SCREENSHOT, tracking_screenshot)
        shutil.copy2(SETUP_STATE, state)
    if not screenshot.exists() or not tracking_screenshot.exists() or not state.exists():
        raise EvidenceCaptureError("visible Ads setting evidence is missing")


def capture_tracking_denied(config):
    """Deny tracking through Delete ID (modern UI) or Opt out (legacy UI)."""
    for path in (SETUP_TRACKING_DENIED_SCREENSHOT, SETUP_TRACKING_DENIED_STATE):
        path.unlink(missing_ok=True)
    _adb(config.udid, "shell", "cmd", "statusbar", "collapse", check=False)
    _adb(config.udid, "shell", "input", "keyevent", "4", check=False)
    _open_ads_settings_via_search(config.udid)
    time.sleep(2)
    state = _position_visible_opt_out(config.udid)
    if state["ui_model"] == "delete-renew" and state["delete_visible"]:
        state = _tap_ads_action(config.udid, "Delete advertising ID")
    elif state["ui_model"] == "legacy-opt-out" and not state["opt_out"]:
        x, y = state["switch_center"]
        _adb(config.udid, "shell", "input", "tap", str(x), str(y))
        time.sleep(0.5)
        _adb(config.udid, "shell", "uiautomator", "dump", "/sdcard/laf2_ads_confirm.xml")
        confirm = ET.fromstring(_adb(config.udid, "exec-out", "cat", "/sdcard/laf2_ads_confirm.xml", binary=True))
        ok = next((node for node in confirm.iter("node") if node.attrib.get("text") == "OK"), None)
        if ok is not None:
            x, y = _bounds_center(ok.attrib.get("bounds"))
            _adb(config.udid, "shell", "input", "tap", str(x), str(y))
        time.sleep(1)
        state = _position_visible_opt_out(config.udid)
    if state["tracking_allowed"] is not False:
        raise EvidenceCaptureError("Advertising tracking could not be visibly disabled")
    SETUP_TRACKING_DENIED_SCREENSHOT.write_bytes(
        _adb(config.udid, "exec-out", "screencap", "-p", binary=True)
    )
    SETUP_TRACKING_DENIED_STATE.write_text(
        json.dumps({
            "gaid": state["gaid"],
            "opt_out": state["opt_out"],
            "tracking_allowed": False,
            "ui_model": state["ui_model"],
            "visible_action": "Renew advertising ID" if state["renew_visible"] else None,
            "visual_contract": "advertising-id-disabled-visible-v3",
        }, indent=2) + "\n"
    )


def materialize_tracking_denied(folder):
    folder = Path(folder)
    shutil.copy2(SETUP_TRACKING_DENIED_SCREENSHOT, folder / "tracking-denied.png")
    shutil.copy2(SETUP_TRACKING_DENIED_SCREENSHOT, folder / "advertising-id-opt-out.png")
    shutil.copy2(SETUP_TRACKING_DENIED_STATE, folder / "tracking-denied-state.json")


def _request_sdk_version(decoded):
    plaintext = decoded.get("req", {}).get("plaintext", {})
    app = plaintext.get("app") if isinstance(plaintext, dict) else None
    return app.get("sdk_version") if isinstance(app, dict) else None


def capture_sdk_build_info(folder):
    folder = Path(folder)
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    (folder / "sdk-build-info.json").write_text(
        json.dumps(
            {
                "expected": {"build_sdk_version": None, "source": "manual report review"},
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


def capture_installed_app_list_info(folder):
    """Materialize applist availability, count, and values for human review."""
    folder = Path(folder)
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    plaintext = decoded.get("ext", {}).get("plaintext", {})
    device = plaintext.get("device") if isinstance(plaintext, dict) else None
    device_ext = device.get("ext") if isinstance(device, dict) else None
    present = isinstance(device_ext, dict) and "applist" in device_ext
    value = device_ext.get("applist") if present else None
    if not present:
        state = "UNAVAILABLE"
    elif isinstance(value, list) and not value:
        state = "EMPTY"
    elif isinstance(value, list):
        state = "CAPTURED"
    else:
        state = "INVALID"
    (folder / "installed-app-list.json").write_text(
        json.dumps(
            {
                "source": "ext.plaintext.device.ext.applist",
                "actual": {
                    "collection_status": state,
                    "package_count": len(value) if isinstance(value, list) else 0,
                    "packages": value,
                },
                "note": "清單來自抓包解密；不固定套件數量、內容或順序。",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def capture_installed_apps_settings(config):
    """Capture a human-readable, supplementary view of Android's app list."""
    try:
        SETUP_INSTALLED_APPS_SCREENSHOT.unlink()
    except FileNotFoundError:
        pass
    _adb(config.udid, "shell", "am", "start", "-a", INSTALLED_APPS_SETTINGS_ACTION)
    time.sleep(2)
    _adb(config.udid, "shell", "input", "swipe", "540", "1750", "540", "750", "450")
    time.sleep(1)
    SETUP_INSTALLED_APPS_SCREENSHOT.write_bytes(
        _adb(config.udid, "exec-out", "screencap", "-p", binary=True)
    )


def materialize_installed_apps_settings(folder):
    target = Path(folder) / "installed-apps-settings.png"
    if SETUP_INSTALLED_APPS_SCREENSHOT.exists():
        shutil.copy2(SETUP_INSTALLED_APPS_SCREENSHOT, target)
    if not target.exists():
        raise EvidenceCaptureError("Android installed-apps settings screenshot is missing")
    capture_installed_app_list_info(folder)


def capture_in_app_purchase_history_info(folder):
    folder = Path(folder)
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    plaintext = decoded.get("ext", {}).get("plaintext", {})
    device = plaintext.get("device") if isinstance(plaintext, dict) else None
    device_ext = device.get("ext") if isinstance(device, dict) else None
    present = isinstance(device_ext, dict) and "iaphistory" in device_ext
    value = device_ext.get("iaphistory") if present else None
    (folder / "in-app-purchase-history.json").write_text(
        json.dumps(
            {
                "source": "ext.plaintext.device.ext.iaphistory",
                "actual": {
                    "field_present": present,
                    "product_count": len(value) if isinstance(value, list) else 0,
                    "product_ids": value,
                },
                "note": "Sample App 沒有購買流程，因此空陣列是目前預期且合法的結果。",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def capture_boot_time_reference(config):
    """Photograph visible Uptime, then independently calculate the boot epoch."""
    SETUP_UPTIME_SCREENSHOT.unlink(missing_ok=True)
    component = r"com.android.settings/.Settings\$MyDeviceInfoActivity"
    result = _adb(config.udid, "shell", "am", "start", "-W", "-n", component, check=False)
    if "Error" in result or "Exception" in result:
        raise EvidenceCaptureError("Cannot open Android About phone for visible Uptime Evidence")
    time.sleep(1)
    for _ in range(5):
        _adb(config.udid, "shell", "input", "swipe", "540", "1900", "540", "400", "350")
        time.sleep(0.25)
    SETUP_UPTIME_SCREENSHOT.write_bytes(
        _adb(config.udid, "exec-out", "screencap", "-p", binary=True)
    )
    epoch_ms = int(_adb(config.udid, "shell", "date", "+%s%3N").strip())
    uptime_seconds = float(_adb(config.udid, "shell", "cat", "/proc/uptime").split()[0])
    device_current_time = _adb(
        config.udid, "shell", "date", "+%Y-%m-%dT%H:%M:%S%z"
    ).strip().replace("T", " ")
    uptime_started_at = _adb(config.udid, "shell", "uptime", "-s").strip()
    SETUP_BOOT_TIME_REFERENCE.write_text(
        json.dumps(
            {
                "captured_epoch_ms": epoch_ms,
                "uptime_ms": round(uptime_seconds * 1000),
                "uptime_seconds": uptime_seconds,
                "device_current_time": device_current_time,
                "uptime_started_at": uptime_started_at,
                "current_boot_time_ms": round(epoch_ms - uptime_seconds * 1000),
                "source": "device date - /proc/uptime",
                "visible_source": "Android Settings > About phone > Uptime",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def materialize_boot_timestamps(folder):
    folder = Path(folder)
    if not SETUP_BOOT_TIME_REFERENCE.exists():
        raise EvidenceCaptureError("independent boot-time reference is missing")
    reference = json.loads(SETUP_BOOT_TIME_REFERENCE.read_text())
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    plaintext = decoded.get("ext", {}).get("plaintext", {})
    device = plaintext.get("device") if isinstance(plaintext, dict) else None
    device_ext = device.get("ext") if isinstance(device, dict) else None
    value = device_ext.get("pot") if isinstance(device_ext, dict) else None
    reference["actual"] = {"pot": value}
    latest = value[-1] if isinstance(value, list) and value and type(value[-1]) is int else None
    calculated = reference.get("current_boot_time_ms")
    delta = abs(latest - calculated) if latest is not None and isinstance(calculated, int) else None
    reference["comparison"] = {
        "payload_latest_pot_ms": latest,
        "calculated_boot_time_ms": calculated,
        "absolute_difference_ms": delta,
        "tolerance_ms": 120_000,
    }
    (folder / "boot-timestamps.json").write_text(
        json.dumps(reference, ensure_ascii=False, indent=2) + "\n"
    )
    hours, remainder = divmod(int(reference.get("uptime_seconds", 0)), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    result = "PASS" if delta is not None and delta <= 120_000 else "FAILED"
    color = "#287a3d" if result == "PASS" else "#b9342b"
    if not SETUP_UPTIME_SCREENSHOT.exists():
        raise EvidenceCaptureError("visible Android Uptime screenshot is missing")
    uptime_image = base64.b64encode(SETUP_UPTIME_SCREENSHOT.read_bytes()).decode()
    calculation = folder / "boot-time-calculation.html"
    calculation.write_text(
        f'''<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{width:1400px;height:1000px;padding:42px 68px}}.eyebrow{{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}}h1{{font-size:38px;margin:7px 0 18px}}.source{{height:220px;overflow:hidden;border-radius:20px;border:1px solid #cbd4e8;background:#e7eaff;position:relative}}.source img{{position:absolute;width:1264px;height:auto;left:0;top:-2050px;filter:none}}.caption{{font-size:14px;color:#60717c;margin:7px 0 15px}}.formula{{background:#fff;border:1px solid #dbe2e8;border-radius:18px;padding:18px 28px;box-shadow:0 8px 24px #131a2112}}.row{{display:grid;grid-template-columns:245px 45px 1fr;align-items:center;padding:9px 0;border-bottom:1px solid #e4e9ed}}.row:last-child{{border:0}}.label{{color:#60717c;font-size:17px}}.op{{font:700 25px ui-monospace,monospace;color:#0e7c86}}.value{{font:700 20px ui-monospace,monospace}}.comparison{{display:flex;justify-content:space-between;align-items:center;margin-top:16px;padding:14px 25px;background:#fff;border-radius:14px;border-left:8px solid {color}}}.comparison b{{font-size:25px;color:{color}}}.comparison span{{font:700 17px ui-monospace,monospace}}footer{{margin-top:12px;color:#6c7b85;font-size:14px}}</style></head><body><main>
<div class="eyebrow">PRIMARY SOURCE · ANDROID SETTINGS</div><h1>About phone → Uptime</h1><div class="source"><img src="data:image/png;base64,{uptime_image}"></div><div class="caption">Privacy-safe crop: only the visible Uptime row is retained; device identifiers above it are excluded.</div><section class="formula">
<div class="row"><span class="label">Screenshot time</span><span class="op"></span><span class="value">{html.escape(str(reference.get("device_current_time", "—")))}</span></div>
<div class="row"><span class="label">Uptime sampled ≤1s later</span><span class="op">−</span><span class="value">{uptime_text}</span></div>
<div class="row"><span class="label">Calculated boot time</span><span class="op">=</span><span class="value">{html.escape(str(reference.get("uptime_started_at", "—")))}</span></div>
<div class="row"><span class="label">Calculated epoch</span><span class="op"></span><span class="value">{calculated if calculated is not None else "—"} ms</span></div>
<div class="row"><span class="label">SDK latest pot</span><span class="op">vs</span><span class="value">{latest if latest is not None else "—"} ms</span></div></section>
<div class="comparison"><div><span>Difference from visible-source calculation</span><br><b>{delta if delta is not None else "—"} ms</b></div><b>{result}</b></div>
<footer>Formula: device time − Android Uptime · acceptance tolerance ±120,000 ms</footer>
</main></body></html>''',
        encoding="utf-8",
    )
    chrome = os.environ.get(
        "CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    screenshot = folder / "boot-time-calculation.png"
    screenshot.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="laf2-chrome-") as profile:
        process = subprocess.Popen(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--disable-background-networking",
                "--hide-scrollbars",
                "--no-first-run",
                f"--user-data-dir={profile}",
                "--window-size=1400,1000",
                f"--screenshot={screenshot}",
                calculation.resolve().as_uri(),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if screenshot.exists() and screenshot.stat().st_size > 1000:
                break
            if process.poll() is not None:
                break
            time.sleep(0.1)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    if not screenshot.exists():
        raise EvidenceCaptureError("boot-time calculation screenshot was not created")


def _parse_meminfo(raw):
    values = {}
    for line in raw.splitlines():
        match = re.fullmatch(r"([A-Za-z_()]+):\s+(\d+)\s+kB", line.strip())
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    return values


def _parse_data_filesystem(raw):
    rows = [line.split() for line in raw.splitlines() if line.strip()]
    if len(rows) < 2 or len(rows[-1]) < 4:
        raise EvidenceCaptureError(f"Cannot parse df -k /data output: {raw!r}")
    try:
        return {
            "total_bytes": int(rows[-1][1]) * 1024,
            "free_bytes": int(rows[-1][3]) * 1024,
        }
    except ValueError as exc:
        raise EvidenceCaptureError(f"Invalid df -k /data values: {raw!r}") from exc


def _open_settings_screenshot(udid, component, target, expected_text, *, action=False):
    target.unlink(missing_ok=True)
    if action:
        result = _adb(udid, "shell", "am", "start", "-W", "-a", component, check=False)
    else:
        escaped_component = component.replace("$", r"\$")
        result = _adb(udid, "shell", "am", "start", "-W", "-n", escaped_component, check=False)
    if "Error" in result or "Exception" in result:
        return ""
    time.sleep(1.5)
    _adb(udid, "shell", "uiautomator", "dump", "/sdcard/laf2_resource_settings.xml")
    hierarchy = _adb(udid, "exec-out", "cat", "/sdcard/laf2_resource_settings.xml", binary=True)
    visible_text = hierarchy.decode(errors="replace")
    if expected_text not in visible_text:
        return ""
    target.write_bytes(_adb(udid, "exec-out", "screencap", "-p", binary=True))
    return visible_text if target.exists() and target.stat().st_size > 1000 else ""


def capture_resource_status_reference(config):
    """Capture one independent RAM/disk snapshot shared by four TestCases."""
    raw_meminfo = _adb(config.udid, "shell", "cat", "/proc/meminfo")
    meminfo = _parse_meminfo(raw_meminfo)
    if "MemTotal" not in meminfo or "MemAvailable" not in meminfo:
        raise EvidenceCaptureError("/proc/meminfo lacks MemTotal or MemAvailable")
    filesystem = _parse_data_filesystem(_adb(config.udid, "shell", "df", "-k", "/data"))
    memory_visible = _open_settings_screenshot(
        config.udid,
        "com.android.settings/.Settings$AppMemoryUsageActivity",
        SETUP_MEMORY_SCREENSHOT,
        "Average memory use",
    )
    storage_visible = _open_settings_screenshot(
        config.udid,
        "com.android.settings/.Settings$StorageDashboardActivity",
        SETUP_STORAGE_SCREENSHOT,
        "Storage",
    )
    SETUP_RESOURCE_STATUS.write_text(
        json.dumps(
            {
                "captured_epoch_ms": int(_adb(config.udid, "shell", "date", "+%s%3N").strip()),
                "reference": {
                    "mem_total": meminfo["MemTotal"],
                    "mem_available": meminfo["MemAvailable"],
                    "disk_total": filesystem["total_bytes"],
                    "disk_free": filesystem["free_bytes"],
                },
                "sources": {
                    "memory": "/proc/meminfo (kB converted to bytes)",
                    "disk": "df -k /data (1 KiB blocks converted to bytes)",
                    "memory_settings_visible": bool(memory_visible),
                    "storage_settings_visible": bool(storage_visible),
                    "meminfo_lines": [
                        line.strip()
                        for line in raw_meminfo.splitlines()
                        if line.startswith("MemTotal:") or line.startswith("MemAvailable:")
                    ],
                    "storage_visible_text": list(dict.fromkeys(
                        html.unescape(value)
                        for value in re.findall(r'text="([^"]+)"', storage_visible)
                        if "GB" in value or value == "Storage"
                    )),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _write_html_screenshot(document, screenshot, width=1400, height=1000):
    screenshot.unlink(missing_ok=True)
    chrome = os.environ.get("CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    with tempfile.TemporaryDirectory(prefix="laf2-resource-chrome-") as profile:
        process = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--disable-background-networking", "--hide-scrollbars", "--no-first-run", f"--user-data-dir={profile}", f"--window-size={width},{height}", f"--screenshot={screenshot}", document.resolve().as_uri()],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if screenshot.exists() and screenshot.stat().st_size > 1000:
                break
            if process.poll() is not None:
                break
            time.sleep(0.1)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    if not screenshot.exists():
        raise EvidenceCaptureError(f"Evidence screenshot was not created: {screenshot.name}")


def _resource_evidence_document(field, info, source_image):
    labels = {
        "mem_total": "RAM Status (Total)",
        "mem_available": "RAM Status (Available)",
        "disk_total": "Disk Storage (Total)",
        "disk_free": "Disk Storage (Free)",
    }
    reference = info["reference"][field]
    actual = info["actual"].get(field)
    comparison = info["comparisons"][field]
    result = "PASS" if comparison["within_tolerance"] else "FAILED"
    color = "#287a3d" if result == "PASS" else "#b9342b"
    difference = comparison.get("difference_bytes")
    tolerance = comparison.get("tolerance_bytes")
    source_note = ""
    if field.startswith("mem_"):
        raw_lines = info.get("sources", {}).get("meminfo_lines", [])
        source_html = '<div class="kernel"><b>Android kernel source: /proc/meminfo</b><pre>' + html.escape("\n".join(raw_lines)) + "</pre></div>"
        raw_kb = reference // 1024
        formula = f"{raw_kb:,} kB × 1,024 = {reference:,} bytes"
        source_note = "Settings does not expose instantaneous Total/Available RAM on this Pixel; the kernel lines are the direct OS source."
    else:
        encoded = base64.b64encode(source_image.read_bytes()).decode() if source_image.exists() else ""
        source_html = f'<div class="phone"><img src="data:image/png;base64,{encoded}"></div>'
        visible = info.get("sources", {}).get("storage_visible_text", [])
        visible_line = " · ".join(visible) or "Android Settings Storage"
        total_match = next((re.search(r"([\d.]+)\s*GB total", text) for text in visible if "GB total" in text), None)
        used_match = next((re.search(r"([\d.]+)\s*GB used", text) for text in visible if "GB used" in text), None)
        if field == "disk_free" and total_match and used_match:
            total_gb = float(total_match.group(1))
            used_gb = float(used_match.group(1))
            formula = f"{total_gb:g} GB total − {used_gb:g} GB used ≈ {total_gb - used_gb:g} GB free"
        elif total_match:
            formula = f"{float(total_match.group(1)):g} GB total (visible in Android Settings)"
        else:
            formula = visible_line
        source_note = "Settings values are rounded for people; exact validation uses the same /data filesystem in bytes."
    actual_text = f"{actual:,} bytes" if type(actual) is int else "—"
    difference_text = f"{difference:,} bytes" if type(difference) is int else "—"
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{width:1400px;height:1000px;padding:42px 68px}}.eyebrow{{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}}h1{{font-size:38px;margin:7px 0 18px}}.phone{{height:330px;overflow:hidden;border-radius:20px;background:#e7eaff;border:1px solid #cbd4e8;position:relative}}.phone img{{position:absolute;width:760px;height:auto;left:252px;top:0}}.kernel{{height:245px;padding:27px 34px;background:#131a21;color:#dbe8e9;border-radius:18px;font:18px ui-monospace,monospace}}.kernel b{{color:#79d1d8}}pre{{font:700 28px/1.7 ui-monospace,monospace;margin:18px 0}}.note{{font-size:14px;color:#60717c;margin:8px 0 15px}}.formula{{background:#fff;border-radius:17px;padding:17px 27px;box-shadow:0 8px 24px #131a2112}}.row{{display:grid;grid-template-columns:250px 1fr;gap:18px;padding:10px 0;border-bottom:1px solid #e4e9ed}}.row:last-child{{border:0}}.label{{color:#60717c;font-size:16px}}.value{{font:700 19px ui-monospace,monospace}}.conclusion{{display:flex;justify-content:space-between;align-items:center;margin-top:16px;padding:15px 25px;background:#fff;border-radius:14px;border-left:8px solid {color}}}.conclusion b{{font-size:26px;color:{color}}}.conclusion span{{font:700 17px ui-monospace,monospace}}</style></head><body><main>
<div class="eyebrow">PRIMARY SOURCE · ANDROID OS</div><h1>{labels[field]}</h1>{source_html}<div class="note">{html.escape(source_note)}</div><section class="formula">
<div class="row"><span class="label">Visible-source formula</span><span class="value">{html.escape(formula)}</span></div>
<div class="row"><span class="label">Exact OS reference</span><span class="value">{reference:,} bytes</span></div>
<div class="row"><span class="label">SDK answer</span><span class="value">{actual_text}</span></div>
<div class="row"><span class="label">Difference / tolerance</span><span class="value">{difference_text} / {tolerance:,} bytes</span></div></section>
<div class="conclusion"><span>Compare source-derived value with SDK answer</span><b>{result}</b></div>
</main></body></html>'''


def _render_resource_status_screenshots(folder, info):
    for field in ("mem_total", "mem_available", "disk_total", "disk_free"):
        stem = field.replace("_", "-") + "-evidence"
        document = folder / f"{stem}.html"
        screenshot = folder / f"{stem}.png"
        document.write_text(
            _resource_evidence_document(field, info, SETUP_STORAGE_SCREENSHOT),
            encoding="utf-8",
        )
        _write_html_screenshot(document, screenshot)


def materialize_resource_status(folder):
    folder = Path(folder)
    if not SETUP_RESOURCE_STATUS.exists():
        raise EvidenceCaptureError("independent resource-status reference is missing")
    info = json.loads(SETUP_RESOURCE_STATUS.read_text())
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    values = {}
    for field in ("mem_total", "mem_available", "disk_total", "disk_free"):
        plaintext = decoded.get("ext", {}).get("plaintext", {})
        device = plaintext.get("device") if isinstance(plaintext, dict) else None
        device_ext = device.get("ext") if isinstance(device, dict) else None
        values[field] = device_ext.get(field) if isinstance(device_ext, dict) else None
    reference = info["reference"]
    comparisons = {}
    for field, expected in reference.items():
        actual = values.get(field)
        dynamic = field in {"mem_available", "disk_free"}
        base_total = reference["mem_total" if field.startswith("mem_") else "disk_total"]
        tolerance = max(round(base_total * (0.10 if field == "mem_available" else 0.02)), 512 * 1024 * 1024) if dynamic else round(expected * 0.02)
        difference = abs(actual - expected) if type(actual) is int else None
        comparisons[field] = {
            "difference_bytes": difference,
            "tolerance_bytes": tolerance,
            "within_tolerance": difference is not None and difference <= tolerance,
        }
    info.update({"actual": values, "comparisons": comparisons})
    (folder / "resource-status.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    for source, name in ((SETUP_MEMORY_SCREENSHOT, "memory-settings.png"), (SETUP_STORAGE_SCREENSHOT, "storage-settings.png")):
        if source.exists():
            shutil.copy2(source, folder / name)
    _render_resource_status_screenshots(folder, info)


def _key_value_lines(raw):
    values = {}
    for line in raw.splitlines():
        if ":" in line:
            key, value = line.strip().split(":", 1)
            values[key.strip()] = value.strip()
    return values


def capture_battery_status(config):
    battery_text = _open_settings_screenshot(
        config.udid, "android.intent.action.POWER_USAGE_SUMMARY",
        SETUP_BATTERY_SCREENSHOT, "Battery Saver", action=True,
    )
    saver_text = _open_settings_screenshot(
        config.udid, "android.settings.BATTERY_SAVER_SETTINGS",
        SETUP_BATTERY_SAVER_SCREENSHOT, "Battery Saver", action=True,
    )
    if not battery_text or not saver_text:
        raise EvidenceCaptureError("native Battery or Battery Saver page is unavailable")
    raw = _adb(config.udid, "shell", "dumpsys", "battery")
    values = _key_value_lines(raw)
    powered = any(values.get(name) == "true" for name in ("AC powered", "USB powered", "Wireless powered", "Dock powered"))
    SETUP_BATTERY_STATUS.write_text(json.dumps({"level": int(values["level"]), "charging": 1 if powered else 0, "battery_saver": _adb(config.udid, "shell", "settings", "get", "global", "low_power").strip() == "1", "source": {"battery": "dumpsys battery", "battery_saver": "settings get global low_power"}}, indent=2) + "\n")


def materialize_battery_status(folder):
    folder = Path(folder)
    info = json.loads(SETUP_BATTERY_STATUS.read_text())
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    ext = decoded.get("ext", {}).get("plaintext", {})
    device = ext.get("device", {}) if isinstance(ext, dict) else {}
    device_ext = device.get("ext", {}) if isinstance(device, dict) else {}
    info["actual"] = {"batterylevel": device.get("batterylevel"), "charging": device.get("charging"), "battery_saver": device_ext.get("battery_saver") if isinstance(device_ext, dict) else None}
    (folder / "battery-status.json").write_text(json.dumps(info, indent=2) + "\n")
    shutil.copy2(SETUP_BATTERY_SCREENSHOT, folder / "battery-settings.png")
    shutil.copy2(SETUP_BATTERY_SAVER_SCREENSHOT, folder / "battery-saver-settings.png")


def _wm_value(raw, label):
    match = re.search(rf"Override {label}:\s*(\d+)(?:x(\d+))?", raw)
    if not match:
        match = re.search(rf"Physical {label}:\s*(\d+)(?:x(\d+))?", raw)
    if not match:
        raise EvidenceCaptureError(f"cannot parse wm {label}: {raw!r}")
    return tuple(int(value) for value in match.groups() if value is not None)


def capture_display_status(config):
    visible = _open_settings_screenshot(config.udid, "com.android.settings/.Settings$DisplaySettingsActivity", SETUP_DISPLAY_SCREENSHOT, "Display")
    if not visible:
        raise EvidenceCaptureError("native Display page is unavailable")
    font_scale_visible = _open_settings_screenshot(
        config.udid,
        "com.android.settings/.Settings$TextReadingSettingsActivity",
        SETUP_FONT_SCALE_SCREENSHOT,
        "Font size",
    )
    if not font_scale_visible:
        raise EvidenceCaptureError("native Display size & text page is unavailable")
    visible_labels = [html.unescape(value) for value in re.findall(r'text="([^"]+)"', visible)]
    brightness_ui_percent = next(
        (value for value in visible_labels if re.fullmatch(r"\d+%", value)),
        None,
    )
    width, height = _wm_value(_adb(config.udid, "shell", "wm", "size"), "size")
    (density,) = _wm_value(_adb(config.udid, "shell", "wm", "density"), "density")
    model = _adb(config.udid, "shell", "getprop", "ro.product.model").strip()
    brightness_raw = int(_adb(config.udid, "shell", "settings", "get", "system", "screen_brightness").strip())
    display_dump = _adb(config.udid, "shell", "dumpsys", "display")
    brightness_float_match = re.search(r"mLastUserSetScreenBrightness=([0-9.]+)", display_dump)
    synchronizer_int_match = re.search(r"mLatestIntBrightness=(\d+)", display_dump)
    synchronizer_float_match = re.search(r"mLatestFloatBrightness=([0-9.]+)", display_dump)
    if not brightness_float_match or not synchronizer_int_match or not synchronizer_float_match:
        raise EvidenceCaptureError("cannot read Android BrightnessSynchronizer evidence")
    brightness_system_float = float(brightness_float_match.group(1))
    brightness_sync_int = int(synchronizer_int_match.group(1))
    brightness_sync_float = float(synchronizer_float_match.group(1))
    font_scale = float(_adb(config.udid, "shell", "settings", "get", "system", "font_scale").strip())
    dark_mode = _adb(config.udid, "shell", "cmd", "uimode", "night").strip().lower().endswith("yes")
    SETUP_QUICK_BRIGHTNESS_SCREENSHOT.unlink(missing_ok=True)
    try:
        _adb(config.udid, "shell", "cmd", "statusbar", "expand-settings")
        time.sleep(1.5)
        SETUP_QUICK_BRIGHTNESS_SCREENSHOT.write_bytes(
            _adb(config.udid, "exec-out", "screencap", "-p", binary=True)
        )
    finally:
        _adb(config.udid, "shell", "cmd", "statusbar", "collapse", check=False)
    if SETUP_QUICK_BRIGHTNESS_SCREENSHOT.stat().st_size < 1000:
        raise EvidenceCaptureError("Quick Settings brightness screenshot is unavailable")
    SETUP_DISPLAY_STATUS.write_text(
        json.dumps(
            {
                "model": model,
                "width": width,
                "height": height,
                "density_dpi": density,
                "pixel_ratio": density / 160,
                "brightness_raw": brightness_raw,
                "screen_brightness": brightness_raw / 255,
                "brightness_ui_percent": brightness_ui_percent,
                "brightness_system_float": brightness_system_float,
                "brightness_sync_int": brightness_sync_int,
                "brightness_sync_float": brightness_sync_float,
                "font_scale": font_scale,
                "dark_mode": dark_mode,
                "official_spec": OFFICIAL_DISPLAY_SPECS.get(model),
                "source": ["native Display screenshot", "native Display size & text screenshot", "wm size", "wm density"],
            },
            indent=2,
        )
        + "\n"
    )


def _display_evidence_document(field, info, source_image):
    actual_key = {"width": "sw", "height": "sh", "density_dpi": "ppi", "pixel_ratio": "pxratio", "screen_brightness": "screen_bright", "font_scale": "fontscale", "dark_mode": "darkmode"}[field]
    title = {"width": "Screen Width", "height": "Screen Height", "density_dpi": "Screen PPI", "pixel_ratio": "Pixel Ratio", "screen_brightness": "Screen Brightness", "font_scale": "Font Scale", "dark_mode": "Dark Mode"}[field]
    reference = info[field]
    actual = info["actual"].get(actual_key)
    tolerances = {"pixel_ratio": 1e-6, "screen_brightness": 1 / 255 + 1e-8, "font_scale": 1e-6}
    if field == "dark_mode":
        logical_match = type(actual) is bool and actual is reference
    elif field in tolerances:
        logical_match = type(actual) in (int, float) and abs(actual - reference) <= tolerances[field]
    else:
        logical_match = type(actual) is int and actual == reference
    official = info.get("official_spec") or {}
    physical_ppi = official.get("physical_ppi")
    spec_difference = abs(actual - physical_ppi) if field == "density_dpi" and type(actual) is int and physical_ppi else None
    spec_within_tolerance = spec_difference is None or spec_difference / physical_ppi <= 0.05
    passed = logical_match and spec_within_tolerance
    color = "#287a3d" if passed else "#b9342b"
    result = "PASS" if passed else "FAILED"
    encoded = base64.b64encode(source_image.read_bytes()).decode()
    dimension = f'{info["width"]:,} px × {info["height"]:,} px'
    phone_class = "phone"
    if field == "screen_brightness":
        dimension_marker = '<div class="dimension horizontal">DISPLAY SETTINGS · VISIBLE PERCENT</div>'
    elif field == "height":
        dimension_marker = f'<div class="dimension vertical">{info["height"]:,} px · HEIGHT</div>'
    elif field == "width":
        dimension_marker = f'<div class="dimension horizontal">{info["width"]:,} px · WIDTH</div>'
    else:
        dimension_marker = f'<div class="dimension horizontal">{dimension}</div>'
    if field == "density_dpi":
        official_row = (
            f'<div class="row"><span>Official physical PPI</span><b>{physical_ppi:g} PPI · {html.escape(official["source"])}</b></div>'
            f'<div class="row"><span>Logical vs physical</span><b>{spec_difference:.1f} · within ±5%</b></div>'
            if physical_ppi else
            '<div class="row"><span>Official physical PPI</span><b>Model not mapped · informational check skipped</b></div>'
        )
        explanation = "Android logical density is expected to be close to, but not identical to, panel physical PPI."
        source_label = f'wm density = {reference:,} dpi'
    elif field in {"width", "height"}:
        axis = "horizontal width" if field == "width" else "vertical height"
        official_row = f'<div class="row"><span>Visible image dimensions</span><b>{dimension}</b></div>'
        explanation = f'The captured phone image itself is {dimension}; the highlighted {axis} is the direct pixel evidence.'
        source_label = f'wm size = {dimension}'
    elif field == "pixel_ratio":
        official_row = f'<div class="row"><span>Formula</span><b>{info["density_dpi"]} ÷ 160 = {reference:g}</b></div>'
        explanation = "Android density scale is derived directly from logical density DPI divided by the 160-dpi baseline."
        source_label = f'wm density {info["density_dpi"]} ÷ 160 = {reference:g}'
    elif field == "screen_brightness":
        official_row = (
            f'<div class="row"><span>Visible UI brightness</span><b>{html.escape(info.get("brightness_ui_percent") or "Unavailable")}</b></div>'
            f'<div class="row"><span>Android display service</span><b>{info["brightness_system_float"]:.8f} · same UI state</b></div>'
            f'<div class="row"><span>BrightnessSynchronizer</span><b>int {info["brightness_sync_int"]} ↔ float {info["brightness_sync_float"]:.8f}</b></div>'
            f'<div class="row"><span>SDK normalization</span><b>{info["brightness_raw"]} ÷ 255 = {reference:.8f}</b></div>'
        )
        explanation = "Display Settings shows the perceptual UI percentage. The same Android display-service snapshot links that UI state to a float brightness, while BrightnessSynchronizer links it to the legacy integer used by the SDK. The SDK value is that integer normalized to 0–1."
        source_label = f'UI {info.get("brightness_ui_percent") or "—"} ↔ Android {info["brightness_sync_float"]:.8f} ↔ int {info["brightness_sync_int"]} → SDK {reference:.8f}'
    elif field == "font_scale":
        official_row = '<div class="row"><span>OS source</span><b>settings get system font_scale</b></div>'
        explanation = "Display size & text is the visible setting; Android font_scale supplies its exact numeric state."
        source_label = f'Android font_scale = {reference:g}'
    else:
        official_row = '<div class="row"><span>OS source</span><b>cmd uimode night</b></div>'
        explanation = "The native Dark theme switch is the visible source and Android UI mode supplies the exact boolean state."
        source_label = f'Android night mode = {str(reference).lower()}'
    expected_rule = (
        "Match wm density; mapped official physical PPI difference must be within ±5%."
        if field == "density_dpi" else
        f'Match the captured Android {field.replace("_", " ")} state.'
    )
    reference_text = str(reference).lower() if isinstance(reference, bool) else (f"{reference:.8f}" if field == "screen_brightness" else f"{reference:,}")
    actual_text = str(actual).lower() if isinstance(actual, bool) else (f"{actual:,}" if isinstance(actual, (int, float)) else "—")
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{width:1400px;height:1000px;padding:38px 62px}}.eyebrow{{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}}h1{{font-size:38px;margin:7px 0 16px}}.content{{display:grid;grid-template-columns:430px 1fr;gap:38px}}.phone{{height:690px;display:flex;justify-content:center;position:relative;overflow:hidden;background:#dfe5f5;border-radius:24px;padding:22px 42px 22px 22px}}.phone img{{height:646px;width:auto;border-radius:13px;box-shadow:0 12px 28px #17233335}}.quick-brightness{{height:190px;margin-top:155px;padding:0}}.quick-brightness img{{position:absolute;height:1400px;max-width:none;top:-200px;left:50%;transform:translateX(-50%);border-radius:0}}.dimension{{position:absolute;z-index:2;text-align:center;background:#0e7c86;color:white;border-radius:20px;padding:6px;font:700 18px ui-monospace,monospace}}.horizontal{{left:20px;right:20px;top:13px}}.vertical{{right:7px;top:90px;height:540px;writing-mode:vertical-rl;display:flex;align-items:center;justify-content:center;padding:12px 7px}}.panel{{padding-top:18px}}.source{{padding:22px 26px;background:#14202a;color:#8ee0e6;border-radius:17px;font:700 23px ui-monospace,monospace}}.note{{font-size:17px;line-height:1.45;color:#526571;margin:17px 3px 25px}}.rows{{background:white;border-radius:18px;padding:10px 25px}}.row{{display:grid;grid-template-columns:220px 1fr;gap:18px;padding:18px 0;border-bottom:1px solid #e3e9ed}}.row:last-child{{border:0}}.row span{{color:#60717c}}.row b{{font:700 18px ui-monospace,monospace}}.conclusion{{display:flex;justify-content:space-between;align-items:center;margin-top:22px;padding:20px 26px;background:white;border-radius:16px}}.conclusion b{{font-size:28px}}</style></head><body><main>
<div class="eyebrow">DIRECT SCREEN EVIDENCE · ANDROID OS</div><h1>{title}</h1><div class="content"><div class="{phone_class}">{dimension_marker}<img src="data:image/png;base64,{encoded}"></div><div class="panel"><div class="source">{source_label}</div><p class="note">{explanation}</p><div class="rows">
<div class="row"><span>Expected</span><b>{expected_rule}</b></div><div class="row"><span>Device model</span><b>{html.escape(info.get("model") or "Unknown")}</b></div><div class="row"><span>Captured Device State</span><b>{reference_text}</b></div><div class="row"><span>Decoded Bid Request</span><b>{actual_text}</b></div>{official_row}</div>
<div class="conclusion" style="border-left:8px solid {color}"><span>Compare direct source with SDK answer</span><b style="color:{color}">{result}</b></div></div></div></main></body></html>'''


def _render_display_evidence(folder, info):
    fields = {"width": "screen-width", "height": "screen-height", "density_dpi": "screen-ppi", "pixel_ratio": "pixel-ratio", "screen_brightness": "screen-brightness", "font_scale": "font-scale", "dark_mode": "dark-mode"}
    for field, name in fields.items():
        stem = name + "-evidence"
        document = folder / f"{stem}.html"
        screenshot = folder / f"{stem}.png"
        source_image = (
            SETUP_DISPLAY_SCREENSHOT
            if field == "screen_brightness"
            else SETUP_FONT_SCALE_SCREENSHOT
            if field == "font_scale"
            else SETUP_DISPLAY_SCREENSHOT
        )
        document.write_text(_display_evidence_document(field, info, source_image), encoding="utf-8")
        _write_html_screenshot(document, screenshot)
        if field == "screen_brightness":
            document.unlink(missing_ok=True)


def materialize_display_status(folder):
    folder = Path(folder)
    info = json.loads(SETUP_DISPLAY_STATUS.read_text())
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    ext = decoded.get("ext", {}).get("plaintext", {})
    device = ext.get("device", {}) if isinstance(ext, dict) else {}
    device_ext = device.get("ext", {}) if isinstance(device, dict) else {}
    info["actual"] = {field: device.get(field) for field in ("sw", "sh", "ppi", "pxratio")}
    info["actual"].update({field: device_ext.get(field) for field in ("screen_bright", "fontscale", "darkmode", "gyroscope", "accelerometer")})
    (folder / "display-status.json").write_text(json.dumps(info, indent=2) + "\n")
    shutil.copy2(SETUP_DISPLAY_SCREENSHOT, folder / "display-settings.png")
    shutil.copy2(SETUP_FONT_SCALE_SCREENSHOT, folder / "font-scale-settings.png")
    _render_display_evidence(folder, info)


def _utc_offset_minutes(raw):
    match = re.fullmatch(r"([+-])(\d{2})(\d{2})", raw.strip())
    if not match:
        raise EvidenceCaptureError(f"cannot parse UTC offset: {raw!r}")
    minutes = int(match.group(2)) * 60 + int(match.group(3))
    return minutes if match.group(1) == "+" else -minutes


def _music_volume(raw):
    match = re.search(
        r"- STREAM_MUSIC:.*?Max:\s*(\d+).*?streamVolume:(\d+)",
        raw,
        re.DOTALL,
    )
    if not match:
        raise EvidenceCaptureError("cannot parse STREAM_MUSIC from dumpsys audio")
    maximum, current = map(int, match.groups())
    if maximum <= 0:
        raise EvidenceCaptureError("STREAM_MUSIC maximum is not positive")
    return current, maximum


def capture_volume_status(config):
    if not _open_settings_screenshot(
        config.udid, "com.android.settings/.Settings$SoundSettingsActivity", SETUP_SOUND_SCREENSHOT, "Media volume"
    ):
        raise EvidenceCaptureError("native Sound & vibration page is unavailable")
    current, maximum = _music_volume(_adb(config.udid, "shell", "dumpsys", "audio"))
    SETUP_VOLUME_STATUS.write_text(json.dumps({"current": current, "max": maximum, "normalized": current / maximum}, indent=2) + "\n")


def materialize_volume_status(folder):
    folder = Path(folder)
    info = json.loads(SETUP_VOLUME_STATUS.read_text())
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    ext = decoded.get("ext", {}).get("plaintext", {})
    device = ext.get("device", {}) if isinstance(ext, dict) else {}
    device_ext = device.get("ext", {}) if isinstance(device, dict) else {}
    info["actual"] = device_ext.get("volume") if isinstance(device_ext, dict) else None
    (folder / "volume-status.json").write_text(json.dumps(info, indent=2) + "\n")
    shutil.copy2(SETUP_SOUND_SCREENSHOT, folder / "volume-evidence.png")


def capture_timezone_status(config):
    if not _open_settings_screenshot(
        config.udid,
        "com.android.settings/.Settings$DateTimeSettingsActivity",
        SETUP_DATETIME_SCREENSHOT,
        "Time zone",
    ):
        raise EvidenceCaptureError("native Date & time page is unavailable")
    SETUP_TIMEZONE_STATUS.write_text(json.dumps({
        "timezone": _adb(config.udid, "shell", "getprop", "persist.sys.timezone").strip(),
        "utcoffset": _utc_offset_minutes(_adb(config.udid, "shell", "date", "+%z")),
    }, indent=2) + "\n")


def materialize_timezone_status(folder):
    folder = Path(folder)
    info = json.loads(SETUP_TIMEZONE_STATUS.read_text())
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    info["actual"] = {
        "req_utcoffset": decoded.get("req", {}).get("plaintext", {}).get("device", {}).get("utcoffset"),
        "ext_utcoffset": decoded.get("ext", {}).get("plaintext", {}).get("device", {}).get("utcoffset"),
    }
    (folder / "timezone-status.json").write_text(json.dumps(info, indent=2) + "\n")
    shutil.copy2(SETUP_DATETIME_SCREENSHOT, folder / "timezone-changed.png")


def _tap_visible_text(udid, text):
    _adb(udid, "shell", "uiautomator", "dump", "/sdcard/laf2_settings_tap.xml")
    document = ET.fromstring(_adb(udid, "exec-out", "cat", "/sdcard/laf2_settings_tap.xml", binary=True))
    node = next((item for item in document.iter("node") if item.attrib.get("text") == text), None)
    if node is None:
        return False
    x, y = _bounds_center(node.attrib.get("bounds"))
    _adb(udid, "shell", "input", "tap", str(x), str(y))
    time.sleep(1)
    return True


def capture_location_permission_status(config):
    SETUP_LOCATION_PERMISSION_SCREENSHOT.unlink(missing_ok=True)
    _adb(
        config.udid, "shell", "am", "start", "-W", "-a",
        "android.settings.APPLICATION_DETAILS_SETTINGS", "-d", f"package:{config.app_package}",
    )
    time.sleep(1)
    if not _tap_visible_text(config.udid, "Permissions"):
        raise EvidenceCaptureError("App info does not expose a visible Permissions row")
    _adb(config.udid, "shell", "uiautomator", "dump", "/sdcard/laf2_location_permission.xml")
    hierarchy = _adb(config.udid, "exec-out", "cat", "/sdcard/laf2_location_permission.xml", binary=True)
    visible = hierarchy.decode(errors="replace")
    if "Location" not in visible or "Not allowed" not in visible:
        raise EvidenceCaptureError("Location permission is not visibly listed under Not allowed")
    permission = _adb(
        config.udid, "shell", "cmd", "package", "check-permission",
        "android.permission.ACCESS_FINE_LOCATION", config.app_package, "0", check=False,
    ).strip().lower()
    denied = "granted" not in permission
    if not denied:
        raise EvidenceCaptureError("ACCESS_FINE_LOCATION is still granted")
    SETUP_LOCATION_PERMISSION_SCREENSHOT.write_bytes(
        _adb(config.udid, "exec-out", "screencap", "-p", binary=True)
    )
    SETUP_LOCATION_PERMISSION_STATUS.write_text(json.dumps({
        "permission": "android.permission.ACCESS_FINE_LOCATION",
        "denied": True,
        "command_result": permission,
    }, indent=2) + "\n")


def materialize_location_permission_status(folder):
    folder = Path(folder)
    info = json.loads(SETUP_LOCATION_PERMISSION_STATUS.read_text())
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    info["actual"] = {}
    for section in ("req", "ext"):
        device = decoded.get(section, {}).get("plaintext", {}).get("device", {})
        info["actual"][section] = {
            "geo_lat_present": "geo_lat" in device,
            "geo_lon_present": "geo_lon" in device,
            "geo_lat": device.get("geo_lat"),
            "geo_lon": device.get("geo_lon"),
        }
    (folder / "location-permission-status.json").write_text(json.dumps(info, indent=2) + "\n")
    shutil.copy2(SETUP_LOCATION_PERMISSION_SCREENSHOT, folder / "location-permission-denied.png")


def capture_device_context(config):
    pages = (
        ("com.android.settings/.Settings$SoundSettingsActivity", SETUP_SOUND_SCREENSHOT, "Media volume"),
        ("com.android.settings/.Settings$DateTimeSettingsActivity", SETUP_DATETIME_SCREENSHOT, "Time zone"),
        ("com.android.settings/.Settings$LanguageAndRegionSettingsActivity", SETUP_LANGUAGE_SCREENSHOT, "Languages"),
    )
    for component, target, expected_text in pages:
        if not _open_settings_screenshot(config.udid, component, target, expected_text):
            raise EvidenceCaptureError(f"native Settings page is unavailable: {expected_text}")
    about_result = _adb(
        config.udid,
        "shell",
        "am",
        "start",
        "-W",
        "-n",
        r"com.android.settings/.Settings\$MyDeviceInfoActivity",
        check=False,
    )
    if "Error" in about_result or "Exception" in about_result:
        raise EvidenceCaptureError("native About phone page is unavailable")
    time.sleep(1.5)
    SETUP_ABOUT_SCREENSHOT.write_bytes(_adb(config.udid, "exec-out", "screencap", "-p", binary=True))
    _adb(config.udid, "shell", "am", "start", "-a", "android.settings.WIFI_SETTINGS")
    time.sleep(1.5)
    _adb(config.udid, "shell", "uiautomator", "dump", "/sdcard/laf2_wifi_settings.xml")
    wifi_document = _adb(config.udid, "exec-out", "cat", "/sdcard/laf2_wifi_settings.xml", binary=True)
    wifi_text = "\n".join(node.attrib.get("text", "") for node in ET.fromstring(wifi_document).iter("node"))
    if "Wi‑Fi" not in wifi_text and "Wi-Fi" not in wifi_text and "Internet" not in wifi_text:
        raise EvidenceCaptureError("native Wi-Fi detail page is unavailable")
    SETUP_NETWORK_SCREENSHOT.write_bytes(_adb(config.udid, "exec-out", "screencap", "-p", binary=True))
    _adb(config.udid, "shell", "am", "force-stop", "com.google.android.inputmethod.latin")
    _adb(config.udid, "shell", "am", "start", "-n", "com.google.android.inputmethod.latin/com.google.android.apps.inputmethod.latin.preference.SettingsActivity")
    time.sleep(1.5)
    _adb(config.udid, "shell", "uiautomator", "dump", "/sdcard/laf2_keyboard.xml")
    keyboard_root = ET.fromstring(_adb(config.udid, "exec-out", "cat", "/sdcard/laf2_keyboard.xml", binary=True))
    language_node = next((node for node in keyboard_root.iter("node") if node.attrib.get("text") == "Languages"), None)
    if language_node is None:
        raise EvidenceCaptureError("Gboard Settings does not expose Languages")
    x, y = _bounds_center(language_node.attrib.get("bounds"))
    _adb(config.udid, "shell", "input", "tap", str(x), str(y))
    time.sleep(1.5)
    SETUP_KEYBOARD_SCREENSHOT.write_bytes(_adb(config.udid, "exec-out", "screencap", "-p", binary=True))
    _adb(config.udid, "shell", "am", "start", "-n", "com.topjohnwu.magisk/.ui.MainActivity", check=False)
    time.sleep(1.5)
    SETUP_ROOT_SCREENSHOT.write_bytes(_adb(config.udid, "exec-out", "screencap", "-p", binary=True))
    volume_current, volume_max = _music_volume(_adb(config.udid, "shell", "dumpsys", "audio"))
    locale = _adb(config.udid, "shell", "getprop", "persist.sys.locale").strip()
    if not locale:
        raise EvidenceCaptureError("Android system locale is empty")
    input_method = _adb(config.udid, "shell", "dumpsys", "input_method")
    enabled_ime = _adb(config.udid, "shell", "settings", "get", "secure", "enabled_input_methods")
    subtype_ids = re.findall(r"com\.google\.android\.inputmethod\.latin/com\.android\.inputmethod\.latin\.LatinIME((?:;-?\d+)+)", enabled_ime)
    subtype_ids = re.findall(r"-?\d+", subtype_ids[0]) if subtype_ids else []
    input_languages = []
    for subtype_id in subtype_ids:
        match = re.search(rf"mSubtypeId={re.escape(subtype_id)}\b[^\n]*mSubtypeLanguageTag=([^ ]+)", input_method)
        if match and match.group(1) and match.group(1) not in input_languages:
            input_languages.append(match.group(1))
    root_output = _adb(config.udid, "shell", "su", "-c", "id", check=False)
    qemu = _adb(config.udid, "shell", "getprop", "ro.kernel.qemu").strip()
    product = _adb(config.udid, "shell", "getprop", "ro.product.name").strip().lower()
    connectivity = _adb(config.udid, "shell", "dumpsys", "connectivity")
    connection_type = "wifi" if re.search(r"Active default network:.*?Transports: WIFI", connectivity, re.DOTALL) else "cellular" if re.search(r"Active default network:.*?Transports: CELLULAR", connectivity, re.DOTALL) else "unknown"
    wifi_info = _adb(config.udid, "shell", "dumpsys", "wifi")
    ssid_match = re.search(r"SSID: ([^,\n]+)", wifi_info)
    subscriptions = _adb(config.udid, "shell", "dumpsys", "isub")
    no_active_sim = "activeDataSubId=-1" in subscriptions and "Active subscriptions:\n  [" not in subscriptions
    SETUP_DEVICE_CONTEXT.write_text(json.dumps({
        "volume_current": volume_current,
        "volume_max": volume_max,
        "volume_normalized": volume_current / volume_max,
        "make": _adb(config.udid, "shell", "getprop", "ro.product.manufacturer").strip(),
        "model": _adb(config.udid, "shell", "getprop", "ro.product.model").strip(),
        "locale": locale,
        "lang": re.split(r"[-_]", locale, maxsplit=1)[0].lower(),
        "langb_system_hint": locale.replace("_", "-"),
        "langb_process_ground_truth": None,
        "timezone": _adb(config.udid, "shell", "getprop", "persist.sys.timezone").strip(),
        "utcoffset": _utc_offset_minutes(_adb(config.udid, "shell", "date", "+%z")),
        "input_lang": input_languages,
        "jailbreak": "uid=0(root)" in root_output,
        "root_source": root_output.strip(),
        "emulator": qemu == "1" or any(token in product for token in ("sdk", "generic", "emulator")),
        "emulator_source": {"ro.kernel.qemu": qemu, "ro.product.name": product},
        "conntype": connection_type,
        "connected_wifi_ssid": ssid_match.group(1).strip().strip('"') if ssid_match else None,
        "no_active_sim": no_active_sim,
    }, ensure_ascii=False, indent=2) + "\n")


def _device_context_evidence(field, info, image_path):
    definitions = {
        "volume": ("Output Volume", f'{info["volume_current"]} ÷ {info["volume_max"]} = {info["volume_normalized"]:g}', info["actual"]["volume"], "Android Media volume"),
        "make": ("Device Make", info["make"], f'{info["actual"]["req_make"]} / {info["actual"]["make"]}', "Android manufacturer · req / ext"),
        "model": ("Device Model", info["model"], f'{info["actual"]["req_model"]} / {info["actual"]["model"]} · hwv {info["actual"]["req_hwv"]} / {info["actual"]["hwv"]}', "Android product model · req / ext"),
        "utcoffset": ("Default Timezone", f'UTC offset {info["utcoffset"]:+d} minutes', f'{info["actual"]["req_utcoffset"]} / {info["actual"]["utcoffset"]}', f'{info["timezone"]} · req / ext'),
        "lang": ("Default Language (ISO-639-1)", info["lang"], info["actual"]["lang"], f'System locale {info["locale"]}'),
        "langb": ("Default Language (BCP 47)", "Sample App Locale.getDefault().toLanguageTag() output required", f'{info["actual"]["req_langb"]} / {info["actual"]["langb"]}', f'System locale hint {info["locale"]} · not process ground truth'),
        "input_lang": ("Installed Keyboard Languages", info["input_lang"], info["actual"]["input_lang"], "Enabled Gboard subtypes"),
        "jailbreak": ("Root Status", info["jailbreak"], info["actual"]["jailbreak"], "su -c id · Android field name remains jailbreak"),
        "emulator": ("Emulator Detection", info["emulator"], info["actual"]["emulator"], "Android hardware properties"),
        "conntype": ("Connection Type", info["conntype"], f'{info["actual"]["req_conntype"]} / {info["actual"]["conntype"]}', f'Connected Wi-Fi {info.get("connected_wifi_ssid") or "unknown"} · req / ext'),
        "carrier": ("Carrier", "empty · no active SIM" if info["no_active_sim"] else "active carrier", info["actual"]["carrier"], "Android subscription state"),
        "mccmnc": ("MCC/MNC", "empty · no active SIM" if info["no_active_sim"] else "active MCC/MNC", info["actual"]["mccmnc"], "Android subscription state"),
    }
    title, expected, actual, source = definitions[field]
    encoded = base64.b64encode(image_path.read_bytes()).decode()
    checks = {
        "volume": lambda: type(info["actual"]["volume"]) in (int, float) and abs(info["actual"]["volume"] - info["volume_normalized"]) <= 1 / info["volume_max"] + 1e-8,
        "make": lambda: all(info["actual"][name] == info["make"] for name in ("req_make", "make")),
        "model": lambda: all(info["actual"][name] == info["model"] for name in ("req_model", "model", "req_hwv", "hwv")),
        "utcoffset": lambda: all(info["actual"][name] == info["utcoffset"] for name in ("req_utcoffset", "utcoffset")),
        "lang": lambda: info["actual"]["lang"] == info["lang"],
        "langb": lambda: False,
        "input_lang": lambda: info["actual"]["input_lang"] == info["input_lang"],
        "jailbreak": lambda: info["actual"]["jailbreak"] is info["jailbreak"],
        "emulator": lambda: info["actual"]["emulator"] is info["emulator"],
        "conntype": lambda: all(info["actual"][name] == info["conntype"] for name in ("req_conntype", "conntype")),
        "carrier": lambda: info["no_active_sim"] and info["actual"]["carrier"] == "",
        "mccmnc": lambda: info["no_active_sim"] and info["actual"]["mccmnc"] == "",
    }
    passed = checks[field]()
    if field == "langb":
        color, result = "#b5761a", "BLOCKED"
        evidence_note = "The Settings page and persist.sys.locale are supporting context only. Add a Sample App output for Locale.getDefault().toLanguageTag() before comparing req/ext device.langb."
        result_action = "Waiting for independent App process ground truth"
    else:
        color, result = ("#287a3d", "PASS") if passed else ("#b9342b", "FAILED")
        evidence_note = "The visible Settings page establishes the human-readable device state; the independent Android system value is compared with the decoded bid."
        result_action = "Compare Android source with SDK answer"
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{width:1400px;height:1000px;padding:42px 62px}}.eyebrow{{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}}h1{{font-size:38px;margin:8px 0 20px}}.content{{display:grid;grid-template-columns:430px 1fr;gap:38px}}.phone{{height:700px;display:flex;justify-content:center;overflow:hidden;background:#dfe5f5;border-radius:24px;padding:22px}}.phone img{{height:656px;width:auto;border-radius:13px;box-shadow:0 12px 28px #17233335}}.panel{{padding-top:22px}}.source{{padding:22px 26px;background:#14202a;color:#8ee0e6;border-radius:17px;font:700 22px ui-monospace,monospace}}.note{{font-size:18px;line-height:1.5;color:#526571;margin:18px 3px 26px}}.rows{{background:#fff;border-radius:18px;padding:10px 25px}}.row{{display:grid;grid-template-columns:210px 1fr;gap:18px;padding:21px 0;border-bottom:1px solid #e3e9ed}}.row:last-child{{border:0}}.row span{{color:#60717c}}.row b{{font:700 19px ui-monospace,monospace;overflow-wrap:anywhere}}.result{{display:flex;justify-content:space-between;margin-top:22px;padding:22px 26px;background:#fff;border-radius:16px;border-left:8px solid {color};}}.result b{{font-size:28px;color:{color};}}</style></head><body><main>
<div class="eyebrow">DIRECT SETTINGS EVIDENCE · ANDROID OS</div><h1>{html.escape(title)}</h1><div class="content"><div class="phone"><img src="data:image/png;base64,{encoded}"></div><div class="panel"><div class="source">{html.escape(str(source))}</div><p class="note">{html.escape(evidence_note)}</p><div class="rows"><div class="row"><span>Expected · Android</span><b>{html.escape(str(expected))}</b></div><div class="row"><span>Captured · Bid</span><b>{html.escape(str(actual))}</b></div></div><div class="result"><span>{html.escape(result_action)}</span><b>{result}</b></div></div></div></main></body></html>'''


def materialize_device_context(folder):
    folder = Path(folder)
    info = json.loads(SETUP_DEVICE_CONTEXT.read_text())
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    req = decoded.get("req", {}).get("plaintext", {}).get("device", {})
    ext = decoded.get("ext", {}).get("plaintext", {}).get("device", {})
    ext_fields = ext.get("ext", {}) if isinstance(ext, dict) else {}
    info["actual"] = {
        "volume": ext_fields.get("volume"), "make": ext.get("make"), "model": ext.get("model"),
        "hwv": ext.get("hwv"), "utcoffset": ext.get("utcoffset"), "lang": ext.get("lang"), "langb": ext.get("langb"),
        "req_make": req.get("make"), "req_model": req.get("model"), "req_hwv": req.get("hwv"),
        "req_utcoffset": req.get("utcoffset"), "req_langb": req.get("langb"),
        "input_lang": ext.get("input_lang"), "jailbreak": ext_fields.get("jailbreak"), "emulator": ext_fields.get("emulator"),
        "conntype": ext.get("conntype"), "req_conntype": req.get("conntype"), "carrier": req.get("carrier"), "mccmnc": req.get("mccmnc"),
    }
    (folder / "device-context.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    images = {"volume": SETUP_SOUND_SCREENSHOT, "make": SETUP_ABOUT_SCREENSHOT, "model": SETUP_ABOUT_SCREENSHOT,
              "utcoffset": SETUP_DATETIME_SCREENSHOT, "lang": SETUP_LANGUAGE_SCREENSHOT, "langb": SETUP_LANGUAGE_SCREENSHOT}
    images.update({"input_lang": SETUP_KEYBOARD_SCREENSHOT, "jailbreak": SETUP_ROOT_SCREENSHOT,
                   "emulator": SETUP_ABOUT_SCREENSHOT, "conntype": SETUP_NETWORK_SCREENSHOT,
                   "carrier": SETUP_NETWORK_SCREENSHOT, "mccmnc": SETUP_NETWORK_SCREENSHOT})
    for field, image_path in images.items():
        document = folder / f"{field}-evidence.html"
        document.write_text(_device_context_evidence(field, info, image_path), encoding="utf-8")
        _write_html_screenshot(document, folder / f"{field}-evidence.png")
        document.unlink(missing_ok=True)


EVIDENCE_CAPTURES = {
    ADS_SETTINGS: EvidenceProvider(capture_ads_settings, materialize_ads_settings),
    ADS_TRACKING_DENIED: EvidenceProvider(capture_tracking_denied, materialize_tracking_denied),
    APP_SET_ID: EvidenceProvider(after_bid=capture_app_set_id_info),
    BID: EvidenceProvider(),
    BOOT_TIMESTAMPS: EvidenceProvider(
        before_bid=capture_boot_time_reference,
        after_bid=materialize_boot_timestamps,
    ),
    BATTERY_STATUS: EvidenceProvider(capture_battery_status, materialize_battery_status),
    DISPLAY_STATUS: EvidenceProvider(capture_display_status, materialize_display_status),
    DEVICE_CONTEXT: EvidenceProvider(capture_device_context, materialize_device_context),
    IN_APP_PURCHASE_HISTORY: EvidenceProvider(after_bid=capture_in_app_purchase_history_info),
    INSTALLED_APP_LIST: EvidenceProvider(
        before_bid=capture_installed_apps_settings,
        after_bid=materialize_installed_apps_settings,
    ),
    RESOURCE_STATUS: EvidenceProvider(
        before_bid=capture_resource_status_reference,
        after_bid=materialize_resource_status,
    ),
    VOLUME_STATUS: EvidenceProvider(capture_volume_status, materialize_volume_status),
    TIMEZONE_STATUS: EvidenceProvider(capture_timezone_status, materialize_timezone_status),
    LOCATION_PERMISSION_STATUS: EvidenceProvider(capture_location_permission_status, materialize_location_permission_status),
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
