#!/usr/bin/env python3
"""build_platform.py — 把各 round 的報告組成一個「報告平台」單頁。

平台結構：兩個入口 AOS / iOS，各含三種投放類別
AIBID / REEN-STATIC / REEN-DYNAMIC，共 6 格。每格內嵌對應 round 的
完整報告（重用 build_artifact.build 的輸出，以 iframe 隔離，CSS/JS/id 互不衝突）。

用法：
    python build_platform.py                        # 掃預設 evidence 目錄自動組
    python page.py --out artifact-platform.html   # 只在本地產檔，不發佈
    python build_platform.py --evidence <dir> ...   # 追加要掃的 evidence 目錄

沒有 evidence 的格（例如目前 iOS）會顯示「尚無資料」佔位，
之後補進對應 round 就會自動填入。輸出為 artifact 風格的 content-only
HTML（無 <html>/<body> 外殼，與平台檔產出的單輪報告一致），可直接用瀏覽器
開啟或透過 Artifact 發佈。
"""
import argparse
import html
import json
import os
import re
import sys
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path


# ── 平台與類別定義 ──────────────────────────────────────────────
PLATFORMS = [
    ("aos", "AOS", "Android"),
    ("ios", "iOS", "iOS"),
]
MODES = [
    ("standalone", "Standalone"),
    ("mediation", "Mediation"),
]
TYPES = [
    ("aibid", "AIBID", "首購 / 新客競價"),
    ("reen-static", "REEN-STATIC", "再行銷 · 靜態素材"),
    ("reen-dynamic", "REEN-DYNAMIC", "再行銷 · 動態素材"),
]

# iOS round 由 build_artifact_ios（ios_bid_inspector 的 IOS-xx 規則）render，
# 不會誤用 Android 驗證器。若 iOS 報告層需暫時下線，把這個旗標關掉即可。
IOS_REPORTING_READY = True

DEFAULT_EVIDENCE_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence"),
    os.path.expanduser("~/Desktop/LazyAdFinder_evidence"),
]

# 資料夾名稱 → 類別（順序重要：DYNAMIC/STATIC 要先於裸 REEN 比對）
TYPE_PATTERNS = [
    ("reen-dynamic", re.compile(r"REEN[_-]?DYNAMIC", re.I)),
    ("reen-static", re.compile(r"REEN[_-]?STATIC", re.I)),
    ("aibid", re.compile(r"AIBID", re.I)),
]
PLATFORM_PATTERNS = [
    ("ios", re.compile(r"(?:^|[_-])(IOS|IPHONE|IPAD)(?:[_-]|$)", re.I)),
    ("aos", re.compile(r"(?:^|[_-])(AOS|ANDROID)(?:[_-]|$)", re.I)),
]
# 整合模式：ADMOB / APPLOVIN / MEDIATION 都歸 mediation，其餘（含裸 STANDALONE
# 或無標記的舊 round）歸 standalone。
MODE_PATTERNS = [
    ("mediation", re.compile(r"ADMOB|APPLOVIN|MEDIATION", re.I)),
    ("standalone", re.compile(r"STANDALONE", re.I)),
]


def classify_round(name):
    """從 round 資料夾名推出 (platform, mode, type)。platform 無標記時預設 aos，
    mode 無 mediation 標記時預設 standalone。回傳 (platform, mode, type) 或 None。

    只解析 `_CID_` 之前的「結構化前綴」（run_qa 命名：MODE_TYPE_CID_<cid>_<round>_<ts>）；
    絕不掃到使用者輸入的 CID / TEST_ROUND，否則 CID 含 admob/ipad、round 標籤含 ADMOB
    之類會被誤判 mode/platform/type（實測會把 standalone 判成 mediation、aos 判成 ios）。
    舊 round 無 `_CID_` 時退回用整個名稱（向後相容）。"""
    if name.startswith("SCRAPPED"):
        return None
    prefix = name.split("_CID_", 1)[0]   # 只看 MODE_TYPE 這段，不看 CID/round 尾巴
    ttype = next((t for t, rx in TYPE_PATTERNS if rx.search(prefix)), None)
    if not ttype:
        return None
    plat = next((p for p, rx in PLATFORM_PATTERNS if rx.search(prefix)), "aos")
    mode = next((m for m, rx in MODE_PATTERNS if rx.search(prefix)), "standalone")
    return plat, mode, ttype


def _has_results(round_dir):
    for entry in os.scandir(round_dir):
        if entry.is_dir() and os.path.exists(
                os.path.join(entry.path, "results.json")):
            return True
    return False


_FLOW_ORDER = [
    ("init",       "① SDK Init"),
    ("bid",        "② Bid 請求/回應"),
    ("render",     "③ 廣告渲染"),
    ("impression", "④ Impression 回報"),
    ("click",      "⑤ 點擊"),
    ("landing",    "⑥ 落地"),
]


