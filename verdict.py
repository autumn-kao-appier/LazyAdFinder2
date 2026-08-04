#!/usr/bin/env python3
"""verdict.py — 判定與報告的共用契約（平台無關）。

架構邊界：**按平台垂直分離 runner，但不複製共同語義。**

  qa_aos.py / qa_ios.py   平台專屬：佈狀態、capture、證據落地、**該平台的 TC 目錄**
                          （欄位路徑與期望值、限制表、批次歸屬）
  verdict.py（本檔）      平台無關：check 實作、判定狀態機、round 彙總、報告版面
  apr_xorenc.py           SDK 的 ae1 加解密

為什麼要有這條線：check 實作（regex / int_range / absent / array_timestamp…）與報告
版面是**共同語義**，兩平台若各留一份，改一邊忘另一邊就會產生「同一個值在 AOS 判 PASS、
在 iOS 判 FAIL」這種靜默不一致。2026-08 的 req_enc 事故就是同一個類別：解密被複製成
6 份，SDK 一改只有其中幾份壞掉。

平台資料一律**用參數傳進來**，不用全域註冊——契約不得偷讀平台的全域變數，否則平台忘了
設定時會靜默拿到空表、把所有 TC 判成 PASS/FAIL。
"""

import base64
import copy
import html
import io
import json
import os
import re
import sys
import time
from datetime import datetime

try:
    from PIL import Image
except Exception:
    Image = None


UUID_RE      = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


BCP47_RE     = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


INPUT_LANG_RE = re.compile(r"^[A-Za-z]{2,3}([_-][A-Za-z0-9]{2,8})*$")


ISO639_RE    = re.compile(r"^[a-z]{2}$")


CELL_4G5G_RE = re.compile(r"^cellular_[45]g$")


IPV4_RE      = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


SEMVER_RE    = re.compile(r"^\d+\.\d+\.\d+$")


ANDROID_OS_RE = re.compile(r"^android$", re.IGNORECASE)


ZERO_UUID    = "00000000-0000-0000-0000-000000000000"


ABSENT_CHECKS = {"absent", "absent_or_empty", "falsy", "value_or_absent"}


PARTIAL_CHECKS = {"range", "positive_int", "positive_float", "nonempty",
                  "nonempty_notunknown", "array_nonempty", "array_number",
                  "array_regex", "array_impression", "leq_field",
                  "timestamp_recent", "truthy", "ipv4_nonzero", "regex"}


CATEGORIES = {
    "A": "Core Identifiers",
    "B": "Device State — Bool",
    "C": "Device State — Numeric",
    "D": "Device / App — Format",
    "E": "Device State — Arrays",
    "F": "Geolocation",
    "G": "In-Session",
    "H": "Memory / Disk",
    "I": "Screen / Display",
    "J": "Negative / Absent",
    "K": "Network Latency",
    "L": "Privacy Compliance",
    "M": "SKAdNetwork",
    "N": "Request Envelope",
}


FIELD_SCHEMA = {}


STATUS_META = {
    "PASS":    ("pass", "Pass"),
    "FAIL":    ("fail", "Fail"),
    "BLOCKED": ("blocked", "Blocked"),
}


