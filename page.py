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
        f'<div class="evidence-guidance"><b>Evidence 說明</b><p>{html.escape(guidance)}</p></div>'
        if guidance else ""
    )
    reference = row.get("evidence")
    if not reference:
        return guidance_html + '<div class="evidence-missing">此結果沒有 Evidence。</div>'
    target = Path(reference)
    if not target.is_absolute():
        target = row["source"].parent / target
    if not target.exists():
        return guidance_html + f'<div class="evidence-missing">找不到 {html.escape(reference)}</div>'
    mime, _encoding = mimetypes.guess_type(target.name)
    if mime and mime.startswith("image/"):
        encoded = base64.b64encode(target.read_bytes()).decode("ascii")
        return guidance_html + f'''<figure class="evidence-image"><img src="data:{mime};base64,{encoded}" alt="{html.escape(reference)}">
<figcaption>{html.escape(reference)}</figcaption></figure>'''
    if target.suffix.lower() == ".json":
        document = _load_json(target)
        expected_html = ""
        if "expected" in document:
            expected_html = f'<label>Expected</label>{_fact_list(document.get("expected"))}'
        note_html = ""
        if document.get("note"):
            note_html = f'<div class="result-note"><b>Note</b><p>{html.escape(str(document["note"]))}</p></div>'
        return guidance_html + f'''<div class="evidence-data"><b>{html.escape(reference)}</b>{expected_html}<label>Captured evidence</label>{_fact_list(document.get("actual", {}))}{note_html}</div>'''
    return guidance_html + f'<pre class="evidence-text">{html.escape(target.read_text(errors="replace"))}</pre>'


def _result_card(row, catalog_by_key):
    spec = catalog_by_key.get(row["tc"], {})
    platform_spec = spec.get(row["platform"], {}) if isinstance(spec, dict) else {}
    expected_text = str(platform_spec.get("expected") or _display(row["expected"]))
    priority = str(spec.get("priority") or "—")
    result_note = ""
    if row["reason"]:
        result_note = f'<div class="result-note"><b>Result note</b><p>{html.escape(row["reason"])}</p></div>'
    override_key = ":".join((
        row["platform"], row["mode_group"], row["test_type"], row["captured_at"], row["tc"]
    ))
    return f'''<article class="result-card" data-result-status="{row["status"].lower()}" data-automation-status="{row["status"].lower()}" data-layer="{html.escape(row["layer"])}" data-override-key="{html.escape(override_key, quote=True)}">
<div class="result-head"><div><strong>{html.escape(row["title"])}</strong>
<span class="tc-id">{html.escape(_tc_label(row["tc"], catalog_by_key))}</span></div><div class="result-badges"><span class="priority-tag">{html.escape(priority)}</span><span class="status {row["status"].lower()}">{row["status"]}</span></div></div>
<div class="card-tabs"><button class="on" data-card-tab="summary">Result</button><button data-card-tab="evidence">Evidence</button></div>
<div class="card-page" data-card-page="summary"><section class="expected-block"><label>Expected</label><p>{html.escape(expected_text)}</p></section>
<section class="actual-block"><label>Actual</label>{_fact_list(row["actual"])}</section>{result_note}</div>
<div class="card-page" data-card-page="evidence" hidden>{_evidence_content(row, str(platform_spec.get("evidence_note") or ""))}
<details class="manual-review"><summary><span>Manual override</span><span class="manual-indicator" hidden>MANUAL</span></summary><div class="manual-form"><small>Automation status：{row["status"]}</small>
<label>Status<select data-manual-status><option value="">Use automation result</option><option value="PASS">PASS</option><option value="FAILED">FAILED</option><option value="BLOCKED">BLOCKED</option></select></label>
<label>Reason<textarea data-manual-reason rows="2" placeholder="請填寫人工修改理由"></textarea></label>
<div class="manual-actions"><button data-manual-save>Save override</button><button data-manual-reset>Clear override</button></div>
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
        ("Device", device_name),
        ("System", os_text),
        ("Round", row["test_round"] or "—"),
        ("Mode", row["test_mode"] or "—"),
        ("Type", row["test_type"] or "—"),
        ("CID", row["test_cid"] or "—"),
        ("Executed", row["captured_at"] or "—"),
    )
    cells = "".join(
        f'<div><label>{html.escape(label)}</label><b>{html.escape(str(value))}</b></div>'
        for label, value in values
    )
    return f'<section class="run-info"><div class="run-info-title"><span>Latest Run</span><b>Test specification</b></div><div class="run-info-grid">{cells}</div></section>'


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
    return f'''<section class="status-filters"><div><span>Result filter</span><small>再次點擊可顯示全部</small></div><div class="status-filter-buttons">{"".join(buttons)}</div></section>'''


def _slot_card(platform, mode, kind, label, description, rows):
    signal_rows = [row for row in rows if row["layer"] != "e2e"]
    e2e_rows = [row for row in rows if row["layer"] == "e2e"]
    signal = Counter(row["status"] for row in signal_rows)
    e2e = Counter(row["status"] for row in e2e_rows)
    return f'''<button class="type-card" data-slot="{platform}:{mode}:{kind}">