def compute_round_progress(e2e_data):
    """依 E2E 各段狀態判定這一輪跑到哪 / 卡在哪。
    回 {'complete': bool, 'reached': '⑥ 落地', 'stall': None|'④ Impression', 'label': str}。
    段落狀態：只要該段有 TC pass/observe 視為「有進展」；全 pending/fail 視為未達。"""
    by_step = {}
    for row in e2e_data or []:
        by_step.setdefault(row.get("step", ""), []).append(row.get("status"))

    def step_reached(key):
        st = by_step.get(key, [])
        # 該段沒有任何適用 TC（全 na_*）→ 不擋流程，視為 N/A 跳過
        applicable = [s for s in st if s not in ("na_mode", "na_type", "na_platform")]
        if not applicable:
            return None  # N/A：此 mode/type 無此段
        return any(s in ("pass", "observe") for s in applicable)

    reached_label, stall_label = None, None
    for key, label in _FLOW_ORDER:
        r = step_reached(key)
        if r is None:
            continue          # N/A 段跳過
        if r:
            reached_label = label
        elif stall_label is None:
            stall_label = label   # 第一個「有適用 TC 但未達」的段 = 卡關點
    complete = stall_label is None and reached_label == _FLOW_ORDER[-1][1]
    if complete:
        label = f"完整跑完 · 最後到 {reached_label}"
    elif stall_label:
        label = f"未完整 · 卡在 {stall_label}（已完成到 {reached_label or '—'}）"
    else:
        label = f"部分完成 · 到 {reached_label or '—'}"
    return {"complete": complete, "reached": reached_label,
            "stall": stall_label, "label": label}


def _round_score(round_dir):
    """挑展示 round：完整 E2E 優先，再取最新，capture 數只作最後 tie-break。"""
    e2e_path = os.path.join(round_dir, "e2e_results.json")
    has_e2e = os.path.exists(e2e_path)
    e2e_complete = False
    if has_e2e:
        try:
            with open(e2e_path) as f:
                rows = json.load(f).get("results", [])
            e2e_complete = compute_round_progress(rows)["complete"]
        except (OSError, ValueError, TypeError):
            pass
    n_caps = sum(
        1 for e in os.scandir(round_dir)
        if e.is_dir() and os.path.exists(os.path.join(e.path, "results.json")))
    mtime = os.path.getmtime(round_dir)
    return (e2e_complete, has_e2e, mtime, n_caps)


def discover(evidence_dirs):
    """掃所有 evidence 目錄，每個 (platform, mode, type) 挑一個最佳 round。"""
    buckets = {}
    for base in evidence_dirs:
        if not os.path.isdir(base):
            continue
        try:
            entries = list(os.scandir(base))
        except PermissionError as exc:
            print(f"  skip evidence dir (no permission): {base} ({exc})", file=sys.stderr)
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            slot = classify_round(entry.name)
            if not slot:
                continue
            if slot[0] == "ios" and not IOS_REPORTING_READY:
                print(f"  skip iOS round (reporting not ready, Phase 3): {entry.name}",
                      file=sys.stderr)
                continue
            if not _has_results(entry.path):
                continue
            buckets.setdefault(slot, []).append(entry.path)
    chosen = {}
    for slot, dirs in buckets.items():
        chosen[slot] = max(dirs, key=_round_score)
    return chosen


# ── 報告產生 ───────────────────────────────────────────────────
# 本檔**不 import 任何平台模組**：平台檔（qa_aos / qa_ios）互不干擾，本檔也不介入。
# 呼叫平台檔的 CLI 取回 HTML 與 --meta JSON，所以新增平台只要在這裡加一行。
PLATFORM_RUNNER = {"aos": "qa_aos.py", "ios": "qa_ios.py"}
ROOT = os.path.dirname(os.path.abspath(__file__))


def render_report(round_dir, tmpdir, platform="aos"):
    """呼叫該平台的報告產生器，讀回 (html_str, meta)。"""
    out = os.path.join(tmpdir, "report.html")
    meta_path = os.path.join(tmpdir, "meta.json")
    runner = PLATFORM_RUNNER.get(platform, PLATFORM_RUNNER["aos"])
    subprocess.run([sys.executable, os.path.join(ROOT, runner),
                    "--report", round_dir, "--out", out, "--meta", meta_path], check=True)
    with open(meta_path) as f:
        meta = json.load(f)
    return open(out, encoding="utf-8").read(), meta


