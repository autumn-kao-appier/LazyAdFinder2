#!/usr/bin/env python3
"""Render the LazyAdFinder result platform and TestCase catalog."""

import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from campaign_testcases import supports as campaign_supports
from verdict import Status


ROOT = Path(__file__).parent
VERDICTS_FILE = "verdicts.json"
SUMMARY_FILE = "summary.json"
LEGACY_METADATA_FILE = "metadata.json"
DEFAULT_CATALOG = ROOT / "testcases" / "testcase_catalog.json"
VALID_STATUSES = {status.value for status in Status}
STATUS_ORDER = (Status.FAILED.value, Status.BLOCKED.value, Status.PASS.value)
UNEXECUTED = "UNEXECUTED"
PLATFORMS = (("aos", "AOS", "Android"), ("ios", "iOS", "Apple"))
MODES = (("standalone", "Standalone"), ("mediation", "Mediation"))
TYPES = (
    ("aibid", "AIBID", "首購 / 新客競價"),
    ("reen-static", "REEN Static", "再行銷 · 靜態素材"),
    ("reen-dynamic", "REEN Dynamic", "再行銷 · 動態素材"),
)
TC_ALIASES = {
    "ios-ipv6-launch": "ipv6-refresh-launch",
    "ios-ipv6-wifi-switch": "ipv6-refresh-wifi-switch",
    "ios-ipv6-recovery": "ipv6-refresh-recovery",
    "ios-ipv6-debounce": "ipv6-refresh-debounce",
    "ios-ipv6-slow-network": "ipv6-refresh-slow-network",
}

_REPORT_ASSETS = {}
_REPORT_ASSET_PREFIX = "assets"


class ReportError(RuntimeError):
    pass


def _load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except OSError as exc:
        raise ReportError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReportError(f"Invalid JSON in {path}: {exc}") from exc


def _begin_report_assets(prefix):
    global _REPORT_ASSETS, _REPORT_ASSET_PREFIX
    _REPORT_ASSETS = {}
    _REPORT_ASSET_PREFIX = str(prefix).strip("/") or "assets"


def _register_report_asset(path):
    path = Path(path).resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:20]
    suffix = path.suffix.lower() or ".bin"
    name = f"{digest}{suffix}"
    _REPORT_ASSETS[name] = path
    return f"{_REPORT_ASSET_PREFIX}/{name}"


def _write_report_assets(directory):
    directory = Path(directory)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, source in _REPORT_ASSETS.items():
        shutil.copy2(source, directory / name)


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


def current_verdicts(verdicts, catalog, captures=()):
    """Return the latest result per registered TC and report slot.

    Evidence discovery intentionally reads all historical runs.  The current
    report must not turn those runs, or retired legacy IDs, into extra TC cards.
    """
    registered = {str(row["key"]) for row in catalog}
    explicit_runs = {}
    for row in verdicts:
        if row.get("coverage_only") or not row.get("test_run_id"):
            continue
        slot = (row["platform"], row["mode_group"], row["test_type"])
        current = explicit_runs.get(slot)
        if current is None or row["captured_at"] >= current[0]:
            explicit_runs[slot] = (row["captured_at"], row["run_group"])
    latest = {}
    for row in verdicts:
        if row.get("coverage_only") or row["tc"] not in registered:
            continue
        slot = (row["platform"], row["mode_group"], row["test_type"])
        selected = explicit_runs.get(slot)
        if selected is not None and row.get("run_group") != selected[1]:
            continue
        key = (row["platform"], row["mode_group"], row["test_type"], row["tc"])
        previous = latest.get(key)
        if previous is None or row["captured_at"] >= previous["captured_at"]:
            latest[key] = row
    skipped_at = {}
    for folder in captures:
        summary_path = Path(folder) / SUMMARY_FILE
        if not summary_path.is_file():
            continue
        summary = _load_json(summary_path)
        if summary.get("result") != "SKIPPED":
            continue
        mode = _mode_group(summary.get("test_mode", ""))
        platform = str(summary.get("platform", "")).strip().lower()
        test_type = str(summary.get("test_type", "")).strip().lower()
        slot = (platform, mode, test_type)
        selected = explicit_runs.get(slot)
        summary_run_group = str(summary.get("test_run_id", "")).strip() or str(Path(folder).parent.resolve())
        if selected is not None and summary_run_group != selected[1]:
            continue
        captured_at = str(summary.get("finished_at") or summary.get("started_at", ""))
        for tc in summary.get("skipped_testcases", []):
            key = (platform, mode, test_type, str(tc))
            if captured_at >= skipped_at.get(key, ""):
                skipped_at[key] = captured_at
    for key, captured_at in skipped_at.items():
        previous = latest.get(key)
        if previous is not None and captured_at >= previous["captured_at"]:
            del latest[key]
    return list(latest.values())


