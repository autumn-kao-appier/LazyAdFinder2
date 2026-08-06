#!/usr/bin/env python3
"""Render the LazyAdFinder result platform and TestCase catalog."""

import argparse
import base64
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from verdict import Status


ROOT = Path(__file__).parent
VERDICTS_FILE = "verdicts.json"
SUMMARY_FILE = "summary.json"
LEGACY_METADATA_FILE = "metadata.json"
DEFAULT_CATALOG = ROOT / "testcases" / "testcase_catalog.json"
VALID_STATUSES = {status.value for status in Status}
STATUS_ORDER = (Status.FAILED.value, Status.BLOCKED.value, Status.PASS.value)
PLATFORMS = (("aos", "AOS", "Android"), ("ios", "iOS", "Apple"))
MODES = (("standalone", "Standalone"), ("mediation", "Mediation"))
TYPES = (
    ("aibid", "AIBID", "首購 / 新客競價"),
    ("reen-static", "REEN Static", "再行銷 · 靜態素材"),
    ("reen-dynamic", "REEN Dynamic", "再行銷 · 動態素材"),
)


class ReportError(RuntimeError):
    pass


def _load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except OSError as exc:
        raise ReportError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReportError(f"Invalid JSON in {path}: {exc}") from exc


def load_catalog(path):
    path = Path(path).expanduser().resolve()
    if not path.exists():
        return []
    document = _load_json(path)
    rows = document.get("testcases") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ReportError(f"{path} must contain {{\"testcases\": [...]}}")
    seen, display_ids, normalized = set(), set(), []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ReportError(f"{path}: testcase #{index + 1} must be an object")
        tc = str(row.get("key", "")).strip()
        if not tc or tc in seen:
            raise ReportError(f"{path}: testcase #{index + 1} has an empty or duplicate key")
        seen.add(tc)
        display_id = row.get("display_id")
        if display_id is not None:
            display_id = str(display_id).strip()
            if not display_id or display_id in display_ids:
                raise ReportError(f"{path}: {tc}.display_id is empty or duplicate")
            display_ids.add(display_id)
        order = row.get("order")
        if not isinstance(order, (int, float)) or isinstance(order, bool):
            raise ReportError(f"{path}: {tc}.order must be a number")
        modes = row.get("integration_modes", [])
        if not isinstance(modes, list) or any(mode not in {"standalone", "admob-mediation", "applovin-mediation"} for mode in modes):
            raise ReportError(f"{path}: {tc}.integration_modes contains an unsupported mode")
        for platform, _label, _device in PLATFORMS:
            spec = row.get(platform, {})
            if not isinstance(spec, dict):
                raise ReportError(f"{path}: {tc}.{platform} must be an object")
        normalized.append(row)
    return sorted(normalized, key=lambda item: (item["order"], item["key"]))


def _tc_label(key, catalog_by_key):
    display_id = catalog_by_key.get(key, {}).get("display_id")
    return str(display_id).strip() if display_id is not None else key


def current_verdicts(verdicts, catalog):
    """Return the latest result per registered TC and report slot.

    Evidence discovery intentionally reads all historical runs.  The current
    report must not turn those runs, or retired legacy IDs, into extra TC cards.
    """
    registered = {str(row["key"]) for row in catalog}
    latest = {}
    for row in verdicts:
        if row["tc"] not in registered:
            continue
        key = (row["platform"], row["mode_group"], row["test_type"], row["tc"])
        previous = latest.get(key)
        if previous is None or row["captured_at"] >= previous["captured_at"]:
            latest[key] = row
    return list(latest.values())


def _metadata_for(verdict_path):
    path = verdict_path.parent / SUMMARY_FILE
    if not path.exists():
        path = verdict_path.parent / LEGACY_METADATA_FILE
    if not path.exists():
        return {}
    document = _load_json(path)
    if not isinstance(document, dict):
        raise ReportError(f"{path} must contain an object")
    return document


def _platform_of(metadata, verdict_path):
    explicit = str(metadata.get("platform", "")).strip().lower()
    if explicit in {item[0] for item in PLATFORMS}:
        return explicit
    for part in verdict_path.parts:
        if re.search(r"(?:^|[_-])IOS(?:[_-]|$)", part, re.I):
            return "ios"
        if re.search(r"(?:^|[_-])AOS(?:[_-]|$)", part, re.I):
            return "aos"
    return "unknown"


def _mode_group(value):
    value = str(value).strip().lower()
    return "standalone" if value == "standalone" else "mediation" if "mediation" in value else value


def _verdict_rows(document, path):
    if isinstance(document, list):
        rows = document
    elif isinstance(document, dict) and isinstance(document.get("verdicts"), list):
        rows = document["verdicts"]
    else:
        raise ReportError(f"{path} must contain a list or {{\"verdicts\": [...]}}")
    metadata = _metadata_for(path)
    config = metadata.get("config") if isinstance(metadata.get("config"), dict) else metadata
    normalized = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ReportError(f"{path}: verdict #{index + 1} must be an object")
        tc, status = row.get("tc"), row.get("status")
        reason, evidence = row.get("reason", ""), row.get("evidence")
        if not isinstance(tc, str) or not tc.strip():
            raise ReportError(f"{path}: verdict #{index + 1} has no TC id")
        if status not in VALID_STATUSES:
            raise ReportError(f"{path}: {tc} has invalid status {status!r}")
        if not isinstance(reason, str):
            raise ReportError(f"{path}: {tc} reason must be a string")
        if status == Status.BLOCKED.value:
            if not reason.strip():
                raise ReportError(f"{path}: BLOCKED verdict {tc} requires a reason")
            if any(row.get(key) is not None for key in ("expected", "actual", "evidence")):
                raise ReportError(f"{path}: BLOCKED verdict {tc} cannot claim an answer")
        elif not isinstance(evidence, str) or not evidence.strip():
            raise ReportError(f"{path}: evaluated verdict {tc} requires evidence")
        test_mode = str(config.get("test_mode", "")).strip().lower()
        normalized.append({
            "tc": tc.strip(), "title": str(row.get("title", tc)).strip() or tc.strip(),
            "description": str(row.get("description", "")).strip(), "status": status,
            "reason": reason, "expected": row.get("expected"), "actual": row.get("actual"),
            "comparison_view": row.get("comparison_view") if isinstance(row.get("comparison_view"), dict) else None,
            "evidence": evidence, "source": path, "platform": _platform_of(metadata, path),
            "test_mode": test_mode, "mode_group": _mode_group(test_mode),
            "test_type": str(config.get("test_type", "")).strip().lower(),
            "captured_at": str(metadata.get("captured_at") or metadata.get("finished_at", "")),
            "capture_name": str(metadata.get("capture_name", "")),
            "test_round": str(config.get("test_round", "")),
            "test_cid": str(config.get("test_cid", "")),
            "device": metadata.get("device") if isinstance(metadata.get("device"), dict) else {},
            "layer": str(row.get("layer", "Signal")).strip().lower(),
        })
    return normalized


def discover(evidence_dirs):
    verdicts, captures, verdict_files, seen = [], [], [], set()
    for root_value in evidence_dirs:
        root = Path(root_value).expanduser().resolve()
        if not root.exists():
            continue
        captures.extend(sorted(
            {p.parent for p in root.rglob(SUMMARY_FILE)} |
            {p.parent for p in root.rglob(LEGACY_METADATA_FILE)}
        ))
        for path in sorted(root.rglob(VERDICTS_FILE)):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            verdict_files.append(resolved)
            verdicts.extend(_verdict_rows(_load_json(resolved), resolved))
    return verdicts, captures, verdict_files


def _display(value):
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


DISPLAY_LABELS = {
    "settings_gaid": "設定頁 GAID",
    "opt_out": "Opt out",
    "req_device_ia": "Request GAID",
    "ext_device_ia": "Extended GAID",
    "visible_opt_out": "設定頁 Opt out",
    "req_device_lat": "Request LAT",
    "ext_device_lat": "Extended LAT",
    "req_app_sdk_version": "Request SDK version",
    "build_sdk_version": "Build SDK version",
    "collection_status": "Collection status",
    "package_count": "Package count",
    "packages": "Packages",
    "field_present": "Field present",
    "product_count": "Product count",
    "product_ids": "Product IDs",
    "timestamp_count": "Timestamp count",
    "current_boot_reference_ms": "Current boot reference (ms)",
    "pot": "Boot timestamps",
    "source": "Answer key source",
}