# ── 平台外殼 ────────────────────────────────────────────────────
CSS = r"""
:root{
  --bg:#eef1f4; --panel:#ffffff; --panel-2:#f6f8fa;
  --ink:#131a21; --ink-soft:#516069; --ink-faint:#7d8b94; --line:#dbe2e8;
  --accent:#0e7c86; --accent-ink:#0a5c64; --accent-soft:#e2eff1;
  --aos:#2e9e5b; --aos-soft:#e4f4ea; --ios:#3a6ea5; --ios-soft:#e6eef7;
  --pass:#2f7d3a; --fail:#c0392b; --pend:#5b6b78; --block:#b5761a;
  --shadow:0 1px 2px rgba(19,26,33,.06),0 8px 24px rgba(19,26,33,.06);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans TC",sans-serif;
  --radius:14px; --topbar:clamp(40px,6vh,50px);
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0d1216; --panel:#151d23; --panel-2:#111820;
  --ink:#e7edf1; --ink-soft:#a6b6c1; --ink-faint:#71828d; --line:#243039;
  --accent:#38bdc9; --accent-ink:#7ad6df; --accent-soft:#123037;
  --aos:#4cc57d; --aos-soft:#12281b; --ios:#6ba6dd; --ios-soft:#132132;
  --pass:#5cc46a; --fail:#f0766a; --pend:#9fb0bc; --block:#e0a94a;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
}}
:root[data-theme="light"]{
  --bg:#eef1f4; --panel:#ffffff; --panel-2:#f6f8fa;
  --ink:#131a21; --ink-soft:#516069; --ink-faint:#7d8b94; --line:#dbe2e8;
  --accent:#0e7c86; --accent-ink:#0a5c64; --accent-soft:#e2eff1;
  --aos:#2e9e5b; --aos-soft:#e4f4ea; --ios:#3a6ea5; --ios-soft:#e6eef7;
  --pass:#2f7d3a; --fail:#c0392b; --pend:#5b6b78; --block:#b5761a;
  --shadow:0 1px 2px rgba(19,26,33,.06),0 8px 24px rgba(19,26,33,.06);
}
:root[data-theme="dark"]{
  --bg:#0d1216; --panel:#151d23; --panel-2:#111820;
  --ink:#e7edf1; --ink-soft:#a6b6c1; --ink-faint:#71828d; --line:#243039;
  --accent:#38bdc9; --accent-ink:#7ad6df; --accent-soft:#123037;
  --aos:#4cc57d; --aos-soft:#12281b; --ios:#6ba6dd; --ios-soft:#132132;
  --pass:#5cc46a; --fail:#f0766a; --pend:#9fb0bc; --block:#e0a94a;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{
  background:var(--bg); color:var(--ink); font-family:var(--sans);
  font-size:15px; line-height:1.5; -webkit-font-smoothing:antialiased;
  display:flex; flex-direction:column; min-height:100vh;
}
.mono{font-family:var(--mono)}
.tnum{font-variant-numeric:tabular-nums}

/* ── top bar ── */
.top{
  position:sticky; top:0; z-index:30; height:var(--topbar);
  display:flex; align-items:center; gap:10px; padding:0 16px;
  background:color-mix(in srgb,var(--panel) 88%,transparent);
  backdrop-filter:saturate(1.4) blur(10px);
  border-bottom:1px solid var(--line);
}
.brand{display:flex; align-items:center; gap:9px; flex:none}
.sig{
  width:23px; height:23px; border-radius:6px; flex:none;
  background:linear-gradient(135deg,var(--accent),var(--accent-ink));
  position:relative;
}
.sig::after{
  content:""; position:absolute; inset:6px; border-radius:2px;
  border:2px solid rgba(255,255,255,.9);
  border-right-color:transparent; border-bottom-color:transparent;
  transform:rotate(45deg);
}
.brand-t{font-weight:700; font-size:13px; letter-spacing:-.01em; line-height:1.1}
.brand-s{font-size:10px; color:var(--ink-faint); letter-spacing:.07em; text-transform:uppercase}
.theme{
  flex:none; margin-left:auto; width:28px; height:28px; border-radius:8px; cursor:pointer;
  border:1px solid var(--line); background:var(--panel-2); color:var(--ink);
  font-size:14px; display:grid; place-items:center;
}
.theme:hover{border-color:var(--accent)}

/* ── nav (platform tab + mode tab, single row) ── */
.nav{
  position:sticky; top:var(--topbar); z-index:20;
  display:flex; align-items:center; gap:10px; padding:6px 16px; flex-wrap:wrap;
  background:var(--panel-2); border-bottom:1px solid var(--line);
}
.nav-div{width:1px; height:20px; background:var(--line); flex:none}
.seg{display:inline-flex; background:var(--bg); border:1px solid var(--line);
  border-radius:9px; padding:2px; gap:2px}
.seg button{
  appearance:none; border:0; background:transparent; cursor:pointer;
  font:inherit; font-weight:600; font-size:12.5px; color:var(--ink-soft);
  padding:5px 14px; border-radius:7px; display:flex; align-items:center; gap:6px;
}
.seg button .dot{width:8px; height:8px; border-radius:50%; background:currentColor; opacity:.55}
.seg button[data-plat="aos"]{--pc:var(--aos)}
.seg button[data-plat="ios"]{--pc:var(--ios)}
.seg button.on{background:var(--panel); color:var(--ink); box-shadow:var(--shadow)}
.seg button.on .dot{background:var(--pc); opacity:1}
.seg.mode button.on{color:var(--accent-ink)}

/* ── main ── */
main{flex:1; min-height:0; display:flex; flex-direction:column}
.overview{padding:clamp(14px,2.4vw,26px) clamp(14px,2.2vw,22px) 40px;
  max-width:1180px; margin:0 auto; width:100%}
.ov-lead{margin:0 0 clamp(14px,1.8vw,22px); color:var(--ink-soft); max-width:70ch;
  font-size:clamp(13px,1vw,15px)}
.ov-lead b{color:var(--ink)}
.combo-block{margin-bottom:30px}
.combo-block[hidden]{display:none}
.plat-head{display:flex; align-items:center; gap:11px; margin:0 0 13px; flex-wrap:wrap}
.plat-badge{
  font-family:var(--mono); font-weight:700; font-size:13px; letter-spacing:.02em;
  padding:4px 11px; border-radius:8px;
}
.plat-badge[data-plat="aos"]{background:var(--aos-soft); color:var(--aos)}
.plat-badge[data-plat="ios"]{background:var(--ios-soft); color:var(--ios)}
.mode-badge{
  font-weight:700; font-size:12px; letter-spacing:.02em;
  padding:4px 10px; border-radius:8px; background:var(--accent-soft); color:var(--accent-ink);
}
.plat-head h2{margin:0; font-size:16px; letter-spacing:-.01em}
.plat-head .sub{color:var(--ink-faint); font-size:13px}
.grid{display:grid; gap:clamp(10px,1.2vw,15px);
  grid-template-columns:repeat(auto-fill,minmax(min(100%,clamp(228px,24vw,300px)),1fr))}
.card{
  text-align:left; font:inherit; color:inherit; cursor:pointer;
  background:var(--panel); border:1px solid var(--line); border-radius:var(--radius);
  padding:clamp(12px,1.4vw,17px); display:flex; flex-direction:column; gap:clamp(9px,1vw,13px);
  box-shadow:var(--shadow); transition:transform .12s ease,border-color .12s ease;
  position:relative; overflow:hidden;
}
.card::before{content:""; position:absolute; left:0; top:0; bottom:0; width:4px; background:var(--accent)}
.card[data-plat="aos"]::before{background:var(--aos)}
.card[data-plat="ios"]::before{background:var(--ios)}
.card.live:hover{transform:translateY(-2px); border-color:var(--accent)}
.card.empty{cursor:not-allowed; box-shadow:none; background:var(--panel-2)}
.card.empty::before{background:var(--line)}
.card-top{display:flex; align-items:flex-start; justify-content:space-between; gap:10px}
.card-cat{font-family:var(--mono); font-weight:700; font-size:clamp(13px,1.1vw,15px); letter-spacing:.01em}
.card-desc{color:var(--ink-faint); font-size:12.5px; margin-top:2px}
.card-state{font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.06em;
  padding:3px 9px; border-radius:20px; white-space:nowrap; flex:none}
.card-state.ready{background:var(--accent-soft); color:var(--accent-ink)}
.card-state.none{background:var(--panel); color:var(--ink-faint); border:1px solid var(--line)}
.card-meta{display:flex; flex-wrap:wrap; gap:6px 14px; font-size:12px; color:var(--ink-soft)}
.card-meta .k{color:var(--ink-faint)}
.bars{display:flex; flex-direction:column; gap:7px}
.bar-row{display:flex; align-items:center; gap:9px; font-size:12px}
.bar-row .lab{width:44px; color:var(--ink-faint); flex:none}
.bar{flex:1; height:7px; border-radius:4px; background:var(--panel-2); overflow:hidden; display:flex}
.bar i{height:100%}
.bar i.p{background:var(--pass)} .bar i.f{background:var(--fail)}
.bar i.b{background:var(--block)}
.bar-row .cnt{width:78px; text-align:right; color:var(--ink-soft); flex:none}
.card-empty-note{color:var(--ink-faint); font-size:13px; line-height:1.55}
.card-open{margin-top:auto; font-size:12.5px; font-weight:600; color:var(--accent);
  display:flex; align-items:center; gap:5px}
.card.empty .card-open{color:var(--ink-faint); font-weight:500}

/* ── report view ── */
.report{flex:1; min-height:0; display:none; flex-direction:column}
.report.show{display:flex}
.report-bar{
  display:flex; align-items:center; gap:10px; padding:5px 16px;
  background:var(--panel-2); border-bottom:1px solid var(--line); flex-wrap:wrap;
}
.back{
  appearance:none; font:inherit; cursor:pointer; font-size:12.5px; font-weight:600;
  padding:5px 12px; border-radius:7px; border:1px solid var(--line);
  background:var(--panel); color:var(--ink); display:inline-flex; align-items:center; gap:5px;
}
.back:hover{border-color:var(--accent); color:var(--accent)}
.report-title{font-weight:700; font-size:12.5px; display:flex; align-items:center; gap:8px}
.report-title .chip{font-family:var(--mono); font-size:11px; font-weight:700;
  padding:2px 8px; border-radius:6px}
.report-title .chip[data-plat="aos"]{background:var(--aos-soft); color:var(--aos)}
.report-title .chip[data-plat="ios"]{background:var(--ios-soft); color:var(--ios)}
.report-frame{flex:1; min-height:0; width:100%; border:0; background:var(--bg)}
.overview[hidden]{display:none}

@media (max-width:640px){
  .brand-box{display:none}
  .nav{gap:10px}
  .overview{padding:18px 14px 32px}
}
"""


