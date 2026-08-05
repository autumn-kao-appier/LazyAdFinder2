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
APP_SET_ID = "app-set-id"
BID = "bid"
BOOT_TIMESTAMPS = "boot-timestamps"
IN_APP_PURCHASE_HISTORY = "in-app-purchase-history"
INSTALLED_APP_LIST = "installed-app-list"
RESOURCE_STATUS = "resource-status"
SDK_BUILD_INFO = "sdk-build-info"
ADS_SETTINGS_ACTION = "com.google.android.gms.settings.ADS_PRIVACY"
INSTALLED_APPS_SETTINGS_ACTION = "android.settings.MANAGE_APPLICATIONS_SETTINGS"
SETUP_SCREENSHOT = Path("/tmp/laf2_ads_settings.png")
SETUP_STATE = Path("/tmp/laf2_ads_settings_state.json")
SETUP_INSTALLED_APPS_SCREENSHOT = Path("/tmp/laf2_installed_apps_settings.png")
SETUP_BOOT_TIME_REFERENCE = Path("/tmp/laf2_boot_time_reference.json")
SETUP_UPTIME_SCREENSHOT = Path("/tmp/laf2_uptime_settings.png")
SETUP_RESOURCE_STATUS = Path("/tmp/laf2_resource_status.json")
SETUP_MEMORY_SCREENSHOT = Path("/tmp/laf2_memory_settings.png")
SETUP_STORAGE_SCREENSHOT = Path("/tmp/laf2_storage_settings.png")
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


def _open_settings_screenshot(udid, component, target, expected_text):
    target.unlink(missing_ok=True)
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


EVIDENCE_CAPTURES = {
    ADS_SETTINGS: EvidenceProvider(capture_ads_settings, materialize_ads_settings),
    APP_SET_ID: EvidenceProvider(after_bid=capture_app_set_id_info),
    BID: EvidenceProvider(),
    BOOT_TIMESTAMPS: EvidenceProvider(
        before_bid=capture_boot_time_reference,
        after_bid=materialize_boot_timestamps,
    ),
    IN_APP_PURCHASE_HISTORY: EvidenceProvider(after_bid=capture_in_app_purchase_history_info),
    INSTALLED_APP_LIST: EvidenceProvider(
        before_bid=capture_installed_apps_settings,
        after_bid=materialize_installed_apps_settings,
    ),
    RESOURCE_STATUS: EvidenceProvider(
        before_bid=capture_resource_status_reference,
        after_bid=materialize_resource_status,
    ),
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