<div><span class="type-id">{html.escape(kind)}</span><span class="total">{len(rows)} results</span></div>
<h3>{html.escape(label)}</h3><p>{html.escape(description)}</p>
<div class="layer-row" data-layer-row="e2e"><b>E2E</b><span class="pass-text">{e2e[Status.PASS.value]}✓</span><span class="failed-text">{e2e[Status.FAILED.value]}✗</span><span class="blocked-text">{e2e[Status.BLOCKED.value]} blocked</span><small>{len(e2e_rows)} TC</small></div>
<div class="layer-row" data-layer-row="signal"><b>Signal</b><span class="pass-text">{signal[Status.PASS.value]}✓</span><span class="failed-text">{signal[Status.FAILED.value]}✗</span><span class="blocked-text">{signal[Status.BLOCKED.value]} blocked</span><small>{len(signal_rows)} TC</small></div>
<b class="open">查看結果 →</b></button>'''


def _slot_detail(platform, mode, kind, label, rows, catalog_by_key):
    def cards_for(layer):
        selected = [row for row in rows if (row["layer"] == "e2e") == (layer == "e2e")]
        return "".join(_result_card(row, catalog_by_key) for row in sorted(
            selected, key=lambda row: (STATUS_ORDER.index(row["status"]), row["captured_at"])
        ))
    signal_cards = cards_for("signal") or '<div class="empty"><b>Signal 尚無結果</b><p>加入 Signal TC 並產生 Verdict 後顯示於此。</p></div>'
    e2e_cards = cards_for("e2e") or '<div class="empty"><b>E2E 尚未建立</b><p>位置已保留；Signal 完成後再加入完整鏈路 TC。</p></div>'
    platform_label = next(item[1] for item in PLATFORMS if item[0] == platform)
    mode_label = next(item[1] for item in MODES if item[0] == mode)
    return f'''<section class="slot-detail" data-slot="{platform}:{mode}:{kind}" hidden>
<div class="detail-bar"><button class="back">← 返回分類</button><div><span class="crumb">{platform_label} / {mode_label}</span><h2>{html.escape(label)}</h2></div></div>
{_status_filters(rows)}
{_run_information(rows)}
<div class="report-section"><div class="section-title"><span>01</span><div><h3>E2E</h3><p>Init → Bid → Render → Impression → Click → Landing</p></div></div><div class="result-grid">{e2e_cards}</div></div>
<div class="report-section"><div class="section-title"><span>02</span><div><h3>Signal</h3><p>SDK 欄位、識別碼與事件訊號</p></div></div><div class="result-grid">{signal_cards}</div></div></section>'''


def _catalog_cell(spec):
    if not spec.get("applicable", False):
        return f'<div class="na"><b>N/A</b><p>{html.escape(str(spec.get("expected", "")))}</p></div>'
    return f'''<div class="platform-spec"><b>Setup</b><p>{html.escape(str(spec.get("setup", "—")))}</p>