def _bars(counts, total):
    """把計數轉成 100% 堆疊條的 inline width，避免除以零。"""
    if total <= 0:
        return {"p": 0, "f": 0, "b": 0}
    def pct(x):
        return round(x / total * 100, 2)
    return {"p": pct(counts.get("PASS", 0)), "f": pct(counts.get("FAIL", 0)),
            "b": pct(counts.get("BLOCKED", 0))}


def render_card(plat_id, plat_lbl, mode_id, mode_lbl, type_id, type_lbl, type_desc, meta):
    slot = f"{plat_id}:{mode_id}:{type_id}"
    title = f"{plat_lbl} · {mode_lbl} · {type_lbl}"
    if not meta:
        # 沒有對應 round＝本輪根本沒跑這個組合 → 標「未執行 / No run」，
        # 不可與「有跑但某些 TC 本輪不適用（Blocked）」混淆（不顯示 0/0/84 blocked）。
        return (
            f'<div class="card empty" data-plat="{plat_id}" data-slot="{slot}">'
            f'<div class="card-top"><div>'
            f'<div class="card-cat">{html.escape(type_lbl)}</div>'
            f'<div class="card-desc">{html.escape(type_desc)}</div></div>'
            f'<span class="card-state none">未執行 / No run</span></div>'
            f'<div class="card-empty-note">本輪未執行此組合（無對應 round evidence）；'
            f'跑完 {html.escape(title)} 後會自動出現在這裡。</div>'
            f'<div class="card-open">未執行 / No run</div></div>'
        )
    sig_total = meta["signal_total"]
    sc = meta["signal_counts"]
    sig = _bars(sc, sig_total)
    e2e_total = meta["e2e_total"]
    es = meta["e2e_score"]
    e2e = _bars({"PASS": es.get("PASS", 0), "FAIL": es.get("FAILED", 0),
                 "BLOCKED": es.get("BLOCKED", 0)}, e2e_total)
    sig_cnt = f'{sc.get("PASS",0)}✓ {sc.get("FAIL",0)}✗'
    e2e_cnt = f'{es.get("PASS",0)}✓ {es.get("FAILED",0)}✗'
    return (
        f'<button class="card live" data-plat="{plat_id}" data-slot="{slot}" '
        f'data-title="{html.escape(title)}">'
        f'<div class="card-top"><div>'
        f'<div class="card-cat">{html.escape(type_lbl)}</div>'
        f'<div class="card-desc">{html.escape(type_desc)}</div></div>'
        f'<span class="card-state ready">已就緒</span></div>'
        f'<div class="card-meta">'
        f'<span><span class="k">Round</span> <span class="mono">{html.escape(meta["round_name"][:26])}</span></span>'
        f'<span><span class="k">整合</span> {html.escape(meta["test_mode"] or "—")}</span>'
        f'<span><span class="k">裝置</span> {html.escape(meta["model"])}</span>'
        f'</div>'
        f'<div class="bars">'
        f'<div class="bar-row"><span class="lab">Signal</span>'
        f'<span class="bar"><i class="p" style="width:{sig["p"]}%"></i>'
        f'<i class="f" style="width:{sig["f"]}%"></i>'
        f'<i class="b" style="width:{sig["b"]}%"></i></span>'
        f'<span class="cnt tnum">{sig_cnt} / {sig_total}</span></div>'
        f'<div class="bar-row"><span class="lab">E2E</span>'
        f'<span class="bar"><i class="p" style="width:{e2e["p"]}%"></i>'
        f'<i class="f" style="width:{e2e["f"]}%"></i>'
        f'<i class="b" style="width:{e2e["b"]}%"></i></span>'
        f'<span class="cnt tnum">{e2e_cnt} / {e2e_total}</span></div>'
        f'</div>'
        f'<div class="card-open">開啟報告 →</div></button>'
    )


