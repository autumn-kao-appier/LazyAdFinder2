#!/usr/bin/env python3
"""Render a testcase-first HTML report from LazyAdFinder verdicts.

The home page is generated from the TC ids that actually exist in
``verdicts.json``.  Selecting a testcase opens its AOS/iOS results, with
TEST_MODE and TEST_TYPE available as filters.  No testcase catalog or expected
answer is hard-coded here; Page validates and presents Verdict data only.
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from verdict import Status


VERDICTS_FILE = "verdicts.json"
METADATA_FILE = "metadata.json"
VALID_STATUSES = {status.value for status in Status}
STATUS_ORDER = (Status.FAILED.value, Status.BLOCKED.value, Status.PASS.value)
PLATFORMS = (("aos", "AOS", "Android"), ("ios", "iOS", "Apple"))
TEST_MODES = (
    ("standalone", "Standalone"),
    ("admob-mediation", "AdMob Mediation"),
    ("applovin-mediation", "AppLovin Mediation"),
)
TEST_TYPES = (
    ("aibid", "AIBID"),
    ("reen-static", "REEN Static"),
    ("reen-dynamic", "REEN Dynamic"),
)


class ReportError(RuntimeError):
    pass


def _load_json(path):
    try:
        return json.loads(path.read_text())
    except OSError as exc:
        raise ReportError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReportError(f"Invalid JSON in {path}: {exc}") from exc


def _metadata_for(verdict_path):
    metadata_path = verdict_path.parent / METADATA_FILE
    if not metadata_path.exists():
        return {}
    document = _load_json(metadata_path)
    if not isinstance(document, dict):
        raise ReportError(f"{metadata_path} must contain an object")
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


def _verdict_rows(document, path):
    if isinstance(document, list):
        rows = document
    elif isinstance(document, dict) and isinstance(document.get("verdicts"), list):
        rows = document["verdicts"]
    else:
        raise ReportError(f"{path} must contain a list or {{\"verdicts\": [...]}}")

    metadata = _metadata_for(path)
    config = metadata.get("config") if isinstance(metadata.get("config"), dict) else {}
    platform = _platform_of(metadata, path)
    normalized = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ReportError(f"{path}: verdict #{index + 1} must be an object")
        tc = row.get("tc")
        status = row.get("status")
        reason = row.get("reason", "")
        if not isinstance(tc, str) or not tc.strip():
            raise ReportError(f"{path}: verdict #{index + 1} has no TC id")
        if status not in VALID_STATUSES:
            raise ReportError(f"{path}: {tc} has invalid status {status!r}")
        if not isinstance(reason, str):
            raise ReportError(f"{path}: {tc} reason must be a string")
        evidence = row.get("evidence")
        if status == Status.BLOCKED.value:
            if not reason.strip():
                raise ReportError(f"{path}: BLOCKED verdict {tc} requires a reason")
            if any(row.get(key) is not None for key in ("expected", "actual", "evidence")):
                raise ReportError(f"{path}: BLOCKED verdict {tc} cannot claim an answer")
        elif not isinstance(evidence, str) or not evidence.strip():
            raise ReportError(f"{path}: evaluated verdict {tc} requires evidence")

        title = row.get("title", tc)
        description = row.get("description", "")
        if not isinstance(title, str) or not isinstance(description, str):
            raise ReportError(f"{path}: {tc} title/description must be strings")
        normalized.append({
            "tc": tc.strip(),
            "title": title.strip() or tc.strip(),
            "description": description.strip(),
            "status": status,
            "reason": reason,
            "expected": row.get("expected"),
            "actual": row.get("actual"),
            "evidence": evidence,
            "source": path,
            "platform": platform,
            "test_mode": str(config.get("test_mode", "")).strip().lower(),
            "test_type": str(config.get("test_type", "")).strip().lower(),
            "captured_at": str(metadata.get("captured_at", "")),
            "capture_name": str(metadata.get("capture_name", "")),
            "device": metadata.get("device") if isinstance(metadata.get("device"), dict) else {},
        })
    return normalized


def discover(evidence_dirs):
    verdicts, captures, verdict_files, seen = [], [], [], set()
    for root_value in evidence_dirs:
        root = Path(root_value).expanduser().resolve()
        if not root.exists():
            continue
        captures.extend(path.parent for path in sorted(root.rglob(METADATA_FILE)))
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


def _evidence_link(row):
    reference = row.get("evidence")
    if not reference:
        return "—"
    target = Path(reference)
    if not target.is_absolute():
        target = row["source"].parent / target
    label = html.escape(reference)
    if target.exists():
        uri = html.escape(target.resolve().as_uri(), quote=True)
        return f'<a class="ev" href="{uri}">{label} ↗</a>'
    return f'<span class="missing" title="Evidence path does not exist">{label}</span>'


def _percent(count, total):
    return round(count / total * 100, 2) if total else 0


def _tc_groups(verdicts):
    groups = {}
    for row in verdicts:
        groups.setdefault(row["tc"], []).append(row)
    return groups


def _home_card(index, tc, rows):
    counts = Counter(row["status"] for row in rows)
    title = next((row["title"] for row in rows if row["title"] != tc), tc)
    description = next((row["description"] for row in rows if row["description"]), "")
    platforms = {row["platform"] for row in rows}
    total = len(rows)
    platform_chips = "".join(
        f'<span class="platform-chip {pid}">{label}</span>'
        for pid, label, _ in PLATFORMS if pid in platforms
    ) or '<span class="platform-chip unknown">未標記平台</span>'
    return (
        f'<button class="tc-card" data-detail="detail-{index}">'
        '<div class="card-top"><div>'
        f'<div class="tc-id">{html.escape(tc)}</div>'
        f'<div class="tc-title">{html.escape(title)}</div></div>'
        f'<span class="result-count">{total} result{"s" if total != 1 else ""}</span></div>'
        f'<p class="tc-description">{html.escape(description or "尚未提供 TC 說明")}</p>'
        f'<div class="platform-chips">{platform_chips}</div>'
        '<div class="status-bar">'
        f'<i class="failed" style="width:{_percent(counts[Status.FAILED.value], total)}%"></i>'
        f'<i class="blocked" style="width:{_percent(counts[Status.BLOCKED.value], total)}%"></i>'
        f'<i class="pass" style="width:{_percent(counts[Status.PASS.value], total)}%"></i></div>'
        '<div class="card-counts">'
        f'<span class="failed-text">{counts[Status.FAILED.value]} FAILED</span>'
        f'<span class="blocked-text">{counts[Status.BLOCKED.value]} BLOCKED</span>'
        f'<span class="pass-text">{counts[Status.PASS.value]} PASS</span></div>'
        '<div class="card-open">查看 AOS / iOS 結果 →</div></button>'
    )


def _result_card(row):
    device = row["device"]
    device_name = device.get("model") or device.get("name") or "—"
    status = row["status"].lower()
    return (
        f'<article class="result-card" data-platform="{html.escape(row["platform"])}" '
        f'data-mode="{html.escape(row["test_mode"])}" data-type="{html.escape(row["test_type"])}">'
        '<div class="result-head">'
        f'<span class="status {status}">{html.escape(row["status"])}</span>'
        f'<span class="capture">{html.escape(row["capture_name"] or "capture")}</span></div>'
        '<div class="context">'
        f'<span><b>Mode</b>{html.escape(row["test_mode"] or "—")}</span>'
        f'<span><b>Type</b>{html.escape(row["test_type"] or "—")}</span>'
        f'<span><b>Device</b>{html.escape(str(device_name))}</span></div>'
        '<div class="answer-grid">'
        f'<div><label>Expected</label><pre>{html.escape(_display(row["expected"]))}</pre></div>'
        f'<div><label>Actual</label><pre>{html.escape(_display(row["actual"]))}</pre></div></div>'
        f'<p class="reason"><b>Reason</b> {html.escape(row["reason"] or "—")}</p>'
        '<div class="result-foot">'
        f'{_evidence_link(row)}<time>{html.escape(row["captured_at"] or "—")}</time></div>'
        '</article>'
    )


def _filter_options(items):
    return '<option value="">全部</option>' + "".join(
        f'<option value="{html.escape(value)}">{html.escape(label)}</option>'
        for value, label in items
    )


def _detail(index, tc, rows):
    title = next((row["title"] for row in rows if row["title"] != tc), tc)
    description = next((row["description"] for row in rows if row["description"]), "")
    tabs = "".join(
        f'<button data-platform="{pid}"{" class=on" if n == 0 else ""}>'
        f'<span class="dot"></span>{label}</button>'
        for n, (pid, label, _device) in enumerate(PLATFORMS)
    )
    panes = []
    for n, (pid, label, device) in enumerate(PLATFORMS):
        platform_rows = [row for row in rows if row["platform"] == pid]
        cards = "".join(_result_card(row) for row in sorted(
            platform_rows,
            key=lambda row: (STATUS_ORDER.index(row["status"]), row["captured_at"]),
        ))
        if not cards:
            cards = (
                '<div class="platform-empty"><b>尚無結果</b>'
                f'<span>{html.escape(label)} 尚未產生這條 TC 的 Verdict。</span></div>'
            )
        panes.append(
            f'<section class="platform-pane" data-platform="{pid}"{"" if n == 0 else " hidden"}>'
            f'<div class="pane-title"><span class="platform-chip {pid}">{label}</span>'
            f'<b>{device}</b><span class="visible-count"></span></div>'
            f'<div class="result-grid">{cards}</div></section>'
        )
    return (
        f'<section class="detail" id="detail-{index}" hidden>'
        '<div class="detail-bar"><button class="back">← TestCase 清單</button>'
        f'<div><span class="detail-id">{html.escape(tc)}</span>'
        f'<strong>{html.escape(title)}</strong></div></div>'
        f'<p class="detail-description">{html.escape(description or "尚未提供 TC 說明")}</p>'
        '<div class="detail-tools"><div class="seg platform-tabs">'
        f'{tabs}</div><label>TEST_MODE<select class="mode-filter">{_filter_options(TEST_MODES)}</select></label>'
        f'<label>TEST_TYPE<select class="type-filter">{_filter_options(TEST_TYPES)}</select></label></div>'
        + "".join(panes) + '</section>'
    )


CSS = r"""
:root{--bg:#eef1f4;--panel:#fff;--panel-2:#f6f8fa;--ink:#131a21;--soft:#516069;
--faint:#7d8b94;--line:#dbe2e8;--accent:#0e7c86;--accent-soft:#e2eff1;
--aos:#2e9e5b;--aos-soft:#e4f4ea;--ios:#3a6ea5;--ios-soft:#e6eef7;
--pass:#2f7d3a;--pass-bg:#e5f4e8;--failed:#c0392b;--failed-bg:#fbe9e7;
--blocked:#b5761a;--blocked-bg:#fff2dc;--shadow:0 1px 2px #131a210f,0 8px 24px #131a210f;
--mono:ui-monospace,SFMono-Regular,Menlo,monospace;--sans:system-ui,-apple-system,"Segoe UI","Noto Sans TC",sans-serif}
@media(prefers-color-scheme:dark){:root{--bg:#0d1216;--panel:#151d23;--panel-2:#111820;
--ink:#e7edf1;--soft:#a6b6c1;--faint:#71828d;--line:#243039;--accent:#38bdc9;
--accent-soft:#123037;--aos:#4cc57d;--aos-soft:#12281b;--ios:#6ba6dd;--ios-soft:#132132;
--pass:#5cc46a;--pass-bg:#163520;--failed:#f0766a;--failed-bg:#3b1c1a;
--blocked:#e0a94a;--blocked-bg:#392b13;--shadow:0 10px 30px #0006}}
:root[data-theme=light]{color-scheme:light}:root[data-theme=dark]{color-scheme:dark;
--bg:#0d1216;--panel:#151d23;--panel-2:#111820;--ink:#e7edf1;--soft:#a6b6c1;
--faint:#71828d;--line:#243039;--accent:#38bdc9;--accent-soft:#123037;
--aos:#4cc57d;--aos-soft:#12281b;--ios:#6ba6dd;--ios-soft:#132132;
--pass:#5cc46a;--pass-bg:#163520;--failed:#f0766a;--failed-bg:#3b1c1a;
--blocked:#e0a94a;--blocked-bg:#392b13;--shadow:0 10px 30px #0006}
*{box-sizing:border-box}html,body{margin:0;min-height:100%}body{background:var(--bg);color:var(--ink);
font:14px/1.5 var(--sans);-webkit-font-smoothing:antialiased}.top{position:sticky;top:0;z-index:20;
height:52px;display:flex;align-items:center;padding:0 18px;background:color-mix(in srgb,var(--panel) 88%,transparent);
backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}.brand{display:flex;align-items:center;gap:10px}
.sig{width:25px;height:25px;border-radius:7px;background:linear-gradient(135deg,var(--accent),#0a5c64);position:relative}
.sig:after{content:"";position:absolute;inset:7px;border:2px solid #fff;border-right-color:transparent;
border-bottom-color:transparent;transform:rotate(45deg)}.brand strong{display:block;font-size:13px}.brand small{color:var(--faint);font-size:10px;letter-spacing:.08em}
.theme{margin-left:auto;width:30px;height:30px;border:1px solid var(--line);border-radius:8px;background:var(--panel-2);color:var(--ink);cursor:pointer}
main{max-width:1180px;margin:auto;padding:26px 20px 48px}.lead{color:var(--soft);max-width:72ch;margin:3px 0 24px}.overview h1{font-size:22px;margin:0}
.summary{display:flex;gap:16px;flex-wrap:wrap;color:var(--faint);font-size:12px;margin-bottom:18px}.summary b{color:var(--ink)}
.tc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,280px),1fr));gap:15px}.tc-card{
font:inherit;color:inherit;text-align:left;cursor:pointer;background:var(--panel);border:1px solid var(--line);
border-radius:14px;padding:17px;box-shadow:var(--shadow);transition:.14s;position:relative;overflow:hidden}
.tc-card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--accent)}
.tc-card:hover{transform:translateY(-2px);border-color:var(--accent)}.card-top{display:flex;justify-content:space-between;gap:12px}.tc-id{font:700 13px var(--mono);color:var(--accent)}
.tc-title{font-weight:700;font-size:15px;margin-top:3px}.result-count{font-size:11px;color:var(--faint);white-space:nowrap}.tc-description{color:var(--soft);min-height:42px}
.platform-chips{display:flex;gap:6px}.platform-chip{font:700 11px var(--mono);padding:3px 9px;border-radius:7px}.platform-chip.aos{color:var(--aos);background:var(--aos-soft)}
.platform-chip.ios{color:var(--ios);background:var(--ios-soft)}.platform-chip.unknown{color:var(--faint);background:var(--panel-2)}
.status-bar{display:flex;height:7px;background:var(--panel-2);border-radius:5px;overflow:hidden;margin-top:13px}.status-bar i{height:100%}.status-bar .pass{background:var(--pass)}
.status-bar .failed{background:var(--failed)}.status-bar .blocked{background:var(--blocked)}.card-counts{display:flex;gap:10px;flex-wrap:wrap;font-size:10px;margin-top:7px}
.pass-text{color:var(--pass)}.failed-text{color:var(--failed)}.blocked-text{color:var(--blocked)}.card-open{margin-top:14px;color:var(--accent);font-weight:650;font-size:12px}
.empty{padding:52px;text-align:center;background:var(--panel);border:1px solid var(--line);border-radius:14px}.empty p{color:var(--soft)}
.detail-bar{display:flex;align-items:center;gap:14px}.back{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:8px;padding:7px 12px;cursor:pointer}.detail-id{font:700 12px var(--mono);color:var(--accent);margin-right:9px}
.detail-description{color:var(--soft)}.detail-tools{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:8px 0 17px;border-bottom:1px solid var(--line)}
.seg{display:flex;padding:2px;border:1px solid var(--line);border-radius:9px;background:var(--panel-2)}.seg button{border:0;background:transparent;color:var(--soft);padding:6px 14px;border-radius:7px;cursor:pointer;font:650 12px var(--sans)}
.seg button.on{background:var(--panel);color:var(--ink);box-shadow:var(--shadow)}.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:currentColor;margin-right:6px}
.detail-tools label{font-size:10px;color:var(--faint);letter-spacing:.04em}.detail-tools select{display:block;margin-top:2px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--ink);padding:5px 8px}
.platform-pane{padding-top:18px}.pane-title{display:flex;align-items:center;gap:9px;margin-bottom:12px}.pane-title>span:last-child{color:var(--faint);font-size:12px}
.result-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,350px),1fr));gap:14px}.result-card{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:15px;box-shadow:var(--shadow)}
.result-head,.result-foot{display:flex;justify-content:space-between;gap:10px;align-items:center}.status{font:750 11px var(--mono);padding:4px 9px;border-radius:999px}.status.pass{color:var(--pass);background:var(--pass-bg)}
.status.failed{color:var(--failed);background:var(--failed-bg)}.status.blocked{color:var(--blocked);background:var(--blocked-bg)}.capture,time{font-size:11px;color:var(--faint)}
.context{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}.context span{font-size:11px;background:var(--panel-2);border-radius:6px;padding:4px 7px}.context b{color:var(--faint);margin-right:5px}
.answer-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px}.answer-grid label{font-size:10px;color:var(--faint);text-transform:uppercase}.answer-grid pre{margin:3px 0 0;padding:9px;background:var(--panel-2);border-radius:7px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.45 var(--mono);min-height:50px}
.reason{color:var(--soft);font-size:12px}.reason b{color:var(--ink)}.ev{color:var(--accent);font-size:12px}.missing{color:var(--failed);text-decoration:line-through}.platform-empty{display:flex;flex-direction:column;gap:5px;padding:35px;text-align:center;background:var(--panel);border:1px dashed var(--line);border-radius:12px;color:var(--soft)}
.meta{margin-top:22px;color:var(--faint);font-size:11px;overflow-wrap:anywhere}[hidden]{display:none!important}@media(max-width:620px){main{padding:20px 12px}.answer-grid{grid-template-columns:1fr}.detail-tools{align-items:flex-end}}
"""


SCRIPT = r"""
(function(){
  var root=document.documentElement,key="laf2-theme";
  try{var saved=localStorage.getItem(key);if(saved)root.dataset.theme=saved}catch(e){}
  function theme(){return root.dataset.theme||(matchMedia("(prefers-color-scheme:dark)").matches?"dark":"light")}
  document.getElementById("theme").onclick=function(){root.dataset.theme=theme()==="dark"?"light":"dark";try{localStorage.setItem(key,root.dataset.theme)}catch(e){}};
  var overview=document.getElementById("overview"),details=[].slice.call(document.querySelectorAll(".detail"));
  function filter(detail){
    var platform=detail.querySelector(".platform-tabs button.on").dataset.platform;
    var mode=detail.querySelector(".mode-filter").value,type=detail.querySelector(".type-filter").value;
    detail.querySelectorAll(".platform-pane").forEach(function(pane){pane.hidden=pane.dataset.platform!==platform});
    var pane=detail.querySelector('.platform-pane[data-platform="'+platform+'"]'),visible=0;
    if(!pane)return;
    pane.querySelectorAll(".result-card").forEach(function(card){
      var show=(!mode||card.dataset.mode===mode)&&(!type||card.dataset.type===type);card.hidden=!show;if(show)visible++;
    });
    var count=pane.querySelector(".visible-count");if(count)count.textContent=visible?visible+" 筆符合":"沒有符合篩選的結果";
  }
  function openDetail(id){overview.hidden=true;details.forEach(function(d){d.hidden=d.id!==id});var d=document.getElementById(id);if(d){d.hidden=false;filter(d)}scrollTo(0,0)}
  function home(){details.forEach(function(d){d.hidden=true});overview.hidden=false;scrollTo(0,0)}
  document.querySelectorAll(".tc-card").forEach(function(card){card.onclick=function(){openDetail(card.dataset.detail)}});
  document.querySelectorAll(".detail").forEach(function(detail){
    detail.querySelector(".back").onclick=home;
    detail.querySelectorAll(".platform-tabs button").forEach(function(button){button.onclick=function(){detail.querySelectorAll(".platform-tabs button").forEach(function(b){b.classList.toggle("on",b===button)});filter(detail)}});
    detail.querySelector(".mode-filter").onchange=function(){filter(detail)};detail.querySelector(".type-filter").onchange=function(){filter(detail)};
  });
  addEventListener("keydown",function(e){if(e.key==="Escape")home()});
})();
"""


def render(verdicts, captures, verdict_files, evidence_dirs):
    groups = _tc_groups(verdicts)
    ordered_groups = sorted(groups.items(), key=lambda item: item[0])
    cards = "".join(_home_card(index, tc, rows) for index, (tc, rows) in enumerate(ordered_groups))
    details = "".join(_detail(index, tc, rows) for index, (tc, rows) in enumerate(ordered_groups))
    if not cards:
        cards = (
            '<section class="empty"><h2>TestCase 清單尚未建立</h2>'
            '<p>加入第一條 TC 並產生 <code>verdicts.json</code> 後，這裡才會出現第一張卡片。'
            '<br>Page 不會預建或硬編任何 TestCase。</p></section>'
        )
    counts = Counter(row["status"] for row in verdicts)
    roots = "、".join(html.escape(str(Path(root).expanduser())) for root in evidence_dirs)
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LazyAdFinder2 · TestCase Report</title><style>{CSS}</style></head><body>
<header class="top"><div class="brand"><div class="sig"></div><div><strong>SDK TestCase Report</strong><small>Appier · LazyAdFinder2</small></div></div><button class="theme" id="theme" aria-label="切換深淺色">◐</button></header>
<main><section class="overview" id="overview"><h1>TestCase 清單</h1>
<p class="lead">卡片依實際 Verdict 動態產生。點入 TestCase 後，可分別查看 AOS／iOS，並用 TEST_MODE 與 TEST_TYPE 篩選結果。</p>
<div class="summary"><span><b>{len(groups)}</b> TestCases</span><span><b>{len(verdicts)}</b> Results</span><span class="failed-text">{counts[Status.FAILED.value]} FAILED</span><span class="blocked-text">{counts[Status.BLOCKED.value]} BLOCKED</span><span class="pass-text">{counts[Status.PASS.value]} PASS</span></div>
<div class="tc-grid">{cards}</div></section>{details}
<p class="meta">Raw captures: {len(captures)} · Verdict files: {len(verdict_files)} · Generated: {html.escape(generated)}<br>Evidence roots: {roots or '—'}</p></main>
<script>{SCRIPT}</script></body></html>'''


def write_report(output, content):
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    os.replace(temporary, output)
    return output


def _origin_url():
    try:
        return subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=Path(__file__).parent,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise ReportError(
            "Git remote 'origin' is not configured; cannot publish GitHub Pages"
        ) from exc


def _pages_url(remote):
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", remote)
    if not match:
        return ""
    owner, repository = match.groups()
    return f"https://{owner}.github.io/{repository}/"


def publish(evidence_dirs, remote=None, open_page=True):
    """Render and push ``index.html`` to the origin repository's gh-pages branch."""
    remote = remote or _origin_url()
    verdicts, captures, verdict_files = discover(evidence_dirs)
    document = render(verdicts, captures, verdict_files, evidence_dirs)
    with tempfile.TemporaryDirectory(prefix="lazyadfinder2-pages-") as temp:
        checkout = Path(temp) / "pages"
        branch_exists = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", remote, "gh-pages"],
            text=True,
            capture_output=True,
        ).returncode == 0
        if branch_exists:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", "gh-pages", remote, str(checkout)],
                check=True,
            )
        else:
            subprocess.run(["git", "clone", "--depth", "1", remote, str(checkout)], check=True)
            subprocess.run(["git", "switch", "--orphan", "gh-pages"], cwd=checkout, check=True)
            subprocess.run(
                ["git", "rm", "-rf", "--ignore-unmatch", "."],
                cwd=checkout,
                check=True,
                stdout=subprocess.DEVNULL,
            )
        (checkout / "index.html").write_text(document, encoding="utf-8")
        subprocess.run(["git", "add", "index.html"], cwd=checkout, check=True)
        changed = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=checkout
        ).returncode != 0
        if changed:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            subprocess.run(
                ["git", "commit", "-m", f"publish: QA report {stamp}"],
                cwd=checkout,
                check=True,
            )
            subprocess.run(
                ["git", "push", "origin", "HEAD:gh-pages"], cwd=checkout, check=True
            )
    url = _pages_url(remote)
    print(f"[publish] {'updated' if changed else 'unchanged'} · {url or remote}")
    if url and open_page and os.environ.get("OPEN_PAGES", "1") != "0":
        subprocess.run(["open", url], check=False)
    return url or remote


def auto_publish(evidence_dir):
    if os.environ.get("AUTO_PUBLISH", "1") == "0":
        print("[publish] AUTO_PUBLISH=0; skipped")
        return None
    try:
        return subprocess.run(
            [sys.executable, str(Path(__file__).parent / "page.py"),
             "--evidence", str(evidence_dir), "--publish"],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"[warn] report publishing failed; evidence is preserved: {exc}", file=sys.stderr)
        return None


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", nargs="+", default=[str(Path(__file__).parent / "evidence")])
    parser.add_argument("--out", default="report.html")
    parser.add_argument("--publish", action="store_true", help="push report to origin/gh-pages")
    parser.add_argument("--no-open", action="store_true", help="do not open the public Pages URL")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.publish:
        publish(args.evidence, open_page=not args.no_open)
        return 0
    verdicts, captures, verdict_files = discover(args.evidence)
    output = write_report(args.out, render(verdicts, captures, verdict_files, args.evidence))
    print(f"[report] {output} · testcases={len(_tc_groups(verdicts))} verdicts={len(verdicts)} captures={len(captures)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        raise SystemExit(1)