CSS = """
:root{
  --bg:#f4f6f8; --panel:#ffffff; --ink:#131a21; --ink-soft:#4a5761; --line:#dde3e9;
  --accent:#0e7c86; --accent-soft:#e3f0f1;
  --pass:#2f7d3a; --pass-bg:#e6f2e8; --fail:#c0392b; --fail-bg:#fbe9e7;
  --pend:#5b6b78; --pend-bg:#eceff2; --block:#b5761a; --block-bg:#fbf0dd;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans TC",sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0f1519; --panel:#161e24; --ink:#e7edf1; --ink-soft:#9fb0bc; --line:#26313a;
  --accent:#38bdc9; --accent-soft:#123037;
  --pass:#5cc46a; --pass-bg:#16281a; --fail:#f0766a; --fail-bg:#2c1613;
  --pend:#9fb0bc; --pend-bg:#1c252c; --block:#e0a94a; --block-bg:#2a2011;
}}
:root[data-theme="dark"]{
  --bg:#0f1519; --panel:#161e24; --ink:#e7edf1; --ink-soft:#9fb0bc; --line:#26313a;
  --accent:#38bdc9; --accent-soft:#123037;
  --pass:#5cc46a; --pass-bg:#16281a; --fail:#f0766a; --fail-bg:#2c1613;
  --pend:#9fb0bc; --pend-bg:#1c252c; --block:#e0a94a; --block-bg:#2a2011;
}
:root[data-theme="light"]{
  --bg:#f4f6f8; --panel:#ffffff; --ink:#131a21; --ink-soft:#4a5761; --line:#dde3e9;
  --accent:#0e7c86; --accent-soft:#e3f0f1;
  --pass:#2f7d3a; --pass-bg:#e6f2e8; --fail:#c0392b; --fail-bg:#fbe9e7;
  --pend:#5b6b78; --pend-bg:#eceff2; --block:#b5761a; --block-bg:#fbf0dd;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  line-height:1.5;-webkit-font-smoothing:antialiased}
.top{position:relative;z-index:20;background:color-mix(in srgb,var(--panel) 92%,transparent);
  backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.top-in{max-width:1180px;margin:0 auto;padding:18px 24px 12px;display:grid;gap:14px}
.brand{display:flex;gap:14px;align-items:center}
.sig{width:40px;height:40px;border-radius:9px;flex:none;
  background:
    linear-gradient(var(--accent),var(--accent)) 0 50%/100% 2px no-repeat,
    radial-gradient(circle at 18% 50%,var(--accent) 3px,transparent 3.5px),
    radial-gradient(circle at 50% 22%,var(--accent) 3px,transparent 3.5px),
    radial-gradient(circle at 82% 68%,var(--accent) 3px,transparent 3.5px);
  border:1px solid var(--line);background-color:var(--accent-soft)}
.kicker{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-size:21px;margin:2px 0 0;letter-spacing:-.01em}
.meta{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
  gap:0;margin:0;width:100%;border-top:1px solid var(--line);padding-top:10px}
.meta div{display:flex;flex-direction:column;min-width:0;padding:0 12px;border-left:1px solid var(--line)}
.meta div{padding-block:4px}.meta div:nth-child(4n+1){padding-left:0;border-left:0}
.meta dt{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-soft)}
.meta dd{margin:0;font-family:var(--mono);font-size:12px;font-variant-numeric:tabular-nums;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.progress-banner{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:10px;
  padding:8px 14px;border-radius:8px;font-size:13px;border:1px solid var(--line)}
.progress-banner .pg-icon{font-size:15px}
.progress-banner .pg-label{font-weight:600}
.progress-banner .pg-stall{font-family:var(--mono);font-size:12px;padding:2px 8px;border-radius:5px;
  background:rgba(0,0,0,.06)}
.progress-banner.ok{background:rgba(34,160,90,.12);border-color:rgba(34,160,90,.4);color:#137a43}
.progress-banner.stall{background:rgba(214,138,20,.14);border-color:rgba(214,138,20,.45);color:#a5670a}
.progress-banner.warn{background:rgba(120,120,120,.12);border-color:var(--line);color:var(--ink-soft)}
/* ── Signal / E2E 分頁 ── */
.tabbar{max-width:1180px;margin:0 auto;padding:4px 24px 0;display:flex;gap:6px}
.tabbtn{cursor:pointer;border:1px solid var(--line);border-bottom:none;background:transparent;
  color:var(--ink-soft);font-family:var(--sans);font-size:14px;font-weight:700;
  padding:9px 18px;border-radius:9px 9px 0 0;display:flex;align-items:center;gap:8px}
.tabbtn:hover{color:var(--ink)}
.tabbtn.is-on{background:var(--panel);color:var(--accent);border-color:var(--accent);box-shadow:0 -2px 0 var(--accent) inset}
.tabbtn-n{font:700 12px var(--mono);background:var(--accent-soft);color:var(--accent);
  padding:1px 8px;border-radius:999px}
.tab-pane[hidden]{display:none}
/* E2E scorecard */
.e2e-scorecard{max-width:1180px;margin:0 auto;padding:4px 0 16px;display:flex;gap:10px;flex-wrap:wrap}
.e2e-tile{border:1px solid var(--line);background:var(--panel);border-radius:10px;
  padding:9px 16px;display:flex;flex-direction:column;min-width:88px}
.e2e-tile-n{font:700 21px var(--mono);font-variant-numeric:tabular-nums}
.e2e-tile-l{font-size:11px;color:var(--ink-soft);letter-spacing:.03em}
.e2e-t-pass .e2e-tile-n{color:var(--pass)} .e2e-t-fail .e2e-tile-n{color:var(--fail)}
.e2e-t-blocked .e2e-tile-n{color:var(--block)}
.e2e-lead{margin-top:0}
/* E2E 流程時間軸 */
.e2e-timeline{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
.e2e-step{border:1px solid var(--line);border-radius:12px;background:var(--panel);overflow:hidden}
.e2e-step-head{padding:11px 16px;background:var(--accent-soft);display:flex;align-items:baseline;gap:12px;
  border-bottom:1px solid var(--line)}
.e2e-step-head h3{margin:0;font-size:15px;color:var(--accent)}
.e2e-step-desc{font-size:12px;color:var(--ink-soft)}
.e2e-step-rows{display:flex;flex-direction:column}
.e2e-row{display:flex;gap:14px;padding:14px 16px;border-top:1px solid var(--line)}
.e2e-row:first-child{border-top:none}
.e2e-row-shot{flex:0 0 132px;width:132px}
.e2e-row-shot .shot{cursor:zoom-in;border:1px solid var(--line);border-radius:8px;overflow:hidden;
  display:block;padding:0;background:#111;width:100%}
.e2e-row-shot .shot img{display:block;width:100%;max-height:220px;object-fit:contain;object-position:top}
.e2e-noshot{border:1px dashed var(--line);border-radius:8px;padding:18px 8px;text-align:center;
  color:var(--ink-soft);font-size:12px;line-height:1.5;background:var(--pend-bg)}
.e2e-row-body{flex:1;min-width:0}
.e2e-row-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.e2e-tc{font:700 13px var(--mono)}
/* 應有值 / 實際 兩塊並排（比照 Signal 卡） */
.e2e-kv{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px}
.e2e-block{border:1px solid var(--line);border-radius:9px;padding:8px 11px;min-width:0}
.e2e-expect{background:var(--accent-soft)}
.e2e-lbl{display:block;font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--ink-soft);margin-bottom:4px}
.e2e-val{font-size:13px;line-height:1.55;color:var(--ink);word-break:break-word;overflow-wrap:anywhere}
.e2e-endpoint{display:block;margin-top:6px;padding-top:5px;border-top:1px dashed var(--line);
  font:11px var(--mono);color:var(--ink-soft);word-break:break-all;white-space:normal}
@media(max-width:640px){.e2e-kv{grid-template-columns:1fr}}
.e2e-badge{font-size:11px;font-weight:800;padding:3px 10px;border-radius:999px;white-space:nowrap}
.e2e-b-pass{color:#0a7d3c;background:var(--pass-bg)} .e2e-b-fail{color:var(--fail);background:var(--fail-bg)}
.e2e-b-blocked{color:var(--block);background:var(--block-bg)}
@media (max-width:640px){.e2e-row{flex-direction:column}.e2e-row-shot{width:100%;flex-basis:auto}}
.tiles{max-width:1180px;margin:0 auto;padding:6px 24px 14px;display:flex;gap:8px;flex-wrap:wrap}
.tile{cursor:pointer;border:1px solid var(--line);background:var(--panel);border-radius:9px;
  padding:8px 14px;display:flex;flex-direction:column;min-width:78px;font-family:var(--sans);
  color:var(--ink);transition:border-color .15s,transform .05s}
.tile:hover{border-color:var(--accent)}
.tile:active{transform:translateY(1px)}
.tile.is-on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.tile-n{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums;font-family:var(--mono)}
.tile-l{font-size:11px;color:var(--ink-soft);letter-spacing:.02em}
.tile[data-filter=pass] .tile-n{color:var(--pass)} .tile[data-filter=fail] .tile-n{color:var(--fail)}
.tile[data-filter=blocked] .tile-n{color:var(--block)}
main{max-width:1180px;margin:0 auto;padding:22px 24px 80px}
.setup-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-bottom:18px}
.setup-cards article{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.setup-cards h2{font-size:13px;color:var(--accent);margin:0 0 10px}
.setup-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px 14px}
.setup-grid div{display:flex;flex-direction:column;min-width:0}.setup-grid span{font-size:9.5px;color:var(--ink-soft)}
.setup-grid strong{font:11px var(--mono);word-break:break-all}
.lead{color:var(--ink-soft);font-size:14px;max-width:80ch;margin:0 0 26px;border-left:2px solid var(--accent);
  padding-left:14px}
.lead b{color:var(--ink)}
.cat{margin:0 0 34px}
.cat-h{display:flex;align-items:center;gap:12px;font-size:15px;margin:0 0 14px;
  padding-bottom:8px;border-bottom:1px solid var(--line);letter-spacing:.01em}
.cat-k{font-family:var(--mono);font-size:12px;color:var(--accent);background:var(--accent-soft);
  padding:3px 8px;border-radius:6px;letter-spacing:.04em}
.cat-n{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--ink-soft);
  font-variant-numeric:tabular-nums}
/* 欄數依寬度自適應（窄→單欄、寬→多欄），卡高依視窗高縮放；內容超出由正/反面自行捲動 */
.grid{display:grid;gap:clamp(12px,1.4vw,18px);align-items:stretch;
  grid-template-columns:repeat(auto-fill,minmax(min(100%,clamp(300px,42vw,440px)),1fr))}
.card{height:clamp(480px,72vh,600px);position:relative;perspective:1200px;border-radius:12px}
.card-inner{position:absolute;inset:0;transition:transform .42s cubic-bezier(.2,.7,.2,1);
  transform-style:preserve-3d}
.card.is-flipped .card-inner{transform:rotateY(180deg)}
.face{position:absolute;inset:0;background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:clamp(11px,1.8vh,16px);overflow:hidden;backface-visibility:hidden;-webkit-backface-visibility:hidden}
/* 正面不捲動：內距/間距/字級皆隨卡片高（72vh）縮放，保持簡潔一頁到底。
   完整證據（截圖、mock 指令、retry 歷史…）在背面（可捲）。 */
.card-front{display:flex;flex-direction:column;gap:clamp(5px,.9vh,10px);overflow:hidden}
.card-back{transform:rotateY(180deg);display:flex;flex-direction:column;padding-bottom:0}
.face::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px}
.card[data-status=pass] .face::before{background:var(--pass)}
.card[data-status=fail] .face::before{background:var(--fail)}
.card[data-status=blocked] .face::before{background:var(--block)}
.card-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tc{font-family:var(--mono);font-weight:700;font-size:clamp(12px,1.9vh,15px);letter-spacing:.02em}
.tier{font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-soft);
  border:1px solid var(--line);border-radius:5px;padding:1px 6px}
.pill{margin-left:auto;font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;letter-spacing:.02em}
.pill-pass{color:var(--pass);background:var(--pass-bg)} .pill-fail{color:var(--fail);background:var(--fail-bg)}
.pill-blocked{color:var(--block);background:var(--block-bg)}
.field{font-family:var(--mono);font-size:clamp(11px,1.7vh,12.5px);color:var(--accent);word-break:break-all}
.signal{font-size:clamp(11.5px,1.8vh,13px);font-weight:700;color:var(--text);margin-top:clamp(-9px,-1.2vh,-6px)}
.schema{display:grid;gap:4px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--bg)}
.schema strong{font-size:13px}.schema span,.schema small{font-size:11.5px;color:var(--muted);line-height:1.45}
.schema code{color:var(--accent)}
.cond{margin:0;font-size:12.5px;color:var(--ink-soft)}
.result-kv{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,.9fr);gap:clamp(7px,1vh,9px);
  align-items:stretch;margin:3px 0 0;min-height:clamp(96px,18vh,145px)}
.result-block{border:1px solid var(--line);border-radius:10px;padding:clamp(9px,1.5vh,13px) 12px;
  background:var(--bg);min-width:0;min-height:clamp(96px,18vh,145px);
  display:flex;flex-direction:column;gap:clamp(6px,1vh,9px);justify-content:flex-start}
.result-label{font-size:clamp(9px,1.3vh,9.5px);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-soft)}
.result-block strong{font-family:var(--mono);font-size:clamp(11.5px,1.8vh,13px);line-height:1.5;word-break:break-word}
.result-block small{font-size:clamp(10px,1.5vh,11px);line-height:1.4;color:var(--ink-soft)}
.result-block small b{font-family:var(--mono);color:var(--ink)}
.capture-ref{margin-top:auto;padding-top:7px;border-top:1px dashed var(--line);word-break:break-all}
.golden-block{border-color:color-mix(in srgb,var(--accent) 48%,var(--line));background:var(--accent-soft)}
.schema-ref{margin-top:6px;padding-top:5px;border-top:1px dashed var(--line);
  font-family:var(--mono);color:var(--ink-soft);opacity:.85;
  word-break:break-word;overflow-wrap:anywhere;white-space:normal}
.absent-why{margin:5px 0 2px;padding:5px 9px;border-radius:8px;font-size:clamp(10.5px,1.55vh,12px);line-height:1.5;
  color:#b4431f;background:rgba(220,90,40,.1);border-left:4px solid #dc5a28}
.absent-why b{font-weight:800}
.absent-shot{color:var(--ink-soft);font-size:11.5px}
@media(prefers-color-scheme:dark){.absent-why{color:#ff9b6b;background:rgba(220,90,40,.16)}}
.mock-cmd{margin:5px 0 2px;padding:6px 9px;border-radius:8px;min-width:0;
  background:rgba(59,110,165,.09);border-left:4px solid #3b6ea5}
.mock-label{display:block;font-size:11px;font-weight:700;color:#2f5f96;margin-bottom:3px}
/* 長指令換行、不橫向撐破卡片（pre-wrap 保留換行、break-all 允許在字元間斷） */
.mock-cmd code{display:block;font-family:var(--mono);font-size:clamp(10px,1.5vh,11.5px);line-height:1.45;
  white-space:pre-wrap;word-break:break-all;overflow-wrap:anywhere;color:var(--ink)}
.mock-cmd .mock-reset{color:var(--ink-soft);opacity:.85;margin-top:2px}
@media(prefers-color-scheme:dark){.mock-label{color:#8fbdf0}.mock-cmd{background:rgba(59,110,165,.16)}}
.schema-note{margin-top:auto}
.card[data-status=pass] .actual-block strong{color:var(--pass)}
.card[data-status=fail] .actual-block strong{color:var(--fail)}
.status-result{display:flex;align-items:center;justify-content:space-between;border:1px solid currentColor;
  border-radius:10px;padding:clamp(6px,1.2vh,9px) 13px;font-weight:800}
.status-result span{font:700 clamp(8.5px,1.3vh,9.5px) var(--sans);letter-spacing:.13em;opacity:.75}
.status-result strong{font-size:clamp(15px,2.6vh,20px);line-height:1;text-transform:uppercase;letter-spacing:.02em}
.status-pass{color:var(--pass);background:var(--pass-bg)}
.status-fail{color:var(--fail);background:var(--fail-bg)}
.status-blocked{color:var(--block);background:var(--block-bg)}
.flip-btn{cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--accent);
  border-radius:8px;padding:clamp(5px,1vh,7px) 10px;font:600 clamp(10.5px,1.5vh,11.5px) var(--sans)}
.flip-btn:hover{border-color:var(--accent);background:var(--accent-soft)}
.flip-open{margin-top:auto;width:100%}
.back-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding-bottom:10px;
  border-bottom:1px solid var(--line);flex:none}
.back-title{font-size:11px;color:var(--ink-soft);margin-left:8px}
.flip-close{padding:4px 8px}
.back-scroll{display:flex;flex-direction:column;gap:9px;overflow:auto;min-height:0;padding:11px 2px 12px}
/* flex 空間不足時 overflow:hidden 的 .shot 會被壓成 0 高（截圖看起來像消失）；禁止壓縮，超出改由 back-scroll 捲動 */
.back-scroll>*{flex-shrink:0}
.ev-source{font-size:11px;color:var(--ink-soft);display:flex;gap:8px;align-items:flex-start}
.ev-source .rl{font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);flex:none}
.ev-source code{font-family:var(--mono);font-size:10.5px;word-break:break-all}
.capture-id{display:block;padding:10px 12px;
  border:1px solid var(--line);border-radius:9px;background:var(--bg)}
.capture-id>div{display:flex;flex-direction:column;gap:3px;min-width:0}
.capture-id span,.bid-identity .rl{font-size:9px;letter-spacing:.09em;color:var(--accent);font-weight:700}
.capture-id strong,.capture-id code{font:600 11px var(--mono);word-break:break-all}
.capture-id .capture-file{padding:0}
.bid-evidence{display:flex;flex-direction:column;gap:7px;padding:11px 12px;border:1px solid var(--line);
  border-radius:9px;background:var(--bg)}
.bid-evidence code{font-family:var(--mono);font-size:12px;line-height:1.55;word-break:break-all}
.bid-evidence code b{color:var(--accent)}
.bid-identity{display:grid;grid-template-columns:auto minmax(0,1fr);gap:5px 10px;padding:10px 12px;
  border:1px solid var(--line);border-radius:9px;background:var(--bg)}
.bid-identity>.rl{grid-column:1/-1;margin-bottom:2px}
.bid-identity>div{display:contents}.bid-identity div span{font-size:10px;color:var(--ink-soft)}
.bid-identity div code{font:11px var(--mono);word-break:break-all;color:var(--ink)}
.proof-state{font-size:clamp(10px,1.45vh,11px);font-weight:700;padding:clamp(5px,.9vh,6px) 9px;border-radius:7px}
.proof-ok{color:var(--pass);background:var(--pass-bg)}
.proof-missing{color:var(--pend);background:var(--pend-bg)}
.proof-why{padding:10px 12px;border-left:3px solid var(--accent);background:var(--accent-soft);border-radius:0 8px 8px 0}
.proof-why .rl{font-size:9.5px;letter-spacing:.08em;color:var(--accent);text-transform:uppercase;font-weight:700}
.proof-why p{font-size:12px;line-height:1.55;margin:5px 0 0;color:var(--ink)}
.tc-detail{font-size:11.5px;color:var(--ink-soft);border-top:1px dashed var(--line);padding-top:8px}
.tc-detail summary{cursor:pointer;color:var(--accent);font-weight:600}.tc-detail .cond{margin-top:7px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;align-items:baseline}
.kv .k{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-soft)}
.kv .v{font-family:var(--mono);font-size:12.5px;word-break:break-all;font-variant-numeric:tabular-nums}
.v-exp{color:var(--ink)} .v-act{color:var(--ink);font-weight:600}
.card[data-status=fail] .v-act{color:var(--fail)}
.card[data-status=pass] .v-act{color:var(--pass)}
.edit{display:flex;align-items:center;gap:7px;flex-wrap:wrap;border-top:1px solid var(--line);
  padding:10px 0 11px;background:var(--panel);flex:none;margin-top:auto}
.edit .rl{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent)}
.edit .ovr{font-family:var(--sans);font-size:12px;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 6px}
.edit .ovr-note{flex:1;min-width:120px;font-family:var(--sans);font-size:12px;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 8px}
.edit .ovr-note::placeholder{color:var(--ink-soft)}
.review-edit{display:grid;grid-template-columns:130px minmax(0,1fr);gap:8px;margin:0;
  padding:clamp(7px,1.2vh,10px);border:1px solid var(--line);border-radius:10px;background:var(--bg)}
.review-edit label{display:flex;flex-direction:column;gap:clamp(3px,.6vh,5px);min-width:0}
.review-edit .rl{font-weight:700;color:var(--ink-soft)}
.review-edit .ovr,.review-edit .ovr-note{width:100%;box-sizing:border-box;
  height:clamp(26px,3.6vh,30px);background:var(--panel)}
.review-edit .reason-label{min-width:0}
.note{font-size:clamp(10px,1.5vh,11.5px);border-radius:7px;padding:clamp(5px,1vh,7px) 10px;line-height:1.4}
.note-rd{background:var(--fail-bg);color:var(--fail)}
.note-bl{background:var(--block-bg);color:var(--block)}
.action{font-size:11.5px;color:var(--ink-soft);border-top:1px dashed var(--line);padding-top:9px}
.action .rl{display:inline-block;min-width:64px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-right:8px}
.ids{font-size:11.5px;color:var(--ink-soft);border-top:1px dashed var(--line);padding-top:9px;line-height:1.7}
.ids .rl{display:inline-block;min-width:64px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-right:8px}
.ids code{font-family:var(--mono);font-size:11px;background:var(--bg);padding:1px 5px;border-radius:4px;word-break:break-all}
.chklist{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:0 0 26px}
.chklist summary{cursor:pointer;font-weight:600;font-size:14px;color:var(--ink)}
.chklist-lead{font-size:12.5px;color:var(--ink-soft);margin:10px 0}
.chklist code{font-family:var(--mono);font-size:11.5px;background:var(--bg);padding:1px 5px;border-radius:4px}
.mwrap{overflow-x:auto}
.mtable{border-collapse:collapse;width:100%;font-size:12.5px}
.mtable td{padding:6px 10px;border-top:1px solid var(--line);vertical-align:top}
.mtc{font-family:var(--mono);font-weight:600;white-space:nowrap}
.mtag{white-space:nowrap;font-size:10px;letter-spacing:.04em;border-radius:5px;padding:2px 7px}
.mtag-blk{color:var(--block);background:var(--block-bg)}
.mtag-pass{color:var(--pass);background:var(--pass-bg)} .mtag-fail{color:var(--fail);background:var(--fail-bg)}
.con{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:0 0 26px}
.con-h{font-size:14px;margin:0;color:var(--accent)}
.con-lead{font-size:12.5px;color:var(--ink-soft);margin:6px 0 12px}
.con-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-top:1px solid var(--line);flex-wrap:wrap}
.con-ok{font-weight:700;width:18px;text-align:center}
.con-y{color:var(--pass)} .con-n{color:var(--fail)}
.con-lab{font-weight:600;font-size:13px;min-width:180px}
.con-msg{font-size:12px;color:var(--ink-soft);flex:1}
.con-val{font-family:var(--mono);font-size:11.5px;background:var(--bg);padding:2px 7px;border-radius:5px;word-break:break-all}
.repro{display:flex;flex-direction:column;gap:5px;font-size:11.5px;color:var(--ink-soft);
  border-top:1px dashed var(--line);padding-top:9px}
.repro .rl{display:inline-block;min-width:64px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--accent);margin-right:8px}
.shot{margin-top:2px;padding:0;border:1px solid var(--line);border-radius:8px;overflow:hidden;
  cursor:zoom-in;background:var(--bg);display:block;width:100%;text-align:left}
.shot img{display:block;width:100%;max-height:360px;object-fit:contain;object-position:top;background:#111}
.e2e-shot{max-width:520px;margin:14px auto 2px;background:var(--panel)}
.e2e-shot img{max-height:520px;object-fit:contain;background:var(--bg)}
.shot[data-unmatched] img{filter:grayscale(.5) opacity(.7)}
.shot-cap{display:block;font-size:10px;color:var(--ink-soft);padding:4px 8px;background:var(--panel)}
.lightbox{position:fixed;inset:0;z-index:50;background:rgba(6,10,13,.85);display:none;
  align-items:center;justify-content:center;padding:30px}
.lightbox.open{display:flex}
.lightbox img{max-width:min(440px,90vw);max-height:90vh;border-radius:10px;
  box-shadow:0 20px 60px rgba(0,0,0,.5)}
.lb-x{position:absolute;top:18px;right:22px;width:40px;height:40px;border-radius:50%;border:none;
  background:rgba(255,255,255,.14);color:#fff;font-size:24px;cursor:pointer;line-height:1}
.lb-x:hover{background:rgba(255,255,255,.26)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media (max-width:820px){.meta{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px 0}
  .meta div:nth-child(odd){padding-left:0;border-left:0}}
@media (max-width:420px){.review-edit{grid-template-columns:1fr}}
@media (max-width:640px){.top-in{padding:14px 16px 10px}.tiles{padding:6px 16px 12px}main{padding:18px 16px 60px}}
@media (max-width:640px){.setup-cards{grid-template-columns:1fr}}
"""


