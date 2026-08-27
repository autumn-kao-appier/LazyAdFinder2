"""iOS-only Evidence provider registry and capture orchestration."""

import base64
import html
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from testcases.ios_signal_testcases import (
    BID, IOS_BATTERY_VISIBLE, IOS_BOOT_PAYLOAD, IOS_BRIGHTNESS_VISIBLE, IOS_CHARGING_VISIBLE,
    IOS_DARK_MODE_VISIBLE, IOS_DEVICE_CONTEXT, IOS_DEVICE_IDENTITY, IOS_DISPLAY_STATUS, IOS_FONT_SIZE_VISIBLE,
    IOS_IDFA_VISIBLE, IOS_IDFV_PAYLOAD, IOS_IAP_PAYLOAD, IOS_LIFECYCLE_SEQUENCE,
    IOS_LOW_POWER_VISIBLE, IOS_OUTPUT_VOLUME_VISIBLE, IOS_QA_EVIDENCE, IOS_RAM_PAYLOAD,
    IOS_REVIEW_CONTEXT, IOS_SETTINGS_STATE, IOS_SYSTEM_CONTEXT_VISIBLE,
)


IOS_OFFICIAL_DISPLAY_SPECS = {
    "iPhone12,3": {
        "model": "iPhone 11 Pro",
        "native_width": 1125,
        "native_height": 2436,
        "physical_ppi": 458,
        "source": "Apple iPhone 11 Pro Technical Specifications",
        "url": "https://support.apple.com/en-ca/111879",
    },
}


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
    state = json.loads(source.read_text()) if source.is_file() else {}
    if state.get("scenario") == "TRACKING-ALLOWED":
        materialize_ios_tracking_allowed(folder)


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


def materialize_ios_idfa_visible(folder):
    folder = Path(folder)
    source = Path(os.environ.get("IOS_IDFA_STATE_FILE", "/tmp/laf2-ios-idfa-state.json"))
    screenshot = Path(os.environ.get("IOS_IDFA_SCREENSHOT", "/tmp/laf2-ios-idfa.png"))
    if source.is_file():
        shutil.copy2(source, folder / "ios-idfa-state.json")
    else:
        (folder / "ios-idfa-state.json").write_text(json.dumps({
            "status": "UNAVAILABLE",
            "source": "GetMyIDFA",
            "reason": "GetMyIDFA visible Evidence was not captured before the Bid.",
        }, ensure_ascii=False, indent=2) + "\n")
    if screenshot.is_file() and screenshot.stat().st_size:
        shutil.copy2(screenshot, folder / "ios-idfa.png")


def materialize_ios_idfv_payload(folder):
    """Materialize decoded IDFV as payload-only Evidence, matching the AOS contract."""
    folder = Path(folder)
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    plaintext = decoded.get("ext", {}).get("plaintext", {})
    device = plaintext.get("device") if isinstance(plaintext, dict) else None
    ext_value = device.get("ifv") if isinstance(device, dict) else None
    (folder / "app-set-id.json").write_text(json.dumps({
        "source": "ext.plaintext.device.ifv",
        "actual": {"ext_device_ifv": ext_value},
        "note": (
            "目前是單純抓包並解密 device.ifv。若需要可截圖的人眼 Evidence，"
            "需請 RD 在 iOS Sample App 增加顯示 IDFV 的測試入口。"
        ),
    }, ensure_ascii=False, indent=2) + "\n")


def materialize_ios_iap_payload(folder):
    """Materialize decoded in-app product IDs using the same scope as AOS."""
    folder = Path(folder)
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    plaintext = decoded.get("ext", {}).get("plaintext", {})
    device = plaintext.get("device") if isinstance(plaintext, dict) else None
    device_ext = device.get("ext") if isinstance(device, dict) else None
    present = isinstance(device_ext, dict) and "iaphistory" in device_ext
    value = device_ext.get("iaphistory") if present else None
    (folder / "in-app-purchase-history.json").write_text(json.dumps({
        "source": "ext.plaintext.device.ext.iaphistory",
        "actual": {
            "field_present": present,
            "product_count": len(value) if isinstance(value, list) else 0,
            "product_ids": value,
        },
        "note": (
            "Sample App 沒有購買流程或獨立 expected product IDs，"
            "因此合法陣列仍無法驗證內容正確性。"
        ),
    }, ensure_ascii=False, indent=2) + "\n")


def materialize_ios_boot_payload(folder):
    """Materialize decoded iOS power-on timestamps as payload-format Evidence."""
    folder = Path(folder)
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    plaintext = decoded.get("ext", {}).get("plaintext", {})
    device = plaintext.get("device") if isinstance(plaintext, dict) else None
    device_ext = device.get("ext") if isinstance(device, dict) else None
    value = device_ext.get("pot") if isinstance(device_ext, dict) else None
    (folder / "boot-timestamps.json").write_text(json.dumps({
        "source": "ext.plaintext.device.ext.pot",
        "actual": {
            "timestamp_count": len(value) if isinstance(value, list) else 0,
            "pot": value,
        },
        "note": "iOS 目前拿不到肉眼可見 Evidence；本 TC 使用解碼後 payload 驗證欄位格式。",
    }, ensure_ascii=False, indent=2) + "\n")


def materialize_ios_ram_payload(folder):
    """Materialize decoded iOS RAM fields as payload-only Evidence."""
    folder = Path(folder)
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    plaintext = decoded.get("ext", {}).get("plaintext", {})
    device = plaintext.get("device") if isinstance(plaintext, dict) else None
    device_ext = device.get("ext") if isinstance(device, dict) else None
    total = device_ext.get("mem_total") if isinstance(device_ext, dict) else None
    available = device_ext.get("mem_available") if isinstance(device_ext, dict) else None
    note = "iOS 目前拿不到肉眼可見 Evidence；本 TC 使用解碼後 payload 驗證欄位格式與數值關係。"
    documents = {
        "ram-total.json": {"source": "ext.plaintext.device.ext.mem_total", "actual": {"mem_total": total}, "note": note},
        "ram-available.json": {
            "source": "ext.plaintext.device.ext.mem_available",
            "actual": {"mem_available": available, "mem_total": total},
            "note": note,
        },
    }
    for name, document in documents.items():
        (folder / name).write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def materialize_ios_battery_visible(folder):
    """Preserve the visible Control Center battery percentage and screenshot."""
    folder = Path(folder)
    source = Path(os.environ.get("IOS_BATTERY_STATE_FILE", "/tmp/laf2-ios-battery-level.json"))
    screenshot = Path(os.environ.get("IOS_BATTERY_SCREENSHOT", "/tmp/laf2-ios-battery-level.png"))
    if source.is_file():
        shutil.copy2(source, folder / "ios-battery-level.json")
    else:
        (folder / "ios-battery-level.json").write_text(json.dumps({
            "status": "UNAVAILABLE",
            "source": "iOS Control Center",
            "reason": "Visible battery Evidence was not captured before the Bid.",
        }, ensure_ascii=False, indent=2) + "\n")
    if screenshot.is_file() and screenshot.stat().st_size:
        shutil.copy2(screenshot, folder / "ios-battery-level.png")


def materialize_ios_charging_visible(folder):
    """Preserve the independently visible Control Center charging state."""
    folder = Path(folder)
    source = Path(os.environ.get("IOS_CHARGING_STATE_FILE", "/tmp/laf2-ios-charging-status.json"))
    screenshot = Path(os.environ.get("IOS_CHARGING_SCREENSHOT", "/tmp/laf2-ios-charging-status.png"))
    if source.is_file():
        shutil.copy2(source, folder / "ios-charging-status.json")
    else:
        (folder / "ios-charging-status.json").write_text(json.dumps({
            "status": "UNAVAILABLE",
            "source": "iOS Control Center",
            "reason": "Visible charging-state Evidence was not captured before the Bid.",
        }, ensure_ascii=False, indent=2) + "\n")
    if screenshot.is_file() and screenshot.stat().st_size:
        shutil.copy2(screenshot, folder / "ios-charging-status.png")


def materialize_ios_low_power_visible(folder):
    """Preserve the independently visible native Low Power Mode switch."""
    folder = Path(folder)
    source = Path(os.environ.get("IOS_LOW_POWER_STATE_FILE", "/tmp/laf2-ios-low-power-mode.json"))
    screenshot = Path(os.environ.get("IOS_LOW_POWER_SCREENSHOT", "/tmp/laf2-ios-low-power-mode.png"))
    if source.is_file():
        shutil.copy2(source, folder / "ios-low-power-mode.json")
    else:
        (folder / "ios-low-power-mode.json").write_text(json.dumps({
            "status": "UNAVAILABLE",
            "source": "iOS Settings > Battery > Low Power Mode",
            "reason": "Visible Low Power Mode Evidence was not captured before the Bid.",
        }, ensure_ascii=False, indent=2) + "\n")
    if screenshot.is_file() and screenshot.stat().st_size:
        shutil.copy2(screenshot, folder / "ios-low-power-mode.png")


def _png_dimensions(path):
    try:
        header = Path(path).read_bytes()[:24]
    except OSError:
        return None
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", header[16:24])
    return {"width": width, "height": height}