def current_skip_reasons(captures):
    """Return per-TC SKIP reasons from the newest run in each report slot."""
    runs = {}
    for folder in captures:
        summary_path = Path(folder) / SUMMARY_FILE
        if not summary_path.is_file():
            continue
        summary = _load_json(summary_path)
        slot = (
            str(summary.get("platform", "")).strip().lower(),
            _mode_group(summary.get("test_mode", "")),
            str(summary.get("test_type", "")).strip().lower(),
        )
        run_id = str(summary.get("test_run_id", "")).strip() or str(Path(folder).parent.resolve())
        started = str(summary.get("test_run_started_at") or summary.get("started_at") or "")
        record = runs.setdefault((slot, run_id), {"started": started, "summaries": []})
        record["started"] = max(record["started"], started)
        record["summaries"].append(summary)
    selected = {}
    for (slot, _run_id), record in runs.items():
        if slot not in selected or record["started"] > selected[slot]["started"]:
            selected[slot] = record
    result = {}
    for slot, record in selected.items():
        for summary in record["summaries"]:
            if summary.get("result") != "SKIPPED":
                continue
            reason = str(summary.get("skip_reason") or "Scenario was skipped by the execution plan")
            for testcase in summary.get("skipped_testcases", []):
                result[(*slot, str(testcase))] = reason
    return result


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
            if row.get("expected") is not None:
                raise ReportError(f"{path}: BLOCKED verdict {tc} cannot claim an expected answer")
        elif not isinstance(evidence, str) or not evidence.strip():
            raise ReportError(f"{path}: evaluated verdict {tc} requires evidence")
        test_mode = str(config.get("test_mode", "")).strip().lower()
        canonical_tc = TC_ALIASES.get(tc.strip(), tc.strip())
        if evidence == "bid_decoded.json and captured Evidence artifacts":
            evidence = "evidence-errors.json" if (path.parent / "evidence-errors.json").is_file() else "summary.json"
        actual = row.get("actual")
        if isinstance(actual, str):
            legacy_missing = re.search(r"No such file or directory: ['\"](.+?)['\"]", actual)
            if legacy_missing:
                actual = {
                    "error": "Required Evidence was not produced",
                    "missing_artifact": Path(legacy_missing.group(1)).name,
                }
        normalized.append({
            "tc": canonical_tc, "title": str(row.get("title", tc)).strip() or canonical_tc,
            "description": str(row.get("description", "")).strip(), "status": status,
            "reason": reason, "expected": row.get("expected"), "actual": actual,
            "comparison_view": row.get("comparison_view") if isinstance(row.get("comparison_view"), dict) else None,
            "evidence": evidence, "source": path, "platform": _platform_of(metadata, path),
            "test_mode": test_mode, "mode_group": _mode_group(test_mode),
            "test_type": str(config.get("test_type", "")).strip().lower(),
            "captured_at": str(metadata.get("captured_at") or metadata.get("finished_at", "")),
            "started_at": str(metadata.get("started_at", "")),
            "finished_at": str(metadata.get("finished_at") or metadata.get("captured_at", "")),
            "automation_started_at": str(metadata.get("automation_started_at", "")),
            "automation_finished_at": str(metadata.get("automation_finished_at", "")),
            "run_root": str(path.parent.parent.resolve()),
            "test_run_id": str(metadata.get("test_run_id", "")).strip(),
            "test_run_started_at": str(metadata.get("test_run_started_at", "")).strip(),
            "coverage_only": bool(metadata.get("coverage_only", False)),
            "run_group": str(metadata.get("test_run_id", "")).strip() or str(path.parent.parent.resolve()),
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
    return _apply_standalone_privacy_coverage(verdicts), captures, verdict_files


def _apply_standalone_privacy_coverage(verdicts):
    """Reuse same-suite Standalone R5-1 Signal results on AIBID Mediation cards."""
    privacy_keys = {"advertising-id-opt-out", "tracking-denied"}
    standalone = {}
    for row in verdicts:
        if (
            row.get("tc") in privacy_keys
            and row.get("platform") == "aos"
            and row.get("mode_group") == "standalone"
            and row.get("test_type") == "aibid"
            and row.get("status") in {Status.PASS.value, Status.FAILED.value}
        ):
            key = (row.get("run_group"), row.get("tc"))
            previous = standalone.get(key)
            if previous is None or row.get("captured_at", "") >= previous.get("captured_at", ""):
                standalone[key] = row
    linked = []
    for row in verdicts:
        candidate = standalone.get((row.get("run_group"), row.get("tc")))
        if not (
            candidate
            and row.get("platform") == "aos"
            and row.get("mode_group") == "mediation"
            and row.get("test_type") == "aibid"
            and row.get("tc") in privacy_keys
            and row.get("status") == Status.BLOCKED.value
        ):
            linked.append(row)
            continue
        inherited = dict(row)
        inherited.update({
            "status": candidate["status"],
            "reason": "Verified by the same suite's Standalone R5-1 Android SDK Signal evidence; no GAID-denied AdMob Mediation request was sent.",
            "expected": candidate.get("expected"),
            "actual": candidate.get("actual"),
            "comparison_view": candidate.get("comparison_view"),
            "evidence": candidate.get("evidence"),
            "source": candidate.get("source"),
            "coverage_source": "Standalone R5-1 · Shared Android SDK Signal",
        })
        linked.append(inherited)
    return linked


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
    "visible_tracking_allowed": "設定頁：允許廣告追蹤",
    "req_limit_ad_tracking": "Request：限制追蹤旗標",
    "ext_limit_ad_tracking": "Extended：限制追蹤旗標",
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
    "default-language-iso": "系統語言代碼", "default-language-bcp47": "系統語言與地區標籤",
    "keyboard-languages": "已安裝的鍵盤語言", "root-status": "Root 狀態",
    "emulator-detection": "模擬器偵測", "ipv6-address": "IPv6 位址", "connection-type": "連線類型",
    "ipv6-refresh-launch": "App 啟動時取得 IPv6",
    "ipv6-refresh-wifi-switch": "Wi-Fi 切換後更新 IPv6",
    "ipv6-refresh-recovery": "網路恢復後更新 IPv6",
    "ipv6-refresh-debounce": "快速切換 Wi-Fi 後的 IPv6",
    "ipv6-refresh-slow-network": "慢速網路下更新 IPv6",
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
    "standalone-landing": "Campaign 指定目的地", "standalone-privacy": "隱私資訊",
    "standalone-install-attribution": "MMP Click Action",
    "standalone-attribution-reconciliation": "歸因認列",
    "admob-sdk-init": "SDK 初始化", "admob-pubsetting": "AdMob Mediation 設定",
    "admob-gma-request": "AdMob GMA 請求與 Mediation 分流",
    "admob-appier-ad-request": "Appier Adapter 廣告請求",
    "admob-creative-render": "Mediation 素材載入與渲染",
    "admob-impression": "AdMob 曝光回報",
    "admob-fill-result": "Mediation Fill 結果",
    "admob-click": "AdMob 點擊回報",
    "admob-landing-privacy": "Mediation Landing 與隱私資訊",
    "last-foreground-times": "最近前景時間", "last-background-times": "最近背景時間",
    "impression-history": "曝光紀錄", "vpn-status": "VPN 狀態", "argus-sdk-version": "Argus SDK 版本",
    "network-latency": "網路延遲",
    "advertising-id-opt-out": "廣告識別碼 — 拒絕追蹤",
    "tracking-denied": "廣告追蹤 — 已拒絕",
    "dark-mode-enabled": "深色模式 — 已開啟",
    "font-scale-maximum": "字型縮放 — 最大",
    "screen-brightness-minimum": "螢幕亮度 — 最低",
    "output-volume-muted": "輸出音量 — 靜音",
    "battery-saver-enabled": "省電模式 — 已開啟",
    "screen-brightness-maximum": "螢幕亮度 — 最高",
    "output-volume-maximum": "輸出音量 — 最大",
    "timezone-changed": "時區 — 已變更",
    "location-permission-denied": "定位權限 — 已拒絕",
}


def _bi(en, zh):
    return (
        f'<span class="lang-en">{html.escape(str(en))}</span>'
        f'<span class="lang-zh">{html.escape(str(zh))}</span>'
    )


DYNAMIC_ZH = {
    "Actual SDK Payload": "解碼後的 Bid Request",
    "Decoded Bid Request": "解碼後的 Bid Request",
    "Evidence status": "Evidence 狀態",
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
    "Settings: tracking allowed": "設定頁：允許廣告追蹤",
    "LAT inverse flag · req/ext": "LAT 反向旗標 · Request／Extended",
    "Tracking is allowed when Opt out is OFF; because LAT means Limit Ad Tracking, its inverse flag must be 0 or absent.": "Opt out 關閉代表允許追蹤；LAT 是 Limit Ad Tracking 的反向旗標，因此必須為 0 或不傳送。",
    "Waiting for a reviewer to enter the expected SDK version in the report": "等待 reviewer 在報告輸入預期 SDK 版號。",
    "Waiting for a reviewer to enter the expected Argus SDK version in the report": "等待 reviewer 在報告輸入預期 Argus SDK 版號。",
    "Enter the intended build SDK version; an exact match passes and a mismatch fails.": "輸入本次 Build 應使用的 SDK 版號；完全相同為 PASS，不同為 FAILED。",
    "Enter the intended Argus SDK version; an exact match passes and a mismatch fails.": "輸入本次整合應使用的 Argus SDK 版號；完全相同為 PASS，不同為 FAILED。",
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
    "Environment prerequisite unavailable: no successful Appier adx6 IPv6 probe was captured": "環境前提不足：本輪沒有成功擷取 Appier adx6 IPv6 probe。",
    "AOS obtains public IPv6 from the Appier adx6 net endpoint.": "AOS 從 Appier adx6 net endpoint 取得公網 IPv6。",
    "Hardware limitation: QA device has no active SIM; 4G/5G transport cannot be established": "硬體限制：QA 裝置沒有 active SIM，無法建立 4G／5G 連線。",
    "Round limitation: the SDK latency probe endpoint timing is not captured independently yet": "本輪限制：尚未獨立擷取 SDK latency probe endpoint 的計時證據。",
    "Sample App limitation: setForceGDPRApplies(true) is not exposed or invoked": "Sample App 限制：目前沒有提供或呼叫 setForceGDPRApplies(true)。",
    "req_langb, langb do not match Android locale tag": "req.langb 與 ext.langb 未符合 Android 系統地區語言標籤。",
    "req_langb, langb do not match the primary Android system language tag": "req.langb 與 ext.langb 不符合 Android 設定頁的第一順位系統語言標籤。",
    "req_langb, langb do not match the current App language tag": "req.langb 與 ext.langb 不符合 App 當前語言與地區標籤。",
    "lang does not match the current App language code": "device.lang 不符合 App 當前語言代碼。",
    "Current App language code": "App 當前語言代碼",
    "Current App language and region": "App 當前語言與地區",
    "Visible primary Android language": "Android 設定頁的第一順位語言",
    "Visible system language code": "設定頁顯示的系統語言代碼",
    "Visible system language and region": "設定頁顯示的系統語言與地區",
    "The extended ISO-639-1 code must equal the language component of the primary language shown in Android Settings.": "Extended 的 ISO-639-1 代碼必須等於 Android 設定頁第一順位語言的語言部分。",
    "Request and extended BCP 47 tags must equal the primary language and region shown in Android Settings.": "Request 與 Extended 的 BCP 47 標籤必須等於 Android 設定頁顯示的第一順位語言與地區。",
    "The extended ISO-639-1 code must equal the low-precision language component of the current App locale.": "Extended 的 ISO-639-1 代碼必須等於 App 當前語言的低精度語言部分。",
    "Request and extended BCP 47 tags must equal the precise language and region of the current App locale.": "Request 與 Extended 的 BCP 47 標籤必須等於 App 當前語言與地區的高精度標籤。",
    "lang does not match the primary Android system language code": "device.lang 不符合 Android 設定頁第一順位的系統語言代碼。",
    "Platform definition: Android Ads SDK has no standalone Init endpoint; keep BLOCKED while deciding whether AOS needs an equivalent contract aligned with the iOS Init flow": "平台定義：Android Ads SDK 沒有獨立的 Standalone Init endpoint；此項維持 BLOCKED，等待確認是否需要建立與 iOS Init 流程對齊的 AOS 等效契約。",
    "Verified by the same suite's Standalone R5-1 Android SDK Signal evidence; no GAID-denied AdMob Mediation request was sent.": "已使用同一輪 Standalone R5-1 的 Android SDK Signal Evidence 完成驗證；沒有在 GAID 拒絕狀態下送出 AdMob Mediation request。",
    "Capture limitation: no preserved proxy transaction proves that POST /v2/sdk/aos/ad request and response belong to the same flow": "擷取限制：目前沒有保存可證明 POST /v2/sdk/aos/ad request 與 response 屬於同一個 flow 的 proxy transaction。",
    "Capture limitation: the proxy flow exists, but its request or response body was not preserved": "擷取限制：已取得 proxy flow，但沒有完整保存 request 或 response body。",
    "Capture limitation: no image response metadata was preserved; asset HTTP and MIME checks could not run": "擷取限制：沒有保存圖片 response metadata，因此無法執行素材 HTTP 與 MIME 檢查。",
    "Manual visual review remains: all captured image responses passed transport checks, but the screenshot must confirm that the rendered ad has no broken or mismatched asset": "仍需人工檢視：已擷取的圖片 response 均通過傳輸檢查，但仍須用實機截圖確認廣告沒有破圖或素材不符。",
    "Manual visual review required: screenshot.png must be compared with bid_response.json for text, CTA, images, privacy icon, ad label, clipping, and layout": "需要人工檢視：必須將 screenshot.png 與 bid_response.json 對照，確認文字、CTA、圖片、Privacy icon、廣告標示、裁切與版面配置。",
    "Capture limitation: the proxy session does not yet contain both show_cb and winshowimg responses": "擷取限制：proxy session 尚未同時包含 show_cb 與 winshowimg response。",
    "Not executed: a cost-bearing real ad click requires explicit manual confirmation": "尚未執行：真實廣告點擊可能產生費用，必須先取得明確的人工確認。",
    "Not executed: landing validation requires the confirmed click step": "尚未執行：Landing 驗證必須先完成已確認的廣告點擊步驟。",
    "Not executed: privacy icon interaction is not automated in this capture": "尚未執行：本次擷取尚未自動操作 Privacy icon。",
    "Not executed: install attribution requires a coordinated attribution window": "尚未執行：Install attribution 需要事先協調歸因測試窗口。",
    "Not executed: backend reconciliation requires completed install attribution and internal-system access": "尚未執行：後台歸因核對需要先完成 install attribution，並取得內部系統查詢權限。",
    "The proxy traffic session proves a POST /v2/sdk/aos/ad transaction and preserves both bodies from the same flow.": "Proxy traffic session 已證明存在 POST /v2/sdk/aos/ad transaction，並保存同一個 flow 的 request 與 response body。",
    "The complete Appier impression callback chain was captured.": "已擷取完整的 Appier impression callback chain。",
    "At least one response-specified creative asset was not captured or failed its transport contract.": "至少一個 response 指定的廣告素材未被擷取，或未通過傳輸契約檢查。",
    "The response-specified creative assets either loaded successfully in traffic or were proven as rendered cached views in the saved screenshot.": "Response 指定的廣告素材已在流量中成功載入，或由保存的截圖證明為已渲染的快取畫面。",
    "The CTA interaction emitted an xclk whose correlation IDs match the visible impression, and preserved its response.": "CTA 點擊已送出 xclk，其 correlation IDs 與畫面曝光的廣告一致，並保存了 response。",
    "The traffic lookup key was captured automatically. MMP install-click verification still requires the MMP action query.": "已自動保存流量查詢鍵；MMP install click 仍需透過 MMP action query 驗證。",
    "The traffic lookup key was captured automatically. MMP re-engagement-click verification still requires the MMP action query.": "已自動保存流量查詢鍵；MMP re-engagement click 仍需透過 MMP action query 驗證。",
    "The traffic lookup key was captured automatically. install attribution recognition still requires Spark/MMP reconciliation.": "已自動保存流量查詢鍵；Install 歸因認列仍需完成 Spark／MMP 對帳。",
    "The traffic lookup key was captured automatically. re-engagement attribution recognition still requires Spark/MMP reconciliation.": "已自動保存流量查詢鍵；Re-engagement 歸因認列仍需完成 Spark／MMP 對帳。",
    "Mediation-only validator is not implemented yet; the shared S baseline is evaluated separately in the same Round": "Mediation-only validator 尚未實作；同一輪仍會另外執行並判定共用的 S baseline。",
    "Sample App limitation: process Locale.getDefault().toLanguageTag() is not exposed; persist.sys.locale is only supporting context and cannot be the expected answer": "Sample App 限制：目前沒有輸出 process 的 Locale.getDefault().toLanguageTag()；persist.sys.locale 只能作為輔助資訊，不能當作 Expected 答案。",
    "Dependency blocked: the specified CID has not been proven by the Appier ad request flow": "相依條件未通過：Appier ad request flow 尚未證明本次廣告來自指定 CID。",
    "Capture limitation: the specified CID was confirmed, but no rendered-ad screenshot was saved": "擷取限制：已確認指定 CID，但沒有保存廣告渲染截圖。",
    "The specified CID was proven by the preceding traffic flow and the rendered ad was preserved as visible screenshot evidence.": "前一個 traffic flow 已證明指定 CID，並已將實機顯示的廣告保存為人眼可見的截圖 Evidence。",
    "The recorded CTA interaction emitted an xclk whose correlation IDs match the visible impression, and preserved its response.": "錄製的 CTA 點擊已送出 xclk，其 correlation IDs 與畫面曝光的廣告一致，並保存了 response。",
    "FAILED: the E2E round does not prove the CTA click with visible interaction evidence and an xclk matching the visible impression.": "FAILED：本輪 E2E 沒有以可見互動 Evidence 與符合畫面曝光廣告的 xclk 共同證明 CTA 點擊。",
    "The visible native ad was reviewed for response elements, Ad label, assets, clipping, and layout.": "已人工檢視 Native ad 的 response 元素、Ad label、素材、裁切與版面配置。",
    "The response and screenshot were saved, and the rendered View tree matches every text or asset actually returned by the response. Pixel quality, clipping, and layout remain visible for human review.": "已保存 response 與截圖，且畫面 View tree 對應 response 實際回傳的每項文字與素材；圖片品質、裁切與版面仍保留在截圖中供人眼覆核。",
    "FAILED: the saved screenshot or rendered View tree does not satisfy the objective response-to-UI contract; see visual-review.json for the exact failed check.": "FAILED：保存的截圖或畫面 View tree 未符合客觀的 response-to-UI 契約；請在 visual-review.json 查看實際失敗項目。",
    "The traffic lookup key was captured automatically. Install and first-open verification still require a coordinated attribution window.": "已自動保存流量查詢鍵；安裝與首次開啟驗證仍需協調歸因測試窗口。",
    "The traffic lookup key was captured automatically. Spark/MMP reconciliation still requires internal-system access and a completed attribution action.": "已自動保存流量查詢鍵；Spark／MMP 對帳仍需內部系統權限與已完成的歸因操作。",
    "The click redirect completed and the final external destination was preserved as visible evidence.": "Click redirect 已完成，並將最終外部 Landing destination 保存為人眼可見的 Evidence。",
    "FAILED: the E2E round does not preserve a proven final landing destination after the tracked click.": "FAILED：本輪 E2E 沒有保存可證明 tracked click 最終 Landing destination 的 Evidence。",
    "The tracked ad click opened the campaign's required destination and preserved it as visible evidence.": "已追蹤的廣告點擊成功開啟此 Campaign 指定的目的地，並保存人眼可見的 Evidence。",
    "FAILED: the tracked click did not prove the campaign's required destination; REEN must open the configured target App.": "FAILED：點擊結果未證明到達 Campaign 指定目的地；REEN 必須開啟設定的 Target App。",
    "This campaign cannot validate the tracking-denied identity flow after the advertising identifier is deleted": "此 Campaign 刪除 Advertising ID 後無法驗證 tracking-denied identity flow，因此本 Scenario 不執行。",
    "The Privacy icon interaction opened an external destination and preserved the visible result alongside the response contract.": "Privacy icon 操作已開啟外部 destination，並將可見結果與 response 契約一併保存。",
    "FAILED: the E2E round does not prove the Privacy icon destination with response data, an executed interaction, and a visible screenshot.": "FAILED：本輪 E2E 沒有以 response 資料、實際操作與可見截圖共同證明 Privacy icon destination。",
    "FAILED: the round ran, but no preserved proxy transaction proves that the request and response belong to the same Appier ad flow.": "FAILED：本輪已執行，但沒有保存可證明 request 與 response 屬於同一條 Appier 廣告流程的 proxy transaction。",
    "FAILED: the round ran, but the specified CID was not proven by the Appier ad request flow.": "FAILED：本輪已執行，但 Appier ad request flow 沒有證明廣告來自指定 CID。",
    "FAILED: the round ran, but no recorded visual comparison proves that the rendered ad matches the response and layout contract.": "FAILED：本輪已執行，但沒有保存可證明廣告畫面符合 response 內容與版面契約的視覺對照 Evidence。",
    "FAILED: the round ran, but the proxy evidence does not contain both show_cb and winshowimg responses.": "FAILED：本輪已執行，但 proxy Evidence 沒有同時包含 show_cb 與 winshowimg response。",
    "R3 termination: swiping the App from Recents did not stop its process": "R3 終止步驟：從最近使用的 App 畫面滑除 Sample App 後，App process 仍未停止。",
    "R3 termination setup did not produce a new App process; the termination-dependent comparison was not executed": "R3 終止前置操作沒有建立新的 App process，因此未執行依賴終止狀態的比較。",
    "R5 PRIVACY-DENIED failed at Evidence capture: Delete advertising ID did not produce the expected Advertising ID state": "R5 PRIVACY-DENIED 在擷取 Evidence 時失敗：點擊 Delete advertising ID 後，實機沒有進入預期的 Advertising ID 已刪除狀態。",
    "R5 mutation did not produce Android minimum brightness raw 1": "R5 狀態切換沒有讓 Android 螢幕亮度達到最低有效原始值 1。",
    "Android Settings does not expose: Security and privacy": "Android 設定頁沒有顯示「安全性與隱私權」入口。",
    "Android fused location and ext.device geo_lat/geo_lon must all be numeric": "Android fused location 與 Extended device.geo_lat／geo_lon 都必須是數值。",
    "R3 cold-start: no eligible bid/impression": "R3 冷啟動後沒有取得可用的 Bid／曝光。",
    "FAILED: the E2E round does not contain a successful xclk response matching the visible impression.": "FAILED：本輪 E2E 沒有取得與畫面曝光廣告相符且成功的 xclk response。",
    "the SDK latency HEAD endpoint must return HTTP 200 in the same capture": "SDK latency HEAD endpoint 必須在同一份 Capture 回傳 HTTP 200。（舊版判定；新版已改為同一次 Automation。）",
    "The captured pubsetting response succeeded and contains Appier mediation configuration.": "已成功擷取 pubsetting response，且內容包含 Appier mediation 設定。",
    "FAILED: pubsetting transport or raw response evidence does not prove the Appier mediation configuration.": "FAILED：pubsetting transport 或 raw response Evidence 無法證明 Appier mediation 設定。",
    "The captured GMA transaction succeeded and its response contains Appier routing evidence.": "GMA transaction 成功，response 內含 Appier routing Evidence。",
    "FAILED: the GMA transaction or raw response does not prove Appier mediation routing.": "FAILED：GMA transaction 或 raw response 無法證明 Appier mediation routing。",
    "The proxy timeline proves GMA invoked the Appier adapter and received a successful Appier bid response.": "Proxy timeline 證明 GMA 已呼叫 Appier adapter，並收到成功的 Appier bid response。",
    "FAILED: no ordered GMA → Appier adapter request/response chain was captured.": "FAILED：沒有擷取到依序發生的 GMA → Appier adapter request/response chain。",
    "The Google mediation impression event was captured successfully.": "已成功擷取 Google mediation impression event。",
    "FAILED: the executed Mediation round has no successful Google impression event evidence.": "FAILED：已執行的 Mediation round 沒有成功的 Google impression event Evidence。",
    "The mediation fill-result event was captured successfully.": "已成功擷取 mediation fill-result event。",
    "FAILED: the executed Mediation round has no successful fill-result event evidence.": "FAILED：已執行的 Mediation round 沒有成功的 fill-result event Evidence。",
    "The AdMob click-reporting event was captured successfully.": "已成功擷取 AdMob click-reporting event。",
    "FAILED: the executed Mediation round has no successful AdMob click-reporting evidence.": "FAILED：已執行的 Mediation round 沒有成功的 AdMob click-reporting Evidence。",
}


def _dynamic_bi(text, zh=None):
    text = str(text)
    if text == "Actual SDK Payload":
        text = "Decoded Bid Request"
    stopped = re.fullmatch(
        r"Standalone R5-1 was stopped by the user after (\d+) attempts without capturing "
        r"an eligible bid for CID (.+?)\. The current runner had no attempt or phase timeout "
        r"limit, so no comparison result is claimed\.",
        text,
    )
    attempt_limit = re.search(
        r"No eligible bid for CID (.+?) after (\d+) attempts \((.+)\)",
        text,
    )
    stopped_testcase = re.fullmatch(
        r"Stopped by user before this TestCase could be completed: (.+)", text,
    )
    missing_evidence = re.fullmatch(
        r"Validator error after execution: \[Errno 2\] No such file or directory: ['\"](.+?)['\"]",
        text,
    )
    missing_artifact = re.fullmatch(
        r"Evidence capture did not produce required artifact: (.+)", text,
    )
    r5_appium = re.fullmatch(
        r"R5 (.+?) failed at Evidence capture: Cannot launch Appium session:.*", text, re.DOTALL,
    )
    r5_mutation = re.fullmatch(r"R5 state mutation failed: (.+)", text, re.DOTALL)
    if missing_evidence:
        artifact = Path(missing_evidence.group(1)).name
        text = f"Evidence capture did not produce {artifact}, so this TestCase could not be compared."
        zh = f"Evidence 擷取未產生 {artifact}，因此這個 TestCase 無法完成比較。"
    elif missing_artifact:
        artifact = Path(missing_artifact.group(1)).name
        text = f"Evidence capture did not produce {artifact}, so this TestCase could not be compared."
        zh = f"Evidence 擷取未產生 {artifact}，因此這個 TestCase 無法完成比較。"
    elif r5_appium:
        scenario = r5_appium.group(1)
        text = f"R5 {scenario} could not capture Evidence because Appium was unavailable."
        zh = f"R5 {scenario} 因 Appium 無法使用，未能擷取 Evidence。"
    elif r5_mutation:
        detail = r5_mutation.group(1)
        text = f"R5 could not apply the required device state: {detail}"
        zh = f"R5 無法完成必要的裝置狀態切換：{detail}"
    elif stopped:
        attempts, cid = stopped.groups()
        zh = (
            f"Standalone R5-1 嘗試 {attempts} 次後由使用者中止；仍未擷取到指定 CID {cid} "
            "的有效 Bid。當時 runner 尚未設定嘗試次數或階段逾時上限，因此本次不宣稱任何比較結果。"
        )
    elif stopped_testcase:
        zh = f"使用者中止執行，因此這個 TestCase 尚未完成：{stopped_testcase.group(1)}"
    elif attempt_limit:
        cid, attempts, counts = attempt_limit.groups()
        if "Appier Server error: 3 consecutive 5xx responses" in text:
            zh = (
                f"Appier Server 連續 3 次回傳 5xx，因此提前停止；指定 CID {cid} "
                f"在 {attempts} 次嘗試內未取得有效 Bid（{counts}）。"
            )
        else:
            zh = (
                f"指定 CID {cid} 在 {attempts} 次嘗試內未取得有效 Bid；"
                f"各類結果統計：{counts}。"
            )
    elif text.startswith("The traffic lookup key was captured automatically. MMP "):
        zh = "已自動保存流量查詢鍵；MMP Click Action 仍需查詢 MMP action 才能完成驗證。"
    elif text.startswith("The traffic lookup key was captured automatically.") and "attribution recognition" in text:
        zh = "已自動保存流量查詢鍵；歸因認列仍需完成 Spark／MMP 對帳。"
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


def _compact_json_facts(value):
    """Describe structured Evidence without dumping its payload into the card."""
    if not isinstance(value, dict):
        return (("Value", _display(value)),)
    facts = []
    for key, item in value.items():
        label = DISPLAY_LABELS.get(key, key.replace("_", " "))
        if isinstance(item, list):
            display = f"{len(item)} records"
        elif isinstance(item, dict):
            display = f"{len(item)} fields"
        elif isinstance(item, bool):
            display = "PASS" if item else "FAIL"
        elif item is None:
            display = "—"
        else:
            text = str(item)
            display = text if len(text) <= 48 else text[:45] + "…"
        facts.append((label, display))
    return tuple(facts)


def _compact_fact_grid(facts):
    return '<div class="mediation-facts">' + "".join(
        f'<div class="mediation-fact"><small>{html.escape(str(label))}</small><b>{html.escape(str(value))}</b></div>'
        for label, value in facts
    ) + '</div>'


def _evidence_content(row, guidance="", guidance_en=""):
    guidance_html = (
        f'<div class="evidence-guidance"><b>{_bi("Evidence guidance", "Evidence 說明")}</b><p>{_bi(guidance_en or guidance, guidance)}</p></div>'
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
    if row.get("tc") in {"standalone-creative-assets", "standalone-click"}:
        network_evidence = target.parent / "e2e-network-evidence.json"
        if network_evidence.is_file():
            target = network_evidence
            reference = network_evidence.name
    if row.get("tc") == "admob-pubsetting" and target.suffix.lower() == ".json":
        document = _load_json(target)
        pubsetting = document.get("pubsetting", {}) if isinstance(document, dict) else {}
        body = pubsetting.get("body", {}) if isinstance(pubsetting, dict) else {}
        responses = pubsetting.get("responses", []) if isinstance(pubsetting, dict) else []
        status = responses[-1].get("status") if responses and isinstance(responses[-1], dict) else None
        mediation_values = body.get("is_mediation") or []
        mediation_enabled = True in mediation_values if isinstance(mediation_values, list) else bool(mediation_values)
        zone_ids = body.get("zone_ids") or []
        class_names = body.get("class_names") or []
        compact_facts = (
            ("HTTP", status or "—"),
            ("Config", body.get("status") if body.get("status") is not None else "—"),
            ("Appier", "PASS" if body.get("contains_appier") else "FAIL"),
            ("Mediation", "PASS" if mediation_enabled else "FAIL"),
            ("Zone", ", ".join(map(str, zone_ids)) or "—"),
        )
        facts_html = "".join(
            f'<div class="mediation-fact"><small>{html.escape(label)}</small><b>{html.escape(str(value))}</b></div>'
            for label, value in compact_facts
        )
        details = html.escape(json.dumps({"adapter_class_names": class_names, "zone_ids": zone_ids}, ensure_ascii=False, indent=2))
        raw = html.escape(json.dumps(pubsetting, ensure_ascii=False, indent=2))
        return guidance_html + f'''<div class="evidence-data compact-evidence"><b>{html.escape(reference)}</b>
<label>{_bi("Key mediation facts", "Mediation 關鍵證據")}</label><div class="mediation-facts">{facts_html}</div>
<details class="mediation-details"><summary>{_bi("Adapter and zone details", "Adapter 與 Zone 詳情")}</summary><pre>{details}</pre></details>
<details class="raw-capture"><summary>{_bi("Open full request / response capture", "展開完整 Request／Response Capture")}</summary><pre>{raw}</pre></details></div>'''
    mime, _encoding = mimetypes.guess_type(target.name)
    if mime and mime.startswith("image/"):
        if row.get("tc") in {"advertising-id-opt-out", "tracking-denied"}:
            state = _load_json(target.parent / "tracking-denied-state.json") if (target.parent / "tracking-denied-state.json").exists() else {}
            if state.get("visual_contract") != "opt-out-row-visible-v2":
                return guidance_html + f'<div class="evidence-missing">{_bi("Stale Evidence hidden: recapture the complete Opt out row with the switch visibly ON.", "已隱藏舊 Evidence：請重新擷取完整的 Opt out 開關列，並讓 ON 狀態清楚可見。")}</div>'
        asset_url = _register_report_asset(target)
        return guidance_html + f'''<figure class="evidence-image"><button class="evidence-zoom" type="button" aria-label="放大 {html.escape(reference)}"><img loading="lazy" src="{html.escape(asset_url, quote=True)}" alt="{html.escape(reference)}"></button>
<figcaption>{html.escape(reference)} · {_bi("Click to view full image", "點擊查看全圖")}</figcaption></figure>'''
    if mime and mime.startswith("video/"):
        asset_url = _register_report_asset(target)
        return guidance_html + f'''<figure class="evidence-video"><video controls preload="metadata" src="{html.escape(asset_url, quote=True)}"></video>
<figcaption>{html.escape(reference)}</figcaption></figure>'''
    if mime and mime.startswith("audio/"):
        asset_url = _register_report_asset(target)
        return guidance_html + f'<audio controls preload="metadata" src="{html.escape(asset_url, quote=True)}"></audio>'
    if target.suffix.lower() == ".json":
        document = _load_json(target)
        if str(row.get("layer", "")).lower() == "e2e":
            raw = html.escape(json.dumps(document, ensure_ascii=False, indent=2))
            return guidance_html + f'''<div class="evidence-data compact-evidence e2e-json-evidence"><b>{html.escape(reference)}</b>
<label>{_bi("Captured data summary", "擷取資料摘要")}</label>{_compact_fact_grid(_compact_json_facts(document))}
<details class="raw-capture"><summary>{_bi("View complete JSON evidence", "查看完整 JSON Evidence")}</summary><pre><code>{raw}</code></pre></details></div>'''
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


def _comparison_summary(row, fallback_criterion, fallback_criterion_zh=None):
    fallback_criterion_zh = fallback_criterion if fallback_criterion_zh is None else fallback_criterion_zh
    view = row.get("comparison_view")
    if isinstance(view, dict) and view.get("kind") == "manual-expected":
        return f'''<section class="comparison-hero manual-expected-comparison"><div class="comparison-pair">
{_comparison_cell({"label": "Expected version", "value": "Enter in Evidence"}, "required-value")}
<div class="comparison-operator">?</div>{_comparison_cell(view.get("actual"), "actual-value")}</div>
<p><span>{_bi("Review gate", "人工核對")}</span>{_dynamic_bi(str(view.get("criterion") or fallback_criterion), fallback_criterion_zh)}</p></section>'''
    if row["status"] == Status.BLOCKED.value:
        return f'<section class="comparison-hero blocked-comparison"><b>{_bi("Not executed", "未執行")}</b><span>{_bi("No comparison is claimed.", "未宣稱任何比較結果。")}</span></section>'
    if str(row.get("layer", "")).lower() == "e2e":
        expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
        actual = row.get("actual") if isinstance(row.get("actual"), dict) else {}
        checks = _compact_fact_grid(_compact_json_facts(expected))
        captured = _compact_fact_grid(_compact_json_facts(actual))
        return f'''<section class="comparison-hero e2e-comparison">
<div class="e2e-summary-block"><label>{_bi("Pass checks", "通過條件")}</label>{checks}</div>
<div class="e2e-summary-block"><label>{_bi("Captured overview", "擷取摘要")}</label>{captured}</div>
<p><span>{_bi("Pass criterion", "通過標準")}</span>{_dynamic_bi(fallback_criterion, fallback_criterion_zh)}</p></section>'''
    if not isinstance(view, dict):
        actual = row.get("actual")
        actual_label = "Evidence status" if isinstance(actual, dict) and actual.get("error") else "Decoded Bid Request"
        return f'''<section class="comparison-hero rule-comparison">{_comparison_cell({"label": actual_label, "value": actual}, "actual-value")}
<p><span>{_bi("Pass criterion", "通過標準")}</span>{_dynamic_bi(fallback_criterion, fallback_criterion_zh)}</p></section>'''
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
<p><span>{_bi("Pass criterion", "通過標準")}</span>{_dynamic_bi(criterion, fallback_criterion_zh)}</p>{supporting}</section>'''


def _result_card(row, catalog_by_key):
    spec = catalog_by_key.get(row["tc"], {})
    platform_spec = spec.get(row["platform"], {}) if isinstance(spec, dict) else {}
    expected_text_zh = str(platform_spec.get("expected") or _display(row["expected"]))
    expected_text = str(platform_spec.get("expected_en") or expected_text_zh)
    priority = str(spec.get("priority") or "—")
    result_note = ""
    if row["reason"]:
        result_note = f'<div class="result-note"><b>{_bi("Result note", "結果說明")}</b><p>{_dynamic_bi(row["reason"])}</p></div>'
    coverage_note = ""
    if row.get("coverage_source"):
        coverage_note = f'<div class="coverage-source"><span>{_bi("Evidence source", "驗證來源")}</span><b>{html.escape(str(row["coverage_source"]))}</b><small>{_bi("No GAID-denied Mediation request was sent.", "未在 GAID 刪除狀態下送出 Mediation request。")}</small></div>'
    override_key = ":".join((
        row["platform"], row["mode_group"], row["test_type"], row["captured_at"], row["tc"]
    ))
    comparison_html = _comparison_summary(row, expected_text, expected_text_zh)
    evidence_comparison_html = "" if row["tc"] == "admob-pubsetting" else comparison_html
    version_review = ""
    if row["tc"] in {"sdk-version", "argus-sdk-version"}:
        actual_key = "req_app_sdk_version" if row["tc"] == "sdk-version" else "argus_ver"
        actual_version = (row.get("actual") or {}).get(actual_key)
        version_review = f'''<div class="version-review" data-version-actual="{html.escape(str(actual_version or ''), quote=True)}">
<label>{_bi("Expected version", "預期版號")}<input data-version-expected placeholder="例如：2.2.0" autocomplete="off"></label>
<small>{_bi("Captured actual", "實際抓到")}：<b>{html.escape(str(actual_version or '—'))}</b></small>
<button data-version-review-save>{_bi("Compare and save", "比對並儲存")}</button></div>'''
    return f'''<article class="result-card" data-tc="{html.escape(row["tc"], quote=True)}" data-result-status="{row["status"].lower()}" data-automation-status="{row["status"].lower()}" data-layer="{html.escape(row["layer"])}" data-override-key="{html.escape(override_key, quote=True)}">
<div class="result-head"><div><strong>{_tc_title(row)}</strong>
<span class="tc-id">{html.escape(_tc_label(row["tc"], catalog_by_key))}</span></div><div class="result-badges"><span class="priority-tag">{html.escape(priority)}</span><span class="status {row["status"].lower()}">{row["status"]}</span></div></div>
<div class="card-tabs"><button class="on" data-card-tab="summary">{_bi("Result", "結果")}</button><button data-card-tab="evidence">Evidence</button></div>
<div class="card-page" data-card-page="summary"><div class="manual-override-summary" data-manual-override-summary hidden><div><b>{_bi("Manual override", "人工覆寫")}</b><span data-manual-summary-status></span></div><p data-manual-summary-reason></p></div>{coverage_note}{comparison_html}{result_note}</div>
<div class="card-page" data-card-page="evidence" hidden><section class="evidence-contract captured-block"><label>{_bi("Captured source", "擷取來源")}</label>{_evidence_content(row, str(platform_spec.get("evidence_note") or ""), str(platform_spec.get("evidence_note_en") or ""))}</section>
{evidence_comparison_html}
{version_review}
<details class="manual-review"><summary><span>{_bi("Manual override", "人工覆寫")}</span><span class="manual-indicator" hidden>MANUAL</span></summary><div class="manual-form"><small>{_bi("Automation status", "自動化狀態")}：{row["status"]}</small>
<label>{_bi("Status", "狀態")}<select data-manual-status><option value="">Use automation result／使用自動化結果</option><option value="PASS">PASS</option><option value="FAILED">FAILED</option><option value="BLOCKED">BLOCKED</option></select></label>
<label>{_bi("Reason", "理由")}<textarea data-manual-reason rows="2" placeholder="Manual override reason／人工修改理由"></textarea></label>
<div class="manual-actions"><button data-manual-save>{_bi("Save override", "儲存覆寫")}</button><button data-manual-reset>{_bi("Clear override", "清除覆寫")}</button></div>
<div class="manual-saved" hidden></div></div></details></div></article>'''


def _unexecuted_card(spec, platform, reason=""):
    key = str(spec["key"])
    platform_spec = spec.get(platform, {})
    title = str(spec.get("title") or key)
    title_html = _bi(title, TC_TITLES_ZH.get(key, title))
    priority = html.escape(str(spec.get("priority") or "—"))
    setup = _catalog_text(platform_spec, "setup")
    expected = _catalog_text(platform_spec, "expected")
    execution_status = _bi("CANNOT RUN", "不可執行") if reason else _bi("NOT RUN", "未執行")
    return f'''<article class="result-card unexecuted-card" data-tc="{html.escape(key, quote=True)}" data-result-status="unexecuted" data-automation-status="unexecuted" data-layer="{html.escape(str(spec.get("layer", "Signal")).lower())}">
<div class="result-head"><div><strong>{title_html}</strong><span class="tc-id">{html.escape(_tc_label(key, {key: spec}))}</span></div><div class="result-badges"><span class="priority-tag">{priority}</span><span class="status unexecuted">{execution_status}</span></div></div>
<div class="unexecuted-body"><b>{_dynamic_bi(reason) if reason else _bi("No verdict was generated for this TestCase in the selected run.", "所選執行中沒有產生這條 TestCase 的 Verdict。")}</b>
<label>{_bi("Planned setup", "預定設定")}</label><p>{setup}</p><label>{_bi("Expected", "預期")}</label><p>{expected}</p></div></article>'''


def _catalog_applicable(spec, platform, mode, test_type):
    if not spec.get(platform, {}).get("applicable", False):
        return False
    if not campaign_supports(test_type, spec.get("key")):
        return False
    modes = spec.get("integration_modes") or []
    if not modes:
        return True
    catalog_mode = "standalone" if mode == "standalone" else "admob-mediation"
    return catalog_mode in modes


def _round_sort_value(round_name):
    match = re.fullmatch(r"R(\d+)", str(round_name or ""), re.I)
    return (int(match.group(1)), str(round_name)) if match else (10_000, str(round_name or "Unassigned"))


def _run_information(rows, latest=True):
    if not rows:
        return ""
    row = max(rows, key=lambda item: item["captured_at"])
    device = row["device"]
    device_name = device.get("model") or device.get("name") or "—"
    os_version = device.get("android_version") or device.get("os_version") or "—"
    sdk = device.get("sdk")
    os_text = f"Android {os_version}" + (f" · API {sdk}" if sdk else "")
    run_rows = [item for item in rows if item.get("run_group") == row.get("run_group")]
    try:
        suite_starts = [datetime.fromisoformat(item["test_run_started_at"]) for item in run_rows if item.get("test_run_started_at")]
        automation_starts = [datetime.fromisoformat(item["automation_started_at"]) for item in run_rows if item.get("automation_started_at")]
        automation_finishes = [datetime.fromisoformat(item["automation_finished_at"]) for item in run_rows if item.get("automation_finished_at")]
        if suite_starts:
            starts = suite_starts
            finishes = automation_finishes or [datetime.fromisoformat(item["finished_at"]) for item in run_rows if item.get("finished_at")]
        elif automation_starts:
            starts = automation_starts
            finishes = automation_finishes or [datetime.now().astimezone()]
        else:
            starts = [datetime.fromisoformat(item["started_at"]) for item in run_rows if item.get("started_at")]
            finishes = [datetime.fromisoformat(item["finished_at"]) for item in run_rows if item.get("finished_at")]
        elapsed = (max(finishes) - min(starts)).total_seconds()
        duration = f"{elapsed:.1f} s" if elapsed >= 0 else "—"
    except (TypeError, ValueError):
        duration = "—"
    values = (
        (_bi("Device", "裝置"), device_name),
        (_bi("System", "系統"), os_text),
        (_bi("Rounds", "輪次"), " · ".join(sorted({item["test_round"] for item in run_rows if item.get("test_round")}, key=_round_sort_value)) or "—"),
        (_bi("Mode", "模式"), row["test_mode"] or "—"),
        (_bi("Type", "類型"), row["test_type"] or "—"),
        ("CID", row["test_cid"] or "—"),
        (_bi("Executed", "執行時間"), row["captured_at"] or "—"),
        (_bi("Test duration", "本次測試耗時"), duration),
    )
    cells = "".join(
        f'<div><label>{label}</label><b>{html.escape(str(value))}</b></div>'
        for label, value in values
    )
    run_label = _bi("Latest Run", "最新執行") if latest else _bi("Selected Run", "選取的執行")
    return f'<section class="run-info"><div class="run-info-title"><span>{run_label}</span><b>{_bi("Test specification", "測試規格")}</b></div><div class="run-info-grid">{cells}</div></section>'


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
    buttons.append(
        f'<button class="status-filter unexecuted" data-status-filter="unexecuted">'
        f'<span>{_bi("NOT RUN", "未執行")}</span><b data-unexecuted-count>0</b></button>'
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


def _e2e_run_recording(rows):
    """Render one shared operation recording for the E2E journey, not per TC."""
    candidates = sorted(
        (row for row in rows if str(row.get("layer", "")).lower() == "e2e"),
        key=lambda row: row.get("captured_at", ""),
        reverse=True,
    )
    for row in candidates:
        video = Path(row["source"]).parent / "e2e-interactions.mp4"
        if not video.is_file():
            continue
        asset_url = _register_report_asset(video)
        return f'''<section class="e2e-run-recording">
<div class="e2e-run-recording-copy"><span>{_bi("Shared E2E Evidence", "E2E 共用 Evidence")}</span><h4>{_bi("Complete interaction recording", "完整操作紀錄")}</h4><p>{_bi("Ad display → Privacy interaction → Return to ad → CTA click → Final destination.", "廣告顯示 → Privacy 操作 → 返回廣告 → CTA 點擊 → 最終目的地。")}<br>{_bi("This recording documents the complete journey.", "影片記錄完整流程。")}</p></div>
<video controls preload="metadata" src="{html.escape(asset_url, quote=True)}"></video></section>'''
    return ""


def _slot_detail(platform, mode, kind, label, rows, catalog_by_key, skip_reasons=None):
    skip_reasons = skip_reasons or {}
    planned = [
        spec for spec in catalog_by_key.values()
        if _catalog_applicable(spec, platform, mode, kind)
    ]
    actual_by_key = {row["tc"]: row for row in rows}

    def cards_for(layer):
        selected_specs = [spec for spec in planned if str(spec.get("layer", "Signal")).lower() == layer]
        if selected_specs:
            return "".join(
                _result_card(actual_by_key[str(spec["key"])], catalog_by_key)
                if str(spec["key"]) in actual_by_key else _unexecuted_card(spec, platform, skip_reasons.get(str(spec["key"]), ""))
                for spec in sorted(selected_specs, key=lambda spec: (spec.get("order", float("inf")), spec["key"]))
            )
        selected = [row for row in rows if (row["layer"] == "e2e") == (layer == "e2e")]
        return "".join(_result_card(row, catalog_by_key) for row in sorted(selected, key=lambda row: (catalog_by_key.get(row["tc"], {}).get("order", float("inf")), row["tc"])))

    def signal_rounds():
        signal_specs = [spec for spec in planned if str(spec.get("layer", "Signal")).lower() == "signal"]
        if not signal_specs:
            return f'<div class="result-grid">{cards_for("signal")}</div>'
        groups = []
        round_names = sorted({str(spec.get("round") or "Unassigned") for spec in signal_specs}, key=_round_sort_value)
        for round_name in round_names:
            specs = sorted((spec for spec in signal_specs if str(spec.get("round") or "Unassigned") == round_name), key=lambda spec: (spec.get("order", float("inf")), spec["key"]))
            cards = "".join(_result_card(actual_by_key[str(spec["key"])], catalog_by_key) if str(spec["key"]) in actual_by_key else _unexecuted_card(spec, platform, skip_reasons.get(str(spec["key"]), "")) for spec in specs)
            groups.append(f'''<section class="result-round"><div class="result-round-head"><b>{html.escape(round_name)}</b><span>{len(specs)} TC</span></div><div class="result-grid">{cards}</div></section>''')
        return "".join(groups)
    signal_cards = cards_for("signal") or '<div class="empty"><b>Signal 尚無結果</b><p>加入 Signal TC 並產生 Verdict 後顯示於此。</p></div>'
    e2e_cards = cards_for("e2e") or '<div class="empty"><b>E2E 尚未建立</b><p>位置已保留；Signal 完成後再加入完整鏈路 TC。</p></div>'
    e2e_recording = _e2e_run_recording(rows)
    platform_label = next(item[1] for item in PLATFORMS if item[0] == platform)
    mode_label = next(item[1] for item in MODES if item[0] == mode)
    return f'''<section class="slot-detail" data-slot="{platform}:{mode}:{kind}" hidden>
<div class="detail-bar"><button class="back">{_bi("← Back to categories", "← 返回分類")}</button><div><span class="crumb">{platform_label} / {mode_label}</span><h2>{html.escape(label)}</h2></div></div>
{_status_filters(rows)}
{_run_information(rows)}
<div class="report-section"><div class="section-title"><span>01</span><div><h3>E2E</h3><p>Init → Config / Route → Appier Ad → Creative / Render → Impression / Fill → Click → Landing / Privacy → Attribution</p></div></div>{e2e_recording}<div class="result-grid">{e2e_cards}</div></div>
<div class="report-section"><div class="section-title"><span>02</span><div><h3>Signal</h3><p>{_bi("Ordered by execution Round; gray cards were not run.", "依執行 Round 排列；灰色卡片代表未執行。")}</p></div></div>{signal_rounds()}</div></section>'''


def _catalog_text(spec, key, default="—"):
    zh = str(spec.get(key, default))
    en = str(spec.get(f"{key}_en", zh))
    return _bi(en, zh)


def _catalog_cell(spec):
    if not spec.get("applicable", False):
        return f'<div class="na"><b>N/A</b><p>{_catalog_text(spec, "expected", "")}</p></div>'
    return f'''<div class="platform-spec"><b>{_bi("Setup", "設定")}</b><p>{_catalog_text(spec, "setup")}</p>
<b>{_bi("Expected", "預期")}</b><p>{_catalog_text(spec, "expected")}</p>
<b>Evidence</b><p>{_catalog_text(spec, "evidence")}</p></div>'''


def _catalog_mode_cell(tc, mode, layer):
    modes = tc.get("integration_modes", [])
    if layer == "signal" and not modes:
        return f'<span class="mode-availability yes">✓</span><small>{_bi("Shared", "共用")}</small>'
    available = mode == "standalone" and "standalone" in modes
    if mode == "mediation":
        available = any(item in modes for item in ("admob-mediation", "applovin-mediation"))
    if available:
        return f'<span class="mode-availability yes">✓</span><small>{_bi("Applicable", "適用")}</small>'
    return '<span class="mode-availability no">—</span>'


def _catalog_table(catalog, catalog_by_key, layer, round_name=None):
    rows = []
    selected = [
        tc for tc in catalog
        if str(tc.get("layer", "Signal")).lower() == layer
        and (round_name is None or str(tc.get("round", "")) == round_name)
    ]
    if layer == "e2e":
        selected.sort(key=lambda tc: (tc.get("order", float("inf")), str(tc.get("display_id") or "")))
    for tc in selected:
        key = str(tc["key"])
        label = _tc_label(key, catalog_by_key)
        key_line = f'<code class="catalog-key">{html.escape(key)}</code>' if label != key else ""
        catalog_status = str(tc.get("status", "DRAFT"))
        if layer == "e2e" and catalog_status == "DRAFT":
            catalog_status = "IMPLEMENTED"
        mode_cells = ""
        if layer == "e2e":
            mode_cells = f'''<td class="mode-cell">{_catalog_mode_cell(tc, "standalone", layer)}</td>
<td class="mode-cell">{_catalog_mode_cell(tc, "mediation", layer)}</td>'''
        rows.append(f'''<tr><td><span class="draft {catalog_status.lower()}">{html.escape(catalog_status)}</span>
<strong class="catalog-id">{html.escape(label)}</strong>{key_line}<small class="catalog-round-id">{html.escape(str(tc.get("round", "")))}</small></td>
<td><b>{_bi(str(tc.get("title", "")), TC_TITLES_ZH.get(key, str(tc.get("title", ""))))}</b><p>{html.escape(str(tc.get("layer", "Signal")))} · {html.escape(str(tc.get("category", "")))}</p>
<code>{html.escape(str(tc.get("field", "")))}</code><span class="priority">{html.escape(str(tc.get("priority", "")))}</span></td>
{mode_cells}
<td>{_catalog_cell(tc.get("aos", {}))}</td><td>{_catalog_cell(tc.get("ios", {}))}</td></tr>''')
    columns = 6 if layer == "e2e" else 4
    body = "".join(rows) or f'<tr><td colspan="{columns}" class="empty">{_bi("No TestCase is defined.", "尚未定義 TestCase。")}</td></tr>'
    mode_headers = "<th>Standalone</th><th>Mediation</th>" if layer == "e2e" else ""
    return f'''<div class="table-wrap"><table class="catalog-table catalog-{layer}-table"><thead><tr><th>TestCase</th><th>{_bi("Purpose / field", "目的／欄位")}</th>{mode_headers}<th>AOS</th><th>iOS</th></tr></thead><tbody>{body}</tbody></table></div>'''


def _catalog_section(catalog, catalog_by_key, layer, number, title, description):
    selected = [tc for tc in catalog if str(tc.get("layer", "Signal")).lower() == layer]
    count = len(selected)
    if layer == "e2e":
        journey = f'''<article class="catalog-round" id="catalog-e2e-journey">
<div class="catalog-round-head"><div><span>01–12</span><h3>{_bi("End-to-end journey", "完整 E2E 流程")}</h3><p>{_bi("One numeric sequence; S/M marks applicability without splitting the causal flow.", "以同一條數字順序閱讀；S／M 只標示適用模式，不拆散因果流程。")}</p></div><b>{count} TC</b></div>
{_catalog_table(catalog, catalog_by_key, layer)}</article>'''
        return f'''<section class="catalog-section" id="catalog-{layer}"><div class="section-title"><span>{number}</span><div><h2>{title}</h2><p>{description}</p></div><b>{count} TestCases</b></div>
{journey}</section>'''
    preferred = (
        ("E2E-BASELINE", "E2E-ADMOB", "E2E-BASELINE-ATTRIBUTION")
        if layer == "e2e" else ("R1", "R2", "R3", "R4", "R5")
    )
    discovered = []
    for tc in selected:
        round_name = str(tc.get("round", "Unassigned")) or "Unassigned"
        if round_name not in discovered:
            discovered.append(round_name)
    rounds = [name for name in preferred if name in discovered]
    rounds.extend(name for name in discovered if name not in rounds)
    round_copy = {
        "E2E-BASELINE": (_bi("Shared serving and interaction baseline", "共用的廣告供應與互動 Happy Path"), _bi("Standalone executes this baseline. Mediation inherits the same baseline before its M-only checks.", "Standalone 執行此 baseline；Mediation 也必須先通過，再接續 M 專屬檢查。")),
        "E2E-ADMOB": (_bi("AdMob Mediation extensions", "AdMob Mediation 專屬延伸"), _bi("Pubsetting → GMA routing → Appier adapter → Google impression/fill/click.", "Pubsetting → GMA routing → Appier adapter → Google impression／fill／click。")),
        "E2E-BASELINE-ATTRIBUTION": (_bi("Attribution continuation", "歸因延伸流程"), _bi("Install attribution and backend reconciliation after the click journey.", "Click journey 之後的安裝歸因與後端對帳。")),
        "R1": (_bi("Device and payload baseline", "裝置與 Payload 基礎訊號"), _bi("Core identifiers, device state, format, network, privacy, and lifecycle fields from the baseline capture.", "基礎 Capture 中的識別碼、裝置狀態、格式、網路、隱私與 lifecycle 欄位。")),
        "R2": (_bi("Second-impression signals", "第二次曝光訊號"), _bi("Signals that cannot be validated from the first ad impression.", "第一個廣告無法判定、必須到第二次曝光才驗證的訊號。")),
        "R3": (_bi("In-session lifecycle sequence", "App Session 生命週期序列"), _bi("Continuous use, background/resume, termination/reset, initialization, and accumulated duration.", "連續使用、背景恢復、終止重設、初始化與累積時長。")),
        "R4": (_bi("AOS / iOS IPv6 network refresh", "AOS／iOS IPv6 網路更新序列"), _bi("Cold launch, Wi-Fi switch, recovery, debounce, and slow-network behavior in one App session.", "同一 App session 內的冷啟動、Wi-Fi 切換、恢復、debounce 與慢速網路。")),
        "R5": (_bi("Alternate and negative states", "替代與反向狀態"), _bi("Privacy, low/high display-audio boundaries, power, timezone, and denied location permission are isolated into restorable scenarios.", "隱私、顯示／音量上下界、省電、時區與定位拒絕均拆成可還原的獨立 scenario。")),
    }
    groups = []
    for round_name in rounds:
        round_count = sum(str(tc.get("round", "")) == round_name for tc in selected)
        round_title, round_description = round_copy.get(
            round_name,
            (_bi("TestCase group", "TestCase 群組"), _bi("Catalog entries assigned to this Round.", "歸屬於此 Round 的 Catalog 項目。")),
        )
        groups.append(f'''<article class="catalog-round" id="catalog-{layer}-{html.escape(round_name.lower())}">
<div class="catalog-round-head"><div><span>{html.escape(round_name)}</span><h3>{round_title}</h3><p>{round_description}</p></div><b>{round_count} TC</b></div>
{_catalog_table(catalog, catalog_by_key, layer, round_name)}</article>''')
    return f'''<section class="catalog-section" id="catalog-{layer}"><div class="section-title"><span>{number}</span><div><h2>{title}</h2><p>{description}</p></div><b>{count} TestCases</b></div>
{"".join(groups)}</section>'''


CSS = r"""
:root{--bg:#eef1f4;--panel:#fff;--panel2:#f6f8fa;--ink:#131a21;--soft:#516069;--faint:#7d8b94;--line:#dbe2e8;--accent:#0e7c86;--accent2:#e2eff1;--aos:#2e9e5b;--ios:#3a6ea5;--pass:#2f7d3a;--fail:#c0392b;--block:#b5761a;--shadow:0 1px 2px #131a210f,0 8px 24px #131a210f;--mono:ui-monospace,SFMono-Regular,Menlo,monospace;--sans:system-ui,-apple-system,"Segoe UI","Noto Sans TC",sans-serif}
:root[data-lang=en] .lang-zh,:root[data-lang=zh] .lang-en{display:none!important}.language{border:1px solid var(--line);background:var(--panel);color:var(--ink);padding:6px 10px;border-radius:8px;cursor:pointer;font:750 11px var(--mono)}
.type-card h3,.type-card p,.type-card .total,.type-card .layer-row,.type-card .open{font-family:"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif}.type-card .lang-en,.type-card .lang-zh{font-size:inherit;line-height:inherit;font-weight:inherit;letter-spacing:inherit}
@media(prefers-color-scheme:dark){:root{--bg:#0d1216;--panel:#151d23;--panel2:#111820;--ink:#e7edf1;--soft:#a6b6c1;--faint:#71828d;--line:#243039;--accent:#38bdc9;--accent2:#123037;--aos:#4cc57d;--ios:#6ba6dd;--pass:#5cc46a;--fail:#f0766a;--block:#e0a94a;--shadow:0 10px 30px #0006}}
:root[data-theme=dark]{--bg:#0d1216;--panel:#151d23;--panel2:#111820;--ink:#e7edf1;--soft:#a6b6c1;--faint:#71828d;--line:#243039;--accent:#38bdc9;--accent2:#123037;--aos:#4cc57d;--ios:#6ba6dd;--pass:#5cc46a;--fail:#f0766a;--block:#e0a94a;--shadow:0 10px 30px #0006}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans)}button,select{font:inherit}.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:18px;padding:10px 18px;background:color-mix(in srgb,var(--panel) 90%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}.brand{font-weight:800}.brand small{display:block;color:var(--faint);font:10px var(--mono)}.main-nav{display:flex;gap:4px}.main-nav button,.seg button,.back,.theme{border:1px solid transparent;background:transparent;color:var(--soft);padding:7px 12px;border-radius:8px;cursor:pointer}.main-nav button.on,.seg button.on{background:var(--accent2);color:var(--accent);font-weight:750}.theme{margin-left:auto;border-color:var(--line)}main{max-width:1180px;margin:auto;padding:25px 20px 50px}.hero h1{margin:0;font-size:23px}.hero p{color:var(--soft)}.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:20px 0}.seg{display:flex;gap:3px;padding:3px;background:var(--panel);border:1px solid var(--line);border-radius:11px}.seg.platform button[data-value=aos].on{color:var(--aos)}.seg.platform button[data-value=ios].on{color:var(--ios)}.type-grid,.result-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:15px}.type-card{background:var(--panel);color:inherit;text-align:left;border:1px solid var(--line);border-radius:14px;padding:17px;box-shadow:var(--shadow);cursor:pointer;transition:.15s}.type-card:hover{transform:translateY(-2px);border-color:var(--accent)}.type-card>div:first-child,.result-head,footer{display:flex;justify-content:space-between;gap:10px}.type-id,.tc-id,.crumb{font:700 11px var(--mono);color:var(--accent)}.total{color:var(--faint);font-size:11px}.type-card h3{margin:8px 0 2px;font-size:18px}.type-card p{color:var(--soft);min-height:40px}.counts{display:flex;gap:10px;font-size:10px}.pass-text{color:var(--pass)}.failed-text{color:var(--fail)}.blocked-text{color:var(--block)}.open{display:block;color:var(--accent);margin-top:15px;font-size:12px}.detail-bar{display:flex;align-items:center;gap:15px;margin-bottom:18px}.detail-bar h2{margin:2px 0}.back{border-color:var(--line);background:var(--panel)}.result-card{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:15px;box-shadow:var(--shadow)}.result-head>div{display:flex;flex-direction:column}.status{font:750 11px var(--mono);padding:4px 9px;border-radius:999px;height:max-content}.status.pass{color:var(--pass);background:#2f7d3a20}.status.failed{color:var(--fail);background:#c0392b20}.status.blocked{color:var(--block);background:#b5761a20}.context{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}.context span{background:var(--panel2);padding:4px 7px;border-radius:6px;font-size:11px}.answers{display:grid;grid-template-columns:1fr 1fr;gap:9px}.answers label,.platform-spec>b{font-size:10px;color:var(--faint);text-transform:uppercase}.answers pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--panel2);padding:9px;border-radius:7px;min-height:50px;font:12px var(--mono)}footer{color:var(--faint);font-size:11px}a{color:var(--accent)}.missing{color:var(--fail);text-decoration:line-through}.empty{padding:45px;text-align:center;background:var(--panel);border:1px dashed var(--line);border-radius:13px;color:var(--soft)}.catalog-head{display:flex;justify-content:space-between;align-items:end;gap:20px}.catalog-head p{color:var(--soft)}.catalog-section{margin-top:28px}.catalog-section>.section-title>b{margin-left:auto;color:var(--faint);font:700 11px var(--mono)}.table-wrap{overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}table{border-collapse:collapse;width:100%;min-width:1180px}th,td{text-align:left;vertical-align:top;padding:14px;border-bottom:1px solid var(--line)}th{position:sticky;top:0;background:var(--panel2);font-size:11px;color:var(--faint);text-transform:uppercase}td:first-child{width:120px}.catalog-id{display:block;font:750 13px var(--mono);margin:7px 0}.draft,.priority{display:inline-block;padding:2px 6px;border-radius:5px;background:var(--accent2);color:var(--accent);font:700 9px var(--mono)}td code{color:var(--accent)}.priority{margin-left:7px}.platform-spec p,.na p{margin:3px 0 10px;color:var(--soft);min-width:250px}.na{color:var(--faint)}.mode-cell{width:86px;min-width:86px;text-align:center}.mode-cell small{display:block;margin-top:4px;color:var(--faint)}.mode-availability{display:inline-grid;place-items:center;width:25px;height:25px;border-radius:999px;font:800 12px var(--mono)}.mode-availability.yes{color:var(--pass);background:#2f7d3a20}.mode-availability.no{color:var(--faint);background:var(--panel2)}.meta{color:var(--faint);font-size:11px;margin-top:20px}[hidden]{display:none!important}@media(max-width:650px){main{padding:18px 12px}.top{flex-wrap:wrap}.main-nav{order:3;width:100%}.answers{grid-template-columns:1fr}}
.layer-row{display:grid;grid-template-columns:58px 26px 26px 1fr auto;gap:6px;align-items:center;padding:7px 0;border-top:1px solid var(--line);font-size:10px}.layer-row>b{font-size:11px}.layer-row small{color:var(--faint)}
.report-section{margin:22px 0 32px}.section-title{display:flex;align-items:center;gap:11px;margin-bottom:12px}.section-title>span{font:800 11px var(--mono);color:var(--accent);background:var(--accent2);padding:6px;border-radius:7px}.section-title h3,.section-title p{margin:0}.section-title p{color:var(--faint);font-size:11px}
.result-badges{align-items:flex-end;gap:5px}.priority-tag{font:800 11px var(--mono);padding:4px 8px;border-radius:999px;background:var(--accent2);color:var(--accent)}.card-tabs{display:flex;gap:4px;margin:14px 0 11px;border-bottom:1px solid var(--line)}.card-tabs button{border:0;border-bottom:2px solid transparent;background:transparent;color:var(--faint);padding:6px 9px;cursor:pointer}.card-tabs button.on{color:var(--accent);border-bottom-color:var(--accent);font-weight:750}.card-page{min-height:250px}.comparison-hero{background:var(--panel2);border-radius:12px;padding:14px;margin-bottom:10px}.comparison-pair,.comparison-triplet{display:grid;grid-template-columns:minmax(0,1fr) 34px minmax(0,1fr);align-items:center;gap:7px}.comparison-triplet{grid-template-columns:minmax(0,1fr) 22px minmax(0,1fr) 22px minmax(0,1fr)}.comparison-value{min-width:0;text-align:center;padding:13px 8px;border:1px solid var(--line);border-radius:9px;background:var(--panel)}.comparison-value label{display:block;color:var(--faint);font-size:9px;font-weight:750;letter-spacing:.05em;text-transform:uppercase}.comparison-value b{display:block;margin-top:7px;font:800 16px var(--mono);overflow-wrap:anywhere}.captured-value{border-top:3px solid var(--accent)}.actual-value{border-top:3px solid var(--pass)}.required-value{border-top:3px solid var(--block)}.comparison-operator{text-align:center;font:900 19px var(--mono);color:var(--soft)}.comparison-hero>p{margin:11px 1px 0;line-height:1.45;color:var(--soft)}.comparison-hero>p span{display:block;color:var(--faint);font-size:9px;font-weight:800;letter-spacing:.07em;text-transform:uppercase}.comparison-hero>small{display:block;margin-top:7px;color:var(--faint)}.comparison-rule-value .comparison-value{text-align:left}.blocked-comparison{display:flex;justify-content:space-between;color:var(--block)}.expected-block,.actual-block{background:var(--panel2);border-radius:9px;padding:11px 12px;margin-bottom:10px}.expected-block label,.actual-block label,.captured-block>label,.run-info label{display:block;color:var(--faint);font-size:9px;font-weight:750;letter-spacing:.08em;text-transform:uppercase}.expected-block p{margin:5px 0 0;color:var(--ink);line-height:1.6}.captured-block{border:1px solid var(--line);border-radius:10px;padding:11px;margin-bottom:10px}.captured-block>label{margin-bottom:9px}.facts{margin:5px 0 0}.facts>div{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-top:1px solid var(--line)}.facts>div:first-child{border-top:0}.facts dt{color:var(--soft)}.facts dd{margin:0;text-align:right;font:650 11px var(--mono);overflow-wrap:anywhere}.result-note{border-left:3px solid var(--block);padding:7px 10px}.result-note p{margin:3px 0}.evidence-guidance{border-left:3px solid var(--accent);background:var(--accent2);border-radius:0 9px 9px 0;padding:10px 12px;margin-bottom:10px}.evidence-guidance p{margin:4px 0 0;line-height:1.55}.evidence-image,.evidence-video{margin:0;display:flex;min-width:0;flex-direction:column;align-items:center}.evidence-image img,.evidence-video video{display:block;width:auto;max-width:100%;height:auto;max-height:390px;object-fit:contain;border-radius:9px;background:#000}.evidence-image figcaption,.evidence-video figcaption{max-width:100%;color:var(--faint);font:10px var(--mono);margin-top:6px;overflow-wrap:anywhere}.evidence-data,.evidence-text{background:var(--panel2);border-radius:9px;padding:12px;overflow:auto}.evidence-data>b{display:block;margin-bottom:8px}.evidence-missing{padding:45px 10px;text-align:center;color:var(--fail)}.run-info{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px;box-shadow:var(--shadow);margin-bottom:24px}.run-info-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.run-info-title span{font:750 10px var(--mono);color:var(--accent)}.run-info-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:9px;overflow:hidden}.run-info-grid>div{background:var(--panel2);padding:10px}.run-info-grid b{display:block;margin-top:3px;font-size:12px;overflow-wrap:anywhere}
.status-filters{display:flex;justify-content:space-between;align-items:center;gap:15px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 15px;box-shadow:var(--shadow);margin-bottom:12px}.status-filters>div:first-child{display:flex;flex-direction:column}.status-filters>div:first-child>span{font-weight:800}.status-filters small{color:var(--faint)}.status-filter-buttons{display:flex;gap:7px;flex-wrap:wrap}.status-filter{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:999px;background:var(--panel2);color:var(--soft);padding:6px 10px;cursor:pointer}.status-filter span{font:750 10px var(--mono)}.status-filter b{min-width:20px;text-align:center;border-radius:999px;background:var(--panel);padding:1px 5px}.status-filter.pass{color:var(--pass)}.status-filter.failed{color:var(--fail)}.status-filter.blocked{color:var(--block)}.status-filter.on{color:#fff;border-color:transparent}.status-filter.pass.on{background:var(--pass)}.status-filter.failed.on{background:var(--fail)}.status-filter.blocked.on{background:var(--block)}.status-filter.on b{color:var(--ink)}.status-filter:disabled{opacity:.4;cursor:not-allowed}@media(max-width:650px){.status-filters{align-items:flex-start;flex-direction:column}.status-filter-buttons{width:100%}.status-filter{flex:1;justify-content:center}}
.status-filter.unexecuted{color:var(--faint)}.status-filter.unexecuted.on{background:#737982}.result-card.unexecuted-card{background:color-mix(in srgb,var(--panel2) 82%,#888 18%);border-color:color-mix(in srgb,var(--line) 70%,#888 30%);box-shadow:none;color:var(--soft)}.status.unexecuted{color:var(--faint);background:#7772}.unexecuted-body{margin-top:14px;padding:12px;border-radius:9px;background:color-mix(in srgb,var(--panel) 55%,transparent)}.unexecuted-body>b{display:block;margin-bottom:12px}.unexecuted-body label{display:block;margin-top:9px;color:var(--faint);font:750 9px var(--mono);letter-spacing:.07em;text-transform:uppercase}.unexecuted-body p{margin:3px 0;color:var(--soft);line-height:1.5}.result-round{margin:0 0 24px}.result-round-head{display:flex;align-items:center;justify-content:space-between;margin:0 0 9px;padding:8px 11px;border-left:3px solid var(--accent);background:var(--panel2);border-radius:0 8px 8px 0}.result-round-head b{font:850 12px var(--mono);color:var(--accent)}.result-round-head span{color:var(--faint);font:700 10px var(--mono)}
.manual-review{margin-top:10px}.manual-review summary{display:flex;align-items:center;gap:6px;width:max-content;margin-left:auto;color:var(--faint);font:700 9px var(--mono);cursor:pointer;list-style:none}.manual-review summary::-webkit-details-marker{display:none}.manual-review summary:before{content:"＋"}.manual-review[open] summary:before{content:"−"}.manual-form{margin-top:7px;padding:9px 10px;background:var(--panel2);border:1px solid var(--line);border-radius:8px}.manual-form>small{color:var(--faint);font-size:9px}.manual-indicator{font:800 8px var(--mono);color:#fff;background:var(--block);padding:2px 5px;border-radius:999px}.manual-review label{display:block;color:var(--faint);font-size:9px;font-weight:750;margin-top:6px}.manual-review select,.manual-review textarea{display:block;width:100%;margin-top:3px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);padding:5px 6px}.manual-review textarea{resize:vertical;font:11px var(--sans)}.manual-actions{display:flex;gap:6px;margin-top:7px}.manual-actions button,.export-overrides{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:7px;padding:5px 8px;cursor:pointer}.manual-actions button:first-child{background:var(--accent);border-color:var(--accent);color:#fff}.manual-saved{margin-top:7px;padding:6px 8px;border-left:3px solid var(--block);background:var(--panel);font-size:10px;white-space:pre-wrap}.export-overrides{margin-left:auto}.theme{margin-left:0}
.version-review{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:end;gap:8px;margin:10px 0;padding:10px;border:1px solid var(--line);border-radius:9px;background:var(--panel2)}.version-review label{grid-row:span 2;color:var(--faint);font-size:9px;font-weight:750}.version-review input{display:block;width:100%;margin-top:4px;padding:7px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);font:700 12px var(--mono)}.version-review small{color:var(--faint)}.version-review button{border:0;border-radius:7px;padding:7px 9px;background:var(--accent);color:#fff;cursor:pointer}
.comparison-pair{grid-template-columns:minmax(0,1fr) 26px minmax(0,1fr)}.comparison-triplet{grid-template-columns:minmax(0,1fr) 18px minmax(0,1fr) 18px minmax(0,1fr)}.comparison-value{padding-left:7px;padding-right:7px}.comparison-value label{font-size:8px;line-height:1.35;letter-spacing:.035em;overflow-wrap:anywhere}.comparison-value b{font-size:clamp(11px,1.05vw,15px);line-height:1.35;word-break:break-word}.comparison-operator{font-size:15px}
.evidence-zoom{display:block;max-width:100%;border:0;padding:0;background:transparent;cursor:zoom-in}.evidence-zoom img{pointer-events:none}.image-lightbox{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;padding:30px;background:#071014eb;cursor:zoom-out}.image-lightbox[hidden]{display:none}.image-lightbox img{display:block;max-width:96vw;max-height:90vh;object-fit:contain;filter:drop-shadow(0 16px 50px #000)}.image-lightbox p{position:absolute;left:24px;bottom:12px;margin:0;color:#dce7eb;font:11px var(--mono)}.image-lightbox-close{position:absolute;right:20px;top:16px;z-index:1;border:1px solid #ffffff55;border-radius:999px;background:#111c;color:#fff;width:38px;height:38px;font-size:24px;line-height:1;cursor:pointer}
.comparison-list{max-height:190px;margin:9px 0 0;padding:0;overflow:auto;list-style:none;text-align:left;border-top:1px solid var(--line)}.comparison-list li{padding:6px 3px;border-bottom:1px solid var(--line);font:650 11px/1.35 var(--mono);overflow-wrap:anywhere}.comparison-list li:last-child{border-bottom:0}
.catalog-round{margin:18px 0 30px}.catalog-round-head{display:flex;justify-content:space-between;align-items:end;gap:16px;padding:14px 16px;margin-bottom:9px;background:var(--panel);border:1px solid var(--line);border-radius:12px}.catalog-round-head span{display:inline-block;color:var(--accent);font:800 11px var(--mono);background:var(--accent2);padding:4px 8px;border-radius:6px}.catalog-round-head h3{display:inline;margin-left:9px;font-size:16px}.catalog-round-head p{margin:7px 0 0;color:var(--soft)}.catalog-round-head>b{white-space:nowrap;color:var(--faint);font:800 11px var(--mono)}.catalog-key{display:block;margin:0 0 7px;font:10px/1.35 var(--mono);overflow-wrap:anywhere}.catalog-round-id{display:inline-block;padding:3px 6px;border:1px solid var(--line);border-radius:5px;color:var(--faint);font:700 9px var(--mono)}.draft.implemented{color:var(--pass);background:#2f7d3a20}
.compact-evidence{overflow:visible}.compact-evidence>label{display:block;margin-top:10px;color:var(--faint);font-size:9px;font-weight:750;letter-spacing:.08em;text-transform:uppercase}.raw-capture{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}.raw-capture summary{width:max-content;max-width:100%;color:var(--accent);font:750 10px var(--mono);cursor:pointer}.raw-capture pre{max-height:320px;margin:10px 0 0;padding:11px;border:1px solid var(--line);border-radius:8px;background:var(--panel);overflow:auto;white-space:pre;font:10px/1.45 var(--mono)}
.result-card[data-layer="e2e"] .card-page{min-height:0}.mediation-facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(74px,1fr));gap:6px;margin-top:6px}.mediation-fact{min-width:0;padding:7px 8px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}.mediation-fact small{display:block;color:var(--faint);font:750 8px var(--mono);letter-spacing:.06em;text-transform:uppercase}.mediation-fact b{display:block;margin-top:3px;font:800 11px var(--mono);overflow-wrap:anywhere}.mediation-details{margin-top:10px}.mediation-details summary{color:var(--accent);font:750 10px var(--mono);cursor:pointer}.mediation-details pre{max-height:180px;margin:8px 0 0;padding:9px;border:1px solid var(--line);border-radius:8px;background:var(--panel);overflow:auto;white-space:pre;font:10px/1.4 var(--mono)}.e2e-summary-block+ .e2e-summary-block{margin-top:11px}.e2e-summary-block>label{display:block;color:var(--faint);font:750 9px var(--mono);letter-spacing:.07em;text-transform:uppercase}.e2e-json-evidence .raw-capture pre code{font:inherit}
.e2e-run-recording{display:grid;grid-template-columns:minmax(190px,.8fr) minmax(260px,1.2fr);gap:18px;align-items:center;margin:0 0 15px;padding:15px;border:1px solid var(--line);border-radius:13px;background:var(--panel);box-shadow:var(--shadow)}.e2e-run-recording-copy span{color:var(--accent);font:800 9px var(--mono);letter-spacing:.08em;text-transform:uppercase}.e2e-run-recording-copy h4{margin:5px 0 4px;font-size:16px}.e2e-run-recording-copy p{margin:0;color:var(--soft);font-size:12px}.e2e-run-recording video{display:block;width:100%;max-height:420px;border-radius:9px;background:#000;object-fit:contain}@media(max-width:700px){.e2e-run-recording{grid-template-columns:1fr}}
.coverage-source{display:flex;flex-direction:column;gap:2px;margin:0 0 10px;padding:9px 11px;border-left:3px solid var(--accent);border-radius:0 8px 8px 0;background:var(--accent2)}.coverage-source span{color:var(--faint);font:750 8px var(--mono);letter-spacing:.07em;text-transform:uppercase}.coverage-source b{font-size:11px}.coverage-source small{color:var(--soft);font-size:10px}
.manual-override-summary{margin:0 0 11px;padding:10px 12px;border:1px solid color-mix(in srgb,var(--block) 55%,var(--line));border-left:4px solid var(--block);border-radius:0 9px 9px 0;background:color-mix(in srgb,var(--block) 12%,var(--panel))}.manual-override-summary>div{display:flex;align-items:center;gap:7px}.manual-override-summary b{font:850 10px var(--mono);color:var(--block);letter-spacing:.04em;text-transform:uppercase}.manual-override-summary span[data-manual-summary-status]{padding:2px 6px;border-radius:999px;background:var(--block);color:#fff;font:800 9px var(--mono)}.manual-override-summary p{margin:7px 0 0;color:var(--ink);font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}
.result-note p,.plain-value,.evidence-missing{overflow-wrap:anywhere;word-break:break-word}.evidence-missing{border:1px dashed color-mix(in srgb,var(--fail) 45%,var(--line));border-radius:9px;background:color-mix(in srgb,var(--fail) 5%,var(--panel))}
"""


SCRIPT = r"""
(function(){
 var root=document.documentElement,latestSlot=(root.dataset.latestSlot||"aos:standalone:aibid").split(":"),platform=latestSlot[0]||"aos",mode=latestSlot[1]||"standalone",activePage="reports";
 var overrideStorageKey="laf2-manual-overrides-v1",overrides={};
 try{var saved=localStorage.getItem("laf2-theme");if(saved)root.dataset.theme=saved}catch(e){}
 try{root.dataset.lang=localStorage.getItem("laf2-language")||"zh"}catch(e){root.dataset.lang="zh"}
 try{overrides=JSON.parse(localStorage.getItem(overrideStorageKey)||"{}")||{}}catch(e){overrides={}}
 document.getElementById("theme").onclick=function(){var dark=root.dataset.theme==="dark";root.dataset.theme=dark?"light":"dark";try{localStorage.setItem("laf2-theme",root.dataset.theme)}catch(e){}};
 function applyLanguage(){var zh=root.dataset.lang==="zh",button=document.getElementById("language");button.textContent=zh?"EN":"中文";button.title=zh?"Switch to English":"切換為中文";document.documentElement.lang=zh?"zh-Hant":"en";document.querySelectorAll(".type-card").forEach(function(card){var count=card.querySelectorAll(".result-card").length||Number((card.querySelector(".total")||{}).dataset&&card.querySelector(".total").dataset.resultCount)||0,total=card.querySelector(".total");if(total)total.textContent=count+(zh?" 個 TC":" TestCases")})}
 document.getElementById("language").onclick=function(){root.dataset.lang=root.dataset.lang==="zh"?"en":"zh";try{localStorage.setItem("laf2-language",root.dataset.lang)}catch(e){}applyLanguage();refreshCounts()};
 function persistOverrides(){try{localStorage.setItem(overrideStorageKey,JSON.stringify(overrides));return true}catch(e){alert("無法儲存 manual override："+e);return false}}
 function applyManualOverride(card){
  var item=overrides[card.dataset.overrideKey],automation=card.dataset.automationStatus,status=item&&item.status?item.status.toLowerCase():automation;
  card.dataset.resultStatus=status;
  var badge=card.querySelector(".result-badges .status");badge.classList.remove("pass","failed","blocked");badge.classList.add(status);badge.textContent=status.toUpperCase();
  var select=card.querySelector("[data-manual-status]"),reason=card.querySelector("[data-manual-reason]"),indicator=card.querySelector(".manual-indicator"),saved=card.querySelector(".manual-saved"),summary=card.querySelector("[data-manual-override-summary]");
  select.value=item?item.status:"";reason.value=item?item.reason:"";indicator.hidden=!item;saved.hidden=!item;
  summary.hidden=!item;summary.querySelector("[data-manual-summary-status]").textContent=item?item.status:"";summary.querySelector("[data-manual-summary-reason]").textContent=item?item.reason:"";
  var expectedInput=card.querySelector("[data-version-expected]");if(expectedInput)expectedInput.value=item&&item.expected_version?item.expected_version:"";
  card.querySelectorAll(".manual-expected-comparison").forEach(function(versionSummary){var expectedValue=versionSummary.querySelector(".required-value b"),operator=versionSummary.querySelector(".comparison-operator");expectedValue.textContent=item&&item.expected_version?item.expected_version:"Enter in Evidence";operator.textContent=item&&item.expected_version?(item.status==="PASS"?"=":"≠"):"?"});
  if(item)saved.textContent=item.status+" — "+item.reason+"\nUpdated "+item.updated_at;
 }
 function refreshCounts(){
  document.querySelectorAll(".slot-detail").forEach(function(detail){
   var cards=Array.from(detail.querySelectorAll(".result-card")),counts={pass:0,failed:0,blocked:0};cards.forEach(function(card){counts[card.dataset.resultStatus]=(counts[card.dataset.resultStatus]||0)+1});
   detail.querySelectorAll("[data-status-filter]").forEach(function(button){var count=counts[button.dataset.statusFilter]||0;button.querySelector("b").textContent=count;button.disabled=count===0});
   applyStatusFilter(detail,detail.dataset.statusFilter||"");
   var typeCard=Array.from(document.querySelectorAll(".type-card")).find(function(card){return card.dataset.slot===detail.dataset.slot});
   if(typeCard){var total=typeCard.querySelector(".total");total.dataset.resultCount=cards.length;total.textContent=cards.length+(root.dataset.lang==="zh"?" 個 TC":" TestCases");["e2e","signal"].forEach(function(layer){var rows=cards.filter(function(card){return card.dataset.layer===layer}),row=typeCard.querySelector('[data-layer-row="'+layer+'"]'),layerCounts={pass:0,failed:0,blocked:0,unexecuted:0};rows.forEach(function(card){layerCounts[card.dataset.resultStatus]=(layerCounts[card.dataset.resultStatus]||0)+1});var executed=rows.length-layerCounts.unexecuted;row.querySelector(".pass-text").textContent=layerCounts.pass+"✓";row.querySelector(".failed-text").textContent=layerCounts.failed+"✗";row.querySelector(".blocked-text").textContent=layerCounts.blocked+"▲";row.querySelector("small").textContent=root.dataset.lang==="zh"?rows.length+" TC・已執行 "+executed+"・未執行 "+layerCounts.unexecuted:rows.length+" TC · Executed "+executed+" · Not run "+layerCounts.unexecuted})}
  })
 }
 document.querySelectorAll(".main-nav button").forEach(function(b){b.onclick=function(){activePage=b.dataset.page;document.querySelectorAll(".main-nav button").forEach(function(x){x.classList.toggle("on",x===b)});document.querySelectorAll(".app-page").forEach(function(p){p.hidden=p.id!==activePage+"-page"});if(activePage==="reports")showOverview()}});
 function select(group,value){document.querySelectorAll('.seg.'+group+' button').forEach(function(b){b.classList.toggle("on",b.dataset.value===value)})}
 function update(){select("platform",platform);select("mode",mode);document.querySelectorAll(".type-card").forEach(function(c){c.hidden=!c.dataset.slot.startsWith(platform+":"+mode+":")});document.getElementById("result-context").textContent=(platform==="aos"?"AOS":"iOS")+" · "+(mode==="standalone"?"Standalone":"Mediation")}
 document.querySelectorAll(".seg.platform button").forEach(function(b){b.onclick=function(){platform=b.dataset.value;showOverview();update()}});
 document.querySelectorAll(".seg.mode button").forEach(function(b){b.onclick=function(){mode=b.dataset.value;showOverview();update()}});
 var overview=document.getElementById("slot-overview"),details=document.querySelectorAll(".slot-detail");
 function showOverview(){details.forEach(function(d){d.hidden=true});overview.hidden=false}
 function applyStatusFilter(detail,status){detail.dataset.statusFilter=status;detail.querySelectorAll("[data-status-filter]").forEach(function(button){button.classList.toggle("on",button.dataset.statusFilter===status)});detail.querySelectorAll(".report-section").forEach(function(section){var cards=Array.from(section.querySelectorAll(".result-card")),matches=cards.filter(function(card){return !status||card.dataset.resultStatus===status});cards.forEach(function(card){card.hidden=!!status&&card.dataset.resultStatus!==status});section.querySelectorAll(".result-round").forEach(function(group){group.hidden=!!status&&!Array.from(group.querySelectorAll(".result-card")).some(function(card){return card.dataset.resultStatus===status})});section.hidden=!!status&&matches.length===0})}
 document.querySelectorAll("[data-status-filter]").forEach(function(button){button.onclick=function(){var detail=button.closest(".slot-detail"),next=detail.dataset.statusFilter===button.dataset.statusFilter?"":button.dataset.statusFilter;applyStatusFilter(detail,next)}});
 document.querySelectorAll(".type-card").forEach(function(c){c.onclick=function(){overview.hidden=true;details.forEach(function(d){var active=d.dataset.slot===c.dataset.slot;d.hidden=!active;if(active)applyStatusFilter(d,"")});scrollTo(0,0)}});
 document.querySelectorAll(".back").forEach(function(b){b.onclick=function(){showOverview();scrollTo(0,0)}});
 document.querySelectorAll("[data-card-tab]").forEach(function(button){button.onclick=function(){var card=button.closest(".result-card"),target=button.dataset.cardTab;card.querySelectorAll("[data-card-tab]").forEach(function(item){item.classList.toggle("on",item===button)});card.querySelectorAll("[data-card-page]").forEach(function(page){page.hidden=page.dataset.cardPage!==target})}});
 document.querySelectorAll("[data-version-review-save]").forEach(function(button){button.onclick=function(){var card=button.closest(".result-card"),review=button.closest(".version-review"),expected=review.querySelector("[data-version-expected]").value.trim(),actual=review.dataset.versionActual;if(!expected){alert("請先輸入預期版號");return}var status=expected===actual?"PASS":"FAILED",reason=(expected===actual?"Expected version matches captured value":"Expected version does not match captured value")+" (expected "+expected+", actual "+(actual||"ABSENT")+")";overrides[card.dataset.overrideKey]={status:status,reason:reason,expected_version:expected,actual_version:actual,updated_at:new Date().toISOString(),automation_status:card.dataset.automationStatus.toUpperCase()};if(persistOverrides()){applyManualOverride(card);refreshCounts()}}});
 document.querySelectorAll("[data-manual-save]").forEach(function(button){button.onclick=function(){var card=button.closest(".result-card"),status=card.querySelector("[data-manual-status]").value,reason=card.querySelector("[data-manual-reason]").value.trim(),previous=overrides[card.dataset.overrideKey]||{};if(!status){delete overrides[card.dataset.overrideKey];persistOverrides();applyManualOverride(card);refreshCounts();return}if(!reason){alert("Manual override 必須填寫理由");card.querySelector("[data-manual-reason]").focus();return}overrides[card.dataset.overrideKey]={status:status,reason:reason,expected_version:previous.expected_version,actual_version:previous.actual_version,updated_at:new Date().toISOString(),automation_status:card.dataset.automationStatus.toUpperCase()};if(persistOverrides()){applyManualOverride(card);refreshCounts()}}});
 document.querySelectorAll("[data-manual-reset]").forEach(function(button){button.onclick=function(){var card=button.closest(".result-card");delete overrides[card.dataset.overrideKey];if(persistOverrides()){applyManualOverride(card);refreshCounts()}}});
 document.getElementById("export-overrides").onclick=function(){var payload={schema_version:1,exported_at:new Date().toISOString(),page:location.href,overrides:overrides},blob=new Blob([JSON.stringify(payload,null,2)+"\n"],{type:"application/json"}),url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download="lazyadfinder2-manual-overrides.json";a.click();setTimeout(function(){URL.revokeObjectURL(url)},1000)};
 document.querySelectorAll(".result-card:not(.unexecuted-card)").forEach(applyManualOverride);refreshCounts();
 var lightbox=document.getElementById("image-lightbox"),lightboxImage=lightbox.querySelector("img"),lightboxCaption=lightbox.querySelector("p");
 function closeLightbox(){lightbox.hidden=true;lightboxImage.removeAttribute("src");document.body.style.overflow=""}
 document.querySelectorAll(".evidence-zoom").forEach(function(button){button.onclick=function(){var source=button.querySelector("img");lightboxImage.src=source.src;lightboxImage.alt=source.alt;lightboxCaption.textContent=source.alt;lightbox.hidden=false;document.body.style.overflow="hidden"}});
 lightbox.onclick=function(e){if(e.target===lightbox||e.target===lightboxImage)closeLightbox()};lightbox.querySelector("button").onclick=closeLightbox;
 addEventListener("keydown",function(e){if(e.key==="Escape"){if(!lightbox.hidden)closeLightbox();else if(activePage==="reports")showOverview()}});applyLanguage();update();
 var requestedSlot=new URLSearchParams(location.search).get("slot");
 if(requestedSlot){var target=Array.from(document.querySelectorAll(".type-card")).find(function(card){return card.dataset.slot===requestedSlot});if(target){var parts=requestedSlot.split(":");platform=parts[0];mode=parts[1];update();target.click()}}
})();
"""


def render(verdicts, captures, verdict_files, evidence_dirs, catalog, asset_prefix="assets"):
    _begin_report_assets(asset_prefix)
    cards, details = [], []
    catalog_by_key = {str(row["key"]): row for row in catalog}
    skip_reasons = current_skip_reasons(captures)
    latest = max(verdicts, key=lambda row: row["captured_at"], default=None)
    latest_slot = ":".join((latest["platform"], latest["mode_group"], latest["test_type"])) if latest else "aos:standalone:aibid"
    for platform, _plabel, _device in PLATFORMS:
        for mode, _mlabel in MODES:
            for kind, label, description in TYPES:
                rows = [row for row in verdicts if row["platform"] == platform and row["mode_group"] == mode and row["test_type"] == kind]
                cards.append(_slot_card(platform, mode, kind, label, description, rows))
                slot_skips = {
                    tc: reason for (skip_platform, skip_mode, skip_kind, tc), reason in skip_reasons.items()
                    if (skip_platform, skip_mode, skip_kind) == (platform, mode, kind)
                }
                details.append(_slot_detail(platform, mode, kind, label, rows, catalog_by_key, slot_skips))
    counts = Counter(row["status"] for row in verdicts)
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    roots = "、".join(html.escape(str(Path(root).expanduser())) for root in evidence_dirs)
    return f'''<!doctype html><html lang="zh-Hant" data-lang="zh" data-latest-slot="{html.escape(latest_slot, quote=True)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LazyAdFinder2</title><style>{CSS}</style></head><body>
<header class="top"><div class="brand">SDK QA Platform<small>LazyAdFinder2</small></div><nav class="main-nav"><button class="on" data-page="reports">{_bi("Latest Report", "最新報告")}</button><button data-page="catalog">{_bi("All TestCases", "總 TestCase 目錄")}</button></nav><button class="export-overrides" id="export-overrides">{_bi("Export overrides", "匯出人工覆寫")}</button><button class="language" id="language">EN</button><button class="theme" id="theme">◐</button></header>
<main><section class="app-page" id="reports-page"><div id="slot-overview"><div class="hero"><h1>{_bi("Latest Report", "最新報告")}</h1><p>{_bi("The latest result for each TestCase. Select a platform and integration mode, then open AIBID, REEN Static, or REEN Dynamic.", "顯示每條 TestCase 的最新結果。先選平台與整合模式，再進入 AIBID／REEN Static／REEN Dynamic。")}</p></div>
<div class="controls"><div class="seg platform"><button class="on" data-value="aos">AOS</button><button data-value="ios">iOS</button></div><div class="seg mode"><button class="on" data-value="standalone">Standalone</button><button data-value="mediation">Mediation</button></div><b id="result-context"></b></div>
<div class="type-grid">{"".join(cards)}</div></div>{"".join(details)}</section>
<section class="app-page" id="catalog-page" hidden><div class="catalog-head"><div><h1>{_bi("All TestCases", "總 TestCase 目錄")}</h1><p>{_bi("Applicability is separate from execution status. Use these tables to find the contract; use Latest Report for PASS, FAILED, or BLOCKED.", "適用範圍不等於執行結果。這裡用來查 TC 契約；PASS／FAILED／BLOCKED 請看最新報告。")}</p></div><b>{len(catalog)} TestCases</b></div>
{_catalog_section(catalog, catalog_by_key, "e2e", "01", "E2E TestCases", _bi("Ordered user and network journeys, with Standalone and Mediation applicability.", "依操作與流量順序排列，並標示 Standalone／Mediation 適用性。"))}
{_catalog_section(catalog, catalog_by_key, "signal", "02", "Signal TestCases", _bi("SDK fields and device signals shared by the applicable integration modes.", "SDK 欄位與裝置訊號；適用的整合模式共用同一份欄位契約。"))}</section>
<p class="meta">{_bi("Results", "結果")}: {len(verdicts)} · PASS {counts[Status.PASS.value]} · FAILED {counts[Status.FAILED.value]} · BLOCKED {counts[Status.BLOCKED.value]}<br>{_bi("Raw captures", "原始擷取")}: {len(captures)} · {_bi("Verdict files", "Verdict 檔案")}: {len(verdict_files)} · {_bi("Generated", "產生時間")}: {html.escape(generated)}<br>Evidence roots: {roots or '—'}</p></main>
<div class="image-lightbox" id="image-lightbox" role="dialog" aria-modal="true" aria-label="Evidence full image" hidden><button class="image-lightbox-close" type="button" aria-label="Close">×</button><img alt=""><p></p></div><script>{SCRIPT}</script></body></html>'''


def write_report(output, content, asset_directory=None):
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as stream:
        temporary = Path(stream.name); stream.write(content)
    os.replace(temporary, output)
    asset_directory = Path(asset_directory) if asset_directory else output.parent / f"{output.stem}_assets"
    _write_report_assets(asset_directory)
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
    verdicts = current_verdicts(verdicts, catalog, captures)
    document = render(verdicts, captures, verdict_files, evidence_dirs, catalog, asset_prefix="assets")
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
        _write_report_assets(checkout / "assets")
        subprocess.run(["git", "add", "index.html", "assets"], cwd=checkout, check=True)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=checkout).returncode != 0
        if changed:
            subprocess.run(["git", "commit", "-m", f"publish: QA report {datetime.now():%Y-%m-%d %H:%M:%S}"], cwd=checkout, check=True)
            subprocess.run(["git", "push", "origin", "HEAD:gh-pages"], cwd=checkout, check=True)
        published_revision = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=checkout, text=True
        ).strip()
    url = _pages_url(remote)
    latest = max(verdicts, key=lambda row: row["captured_at"], default=None)
    latest_slot = ":".join((latest["platform"], latest["mode_group"], latest["test_type"])) if latest else ""
    slot_query = latest_slot.replace(":", "%3A")
    public_url = f"{url}?build={published_revision}" + (f"&slot={slot_query}" if slot_query else "") if url else remote
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
    verdicts = current_verdicts(verdicts, catalog, captures)
    output_path = Path(args.out).expanduser().resolve()
    asset_name = f"{output_path.stem}_assets"
    output = write_report(
        output_path,
        render(verdicts, captures, verdict_files, args.evidence, catalog, asset_prefix=asset_name),
        asset_directory=output_path.parent / asset_name,
    )
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