def _deep_merge(base, overlay):
    """Return a recursive merge without mutating either evidence object."""
    merged = copy.deepcopy(base) if isinstance(base, dict) else {}
    if not isinstance(overlay, dict):
        return merged
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _decode_ext_enc(bid):
    """Decode the real AOS Signal payload embedded in the request, if present."""
    if not isinstance(bid, dict) or not bid.get("ext_enc"):
        return None
    from apr_xorenc import decode_ext_enc
    _raw, decoded = decode_ext_enc(bid)
    return decoded


def _unwrap(bid):
    """Locate the data-signal payload {app, device, user}.

    Signal fields live under top-level "ext" in real bid traffic; the bid
    file may also be the raw payload itself (from the [AppierDataSignal]
    logcat line). "req" is the ads SDK's own params — last-resort fallback
    only, most signal TCs will report missing against it.
    """
    if isinstance(bid, dict):
        ext = bid.get("ext")
        if isinstance(ext, str):
            sys.exit(
                "ext is a string — data-signal encryption appears to be "
                "re-enabled in the SDK; plaintext inspection is not possible."
            )
        if isinstance(ext, dict) and ({"app", "device", "user"} & ext.keys()):
            return ext
        if {"device", "user"} & bid.keys():
            return bid
        if isinstance(bid.get("req"), dict):
            # AOS requests keep ordinary ad fields in req（2026-08 起改送加密的
            # req_enc，見 normalize_bid）and the full Signal payload in ext_enc.
            # Decode and overlay every decrypted device/user/app field so all
            # validators inspect the real signal values, while callers retain
            # the untouched raw request.
            decoded = _decode_ext_enc(bid)
            if isinstance(decoded, dict):
                return _deep_merge(bid["req"], decoded)
            return bid["req"]
    return bid