def render_platform(out_path, evidence_dirs):
    chosen = discover(evidence_dirs)
    reports = {}   # slot "plat:mode:type" -> (html, meta)
    with tempfile.TemporaryDirectory() as tmp:
        for i, ((plat, mode, ttype), round_dir) in enumerate(sorted(chosen.items())):
            slot = f"{plat}:{mode}:{ttype}"
            sub = os.path.join(tmp, f"r{i}")
            os.makedirs(sub, exist_ok=True)
            try:
                doc, meta = render_report(round_dir, sub, platform=plat)
                reports[slot] = (doc, meta)
            except SystemExit as e:
                print(f"  skip {slot} ({round_dir}): {e}", file=sys.stderr)
            except Exception as e:
                print(f"  skip {slot} ({round_dir}): {e}", file=sys.stderr)

    # 導覽第一層：平台分段（AOS / iOS）
    plat_seg = "".join(
        f'<button data-plat="{pid}"{" class=on" if idx==0 else ""}>'
        f'<span class="dot"></span>{html.escape(plbl)}</button>'
        for idx, (pid, plbl, _dev) in enumerate(PLATFORMS))

    # 導覽第二層：整合模式分段（Standalone / Mediation）
    mode_seg = "".join(
        f'<button data-mode="{mid}"{" class=on" if idx==0 else ""}>{html.escape(mlbl)}</button>'
        for idx, (mid, mlbl) in enumerate(MODES))

    # 總覽：platform × mode 共 4 個 combo，各一組三張分類卡；tab 切換只顯示一個 combo
    blocks = []
    for pi, (pid, plbl, dev) in enumerate(PLATFORMS):
        for mi, (mid, mlbl) in enumerate(MODES):
            cards = "".join(
                render_card(pid, plbl, mid, mlbl, tid, tlbl, tdesc,
                            reports.get(f"{pid}:{mid}:{tid}", (None, None))[1])
                for tid, tlbl, tdesc in TYPES)
            n_live = sum(1 for tid, _l, _d in TYPES if f"{pid}:{mid}:{tid}" in reports)
            shown = (pi == 0 and mi == 0)
            blocks.append(
                f'<section class="combo-block" data-plat="{pid}" data-mode="{mid}"'
                f'{"" if shown else " hidden"}>'
                f'<div class="plat-head">'
                f'<span class="plat-badge" data-plat="{pid}">{html.escape(plbl)}</span>'
                f'<span class="mode-badge">{html.escape(mlbl)}</span>'
                f'<h2>{html.escape(dev)}</h2>'
                f'<span class="sub">{n_live} / {len(TYPES)} 類別已就緒</span></div>'
                f'<div class="grid">{cards}</div></section>')
    blocks_html = "\n".join(blocks)

    # 隱藏容器：每個 slot 的報告 HTML 存在 data-doc（escape 過），點開才灌進 iframe
    docs = "".join(
        f'<div class="report-src" data-slot="{slot}" data-doc="{html.escape(doc, quote=True)}"></div>'
        for slot, (doc, _m) in reports.items())

    n_reports = len(reports)
    return TEMPLATE.format(
        css=CSS, plat_seg=plat_seg, mode_seg=mode_seg, blocks=blocks_html, docs=docs,
        n_reports=n_reports, n_slots=len(PLATFORMS) * len(MODES) * len(TYPES)), out_path