TC_TITLES_ZH = {
    "advertising-id": "廣告識別碼（GAID）", "app-set-id": "供應商識別碼（App Set ID）",
    "installed-app-list": "已安裝 App 清單", "in-app-purchase-history": "App 內購買紀錄",
    "boot-timestamps": "系統開機時間", "ram-total": "RAM 總容量", "ram-available": "可用 RAM",
    "disk-total": "儲存空間總容量", "disk-free": "可用儲存空間", "battery-level": "電池電量",
    "charging-status": "充電狀態", "battery-saver": "省電模式", "screen-width": "螢幕寬度",
    "screen-height": "螢幕高度", "screen-ppi": "螢幕 PPI", "pixel-ratio": "像素比例",
    "screen-brightness": "螢幕亮度", "font-scale": "字型縮放", "dark-mode": "深色模式",
    "gyroscope": "陀螺儀", "accelerometer": "加速度計", "tracking-allowed": "允許廣告追蹤",
    "sdk-version": "SDK 版本", "output-volume": "輸出音量", "device-make": "裝置品牌",
    "device-model": "裝置型號", "default-timezone": "預設時區",
    "default-language-iso": "預設語言（ISO-639-1）", "default-language-bcp47": "預設語言（BCP 47）",
    "keyboard-languages": "已安裝的鍵盤語言", "root-status": "Root 狀態",
    "emulator-detection": "模擬器偵測", "ipv6-address": "IPv6 位址", "connection-type": "連線類型",
    "carrier": "電信業者", "mcc-mnc": "MCC/MNC", "precise-gps-latitude": "精確 GPS 緯度",
    "precise-gps-longitude": "精確 GPS 經度",
    "session-duration-continuous": "Session 時長 — App 持續開啟",
    "session-duration-background": "Session 時長 — 背景恢復",
    "session-duration-termination": "Session 時長 — 終止後重設",
    "app-initialization-time": "App 初始化時間",
    "app-duration-today": "今日 App 使用總時長",
    "connection-type-cellular": "連線類型（行動網路）",
    "force-gdpr-override": "強制套用 GDPR",
    "coppa-applies": "COPPA 適用旗標",
    "standalone-sdk-init": "SDK 初始化", "standalone-appier-ad-request": "Appier 直接廣告請求",
    "standalone-creative-assets": "廣告素材載入", "standalone-native-render": "原生廣告渲染",
    "standalone-impression": "Appier 曝光追蹤", "standalone-click": "Appier 點擊追蹤",
    "standalone-landing": "Landing 跳轉", "standalone-privacy": "隱私資訊",
    "standalone-install-attribution": "AIBID 安裝歸因",
    "standalone-attribution-reconciliation": "後端歸因對帳",
    "admob-sdk-init": "SDK 初始化", "admob-pubsetting": "AdMob Mediation 設定",
    "admob-gma-request": "AdMob GMA 請求與 Mediation 分流",
    "admob-appier-ad-request": "Appier Adapter 廣告請求",
    "admob-creative-render": "Mediation 素材載入與渲染",
    "admob-impression": "AdMob 與 Appier 曝光追蹤",
    "admob-fill-result": "Mediation Fill 結果",
    "admob-click": "AdMob 與 Appier 點擊追蹤",
    "admob-landing-privacy": "Mediation Landing 與隱私資訊",
    "last-foreground-times": "最近前景時間", "last-background-times": "最近背景時間",
    "impression-history": "曝光紀錄", "vpn-status": "VPN 狀態", "argus-sdk-version": "Argus SDK 版本",
    "network-latency": "網路延遲",
}


def _bi(en, zh):
    return (
        f'<span class="lang-en">{html.escape(str(en))}</span>'
        f'<span class="lang-zh">{html.escape(str(zh))}</span>'
    )


DYNAMIC_ZH = {
    "Actual SDK Payload": "SDK 實際內容",
    "Active Android network": "Android 目前使用的網路",
    "Android Media volume": "Android 媒體音量",
    "Android SIM state": "Android SIM 狀態",
    "Android UTC offset": "Android UTC 時差",
    "Android font scale": "Android 字型大小比例",
    "Android hardware probe": "Android 硬體檢查",
    "Android language": "Android 語言",
    "Android locale tag": "Android 地區語言標籤",
    "Android logical density": "Android 邏輯密度",
    "Android manufacturer": "Android 製造商",
    "Android product model": "Android 產品型號",
    "Android root probe": "Android Root 檢查",
    "Calculated boot time": "推算的開機時間",
    "Captured OS bytes": "實機系統容量（bytes）",
    "Captured brightness": "實機亮度",
    "Captured build": "擷取時的 Build",
    "Captured power state": "實機電源狀態",
    "Captured screen height": "實機螢幕高度",
    "Captured screen width": "實機螢幕寬度",
    "Decoded Bid Request": "解碼後的 Bid Request",
    "Density ÷ 160": "螢幕密度 ÷ 160",
    "Enabled Gboard languages": "已啟用的 Gboard 語言",
    "Latest SDK timestamp": "SDK 最新時間戳",
    "Required version": "要求版本",
    "Reviewed Build Target": "已確認的 Build 目標",
    "SDK Payload": "SDK 解碼內容",
    "SDK payload bytes": "SDK 解碼容量（bytes）",
    "Visible Android GAID": "畫面可見的 Android GAID",
    "Visible Battery Saver": "畫面可見的省電模式",
    "Visible Dark theme": "畫面可見的深色主題",
    "Visible battery level": "畫面可見的電池電量",
    "Visible opt-out state": "畫面可見的退出個人化廣告狀態",
    "Official physical PPI: 422.2 (supporting check)": "官方實體 PPI：422.2（輔助檢查）",
    "Sample App has no purchase flow or independent expected product IDs; the captured array cannot be verified for correctness": "Sample App 沒有購買流程，也沒有獨立定義的預期商品 ID，因此目前無法驗證擷取陣列的內容是否正確。",
    "Round limitation: SampleApp session start timestamp and field unit are not yet exposed": "本輪限制：Sample App 尚未提供 session 開始時間與欄位單位。",
    "Round limitation: no reviewed IPv6 payload field is present in this capture": "本輪限制：這次擷取中沒有已確認的 IPv6 payload 欄位。",
    "Not In Scope: location ground-truth capture is not defined; device.lat is the tracking flag, not latitude": "不在本輪範圍：尚未定義定位真值的擷取方式；device.lat 是追蹤旗標，不是緯度。",
    "Not In Scope: location ground-truth capture is not defined; the observed payload path is device.geo_lon": "不在本輪範圍：尚未定義定位真值的擷取方式；目前觀察到的欄位路徑是 device.geo_lon。",
    "Not In Scope: this round has no sensor motion setup or reviewed expected samples": "不在本輪範圍：本輪沒有感測器動作設定，也沒有已確認的預期樣本。",
    "Not In Scope: this Android payload has no reviewed device.ext.vpn field": "不在本輪範圍：目前 Android payload 沒有已確認的 device.ext.vpn 欄位。",
    "Hardware limitation: QA device has no active SIM; Carrier cannot be captured or verified": "硬體限制：QA 裝置沒有 active SIM，因此無法取得或驗證 Carrier。",
    "Hardware limitation: QA device has no active SIM; MCC/MNC cannot be captured or verified": "硬體限制：QA 裝置沒有 active SIM，因此無法取得或驗證 MCC/MNC。",
    "Round limitation: first-ad proxy timing evidence is missing": "本輪限制：缺少第一個廣告的 proxy timing 證據。",
    "Environment limitation: company network has no IPv6; waiting for IT support": "環境限制：公司網路目前沒有 IPv6，等待 IT 支援。",
    "Hardware limitation: QA device has no active SIM; 4G/5G transport cannot be established": "硬體限制：QA 裝置沒有 active SIM，無法建立 4G／5G 連線。",
    "Round limitation: the SDK latency probe endpoint timing is not captured independently yet": "本輪限制：尚未獨立擷取 SDK latency probe endpoint 的計時證據。",
    "Sample App limitation: setForceGDPRApplies(true) is not exposed or invoked": "Sample App 限制：目前沒有提供或呼叫 setForceGDPRApplies(true)。",
    "req_langb, langb do not match Android locale tag": "req.langb 與 ext.langb 未符合 Android 系統地區語言標籤。",
}