def _trunc(val, n=38):
    s = str(val) if not isinstance(val, str) else val
    return (s[:n] + "…") if len(s) > n else s


def get_field(bid, path):
    """Resolve dotted path against the unwrapped bid object."""
    parts = path.split(".")
    obj = bid
    for part in parts:
        if not isinstance(obj, dict) or part not in obj:
            return None, False
        obj = obj[part]
    return obj, True


def run_validator(bid, v, reference_ms=None):
    """Returns (passed: bool, actual, message: str)."""
    field = v["field"]
    check = v["check"]
    value, found = get_field(bid, field)

    # checks that tolerate an absent field must run before the generic
    # "missing" gate below
    if check == "absent":
        if not found or value is None:
            return True, None, "absent ✓"
        return False, value, "expected absent"

    if check == "present":
        # 只確認欄位存在，值/空與否都可（例：applist 能拿多少算多少）
        if found:
            n = len(value) if isinstance(value, (list, dict, str)) else None
            return True, value, ("欄位存在 ✓" + (f"（{n} 項）" if n is not None else ""))
        return False, None, "欄位不存在"

    if check == "absent_or_empty":
        if not found or value is None or value == "":
            return True, value, "absent/empty ✓"
        return False, value, "expected absent or empty"

    if check == "value_or_absent":
        if not found or value is None:
            return True, value, "absent ✓"
        exp = v["expected"]
        if value == exp:
            return True, value, f"= {exp!r} ✓"
        return False, value, f"expected {exp!r} or absent"

    if check == "int_zero_or_absent":
        if not found or value is None:
            return True, value, "absent ✓"
        return ((True, value, "= 0 ✓") if type(value) is int and value == 0
                else (False, value, "expected integer 0 or absent"))

    if check == "falsy":
        if not found or not value:
            return True, value, "falsy/absent ✓"
        return False, value, "expected falsy/absent"

    if not found or value is None:
        return False, None, "field missing"

    if check == "uuid_nonzero":
        ok = isinstance(value, str) and bool(UUID_RE.fullmatch(value)) and value != ZERO_UUID
        return ((True, value, "valid non-zero UUID ✓") if ok
                else (False, value, "expected lowercase non-zero UUID"))

    if check == "one_of_typed":
        allowed = v["expected"]
        ok = any(type(value) is type(exp) and value == exp for exp in allowed)
        return ((True, value, f"one of {allowed!r} ✓") if ok
                else (False, value, f"expected one of {allowed!r} with exact type"))

    if check == "vpn_active":
        # backend 定義 device.ext.vpn 為 string：VPN on 應為非空協定字串，
        # 不接受 boolean（型別錯本身就是 fail）
        ok = isinstance(value, str) and bool(value.strip())
        return ((True, value, "non-empty VPN protocol string ✓") if ok
                else (False, value, "expected non-empty protocol string (backend type = string)"))

    if check == "value":
        exp = v["expected"]
        # sheet repeatedly calls out "wrong type" as its own failure mode
        # (e.g. int 1 sent where bool true expected) — require exact type match
        if isinstance(exp, bool):
            ok = isinstance(value, bool) and value == exp
        elif isinstance(exp, int):
            ok = type(value) is int and value == exp
        elif isinstance(exp, float):
            # org.json serializes 1.0f as "1" (strips trailing .0), so a
            # float expectation must accept a numerically-equal int
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value == exp
        else:
            ok = value == exp
        if ok:
            return True, value, f"= {exp!r} ✓"
        return False, value, f"expected {exp!r}, got {value!r}"

    if check == "regex":
        if isinstance(value, str) and v["pattern"].match(value):
            return True, value, "format ✓"
        return False, value, f"format mismatch ({v['pattern'].pattern})"

    if check == "ipv4_nonzero":
        if isinstance(value, str) and IPV4_RE.match(value) and value != "0.0.0.0":
            return True, value, "valid IPv4, non-zero ✓"
        return False, value, "invalid format or 0.0.0.0"

    if check == "range":
        try:
            n = float(value)
            lo, hi = v["min"], v["max"]
            if lo <= n <= hi:
                return True, value, f"in [{lo}, {hi}] ✓"
            return False, value, f"out of range [{lo}, {hi}]"
        except (TypeError, ValueError):
            return False, value, "not numeric"

    if check == "int_range":
        lo, hi = v["min"], v["max"]
        ok = type(value) is int and lo <= value <= hi
        return ((True, value, f"integer in [{lo}, {hi}] ✓") if ok
                else (False, value, f"expected integer in [{lo}, {hi}]"))

    if check == "nonzero_range":
        try:
            n = float(value)
            lo, hi = v["min"], v["max"]
            ok = lo <= n <= hi and n != 0
            return ((True, value, f"non-zero in [{lo}, {hi}] ✓") if ok
                    else (False, value, f"expected non-zero in [{lo}, {hi}]"))
        except (TypeError, ValueError):
            return False, value, "not numeric"

    if check == "positive_int":
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return True, value, "> 0 ✓"
        return False, value, "expected positive integer"

    if check == "positive_float":
        try:
            n = float(value)
            if n > 0:
                return True, value, "> 0 ✓"
            return False, value, "expected positive number"
        except (TypeError, ValueError):
            return False, value, "not numeric"

    if check == "nonempty":
        if value and str(value).strip():
            return True, value, "non-empty ✓"
        return False, value, "empty or null"

    if check == "nonempty_notunknown":
        s = str(value).strip().lower()
        if value and s and s != "unknown":
            return True, value, "non-empty ✓"
        return False, value, '"unknown"/empty'

    if check == "truthy":
        return (True, value, "truthy ✓") if value else (False, value, "expected truthy")

    # 陣列類：actual 一律回傳實際內容（讓報告看得到值），數量寫進 message；
    # 過長的內容由 build_artifact.fmt_val 自動截斷，不會撐破卡面
    if check == "array_nonempty":
        if isinstance(value, list) and value:
            return True, value, f"non-empty（{len(value)} 筆）✓"
        return False, value, "expected non-empty array"

    if check == "array":
        return ((True, value, f"array（{len(value)} 筆）✓") if isinstance(value, list)
                else (False, value, "expected array"))

    if check == "array_timestamp":
        ok = (isinstance(value, list) and bool(value) and
              all(type(x) is int and len(str(x)) == 13 for x in value))
        return ((True, value, f"{len(value)} 個 13-digit ms timestamps ✓") if ok
                else (False, value, "expected non-empty array of 13-digit integer timestamps"))

    if check == "array_regex":
        if not isinstance(value, list) or not value:
            return False, value, "expected non-empty array"
        bad = [x for x in value if not isinstance(x, str) or not v["pattern"].match(x)]
        if not bad:
            return True, value, f"{len(value)} 筆全部符合格式 ✓"
        return False, value, f"invalid: {bad}"

    if check == "array_number":
        if not isinstance(value, list) or not value:
            return False, value, "expected non-empty array"
        bad = [x for x in value if not isinstance(x, (int, float)) or isinstance(x, bool)]
        if not bad:
            return True, value, f"{len(value)} 個數值 ✓"
        return False, value, f"{len(bad)} non-numeric elements"

    if check == "array_impression":
        if not isinstance(value, list) or not value:
            return False, value, "expected non-empty array"
        required = {"wintime", "displaytime", "adomain", "bundle",
                    "clicktime", "backgroundtime", "storeviewtime"}
        bad = [e for e in value if not isinstance(e, dict) or not required.issubset(e)]
        if not bad:
            return True, value, f"{len(value)} 筆 impression 結構正確 ✓"
        return False, value, f"{len(bad)} elements missing keys"

    if check == "leq_field":
        ref, ref_found = get_field(bid, v["ref_field"])
        if not ref_found or ref is None:
            return False, value, f"ref {v['ref_field']} not found"
        try:
            if type(value) is int and type(ref) is int and 0 <= value <= ref:
                return True, value, f"<= {v['ref_field']}={ref} ✓"
            return False, value, f"{value} > {v['ref_field']}={ref}"
        except (TypeError, ValueError):
            return False, value, "not numeric"

    if check == "equals_field":
        ref, ref_found = get_field(bid, v["ref_field"])
        if not ref_found:
            return False, value, f"ref {v['ref_field']} not found"
        ok = type(value) is type(ref) and value == ref
        return ((True, value, f"= {v['ref_field']} ({ref!r}) ✓") if ok
                else (False, value, f"expected same as {v['ref_field']}={ref!r}"))

    if check == "timestamp_recent":
        try:
            if type(value) is not int or len(str(value)) != 13:
                return False, value, "expected 13-digit integer ms timestamp"
            ts_sec = int(value) / 1000
            reference_sec = reference_ms / 1000 if reference_ms is not None else time.time()
            diff = abs(reference_sec - ts_sec)
            if diff < 120:
                return True, value, f"within {int(diff)}s ✓"
            return False, value, f"{int(diff)}s off from now"
        except (TypeError, ValueError):
            return False, value, "not a valid ms timestamp"

    return False, value, f"unknown check '{check}'"