TEMPLATE = """<title>LazyAdFinder · SDK 測試報告平台</title>
<style>{css}</style>
<header class="top">
  <div class="brand">
    <div class="sig" aria-hidden="true"></div>
    <div class="brand-box">
      <div class="brand-t">SDK 測試報告平台</div>
      <div class="brand-s">Appier · LazyAdFinder</div>
    </div>
  </div>
  <button class="theme" id="theme" aria-label="切換深淺色">◐</button>
</header>
<div class="nav">
  <div class="seg plat" id="platseg" role="tablist" aria-label="平台">{plat_seg}</div>
  <span class="nav-div" aria-hidden="true"></span>
  <div class="seg mode" id="modeseg" role="tablist" aria-label="整合模式">{mode_seg}</div>
</div>
<main>
  <div class="overview" id="overview">
    <p class="ov-lead">先用上方 <b>平台</b>與<b>整合模式</b> tab 切換，再點下方<b>分類卡片</b>
    檢視該 round 的完整報告。每張卡的橫條為 Pass／Fail 佔比概覽。</p>
    {blocks}
  </div>
  <div class="report" id="report">
    <div class="report-bar">
      <button class="back" id="back">← 總覽</button>
      <div class="report-title" id="rtitle"></div>
    </div>
    <iframe class="report-frame" id="frame" title="測試報告"
            sandbox="allow-scripts allow-same-origin allow-popups"></iframe>
  </div>
</main>
<div id="docs" hidden>{docs}</div>
<script>
(function(){{
  var root=document.documentElement;
  // 主題：跟報告一致，記在 localStorage
  var tkey="ladf-platform-theme";
  try{{var saved=localStorage.getItem(tkey); if(saved)root.setAttribute("data-theme",saved);}}catch(e){{}}
  function curTheme(){{
    var t=root.getAttribute("data-theme");
    return t||(matchMedia("(prefers-color-scheme:dark)").matches?"dark":"light");
  }}
  // 把當前主題灌進報告 iframe（報告是獨立文件，CSS 吃 :root[data-theme]）
  function pushTheme(){{
    try{{
      var fr=document.getElementById("frame");
      if(fr&&fr.contentDocument&&fr.contentDocument.documentElement)
        fr.contentDocument.documentElement.setAttribute("data-theme",curTheme());
    }}catch(e){{}}
  }}
  document.getElementById("theme").addEventListener("click",function(){{
    root.setAttribute("data-theme",curTheme()==="dark"?"light":"dark");
    try{{localStorage.setItem(tkey,curTheme());}}catch(e){{}}
    pushTheme();
  }});

  var overview=document.getElementById("overview");
  var report=document.getElementById("report");
  var frame=document.getElementById("frame");
  var rtitle=document.getElementById("rtitle");
  var docsBox=document.getElementById("docs");
  var nav=document.querySelector(".nav");
  var loaded={{}};   // slot -> true 一次載入後不重灌
  var curPlat=document.querySelector("#platseg button").getAttribute("data-plat");
  var curMode=document.querySelector("#modeseg button").getAttribute("data-mode");

  function docFor(slot){{
    var el=docsBox.querySelector('.report-src[data-slot="'+slot+'"]');
    return el?el.getAttribute("data-doc"):null;
  }}
  function labelOf(slot){{
    var card=document.querySelector('.card[data-slot="'+slot+'"]');
    return card?(card.getAttribute("data-title")||""):slot.toUpperCase();
  }}

  // 依 curPlat + curMode 只顯示對應的 combo-block
  function showActiveCombo(){{
    document.querySelectorAll(".combo-block").forEach(function(s){{
      s.hidden=!(s.getAttribute("data-plat")===curPlat &&
                 s.getAttribute("data-mode")===curMode);
    }});
  }}
  function setPlat(pid){{
    curPlat=pid;
    document.querySelectorAll("#platseg button").forEach(function(b){{
      b.classList.toggle("on",b.getAttribute("data-plat")===pid);
    }});
    showActiveCombo();
  }}
  function setMode(mid){{
    curMode=mid;
    document.querySelectorAll("#modeseg button").forEach(function(b){{
      b.classList.toggle("on",b.getAttribute("data-mode")===mid);
    }});
    showActiveCombo();
  }}

  function openSlot(slot){{
    var doc=docFor(slot);
    if(doc===null)return;               // 無資料格：不開
    var seg=slot.split(":");            // [plat, mode, type]
    var parts=labelOf(slot).split(" · ");  // [PlatLbl, ModeLbl, TypeLbl]
    rtitle.innerHTML='<span class="chip" data-plat="'+seg[0]+'">'+(parts[0]||"")+
      '</span>'+(parts[1]||"")+' · '+(parts[2]||"");
    frame.onload=pushTheme;             // iframe 載入後同步當前主題
    frame.srcdoc=doc; loaded[slot]=true;
    // 對齊 tab 到這個報告所屬的平台/模式（回到總覽時才需要，先設好）
    setPlat(seg[0]); setMode(seg[1]);
    overview.hidden=true; report.classList.add("show");
    nav.hidden=true;                    // 看報告時收起平台/模式 tab，讓出高度
    window.scrollTo(0,0);
  }}
  function showOverview(){{
    report.classList.remove("show"); overview.hidden=false;
    nav.hidden=false;                   // 回總覽時把 tab 顯示回來
    window.scrollTo(0,0);
  }}

  document.querySelectorAll("#platseg button").forEach(function(b){{
    b.addEventListener("click",function(){{ setPlat(b.getAttribute("data-plat")); showOverview(); }});
  }});
  document.querySelectorAll("#modeseg button").forEach(function(b){{
    b.addEventListener("click",function(){{ setMode(b.getAttribute("data-mode")); showOverview(); }});
  }});
  document.querySelectorAll(".card.live").forEach(function(c){{
    c.addEventListener("click",function(){{ openSlot(c.getAttribute("data-slot")); }});
  }});
  document.getElementById("back").addEventListener("click",showOverview);
  document.addEventListener("keydown",function(e){{
    if(e.key==="Escape"&&report.classList.contains("show"))showOverview();
  }});

  showActiveCombo();
}})();
</script>
"""