<b>Expected</b><p>{html.escape(str(spec.get("expected", "—")))}</p>
<b>Evidence</b><p>{html.escape(str(spec.get("evidence", "—")))}</p></div>'''


def _catalog_table(catalog, catalog_by_key):
    rows = []
    for tc in catalog:
        key = str(tc["key"])
        label = _tc_label(key, catalog_by_key)
        key_line = f'<code class="catalog-key">{html.escape(key)}</code>' if label != key else ""
        rows.append(f'''<tr><td><span class="draft">{html.escape(str(tc.get("status", "DRAFT")))}</span>
<strong class="catalog-id">{html.escape(label)}</strong>{key_line}<small>{html.escape(str(tc.get("round", "")))}</small></td>
<td><b>{html.escape(str(tc.get("title", "")))}</b><p>{html.escape(str(tc.get("layer", "Signal")))} · {html.escape(str(tc.get("category", "")))}</p>
<code>{html.escape(str(tc.get("field", "")))}</code><span class="priority">{html.escape(str(tc.get("priority", "")))}</span></td>
<td>{_catalog_cell(tc.get("aos", {}))}</td><td>{_catalog_cell(tc.get("ios", {}))}</td></tr>''')
    body = "".join(rows) or '<tr><td colspan="4" class="empty">尚未定義 TestCase。</td></tr>'
    return f'''<div class="table-wrap"><table><thead><tr><th>TestCase</th><th>目的／欄位</th><th>AOS</th><th>iOS</th></tr></thead><tbody>{body}</tbody></table></div>'''


CSS = r"""
:root{--bg:#eef1f4;--panel:#fff;--panel2:#f6f8fa;--ink:#131a21;--soft:#516069;--faint:#7d8b94;--line:#dbe2e8;--accent:#0e7c86;--accent2:#e2eff1;--aos:#2e9e5b;--ios:#3a6ea5;--pass:#2f7d3a;--fail:#c0392b;--block:#b5761a;--shadow:0 1px 2px #131a210f,0 8px 24px #131a210f;--mono:ui-monospace,SFMono-Regular,Menlo,monospace;--sans:system-ui,-apple-system,"Segoe UI","Noto Sans TC",sans-serif}
@media(prefers-color-scheme:dark){:root{--bg:#0d1216;--panel:#151d23;--panel2:#111820;--ink:#e7edf1;--soft:#a6b6c1;--faint:#71828d;--line:#243039;--accent:#38bdc9;--accent2:#123037;--aos:#4cc57d;--ios:#6ba6dd;--pass:#5cc46a;--fail:#f0766a;--block:#e0a94a;--shadow:0 10px 30px #0006}}
:root[data-theme=dark]{--bg:#0d1216;--panel:#151d23;--panel2:#111820;--ink:#e7edf1;--soft:#a6b6c1;--faint:#71828d;--line:#243039;--accent:#38bdc9;--accent2:#123037;--aos:#4cc57d;--ios:#6ba6dd;--pass:#5cc46a;--fail:#f0766a;--block:#e0a94a;--shadow:0 10px 30px #0006}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans)}button,select{font:inherit}.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:18px;padding:10px 18px;background:color-mix(in srgb,var(--panel) 90%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}.brand{font-weight:800}.brand small{display:block;color:var(--faint);font:10px var(--mono)}.main-nav{display:flex;gap:4px}.main-nav button,.seg button,.back,.theme{border:1px solid transparent;background:transparent;color:var(--soft);padding:7px 12px;border-radius:8px;cursor:pointer}.main-nav button.on,.seg button.on{background:var(--accent2);color:var(--accent);font-weight:750}.theme{margin-left:auto;border-color:var(--line)}main{max-width:1180px;margin:auto;padding:25px 20px 50px}.hero h1{margin:0;font-size:23px}.hero p{color:var(--soft)}.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:20px 0}.seg{display:flex;gap:3px;padding:3px;background:var(--panel);border:1px solid var(--line);border-radius:11px}.seg.platform button[data-value=aos].on{color:var(--aos)}.seg.platform button[data-value=ios].on{color:var(--ios)}.type-grid,.result-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:15px}.type-card{background:var(--panel);color:inherit;text-align:left;border:1px solid var(--line);border-radius:14px;padding:17px;box-shadow:var(--shadow);cursor:pointer;transition:.15s}.type-card:hover{transform:translateY(-2px);border-color:var(--accent)}.type-card>div:first-child,.result-head,footer{display:flex;justify-content:space-between;gap:10px}.type-id,.tc-id,.crumb{font:700 11px var(--mono);color:var(--accent)}.total{color:var(--faint);font-size:11px}.type-card h3{margin:8px 0 2px;font-size:18px}.type-card p{color:var(--soft);min-height:40px}.counts{display:flex;gap:10px;font-size:10px}.pass-text{color:var(--pass)}.failed-text{color:var(--fail)}.blocked-text{color:var(--block)}.open{display:block;color:var(--accent);margin-top:15px;font-size:12px}.detail-bar{display:flex;align-items:center;gap:15px;margin-bottom:18px}.detail-bar h2{margin:2px 0}.back{border-color:var(--line);background:var(--panel)}.result-card{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:15px;box-shadow:var(--shadow)}.result-head>div{display:flex;flex-direction:column}.status{font:750 11px var(--mono);padding:4px 9px;border-radius:999px;height:max-content}.status.pass{color:var(--pass);background:#2f7d3a20}.status.failed{color:var(--fail);background:#c0392b20}.status.blocked{color:var(--block);background:#b5761a20}.context{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}.context span{background:var(--panel2);padding:4px 7px;border-radius:6px;font-size:11px}.answers{display:grid;grid-template-columns:1fr 1fr;gap:9px}.answers label,.platform-spec>b{font-size:10px;color:var(--faint);text-transform:uppercase}.answers pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--panel2);padding:9px;border-radius:7px;min-height:50px;font:12px var(--mono)}footer{color:var(--faint);font-size:11px}a{color:var(--accent)}.missing{color:var(--fail);text-decoration:line-through}.empty{padding:45px;text-align:center;background:var(--panel);border:1px dashed var(--line);border-radius:13px;color:var(--soft)}.catalog-head{display:flex;justify-content:space-between;align-items:end;gap:20px}.catalog-head p{color:var(--soft)}.table-wrap{overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}table{border-collapse:collapse;width:100%;min-width:980px}th,td{text-align:left;vertical-align:top;padding:14px;border-bottom:1px solid var(--line)}th{position:sticky;top:0;background:var(--panel2);font-size:11px;color:var(--faint);text-transform:uppercase}td:first-child{width:120px}.catalog-id{display:block;font:750 13px var(--mono);margin:7px 0}.draft,.priority{display:inline-block;padding:2px 6px;border-radius:5px;background:var(--accent2);color:var(--accent);font:700 9px var(--mono)}td code{color:var(--accent)}.priority{margin-left:7px}.platform-spec p,.na p{margin:3px 0 10px;color:var(--soft);min-width:250px}.na{color:var(--faint)}.meta{color:var(--faint);font-size:11px;margin-top:20px}[hidden]{display:none!important}@media(max-width:650px){main{padding:18px 12px}.top{flex-wrap:wrap}.main-nav{order:3;width:100%}.answers{grid-template-columns:1fr}}
.layer-row{display:grid;grid-template-columns:58px 26px 26px 1fr auto;gap:6px;align-items:center;padding:7px 0;border-top:1px solid var(--line);font-size:10px}.layer-row>b{font-size:11px}.layer-row small{color:var(--faint)}
.report-section{margin:22px 0 32px}.section-title{display:flex;align-items:center;gap:11px;margin-bottom:12px}.section-title>span{font:800 11px var(--mono);color:var(--accent);background:var(--accent2);padding:6px;border-radius:7px}.section-title h3,.section-title p{margin:0}.section-title p{color:var(--faint);font-size:11px}
.result-badges{align-items:flex-end;gap:5px}.priority-tag{font:800 11px var(--mono);padding:4px 8px;border-radius:999px;background:var(--accent2);color:var(--accent)}.card-tabs{display:flex;gap:4px;margin:14px 0 11px;border-bottom:1px solid var(--line)}.card-tabs button{border:0;border-bottom:2px solid transparent;background:transparent;color:var(--faint);padding:6px 9px;cursor:pointer}.card-tabs button.on{color:var(--accent);border-bottom-color:var(--accent);font-weight:750}.card-page{min-height:250px}.expected-block,.actual-block{background:var(--panel2);border-radius:9px;padding:11px 12px;margin-bottom:10px}.expected-block label,.actual-block label,.run-info label{display:block;color:var(--faint);font-size:9px;font-weight:750;letter-spacing:.08em;text-transform:uppercase}.expected-block p{margin:5px 0 0;color:var(--ink);line-height:1.6}.facts{margin:5px 0 0}.facts>div{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-top:1px solid var(--line)}.facts>div:first-child{border-top:0}.facts dt{color:var(--soft)}.facts dd{margin:0;text-align:right;font:650 11px var(--mono);overflow-wrap:anywhere}.result-note{border-left:3px solid var(--block);padding:7px 10px}.result-note p{margin:3px 0}.evidence-guidance{border-left:3px solid var(--accent);background:var(--accent2);border-radius:0 9px 9px 0;padding:10px 12px;margin-bottom:10px}.evidence-guidance p{margin:4px 0 0;line-height:1.55}.evidence-image{margin:0;display:flex;flex-direction:column;align-items:center}.evidence-image img{display:block;max-width:100%;height:390px;object-fit:contain;border-radius:9px;background:#000}.evidence-image figcaption{color:var(--faint);font:10px var(--mono);margin-top:6px}.evidence-data,.evidence-text{background:var(--panel2);border-radius:9px;padding:12px;overflow:auto}.evidence-data>b{display:block;margin-bottom:8px}.evidence-missing{padding:45px 10px;text-align:center;color:var(--fail)}.run-info{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px;box-shadow:var(--shadow);margin-bottom:24px}.run-info-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.run-info-title span{font:750 10px var(--mono);color:var(--accent)}.run-info-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:9px;overflow:hidden}.run-info-grid>div{background:var(--panel2);padding:10px}.run-info-grid b{display:block;margin-top:3px;font-size:12px;overflow-wrap:anywhere}
.status-filters{display:flex;justify-content:space-between;align-items:center;gap:15px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 15px;box-shadow:var(--shadow);margin-bottom:12px}.status-filters>div:first-child{display:flex;flex-direction:column}.status-filters>div:first-child>span{font-weight:800}.status-filters small{color:var(--faint)}.status-filter-buttons{display:flex;gap:7px;flex-wrap:wrap}.status-filter{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:999px;background:var(--panel2);color:var(--soft);padding:6px 10px;cursor:pointer}.status-filter span{font:750 10px var(--mono)}.status-filter b{min-width:20px;text-align:center;border-radius:999px;background:var(--panel);padding:1px 5px}.status-filter.pass{color:var(--pass)}.status-filter.failed{color:var(--fail)}.status-filter.blocked{color:var(--block)}.status-filter.on{color:#fff;border-color:transparent}.status-filter.pass.on{background:var(--pass)}.status-filter.failed.on{background:var(--fail)}.status-filter.blocked.on{background:var(--block)}.status-filter.on b{color:var(--ink)}.status-filter:disabled{opacity:.4;cursor:not-allowed}@media(max-width:650px){.status-filters{align-items:flex-start;flex-direction:column}.status-filter-buttons{width:100%}.status-filter{flex:1;justify-content:center}}
.manual-review{margin-top:10px}.manual-review summary{display:flex;align-items:center;gap:6px;width:max-content;margin-left:auto;color:var(--faint);font:700 9px var(--mono);cursor:pointer;list-style:none}.manual-review summary::-webkit-details-marker{display:none}.manual-review summary:before{content:"＋"}.manual-review[open] summary:before{content:"−"}.manual-form{margin-top:7px;padding:9px 10px;background:var(--panel2);border:1px solid var(--line);border-radius:8px}.manual-form>small{color:var(--faint);font-size:9px}.manual-indicator{font:800 8px var(--mono);color:#fff;background:var(--block);padding:2px 5px;border-radius:999px}.manual-review label{display:block;color:var(--faint);font-size:9px;font-weight:750;margin-top:6px}.manual-review select,.manual-review textarea{display:block;width:100%;margin-top:3px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);padding:5px 6px}.manual-review textarea{resize:vertical;font:11px var(--sans)}.manual-actions{display:flex;gap:6px;margin-top:7px}.manual-actions button,.export-overrides{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:7px;padding:5px 8px;cursor:pointer}.manual-actions button:first-child{background:var(--accent);border-color:var(--accent);color:#fff}.manual-saved{margin-top:7px;padding:6px 8px;border-left:3px solid var(--block);background:var(--panel);font-size:10px;white-space:pre-wrap}.export-overrides{margin-left:auto}.theme{margin-left:0}
"""


SCRIPT = r"""
(function(){
 var root=document.documentElement,platform="aos",mode="standalone",activePage="reports";
 var overrideStorageKey="laf2-manual-overrides-v1",overrides={};
 try{var saved=localStorage.getItem("laf2-theme");if(saved)root.dataset.theme=saved}catch(e){}
 try{overrides=JSON.parse(localStorage.getItem(overrideStorageKey)||"{}")||{}}catch(e){overrides={}}
 document.getElementById("theme").onclick=function(){var dark=root.dataset.theme==="dark";root.dataset.theme=dark?"light":"dark";try{localStorage.setItem("laf2-theme",root.dataset.theme)}catch(e){}};
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
   if(typeCard){typeCard.querySelector(".total").textContent=cards.length+" results";["e2e","signal"].forEach(function(layer){var rows=cards.filter(function(card){return card.dataset.layer===layer}),row=typeCard.querySelector('[data-layer-row="'+layer+'"]'),layerCounts={pass:0,failed:0,blocked:0};rows.forEach(function(card){layerCounts[card.dataset.resultStatus]++});row.querySelector(".pass-text").textContent=layerCounts.pass+"✓";row.querySelector(".failed-text").textContent=layerCounts.failed+"✗";row.querySelector(".blocked-text").textContent=layerCounts.blocked+" blocked";row.querySelector("small").textContent=rows.length+" TC"})}
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
 addEventListener("keydown",function(e){if(e.key==="Escape")showOverview()});update();
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
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LazyAdFinder2</title><style>{CSS}</style></head><body>
<header class="top"><div class="brand">SDK QA Platform<small>LazyAdFinder2</small></div><nav class="main-nav"><button class="on" data-page="reports">Round Reports</button><button data-page="catalog">TestCase Catalog</button></nav><button class="export-overrides" id="export-overrides">Export overrides</button><button class="theme" id="theme">◐</button></header>
<main><section class="app-page" id="reports-page"><div id="slot-overview"><div class="hero"><h1>Round Reports</h1><p>一個 Round 同時包含 Signal 與 E2E。先選平台與整合模式，再進入 AIBID／REEN Static／REEN Dynamic。</p></div>
<div class="controls"><div class="seg platform"><button class="on" data-value="aos">AOS</button><button data-value="ios">iOS</button></div><div class="seg mode"><button class="on" data-value="standalone">Standalone</button><button data-value="mediation">Mediation</button></div><b id="result-context"></b></div>
<div class="type-grid">{"".join(cards)}</div></div>{"".join(details)}</section>
<section class="app-page" id="catalog-page" hidden><div class="catalog-head"><div><h1>TestCase Catalog</h1><p>整理 Signal 與 E2E 的全部 TC；Draft 不是測試結果，只有 Verdict 才會是 PASS／FAILED／BLOCKED。</p></div><b>{len(catalog)} TestCases</b></div>{_catalog_table(catalog, catalog_by_key)}</section>
<p class="meta">Results: {len(verdicts)} · PASS {counts[Status.PASS.value]} · FAILED {counts[Status.FAILED.value]} · BLOCKED {counts[Status.BLOCKED.value]}<br>Raw captures: {len(captures)} · Verdict files: {len(verdict_files)} · Generated: {html.escape(generated)}<br>Evidence roots: {roots or '—'}</p></main><script>{SCRIPT}</script></body></html>'''


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