def _dynamic_bi(text, zh=None):
    text = str(text)
    return _bi(text, zh or DYNAMIC_ZH.get(text, text))


def _tc_title(row):
    return _bi(row["title"], TC_TITLES_ZH.get(row["tc"], row["title"]))


def _friendly_value(key, value):
    if key in {"opt_out", "visible_opt_out"} and isinstance(value, bool):
        return "開啟" if value else "關閉"
    if value == "ABSENT":
        return "未傳送（允許）"
    return _display(value)


def _fact_list(value):
    if not isinstance(value, dict):
        return f'<p class="plain-value">{html.escape(_display(value))}</p>'
    items = []
    for key, item in value.items():
        label = DISPLAY_LABELS.get(key, key.replace("_", " "))
        items.append(
            f'<div><dt>{html.escape(label)}</dt><dd>{html.escape(_friendly_value(key, item))}</dd></div>'
        )
    return f'<dl class="facts">{"".join(items)}</dl>'


def _evidence_content(row, guidance=""):
    guidance_html = (
        f'<div class="evidence-guidance"><b>{_bi("Evidence guidance", "Evidence 說明")}</b><p>{html.escape(guidance)}</p></div>'
        if guidance else ""
    )
    reference = row.get("evidence")
    if not reference:
        return guidance_html + f'<div class="evidence-missing">{_bi("No evidence is attached to this result.", "此結果沒有 Evidence。")}</div>'
    target = Path(reference)
    if not target.is_absolute():
        target = row["source"].parent / target
    if not target.exists():
        return guidance_html + f'<div class="evidence-missing">{_bi("Evidence file not found", "找不到 Evidence 檔案")} · {html.escape(reference)}</div>'
    mime, _encoding = mimetypes.guess_type(target.name)
    if mime and mime.startswith("image/"):
        encoded = base64.b64encode(target.read_bytes()).decode("ascii")
        return guidance_html + f'''<figure class="evidence-image"><button class="evidence-zoom" type="button" aria-label="放大 {html.escape(reference)}"><img src="data:{mime};base64,{encoded}" alt="{html.escape(reference)}"></button>
<figcaption>{html.escape(reference)} · {_bi("Click to view full image", "點擊查看全圖")}</figcaption></figure>'''
    if target.suffix.lower() == ".json":
        document = _load_json(target)
        expected_html = ""
        if "expected" in document:
            expected_html = f'<label>{_bi("Expected", "預期")}</label>{_fact_list(document.get("expected"))}'
        note_html = ""
        if document.get("note"):
            note_html = f'<div class="result-note"><b>{_bi("Note", "補充說明")}</b><p>{_dynamic_bi(document["note"])}</p></div>'
        return guidance_html + f'''<div class="evidence-data"><b>{html.escape(reference)}</b>{expected_html}<label>{_bi("Captured evidence", "擷取證據")}</label>{_fact_list(document.get("actual", {}))}{note_html}</div>'''
    return guidance_html + f'<pre class="evidence-text">{html.escape(target.read_text(errors="replace"))}</pre>'


def _comparison_value(value):
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if type(value) is int:
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:g}"
    return _display(value)


def _comparison_cell(item, css_class):
    item = item if isinstance(item, dict) else {}
    label = str(item.get("label") or "—")
    if label.lower().startswith("sdk "):
        label = "Decoded Bid Request"
    value = item.get("value")
    if isinstance(value, list):
        rendered_value = '<ul class="comparison-list">' + "".join(
            f'<li>{html.escape(_display(entry))}</li>' for entry in value
        ) + "</ul>"
    else:
        rendered_value = f'<b>{html.escape(_comparison_value(value))}</b>'
    return f'''<div class="comparison-value {css_class}"><label>{_dynamic_bi(label)}</label>
{rendered_value}</div>'''


def _comparison_summary(row, fallback_criterion):
    if row["status"] == Status.BLOCKED.value:
        return f'<section class="comparison-hero blocked-comparison"><b>{_bi("Not executed", "未執行")}</b><span>{_bi("No comparison is claimed.", "未宣稱任何比較結果。")}</span></section>'
    view = row.get("comparison_view")
    if not isinstance(view, dict):
        return f'''<section class="comparison-hero rule-comparison">{_comparison_cell({"label": "Actual SDK Payload", "value": row.get("actual")}, "actual-value")}
<p><span>{_bi("Pass criterion", "通過標準")}</span>{_dynamic_bi(fallback_criterion, fallback_criterion)}</p></section>'''
    kind = view.get("kind")
    if row["tc"] == "installed-app-list" and kind == "rule":
        view = dict(view)
        view["actual"] = {
            "label": "Decoded Bid Request",
            "value": (row.get("actual") or {}).get("packages", []),
        }
    criterion = str(view.get("criterion") or fallback_criterion)
    if kind == "compare":
        operator = str(view.get("operator") or "↔")
        if len(operator) > 2:
            operator = "↔"
        body = f'''<div class="comparison-pair">{_comparison_cell(view.get("captured"), "captured-value")}
<div class="comparison-operator">{html.escape(operator)}</div>{_comparison_cell(view.get("actual"), "actual-value")}</div>'''
    elif kind == "fixed":
        required = view.get("required") if isinstance(view.get("required"), dict) else {}
        captured = view.get("captured") if isinstance(view.get("captured"), dict) else {}
        if required.get("value") == captured.get("value"):
            body = f'''<div class="comparison-pair">{_comparison_cell({"label": "Reviewed Build Target", "value": required.get("value")}, "required-value")}
<div class="comparison-operator">=</div>{_comparison_cell(view.get("actual"), "actual-value")}</div>'''
            criterion = "Decoded app.sdk_version must match the reviewed build target."
        else:
            body = f'''<div class="comparison-triplet">{_comparison_cell(required, "required-value")}
<div class="comparison-operator">=</div>{_comparison_cell(captured, "captured-value")}
<div class="comparison-operator">=</div>{_comparison_cell(view.get("actual"), "actual-value")}</div>'''
    else:
        body = f'<div class="comparison-rule-value">{_comparison_cell(view.get("actual"), "actual-value")}</div>'
    supporting = f'<small>{_dynamic_bi(view["supporting"])}</small>' if view.get("supporting") else ""
    return f'''<section class="comparison-hero {html.escape(str(kind or "rule"))}-comparison">{body}
<p><span>{_bi("Pass criterion", "通過標準")}</span>{_dynamic_bi(criterion, fallback_criterion)}</p>{supporting}</section>'''