def _write_html_screenshot(document, screenshot, width=1400, height=1000):
    """Render an Evidence card with the same headless-Chrome contract as AOS."""
    screenshot.unlink(missing_ok=True)
    chrome = os.environ.get("CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    with tempfile.TemporaryDirectory(prefix="laf2-ios-display-chrome-") as profile:
        process = subprocess.Popen(
            [
                chrome, "--headless=new", "--disable-gpu", "--disable-background-networking",
                "--hide-scrollbars", "--no-first-run", f"--user-data-dir={profile}",
                f"--window-size={width},{height}", f"--screenshot={screenshot}",
                document.resolve().as_uri(),
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
            time.sleep(.1)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    if not screenshot.exists() or screenshot.stat().st_size <= 1000:
        raise RuntimeError(f"iOS display Evidence screenshot was not created: {screenshot.name}")


def _display_card_result(key, info):
    logical = info.get("logical_points") or {}
    official = info.get("official_spec") or {}
    actual = info.get("actual") or {}
    request = actual.get("request") or {}
    extended = actual.get("extended") or {}
    ready = (
        info.get("status") == "CAPTURED"
        and str(info.get("orientation") or "").upper().startswith("PORTRAIT")
        and bool(official)
    )
    if not ready:
        return "BLOCKED"
    if key == "screen-width":
        passed = request.get("sw") == logical.get("width") and extended.get("sw") == official.get("native_width")
    elif key == "screen-height":
        passed = request.get("sh") == logical.get("height") and extended.get("sh") == official.get("native_height")
    elif key == "screen-ppi":
        req = request.get("ppi")
        expected = official.get("physical_ppi")
        passed = extended.get("ppi") == expected and (req is None or req == expected)
    else:
        values = (
            official.get("native_width"), logical.get("width"),
            official.get("native_height"), logical.get("height"),
        )
        if not all(type(value) in (int, float) and value > 0 for value in values):
            return "BLOCKED"
        width_ratio = values[0] / values[1]
        height_ratio = values[2] / values[3]
        passed = (
            abs(width_ratio - height_ratio) <= 1e-6
            and all(
                type(value) in (int, float) and abs(value - width_ratio) <= 1e-6
                for value in (request.get("pxratio"), extended.get("pxratio"))
            )
        )
    return "PASS" if passed else "FAILED"


def _display_evidence_document(key, info, source_image):
    logical = info.get("logical_points") or {}
    official = info.get("official_spec") or {}
    actual = info.get("actual") or {}
    request = actual.get("request") or {}
    extended = actual.get("extended") or {}
    screenshot_dimensions = info.get("screenshot_dimensions") or {}
    result = _display_card_result(key, info)
    color = {"PASS": "#287a3d", "FAILED": "#b9342b", "BLOCKED": "#a56516"}[result]
    titles = {
        "screen-width": "Screen Width",
        "screen-height": "Screen Height",
        "screen-ppi": "Screen PPI",
        "pixel-ratio": "Pixel Ratio",
    }
    if key == "screen-width":
        source_label = f'XCUITest {logical.get("width", "—")} pt · Apple {official.get("native_width", "—")} px'
        rows = (
            ("Captured logical points", logical.get("width")),
            ("Request device.sw", request.get("sw")),
            ("Apple native pixels", official.get("native_width")),
            ("Extended device.sw", extended.get("sw")),
        )
        marker = f'{logical.get("width", "—")} pt → {official.get("native_width", "—")} px · WIDTH'
        explanation = "Request width is compared with XCUITest logical points; Extended width is compared with Apple native pixels."
    elif key == "screen-height":
        source_label = f'XCUITest {logical.get("height", "—")} pt · Apple {official.get("native_height", "—")} px'
        rows = (
            ("Captured logical points", logical.get("height")),
            ("Request device.sh", request.get("sh")),
            ("Apple native pixels", official.get("native_height")),
            ("Extended device.sh", extended.get("sh")),
        )
        marker = f'{logical.get("height", "—")} pt → {official.get("native_height", "—")} px · HEIGHT'
        explanation = "Request height is compared with XCUITest logical points; Extended height is compared with Apple native pixels."
    elif key == "screen-ppi":
        source_label = f'Apple physical PPI = {official.get("physical_ppi", "—")}'
        rows = (
            ("Apple physical PPI", official.get("physical_ppi")),
            ("Request device.ppi", request.get("ppi")),
            ("Extended device.ppi", extended.get("ppi")),
            ("Specification", official.get("source")),
        )
        marker = f'{official.get("native_width", "—")} × {official.get("native_height", "—")} px · {official.get("physical_ppi", "—")} PPI'
        explanation = "iOS device.ppi is physical panel PPI and is compared directly with the Apple specification mapped from ProductType."
    else:
        width_ratio = (
            official.get("native_width") / logical.get("width")
            if type(official.get("native_width")) in (int, float) and type(logical.get("width")) in (int, float) and logical.get("width") else None
        )
        height_ratio = (
            official.get("native_height") / logical.get("height")
            if type(official.get("native_height")) in (int, float) and type(logical.get("height")) in (int, float) and logical.get("height") else None
        )
        source_label = f'{official.get("native_width", "—")} ÷ {logical.get("width", "—")} = {width_ratio if width_ratio is not None else "—"}'
        rows = (
            ("Width formula", f'{official.get("native_width", "—")} ÷ {logical.get("width", "—")} = {width_ratio if width_ratio is not None else "—"}'),
            ("Height formula", f'{official.get("native_height", "—")} ÷ {logical.get("height", "—")} = {height_ratio if height_ratio is not None else "—"}'),
            ("Request device.pxratio", request.get("pxratio")),
            ("Extended device.pxratio", extended.get("pxratio")),
        )
        marker = f'NATIVE PIXELS ÷ LOGICAL POINTS = {width_ratio if width_ratio is not None else "—"}'
        explanation = "Like Android density ÷ 160, the iOS expected ratio is derived from two independent display dimensions."
    encoded = base64.b64encode(Path(source_image).read_bytes()).decode()
    row_html = "".join(
        f'<div class="row"><span>{html.escape(label)}</span><b>{html.escape("—" if value is None else str(value))}</b></div>'
        for label, value in rows
    )
    support = f'{screenshot_dimensions.get("width", "—")} × {screenshot_dimensions.get("height", "—")} px · supporting only'
    model = official.get("model") or info.get("product_type") or "Unmapped device"
    spec_url = official.get("url") or "—"
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{width:1400px;height:1000px;padding:38px 62px}}.eyebrow{{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}}h1{{font-size:38px;margin:7px 0 16px}}.content{{display:grid;grid-template-columns:430px 1fr;gap:38px}}.phone{{height:690px;display:flex;justify-content:center;position:relative;overflow:hidden;background:#dfe5f5;border-radius:24px;padding:22px}}.phone img{{height:646px;width:auto;border-radius:13px;box-shadow:0 12px 28px #17233335}}.dimension{{position:absolute;z-index:2;left:20px;right:20px;top:13px;text-align:center;background:#0e7c86;color:white;border-radius:20px;padding:6px;font:700 18px ui-monospace,monospace}}.panel{{padding-top:18px}}.source{{padding:22px 26px;background:#14202a;color:#8ee0e6;border-radius:17px;font:700 23px ui-monospace,monospace}}.note{{font-size:17px;line-height:1.45;color:#526571;margin:17px 3px 25px}}.rows{{background:white;border-radius:18px;padding:10px 25px}}.row{{display:grid;grid-template-columns:235px 1fr;gap:18px;padding:16px 0;border-bottom:1px solid #e3e9ed}}.row:last-child{{border:0}}.row span{{color:#60717c}}.row b{{font:700 17px ui-monospace,monospace;overflow-wrap:anywhere}}.conclusion{{display:flex;justify-content:space-between;align-items:center;margin-top:22px;padding:20px 26px;background:white;border-radius:16px;border-left:8px solid {color}}}.conclusion b{{font-size:28px;color:{color}}}.meta{{font-size:14px;color:#6c7b85;margin-top:14px}}</style></head><body><main>
<div class="eyebrow">DIRECT SCREEN EVIDENCE · iOS</div><h1>{titles[key]}</h1><div class="content"><div class="phone"><div class="dimension">{html.escape(marker)}</div><img src="data:image/png;base64,{encoded}"></div><div class="panel"><div class="source">{html.escape(source_label)}</div><p class="note">{html.escape(explanation)}</p><div class="rows"><div class="row"><span>Device</span><b>{html.escape(str(model))} · {html.escape(str(info.get("product_type") or "—"))}</b></div>{row_html}<div class="row"><span>Visible screenshot</span><b>{html.escape(support)}</b></div></div><div class="conclusion"><span>Compare independent source with SDK answer</span><b>{result}</b></div><div class="meta">Apple source: {html.escape(str(spec_url))}</div></div></div></main></body></html>'''


def _render_ios_display_evidence(folder, info, source_image):
    for key in ("screen-width", "screen-height", "screen-ppi", "pixel-ratio"):
        document = Path(folder) / f"{key}-evidence.html"
        screenshot = Path(folder) / f"{key}-evidence.png"
        document.write_text(_display_evidence_document(key, info, source_image), encoding="utf-8")
        _write_html_screenshot(document, screenshot)


def materialize_ios_display_status(folder):
    """Join capture-time iOS display sources with payload values and render AOS-style cards."""
    folder = Path(folder)
    source_state = Path(os.environ.get("IOS_DISPLAY_STATE_FILE", "/tmp/laf2-ios-display-status.json"))
    source_screenshot = Path(os.environ.get("IOS_DISPLAY_SCREENSHOT", "/tmp/laf2-ios-display-source.png"))
    if source_state.is_file():
        info = json.loads(source_state.read_text())
    else:
        info = {
            "status": "UNAVAILABLE",
            "reason": "Independent iOS display state was not captured before the Bid.",
        }
    product_type = info.get("product_type")
    official = IOS_OFFICIAL_DISPLAY_SPECS.get(product_type)
    info["official_spec"] = official
    if info.get("status") == "CAPTURED" and official is None:
        info["reason"] = f"ProductType {product_type or 'unknown'} is not mapped to an Apple display specification."
    decoded = json.loads((folder / "bid_decoded.json").read_text())
    req = decoded.get("req", {}).get("plaintext", {})
    ext = decoded.get("ext", {}).get("plaintext", {})
    req_device = req.get("device", {}) if isinstance(req, dict) else {}
    ext_device = ext.get("device", {}) if isinstance(ext, dict) else {}
    fields = ("sw", "sh", "ppi", "pxratio")
    info["actual"] = {
        "request": {field: req_device.get(field) for field in fields},
        "extended": {field: ext_device.get(field) for field in fields},
    }
    target_screenshot = folder / "ios-display-source.png"
    if source_screenshot.is_file() and source_screenshot.stat().st_size:
        shutil.copy2(source_screenshot, target_screenshot)
        info["screenshot_dimensions"] = _png_dimensions(target_screenshot)
    else:
        info["status"] = "UNAVAILABLE"
        info["reason"] = "Visible iOS display source screenshot was not captured before the Bid."
    (folder / "ios-display-status.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    if target_screenshot.is_file():
        _render_ios_display_evidence(folder, info, target_screenshot)


def _device_make_card_result(info):
    if info.get("status") != "CAPTURED" or info.get("official_make") != "Apple":
        return "BLOCKED"
    actual = info.get("actual") or {}
    req, ext = actual.get("request_make"), actual.get("extended_make")
    passed = ext == "Apple" and (req is None or req == "Apple")
    return "PASS" if passed else "FAILED"


def _device_model_card_result(info):
    if info.get("status") != "CAPTURED":
        return "BLOCKED"
    actual = info.get("actual") or {}
    official = info.get("official_spec") or {}
    model, hwv = official.get("model"), info.get("product_type")
    passed = (
        actual.get("extended_model") == model
        and (actual.get("request_model") is None or actual.get("request_model") == model)
        and actual.get("extended_hwv") == hwv
        and (actual.get("request_hwv") is None or actual.get("request_hwv") == hwv)
    )
    return "PASS" if passed else "FAILED"


def _device_make_evidence_document(info, source_image, kind="make"):
    actual = info.get("actual") or {}
    official = info.get("official_spec") or {}
    result = _device_make_card_result(info) if kind == "make" else _device_model_card_result(info)
    color = {"PASS": "#287a3d", "FAILED": "#b9342b", "BLOCKED": "#a56516"}[result]
    if kind == "make":
        title = "Device Make"
        note = "Native About supplies the visible model, while ProductType is mapped to an Apple-hosted official specification. The payload manufacturer must be exactly Apple."
        rows = (
            ("Visible About Model Name", info.get("visible_model_name")),
            ("ideviceinfo ProductType", info.get("product_type")),
            ("Apple official model", official.get("model")),
            ("Official manufacturer", info.get("official_make")),
            ("Request device.make", actual.get("request_make")),
            ("Extended device.make", actual.get("extended_make")),
        )
    else:
        title = "Device Model"
        note = "The visible About Model Name must equal the Apple official model. device.hwv must equal the independently captured ProductType."
        rows = (
            ("Visible About Model Name", info.get("visible_model_name")),
            ("Apple official model", official.get("model")),
            ("ideviceinfo ProductType", info.get("product_type")),
            ("Request device.model", actual.get("request_model")),
            ("Extended device.model", actual.get("extended_model")),
            ("Request / Extended device.hwv", f'{actual.get("request_hwv")} / {actual.get("extended_hwv")}'),
        )
    encoded = base64.b64encode(Path(source_image).read_bytes()).decode()
    row_html = "".join(
        f'<div class="row"><span>{html.escape(label)}</span><b>{html.escape("—" if value is None else str(value))}</b></div>'
        for label, value in rows
    )
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{width:1400px;height:1000px;padding:38px 62px}}.eyebrow{{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}}h1{{font-size:38px;margin:7px 0 16px}}.content{{display:grid;grid-template-columns:430px 1fr;gap:38px}}.phone{{height:690px;display:flex;justify-content:center;position:relative;overflow:hidden;background:#dfe5f5;border-radius:24px;padding:22px}}.phone img{{height:646px;width:auto;border-radius:13px;box-shadow:0 12px 28px #17233335}}.marker{{position:absolute;z-index:2;left:20px;right:20px;top:13px;text-align:center;background:#0e7c86;color:white;border-radius:20px;padding:6px;font:700 18px ui-monospace,monospace}}.panel{{padding-top:15px}}.source{{padding:22px 26px;background:#14202a;color:#8ee0e6;border-radius:17px;font:700 23px ui-monospace,monospace}}.note{{font-size:17px;line-height:1.45;color:#526571;margin:17px 3px 22px}}.rows{{background:white;border-radius:18px;padding:8px 25px}}.row{{display:grid;grid-template-columns:260px 1fr;gap:18px;padding:13px 0;border-bottom:1px solid #e3e9ed}}.row:last-child{{border:0}}.row span{{color:#60717c}}.row b{{font:700 16px ui-monospace,monospace;overflow-wrap:anywhere}}.conclusion{{display:flex;justify-content:space-between;align-items:center;margin-top:20px;padding:19px 25px;background:white;border-radius:16px;border-left:8px solid {color}}}.conclusion b{{font-size:28px;color:{color}}}.meta{{font-size:14px;color:#6c7b85;margin-top:12px}}</style></head><body><main>
<div class="eyebrow">DIRECT DEVICE IDENTITY EVIDENCE · iOS</div><h1>{title}</h1><div class="content"><div class="phone"><div class="marker">NATIVE SETTINGS · ABOUT</div><img src="data:image/png;base64,{encoded}"></div><div class="panel"><div class="source">{html.escape(str(info.get("product_type") or "—"))} → Apple {html.escape(str(official.get("model") or "—"))}</div><p class="note">{html.escape(note)}</p><div class="rows">{row_html}</div><div class="conclusion"><span>Compare independent identity with SDK answer</span><b>{result}</b></div><div class="meta">Apple source: {html.escape(str(official.get("url") or "—"))}</div></div></div></main></body></html>'''


def materialize_ios_device_identity(folder):
    folder = Path(folder)
    source_state = Path(os.environ.get("IOS_DISPLAY_STATE_FILE", "/tmp/laf2-ios-display-status.json"))
    source_image = Path(os.environ.get("IOS_DISPLAY_SCREENSHOT", "/tmp/laf2-ios-display-source.png"))
    info = json.loads(source_state.read_text()) if source_state.is_file() else {
        "status": "UNAVAILABLE", "reason": "Native iOS About identity was not captured before the Bid.",
    }
    official = IOS_OFFICIAL_DISPLAY_SPECS.get(info.get("product_type"))
    info["official_spec"] = official
    info["official_make"] = "Apple" if official else None
    req_make, ext_make = _decoded_device_values(folder, "make")
    req_model, ext_model = _decoded_device_values(folder, "model")
    req_hwv, ext_hwv = _decoded_device_values(folder, "hwv")
    info["actual"] = {
        "request_make": req_make, "extended_make": ext_make,
        "request_model": req_model, "extended_model": ext_model,
        "request_hwv": req_hwv, "extended_hwv": ext_hwv,
    }
    if info.get("status") == "CAPTURED":
        if info.get("visual_source") != "native Settings > General > About":
            info["status"] = "UNAVAILABLE"
            info["reason"] = "Native Settings > General > About was not visibly captured."
        elif not info.get("visible_model_name"):
            info["status"] = "UNAVAILABLE"
            info["reason"] = "Native About does not expose one readable Model Name in the accessibility tree."
        elif official is None:
            info["status"] = "UNAVAILABLE"
            info["reason"] = f'ProductType {info.get("product_type") or "unknown"} is not mapped to an Apple specification.'
        elif info.get("visible_model_name") != official.get("model"):
            info["status"] = "UNAVAILABLE"
            info["reason"] = "The visible About Model Name does not match the Apple ProductType mapping."
    target_image = folder / "ios-device-about.png"
    if source_image.is_file() and source_image.stat().st_size:
        shutil.copy2(source_image, target_image)
    else:
        info["status"] = "UNAVAILABLE"
        info["reason"] = "Native iOS About screenshot was not captured before the Bid."
    (folder / "ios-device-identity-status.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    if target_image.is_file():
        document = folder / "device-make-evidence.html"
        card = folder / "device-make-evidence.png"
        document.write_text(_device_make_evidence_document(info, target_image), encoding="utf-8")
        _write_html_screenshot(document, card)
        model_document = folder / "device-model-evidence.html"
        model_card = folder / "device-model-evidence.png"
        model_document.write_text(_device_make_evidence_document(info, target_image, "model"), encoding="utf-8")
        _write_html_screenshot(model_document, model_card)


def _decoded_device_ext_values(folder, field):
    decoded = json.loads((Path(folder) / "bid_decoded.json").read_text())
    values = []
    for envelope in ("req", "ext"):
        plaintext = decoded.get(envelope, {}).get("plaintext", {})
        device = plaintext.get("device") if isinstance(plaintext, dict) else None
        device_ext = device.get("ext") if isinstance(device, dict) else None
        values.append(device_ext.get(field) if isinstance(device_ext, dict) else None)
    return values


def _settings_slider_card_result(kind, info):
    actual = info.get("actual") or {}
    req = actual.get("request")
    ext = actual.get("extended")
    if info.get("status") != "CAPTURED":
        return "BLOCKED"
    if kind == "brightness":
        if info.get("slider_visible_in_screenshot") is not True:
            return "BLOCKED"
        expected = info.get("normalized_brightness")
        valid = type(expected) in (int, float) and 0 <= expected <= 1
        passed = (
            valid and type(ext) in (int, float) and 0 <= ext <= 1
            and abs(ext - expected) <= .01
            and (req is None or (type(req) in (int, float) and abs(req - expected) <= .01))
        )
        return "PASS" if passed else "FAILED"
    values = [value for value in (req, ext) if value is not None]
    valid = (
        bool(values)
        and all(type(value) in (int, float) and value > 0 for value in values)
        and (req is None or ext is None or req == ext)
    )
    return "BLOCKED" if valid else "FAILED"


def _settings_slider_evidence_document(kind, info, source_image):
    actual = info.get("actual") or {}
    result = _settings_slider_card_result(kind, info)
    color = {"PASS": "#287a3d", "FAILED": "#b9342b", "BLOCKED": "#a56516"}[result]
    if kind == "brightness":
        title = "Screen Brightness"
        marker = f'{info.get("visible_percent", "—")}% VISIBLE BRIGHTNESS'
        source = f'Native slider {info.get("slider_accessibility_value") or "—"} → {info.get("normalized_brightness", "—")}'
        rows = (
            ("Visible slider", info.get("slider_accessibility_value")),
            ("Normalized expected", info.get("normalized_brightness")),
            ("Request device.ext.screen_bright", actual.get("request")),
            ("Extended device.ext.screen_bright", actual.get("extended")),
        )
        explanation = (
            "The native accessibility percentage is normalized to 0...1 and compared with the same-round payload within 0.01."
        )
    else:
        title = "Font Scale"
        slider = info.get("slider_accessibility_value")
        marker = f'LARGER TEXT · SLIDER {slider or "VISIBLE"}'
        source = "Native Larger Text state · numeric bridge unavailable"
        rows = (
            ("Visible slider", slider),
            ("Slider position (visual only)", info.get("slider_position")),
            ("Request device.ext.fontscale", actual.get("request")),
            ("Extended device.ext.fontscale", actual.get("extended")),
        )
        explanation = (
            "The screenshot proves the selected Dynamic Type state. Slider position is not treated as the exact payload multiplier, so a valid value remains BLOCKED."
        )
    encoded = base64.b64encode(Path(source_image).read_bytes()).decode()
    row_html = "".join(
        f'<div class="row"><span>{html.escape(label)}</span><b>{html.escape("—" if value is None else str(value))}</b></div>'
        for label, value in rows
    )
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{width:1400px;height:1000px;padding:38px 62px}}.eyebrow{{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}}h1{{font-size:38px;margin:7px 0 16px}}.content{{display:grid;grid-template-columns:430px 1fr;gap:38px}}.phone{{height:690px;display:flex;justify-content:center;position:relative;overflow:hidden;background:#dfe5f5;border-radius:24px;padding:22px}}.phone img{{height:646px;width:auto;border-radius:13px;box-shadow:0 12px 28px #17233335}}.marker{{position:absolute;z-index:2;left:20px;right:20px;top:13px;text-align:center;background:#0e7c86;color:white;border-radius:20px;padding:6px;font:700 18px ui-monospace,monospace}}.panel{{padding-top:18px}}.source{{padding:22px 26px;background:#14202a;color:#8ee0e6;border-radius:17px;font:700 23px ui-monospace,monospace}}.note{{font-size:17px;line-height:1.45;color:#526571;margin:17px 3px 25px}}.rows{{background:white;border-radius:18px;padding:10px 25px}}.row{{display:grid;grid-template-columns:285px 1fr;gap:18px;padding:16px 0;border-bottom:1px solid #e3e9ed}}.row:last-child{{border:0}}.row span{{color:#60717c}}.row b{{font:700 17px ui-monospace,monospace;overflow-wrap:anywhere}}.conclusion{{display:flex;justify-content:space-between;align-items:center;margin-top:22px;padding:20px 26px;background:white;border-radius:16px;border-left:8px solid {color}}}.conclusion b{{font-size:28px;color:{color}}}.meta{{font-size:14px;color:#6c7b85;margin-top:14px}}</style></head><body><main>
<div class="eyebrow">DIRECT SETTINGS EVIDENCE · iOS</div><h1>{title}</h1><div class="content"><div class="phone"><div class="marker">{html.escape(marker)}</div><img src="data:image/png;base64,{encoded}"></div><div class="panel"><div class="source">{html.escape(source)}</div><p class="note">{html.escape(explanation)}</p><div class="rows">{row_html}</div><div class="conclusion"><span>Compare independent source with SDK answer</span><b>{result}</b></div><div class="meta">Source: {html.escape(str(info.get("source") or "native iOS Settings"))}</div></div></div></main></body></html>'''


def _materialize_ios_settings_slider(folder, kind):
    folder = Path(folder)
    if kind == "brightness":
        state_env, state_default = "IOS_BRIGHTNESS_STATE_FILE", "/tmp/laf2-ios-brightness-status.json"
        image_env, image_default = "IOS_BRIGHTNESS_SCREENSHOT", "/tmp/laf2-ios-brightness-settings.png"
        status_name, image_name, card_name, field = (
            "ios-brightness-status.json", "ios-brightness-settings.png", "screen-brightness-evidence", "screen_bright",
        )
        missing_reason = "Visible iOS brightness Evidence was not captured before the Bid."
    else:
        state_env, state_default = "IOS_FONT_SIZE_STATE_FILE", "/tmp/laf2-ios-font-size-status.json"
        image_env, image_default = "IOS_FONT_SIZE_SCREENSHOT", "/tmp/laf2-ios-font-size-settings.png"
        status_name, image_name, card_name, field = (
            "ios-font-size-status.json", "ios-font-size-settings.png", "font-scale-evidence", "fontscale",
        )
        missing_reason = "Visible iOS Larger Text Evidence was not captured before the Bid."
    source_state = Path(os.environ.get(state_env, state_default))
    source_image = Path(os.environ.get(image_env, image_default))
    info = json.loads(source_state.read_text()) if source_state.is_file() else {
        "status": "UNAVAILABLE", "reason": missing_reason,
    }
    req, ext = _decoded_device_ext_values(folder, field)
    info["actual"] = {"request": req, "extended": ext}
    target_image = folder / image_name
    if source_image.is_file() and source_image.stat().st_size:
        shutil.copy2(source_image, target_image)
    else:
        info["status"] = "UNAVAILABLE"
        info["reason"] = missing_reason
    (folder / status_name).write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    if target_image.is_file():
        document = folder / f"{card_name}.html"
        card = folder / f"{card_name}.png"
        document.write_text(_settings_slider_evidence_document(kind, info, target_image), encoding="utf-8")
        _write_html_screenshot(document, card)


def materialize_ios_brightness_visible(folder):
    _materialize_ios_settings_slider(folder, "brightness")


def materialize_ios_font_size_visible(folder):
    _materialize_ios_settings_slider(folder, "font-size")


def _output_volume_card_result(info):
    if info.get("status") != "CAPTURED":
        return "BLOCKED"
    expected = info.get("normalized_volume")
    actual = info.get("actual") or {}
    req, ext = actual.get("request"), actual.get("extended")
    passed = (
        type(expected) in (int, float) and 0 <= expected <= 1
        and type(ext) in (int, float) and 0 <= ext <= 1 and abs(ext - expected) <= .01
        and (req is None or (
            type(req) in (int, float) and 0 <= req <= 1 and abs(req - expected) <= .01
        ))
    )
    return "PASS" if passed else "FAILED"


def _output_volume_evidence_document(info, source_image):
    actual = info.get("actual") or {}
    result = _output_volume_card_result(info)
    color = {"PASS": "#287a3d", "FAILED": "#b9342b", "BLOCKED": "#a56516"}[result]
    rows = (
        ("Visible media-volume slider", info.get("accessibility_text")),
        ("Visible percentage", info.get("visible_percent")),
        ("Normalized expected", info.get("normalized_volume")),
        ("Request device.ext.volume", actual.get("request")),
        ("Extended device.ext.volume", actual.get("extended")),
    )
    encoded = base64.b64encode(Path(source_image).read_bytes()).decode()
    row_html = "".join(
        f'<div class="row"><span>{html.escape(label)}</span><b>{html.escape("—" if value is None else str(value))}</b></div>'
        for label, value in rows
    )
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{width:1400px;height:1000px;padding:38px 62px}}.eyebrow{{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}}h1{{font-size:38px;margin:7px 0 16px}}.content{{display:grid;grid-template-columns:430px 1fr;gap:38px}}.phone{{height:690px;display:flex;justify-content:center;position:relative;overflow:hidden;background:#dfe5f5;border-radius:24px;padding:22px}}.phone img{{height:646px;width:auto;border-radius:13px;box-shadow:0 12px 28px #17233335}}.marker{{position:absolute;z-index:2;left:20px;right:20px;top:13px;text-align:center;background:#0e7c86;color:white;border-radius:20px;padding:6px;font:700 18px ui-monospace,monospace}}.panel{{padding-top:18px}}.source{{padding:22px 26px;background:#14202a;color:#8ee0e6;border-radius:17px;font:700 23px ui-monospace,monospace}}.note{{font-size:17px;line-height:1.45;color:#526571;margin:17px 3px 25px}}.rows{{background:white;border-radius:18px;padding:10px 25px}}.row{{display:grid;grid-template-columns:285px 1fr;gap:18px;padding:14px 0;border-bottom:1px solid #e3e9ed}}.row:last-child{{border:0}}.row span{{color:#60717c}}.row b{{font:700 17px ui-monospace,monospace;overflow-wrap:anywhere}}.conclusion{{display:flex;justify-content:space-between;align-items:center;margin-top:22px;padding:20px 26px;background:white;border-radius:16px;border-left:8px solid {color}}}.conclusion b{{font-size:28px;color:{color}}}</style></head><body><main>
<div class="eyebrow">DIRECT CONTROL CENTER EVIDENCE · iOS</div><h1>Output Volume</h1><div class="content"><div class="phone"><div class="marker">{html.escape(str(info.get("visible_percent") if info.get("visible_percent") is not None else "—"))}% MEDIA VOLUME</div><img src="data:image/png;base64,{encoded}"></div><div class="panel"><div class="source">Visible percentage ÷ 100 = {html.escape(str(info.get("normalized_volume") if info.get("normalized_volume") is not None else "—"))}</div><p class="note">The Control Center media-volume slider is read without mutation and compared with the same-round payload within 0.01.</p><div class="rows">{row_html}</div><div class="conclusion"><span>Compare independent source with SDK answer</span><b>{result}</b></div></div></div></main></body></html>'''


def materialize_ios_output_volume_visible(folder):
    folder = Path(folder)
    source_state = Path(os.environ.get("IOS_OUTPUT_VOLUME_STATE_FILE", "/tmp/laf2-ios-output-volume-status.json"))
    source_image = Path(os.environ.get("IOS_OUTPUT_VOLUME_SCREENSHOT", "/tmp/laf2-ios-output-volume-control-center.png"))
    info = json.loads(source_state.read_text()) if source_state.is_file() else {
        "status": "UNAVAILABLE", "reason": "Visible iOS output-volume Evidence was not captured before the Bid.",
    }
    req, ext = _decoded_device_ext_values(folder, "volume")
    info["actual"] = {"request": req, "extended": ext}
    target_image = folder / "ios-output-volume-control-center.png"
    if source_image.is_file() and source_image.stat().st_size:
        shutil.copy2(source_image, target_image)
    else:
        info["status"] = "UNAVAILABLE"
        info["reason"] = "Visible iOS Control Center media-volume screenshot was not captured before the Bid."
    (folder / "ios-output-volume-status.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    if target_image.is_file():
        document = folder / "output-volume-evidence.html"
        card = folder / "output-volume-evidence.png"
        document.write_text(_output_volume_evidence_document(info, target_image), encoding="utf-8")
        _write_html_screenshot(document, card)


def _dark_mode_card_result(info):
    actual = info.get("actual") or {}
    req = actual.get("request")
    ext = actual.get("extended")
    expected = info.get("dark_mode")
    if info.get("status") != "CAPTURED" or type(expected) is not bool:
        return "BLOCKED"
    passed = type(ext) is bool and ext is expected and (
        req is None or (type(req) is bool and req is expected)
    )
    return "PASS" if passed else "FAILED"


def _dark_mode_evidence_document(info, source_image):
    actual = info.get("actual") or {}
    controls = info.get("appearance_controls") or {}
    result = _dark_mode_card_result(info)
    color = {"PASS": "#287a3d", "FAILED": "#b9342b", "BLOCKED": "#a56516"}[result]
    selected = info.get("selected_appearance") or "UNRESOLVED"
    rows = (
        ("Visible selected appearance", info.get("selected_appearance")),
        ("Independent dark-mode boolean", info.get("dark_mode")),
        ("Light control selected", (controls.get("Light") or {}).get("selected")),
        ("Dark control selected", (controls.get("Dark") or {}).get("selected")),
        ("Request device.ext.darkmode", actual.get("request")),
        ("Extended device.ext.darkmode", actual.get("extended")),
    )
    encoded = base64.b64encode(Path(source_image).read_bytes()).decode()
    row_html = "".join(
        f'<div class="row"><span>{html.escape(label)}</span><b>{html.escape("—" if value is None else str(value))}</b></div>'
        for label, value in rows
    )
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{width:1400px;height:1000px;padding:38px 62px}}.eyebrow{{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}}h1{{font-size:38px;margin:7px 0 16px}}.content{{display:grid;grid-template-columns:430px 1fr;gap:38px}}.phone{{height:690px;display:flex;justify-content:center;position:relative;overflow:hidden;background:#dfe5f5;border-radius:24px;padding:22px}}.phone img{{height:646px;width:auto;border-radius:13px;box-shadow:0 12px 28px #17233335}}.marker{{position:absolute;z-index:2;left:20px;right:20px;top:13px;text-align:center;background:#0e7c86;color:white;border-radius:20px;padding:6px;font:700 18px ui-monospace,monospace}}.panel{{padding-top:18px}}.source{{padding:22px 26px;background:#14202a;color:#8ee0e6;border-radius:17px;font:700 23px ui-monospace,monospace}}.note{{font-size:17px;line-height:1.45;color:#526571;margin:17px 3px 25px}}.rows{{background:white;border-radius:18px;padding:10px 25px}}.row{{display:grid;grid-template-columns:285px 1fr;gap:18px;padding:13px 0;border-bottom:1px solid #e3e9ed}}.row:last-child{{border:0}}.row span{{color:#60717c}}.row b{{font:700 17px ui-monospace,monospace;overflow-wrap:anywhere}}.conclusion{{display:flex;justify-content:space-between;align-items:center;margin-top:22px;padding:20px 26px;background:white;border-radius:16px;border-left:8px solid {color}}}.conclusion b{{font-size:28px;color:{color}}}.meta{{font-size:14px;color:#6c7b85;margin-top:14px}}</style></head><body><main>
<div class="eyebrow">DIRECT SETTINGS EVIDENCE · iOS</div><h1>Dark Mode</h1><div class="content"><div class="phone"><div class="marker">{html.escape(selected)} APPEARANCE SELECTED</div><img src="data:image/png;base64,{encoded}"></div><div class="panel"><div class="source">Visible {html.escape(selected)} → darkmode={html.escape(str(info.get("dark_mode") if info.get("dark_mode") is not None else "—"))}</div><p class="note">The selected native Light/Dark appearance is the independent source. The same-round payload must contain the identical boolean.</p><div class="rows">{row_html}</div><div class="conclusion"><span>Compare independent source with SDK answer</span><b>{result}</b></div><div class="meta">Source: {html.escape(str(info.get("source") or "iOS Settings > Display & Brightness"))}</div></div></div></main></body></html>'''


def materialize_ios_dark_mode_visible(folder):
    folder = Path(folder)
    source_state = Path(os.environ.get("IOS_DARK_MODE_STATE_FILE", "/tmp/laf2-ios-dark-mode-status.json"))
    source_image = Path(os.environ.get("IOS_DARK_MODE_SCREENSHOT", "/tmp/laf2-ios-dark-mode-settings.png"))
    info = json.loads(source_state.read_text()) if source_state.is_file() else {
        "status": "UNAVAILABLE",
        "reason": "Visible iOS Light/Dark appearance Evidence was not captured before the Bid.",
    }
    req, ext = _decoded_device_ext_values(folder, "darkmode")
    info["actual"] = {"request": req, "extended": ext}
    target_image = folder / "ios-dark-mode-settings.png"
    if source_image.is_file() and source_image.stat().st_size:
        shutil.copy2(source_image, target_image)
    else:
        info["status"] = "UNAVAILABLE"
        info["reason"] = "Visible iOS Light/Dark appearance Evidence was not captured before the Bid."
    (folder / "ios-dark-mode-status.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    if target_image.is_file():
        document = folder / "dark-mode-evidence.html"
        card = folder / "dark-mode-evidence.png"
        document.write_text(_dark_mode_evidence_document(info, target_image), encoding="utf-8")
        _write_html_screenshot(document, card)


IOS_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
IOS_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def _decoded_device_values(folder, field):
    decoded = json.loads((Path(folder) / "bid_decoded.json").read_text())
    values = []
    for envelope in ("req", "ext"):
        plaintext = decoded.get(envelope, {}).get("plaintext", {})
        device = plaintext.get("device") if isinstance(plaintext, dict) else None
        values.append(device.get(field) if isinstance(device, dict) and field in device else None)
    return values


def _valid_nonzero_ios_uuid(value):
    return isinstance(value, str) and value.lower() != IOS_ZERO_UUID and bool(IOS_UUID_RE.fullmatch(value))


def _tracking_allowed_card_result(info):
    if info.get("status") != "CAPTURED" or not info.get("screenshot_saved"):
        return "BLOCKED"
    if str((info.get("att") or {}).get("authorization") or "").lower() != "authorized":
        return "BLOCKED"
    visible_idfa = info.get("visible_idfa")
    if info.get("visible_idfa_status") != "CAPTURED" or not _valid_nonzero_ios_uuid(visible_idfa):
        return "BLOCKED"
    actual = info.get("actual") or {}
    req_ia, ext_ia = actual.get("request_idfa"), actual.get("extended_idfa")
    req_lat, ext_lat = actual.get("request_lat"), actual.get("extended_lat")
    lat_allowed = lambda value: value is None or (type(value) is int and value == 0)
    passed = (
        _valid_nonzero_ios_uuid(req_ia) and _valid_nonzero_ios_uuid(ext_ia)
        and req_ia.lower() == visible_idfa.lower() and ext_ia.lower() == visible_idfa.lower()
        and lat_allowed(req_lat) and lat_allowed(ext_lat)
    )
    return "PASS" if passed else "FAILED"


def _tracking_allowed_evidence_document(info, source_image):
    actual = info.get("actual") or {}
    app_switch = info.get("app_switch") or {}
    result = _tracking_allowed_card_result(info)
    color = {"PASS": "#287a3d", "FAILED": "#b9342b", "BLOCKED": "#a56516"}[result]
    rows = (
        ("Visible Sample App switch", f'{app_switch.get("name") or "—"} = {app_switch.get("value") or "—"}'),
        ("ATT authorization", (info.get("att") or {}).get("authorization")),
        ("Visible non-zero IDFA", info.get("visible_idfa")),
        ("Request device.ia", actual.get("request_idfa")),
        ("Extended device.ia", actual.get("extended_idfa")),
        ("Request device.lat", "ABSENT" if actual.get("request_lat") is None else actual.get("request_lat")),
        ("Extended device.lat", "ABSENT" if actual.get("extended_lat") is None else actual.get("extended_lat")),
    )
    encoded = base64.b64encode(Path(source_image).read_bytes()).decode()
    row_html = "".join(
        f'<div class="row"><span>{html.escape(label)}</span><b>{html.escape(str(value))}</b></div>'
        for label, value in rows
    )
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{width:1400px;height:1000px;padding:38px 62px}}.eyebrow{{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}}h1{{font-size:38px;margin:7px 0 16px}}.content{{display:grid;grid-template-columns:430px 1fr;gap:38px}}.phone{{height:690px;display:flex;justify-content:center;position:relative;overflow:hidden;background:#dfe5f5;border-radius:24px;padding:22px}}.phone img{{height:646px;width:auto;border-radius:13px;box-shadow:0 12px 28px #17233335}}.marker{{position:absolute;z-index:2;left:20px;right:20px;top:13px;text-align:center;background:#0e7c86;color:white;border-radius:20px;padding:6px;font:700 18px ui-monospace,monospace}}.panel{{padding-top:10px}}.source{{padding:20px 24px;background:#14202a;color:#8ee0e6;border-radius:17px;font:700 22px ui-monospace,monospace}}.note{{font-size:16px;line-height:1.45;color:#526571;margin:14px 3px 18px}}.rows{{background:white;border-radius:18px;padding:8px 25px}}.row{{display:grid;grid-template-columns:245px 1fr;gap:18px;padding:11px 0;border-bottom:1px solid #e3e9ed}}.row:last-child{{border:0}}.row span{{color:#60717c}}.row b{{font:700 15px ui-monospace,monospace;overflow-wrap:anywhere}}.conclusion{{display:flex;justify-content:space-between;align-items:center;margin-top:18px;padding:18px 24px;background:white;border-radius:16px;border-left:8px solid {color}}}.conclusion b{{font-size:28px;color:{color}}}</style></head><body><main>
<div class="eyebrow">DIRECT SETTINGS EVIDENCE · iOS</div><h1>Advertising Tracking Allowed</h1><div class="content"><div class="phone"><div class="marker">SAMPLE APP TRACKING SWITCH</div><img src="data:image/png;base64,{encoded}"></div><div class="panel"><div class="source">ATT authorized · LAT inverse flag</div><p class="note">The visible Sample App tracking switch and visible non-zero IDFA independently establish the allowed state. Request and Extended LAT must each be integer 0 or absent.</p><div class="rows">{row_html}</div><div class="conclusion"><span>Compare independent sources with SDK answer</span><b>{result}</b></div></div></div></main></body></html>'''


def materialize_ios_tracking_allowed(folder):
    folder = Path(folder)
    state_source = Path(os.environ.get("IOS_SETTINGS_STATE_FILE", "/tmp/laf2-ios-settings-state.json"))
    settings_source = Path(os.environ.get("IOS_SETTINGS_SCREENSHOT", "/tmp/laf2-ios-settings-state.png"))
    idfa_state_source = Path(os.environ.get("IOS_IDFA_STATE_FILE", "/tmp/laf2-ios-idfa-state.json"))
    idfa_image_source = Path(os.environ.get("IOS_IDFA_SCREENSHOT", "/tmp/laf2-ios-idfa.png"))
    info = json.loads(state_source.read_text()) if state_source.is_file() else {
        "status": "UNAVAILABLE", "reason": "Native iOS Tracking state was not captured before the Bid.",
    }
    idfa = json.loads(idfa_state_source.read_text()) if idfa_state_source.is_file() else {}
    info["visible_idfa_status"] = idfa.get("status")
    info["visible_idfa"] = idfa.get("value")
    req_ia, ext_ia = _decoded_device_values(folder, "ia")
    req_lat, ext_lat = _decoded_device_values(folder, "lat")
    info["actual"] = {
        "request_idfa": req_ia, "extended_idfa": ext_ia,
        "request_lat": req_lat, "extended_lat": ext_lat,
    }
    tracking_image = folder / "tracking-allowed.png"
    if settings_source.is_file() and settings_source.stat().st_size:
        shutil.copy2(settings_source, tracking_image)
    else:
        info["status"] = "UNAVAILABLE"
        info["reason"] = "Native iOS Tracking screenshot was not captured before the Bid."
    if idfa_image_source.is_file() and idfa_image_source.stat().st_size:
        shutil.copy2(idfa_image_source, folder / "ios-idfa.png")
    (folder / "ios-tracking-allowed-status.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n"
    )
    if tracking_image.is_file():
        document = folder / "tracking-allowed-evidence.html"
        card = folder / "tracking-allowed-evidence.png"
        document.write_text(_tracking_allowed_evidence_document(info, tracking_image), encoding="utf-8")
        _write_html_screenshot(document, card)


IOS_SYSTEM_SCREENSHOTS = {
    "date_time": ("IOS_DATE_TIME_SCREENSHOT", "/tmp/laf2-ios-date-time.png", "ios-date-time.png"),
    "language_region": ("IOS_LANGUAGE_REGION_SCREENSHOT", "/tmp/laf2-ios-language-region.png", "ios-language-region.png"),
    "keyboards": ("IOS_KEYBOARDS_SCREENSHOT", "/tmp/laf2-ios-keyboards.png", "ios-keyboards.png"),
    "wifi": ("IOS_WIFI_SCREENSHOT", "/tmp/laf2-ios-wifi.png", "ios-wifi.png"),
    "cellular": ("IOS_CELLULAR_SCREENSHOT", "/tmp/laf2-ios-cellular.png", "ios-cellular.png"),
    "vpn": ("IOS_VPN_SCREENSHOT", "/tmp/laf2-ios-vpn.png", "ios-vpn.png"),
    "location": ("IOS_LOCATION_SCREENSHOT", "/tmp/laf2-ios-location-services.png", "ios-location-services.png"),
}


def _decoded_path_values(folder, path):
    decoded = json.loads((Path(folder) / "bid_decoded.json").read_text())
    result = []
    for envelope in ("req", "ext"):
        value = decoded.get(envelope, {}).get("plaintext", {})
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        result.append(value)
    return result


IOS_R5_VISUAL_CASES = {
    "advertising-id-opt-out": {
        "title": "Advertising ID — Tracking Denied",
        "scenario": "PRIVACY-DENIED",
        "path": "device.ia",
        "rule": "ATT denied; IDFA must be absent, empty, or the zero UUID",
    },
    "tracking-denied": {
        "title": "Advertising Tracking Denied",
        "scenario": "PRIVACY-DENIED",
        "path": "device.lat",
        "rule": "ATT denied; LAT must be integer 1",
    },
    "dark-mode-enabled": {
        "title": "Dark Mode — Enabled",
        "scenario": "DISPLAY-DARK",
        "path": "device.ext.darkmode",
        "rule": "Visible Dark appearance; payload must be true",
    },
    "font-scale-maximum": {
        "title": "Font Scale — Maximum",
        "scenario": "TEXT-MAX",
        "path": "device.ext.fontscale",
        "rule": "Visible rightmost Dynamic Type state; payload must match the reviewed scale",
    },
    "screen-brightness-minimum": {
        "title": "Screen Brightness — Minimum",
        "scenario": "DISPLAY-LOW",
        "path": "device.ext.screen_bright",
        "rule": "Visible minimum brightness; payload must match the captured slider state",
    },
    "output-volume-muted": {
        "title": "Output Volume — Muted",
        "scenario": "AUDIO-MUTED",
        "path": "device.ext.volume",
        "rule": "Visible muted media volume; payload must be 0",
    },
    "battery-saver-enabled": {
        "title": "Low Power Mode — Enabled",
        "scenario": "LOW-POWER",
        "path": "device.ext.battery_saver",
        "rule": "Visible Low Power Mode ON; payload must be true",
    },
    "screen-brightness-maximum": {
        "title": "Screen Brightness — Maximum",
        "scenario": "DISPLAY-HIGH",
        "path": "device.ext.screen_bright",
        "rule": "Visible maximum brightness; payload must be approximately 1",
    },
    "output-volume-maximum": {
        "title": "Output Volume — Maximum",
        "scenario": "AUDIO-HIGH",
        "path": "device.ext.volume",
        "rule": "Visible maximum media volume; payload must be approximately 1",
    },
    "timezone-changed": {
        "title": "Timezone — Changed",
        "scenario": "TIMEZONE-ALT",
        "path": "device.utcoffset",
        "rule": "Visible alternate timezone; payload must match its capture-time UTC offset",
    },
    "location-permission-denied": {
        "title": "Location Permission — Denied",
        "scenario": "LOCATION-DENIED",
        "path": "device.geo_lat / device.geo_lon",
        "rule": "Visible Never permission; precise coordinate fields must be absent",
    },
}


def _compact_evidence_value(value):
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _r5_payload_rows(folder, key, metadata):
    folder = Path(folder)
    if not (folder / "bid_decoded.json").is_file():
        return (("Wire path", metadata["path"]), ("Decoded Bid", "NOT CAPTURED"))
    if key in {"advertising-id-opt-out", "tracking-denied"}:
        req_ia, ext_ia = _decoded_path_values(folder, "device.ia")
        req_lat, ext_lat = _decoded_path_values(folder, "device.lat")
        return (
            ("Request device.ia", req_ia), ("Extended device.ia", ext_ia),
            ("Request device.lat", req_lat), ("Extended device.lat", ext_lat),
        )
    if key == "location-permission-denied":
        req_lat, ext_lat = _decoded_path_values(folder, "device.geo_lat")
        req_lon, ext_lon = _decoded_path_values(folder, "device.geo_lon")
        return (
            ("Request geo_lat / geo_lon", f"{_compact_evidence_value(req_lat)} / {_compact_evidence_value(req_lon)}"),
            ("Extended geo_lat / geo_lon", f"{_compact_evidence_value(ext_lat)} / {_compact_evidence_value(ext_lon)}"),
        )
    req, ext = _decoded_path_values(folder, metadata["path"])
    return ((f"Request {metadata['path']}", req), (f"Extended {metadata['path']}", ext))


def _r5_stage_image(folder, state, stage_name, fallback):
    stage = ((state.get("stages") or {}).get(stage_name) or {}) if isinstance(state, dict) else {}
    name = stage.get("screenshot") or fallback
    path = Path(folder) / name if name else None
    return path if path and path.is_file() and path.stat().st_size else None


def _r5_visual_evidence_document(folder, key, metadata, state, verdict):
    status = str(verdict.get("status") or "BLOCKED").upper()
    if status not in {"PASS", "FAILED", "BLOCKED"}:
        status = "BLOCKED"
    color = {"PASS": "#287a3d", "FAILED": "#b9342b", "BLOCKED": "#a56516"}[status]
    scenario = state.get("scenario") or metadata["scenario"]
    restored = ((state.get("stages") or {}).get("restored") or {})
    rows = (
        ("Scenario", scenario),
        ("State before", state.get("before")),
        ("Requested negative state", state.get("desired")),
        ("State after mutation", state.get("after")),
        ("Restore verification", restored.get("status") or "NOT RUN"),
        *_r5_payload_rows(folder, key, metadata),
    )
    row_html = "".join(
        f'<div class="row"><span>{html.escape(label)}</span><b>{html.escape(_compact_evidence_value(value))}</b></div>'
        for label, value in rows
    )
    stages = (
        ("BEFORE", _r5_stage_image(folder, state, "before", "ios-settings-before.png")),
        ("NEGATIVE STATE", _r5_stage_image(folder, state, "mutated", "ios-settings-state.png")),
        ("RESTORED", _r5_stage_image(folder, state, "restored", "ios-settings-restored.png")),
    )
    visible_stages = []
    for label, image_path in stages:
        if image_path:
            encoded = base64.b64encode(image_path.read_bytes()).decode()
            visible_stages.append(
                f'<div class="stage"><div>{label}</div><img src="data:image/png;base64,{encoded}"></div>'
            )
        else:
            visible_stages.append(f'<div class="stage missing"><div>{label}</div><p>NO SCREENSHOT</p></div>')
    reason = verdict.get("reason") or verdict.get("description") or "No verdict explanation was recorded."
    styles = '''
*{box-sizing:border-box}body{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{width:1400px;height:1000px;padding:36px 54px}.eyebrow{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}h1{font-size:38px;margin:7px 0 15px}.content{display:grid;grid-template-columns:620px 1fr;gap:34px}.visual{height:730px;background:#dfe5f5;border-radius:24px;padding:20px;display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.stage{min-width:0;display:flex;flex-direction:column;align-items:center;gap:10px;color:#0e7c86;font:700 14px ui-monospace,monospace}.stage img{width:100%;height:650px;object-fit:contain;border-radius:12px;box-shadow:0 10px 24px #17233335;background:#fff}.stage.missing{justify-content:center;border:2px dashed #a9b7c2;border-radius:14px;color:#6b7c87}.stage.missing p{font-size:15px;text-align:center}.panel{padding-top:4px}.source{padding:19px 23px;background:#14202a;color:#8ee0e6;border-radius:17px;font:700 21px ui-monospace,monospace}.rule{font-size:16px;line-height:1.4;color:#526571;margin:14px 2px 16px}.rows{background:white;border-radius:18px;padding:7px 21px}.row{display:grid;grid-template-columns:225px 1fr;gap:15px;padding:10px 0;border-bottom:1px solid #e3e9ed}.row:last-child{border:0}.row span{color:#60717c}.row b{font:700 14px ui-monospace,monospace;overflow-wrap:anywhere}.reason{font-size:14px;line-height:1.35;color:#526571;margin:13px 3px}.conclusion{display:flex;justify-content:space-between;align-items:center;margin-top:14px;padding:17px 22px;background:white;border-radius:16px;border-left:8px solid __RESULT_COLOR__}.conclusion b{font-size:28px;color:__RESULT_COLOR__}
'''.replace("__RESULT_COLOR__", color)
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>{styles}</style></head><body><main>
<div class="eyebrow">ALTERNATE / NEGATIVE STATE EVIDENCE · iOS R5</div><h1>{html.escape(metadata['title'])}</h1><div class="content"><div class="visual">{''.join(visible_stages)}</div><div class="panel"><div class="source">{html.escape(str(scenario))} · {html.escape(metadata['path'])}</div><p class="rule">{html.escape(metadata['rule'])}</p><div class="rows">{row_html}</div><p class="reason">{html.escape(str(reason))}</p><div class="conclusion"><span>Native state vs decoded Bid</span><b>{status}</b></div></div></div></main></body></html>'''


def materialize_ios_r5_visual_evidence(folder):
    """Build one reviewer-facing card per executed or blocked iOS R5 testcase."""
    folder = Path(folder)
    state = {}
    try:
        state = json.loads((folder / "ios-settings-state.json").read_text())
    except (OSError, json.JSONDecodeError):
        pass
    try:
        verdict_document = json.loads((folder / "verdicts.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    verdicts = verdict_document.get("verdicts") or []
    rendered = []
    for verdict in verdicts:
        key = verdict.get("tc")
        metadata = IOS_R5_VISUAL_CASES.get(key)
        if not metadata:
            continue
        document = folder / f"{key}-evidence.html"
        card = folder / f"{key}-evidence.png"
        document.write_text(
            _r5_visual_evidence_document(folder, key, metadata, state, verdict),
            encoding="utf-8",
        )
        _write_html_screenshot(document, card)
        verdict["evidence"] = card.name
        rendered.append(card.name)
    if rendered:
        (folder / "verdicts.json").write_text(
            json.dumps(verdict_document, ensure_ascii=False, indent=2) + "\n"
        )
    return rendered


IOS_AOS_ALIGNED_VISUAL_CASES = {
    "app-set-id": {
        "title": "Identifier for Vendor (IDFV)", "round": "R1",
        "source": "Decoded Bid Request · payload-only contract", "path": "device.ifv",
        "card": "app-set-id-evidence.png",
        "scope": "Wire-value contract: validate the payload value. Independent Sample App IDFV display is unavailable.",
    },
    "in-app-purchase-history": {
        "title": "In-App Purchase History", "round": "R1",
        "source": "Decoded Bid Request · payload-only contract", "path": "device.ext.iaphistory",
        "card": "in-app-purchase-history-evidence.png",
        "scope": "Payload-shape contract: validate array shape only. The Sample App has no purchase flow or reviewed product IDs.",
    },
    "boot-timestamps": {
        "title": "System Boot Timestamps", "round": "R1",
        "source": "Decoded Bid Request · payload-format contract", "path": "device.ext.pot",
        "card": "boot-timestamps-evidence.png",
        "scope": "Unlike AOS /proc/uptime, iOS has no independent visible uptime source in this harness.",
    },
    "ram-total": {
        "title": "RAM Status — Total", "round": "R1",
        "source": "Decoded Bid Request · payload-format contract", "path": "device.ext.mem_total",
        "card": "mem-total-evidence.png",
        "scope": "Unlike AOS /proc/meminfo, iOS has no independent system RAM source in this harness.",
    },
    "ram-available": {
        "title": "RAM Status — Available", "round": "R1",
        "source": "Decoded Bid Request · payload relationship", "path": "device.ext.mem_available",
        "card": "mem-available-evidence.png",
        "scope": "Shape and relationship review; no independent iOS MemAvailable source is claimed.",
    },
    "gyroscope": {
        "title": "Gyroscope", "round": "R1", "source": "Design scope decision", "path": "device.ext.gyroscope",
        "card": "gyroscope-evidence.png",
        "scope": "NOT IN SCOPE. No sensor motion or reviewed expected samples are executed.",
    },
    "accelerometer": {
        "title": "Accelerometer", "round": "R1", "source": "Design scope decision", "path": "device.ext.accelerometer",
        "card": "accelerometer-evidence.png",
        "scope": "NOT IN SCOPE. No sensor motion or reviewed expected samples are executed.",
    },
    "impression-history": {
        "title": "Previous Impression History", "round": "R2",
        "source": "Same-run impression record + second Bid", "card": "impression-history-evidence.png",
        "scope": "Causal Evidence: a proven first impression is compared with the later request.",
        "supporting_image": "screenshot.png",
    },
    "network-latency": {
        "title": "Connection Latency", "round": "R2",
        "source": "Same-run proxy event + second Bid", "card": "network-latency-evidence.png",
        "scope": "Causal Evidence: the SDK HEAD probe and later payload share one automation run.",
        "supporting_image": "screenshot.png",
    },
    "session-duration-continuous": {
        "title": "Session Duration — Continuous", "round": "R3", "source": "Four-step lifecycle sequence",
        "card": "session-duration-continuous-evidence.png", "sequence": "ios-lifecycle-sequence.json",
        "scope": "Compare the start and continuous-foreground captures.",
    },
    "session-duration-background": {
        "title": "Session Duration — Background", "round": "R3", "source": "Four-step lifecycle sequence",
        "card": "session-duration-background-evidence.png", "sequence": "ios-lifecycle-sequence.json",
        "scope": "Compare continuous foreground with Home/background/resume.",
    },
    "session-duration-termination": {
        "title": "Session Duration — Termination", "round": "R3", "source": "Four-step lifecycle sequence",
        "card": "session-duration-termination-evidence.png", "sequence": "ios-lifecycle-sequence.json",
        "scope": "Compare the resumed process with the cold capture after termination.",
    },
    "app-initialization-time": {
        "title": "App Initialization Time", "round": "R3", "source": "Four-step lifecycle sequence",
        "card": "app-initialization-time-evidence.png", "sequence": "ios-lifecycle-sequence.json",
        "scope": "The value must remain stable in one process and renew after termination.",
    },
    "app-duration-today": {
        "title": "Total App Usage Time Today", "round": "R3", "source": "Four-step lifecycle sequence",
        "card": "app-duration-today-evidence.png", "sequence": "ios-lifecycle-sequence.json",
        "scope": "The daily duration must remain monotonic across all lifecycle steps.",
    },
}

for _ipv6_key, _ipv6_title in (
    ("ipv6-address", "IPv6 Address"),
    ("ipv6-refresh-launch", "IPv6 Refresh — Launch"),
    ("ipv6-refresh-wifi-switch", "IPv6 Refresh — Wi-Fi Switch"),
    ("ipv6-refresh-recovery", "IPv6 Refresh — Recovery"),
    ("ipv6-refresh-debounce", "IPv6 Refresh — Debounce"),
    ("ipv6-refresh-slow-network", "IPv6 Refresh — Slow Network"),
):
    IOS_AOS_ALIGNED_VISUAL_CASES[_ipv6_key] = {
        "title": _ipv6_title, "round": "R4", "source": "Five-step network sequence",
        "card": f"{_ipv6_key}-evidence.png", "sequence": "r4-network-sequence.json",
        "scope": "Sequence Evidence: show the captured transitions and decoded IPv6/conntype values.",
    }


def _aligned_sequence_images(folder, metadata):
    """Return supporting screenshots without turning them into independent truth."""
    folder = Path(folder)
    if metadata.get("supporting_images"):
        return [(label, folder / name) for label, name in metadata["supporting_images"]]
    labels = ("START", "CONTINUOUS", "BACKGROUND", "TERMINATED")
    sequence_name = metadata.get("sequence")
    if sequence_name == "r4-network-sequence.json":
        labels = ("LAUNCH", "WI-FI SWITCH", "RECOVERY", "DEBOUNCE", "SLOW NETWORK")
    if sequence_name:
        try:
            sequence = json.loads((folder / sequence_name).read_text())
        except (OSError, json.JSONDecodeError):
            sequence = {}
        images = []
        captures = sequence.get("captures") or []
        for index, label in enumerate(labels):
            image_path = Path(captures[index]) / "screenshot.png" if index < len(captures) else folder / f"missing-step-{index + 1}.png"
            images.append((label, image_path))
        return images
    supporting = metadata.get("supporting_image")
    return [("SUPPORTING CAPTURE", folder / supporting)] if supporting else []


def _aligned_payload_rows(folder, key, metadata, verdict):
    rows = [("Expected", verdict.get("expected")), ("Actual", verdict.get("actual"))]
    path = metadata.get("path")
    if path and (Path(folder) / "bid_decoded.json").is_file():
        req, ext = _decoded_path_values(folder, path)
        rows.extend(((f"Request {path}", req), (f"Extended {path}", ext)))
    sequence_name = metadata.get("sequence")
    if sequence_name:
        try:
            sequence = json.loads((Path(folder) / sequence_name).read_text())
        except (OSError, json.JSONDecodeError):
            sequence = {}
        check = sequence.get(key) if isinstance(sequence.get(key), dict) else {}
        if check:
            rows.extend((("Sequence values", check.get("values")), ("Sequence rule", check.get("reason"))))
        if sequence_name == "r4-network-sequence.json":
            for index, capture in enumerate(sequence.get("captures") or []):
                capture_folder = Path(capture)
                if not (capture_folder / "bid_decoded.json").is_file():
                    continue
                req_ipv6, ext_ipv6 = _decoded_path_values(capture_folder, "device.ipv6")
                req_type, ext_type = _decoded_path_values(capture_folder, "device.conntype")
                rows.append((
                    f"Network step {index + 1}",
                    {"ipv6": ext_ipv6 if ext_ipv6 is not None else req_ipv6,
                     "conntype": ext_type if ext_type is not None else req_type},
                ))
        rows.append(("Captured steps", len(sequence.get("captures") or [])))
    return rows


def _compact_card_value(value, limit=520):
    rendered = _compact_evidence_value(value)
    return rendered if len(rendered) <= limit else rendered[:limit - 1] + "…"


def _aligned_visual_evidence_document(folder, key, metadata, verdict):
    status = str(verdict.get("status") or "BLOCKED").upper()
    if status not in {"PASS", "FAILED", "BLOCKED"}:
        status = "BLOCKED"
    color = {"PASS": "#287a3d", "FAILED": "#b9342b", "BLOCKED": "#a56516"}[status]
    row_html = "".join(
        f'<div class="row"><span>{html.escape(label)}</span><b>{html.escape(_compact_card_value(value))}</b></div>'
        for label, value in _aligned_payload_rows(folder, key, metadata, verdict)
    )
    images = _aligned_sequence_images(folder, metadata)
    visual = []
    for label, image_path in images:
        if image_path.is_file() and image_path.stat().st_size:
            encoded = base64.b64encode(image_path.read_bytes()).decode()
            visual.append(f'<div class="stage"><div>{html.escape(label)}</div><img src="data:image/png;base64,{encoded}"></div>')
        else:
            visual.append(f'<div class="stage missing"><div>{html.escape(label)}</div><p>NO SCREENSHOT</p></div>')
    if not visual:
        placeholder = "NOT IN SCOPE" if key in {"gyroscope", "accelerometer"} else "NO INDEPENDENT SCREEN"
        visual.append(f'<div class="stage missing only"><div>EVIDENCE SCOPE</div><p>{placeholder}</p></div>')
    reason = verdict.get("reason") or "No verdict explanation was recorded."
    styles = '''
*{box-sizing:border-box}body{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{width:1400px;height:1000px;padding:36px 54px}.eyebrow{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}h1{font-size:38px;margin:7px 0 15px}.content{display:grid;grid-template-columns:650px 1fr;gap:32px}.visual{height:730px;background:#dfe5f5;border-radius:24px;padding:18px;display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px}.stage{min-width:0;display:flex;flex-direction:column;align-items:center;gap:9px;color:#0e7c86;font:700 13px ui-monospace,monospace}.stage img{width:100%;height:650px;object-fit:contain;border-radius:11px;box-shadow:0 10px 24px #17233335;background:#fff}.stage.missing{justify-content:center;border:2px dashed #a9b7c2;border-radius:14px;color:#6b7c87}.stage.missing.only{grid-column:1/-1}.stage.missing p{text-align:center;font-size:17px}.panel{padding-top:4px}.source{padding:19px 22px;background:#14202a;color:#8ee0e6;border-radius:17px;font:700 20px ui-monospace,monospace}.scope{font-size:16px;line-height:1.4;color:#526571;margin:14px 2px 16px}.rows{background:white;border-radius:18px;padding:7px 20px}.row{display:grid;grid-template-columns:190px 1fr;gap:14px;padding:10px 0;border-bottom:1px solid #e3e9ed}.row:last-child{border:0}.row span{color:#60717c}.row b{font:700 13px ui-monospace,monospace;overflow-wrap:anywhere}.reason{font-size:14px;line-height:1.35;color:#526571;margin:13px 3px}.conclusion{display:flex;justify-content:space-between;align-items:center;margin-top:14px;padding:17px 21px;background:white;border-radius:16px;border-left:8px solid __COLOR__}.conclusion b{font-size:28px;color:__COLOR__}
'''.replace("__COLOR__", color)
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>{styles}</style></head><body><main>
<div class="eyebrow">CAPTURED EVIDENCE · iOS {html.escape(metadata['round'])}</div><h1>{html.escape(metadata['title'])}</h1><div class="content"><div class="visual">{''.join(visual)}</div><div class="panel"><div class="source">{html.escape(metadata['source'])}</div><p class="scope">{html.escape(metadata['scope'])}</p><div class="rows">{row_html}</div><p class="reason">{html.escape(str(reason))}</p><div class="conclusion"><span>Recorded contract result</span><b>{status}</b></div></div></div></main></body></html>'''


def materialize_ios_aos_aligned_visual_evidence(folder, skip_existing=False):
    """Render the remaining iOS contracts with the same evidence scope used by AOS."""
    folder = Path(folder)
    try:
        verdict_document = json.loads((folder / "verdicts.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    rendered = []
    for verdict in verdict_document.get("verdicts") or []:
        key = verdict.get("tc")
        metadata = IOS_AOS_ALIGNED_VISUAL_CASES.get(key)
        if not metadata:
            continue
        card = folder / metadata["card"]
        document = card.with_suffix(".html")
        if not (skip_existing and card.is_file() and card.stat().st_size > 1000):
            document.write_text(_aligned_visual_evidence_document(folder, key, metadata, verdict), encoding="utf-8")
            _write_html_screenshot(document, card)
        verdict["evidence"] = card.name
        rendered.append(card.name)
    if rendered:
        (folder / "verdicts.json").write_text(json.dumps(verdict_document, ensure_ascii=False, indent=2) + "\n")
    return rendered


def _context_evidence_document(title, rows, note, result, source_image, source_label):
    color = {"PASS": "#287a3d", "FAILED": "#b9342b", "BLOCKED": "#a56516"}[result]
    encoded = base64.b64encode(Path(source_image).read_bytes()).decode()
    row_html = "".join(
        f'<div class="row"><span>{html.escape(label)}</span><b>{html.escape("—" if value is None else str(value))}</b></div>'
        for label, value in rows
    )
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef1f4;color:#14202a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{width:1400px;height:1000px;padding:38px 62px}}.eyebrow{{color:#0e7c86;font:700 17px ui-monospace,monospace;letter-spacing:.08em}}h1{{font-size:38px;margin:7px 0 16px}}.content{{display:grid;grid-template-columns:430px 1fr;gap:38px}}.phone{{height:690px;display:flex;justify-content:center;overflow:hidden;background:#dfe5f5;border-radius:24px;padding:22px}}.phone img{{height:646px;width:auto;border-radius:13px;box-shadow:0 12px 28px #17233335}}.panel{{padding-top:15px}}.source{{padding:22px 26px;background:#14202a;color:#8ee0e6;border-radius:17px;font:700 22px ui-monospace,monospace}}.note{{font-size:17px;line-height:1.45;color:#526571;margin:17px 3px 22px}}.rows{{background:white;border-radius:18px;padding:8px 25px}}.row{{display:grid;grid-template-columns:270px 1fr;gap:18px;padding:13px 0;border-bottom:1px solid #e3e9ed}}.row:last-child{{border:0}}.row span{{color:#60717c}}.row b{{font:700 16px ui-monospace,monospace;overflow-wrap:anywhere}}.conclusion{{display:flex;justify-content:space-between;align-items:center;margin-top:20px;padding:19px 25px;background:white;border-radius:16px;border-left:8px solid {color}}}.conclusion b{{font-size:28px;color:{color}}}</style></head><body><main>
<div class="eyebrow">DIRECT SYSTEM CONTEXT EVIDENCE · iOS</div><h1>{html.escape(title)}</h1><div class="content"><div class="phone"><img src="data:image/png;base64,{encoded}"></div><div class="panel"><div class="source">{html.escape(source_label)}</div><p class="note">{html.escape(note)}</p><div class="rows">{row_html}</div><div class="conclusion"><span>Independent context vs SDK answer</span><b>{result}</b></div></div></div></main></body></html>'''


def materialize_ios_system_context(folder):
    folder = Path(folder)
    source_state = Path(os.environ.get("IOS_SYSTEM_CONTEXT_STATE_FILE", "/tmp/laf2-ios-system-context.json"))
    info = json.loads(source_state.read_text()) if source_state.is_file() else {
        "status": "UNAVAILABLE", "reason": "Native iOS system context was not captured before the Bid.", "pages": {},
    }
    pages = info.get("pages") or {}
    for key, (env_key, default, target_name) in IOS_SYSTEM_SCREENSHOTS.items():
        source = Path(os.environ.get(env_key, default))
        if source.is_file() and source.stat().st_size:
            shutil.copy2(source, folder / target_name)
        elif isinstance(pages.get(key), dict):
            pages[key]["status"] = "UNAVAILABLE"
            pages[key].setdefault("reason", f"{target_name} was not captured")
    locale = str(info.get("locale") or "").replace("_", "-")
    info["normalized_locale"] = locale or None
    info["language_code"] = locale.split("-", 1)[0].lower() if locale else None
    fields = {
        "timezone": "device.utcoffset", "language_iso": "device.lang",
        "language_bcp47": "device.langb", "keyboards": "device.input_lang",
        "connection_type": "device.conntype", "carrier": "device.carrier",
        "mcc_mnc": "device.mccmnc", "geo_lat": "device.geo_lat", "geo_lon": "device.geo_lon",
        "jailbreak": "device.ext.jailbreak", "emulator": "device.ext.emulator", "vpn": "device.ext.vpn",
    }
    info["actual"] = {key: {"request": values[0], "extended": values[1]} for key, path in fields.items() if (values := _decoded_path_values(folder, path))}
    (folder / "ios-system-context.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n")

    def render(key, title, page_key, rows, note, result, source_label):
        image_name = IOS_SYSTEM_SCREENSHOTS[page_key][2]
        image = folder / image_name
        if not image.is_file():
            return
        document = folder / f"{key}-evidence.html"
        card = folder / f"{key}-evidence.png"
        document.write_text(_context_evidence_document(title, rows, note, result, image, source_label), encoding="utf-8")
        _write_html_screenshot(document, card)

    actual = info["actual"]
    offset = info.get("timezone_offset_minutes")
    timezone_values = actual["timezone"]
    timezone_pass = type(offset) is int and timezone_values["extended"] == offset and (timezone_values["request"] is None or timezone_values["request"] == offset)
    render("default-timezone", "Default Timezone", "date_time", (
        ("Visible / OS timezone", info.get("timezone")), ("Calculated UTC offset minutes", offset),
        ("Request device.utcoffset", timezone_values["request"]), ("Extended device.utcoffset", timezone_values["extended"]),
    ), "The IANA timezone is captured independently and converted to the capture-time UTC offset, including DST.", "PASS" if timezone_pass else "FAILED", "Date & Time + ideviceinfo TimeZone")

    for key, title, expected_key, actual_key in (
        ("default-language-iso", "System Language Code", "language_code", "language_iso"),
        ("default-language-bcp47", "System Language and Region Tag", "normalized_locale", "language_bcp47"),
    ):
        expected = info.get(expected_key); values = actual[actual_key]
        passed = bool(expected and values["extended"] == expected and (values["request"] is None or values["request"] == expected))
        render(key, title, "language_region", (("ideviceinfo Locale", info.get("locale")), ("Normalized expected", expected), ("Request", values["request"]), ("Extended", values["extended"])), "Native Language & Region is the visual source; ideviceinfo Locale supplies the exact normalized tag.", "PASS" if passed else "FAILED", "Language & Region + ideviceinfo Locale")

    keyboard_expected = (pages.get("keyboards") or {}).get("keyboard_tags") or []
    keyboard_values = actual["keyboards"]
    keyboard_pass = bool(keyboard_expected and keyboard_values["extended"] == keyboard_expected and (keyboard_values["request"] is None or keyboard_values["request"] == keyboard_expected))
    render("keyboard-languages", "Installed Keyboard Languages", "keyboards", (("Visible mapped keyboards", keyboard_expected), ("Request device.input_lang", keyboard_values["request"]), ("Extended device.input_lang", keyboard_values["extended"])), "Visible keyboard rows are mapped to reviewed BCP 47 tags in displayed order.", "PASS" if keyboard_pass else ("BLOCKED" if not keyboard_expected else "FAILED"), "Settings > Keyboards")

    wifi = (pages.get("wifi") or {}).get("connected")
    connection_values = actual["connection_type"]
    connection_expected = "wifi" if wifi is True else None
    connection_pass = bool(connection_expected and connection_values["extended"] == connection_expected and (connection_values["request"] is None or connection_values["request"] == connection_expected))
    render("connection-type", "Connection Type", "wifi", (("Visible Wi-Fi connected", wifi), ("Expected transport", connection_expected), ("Request device.conntype", connection_values["request"]), ("Extended device.conntype", connection_values["extended"])), "A checked network on the native Wi-Fi page establishes Wi-Fi as the active visible transport.", "PASS" if connection_pass else ("BLOCKED" if connection_expected is None else "FAILED"), "Settings > Wi-Fi")

    no_sim = (pages.get("cellular") or {}).get("no_sim")
    for key, title, actual_key in (("carrier", "Carrier", "carrier"), ("mcc-mnc", "MCC/MNC", "mcc_mnc")):
        values = actual[actual_key]
        passed = no_sim is True and values["extended"] in (None, "") and values["request"] in (None, "")
        render(key, title, "cellular", (("Visible No SIM", no_sim), ("Request", values["request"]), ("Extended", values["extended"])), "No SIM establishes an empty carrier identity; an active SIM requires a separate exact carrier contract.", "PASS" if passed else ("BLOCKED" if no_sim is not True else "FAILED"), "Settings > Cellular")
    render("connection-type-cellular", "Connection Type (Cellular)", "cellular", (("Visible No SIM", no_sim), ("Observed payload transport", connection_values["extended"])), "This scenario requires an active SIM and cellular data. A visible No SIM state is an unmet environment prerequisite.", "BLOCKED", "Settings > Cellular")

    vpn = (pages.get("vpn") or {}).get("connected")
    vpn_values = actual["vpn"]; vpn_expected = "1" if vpn is True else ("0" if vpn is False else None)
    vpn_pass = vpn_expected is not None and vpn_values["extended"] == vpn_expected and (vpn_values["request"] is None or vpn_values["request"] == vpn_expected)
    render("vpn-status", "VPN Status", "vpn", (("Visible VPN connected", vpn), ("Expected payload", vpn_expected), ("Request", vpn_values["request"]), ("Extended", vpn_values["extended"])), "The native VPN page supplies the visible connected/not-connected state.", "PASS" if vpn_pass else ("BLOCKED" if vpn_expected is None else "FAILED"), "Settings > VPN & Device Management")

    for key, title, actual_key in (("precise-gps-latitude", "Precise GPS Latitude", "geo_lat"), ("precise-gps-longitude", "Precise GPS Longitude", "geo_lon")):
        values = actual[actual_key]
        render(key, title, "location", (("Location Services page", "captured"), ("Request payload", values["request"]), ("Extended payload", values["extended"])), "Location Services proves permission context but does not expose exact coordinates; a Sample App coordinate QA surface is still required.", "BLOCKED", "Settings > Location Services")

    render("root-status", "Jailbreak Status", "cellular", (("Physical ProductType", info.get("product_type")), ("Request jailbreak", actual["jailbreak"]["request"]), ("Extended jailbreak", actual["jailbreak"]["extended"])), "Native Settings and ProductType prove a real device but cannot independently prove absence of jailbreak; a reviewed integrity probe is required.", "BLOCKED", "Physical iPhone context")

    emulator_values = actual["emulator"]
    physical = str(info.get("product_type") or "").startswith(("iPhone", "iPad", "iPod"))
    emulator_pass = physical and emulator_values["extended"] is False and (emulator_values["request"] is None or emulator_values["request"] is False)
    render("emulator-detection", "Simulator Detection", "cellular", (("libimobiledevice ProductType", info.get("product_type")), ("Physical-device connection", physical), ("Request emulator", emulator_values["request"]), ("Extended emulator", emulator_values["extended"])), "A device reachable through libimobiledevice with a hardware ProductType is a physical iOS device.", "PASS" if emulator_pass else ("BLOCKED" if not physical else "FAILED"), "ideviceinfo physical-device context")


def materialize_ios_review_context(folder):
    folder = Path(folder)
    items = {
        "sdk-version": ("SDK Version", "app.sdk_version", "A reviewer or build manifest must provide the expected iOS Ads SDK version; the payload is only the observed value."),
        "argus-sdk-version": ("Argus SDK Version", "device.argus_ver", "A reviewer or build manifest must provide the expected iOS Argus version; the payload is only the observed value."),
        "last-foreground-times": ("Last Foreground Times", "user.last_foreground_time", "R1 has no independent visible foreground-event timeline, so array correctness cannot be claimed."),
        "last-background-times": ("Last Background Times", "user.last_background_time", "R1 has no independent visible background-event timeline, so array correctness cannot be claimed."),
        "force-gdpr-override": ("Force GDPR Override", "compliance.force_gdpr_applies", "The Sample App does not visibly expose its Force GDPR configuration input."),
        "coppa-applies": ("COPPA Applicability Flag", "compliance.coppa_applies", "The Sample App does not visibly expose its COPPA configuration input."),
    }
    status = {"status": "REVIEW_REQUIRED", "items": {}}
    image = folder / "screenshot.png"
    for key, (title, path, note) in items.items():
        req, ext = _decoded_path_values(folder, path)
        status["items"][key] = {"path": path, "request": req, "extended": ext, "reason": note}
        if not image.is_file():
            continue
        rows = (("Wire path", path), ("Request observed", req), ("Extended observed", ext), ("Independent expected", "UNAVAILABLE / REVIEW REQUIRED"))
        document = folder / f"{key}-evidence.html"
        card = folder / f"{key}-evidence.png"
        document.write_text(_context_evidence_document(title, rows, note, "BLOCKED", image, "Sample App capture · independent answer unavailable"), encoding="utf-8")
        _write_html_screenshot(document, card)
    (folder / "ios-review-context.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")


EVIDENCE_CAPTURES = {
    BID: EvidenceProvider(),
    IOS_DEVICE_CONTEXT: EvidenceProvider(after_bid=materialize_ios_device_context),
    IOS_IDFA_VISIBLE: EvidenceProvider(after_bid=materialize_ios_idfa_visible),
    IOS_IDFV_PAYLOAD: EvidenceProvider(after_bid=materialize_ios_idfv_payload),
    IOS_IAP_PAYLOAD: EvidenceProvider(after_bid=materialize_ios_iap_payload),
    IOS_BOOT_PAYLOAD: EvidenceProvider(after_bid=materialize_ios_boot_payload),
    IOS_RAM_PAYLOAD: EvidenceProvider(after_bid=materialize_ios_ram_payload),
    IOS_BATTERY_VISIBLE: EvidenceProvider(after_bid=materialize_ios_battery_visible),
    IOS_CHARGING_VISIBLE: EvidenceProvider(after_bid=materialize_ios_charging_visible),
    IOS_LOW_POWER_VISIBLE: EvidenceProvider(after_bid=materialize_ios_low_power_visible),
    IOS_DISPLAY_STATUS: EvidenceProvider(after_bid=materialize_ios_display_status),
    IOS_DEVICE_IDENTITY: EvidenceProvider(after_bid=materialize_ios_device_identity),
    IOS_BRIGHTNESS_VISIBLE: EvidenceProvider(after_bid=materialize_ios_brightness_visible),
    IOS_FONT_SIZE_VISIBLE: EvidenceProvider(after_bid=materialize_ios_font_size_visible),
    IOS_DARK_MODE_VISIBLE: EvidenceProvider(after_bid=materialize_ios_dark_mode_visible),
    IOS_OUTPUT_VOLUME_VISIBLE: EvidenceProvider(after_bid=materialize_ios_output_volume_visible),
    IOS_SYSTEM_CONTEXT_VISIBLE: EvidenceProvider(after_bid=materialize_ios_system_context),
    IOS_REVIEW_CONTEXT: EvidenceProvider(after_bid=materialize_ios_review_context),
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