def format_report(results, bid_file="", header=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    W = 76
    lines = [
        "=" * W,
        f"  SSP SDK Bid Inspector  —  {ts}",
        (f"  Source: {bid_file}" if bid_file else ""),
        (f"  {header}" if header else ""),
        "=" * W,
        "",
        f"{'TC':<10}  {'Field':<34}  {'Actual':<26}  Result",
        f"{'─'*10}  {'─'*34}  {'─'*26}  {'─'*10}",
    ]
    passed = failed = 0
    for r in results:
        status = "PASS ✓" if r["passed"] else "FAIL ✗"
        note   = f"  ← {r['note']}" if r["note"] and not r["passed"] else ""
        lines.append(
            f"{r['tc']:<10}  {r['field']:<34}  {_trunc(r['actual']):<26}  {status}{note}"
        )
        if r["passed"]:
            passed += 1
        else:
            failed += 1
    lines += [
        f"{'─'*W}",
        f"  {passed} passed  /  {failed} failed  /  {passed + failed} total",
        "=" * W,
    ]
    return "\n".join(l for l in lines if l is not None)


def encode_shot(path, max_w=720, quality=78):
    """縮圖 + JPEG 編碼成 data URI，控制 artifact 體積（原尺寸 PNG 會爆 16MB 上限）。

    720px 寬足以肉眼讀設定頁的開關/文字；無 Pillow 時退回原檔 base64。
    """
    if Image is None:
        return "data:image/png;base64," + base64.b64encode(open(path, "rb").read()).decode()
    im = Image.open(path).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def esc(x):
    return html.escape(str(x), quote=True)


def fmt_val(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False)
        return s if len(s) <= 120 else s[:117] + "…"
    return str(v)


def js_block(shots_json, round_json):
    return """
const SHOTS = %s;
const ROUND = %s;
// lazy-set thumbnails
document.querySelectorAll('.shot img[data-src]').forEach(img=>{
  const k=img.getAttribute('data-src'); if(SHOTS[k]) img.src=SHOTS[k];
});
// filter
const tiles=document.querySelectorAll('.tile');
tiles.forEach(t=>t.addEventListener('click',()=>{
  tiles.forEach(x=>x.classList.remove('is-on')); t.classList.add('is-on');
  const f=t.dataset.filter;
  document.querySelectorAll('.card').forEach(c=>{
    c.style.display=(f==='all'||c.dataset.status===f)?'':'none';
  });
  document.querySelectorAll('.cat').forEach(sec=>{
    const any=[...sec.querySelectorAll('.card')].some(c=>c.style.display!=='none');
    sec.style.display=any?'':'none';
  });
  // 點 Pending/Blocked 但 signal 卡片區沒有對應卡（例如 pending 都在 E2E）時，
  // 捲到「未完成項目」面板顯示原因，不留空白頁
  if(f!=='all' && ![...document.querySelectorAll('.card')].some(c=>c.style.display!=='none')){
    const panel=document.getElementById('checklist');
    if(panel){panel.open=true;panel.scrollIntoView({behavior:'smooth',block:'start'});}
  }
}));
// lightbox
const lb=document.getElementById('lb'), lbImg=document.getElementById('lb-img');
document.querySelectorAll('.shot').forEach(s=>s.addEventListener('click',()=>{
  const k=s.dataset.shot;
  // E2E 逐步截圖 img src 是內嵌的（不在 SHOTS）；SHOTS 找不到就用按鈕自己的 img
  let src=SHOTS[k]; if(!src){const im=s.querySelector('img'); src=im&&im.src;}
  if(!src)return;
  lbImg.src=src; lb.hidden=false; lb.classList.add('open');
}));
function closeLb(){lb.classList.remove('open'); lb.hidden=true; lbImg.src='';}
document.getElementById('lb-x').addEventListener('click',closeLb);
lb.addEventListener('click',e=>{if(e.target===lb)closeLb();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeLb();});

// card flip: front = result, back = evidence
document.querySelectorAll('.card').forEach(card=>{
  card.querySelector('.flip-open')?.addEventListener('click',()=>card.classList.add('is-flipped'));
  card.querySelector('.flip-close')?.addEventListener('click',()=>card.classList.remove('is-flipped'));
});

// ── 人工覆寫判定 (localStorage，重整不掉；同一 artifact URL 持久) ──
const ST = {pass:'Pass', fail:'Fail', blocked:'Blocked'};
const OVR_KEY = 'appier-qa-ovr:'+ROUND;
let OVR = {};
try { OVR = JSON.parse(localStorage.getItem(OVR_KEY) || '{}'); } catch(e){ OVR = {}; }
function applyStatus(card, st){
  card.dataset.status = st;
  const pill = card.querySelector('.pill');
  if(pill){ pill.className = 'pill pill-'+st; pill.textContent = ST[st] || st; }
  const result = card.querySelector('.status-result');
  if(result){ result.className = 'status-result status-'+st; result.querySelector('strong').textContent = ST[st] || st; }
}
function saveOvr(k, st, n){
  if(!st && !n){ delete OVR[k]; } else { OVR[k] = {st:st, note:n}; }
  localStorage.setItem(OVR_KEY, JSON.stringify(OVR));
}
function recount(){
  const rank = {pass:0,blocked:1,fail:2};
  const byTc = {};
  const cards = document.querySelectorAll('.card');
  cards.forEach(x=>{
    const tc=(x.dataset.key||'').split('|')[0], st=x.dataset.status;
    if(!byTc[tc] || rank[st] > rank[byTc[tc]]) byTc[tc]=st;
  });
  const c = {pass:0,fail:0,blocked:0};
  Object.values(byTc).forEach(st=>{c[st]=(c[st]||0)+1;});
  // tile 只算 Signal（E2E 有自己的 scorecard）
  document.querySelectorAll('.tile').forEach(t=>{
    const f=t.dataset.filter, n=t.querySelector('.tile-n');
    if(!n) return;
    if(f==='all') n.textContent = Object.keys(byTc).length;
    else if(c[f]!==undefined) n.textContent = c[f];
  });
}
// ── tab 切換：Signal / E2E ──
document.querySelectorAll('.tabbtn').forEach(b=>b.addEventListener('click',()=>{
  const tab=b.dataset.tab;
  document.querySelectorAll('.tabbtn').forEach(x=>x.classList.toggle('is-on',x===b));
  document.querySelectorAll('.tab-pane').forEach(p=>{p.hidden=(p.dataset.pane!==tab);});
  const tiles=document.querySelector('.tiles');
  if(tiles) tiles.style.display=(tab==='signal')?'':'none';
}));
document.querySelectorAll('.card').forEach(card=>{
  const k=card.dataset.key, sel=card.querySelector('.ovr'), note=card.querySelector('.ovr-note');
  if(!sel) return;
  const o = OVR[k];
  if(o){ if(o.st){ sel.value=o.st; applyStatus(card, o.st); } if(o.note && note){ note.value=o.note; } }
  sel.addEventListener('change',()=>{
    applyStatus(card, sel.value || card.dataset.auto);
    saveOvr(k, sel.value, note ? note.value : '');
    recount();
  });
  if(note) note.addEventListener('input',()=> saveOvr(k, sel.value, note.value));
});
recount();
""" % (shots_json, round_json)


# ── 判定狀態機與卡片版面（平台資料一律由參數/卡片欄位傳入）──────────────────

def classify(tc, has_capture, passed, blocked_tcs=()):
    """判定狀態機（**唯一實作**，兩平台共用）：只回 PASS / FAIL / BLOCKED。

    blocked_tcs：該平台「清楚的限制」集合（RD 未實作、硬體不可得）。恆 block，
                 即使抓到空值也不算產品 FAIL。由平台檔傳入，契約不偷讀全域表。
    """
    # 一旦有 Capture，判定只由 expected vs actual 決定。BLOCKED
    # 只能描述「尚未取得可判讀 Capture」，不得覆蓋 FAIL 或 PASS。
    # block 只給「非常清楚的限制」：本輪 RD 沒做（SDK 未實作，值恆 null/[]）或硬體不可得
    # （沒 SIM／需 AVD／需非 root）。放在 has_capture 之前——這類即使抓到（空）值也不是
    # 產品 FAIL，是 RD/硬體 gap，恆 block。
    if tc in blocked_tcs:
        return "BLOCKED", None
    if has_capture:
        # 有做：值對＝PASS，值錯＝FAIL（失敗一次就算，不看次數）。
        return ("PASS", None) if passed else ("FAIL", None)
    # 這輪沒做（無 eligible capture／未佈狀態）→ block
    return "BLOCKED", None


def tier_of(check, tc, blocked_tcs=()):
    if tc in blocked_tcs:
        return "Blocked"
    if check in ABSENT_CHECKS:
        return "Absent"
    if check in PARTIAL_CHECKS:
        return "Partial"
    return "Verifiable"


def render_card(c):
    badges = f'<span class="tier tier-{c["tier"].lower()}">{esc(c["tier"])}</span>'
    if c.get("shot"):
        badges += '<span class="tier">有狀態截圖</span>'
    shot_html = ""
    if c["shot"]:
        matched = "" if c["shot_matched"] else ' data-unmatched="1"'
        cap_lbl = c["shot_caption"] or ""
        # src 直接內嵌 data URI：不依賴 JS 填圖，任何瀏覽器/時序都必定顯示
        shot_html = (f'<button class="shot" data-shot="{esc(c["shot"])}"{matched} '
                     f'title="點擊放大">'
                     f'<img alt="{esc(c["tc"])} screenshot" src="{esc(c.get("shot_data") or "")}">'
                     f'<span class="shot-cap">狀態截圖 — {esc(cap_lbl)}</span></button>')
    repro = ""
    if c["set"]:
        repro = (f'<div class="repro"><div><span class="rl">設定狀態</span>{esc(c["set"])}</div>'
                 f'<div><span class="rl">截圖佐證</span>{esc(c["shows"])}</div></div>')
    note = ""
    if c["rd_note"]:
        note = f'<div class="note note-rd">⚑ RD gap — {esc(c["rd_note"])}</div>'
    elif c["blocked_reason"]:
        note = f'<div class="note note-bl">⛔ {esc(c["blocked_reason"])}</div>'
    action = ""
    if c.get("action"):
        action = f'<div class="action"><span class="rl">本次執行</span>{esc(c["action"])}</div>'
    b = c.get("bid_ids") or {}
    identity_rows = "".join(
        f'<div><span>{key}</span><code>{esc(b.get(key) or "—")}</code></div>'
        for key in ("bidobjid", "cid", "crid", "crpid")
    )

    evidence_source = (
        f'<div class="capture-id"><div class="capture-file"><span>SOURCE</span>'
        f'<code>{esc(c.get("capture") or "—")}/bid_request.json</code></div></div>'
    )
    bid_evidence = (
        f'<div class="bid-evidence"><span class="result-label">CAPTURE BID REQUEST</span>'
        f'<code><b>{esc(c["field"])}</b> = {esc(c["actual"])}</code></div>'
    )
    gt = c.get("ground_truth")
    ground_truth = (
        f'<div class="bid-evidence"><span class="result-label">INDEPENDENT DEVICE / APP EVIDENCE</span>'
        f'<code><b>{esc(gt["label"])}</b> = {esc(gt["value"])}</code></div>'
        if gt else ""
    )
    attempt_rows = "".join(
        f'<div><span>{esc(item["capture"])}</span><code>'
        f'{"MATCH" if item["passed"] else "MISMATCH"} · actual={esc(item["actual"])} · '
        f'{esc(item["msg"])}</code></div>'
        for item in c.get("attempts", []))
    retry_history = (
        f'<div class="identity"><span class="result-label">ATTEMPT / RETRY HISTORY</span>{attempt_rows}</div>'
        if attempt_rows else ""
    )
    if c["shot"]:
        proof_state = '<div class="proof-state proof-ok">✓ 同一 Capture 有狀態截圖</div>'
    elif c.get("set"):
        proof_state = (
            '<div class="proof-state proof-missing">△ 缺少同一 Capture 的狀態截圖；'
            f'補證方式：先{esc(c["set"])}，截取「{esc(c["shows"])}」，再重新 Capture。</div>'
        )
    else:
        proof_state = (
            '<div class="proof-state proof-missing">△ 本卡已有 bid_request 值證據，'
            '但沒有外部／系統畫面對照；需補同次 Capture 的獨立來源證據。</div>'
        )
    # 卡片正面直接顯示截圖狀態（精簡版），不用翻面才知道有沒有佐證
    if c["shot"]:
        proof_front = '<div class="proof-state proof-ok">✓ 同一 Capture 有狀態截圖</div>'
    elif c.get("set"):
        proof_front = '<div class="proof-state proof-missing">△ 缺少同一 Capture 的狀態截圖</div>'
    else:
        proof_front = ""
    # 自動 Blocked 的卡片：原因直接顯示在正面，不用翻面找
    if c["status_cls"] == "blocked" and c.get("blocked_reason"):
        proof_front = (f'<div class="note note-bl">⛔ {esc(c["blocked_reason"])}</div>'
                       + proof_front)

    return f"""<article class="card" data-status="{c['status_cls']}" data-auto="{c['status_cls']}" data-key="{esc(c['tc'])}|{esc(c['field'])}">
  <div class="card-inner">
    <section class="face card-front" aria-label="{esc(c['tc'])} result">
      <div class="card-top">
        <span class="tc">{esc(c['tc'])}</span>
        {badges}
      </div>
      <div class="field">{esc(c['field'])}</div>
      <div class="signal">{esc(c['signal'])}</div>
      <div class="result-kv">
        <div class="result-block golden-block">
          <span class="result-label">應有值</span>
          <strong>{esc(c['expected'])}</strong>
          {f'<div class="absent-why">預期沒有值，是因為：<b>{esc(c["absent_reason"]["set"])}</b><br><span class="absent-shot">狀態截圖佐證：{esc(c["absent_reason"]["shows"])}</span></div>' if c.get('absent_reason') else ''}
          {f'<div class="mock-cmd"><span class="mock-label">Mock 指令（adb 設定此狀態）</span><code>{esc(c["mock_cmd"])}</code>{f"<code class=mock-reset># 還原：{esc(c.get("mock_reset_cmd", ""))}</code>" if c.get("mock_reset") else ""}</div>' if c.get('mock_cmd') else ''}
          <small class="schema-ref">Schema · {esc(c['schema_type'])} · {esc(c['schema_format'])}</small>
          {f'<small class="schema-note">{esc(c["schema_note"])}</small>' if c['schema_note'] else ''}
        </div>
        <div class="result-block actual-block">
          <span class="result-label">CAPTURE · 實際收到</span>
          <strong>{esc(c['actual'])}</strong>
          {f'<small class="capture-ref">SOURCE · <b>{esc(c["provenance"])}</b></small>' if c.get('provenance') else ''}
        </div>
      </div>
      <div class="status-result status-{c['status_cls']}">
        <span>RESULT</span><strong>{esc(c['status_label'])}</strong>
      </div>
      {proof_front}
      <div class="edit review-edit">
        <label><span class="rl">人工判定</span>
          <select class="ovr" aria-label="人工覆寫判定">
            <option value="">自動（{esc(c['status_label'])}）</option>
            <option value="pass">Pass</option>
            <option value="fail">Fail</option>
            <option value="blocked">Blocked</option>
          </select>
        </label>
        <label class="reason-label"><span class="rl">理由</span>
          <input class="ovr-note" placeholder="例如：無 SIM，無法驗證 cellular" aria-label="人工覆寫理由">
        </label>
      </div>
      <button class="flip-btn flip-open" type="button">查看 Evidence／狀態截圖 <span aria-hidden="true">↗</span></button>
    </section>
    <section class="face card-back" aria-label="{esc(c['tc'])} evidence">
      <div class="back-head">
        <div><span class="tc">{esc(c['tc'])}</span><span class="back-title">Evidence</span></div>
        <button class="flip-btn flip-close" type="button" aria-label="返回結果">返回結果 ↩</button>
      </div>
      <div class="back-scroll">
        {evidence_source}
        {shot_html}
        {proof_state}
        {bid_evidence}
        {ground_truth}
        {retry_history}
        <div class="bid-identity"><span class="rl">BID IDENTITY</span>{identity_rows}</div>
        <div class="proof-why"><span class="rl">如何證明</span><p>{esc(c['evidence_explanation'])}</p></div>
        <details class="tc-detail"><summary>TC 判定條件與技術備註</summary><p class="cond">{esc(c['condition'])}</p></details>
        {note}
        {action}
        {repro}
      </div>
    </section>
  </div>
</article>"""