# ── 發佈到 GitHub Pages ────────────────────────────────────────
PAGES_URL = "https://autumn-kao-appier.github.io/LazyAdFinder/"

# sanity gate（與 deploy_pages.sh 同一組門檻）：壞的 discovery 產出的退化頁面
# 不得覆蓋線上好頁面。兩條發佈路徑必須一樣嚴，否則自動發佈會繞過人工部署的防護。
MIN_INDEX_BYTES = 51200
REQUIRED_MARKER = "已就緒"      # 至少要有一張 live report 卡片


class PublishAborted(RuntimeError):
    """產出的平台頁面沒通過 sanity gate，未推上 gh-pages。"""


def _run(args, cwd=ROOT, **kwargs):
    return subprocess.run(args, cwd=cwd, check=True, text=True, **kwargs)


def _check_index(index):
    """退化頁面（太小／沒有任何 live report）不得部署。"""
    size = index.stat().st_size if index.exists() else 0
    if size < MIN_INDEX_BYTES:
        raise PublishAborted(
            f"產出的 index 只有 {size} bytes（< {MIN_INDEX_BYTES}），疑似退化，未部署")
    if REQUIRED_MARKER.encode("utf-8") not in index.read_bytes():
        raise PublishAborted(
            f"產出的平台沒有任何『{REQUIRED_MARKER}』卡片（0 個 live report），未部署")