def _result_card(row, catalog_by_key):
    spec = catalog_by_key.get(row["tc"], {})
    platform_spec = spec.get(row["platform"], {}) if isinstance(spec, dict) else {}
    expected_text = str(platform_spec.get("expected") or _display(row["expected"]))
    priority = str(spec.get("priority") or "—")
    result_note = ""
    if row["reason"]:
        result_note = f'<div class="result-note"><b>{_bi("Result note", "結果說明")}</b><p>{_dynamic_bi(row["reason"])}</p></div>'
    override_key = ":".join((
        row["platform"], row["mode_group"], row["test_type"], row["captured_at"], row["tc"]
    ))
    comparison_html = _comparison_summary(row, expected_text)
    return f'''<article class="result-card" data-result-status="{row["status"].lower()}" data-automation-status="{row["status"].lower()}" data-layer="{html.escape(row["layer"])}" data-override-key="{html.escape(override_key, quote=True)}">
<div class="result-head"><div><strong>{_tc_title(row)}</strong>
<span class="tc-id">{html.escape(_tc_label(row["tc"], catalog_by_key))}</span></div><div class="result-badges"><span class="priority-tag">{html.escape(priority)}</span><span class="status {row["status"].lower()}">{row["status"]}</span></div></div>
<div class="card-tabs"><button class="on" data-card-tab="summary">{_bi("Result", "結果")}</button><button data-card-tab="evidence">Evidence</button></div>
<div class="card-page" data-card-page="summary">{comparison_html}{result_note}</div>
<div class="card-page" data-card-page="evidence" hidden><section class="evidence-contract captured-block"><label>{_bi("Captured source", "擷取來源")}</label>{_evidence_content(row, str(platform_spec.get("evidence_note") or ""))}</section>
{comparison_html}
<details class="manual-review"><summary><span>{_bi("Manual override", "人工覆寫")}</span><span class="manual-indicator" hidden>MANUAL</span></summary><div class="manual-form"><small>{_bi("Automation status", "自動化狀態")}：{row["status"]}</small>
<label>{_bi("Status", "狀態")}<select data-manual-status><option value="">Use automation result／使用自動化結果</option><option value="PASS">PASS</option><option value="FAILED">FAILED</option><option value="BLOCKED">BLOCKED</option></select></label>
<label>{_bi("Reason", "理由")}<textarea data-manual-reason rows="2" placeholder="Manual override reason／人工修改理由"></textarea></label>
<div class="manual-actions"><button data-manual-save>{_bi("Save override", "儲存覆寫")}</button><button data-manual-reset>{_bi("Clear override", "清除覆寫")}</button></div>
<div class="manual-saved" hidden></div></div></details></div></article>'''


def _run_information(rows):
    if not rows:
        return ""
    row = max(rows, key=lambda item: item["captured_at"])
    device = row["device"]
    device_name = device.get("model") or device.get("name") or "—"
    os_version = device.get("android_version") or device.get("os_version") or "—"
    sdk = device.get("sdk")
    os_text = f"Android {os_version}" + (f" · API {sdk}" if sdk else "")
    values = (
        (_bi("Device", "裝置"), device_name),
        (_bi("System", "系統"), os_text),
        (_bi("Round", "輪次"), row["test_round"] or "—"),
        (_bi("Mode", "模式"), row["test_mode"] or "—"),
        (_bi("Type", "類型"), row["test_type"] or "—"),
        ("CID", row["test_cid"] or "—"),
        (_bi("Executed", "執行時間"), row["captured_at"] or "—"),
    )
    cells = "".join(
        f'<div><label>{label}</label><b>{html.escape(str(value))}</b></div>'
        for label, value in values
    )
    return f'<section class="run-info"><div class="run-info-title"><span>{_bi("Latest Run", "最新執行")}</span><b>{_bi("Test specification", "測試規格")}</b></div><div class="run-info-grid">{cells}</div></section>'


def _status_filters(rows):
    counts = Counter(row["status"] for row in rows)
    buttons = []
    for status in (Status.PASS, Status.FAILED, Status.BLOCKED):
        value = status.value
        count = counts[value]
        disabled = " disabled" if count == 0 else ""
        buttons.append(
            f'<button class="status-filter {value.lower()}" data-status-filter="{value.lower()}"{disabled}>'
            f'<span>{value}</span><b>{count}</b></button>'
        )
    return f'''<section class="status-filters"><div><span>{_bi("Result filter", "結果篩選")}</span><small>{_bi("Click again to show all", "再次點擊可顯示全部")}</small></div><div class="status-filter-buttons">{"".join(buttons)}</div></section>'''


def _slot_card(platform, mode, kind, label, description, rows):
    signal_rows = [row for row in rows if row["layer"] != "e2e"]
    e2e_rows = [row for row in rows if row["layer"] == "e2e"]
    signal = Counter(row["status"] for row in signal_rows)
    e2e = Counter(row["status"] for row in e2e_rows)
    return f'''<button class="type-card" data-slot="{platform}:{mode}:{kind}">
<div><span class="type-id">{html.escape(kind)}</span><span class="total" data-result-count="{len(rows)}">{len(rows)} {_bi("results", "筆結果")}</span></div>
<h3>{html.escape(label)}</h3><p>{html.escape(description)}</p>
<div class="layer-row" data-layer-row="e2e"><b>E2E</b><span class="pass-text">{e2e[Status.PASS.value]}✓</span><span class="failed-text">{e2e[Status.FAILED.value]}✗</span><span class="blocked-text" title="BLOCKED">{e2e[Status.BLOCKED.value]}▲</span><small>{len(e2e_rows)} TC</small></div>
<div class="layer-row" data-layer-row="signal"><b>Signal</b><span class="pass-text">{signal[Status.PASS.value]}✓</span><span class="failed-text">{signal[Status.FAILED.value]}✗</span><span class="blocked-text" title="BLOCKED">{signal[Status.BLOCKED.value]}▲</span><small>{len(signal_rows)} TC</small></div>
<b class="open">{_bi("View results →", "查看結果 →")}</b></button>'''


def _slot_detail(platform, mode, kind, label, rows, catalog_by_key):
    def cards_for(layer):
        selected = [row for row in rows if (row["layer"] == "e2e") == (layer == "e2e")]
        if layer == "e2e":
            # E2E is one causal journey.  Its cards must remain in the reviewed
            # Catalog sequence (Init -> Request -> Render -> Tracking ->
            # Attribution), regardless of PASS/FAILED/BLOCKED status.
            def sort_key(row):
                spec = catalog_by_key.get(row["tc"], {})
                return (spec.get("order", float("inf")), row["tc"])
        else:
            def sort_key(row):
                return (STATUS_ORDER.index(row["status"]), row["captured_at"])
        return "".join(_result_card(row, catalog_by_key) for row in sorted(
            selected, key=sort_key
        ))
    signal_cards = cards_for("signal") or '<div class="empty"><b>Signal 尚無結果</b><p>加入 Signal TC 並產生 Verdict 後顯示於此。</p></div>'
    e2e_cards = cards_for("e2e") or '<div class="empty"><b>E2E 尚未建立</b><p>位置已保留；Signal 完成後再加入完整鏈路 TC。</p></div>'
    platform_label = next(item[1] for item in PLATFORMS if item[0] == platform)
    mode_label = next(item[1] for item in MODES if item[0] == mode)
    return f'''<section class="slot-detail" data-slot="{platform}:{mode}:{kind}" hidden>
<div class="detail-bar"><button class="back">{_bi("← Back to categories", "← 返回分類")}</button><div><span class="crumb">{platform_label} / {mode_label}</span><h2>{html.escape(label)}</h2></div></div>
{_status_filters(rows)}
{_run_information(rows)}
<div class="report-section"><div class="section-title"><span>01</span><div><h3>E2E</h3><p>Init → Bid → Render → Impression → Click → Landing</p></div></div><div class="result-grid">{e2e_cards}</div></div>
<div class="report-section"><div class="section-title"><span>02</span><div><h3>Signal</h3><p>{_bi("SDK fields, identifiers, and event signals", "SDK 欄位、識別碼與事件訊號")}</p></div></div><div class="result-grid">{signal_cards}</div></div></section>'''


def _catalog_cell(spec):
    if not spec.get("applicable", False):
        return f'<div class="na"><b>N/A</b><p>{html.escape(str(spec.get("expected", "")))}</p></div>'
    return f'''<div class="platform-spec"><b>{_bi("Setup", "設定")}</b><p>{html.escape(str(spec.get("setup", "—")))}</p>
<b>{_bi("Expected", "預期")}</b><p>{html.escape(str(spec.get("expected", "—")))}</p>
<b>Evidence</b><p>{html.escape(str(spec.get("evidence", "—")))}</p></div>'''