def _render_to(index_path, evidence_dirs=None):
    """重產平台頁並只寫出 index_path（standalone）。

    刻意不在 repo 留下 artifact-platform.html：那是舊 claude.ai artifact 流程的產物，
    現在唯一的交付是線上 GitHub Pages。要本地檔請明確用 `page.py --out x.html`。
    """
    with tempfile.TemporaryDirectory(prefix="lazyadfinder-render-") as tmp:
        html_out, _ = render_platform(os.path.join(tmp, "platform.html"),
                                     evidence_dirs or DEFAULT_EVIDENCE_DIRS)
    Path(index_path).write_text(wrap_standalone(html_out), encoding="utf-8")


def publish():
    """重產平台頁、通過 sanity gate 後 commit 並推上 gh-pages。"""
    remote = subprocess.check_output(
        ["git", "remote", "get-url", "origin"], cwd=ROOT, text=True
    ).strip()
    with tempfile.TemporaryDirectory(prefix="lazyadfinder-pages-") as tmp:
        pages = Path(tmp) / "pages"
        _run(["git", "clone", "--depth", "1", "--branch", "gh-pages", remote, str(pages)])
        _render_to(str(pages / "index.html"))
        _check_index(pages / "index.html")
        _run(["git", "add", "index.html"], cwd=pages)
        changed = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=pages
        ).returncode != 0
        if changed:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _run(["git", "commit", "-m", f"publish: QA report {stamp}"], cwd=pages)
            _run(["git", "push", "origin", "HEAD:gh-pages"], cwd=pages)
            print(f"[publish] updated → {PAGES_URL}")
        else:
            print(f"[publish] no report changes → {PAGES_URL}")
    # 直接開線上頁面（唯一交付就是它，不再產本地 preview）。
    # 注意：GitHub Pages 重建約需 1–2 分鐘，剛推完立刻開可能還看到上一版，重整即可。
    if os.environ.get("OPEN_PAGES", "1") != "0":
        subprocess.run(["open", PAGES_URL], check=False)
        print("[publish] 已開啟瀏覽器（Pages 重建約 1–2 分鐘，若還是舊版請重整）")
    return PAGES_URL


def auto_publish():
    """Publish unless explicitly disabled; never discard a completed capture on failure."""
    if os.environ.get("AUTO_PUBLISH", "1") == "0":
        print("[publish] AUTO_PUBLISH=0，略過 GitHub Pages")
        return None
    try:
        return publish()
    except (OSError, subprocess.CalledProcessError, PublishAborted) as exc:
        print(f"[warn] GitHub Pages 發佈失敗（evidence 已保存）：{exc}")
        return None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="artifact-platform.html",
                    help="輸出 HTML 路徑（預設 artifact-platform.html，content-only 供 Artifact 發佈）")
    ap.add_argument("--evidence", nargs="*", default=None,
                    help="要掃的 evidence 目錄（可多個）；不給則用預設目錄")
    ap.add_argument("--standalone",
                    help="另外輸出完整 <!doctype html> 文件到此路徑（供 GitHub Pages 等直接開啟）")
    ap.add_argument("--publish", action="store_true",
                    help="重產後推上 gh-pages（含 sanity gate：退化頁面不覆蓋線上）")
    args = ap.parse_args()
    if args.publish:
        return publish()
    evidence_dirs = args.evidence if args.evidence else DEFAULT_EVIDENCE_DIRS
    html_out, out_path = render_platform(args.out, evidence_dirs)
    Path(out_path).write_text(html_out, encoding="utf-8")
    print(f"→ {out_path}  ({os.path.getsize(out_path)/1e6:.1f} MB, content-only)")
    if args.standalone:
        Path(args.standalone).write_text(wrap_standalone(html_out), encoding="utf-8")
        print(f"→ {args.standalone}  ({os.path.getsize(args.standalone)/1e6:.1f} MB, standalone)")


def wrap_standalone(content):
    """把 content-only 平台 HTML 包成可直接開啟的完整文件（<title>/<style> 收進 <head>）。"""
    marker = "</style>"
    cut = content.find(marker)
    if cut != -1:
        cut += len(marker)
        head_inner, body_inner = content[:cut], content[cut:]
    else:
        head_inner, body_inner = "", content
    favicon = ("data:image/svg+xml,"
               "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
               "<text y='.9em' font-size='90'>%F0%9F%8E%AF</text></svg>")
    return (
        "<!doctype html>\n<html lang=\"zh-Hant\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<link rel=\"icon\" href=\"{favicon}\">\n"
        f"{head_inner}\n</head>\n<body>\n{body_inner}\n</body>\n</html>\n"
    )


if __name__ == "__main__":
    main()