def _catalog_table(catalog, catalog_by_key):
    rows = []
    for tc in catalog:
        key = str(tc["key"])
        label = _tc_label(key, catalog_by_key)
        key_line = f'<code class="catalog-key">{html.escape(key)}</code>' if label != key else ""
        modes = " / ".join(str(mode) for mode in tc.get("integration_modes", []))
        mode_line = f'<small class="catalog-modes">{html.escape(modes)}</small>' if modes else ""
        rows.append(f'''<tr><td><span class="draft">{html.escape(str(tc.get("status", "DRAFT")))}</span>
<strong class="catalog-id">{html.escape(label)}</strong>{key_line}<small>{html.escape(str(tc.get("round", "")))}</small>{mode_line}</td>
<td><b>{_bi(str(tc.get("title", "")), TC_TITLES_ZH.get(key, str(tc.get("title", ""))))}</b><p>{html.escape(str(tc.get("layer", "Signal")))} · {html.escape(str(tc.get("category", "")))}</p>
<code>{html.escape(str(tc.get("field", "")))}</code><span class="priority">{html.escape(str(tc.get("priority", "")))}</span></td>
<td>{_catalog_cell(tc.get("aos", {}))}</td><td>{_catalog_cell(tc.get("ios", {}))}</td></tr>''')
    body = "".join(rows) or f'<tr><td colspan="4" class="empty">{_bi("No TestCase is defined.", "尚未定義 TestCase。")}</td></tr>'
    return f'''<div class="table-wrap"><table><thead><tr><th>TestCase</th><th>{_bi("Purpose / field", "目的／欄位")}</th><th>AOS</th><th>iOS</th></tr></thead><tbody>{body}</tbody></table></div>'''


CSS = r"""
:root{--bg:#eef1f4;--panel:#fff;--panel2:#f6f8fa;--ink:#131a21;--soft:#516069;--faint:#7d8b94;--line:#dbe2e8;--accent:#0e7c86;--accent2:#e2eff1;--aos:#2e9e5b;--ios:#3a6ea5;--pass:#2f7d3a;--fail:#c0392b;--block:#b5761a;--shadow:0 1px 2px #131a210f,0 8px 24px #131a210f;--mono:ui-monospace,SFMono-Regular,Menlo,monospace;--sans:system-ui,-apple-system,"Segoe UI","Noto Sans TC",sans-serif}
:root[data-lang=en] .lang-zh,:root[data-lang=zh] .lang-en{display:none!important}.language{border:1px solid var(--line);background:var(--panel);color:var(--ink);padding:6px 10px;border-radius:8px;cursor:pointer;font:750 11px var(--mono)}
.type-card h3,.type-card p,.type-card .total,.type-card .layer-row,.type-card .open{font-family:"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif}.type-card .lang-en,.type-card .lang-zh{font-size:inherit;line-height:inherit;font-weight:inherit;letter-spacing:inherit}
@media(prefers-color-scheme:dark){:root{--bg:#0d1216;--panel:#151d23;--panel2:#111820;--ink:#e7edf1;--soft:#a6b6c1;--faint:#71828d;--line:#243039;--accent:#38bdc9;--accent2:#123037;--aos:#4cc57d;--ios:#6ba6dd;--pass:#5cc46a;--fail:#f0766a;--block:#e0a94a;--shadow:0 10px 30px #0006}}
:root[data-theme=dark]{--bg:#0d1216;--panel:#151d23;--panel2:#111820;--ink:#e7edf1;--soft:#a6b6c1;--faint:#71828d;--line:#243039;--accent:#38bdc9;--accent2:#123037;--aos:#4cc57d;--ios:#6ba6dd;--pass:#5cc46a;--fail:#f0766a;--block:#e0a94a;--shadow:0 10px 30px #0006}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans)}button,select{font:inherit}.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:18px;padding:10px 18px;background:color-mix(in srgb,var(--panel) 90%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}.brand{font-weight:800}.brand small{display:block;color:var(--faint);font:10px var(--mono)}.main-nav{display:flex;gap:4px}.main-nav button,.seg button,.back,.theme{border:1px solid transparent;background:transparent;color:var(--soft);padding:7px 12px;border-radius:8px;cursor:pointer}.main-nav button.on,.seg button.on{background:var(--accent2);color:var(--accent);font-weight:750}.theme{margin-left:auto;border-color:var(--line)}main{max-width:1180px;margin:auto;padding:25px 20px 50px}.hero h1{margin:0;font-size:23px}.hero p{color:var(--soft)}.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:20px 0}.seg{display:flex;gap:3px;padding:3px;background:var(--panel);border:1px solid var(--line);border-radius:11px}.seg.platform button[data-value=aos].on{color:var(--aos)}.seg.platform button[data-value=ios].on{color:var(--ios)}.type-grid,.result-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:15px}.type-card{background:var(--panel);color:inherit;text-align:left;border:1px solid var(--line);border-radius:14px;padding:17px;box-shadow:var(--shadow);cursor:pointer;transition:.15s}.type-card:hover{transform:translateY(-2px);border-color:var(--accent)}.type-card>div:first-child,.result-head,footer{display:flex;justify-content:space-between;gap:10px}.type-id,.tc-id,.crumb{font:700 11px var(--mono);color:var(--accent)}.total{color:var(--faint);font-size:11px}.type-card h3{margin:8px 0 2px;font-size:18px}.type-card p{color:var(--soft);min-height:40px}.counts{display:flex;gap:10px;font-size:10px}.pass-text{color:var(--pass)}.failed-text{color:var(--fail)}.blocked-text{color:var(--block)}.open{display:block;color:var(--accent);margin-top:15px;font-size:12px}.detail-bar{display:flex;align-items:center;gap:15px;margin-bottom:18px}.detail-bar h2{margin:2px 0}.back{border-color:var(--line);background:var(--panel)}.result-card{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:15px;box-shadow:var(--shadow)}.result-head>div{display:flex;flex-direction:column}.status{font:750 11px var(--mono);padding:4px 9px;border-radius:999px;height:max-content}.status.pass{color:var(--pass);background:#2f7d3a20}.status.failed{color:var(--fail);background:#c0392b20}.status.blocked{color:var(--block);background:#b5761a20}.context{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}.context span{background:var(--panel2);padding:4px 7px;border-radius:6px;font-size:11px}.answers{display:grid;grid-template-columns:1fr 1fr;gap:9px}.answers label,.platform-spec>b{font-size:10px;color:var(--faint);text-transform:uppercase}.answers pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--panel2);padding:9px;border-radius:7px;min-height:50px;font:12px var(--mono)}footer{color:var(--faint);font-size:11px}a{color:var(--accent)}.missing{color:var(--fail);text-decoration:line-through}.empty{padding:45px;text-align:center;background:var(--panel);border:1px dashed var(--line);border-radius:13px;color:var(--soft)}.catalog-head{display:flex;justify-content:space-between;align-items:end;gap:20px}.catalog-head p{color:var(--soft)}.table-wrap{overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}table{border-collapse:collapse;width:100%;min-width:980px}th,td{text-align:left;vertical-align:top;padding:14px;border-bottom:1px solid var(--line)}th{position:sticky;top:0;background:var(--panel2);font-size:11px;color:var(--faint);text-transform:uppercase}td:first-child{width:120px}.catalog-id{display:block;font:750 13px var(--mono);margin:7px 0}.draft,.priority{display:inline-block;padding:2px 6px;border-radius:5px;background:var(--accent2);color:var(--accent);font:700 9px var(--mono)}td code{color:var(--accent)}.priority{margin-left:7px}.platform-spec p,.na p{margin:3px 0 10px;color:var(--soft);min-width:250px}.na{color:var(--faint)}.meta{color:var(--faint);font-size:11px;margin-top:20px}[hidden]{display:none!important}@media(max-width:650px){main{padding:18px 12px}.top{flex-wrap:wrap}.main-nav{order:3;width:100%}.answers{grid-template-columns:1fr}}
.layer-row{display:grid;grid-template-columns:58px 26px 26px 1fr auto;gap:6px;align-items:center;padding:7px 0;border-top:1px solid var(--line);font-size:10px}.layer-row>b{font-size:11px}.layer-row small{color:var(--faint)}
.report-section{margin:22px 0 32px}.section-title{display:flex;align-items:center;gap:11px;margin-bottom:12px}.section-title>span{font:800 11px var(--mono);color:var(--accent);background:var(--accent2);padding:6px;border-radius:7px}.section-title h3,.section-title p{margin:0}.section-title p{color:var(--faint);font-size:11px}
.result-badges{align-items:flex-end;gap:5px}.priority-tag{font:800 11px var(--mono);padding:4px 8px;border-radius:999px;background:var(--accent2);color:var(--accent)}.card-tabs{display:flex;gap:4px;margin:14px 0 11px;border-bottom:1px solid var(--line)}.card-tabs button{border:0;border-bottom:2px solid transparent;background:transparent;color:var(--faint);padding:6px 9px;cursor:pointer}.card-tabs button.on{color:var(--accent);border-bottom-color:var(--accent);font-weight:750}.card-page{min-height:250px}.comparison-hero{background:var(--panel2);border-radius:12px;padding:14px;margin-bottom:10px}.comparison-pair,.comparison-triplet{display:grid;grid-template-columns:minmax(0,1fr) 34px minmax(0,1fr);align-items:center;gap:7px}.comparison-triplet{grid-template-columns:minmax(0,1fr) 22px minmax(0,1fr) 22px minmax(0,1fr)}.comparison-value{min-width:0;text-align:center;padding:13px 8px;border:1px solid var(--line);border-radius:9px;background:var(--panel)}.comparison-value label{display:block;color:var(--faint);font-size:9px;font-weight:750;letter-spacing:.05em;text-transform:uppercase}.comparison-value b{display:block;margin-top:7px;font:800 16px var(--mono);overflow-wrap:anywhere}.captured-value{border-top:3px solid var(--accent)}.actual-value{border-top:3px solid var(--pass)}.required-value{border-top:3px solid var(--block)}.comparison-operator{text-align:center;font:900 19px var(--mono);color:var(--soft)}.comparison-hero>p{margin:11px 1px 0;line-height:1.45;color:var(--soft)}.comparison-hero>p span{display:block;color:var(--faint);font-size:9px;font-weight:800;letter-spacing:.07em;text-transform:uppercase}.comparison-hero>small{display:block;margin-top:7px;color:var(--faint)}.comparison-rule-value .comparison-value{text-align:left}.blocked-comparison{display:flex;justify-content:space-between;color:var(--block)}.expected-block,.actual-block{background:var(--panel2);border-radius:9px;padding:11px 12px;margin-bottom:10px}.expected-block label,.actual-block label,.captured-block>label,.run-info label{display:block;color:var(--faint);font-size:9px;font-weight:750;letter-spacing:.08em;text-transform:uppercase}.expected-block p{margin:5px 0 0;color:var(--ink);line-height:1.6}.captured-block{border:1px solid var(--line);border-radius:10px;padding:11px;margin-bottom:10px}.captured-block>label{margin-bottom:9px}.facts{margin:5px 0 0}.facts>div{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-top:1px solid var(--line)}.facts>div:first-child{border-top:0}.facts dt{color:var(--soft)}.facts dd{margin:0;text-align:right;font:650 11px var(--mono);overflow-wrap:anywhere}.result-note{border-left:3px solid var(--block);padding:7px 10px}.result-note p{margin:3px 0}.evidence-guidance{border-left:3px solid var(--accent);background:var(--accent2);border-radius:0 9px 9px 0;padding:10px 12px;margin-bottom:10px}.evidence-guidance p{margin:4px 0 0;line-height:1.55}.evidence-image{margin:0;display:flex;flex-direction:column;align-items:center}.evidence-image img{display:block;max-width:100%;height:390px;object-fit:contain;border-radius:9px;background:#000}.evidence-image figcaption{color:var(--faint);font:10px var(--mono);margin-top:6px}.evidence-data,.evidence-text{background:var(--panel2);border-radius:9px;padding:12px;overflow:auto}.evidence-data>b{display:block;margin-bottom:8px}.evidence-missing{padding:45px 10px;text-align:center;color:var(--fail)}.run-info{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px;box-shadow:var(--shadow);margin-bottom:24px}.run-info-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.run-info-title span{font:750 10px var(--mono);color:var(--accent)}.run-info-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:9px;overflow:hidden}.run-info-grid>div{background:var(--panel2);padding:10px}.run-info-grid b{display:block;margin-top:3px;font-size:12px;overflow-wrap:anywhere}
.status-filters{display:flex;justify-content:space-between;align-items:center;gap:15px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 15px;box-shadow:var(--shadow);margin-bottom:12px}.status-filters>div:first-child{display:flex;flex-direction:column}.status-filters>div:first-child>span{font-weight:800}.status-filters small{color:var(--faint)}.status-filter-buttons{display:flex;gap:7px;flex-wrap:wrap}.status-filter{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:999px;background:var(--panel2);color:var(--soft);padding:6px 10px;cursor:pointer}.status-filter span{font:750 10px var(--mono)}.status-filter b{min-width:20px;text-align:center;border-radius:999px;background:var(--panel);padding:1px 5px}.status-filter.pass{color:var(--pass)}.status-filter.failed{color:var(--fail)}.status-filter.blocked{color:var(--block)}.status-filter.on{color:#fff;border-color:transparent}.status-filter.pass.on{background:var(--pass)}.status-filter.failed.on{background:var(--fail)}.status-filter.blocked.on{background:var(--block)}.status-filter.on b{color:var(--ink)}.status-filter:disabled{opacity:.4;cursor:not-allowed}@media(max-width:650px){.status-filters{align-items:flex-start;flex-direction:column}.status-filter-buttons{width:100%}.status-filter{flex:1;justify-content:center}}
.manual-review{margin-top:10px}.manual-review summary{display:flex;align-items:center;gap:6px;width:max-content;margin-left:auto;color:var(--faint);font:700 9px var(--mono);cursor:pointer;list-style:none}.manual-review summary::-webkit-details-marker{display:none}.manual-review summary:before{content:"＋"}.manual-review[open] summary:before{content:"−"}.manual-form{margin-top:7px;padding:9px 10px;background:var(--panel2);border:1px solid var(--line);border-radius:8px}.manual-form>small{color:var(--faint);font-size:9px}.manual-indicator{font:800 8px var(--mono);color:#fff;background:var(--block);padding:2px 5px;border-radius:999px}.manual-review label{display:block;color:var(--faint);font-size:9px;font-weight:750;margin-top:6px}.manual-review select,.manual-review textarea{display:block;width:100%;margin-top:3px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);padding:5px 6px}.manual-review textarea{resize:vertical;font:11px var(--sans)}.manual-actions{display:flex;gap:6px;margin-top:7px}.manual-actions button,.export-overrides{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:7px;padding:5px 8px;cursor:pointer}.manual-actions button:first-child{background:var(--accent);border-color:var(--accent);color:#fff}.manual-saved{margin-top:7px;padding:6px 8px;border-left:3px solid var(--block);background:var(--panel);font-size:10px;white-space:pre-wrap}.export-overrides{margin-left:auto}.theme{margin-left:0}
.comparison-pair{grid-template-columns:minmax(0,1fr) 26px minmax(0,1fr)}.comparison-triplet{grid-template-columns:minmax(0,1fr) 18px minmax(0,1fr) 18px minmax(0,1fr)}.comparison-value{padding-left:7px;padding-right:7px}.comparison-value label{font-size:8px;line-height:1.35;letter-spacing:.035em;overflow-wrap:anywhere}.comparison-value b{font-size:clamp(11px,1.05vw,15px);line-height:1.35;word-break:break-word}.comparison-operator{font-size:15px}
.evidence-zoom{display:block;max-width:100%;border:0;padding:0;background:transparent;cursor:zoom-in}.evidence-zoom img{pointer-events:none}.image-lightbox{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;padding:30px;background:#071014eb;cursor:zoom-out}.image-lightbox[hidden]{display:none}.image-lightbox img{display:block;max-width:96vw;max-height:90vh;object-fit:contain;filter:drop-shadow(0 16px 50px #000)}.image-lightbox p{position:absolute;left:24px;bottom:12px;margin:0;color:#dce7eb;font:11px var(--mono)}.image-lightbox-close{position:absolute;right:20px;top:16px;z-index:1;border:1px solid #ffffff55;border-radius:999px;background:#111c;color:#fff;width:38px;height:38px;font-size:24px;line-height:1;cursor:pointer}
.comparison-list{max-height:190px;margin:9px 0 0;padding:0;overflow:auto;list-style:none;text-align:left;border-top:1px solid var(--line)}.comparison-list li{padding:6px 3px;border-bottom:1px solid var(--line);font:650 11px/1.35 var(--mono);overflow-wrap:anywhere}.comparison-list li:last-child{border-bottom:0}
"""


SCRIPT = r"""
(function(){
 var root=document.documentElement,platform="aos",mode="standalone",activePage="reports";
 var overrideStorageKey="laf2-manual-overrides-v1",overrides={};
 try{var saved=localStorage.getItem("laf2-theme");if(saved)root.dataset.theme=saved}catch(e){}
 try{root.dataset.lang=localStorage.getItem("laf2-language")||"zh"}catch(e){root.dataset.lang="zh"}
 try{overrides=JSON.parse(localStorage.getItem(overrideStorageKey)||"{}")||{}}catch(e){overrides={}}
 document.getElementById("theme").onclick=function(){var dark=root.dataset.theme==="dark";root.dataset.theme=dark?"light":"dark";try{localStorage.setItem("laf2-theme",root.dataset.theme)}catch(e){}};
 function applyLanguage(){var zh=root.dataset.lang==="zh",button=document.getElementById("language");button.textContent=zh?"EN":"中文";button.title=zh?"Switch to English":"切換為中文";document.documentElement.lang=zh?"zh-Hant":"en";document.querySelectorAll(".type-card").forEach(function(card){var count=card.querySelectorAll(".result-card").length||Number((card.querySelector(".total")||{}).dataset&&card.querySelector(".total").dataset.resultCount)||0,total=card.querySelector(".total");if(total)total.textContent=count+(zh?" 筆結果":" results")})}
 document.getElementById("language").onclick=function(){root.dataset.lang=root.dataset.lang==="zh"?"en":"zh";try{localStorage.setItem("laf2-language",root.dataset.lang)}catch(e){}applyLanguage()};
 function persistOverrides(){try{localStorage.setItem(overrideStorageKey,JSON.stringify(overrides));return true}catch(e){alert("無法儲存 manual override："+e);return false}}
 function applyManualOverride(card){
  var item=overrides[card.dataset.overrideKey],automation=card.dataset.automationStatus,status=item&&item.status?item.status.toLowerCase():automation;
  card.dataset.resultStatus=status;
  var badge=card.querySelector(".result-badges .status");badge.classList.remove("pass","failed","blocked");badge.classList.add(status);badge.textContent=status.toUpperCase();
  var select=card.querySelector("[data-manual-status]"),reason=card.querySelector("[data-manual-reason]"),indicator=card.querySelector(".manual-indicator"),saved=card.querySelector(".manual-saved");
  select.value=item?item.status:"";reason.value=item?item.reason:"";indicator.hidden=!item;saved.hidden=!item;
  if(item)saved.textContent=item.status+" — "+item.reason+"\nUpdated "+item.updated_at;
 }
 function refreshCounts(){
  document.querySelectorAll(".slot-detail").forEach(function(detail){
   var cards=Array.from(detail.querySelectorAll(".result-card")),counts={pass:0,failed:0,blocked:0};cards.forEach(function(card){counts[card.dataset.resultStatus]=(counts[card.dataset.resultStatus]||0)+1});
   detail.querySelectorAll("[data-status-filter]").forEach(function(button){var count=counts[button.dataset.statusFilter]||0;button.querySelector("b").textContent=count;button.disabled=count===0});
   applyStatusFilter(detail,detail.dataset.statusFilter||"");
   var typeCard=Array.from(document.querySelectorAll(".type-card")).find(function(card){return card.dataset.slot===detail.dataset.slot});
   if(typeCard){var total=typeCard.querySelector(".total");total.dataset.resultCount=cards.length;total.textContent=cards.length+(root.dataset.lang==="zh"?" 筆結果":" results");["e2e","signal"].forEach(function(layer){var rows=cards.filter(function(card){return card.dataset.layer===layer}),row=typeCard.querySelector('[data-layer-row="'+layer+'"]'),layerCounts={pass:0,failed:0,blocked:0};rows.forEach(function(card){layerCounts[card.dataset.resultStatus]++});row.querySelector(".pass-text").textContent=layerCounts.pass+"✓";row.querySelector(".failed-text").textContent=layerCounts.failed+"✗";row.querySelector(".blocked-text").textContent=layerCounts.blocked+"▲";row.querySelector("small").textContent=rows.length+" TC"})}
  })
 }
 document.querySelectorAll(".main-nav button").forEach(function(b){b.onclick=function(){activePage=b.dataset.page;document.querySelectorAll(".main-nav button").forEach(function(x){x.classList.toggle("on",x===b)});document.querySelectorAll(".app-page").forEach(function(p){p.hidden=p.id!==activePage+"-page"});if(activePage==="reports")showOverview()}});
 function select(group,value){document.querySelectorAll('.seg.'+group+' button').forEach(function(b){b.classList.toggle("on",b.dataset.value===value)})}
 function update(){select("platform",platform);select("mode",mode);document.querySelectorAll(".type-card").forEach(function(c){c.hidden=!c.dataset.slot.startsWith(platform+":"+mode+":")});document.getElementById("result-context").textContent=(platform==="aos"?"AOS":"iOS")+" · "+(mode==="standalone"?"Standalone":"Mediation")}
 document.querySelectorAll(".seg.platform button").forEach(function(b){b.onclick=function(){platform=b.dataset.value;showOverview();update()}});
 document.querySelectorAll(".seg.mode button").forEach(function(b){b.onclick=function(){mode=b.dataset.value;showOverview();update()}});
 var overview=document.getElementById("slot-overview"),details=document.querySelectorAll(".slot-detail");
 function showOverview(){details.forEach(function(d){d.hidden=true});overview.hidden=false}
 function applyStatusFilter(detail,status){detail.dataset.statusFilter=status;detail.querySelectorAll("[data-status-filter]").forEach(function(button){button.classList.toggle("on",button.dataset.statusFilter===status)});detail.querySelectorAll(".report-section").forEach(function(section){var cards=Array.from(section.querySelectorAll(".result-card")),matches=cards.filter(function(card){return !status||card.dataset.resultStatus===status});cards.forEach(function(card){card.hidden=!!status&&card.dataset.resultStatus!==status});section.hidden=!!status&&matches.length===0})}
 document.querySelectorAll("[data-status-filter]").forEach(function(button){button.onclick=function(){var detail=button.closest(".slot-detail"),next=detail.dataset.statusFilter===button.dataset.statusFilter?"":button.dataset.statusFilter;applyStatusFilter(detail,next)}});
 document.querySelectorAll(".type-card").forEach(function(c){c.onclick=function(){overview.hidden=true;details.forEach(function(d){var active=d.dataset.slot===c.dataset.slot;d.hidden=!active;if(active)applyStatusFilter(d,"")});scrollTo(0,0)}});
 document.querySelectorAll(".back").forEach(function(b){b.onclick=function(){showOverview();scrollTo(0,0)}});
 document.querySelectorAll("[data-card-tab]").forEach(function(button){button.onclick=function(){var card=button.closest(".result-card"),target=button.dataset.cardTab;card.querySelectorAll("[data-card-tab]").forEach(function(item){item.classList.toggle("on",item===button)});card.querySelectorAll("[data-card-page]").forEach(function(page){page.hidden=page.dataset.cardPage!==target})}});
 document.querySelectorAll("[data-manual-save]").forEach(function(button){button.onclick=function(){var card=button.closest(".result-card"),status=card.querySelector("[data-manual-status]").value,reason=card.querySelector("[data-manual-reason]").value.trim();if(!status){delete overrides[card.dataset.overrideKey];persistOverrides();applyManualOverride(card);refreshCounts();return}if(!reason){alert("Manual override 必須填寫理由");card.querySelector("[data-manual-reason]").focus();return}overrides[card.dataset.overrideKey]={status:status,reason:reason,updated_at:new Date().toISOString(),automation_status:card.dataset.automationStatus.toUpperCase()};if(persistOverrides()){applyManualOverride(card);refreshCounts()}}});
 document.querySelectorAll("[data-manual-reset]").forEach(function(button){button.onclick=function(){var card=button.closest(".result-card");delete overrides[card.dataset.overrideKey];if(persistOverrides()){applyManualOverride(card);refreshCounts()}}});
 document.getElementById("export-overrides").onclick=function(){var payload={schema_version:1,exported_at:new Date().toISOString(),page:location.href,overrides:overrides},blob=new Blob([JSON.stringify(payload,null,2)+"\n"],{type:"application/json"}),url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download="lazyadfinder2-manual-overrides.json";a.click();setTimeout(function(){URL.revokeObjectURL(url)},1000)};
 document.querySelectorAll(".result-card").forEach(applyManualOverride);refreshCounts();
 var lightbox=document.getElementById("image-lightbox"),lightboxImage=lightbox.querySelector("img"),lightboxCaption=lightbox.querySelector("p");
 function closeLightbox(){lightbox.hidden=true;lightboxImage.removeAttribute("src");document.body.style.overflow=""}
 document.querySelectorAll(".evidence-zoom").forEach(function(button){button.onclick=function(){var source=button.querySelector("img");lightboxImage.src=source.src;lightboxImage.alt=source.alt;lightboxCaption.textContent=source.alt;lightbox.hidden=false;document.body.style.overflow="hidden"}});
 lightbox.onclick=function(e){if(e.target===lightbox||e.target===lightboxImage)closeLightbox()};lightbox.querySelector("button").onclick=closeLightbox;
 addEventListener("keydown",function(e){if(e.key==="Escape"){if(!lightbox.hidden)closeLightbox();else showOverview()}});applyLanguage();update();
})();
"""


def render(verdicts, captures, verdict_files, evidence_dirs, catalog):
    cards, details = [], []
    catalog_by_key = {str(row["key"]): row for row in catalog}
    for platform, _plabel, _device in PLATFORMS:
        for mode, _mlabel in MODES:
            for kind, label, description in TYPES:
                rows = [row for row in verdicts if row["platform"] == platform and row["mode_group"] == mode and row["test_type"] == kind]
                cards.append(_slot_card(platform, mode, kind, label, description, rows))
                details.append(_slot_detail(platform, mode, kind, label, rows, catalog_by_key))
    counts = Counter(row["status"] for row in verdicts)
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    roots = "、".join(html.escape(str(Path(root).expanduser())) for root in evidence_dirs)
    return f'''<!doctype html><html lang="zh-Hant" data-lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LazyAdFinder2</title><style>{CSS}</style></head><body>
<header class="top"><div class="brand">SDK QA Platform<small>LazyAdFinder2</small></div><nav class="main-nav"><button class="on" data-page="reports">{_bi("Round Reports", "輪次報告")}</button><button data-page="catalog">{_bi("TestCase Catalog", "TestCase 目錄")}</button></nav><button class="export-overrides" id="export-overrides">{_bi("Export overrides", "匯出人工覆寫")}</button><button class="language" id="language">EN</button><button class="theme" id="theme">◐</button></header>
<main><section class="app-page" id="reports-page"><div id="slot-overview"><div class="hero"><h1>{_bi("Round Reports", "輪次報告")}</h1><p>{_bi("Each Round contains Signal and E2E results. Select a platform and integration mode, then open AIBID, REEN Static, or REEN Dynamic.", "一個 Round 同時包含 Signal 與 E2E。先選平台與整合模式，再進入 AIBID／REEN Static／REEN Dynamic。")}</p></div>
<div class="controls"><div class="seg platform"><button class="on" data-value="aos">AOS</button><button data-value="ios">iOS</button></div><div class="seg mode"><button class="on" data-value="standalone">Standalone</button><button data-value="mediation">Mediation</button></div><b id="result-context"></b></div>
<div class="type-grid">{"".join(cards)}</div></div>{"".join(details)}</section>
<section class="app-page" id="catalog-page" hidden><div class="catalog-head"><div><h1>{_bi("TestCase Catalog", "TestCase 目錄")}</h1><p>{_bi("All Signal and E2E TestCases. A Draft is not a test result; only a Verdict can be PASS, FAILED, or BLOCKED.", "整理 Signal 與 E2E 的全部 TC；Draft 不是測試結果，只有 Verdict 才會是 PASS／FAILED／BLOCKED。")}</p></div><b>{len(catalog)} TestCases</b></div>{_catalog_table(catalog, catalog_by_key)}</section>
<p class="meta">{_bi("Results", "結果")}: {len(verdicts)} · PASS {counts[Status.PASS.value]} · FAILED {counts[Status.FAILED.value]} · BLOCKED {counts[Status.BLOCKED.value]}<br>{_bi("Raw captures", "原始擷取")}: {len(captures)} · {_bi("Verdict files", "Verdict 檔案")}: {len(verdict_files)} · {_bi("Generated", "產生時間")}: {html.escape(generated)}<br>Evidence roots: {roots or '—'}</p></main>
<div class="image-lightbox" id="image-lightbox" role="dialog" aria-modal="true" aria-label="Evidence full image" hidden><button class="image-lightbox-close" type="button" aria-label="Close">×</button><img alt=""><p></p></div><script>{SCRIPT}</script></body></html>'''


def write_report(output, content):
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as stream:
        temporary = Path(stream.name); stream.write(content)
    os.replace(temporary, output)
    return output


def _origin_url():
    try:
        return subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError as exc:
        raise ReportError("Git remote 'origin' is not configured; cannot publish") from exc


def _pages_url(remote):
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", remote)
    return f"https://{match.group(1)}.github.io/{match.group(2)}/" if match else ""


def publish(evidence_dirs, catalog_path=DEFAULT_CATALOG, remote=None, open_page=True):
    remote = remote or _origin_url()
    verdicts, captures, verdict_files = discover(evidence_dirs)
    catalog = load_catalog(catalog_path)
    verdicts = current_verdicts(verdicts, catalog)
    document = render(verdicts, captures, verdict_files, evidence_dirs, catalog)
    with tempfile.TemporaryDirectory(prefix="lazyadfinder2-pages-") as temp:
        checkout = Path(temp) / "pages"
        exists = subprocess.run(["git", "ls-remote", "--exit-code", "--heads", remote, "gh-pages"], text=True, capture_output=True).returncode == 0
        if exists:
            subprocess.run(["git", "clone", "--depth", "1", "--branch", "gh-pages", remote, str(checkout)], check=True)
        else:
            subprocess.run(["git", "clone", "--depth", "1", remote, str(checkout)], check=True)
            subprocess.run(["git", "switch", "--orphan", "gh-pages"], cwd=checkout, check=True)
            subprocess.run(["git", "rm", "-rf", "--ignore-unmatch", "."], cwd=checkout, check=True, stdout=subprocess.DEVNULL)
        (checkout / "index.html").write_text(document, encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=checkout, check=True)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=checkout).returncode != 0
        if changed:
            subprocess.run(["git", "commit", "-m", f"publish: QA report {datetime.now():%Y-%m-%d %H:%M:%S}"], cwd=checkout, check=True)
            subprocess.run(["git", "push", "origin", "HEAD:gh-pages"], cwd=checkout, check=True)
        published_revision = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=checkout, text=True
        ).strip()
    url = _pages_url(remote)
    public_url = f"{url}?build={published_revision}" if url else remote
    print(f"[publish] {'updated' if changed else 'unchanged'} · {public_url}")
    if url and open_page and os.environ.get("OPEN_PAGES", "1") != "0":
        subprocess.run(["open", public_url], check=False)
    return public_url


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", nargs="+", default=[str(ROOT / "evidence")])
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--out", default="report.html")
    parser.add_argument("--local", action="store_true", help="render and open the local report without publishing")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.publish:
        publish(args.evidence, args.catalog, open_page=not args.no_open); return 0
    verdicts, captures, verdict_files = discover(args.evidence)
    catalog = load_catalog(args.catalog)
    verdicts = current_verdicts(verdicts, catalog)
    output = write_report(args.out, render(verdicts, captures, verdict_files, args.evidence, catalog))
    print(f"[report] {output} · catalog={len(catalog)} verdicts={len(verdicts)} captures={len(captures)}")
    if args.local and not args.no_open:
        subprocess.run(["open", output.as_uri()], check=False)
        print(f"[local] {output.as_uri()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as exc:
        print(f"[error] {exc}", file=sys.stderr); raise SystemExit(1)
