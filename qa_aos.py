#!/usr/bin/env python3
"""qa_aos.py — Android(AOS) SSP Signal QA：單檔完整流程。

用法：
    python qa_aos.py                       跑一輪：Signal + E2E 全驗（預設）
    python qa_aos.py --signal-only         只驗 Signal（跳過 privacy/廣告點擊/landing）
    python qa_aos.py --e2e-only            只驗 E2E（只跑 CURRENT 那一次 capture）
    python qa_aos.py AND-04,AND-06 [UDID]  補跑指定 TC
    python qa_aos.py --report <round_dir> [--out x.html] [--meta m.json]   重算單輪報告
    python qa_aos.py --inspect <bid.json>  離線驗一份 bid request
    python qa_aos.py --inspect-round <dir> 離線重算整輪

架構規則（勿違反，否則會回到「改一邊、另一邊靜默壞掉」的老路）：
  * 本檔與 qa_ios.py **零 import**，兩平台完全獨立、可各自單獨執行完畢
  * 本檔只 import apr_xorenc（SDK 的 ae1 加解密＝規格，兩平台必須一致）
  * 跨平台整合頁與發佈在 page.py；本檔用 subprocess 呼叫它，不 import
  * mitmdump addon 必須是獨立檔（mitmdump -s mitmdump_addon.py）

前置服務：
    mitmdump -s mitmdump_addon.py --listen-port 8081
    appium
    手機 Wi-Fi proxy → Mac IP:8888（Charles），Charles upstream → 127.0.0.1:8081
"""

import argparse
import atexit
import base64
import copy
import getpass
import glob
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

from appium import webdriver
from appium.options.android.uiautomator2.base import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from datetime import datetime
from pathlib import Path

from verdict import (                                          # 共用判定/報告契約
    classify,
    tier_of,
    render_card,
    _decode_ext_enc,
    UUID_RE,
    BCP47_RE,
    INPUT_LANG_RE,
    ISO639_RE,
    CELL_4G5G_RE,
    IPV4_RE,
    SEMVER_RE,
    ANDROID_OS_RE,
    ZERO_UUID,
    ABSENT_CHECKS,
    PARTIAL_CHECKS,
    CATEGORIES,
    FIELD_SCHEMA,
    STATUS_META,
    CSS,
    _deep_merge,
    _unwrap,
    _trunc,
    get_field,
    run_validator,
    format_report,
    encode_shot,
    esc,
    fmt_val,
    js_block,
)
import apr_xorenc  # noqa: F401  （單一解密入口；下方以 from 匯入具名函式）


# ════════════════════════════════════════════════════════════════════════════
# E2E TC 目錄、適用矩陣與自動判定
#   （原 e2e_catalog.py）
# ════════════════════════════════════════════════════════════════════════════

ALL_MODES = {"standalone", "admob-mediation", "applovin-mediation"}
ADMOB = {"admob-mediation"}
REEN = {"reen-static", "reen-dynamic"}

# ── 廣告流程步驟（E2E 分頁時間軸；每步一列，含逐步截圖）───────────────────────────
# (key, 顯示標題, 這步在做什麼)
FLOW_STEPS = [
    ("init",       "① SDK Init",        "App 啟動、init 請求送出"),
    ("bid",        "② Bid 請求 / 回應",  "送出 bid request、拿到廣告 response"),
    ("render",     "③ 廣告渲染",         "native 素材（icon/main/title/cta）顯示"),
    ("impression", "④ Impression 回報",  "曝光 beacon（show_cb → winshowimg）成對"),
    ("click",      "⑤ 點擊",            "點廣告手勢 → xclk 點擊鏈"),
    ("landing",    "⑥ 落地",            "deeplink 直開 target app / 落地頁"),
]
# 每條 E2E TC 歸到哪個流程步驟
# E2E TC → 廣告流程步驟（init/bid/render/impression/click/landing）。
STEP_OF = {}
# 各流程步驟對應的逐步截圖檔名（run_qa DO_E2E_FLOW 產出）
STEP_SHOT = {
    "init":    "e2e_step_init.png",
    "render":  "e2e_step_render.png",
    "click":   "e2e_step_click.png",
    "landing": "e2e_step_landing.png",
}

E2E_TCS = []

# 對外只有三種狀態：PASS / FAIL / BLOCKED（細分原因收進括號說明，不另立分類）
STATUS_LABEL = {
    "pass": "PASS",
    "observe": "PASS（部分證據）",
    "fail": "FAIL",
    "pending": "BLOCKED（未執行/證據不足）",
    "gated": "BLOCKED（需人工核准）",
    "na_mode": "BLOCKED（整合模式不適用）",
    "na_type": "BLOCKED（投放目的不適用）",
    "na_platform": "BLOCKED（平台不適用）",
    "backend": "BLOCKED（跨系統後端）",
}


# ── 證據載入 ──────────────────────────────────────────────────────────────────

def _load_captures(round_dir):
    """回傳 [(name, folder_path)]，新→舊排序。"""
    caps = []
    for results_path in glob.glob(os.path.join(round_dir, "*", "results.json")):
        folder = os.path.dirname(results_path)
        caps.append((os.path.basename(folder), folder))
    return sorted(caps, key=lambda item: item[0].split("_", 1)[-1], reverse=True)


def _traffic(folder):
    path = os.path.join(folder, "traffic.jsonl")
    rows = []
    if os.path.exists(path):
        for line in open(path):
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _json_file(folder, name):
    path = os.path.join(folder, name)
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            return None
    return None


def _text_file(folder, name):
    path = os.path.join(folder, name)
    return open(path, errors="replace").read() if os.path.exists(path) else ""


def _logcat(folder):
    """SDK logcat（logcat_appier.txt）。Appier 對 apx / 部分主機有 cert pinning，
    proxy 攔不到解密流量時，改用 SDK 自己印的 log 當一手證據。"""
    return _text_file(folder, "logcat_appier.txt") or _text_file(folder, "logcat.txt")


def _first_logcat(caps, pattern):
    """新→舊找出 logcat 命中 pattern 的第一個 capture；回 (name, folder, match) 或 (None,None,None)。"""
    rx = re.compile(pattern)
    for name, folder in caps:
        m = rx.search(_logcat(folder))
        if m:
            return name, folder, m
    return None, None, None


def _first_with(caps, predicate):
    """新→舊找出第一個滿足條件的 capture；回 (name, folder) 或 (None, None)。"""
    for name, folder in caps:
        if predicate(folder):
            return name, folder
    return None, None


def _urls_in(obj):
    """遞迴收集 JSON 內所有 http(s) URL 字串。"""
    urls = []
    if isinstance(obj, dict):
        for value in obj.values():
            urls += _urls_in(value)
    elif isinstance(obj, list):
        for value in obj:
            urls += _urls_in(value)
    elif isinstance(obj, str) and obj.startswith(("http://", "https://")):
        urls.append(obj)
    return urls


def _any_traffic(caps):
    return any(_traffic(folder) for _, folder in caps)


NO_TRAFFIC_NOTE = ("round 內沒有任何 proxy 流量 log（capture 走 logcat 偵測或 VPN 繞過 proxy）；"
                   "需在 mitmdump 在線且無 VPN 的 capture 驗證")


# ── 驗證器 ────────────────────────────────────────────────────────────────────



# native 素材必要欄位——render 類 E2E TC 的通過標準，重建時填回。
REQUIRED_NATIVE = []


def _bid_ad(folder):
    resp = _json_file(folder, "bid_response.json")
    if not resp:
        return None
    try:
        return resp["adUnits"][0]["ad"]
    except Exception:
        return {}






def _norm_text(s):
    """比對前正規化：解 HTML entity（&#10; 等）、全部空白摺成單一空格。"""
    import html as _html
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()






# 測試廣告環境，點擊直接執行（DO_E2E_FLOW=1 自動點）；沒跑到就是「本輪未執行」，非核准問題
CLICK_NOTRUN_NOTE = ("點擊流程本輪未執行；開 DO_E2E_FLOW=1 會自動點廣告 → "
                     "保存 xclk 點擊鏈與落地截圖")


def _find_xclk(caps):
    for name, folder in caps:
        for row in _traffic(folder):
            if "/xclk" in row.get("url", ""):
                return name, row
    return None, None


def _click_evidence(caps):
    """點擊有沒有真的發生（不靠 proxy）：do_e2e_flow 存的 e2e_flow.json clicked=true
    ＋落地截圖，或 logcat 印 "In-app browser initial loads url"（SDK 開落地頁）。
    回傳 (name, folder, landing_url|None) 或 (None,None,None)。"""
    for name, folder in caps:
        flow = _json_file(folder, "e2e_flow.json")
        clicked = bool(flow and flow.get("clicked"))
        has_landing = os.path.exists(os.path.join(folder, "e2e_step_landing.png"))
        m = re.search(r"In-app browser initial loads url:\s*(\S+)", _logcat(folder))
        if clicked or has_landing or m:
            return name, folder, (m.group(1) if m else None)
    return None, None, None












def _admob_traffic(caps, needle):
    for name, folder in caps:
        for row in _traffic(folder):
            if needle in row.get("url", ""):
                return name, row
    return None, None






# 為何某 TC 在特定模式不適用（跳過原因，寫進報告讓人一眼懂）
# 措辭與當前 test_mode 無關（standalone 或 applovin-mediation 都可能觸發 na_mode），
# 只說明「此步驟為 AdMob mediation 專屬」，別寫死「standalone」。
# E2E TC → 「為什麼這個整合模式不適用」的說明文字。
MODE_NA_REASON = {}


# ── 評估入口 ──────────────────────────────────────────────────────────────────



def summarize(round_dir=None, test_mode="standalone", test_type="reen-dynamic"):
    from collections import Counter
    W = 100
    print("=" * W)
    if round_dir:
        rows = evaluate(round_dir, test_mode, test_type)
        print(f"  Ad-Serving E2E — {len(rows)} TC（mode={test_mode} / type={test_type}）")
        print("=" * W)
        for row in rows:
            print(f"  {row['tc']:<6} [{row['priority']}] {STATUS_LABEL[row['status']]:<28} {row['name']}")
            print(f"         ↳ {row['note']}")
        counter = Counter(row["status"] for row in rows)
    else:
        print(f"  Ad-Serving E2E catalog — {len(E2E_TCS)} TC 定義（無 round 資料，僅列適用矩陣）")
        print("=" * W)
        counter = Counter()
        for tc in E2E_TCS:
            modes = "、".join(sorted(tc["modes"])) if tc["modes"] != ALL_MODES else "全部模式"
            auto = tc["auto"] or "backend"
            print(f"  {tc['tc']:<6} [{tc['priority']}] auto={auto:<18} 適用：{modes:<28} {tc['name']}")
    print("-" * W)
    if counter:
        # 收斂成三桶統計（PASS / FAIL / BLOCKED），不逐細分狀態列
        bucket = Counter()
        for key, count in counter.items():
            bucket[STATUS_LABEL.get(key, "BLOCKED").split("（")[0]] += count
        print("  " + " / ".join(f"{count} {label}"
                                for label, count in bucket.most_common()))
    print("=" * W)


# ════════════════════════════════════════════════════════════════════════════
# Signal validator（AND-xx 規則與檢查實作）
#   （原 bid_inspector.py）
# ════════════════════════════════════════════════════════════════════════════

# ── request wrapper unwrap ────────────────────────────────────────────────────



def normalize_bid(body):
    """把 `req_enc` 還原成明文 `req`，讓兩條路徑解析都能對上。

    2026-08 起 AOS SDK 不再送明文 `req`，改送同樣 ae1-XOR 加密的 `req_enc`
    （內容結構不變：{app, compliance, device}）。validator 有兩種解析根：
      * 預設根＝`_unwrap()`（req 與解密後 ext_enc 深合併）→ device.* / app.* / user.*
      * `root: "raw"` 的 validator 直接打原始 body → req.compliance.*（AND-77~80）
    兩者都需要 body 裡有明文 `req`；缺了就會整批讀成 None、變成假 FAIL
    （2026-08-03 實測：52 條假失敗，證據其實完好）。
    """
    if not isinstance(body, dict):
        return body
    if isinstance(body.get("req"), dict) or not body.get("req_enc"):
        return body
    from apr_xorenc import decode_bid          # 單一解密入口
    req = decode_bid(body).get("req")
    if not isinstance(req, dict):
        return body
    out = dict(body)
    out["req"] = req
    return out






# ── field path resolver ───────────────────────────────────────────────────────



# ── regex patterns ────────────────────────────────────────────────────────────

# Locale.toLanguageTag() can emit script/region subtags (zh-Hant-TW) or a bare
# language (en); input-method subtype locales may use underscores (en_US)


# ── validator dispatch ────────────────────────────────────────────────────────



# ── TC validator table (Android TCs tab) ──────────────────────────────────────

VALIDATORS = []


# ── report ────────────────────────────────────────────────────────────────────



def run_inspection(bid, tc_filter=None, reference_ms=None):
    bid = normalize_bid(bid)      # req_enc → req（見 normalize_bid）
    root = _unwrap(bid)
    decoded = _decode_ext_enc(bid)
    results = []
    for v in VALIDATORS:
        if tc_filter and v["tc"] not in tc_filter:
            continue
        if v["check"] == "session_case":
            # 跨 bid 對照（bid A vs bid B），單一 bid 無法判定：
            # 判定由 run_qa 於 capture 當下寫入 session_case.json，報告端讀該檔
            continue
        source = bid if v.get("root") == "raw" else root
        passed, actual, msg = run_validator(source, v, reference_ms=reference_ms)
        _, from_decrypted = get_field(decoded, v["field"]) if isinstance(decoded, dict) else (None, False)
        results.append({
            "tc":     v["tc"],
            "field":  v["field"],
            "passed": passed,
            "actual": actual,
            "msg":    msg,
            "note":   v.get("note", ""),
            "source": "ext_enc" if from_decrypted and v.get("root") != "raw" else "request",
        })
    return results




# ── round aggregation ─────────────────────────────────────────────────────────

def aggregate_round(round_dir):
    """Merge every capture's results.json in a round folder.

    Latest capture wins per (tc, field) — a targeted state capture (e.g.
    AND-04 darkmode-on) overrides the baseline capture's result for that
    check. Returns rows in VALIDATORS order, each with a "capture" key.
    """
    entries = {}
    for path in glob.glob(os.path.join(round_dir, "*", "results.json")):
        capture = os.path.basename(os.path.dirname(path))
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        ts = data.get("captured_at", "")
        for r in data.get("results", []):
            key = (r["tc"], r["field"])
            prev = entries.get(key)
            if prev is None or ts >= prev["_ts"]:
                entries[key] = {**r, "_ts": ts, "capture": capture}
    ordered = []
    for v in VALIDATORS:
        row = entries.get((v["tc"], v["field"]))
        if row is not None and row not in ordered:
            ordered.append(row)
    return ordered


def format_round_report(rows, round_name=""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 與平台（build_artifact.classify）採同一判定，避免 round_report 與平台 HTML 對同一
    # round 給互相矛盾的數字：清楚限制（RD 未實作／硬體不可得＝BLOCKED∪RD_GAP）恆判 BLOCK，
    # 不當 FAIL 也不當 PASS；其餘有 capture 就 PASS/FAIL。lazy import 破循環相依。
    try:
        limit_set = set(BLOCKED) | set(RD_GAP)
    except Exception:
        limit_set = set()

    def verdict(r):
        if r["tc"] in limit_set:
            return "BLOCK"
        return "PASS" if r["passed"] else "FAIL"

    W = 104
    lines = [
        "=" * W,
        f"  SSP SDK Round Report — {round_name}  —  generated {ts}",
        "  每條 check 取該 round 內最新一次 capture 的結果（判定與平台一致）",
        "=" * W,
        "",
        f"{'TC':<8}  {'Field':<32}  {'Actual':<24}  {'Result':<7}  Capture",
        f"{'─'*8}  {'─'*32}  {'─'*24}  {'─'*7}  {'─'*26}",
    ]
    passed = failed = blocked = 0
    label = {"PASS": "PASS ✓", "FAIL": "FAIL ✗", "BLOCK": "BLOCK ▪"}
    for r in rows:
        v = verdict(r)
        lines.append(
            f"{r['tc']:<8}  {r['field']:<32}  {_trunc(r['actual'], 22):<24}  {label[v]:<7}  {r['capture']}"
        )
        if v != "PASS" and r.get("note"):
            lines.append(f"{'':8}  ↳ {r['note']}")
        if v == "PASS":
            passed += 1
        elif v == "FAIL":
            failed += 1
        else:
            blocked += 1
    covered = {(r["tc"], r["field"]) for r in rows}
    missing_tcs = sorted({v["tc"] for v in VALIDATORS if (v["tc"], v["field"]) not in covered})
    lines += [
        "─" * W,
        f"  {passed} passed  /  {failed} failed  /  {blocked} blocked (RD/硬體限制)  /  {len(rows)} checked"
        f"  /  {len(missing_tcs)} TC not yet captured",
    ]
    if missing_tcs:
        lines.append(f"  not yet captured: {', '.join(missing_tcs)}")
    lines.append("=" * W)
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli_inspect(argv):
    """--inspect <bid.json> / --inspect-round <dir>：離線驗證，不碰實機。"""
    p = argparse.ArgumentParser(prog="qa_aos.py --inspect", description="離線驗證 bid request")
    p.add_argument("tc_ids", nargs="*", help="TC IDs（如 AND-04）；不給＝全部")
    p.add_argument("--inspect", dest="file", nargs="?", default="/tmp/appier_bid.json",
                   help="bid request JSON 檔（預設 /tmp/appier_bid.json）")
    p.add_argument("--out", help="報告另存到此路徑")
    p.add_argument("--inspect-round", dest="round",
                   help="round evidence 資料夾——重算所有 capture 並寫回 round_report.txt")
    args = p.parse_args(argv)

    if args.round:
        rows = aggregate_round(args.round)
        if not rows:
            sys.exit(f"no capture results.json found under {args.round}")
        report = format_round_report(rows, os.path.basename(args.round.rstrip("/")))
        print(report)
        out = os.path.join(args.round, "round_report.txt")
        with open(out, "w") as f:
            f.write(report + "\n")
        print(f"\n→ saved: {out}")
        return

    try:
        with open(args.file) as f:
            bid = json.load(f)
    except FileNotFoundError:
        sys.exit(f"bid file not found: {args.file}\n(run mitmdump + trigger app first)")
    except json.JSONDecodeError as e:
        sys.exit(f"invalid JSON in {args.file}: {e}")

    tc_filter = set(args.tc_ids) if args.tc_ids else None
    results   = run_inspection(bid, tc_filter)
    report    = format_report(results, args.file)

    print(report)

    if args.out:
        with open(args.out, "w") as f:
            f.write(report + "\n")
        print(f"\n→ saved: {args.out}")


# ════════════════════════════════════════════════════════════════════════════
# TC 目錄中繼資料、判定狀態機、單輪 HTML 報告
#   （原 build_artifact.py）
# ════════════════════════════════════════════════════════════════════════════

try:
    from PIL import Image
except Exception:
    Image = None



sys.path.insert(0, str(Path(__file__).parent))


# Golden Schema v8 (2026-07-09):
# https://appier.atlassian.net/wiki/spaces/AMT/pages/5215421112
#
# Cards deliberately keep schema facts separate from TC expectations.  The
# latter may reflect the current Android implementation (and can therefore use
# different units while an SDK/Swagger mismatch is under investigation).


# ── TC 分類 / 可驗度 / 狀態重現 metadata ────────────────────────────────────────


# TC → 分類字母（依 sheet Cat A–M）
CAT_OF = {}

# 狀態類 TC：group（互斥組）+ 如何設定 + 截圖該證明什麼
STATE = {}

# adb 可自動設定/模擬該狀態的指令（鏡像 device_state.py 實際流程），
# 寫進「應有值」卡片供人重現。battery 類設定會持續 → 附還原指令。
BATTERY_RESET = "adb shell dumpsys battery reset"
MOCK_CMD = {}
# 這些狀態靠 adb 設定會持續，capture 後要還原
MOCK_NEEDS_RESET = set()

# 環境/硬體限制無法執行
BLOCKED = {}

# SDK 尚未實作 → 值恆為 null/[]，FAIL 屬 RD gap 而非執行問題
RD_GAP = {}

# REEN ↔ GAID opt-out 互斥：opt-out 後 REEN campaign 不出價（204 no-bid），
# 抓不到指定 CID → opt-out 狀態 TC 在 REEN 輪標 N/A，改走 AIBID 輪驗
# 交給共用判定契約（verdict.classify / tier_of）的「清楚的限制」集合：
# RD 未實作（RD_GAP）＋硬體不可得（BLOCKED）。契約不偷讀這兩張表，一律用參數傳，
# 平台忘了傳時會拿到空集合、症狀明顯，不會靜默把 gap 判成產品 FAIL。
BLOCKED_ALL = frozenset(BLOCKED) | frozenset(RD_GAP)

# 以下三張表取代原本硬編在邏輯裡的 TC 編號，讓「哪些 TC 有特殊處理」變成可填的資料，
# 而不是散在程式碼裡。重建目錄時填這裡，不要再把 TC 編號寫進 if 判斷。
#   FIRST_BID_TCS  需用「冷啟第一發 bid」判定的 TC（原本硬編 {AND-48, AND-49}）
#   SESSION_TCS    session_duration 情境編號 → TC 編號（原本硬編 f"AND-47-{case}"）
#   TYPE_NA_REEN   REEN 輪不適用的 signal TC（原本 CTRL2 硬編扣掉 {AND-02, AND-76}）
FIRST_BID_TCS = set()
SESSION_TCS = {}

TYPE_NA_REEN = {}

# E2E 適用性與判定的權威在 e2e_catalog.py（每條 TC 帶 modes/types，evaluate() 依
# 所選 TEST_MODE/TEST_TYPE 自動判 na_mode / na_type → BLOCKED）。這裡不再放靜態
# 清單，避免像舊版 E2E_CASES 那樣硬編「本輪為 Standalone」與 e2e_catalog 牴觸。

# 可驗度分級




# 互斥組 → [(tc, expected), ...]，供狀態互斥檢查（capture_state_eligible）
def build_groups():
    groups = {}
    exp = {}
    for v in VALIDATORS:
        exp.setdefault(v["tc"], v.get("expected"))
    for tc, (grp, _, _) in STATE.items():
        groups.setdefault(grp, []).append(tc)
    return groups, exp


# ── evidence 掃描 ───────────────────────────────────────────────────────────────

def load_captures(round_dir):
    """回傳 {capture_name: {"bid": obj, "shot_b64": str|None, "ts": str}}。"""
    caps = {}
    for results_path in glob.glob(os.path.join(round_dir, "*", "results.json")):
        folder = os.path.dirname(results_path)
        name = os.path.basename(folder)
        bid = None
        bid_path = os.path.join(folder, "bid_request.json")
        if os.path.exists(bid_path):
            try:
                bid = json.load(open(bid_path))
            except Exception:
                bid = None
        first_bid = None
        first_bid_path = os.path.join(folder, "first_bid_request.json")
        if os.path.exists(first_bid_path):
            try:
                first_bid = json.load(open(first_bid_path))
            except Exception:
                first_bid = None
        shot_path = os.path.join(folder, "phone.png")
        shot_path = shot_path if os.path.exists(shot_path) else None
        # state-proof：看得見該狀態的系統畫面（肉眼證據），優先於 app 截圖
        proof_paths = {}
        proof_caps = {}
        for p in glob.glob(os.path.join(folder, "state_proof_*.png")):
            group = os.path.basename(p)[len("state_proof_"):-len(".png")]
            proof_paths[group] = p
        # 向後相容舊 evidence 的單張 state_proof.png。
        legacy_proof = os.path.join(folder, "state_proof.png")
        if os.path.exists(legacy_proof):
            proof_paths["legacy"] = legacy_proof
        caps_path = os.path.join(folder, "state_proof_captions.json")
        if os.path.exists(caps_path):
            try:
                proof_caps = json.load(open(caps_path))
            except Exception:
                proof_caps = {}
        cap_path = os.path.join(folder, "state_proof_caption.txt")
        if os.path.exists(cap_path):
            proof_caps["legacy"] = open(cap_path).read().strip()
        # 本次實際執行了什麼（實機設定 / adb 模擬 real→mock）
        action = None
        act_path = os.path.join(folder, "state_action.txt")
        if os.path.exists(act_path):
            action = open(act_path).read().strip()
        # 本次 bid 識別碼（比廣告截圖有意義）
        bid_ids = None
        ids_path = os.path.join(folder, "bid_ids.json")
        if os.path.exists(ids_path):
            try:
                bid_ids = json.load(open(ids_path))
            except Exception:
                bid_ids = None
        # session 三情境對照（run_qa SESSION_CASE 產出；AND-47-1/2/3）
        session_case = None
        sc_path = os.path.join(folder, "session_case.json")
        if os.path.exists(sc_path):
            try:
                session_case = json.load(open(sc_path))
            except Exception:
                session_case = None
        ts = ""
        stored = {}
        executed_tcs = set()
        test_type = ""
        test_mode = ""
        test_cid = ""
        test_executor = ""
        environment = {}
        try:
            data = json.load(open(results_path))
            tc_id = data.get("tc_id", "")
            executed_tcs = {item.strip() for item in tc_id.split(",") if item.strip()}
            ts = data.get("captured_at", "")
            test_type = data.get("test_type", "")
            test_mode = data.get("test_mode", "")
            test_cid = data.get("test_cid", "")
            test_executor = data.get("test_executor", "")
            environment = data.get("environment", {})
            # capture 當下算的結果才是權威（時間敏感的 check 事後重算會失真）
            stored = {(r["tc"], r["field"]): r for r in data.get("results", [])}
        except Exception:
            pass
        environment_path = os.path.join(folder, "environment.json")
        if os.path.exists(environment_path):
            try:
                environment.update(json.load(open(environment_path)))
            except Exception:
                pass
        # 時間敏感 check（AND-48/49 recency）要用「capture 當下」的時間，不是檔案 mtime
        # （搬檔／複製 evidence 會把 mtime 重設成現在 → recency delta 失真）。
        # 優先用 results.json 記的 captured_at；解析失敗才退回 bid 檔 mtime。
        captured_at_ms = None
        if ts:
            try:
                captured_at_ms = datetime.fromisoformat(ts).timestamp() * 1000
            except (ValueError, TypeError):
                captured_at_ms = None
        if captured_at_ms is None and os.path.exists(bid_path):
            captured_at_ms = os.path.getmtime(bid_path) * 1000
        caps[name] = {"bid": bid, "first_bid": first_bid,
                      "shot_path": shot_path, "ts": ts,
                      "captured_at_ms": captured_at_ms,
                      "folder": name, "stored": stored,
                      "proof_paths": proof_paths, "proof_caps": proof_caps,
                      "action": action, "bid_ids": bid_ids, "session_case": session_case,
                      "test_type": test_type,
                      "test_mode": test_mode, "test_cid": test_cid}
        caps[name]["executed_tcs"] = executed_tcs
        caps[name]["test_executor"] = test_executor
        caps[name]["environment"] = environment
    return caps


CTRL1_TCS = set()
CTRL2_TCS = set()
CTRL3_TCS = set()
# 只收「不隨裝置狀態改變」的欄位。AND-01（device.ia 需合法 UUID）與 AND-75
# （device.lat 需為 0）假設 GAID opt-in；CURRENT 不控制狀態 → 移出，只在 CTRL1 驗。
CURRENT_TCS = set()


# 批次名同時是 evidence 資料夾名，報告端靠前綴比對找 capture，所以必須認得歷史名稱，
# 否則舊 round 會對不上、那些 TC 全被誤判 BLOCKED。新產出只寫新名。
#   CTRL1/2/3 ← R1/R2/R3（2026-08-04）← M1/M2/M3
#   CURRENT   ← AUTO（2026-08-04）← BASELINE / baseline_（2026-07）
#   SD        ← SC（SC 從來不是資料夾前綴；它的 capture 按 TC 命名 AND-47-x_）
LEGACY_BATCH = {
    "CTRL1":   ("R" + "1", "M" + "1"),
    "CTRL2":   ("R" + "2", "M" + "2"),
    "CTRL3":   ("R" + "3", "M" + "3"),
    "CURRENT": ("A" + "UTO", "baseline"),
}


def batch_prefixes(label):
    """該批次可接受的資料夾前綴：新名 ＋ 歷史名 ＋ `baseline_`。

    `baseline_` 對**所有** label 都接受：舊的 baseline-only round 只有一個
    baseline_<ts> capture，狀態類 TC 也要能對到它（能不能算由 declared() 與
    capture_state_eligible() 把關）。漏了這條，那些舊 round 的 TC 會少判。
    """
    names = [label, *LEGACY_BATCH.get(label, ()), "baseline"]
    return tuple(f"{n}_" for n in dict.fromkeys(names))


def expected_capture_label(tc):
    if tc in CTRL1_TCS:
        return "CTRL1"
    if tc in CTRL2_TCS:
        return "CTRL2"
    if tc in CTRL3_TCS:
        return "CTRL3"
    return "CURRENT" if tc in CURRENT_TCS else None


def capture_state_eligible(tc, cap):
    """Gate a Capture with independent device ground truth before bid comparison."""
    env = cap.get("environment", {})
    root = str(env.get("root", "")).lower()
    fingerprint = str(env.get("build_fingerprint", "")).lower()
    battery = str(env.get("battery", "")).lower()
    proofs = cap.get("proof_paths", {})
    # 每個 TC 的「裝置是否真的處於該狀態」斷言——這是該 TC 能不能通過的前置標準。
    # 從 0 重建：key = TC 編號，value = 一個回傳 bool 的 lambda，可用的上下文變數有
    #   env（device_state 快照）／root／fingerprint／battery／proofs（狀態截圖路徑）
    # 沒有登記的 TC 一律回 True（不設前置門檻），所以空表＝完全不 gate。
    checks = {}
    check = checks.get(tc)
    if check is None:
        return True
    try:
        return bool(check())
    except (TypeError, ValueError):
        return False


def pick_capture(tc, caps):
    """Pick the latest capture that actually established this TC's state."""
    matches = capture_candidates(tc, caps)
    return matches[-1] if matches else None


def capture_candidates(tc, caps):
    """All phase-matched attempts, including *_RETRYn Captures."""
    def declared(name):
        declared_tcs = caps[name].get("executed_tcs", set())
        # CURRENT 那一批的歷史宣告值：AUTO（2026-08-03~04）、BASELINE（2026-07），
        # 一併認，否則舊 round 的 CURRENT_TCS 會全被誤判 BLOCKED。
        return tc in declared_tcs or (
            bool(declared_tcs & {"CURRENT", "A" + "UTO", "BASELINE"})
            and tc in CURRENT_TCS)

    # 逗號多選會存成 `AND-04+AND-05_<ts>`（run_qa.py：TC_ID.replace(",", "+")），
    # startswith("AND-04_") 對不上（下個字是 `+`）；改用 folder results.json 宣告的
    # executed_tcs 直接認（該資料夾確實跑了這條 tc），不再只靠檔名前綴。
    def name_matches(n):
        return (n.startswith(tc + "_")
                or n.startswith(tc.replace("-", "") + "_")
                or tc in caps[n].get("executed_tcs", set()))

    matches = sorted(n for n in caps
                     if name_matches(n)
                     and declared(n)
                     and capture_state_eligible(tc, caps[n]))
    if matches:
        return matches
    label = expected_capture_label(tc)
    if label is None:
        return []
    # 新名 + 舊名都收（見 LEGACY_BATCH）：只認單一前綴會讓舊 round 一條都對不上、
    # 全被誤判 BLOCKED。非該批次的 tc declared() 仍為 False，不會誤收。
    return sorted(n for n in caps
                  if n.startswith(batch_prefixes(label))
                  and declared(n)
                  and capture_state_eligible(tc, caps[n]))


# ── 判定 ─────────────────────────────────────────────────────────────────────────



# ── HTML ─────────────────────────────────────────────────────────────────────────







def device_kind_of(environment):
    """實體機 / 模擬機。優先讀 environment.device_kind（新 capture 已存）；
    舊 capture 沒有此欄位時，從 build_fingerprint / device 型號回推。"""
    kind = (environment or {}).get("device_kind")
    if kind in ("實體機", "模擬機"):
        return kind
    fp = str((environment or {}).get("build_fingerprint", "")).lower()
    model = str((environment or {}).get("device", "")).lower()
    is_emu = ("sdk_gphone" in model
              or any(tok in fp for tok in ("emu", "sdk_gphone", "/generic")))
    return "模擬機" if is_emu else "實體機"


def provenance_label(environment):
    """把裝置類型與狀態來源合成一個 SOURCE 標籤：
    實體機(REAL) / 實體機(MOCK) / 模擬機(REAL) / 模擬機(MOCK)。"""
    kind = device_kind_of(environment)
    raw = str((environment or {}).get("battery_source", "")).upper()
    src = "MOCK" if "MOCK" in raw else "REAL"
    return f"{kind}({src})"


def ground_truth_for(field, environment):
    """Return the independent device/app evidence saved beside this Capture."""
    mapping = {
        "app.bundle": ("environment.json · package", "package"),
        "app.ver": ("environment.json · version_name", "version_name"),
        "device.model": ("environment.json · device", "device"),
        "device.osv": ("environment.json · android", "android"),
        "device.langb": ("environment.json · app_locale", "app_locale"),
        "device.ext.darkmode": ("environment.json · dark_mode", "dark_mode"),
        "device.ext.battery_saver": ("environment.json · battery_saver", "battery_saver"),
        "device.ext.screen_bright": ("environment.json · brightness (0–255)", "brightness"),
        "device.ext.fontscale": ("environment.json · font_scale", "font_scale"),
        "device.charging": ("environment.json · battery", "battery"),
        "device.batterylevel": ("environment.json · battery", "battery"),
        "device.ext.jailbreak": ("environment.json · root", "root"),
        "device.utcoffset": ("environment.json · timezone", "timezone"),
    }
    item = mapping.get(field)
    if not item:
        return None
    label, key = item
    value = environment.get(key)
    if field in {"device.charging", "device.batterylevel"} and value not in (None, ""):
        value = f"{value} | source={environment.get('battery_source', 'UNKNOWN')}"
    return {"label": label, "value": value} if value not in (None, "") else None


def read_round_elapsed(round_dir):
    """讀 round_timing.txt（run_qa 每次跑完累寫一行）→ 回總耗時字串。
    多行取最後一次成功（exit=0）的；無檔或無成功行回 None。"""
    path = os.path.join(round_dir, "round_timing.txt")
    if not os.path.exists(path):
        return None
    lines = [l.strip() for l in open(path) if l.strip()]
    ok = [l for l in lines if "exit=0" in l]
    pick = ok[-1] if ok else (lines[-1] if lines else None)
    if not pick:
        return None
    # 格式：<date> <time>  <TC>  <XmYYs>  exit=N
    parts = pick.split()
    for tok in parts:
        if tok.endswith("s") and ("m" in tok or tok[:-1].isdigit()):
            return tok
    return None


# flow 段落順序（與 e2e_catalog.FLOW_STEPS 對齊）；判「跑到哪一段 / 卡在哪」
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


def build(round_dir, out_path, e2e_round=None):
    caps = load_captures(round_dir)
    if not caps:
        sys.exit(f"no capture (results.json) found under {round_dir}")
    groups, exp = build_groups()
    round_name = os.path.basename(round_dir.rstrip("/"))
    # test_type 在組卡片前就要知道：REEN 輪的 opt-out TC（TYPE_NA_REEN）要標 N/A
    test_type = next((c["test_type"] for c in caps.values()
                      if c.get("test_type") and c["test_type"] != "unspecified"), "")

    # Always recompute from bid_request.json using the current approved rules.
    # Stored results.json may have been produced by an older, incorrect validator.
    cap_results = {}
    first_cap_results = {}
    for name, c in caps.items():
        if c["bid"] is not None:
            cap_results[name] = {(r["tc"], r["field"]): r
                                 for r in run_inspection(
                                     c["bid"], reference_ms=c.get("captured_at_ms"))}
        if c.get("first_bid") is not None:
            first_cap_results[name] = {(r["tc"], r["field"]): r
                                       for r in run_inspection(
                                           c["first_bid"], reference_ms=c.get("captured_at_ms"))}

    # session 三情境（AND-47-1/2/3）：跨 bid 對照，單一 bid 驗不了 →
    # 從 capture 的 session_case.json（run_qa 當下記錄的 A/B 值）合成結果列
    for name, c in caps.items():
        sc = c.get("session_case")
        if not sc:
            continue
        tc_id = sc.get("tc") or SESSION_TCS.get(str(sc.get("case")))
        a, b = sc.get("session_a"), sc.get("session_b")
        verdict = sc.get("passed")
        msg = f"預期 {sc.get('expected', '')}"
        if verdict is None:
            msg += "；session 值缺失（A/B 其一未取得），無法對照"
        cap_results.setdefault(name, {})[(tc_id, "user.session_duration")] = {
            "tc": tc_id, "field": "user.session_duration",
            "passed": bool(verdict),
            "actual": f"A={a} → B={b} ms",
            "msg": msg,
            "note": sc.get("action", ""),
        }

    cards = []
    counts = {"PASS": 0, "FAIL": 0, "BLOCKED": 0}
    for v in VALIDATORS:
        tc, field, check = v["tc"], v["field"], v["check"]
        cat = CAT_OF.get(tc, "D")
        cap_name = pick_capture(tc, caps)
        expected_label = expected_capture_label(tc)
        targeted = bool(cap_name and expected_label and cap_name.startswith(expected_label + "_"))
        # first_bid_request.json 若存在可提供更精確的 cold-start 證據；若不存在，
        # 不得把已存在的 regular Capture 隱藏成 Pending，仍以實際收到值判 Pass/Fail。
        result_source = (first_cap_results
                         if tc in FIRST_BID_TCS and cap_name in first_cap_results
                         else cap_results)
        res = result_source.get(cap_name, {}).get((tc, field))
        passed = res["passed"] if res else False
        actual = res["actual"] if res else None
        attempts = []
        for attempt_name in capture_candidates(tc, caps):
            attempt_source = (first_cap_results
                              if tc in FIRST_BID_TCS and attempt_name in first_cap_results
                              else cap_results)
            attempt_result = attempt_source.get(attempt_name, {}).get((tc, field))
            if attempt_result is not None:
                attempts.append({"capture": attempt_name,
                                 "passed": attempt_result["passed"],
                                 "actual": fmt_val(attempt_result["actual"]),
                                 "msg": attempt_result["msg"]})
        failed_attempts = sum(not item["passed"] for item in attempts)
        status, rd_note = classify(tc, res is not None, passed, BLOCKED_ALL)
        counts[status] += 1
        # REEN 輪的 opt-out TC：無 capture 不是缺證據，是投放目的互斥 → 標 N/A
        # （計數仍歸 Blocked tile，與 E2E 的 na_* 處理一致）
        type_na_reason = (TYPE_NA_REEN.get(tc)
                          if (status == "BLOCKED" and res is None
                              and (test_type or "").startswith("reen"))
                          else None)

        expected = v.get("expected", None)
        if "pattern" in v:
            pattern_labels = {
                UUID_RE.pattern: "Valid UUID (8-4-4-4-12)",
                BCP47_RE.pattern: "Valid BCP 47 language tag",
                INPUT_LANG_RE.pattern: "Valid language / locale code",
                ISO639_RE.pattern: "ISO 639-1 lowercase code",
                CELL_4G5G_RE.pattern: "cellular_4g or cellular_5g",
                SEMVER_RE.pattern: "Semantic version (x.y.z)",
                ANDROID_OS_RE.pattern: "Android",
            }
            expected_disp = pattern_labels.get(v["pattern"].pattern, "Matches required format")
        elif "min" in v:
            expected_disp = f"{v['min']} … {v['max']}"
        elif check == "equals_field" and res is not None:
            expected_disp = res["msg"].removeprefix("expected ")
        elif check in ABSENT_CHECKS:
            expected_disp = "absent / empty"
        elif expected is not None:
            expected_disp = fmt_val(expected)
        else:
            expected_disp = check.replace("_", " ")

        st = STATE.get(tc)
        # 「應有值＝absent/empty」的負向互斥卡：寫清楚「為什麼預期沒值」，
        # 否則正反例只看到 absent 會困惑（值是被哪個狀態關掉的、截圖看哪裡）
        absent_reason = None
        if check in ABSENT_CHECKS and st:
            absent_reason = {"set": st[1], "shows": st[2]}
        signal, schema_type, schema_format, schema_note = FIELD_SCHEMA.get(
            field, (field, "—", "Golden Schema 未列", "")
        )
        actual_disp = fmt_val(actual) if res else "—"
        if status == "PASS":
            evidence_explanation = (
                f"同一個 Capture 的 bid_request.json 中，{field} = {actual_disp}；"
                f"符合本 TC 預期 {expected_disp}，因此判定 Pass。"
            )
        elif status == "FAIL":
            evidence_explanation = (
                f"同一個 Capture 的 bid_request.json 中，{field} = {actual_disp}；"
                f"不符合本 TC 預期 {expected_disp}，因此判定 Fail。"
            )
        else:
            if tc in BLOCKED or tc in RD_GAP:
                reason = BLOCKED.get(tc) or RD_GAP.get(tc)
                got = (f"（本輪 Capture 讀到 {field} = {actual_disp}，屬 RD/硬體 gap，不判產品 Fail）"
                       if res is not None else "")
                evidence_explanation = (
                    f"清楚的限制（本輪 RD 沒做或硬體不可得）：{reason}{got}。因此判定 Blocked。"
                )
            elif type_na_reason:
                evidence_explanation = (
                    f"本輪 TEST_TYPE={test_type}；{type_na_reason}。"
                    "非缺證據，本輪判定 N/A。"
                )
            else:
                evidence_explanation = (
                    "本輪未執行此 TC：沒佈到它要求的狀態／沒跑該情境，"
                    "因此沒有可比對的 Capture（非缺證據，是這輪沒做）→ Blocked。"
                )
        cap = caps.get(cap_name, {})
        proof_group = st[0] if st else None
        proof_paths = cap.get("proof_paths", {})
        proof_key = proof_group if proof_group in proof_paths else (
            "legacy" if "legacy" in proof_paths else None
        )
        has_proof = bool(proof_key)
        if has_proof:
            # 只保留「看得見狀態」的設定頁截圖；廣告畫面 phone.png 不再當證據（改用 bidobjid）
            shot_key = cap_name + "::proof::" + proof_key
            shot_caption = cap.get("proof_caps", {}).get(proof_key) or "狀態證據截圖"
            shot_matched = True
        else:
            shot_key = None
            shot_caption = None
            shot_matched = False
        cards.append({
            "tc": tc, "field": field, "cat": cat,
            "round": round_name,
            "signal": signal, "schema_type": schema_type,
            "schema_format": schema_format, "schema_note": schema_note,
            "tier": tier_of(check, tc, BLOCKED_ALL),
            "status": status, "status_cls": STATUS_META[status][0],
            "status_label": ("N/A（投放目的不適用）" if type_na_reason
                             else STATUS_META[status][1]),
            "type_na": bool(type_na_reason),
            "condition": v.get("note", "") or f"{field} — {check}",
            "expected": expected_disp,
            "actual": actual_disp,
            "evidence_explanation": evidence_explanation,
            "rd_note": rd_note,
            "blocked_reason": ((type_na_reason or BLOCKED.get(tc) or RD_GAP.get(tc) or
                                "本輪未執行：沒佈該狀態／沒跑該情境（非缺證據，這輪沒做）")
                               if status == "BLOCKED" else None),
            "set": st[1] if st else None,
            "shows": st[2] if st else None,
            "absent_reason": absent_reason,
            "mock_cmd": MOCK_CMD.get(tc),
            "mock_reset": tc in MOCK_NEEDS_RESET,
            "action": cap.get("action"),
            "bid_ids": cap.get("bid_ids"),
            "shot": shot_key,
            "shot_caption": shot_caption,
            "shot_matched": shot_matched,
            "capture": cap_name,
            # SOURCE 標籤：一律揭露來源裝置（實體機／模擬機）；
            # 可 adb-mock 的狀態（電量/充電）再補 (REAL/MOCK)
            "provenance": (
                provenance_label(cap.get("environment", {}))
                if field in {"device.batterylevel", "device.charging"}
                else (device_kind_of(cap["environment"])
                      if cap.get("environment") else None)),
            "ground_truth": ground_truth_for(field, cap.get("environment", {})),
            "attempts": attempts,
        })

    # Header counts are unique TCs, not validator rows. Multi-field TCs such as
    # geo lat/lon remain separate assertions inside one TC status.
    # FAIL 必須蓋過 BLOCKED：同一 TC 若一個欄位 FAIL、一個 BLOCKED，整條算 FAIL
    # （不准把真 FAIL 藏成 block）。
    precedence = {"PASS": 0, "BLOCKED": 1, "FAIL": 2}
    tc_status = {}
    for card in cards:
        old = tc_status.get(card["tc"])
        if old is None or precedence[card["status"]] > precedence[old]:
            tc_status[card["tc"]] = card["status"]
    counts = {key: sum(1 for value in tc_status.values() if value == key)
              for key in counts}
    total = len(tc_status)
    verified = counts["PASS"] + counts["FAIL"]
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 跨 capture 一致性：同一顆裝置每次 capture 的 ia / ifv 應恆定
    consistency = []
    latest_caps = []
    # 含舊名（CTRL1/CTRL2/CTRL3/baseline）：漏了會讓舊 round 的一致性面板顯示
    # 「0 種值 / 0 次」✗（其實有值）。
    for label in ("CURRENT", "CTRL1", "CTRL2", "CTRL3"):
        names = sorted(name for name in caps if name.startswith(batch_prefixes(label)))
        if names:
            latest_caps.append(caps[names[-1]])
    for label, field in (("GAID (device.ia)", "ia"), ("App Set ID (device.ifv)", "ifv")):
        vals = []
        for c in latest_caps:
            if c["bid"]:
                v = _unwrap(c["bid"]).get("device", {}).get(field)
                if v:
                    vals.append(v)
        distinct = sorted(set(vals))
        ok = len(distinct) == 1 and len(vals) > 1
        consistency.append({
            "label": label, "n": len(vals), "distinct": len(distinct),
            "ok": ok, "value": distinct[0] if distinct else "—",
        })

    # 截圖 data URI：狀態證據＝「該狀態的設定頁、切換後」的截圖（state_proof_<group>.png）
    shots_js = {}
    for name, c in caps.items():
        for group, proof_path in c.get("proof_paths", {}).items():
            shots_js[name + "::proof::" + group] = encode_shot(proof_path)
    latest_auto = sorted(name for name in caps
                         if name.startswith("AUTO_") or name.startswith("baseline_"))
    e2e_ad_shot = None
    if latest_auto:
        auto_cap = caps[latest_auto[-1]]
        if auto_cap.get("shot_path") and os.path.exists(auto_cap["shot_path"]):
            e2e_ad_shot = "e2e::ad-render"
            shots_js[e2e_ad_shot] = encode_shot(auto_cap["shot_path"])

    # 圖片直接寫入各 card 的 <img src="data:...">；不依賴 JS lazy-load。
    for card in cards:
        card["shot_data"] = shots_js.get(card.get("shot"))

    # 分類分組
    by_cat = {}
    for c in cards:
        by_cat.setdefault(c["cat"], []).append(c)

    # 從任一 capture 的 bid 取裝置型號
    model = "Android"
    for c in caps.values():
        if c["bid"]:
            dev = _unwrap(c["bid"]).get("device", {})
            model = dev.get("model") or model
            break
    # 本輪測試類型（aibid / reen-static / reen-dynamic）
    test_mode = next((c["test_mode"] for c in caps.values()
                      if c.get("test_mode") and c["test_mode"] != "unspecified"), "")
    test_cid = next((c["test_cid"] for c in caps.values() if c.get("test_cid")), "")
    test_executor = next((c["test_executor"] for c in caps.values()
                          if c.get("test_executor")), "")
    environment = next((c["environment"] for c in caps.values()
                        if c.get("environment")), {})

    # E2E：用「當前」的 e2e_catalog validators 重新評估（跟 signal 一樣即時重算）。
    # e2e_round 指定時，E2E 從「另一個 round」評估（讓完整報告的 E2E 來自專跑 flow 的 round，
    # signal 仍來自本 round）→ 一份報告涵蓋兩者。
    e2e_src = e2e_round or round_dir
    e2e_data = None
    if test_type and test_mode:
        try:
            e2e_data = evaluate(e2e_src, test_mode, test_type)
        except Exception:
            e2e_data = None
    if e2e_data is None:
        e2e_path = os.path.join(e2e_src, "e2e_results.json")
        if os.path.exists(e2e_path):
            try:
                e2e_data = json.load(open(e2e_path)).get("results")
            except Exception:
                e2e_data = None
    if e2e_data is None:
        # 最後 fallback：連 e2e_catalog.evaluate 都拿不到（本輪未帶 TEST_MODE/TEST_TYPE
        # 或評估丟例外）→ 用 catalog TC 清單標 pending，不臆測 mode（不再硬編 standalone）。
        try:
            e2e_data = [{"tc": t["tc"], "name": t["name"], "priority": t["priority"],
                         "check_kind": t.get("check_kind", ""), "expected": t.get("expected", ""),
                         "endpoint": t.get("endpoint", ""), "status": "pending",
                         "note": "尚未評估 E2E（本輪未帶 TEST_MODE/TEST_TYPE 或評估失敗）；"
                                 "帶齊 mode/type 重跑即依適用矩陣自動判定 na_mode / na_type",
                         "evidence": [], "step": "", "step_shot": None}
                        for t in E2E_TCS]
        except Exception:
            e2e_data = []

    # 逐步截圖：把 e2e_step_*.png（step_shot 相對路徑）編成 data URI 掛到每列，
    # 供 E2E 分頁時間軸顯示；還沒重跑產圖時 step_shot 為 None，時間軸標「待截圖」
    for row in e2e_data:
        rel = row.get("step_shot")
        if rel:
            full = os.path.join(e2e_src, rel)
            if os.path.exists(full):
                row["step_shot_uri"] = encode_shot(full)

    elapsed = read_round_elapsed(round_dir)
    progress = compute_round_progress(e2e_data)
    html_out = render_html(round_name, generated, counts, total, verified,
                            by_cat, shots_js, model, consistency, test_type, test_mode, test_cid,
                            test_executor, environment, e2e_ad_shot, e2e_data, elapsed, progress)
    Path(out_path).write_text(html_out, encoding="utf-8")
    print(f"→ {out_path}")
    # E2E 三態計分（跟 E2E 分頁 scorecard 同一套規則）；stdout 摘要與報告必須同口徑，
    # 否則終端看到的 pass 數會跟 HTML 裡的 scorecard 打架（曾有 4 vs 7 的落差）。
    e2e_score = {k: 0 for k in E2E_SCORE_ORDER}
    for row in e2e_data:
        e2e_score[E2E_SCORE.get(row["status"], "BLOCKED")] += 1
    print(f"  {total + len(e2e_data)} TCs: "
          f"{counts['PASS'] + e2e_score['PASS']} pass / "
          f"{counts['FAIL'] + e2e_score['FAILED']} fail / "
          f"{counts['BLOCKED'] + e2e_score['BLOCKED']} blocked "
          f"({total} Signal / {len(e2e_data)} E2E)")
    return {
        "out": out_path,
        "round_name": round_name,
        "test_type": test_type,
        "test_mode": test_mode,
        "test_cid": test_cid,
        "test_executor": test_executor,
        "model": model,
        "elapsed": elapsed,
        "signal_total": total,
        "signal_counts": dict(counts),
        "e2e_total": len(e2e_data),
        "e2e_score": e2e_score,
    }


# E2E 狀態統一成跟 Signal 一樣的三種：PASS / FAILED / BLOCKED（詳細原因留在說明文字）
# observe（有截圖佐證）算 PASS；na/backend/未執行 都歸 BLOCKED（暫時無法自動判定，說明會寫原因）
E2E_SCORE = {"pass": "PASS", "observe": "PASS", "fail": "FAILED",
             "pending": "BLOCKED", "backend": "BLOCKED", "gated": "BLOCKED",
             "na_mode": "BLOCKED", "na_type": "BLOCKED", "na_platform": "BLOCKED"}
E2E_SCORE_ORDER = ["PASS", "FAILED", "BLOCKED"]
E2E_SCORE_CLS = {"PASS": "pass", "FAILED": "fail", "BLOCKED": "blocked"}
# E2E 狀態 → 徽章色 class（三種）
E2E_STATUS_CLS = {"pass": "pass", "observe": "pass", "fail": "fail",
                  "pending": "blocked", "backend": "blocked", "gated": "blocked",
                  "na_mode": "blocked", "na_type": "blocked", "na_platform": "blocked"}
# 徽章文字也統一三種
E2E_BADGE_LABEL = {"pass": "PASS", "observe": "PASS", "fail": "FAILED",
                   "pending": "BLOCKED", "backend": "BLOCKED", "gated": "BLOCKED",
                   "na_mode": "BLOCKED", "na_type": "BLOCKED", "na_platform": "BLOCKED"}


def render_e2e_pane(e2e_data, test_mode, test_type):
    """E2E 分頁（骨架）：scorecard ＋ 依 FLOW_STEPS 排的流程時間軸，每步一列含逐步截圖。

    重建時的口徑硬規則：scorecard 一律用 `E2E_SCORE` 這套映射算
    （observe→PASS；pending/backend/gated/na_* → BLOCKED），而且**必須跟 build()
    印到 stdout 的摘要同一套** —— 曾經兩套並存，終端印 E2E 4 pass、報告寫 7 pass。

    原版版面：`git show 8307b56:qa_aos.py`
    """
    # 舊 e2e_results.json 沒有 step 欄位 → 用 STEP_OF 補上（不必等重跑）
    for r in e2e_data:
        if not r.get("step"):
            r["step"] = STEP_OF.get(r["tc"], "")
    score = {k: 0 for k in E2E_SCORE_ORDER}
    for r in e2e_data:
        score[E2E_SCORE.get(r["status"], "BLOCKED")] += 1
    tiles = "".join(
        f'<div class="e2e-tile e2e-t-{E2E_SCORE_CLS[k]}">'
        f'<span class="e2e-tile-n">{score[k]}</span><span class="e2e-tile-l">{esc(k)}</span></div>'
        for k in E2E_SCORE_ORDER if score[k])
    rows = "".join(
        f'<div class="e2e-row"><span class="e2e-tc">{esc(r["tc"])}</span>'
        f'<b>{esc(r.get("name", ""))}</b>'
        f'<span class="e2e-badge e2e-b-{E2E_STATUS_CLS.get(r["status"], "blocked")}">'
        f'{esc(E2E_BADGE_LABEL.get(r["status"], "BLOCKED"))}</span></div>'
        for r in e2e_data)
    return (f'<div class="e2e-scorecard">{tiles}</div>'
            f'<div class="e2e-timeline">{rows}</div>'
            f'<!-- TODO(版面): 依 FLOW_STEPS 分段、逐步截圖、應有值/實際並排、endpoint -->')


def render_html(round_name, generated, counts, total, verified, by_cat, shots_js, model,
                consistency, test_type="", test_mode="", test_cid="", test_executor="",
                environment=None, e2e_ad_shot=None, e2e_data=None, elapsed=None, progress=None):
    """單輪 AOS 報告 HTML（骨架）。**簽名固定** —— build() 以位置引數呼叫。

    版面契約（重建時該有的區塊，原版：`git show 8307b56:qa_aos.py`）：

      header    round / 類型 / 整合模式 / Test CID / 執行人 / device / Signal+E2E 數
                / 整體耗時 / generated，外加「本輪流程」banner
                （compute_round_progress 的 complete / stall：跑完＝綠、卡關＝琥珀並指出卡在哪段）
      分頁      Signal / E2E 兩個 .tab-pane，分頁鈕各帶數字
      tiles     Signal 的 Pass / Fail / Blocked / All。**只算 Signal**（E2E 有自己的
                scorecard），且以 TC 去重計數，不是 validator 列數
      面板      測試環境·APK ／ Capture 前置狀態 ／ 跨 capture 一致性（consistency）
      未完成    Blocked 面板要列出**全部** Blocked TC、數字與 tile 一致，並分三類：
                  RD/硬體限制（BLOCKED ∪ RD_GAP）· 本輪未執行 · 投放目的不適用
      卡片      依 CATEGORIES 分段，每段 .grid 內逐張 render_card()
      lightbox  #lb / #lb-img / #lb-x（js_block 綁事件）
    """
    environment = environment or {}
    e2e_data = e2e_data or []
    sections = "\n".join(
        f'<section class="cat" id="cat-{letter}" data-cat="{letter}">'
        f'<h2 class="cat-h"><span class="cat-k">Cat {letter}</span>'
        f'{esc(CATEGORIES[letter])}<span class="cat-n">{len(by_cat[letter])}</span></h2>'
        f'<div class="grid">{"".join(render_card(c) for c in by_cat[letter])}</div></section>'
        for letter in CATEGORIES if letter in by_cat)
    title = os.environ.get("REPORT_TITLE", "SDK_AUTOMATION - " + " · ".join(
        x.upper() for x in (test_mode, test_type) if x))
    return f"""<meta charset="utf-8">
<title>{esc(title)}</title>
<style>{CSS}</style>
<header class="top"><div class="top-in">
  <div class="brand"><div class="sig" aria-hidden="true"></div>
    <div><div class="kicker">Appier SDK 開發案 · 自動化測試</div><h1>{esc(title)}</h1></div>
  </div>
  <dl class="meta">
    <div><dt>Round</dt><dd>{esc(round_name)}</dd></div>
    <div><dt>類型</dt><dd>{esc(test_type or '—')}</dd></div>
    <div><dt>整合模式</dt><dd>{esc(test_mode or '—')}</dd></div>
    <div><dt>Test CID</dt><dd>{esc(test_cid or '—')}</dd></div>
    <div><dt>執行人</dt><dd>{esc(test_executor or '—')}</dd></div>
    <div><dt>Device</dt><dd>Android · {esc(model)}</dd></div>
    <div><dt>Signal / E2E</dt><dd>{total} / {len(e2e_data)}</dd></div>
    <div><dt>整體耗時</dt><dd>{esc(elapsed or '—')}</dd></div>
    <div><dt>Generated</dt><dd>{esc(generated)}</dd></div>
  </dl>
</div></header>
<main>
  <div class="tab-pane" data-pane="signal">{sections}</div>
  <div class="tab-pane" data-pane="e2e" hidden>{render_e2e_pane(e2e_data, test_mode, test_type)}</div>
</main>
<!-- TODO(版面): 分頁鈕 / tiles / 環境面板 / 一致性面板 / 未完成項目清單 / lightbox -->
<script>{js_block(json.dumps(shots_js), json.dumps(round_name))}</script>
"""








def _cli_report(argv):
    """--report <round_dir>：重算單輪 HTML 報告（不碰實機）。"""
    ap = argparse.ArgumentParser(prog="qa_aos.py --report", description="重算單輪報告")
    ap.add_argument("--report", dest="round_dir", required=True,
                    help="evidence round 資料夾（Signal 來源）")
    ap.add_argument("--out", help="輸出 HTML 路徑（預設 <round_dir>/report.html）")
    ap.add_argument("--e2e-round", dest="e2e_round",
                    help="E2E 分頁改從此 round 評估（signal 用 --report、E2E 用專跑 flow 的 round）")
    ap.add_argument("--meta", help="把該輪計數/中繼資料另存成 JSON（供 page.py 讀取，"
                                   "使跨平台頁不需 import 平台模組）")
    args = ap.parse_args(argv)
    out = args.out or os.path.join(args.round_dir, "report.html")
    meta = build(args.round_dir, out, e2e_round=args.e2e_round)
    if args.meta:
        with open(args.meta, "w") as f:
            json.dump(meta, f, ensure_ascii=False, default=str)


# ════════════════════════════════════════════════════════════════════════════
# 用 adb 佈裝置狀態並讀回確認
#   （原 device_state.py）
# ════════════════════════════════════════════════════════════════════════════

UDID = os.environ.get("UDID", "").strip()
APP_PACKAGE = os.environ.get("APP_PACKAGE", "").strip()

# 本批次實際做過的狀態動作；run_capture 取走後清空（見 take_state_actions）
STATE_ACTIONS = []


class StateError(RuntimeError):
    """目標裝置狀態無法建立（讀回值與預期不符）。"""


def take_state_actions():
    """取出並清空本批次的狀態動作 trace。"""
    global STATE_ACTIONS
    actions = STATE_ACTIONS
    STATE_ACTIONS = []
    return actions


def adb_state(*args):
    """狀態讀回專用：stdout 與 stderr **併在一起**回傳（dumpsys / cmd locale 等
    會把要判讀的內容寫到 stderr），且不丟例外。與 run_qa 的 adb()（只取 stdout、
    失敗回 [err: ...]）語意不同，合併成單檔後兩者都要保留，不可互換。"""
    cmd = ["adb"] + (["-s", UDID] if UDID else []) + list(args)
    p = subprocess.run(cmd, text=True, capture_output=True)
    return (p.stdout + p.stderr).strip()


# ── 佈狀態（骨架）─────────────────────────────────────────────────────────────
#
# 刻意留空：每個狀態要怎麼佈、佈完要讀回什麼才算成立，屬於 TC 定義的一部分，
# 跟 TC 目錄一起重新設計。原版（約 350 行，含各狀態的讀回驗證與 Tailscale／GAID
# 那些特例）：`git show 8307b56:qa_aos.py`
#
# **設計原則（原版最重要的一條，重建時務必保留）：設完必須讀回確認，沒有人工
# fallback。** 設不起來就 `raise StateError`，讓 round 排程跳過該批 capture、
# 整輪繼續跑；對應的 TC 在報告裡是「本輪未執行 → Blocked」。絕對不要「設了就假設
# 成功」—— 那會產出「狀態其實沒佈成、但 bid 被當成該狀態的證據」的假 PASS/假 FAIL。
#
# 骨架行為：所有 ensure_/set_ 一律 raise StateError。所以現在跑完整 round 會是
#   CURRENT 批次照跑（它本來就不佈狀態）→ CTRL1/CTRL2/CTRL3/SD 全部乾淨跳過
# 這正是「狀態佈不起來」的既有路徑（`run_round()` 接 StateError → `[<批次> 跳過]`），
# 不是新的失敗模式。
#
# 重建時要填回的狀態（原版的 CTRL1/CTRL2/CTRL3 對照見 CLAUDE.md〈命名契約〉）：
#   時區、電量與充電、省電模式、VPN on/off、GAID opt-in/opt-out、
#   深色模式、亮度、字級、媒體音量、App 語系、定位權限
#
# 查詢類 helper（`battery_state` / `vpn_active` / `tracking_opted_in` /
# `_location_granted` / `dump_ui`）回中性值，讓呼叫端不會炸；
# `restore_standard_state()` 必須是 no-op 而不是 raise —— 它掛在 `atexit`，
# 在收尾階段丟例外會蓋掉真正的錯誤訊息。


def _not_built(what):
    raise StateError(f"{what}：佈狀態未實作（骨架）")


def ensure_timezone(zone, label):
    _not_built(f"{label} 時區 {zone}")


def battery_state():
    """回 (level:int|None, charging:bool|None)。骨架：讀不到。"""
    return None, None


def ensure_battery(title, level=None, charging=None):
    _not_built(f"{title} 電量 level={level} charging={charging}")


def vpn_active():
    """VPN 是否連線中。骨架：一律回 False。"""
    return False


def set_tailscale(expected):
    _not_built(f"Tailscale {'on' if expected else 'off'}")


def ensure_vpn(expected):
    _not_built(f"VPN {'on' if expected else 'off'}")


def dump_ui():
    """當前畫面的 UI dump（XML）。骨架：回空字串。"""
    return ""


def tracking_opted_in():
    """GAID 是否 opt-in。骨架：回 None（讀不到）。"""
    return None


def ensure_tracking(expected):
    _not_built(f"GAID {'opt-in' if expected else 'opt-out'}")


def ensure_app_locale(language_tag):
    _not_built(f"App locale {language_tag}")


def set_and_verify(label, set_args, get_args, expected):
    _not_built(f"{label} = {expected}")


def set_volume(value):
    _not_built(f"媒體音量 {value}")


def _location_granted():
    """定位權限是否已授權。骨架：回 None（讀不到）。"""
    return None


def set_location(grant):
    _not_built(f"定位權限 {'允許' if grant else '拒絕'}")


def auto_common(high):
    """一組受控狀態（high=True 走 CTRL2 的『相反／高／拒絕』那一側）。"""
    _not_built(f"CTRL{'2' if high else '1'} 共同狀態")


def restore_standard_state():
    """還原成標準狀態。掛在 atexit，**不可 raise**。骨架：什麼都不做。"""
    print("[骨架] restore_standard_state 未實作，未還原任何裝置狀態。")



# ── 發佈（交給 page.py，維持平台檔零 import）──────────────────────────────────

def auto_publish():
    """重產跨平台整合頁並推上 gh-pages。

    刻意用 subprocess 而不是 import page：平台檔不得依賴跨平台頁，否則
    「兩平台互不干擾」就破功（page.py 反過來也只用 subprocess 呼叫平台檔）。
    發佈失敗不得影響已保存的證據，所以 check=False。
    """
    if os.environ.get("AUTO_PUBLISH", "1") == "0":
        print("[publish] AUTO_PUBLISH=0，略過 GitHub Pages")
        return None
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.py")
    result = subprocess.run([sys.executable, script, "--publish"], check=False)
    if result.returncode:
        print(f"[warn] GitHub Pages 發佈失敗（evidence 已保存，exit {result.returncode}）")
    return None

# ════════════════════════════════════════════════════════════════════════════
# 完整 round 排程（狀態批次、失敗補跑）
#   （原 ssp_round.py）
# ════════════════════════════════════════════════════════════════════════════

ROOT = Path(__file__).parent


def _round_prefix(env):
    safe_cid = re.sub(r"[^A-Za-z0-9_-]+", "-", env["TEST_CID"]).strip("-")
    return (f"{env['TEST_MODE'].upper().replace('-', '_')}_"
            f"{env['TEST_TYPE'].upper().replace('-', '_')}_CID_"
            f"{safe_cid}_{env['TEST_ROUND']}")


def latest_round_dir(env):
    rounds = sorted(path for path in (ROOT / "evidence").glob(f"{_round_prefix(env)}_*")
                    if path.is_dir())
    return rounds[-1] if rounds else None



def capture_timeout():
    """每個 capture 的牆鐘上限（秒）；0＝不限。預設 20 分鐘。"""
    return float(os.environ.get("PHASE_TIMEOUT_SEC", "1200"))


def run_capture(label, tcs, env, dwell=0, fgbg=False, action=""):
    """Spawn one capture in a fresh process/Appium session."""
    state_actions = take_state_actions()
    action_trace = "; ".join([action] + state_actions) if state_actions else action
    timeout = capture_timeout()
    run_env = {**os.environ, **env, "CAPTURE_LABEL": label,
               "DWELL_SEC": str(dwell), "DO_FGBG": "1" if fgbg else "0",
               "STATE_ACTION": action_trace,
               "PHASE_TIMEOUT_SEC": str(timeout),
               # 不帶 TC＝CURRENT 批次（不佈狀態、驗全部 checks）。用內部 env 而非 argv，
               # 讓對外介面只有「跑一輪」與「補跑指定 TC」兩種。
               "SSP_CAPTURE": "" if tcs else "current",
               # 一輪有 7~11 個 capture；每個都重產平台並 push gh-pages 太貴（而且會把
               # 中間狀態推上線）→ round 期間關掉，收尾在 run() 統一發佈一次。
               "AUTO_PUBLISH": "0"}
    print(f"\n{'='*18} {label}: Capture {'='*18}")
    cmd = [sys.executable, str(ROOT / "run_qa.py")] + ([",".join(tcs)] if tcs else [])
    try:
        # 子行程自己會在 PHASE_TIMEOUT_SEC 到點時乾淨收尾（exit 5）；這裡的硬 timeout
        # 只是備援，防它卡在 loop 以外的地方（例如 Appium 連線 hang）。
        result = subprocess.run(cmd, env=run_env,
                                timeout=(timeout + 120) if timeout else None)
    except subprocess.TimeoutExpired:
        print(f"\n[本批未完成] capture 超過硬上限 {timeout + 120:.0f}s 未收尾，已強制中止。"
              "（Appium session 可能殘留，下一批會重開）")
        return 5
    if result.returncode == 5:
        print(f"\n[本批未完成] 已達 {timeout / 60:.0f} 分鐘上限仍未命中指定 CID，跳過此批。")
    elif result.returncode == 2:
        print("\n[本批未完成] Sample App 沒有觸發 Appier bid request。")
    elif result.returncode == 3:
        print("\n[本批未完成] 回 204 No Fill，未取得指定廣告；不建立正式 Capture。")
    elif result.returncode == 4:
        print("\n[本批未完成] 未能驗證指定 CID 的 loaded ad；不建立正式 Capture。")
    elif result.returncode:
        print(f"\n[本批未完成] Capture 執行失敗（exit {result.returncode}）。")
    return result.returncode


# ── 狀態批次 ────────────────────────────────────────────────────────────────────

def _phase_ctrl1(env):
    print("\n===== CTRL1：Default / Low / Allowed =====")
    ensure_tracking(True)
    ensure_app_locale("en-US")
    ensure_battery("電池／充電", level=100, charging=False)
    ensure_vpn(False)
    ensure_timezone("Asia/Taipei", "台北時區")
    auto_common(False)
    return run_capture("CTRL1", sorted(CTRL1_TCS), env,
                       action="Auto CTRL1：default/low/allowed")


def _m2_tcs(env):
    """CTRL2 的 TC 清單；REEN 輪排除 GAID opt-out 兩條（與投遞互斥）。"""
    tcs = set(CTRL2_TCS)
    if env["TEST_TYPE"].startswith("reen"):
        tcs -= set(TYPE_NA_REEN)   # REEN 輪不適用的 signal TC（見 TYPE_NA_REEN）
    return sorted(tcs)


def _phase_ctrl2(env):
    print("\n===== CTRL2：Opposite / High / Denied =====")
    # REEN 靠 GAID 對受眾：opt-out 後 campaign 一律 204 no-bid，與 CID 鎖定
    # capture 互斥 → REEN 輪不驗 AND-02/AND-76（於 AIBID 輪驗證）。
    # 實證 2026-07-15：opt-out 下 40/40 attempts 全 204；opt-in 下首發即 200。
    reen = env["TEST_TYPE"].startswith("reen")
    # Battery Saver（AND-08）在系統認為充電中時無法開啟 → CTRL2 必須 unplug mock。
    # （2026-07-15 曾疑 unplug mock 造成 adb 掉線，後查為 Appium server 崩潰所致）
    ensure_battery("CTRL2 low battery / unplugged", level=0, charging=False)
    auto_common(True)
    if reen:
        print(f"[略過] {sorted(TYPE_NA_REEN) or '（無）'}：REEN 投遞與該狀態互斥，本輪不驗")
        ensure_tracking(True)
    else:
        ensure_tracking(False)
    ensure_vpn(True)
    ensure_timezone("America/New_York", "紐約時區")
    return run_capture("CTRL2", _m2_tcs(env), env, dwell=35, fgbg=True,
                       action="Auto CTRL2：opposite/high/denied")


def _phase_ctrl3(env):
    print("\n===== CTRL3：Charging / UTC =====")
    set_and_verify("Battery Saver", ("shell", "cmd", "power", "set-mode", "0"),
                      ("shell", "settings", "get", "global", "low_power"), "0")
    ensure_battery("充電", charging=True)
    ensure_timezone("UTC", "UTC 時區")
    return run_capture("CTRL3", sorted(CTRL3_TCS), env, action="Auto CTRL3：charging + UTC")


def _phase_sd(env):
    """SD＝**Session Duration**（user.session_duration 三情境，AND-47-1/2/3）。

    不是裝置狀態批次，而是「行為序列」批次：session_duration＝App 前景累積時間，
    必須抓兩個 bid 對照（bid A → 做動作 → bid B）才驗得出累進/重置，所以這一批會
    產生 3 個 capture、6 個 bid。情境 2 會 force-stop 殺掉 App，因此不能與其他批次
    共用同一次 capture。
    """
    # user.session_duration＝App 前景累積時間（iOS 實作同語意）。
    # 三情境各跑一個 capture：bid A → 情境動作 → bid B 對照（run_qa SESSION_CASE）。
    print("\n===== SD：user.session_duration 三情境（App 前景時間）=====")
    # session 基準必須從乾淨標準狀態量；SD 排在 CTRL2/CTRL3 之後，不先還原會沾到
    # opt-out/VPN-on/UTC/暗色/滿亮度殘留，且 opt-out 可能填不到廣告。
    restore_standard_state()
    # SD capture 給有界重試（預設 40），避免殘留/低填充狀態下 run_qa 無限 retry 卡死。
    sc_attempts = os.environ.get("MAX_AD_ATTEMPTS", "40")
    rc = 0
    for case, desc in (("1", "只關廣告頁→累進"), ("2", "殺整個 App→重置"),
                       ("3", "背景切回→累進")):
        tc = SESSION_TCS.get(case)
        code = run_capture(tc, [tc],
                           {**env, "SESSION_CASE": case, "MAX_AD_ATTEMPTS": sc_attempts},
                           dwell=10, action=f"Auto SD{case}：{desc}")
        rc = rc or code
    return rc


def _phase_current(env):
    """CURRENT＝手機**當下的真實狀態**：不佈任何狀態，也不還原。

    刻意不呼叫 restore_standard_state()——這一輪的價值就是「使用者手機平常長什麼樣，
    SDK 在那個狀態下送什麼」，每次跑到的狀態都可能不同。因此：
      * 它必須排在 PHASE_ORDER 最前面，否則會繼承 CTRL2 的 opt-out/VPN-on/暗色殘留，
        那是實驗殘骸、不是使用者的狀態；
      * CURRENT_TCS 只能收「不隨裝置狀態改變」的欄位。任何期望值假設特定狀態的 TC
        都不該掛這一輪（AND-01/AND-75 假設 GAID opt-in，已移到 CTRL1 專驗）。
    這一輪同時跑 privacy icon 點擊與完整 E2E 生命週期。
    """
    print("\n===== CURRENT：手機當前狀態（不佈狀態、不還原）=====")
    return run_capture("CURRENT", [], env,
                       action="CURRENT 批次（手機當前狀態，未經佈置）")


# 同一個 TEST_ROUND 內的批次順序。CURRENT 必須**最前面**：它的語意是「手機當下的
# 真實狀態」，排在 CTRL 之後就會繼承 CTRL2 的 GAID opt-out／VPN on／暗色殘留。
# E2E TC 的自動判定函式登記表：TC 目錄的 "auto" 欄位指到這裡的 key。
# 每個函式簽名 (caps, ids, test_type) → (status, note, evidence_paths)。
# 從 0 重建：每條 E2E TC 要怎麼算通過，就在這裡加一個函式並登記。
E2E_AUTO_VALIDATORS = {}


def evaluate(round_dir, test_mode, test_type):
    """依 E2E_TCS 目錄逐條判定該輪的 E2E 結果，回報告用的列。

    重建合約（從 0 填回時照這個形狀）：
      * E2E_TCS 每條帶 `auto`（指到 E2E_AUTO_VALIDATORS 的 key）與 `modes`/`types`
        適用矩陣；不適用的組合要回 `na_mode` / `na_type`，報告端會歸 BLOCKED
      * 判定函式簽名 (caps, ids, test_type) → (status, note, evidence_paths)
      * caps / ids 由 round_dir 掃出（traffic log、logcat、逐步截圖），這段也待重建

    目錄為空 → 回空清單，E2E 分頁 0 列。這是目前的預期狀態。
    """
    if not E2E_TCS or not E2E_AUTO_VALIDATORS:
        return []
    raise NotImplementedError(
        "E2E 判定待重建：需先實作由 round_dir 取出 caps/ids 的部分，"
        "再依 E2E_TCS 的 auto 欄位派給 E2E_AUTO_VALIDATORS")


PHASE_ORDER = ["CURRENT", "CTRL1", "CTRL2", "CTRL3", "SD"]
PHASES = {"CTRL1": _phase_ctrl1, "CTRL2": _phase_ctrl2, "CTRL3": _phase_ctrl3,
          "SD": _phase_sd, "CURRENT": _phase_current}


# ── 失敗補跑 ────────────────────────────────────────────────────────────────────

def failed_signal_tcs(env):
    """Recompute the latest result for every executed Signal TC and return failures."""

    round_dir = latest_round_dir(env)
    if not round_dir:
        return set()
    caps = load_captures(str(round_dir))
    normal = {
        name: {(r["tc"], r["field"]): r for r in run_inspection(
            cap["bid"], reference_ms=cap.get("captured_at_ms"))}
        for name, cap in caps.items() if cap.get("bid") is not None
    }
    first = {
        name: {(r["tc"], r["field"]): r for r in run_inspection(
            cap["first_bid"], reference_ms=cap.get("captured_at_ms"))}
        for name, cap in caps.items() if cap.get("first_bid") is not None
    }
    failed = set()
    for validator in VALIDATORS:
        tc, field = validator["tc"], validator["field"]
        capture = pick_capture(tc, caps)
        source = first if tc in FIRST_BID_TCS and capture in first else normal
        result = source.get(capture, {}).get((tc, field))
        if result is not None and not result["passed"]:
            failed.add(tc)
    return failed


def _retry_state_ctrl1():
    ensure_tracking(True)
    ensure_app_locale("en-US")
    ensure_battery("CTRL1 retry battery", level=100, charging=False)
    ensure_vpn(False)
    ensure_timezone("Asia/Taipei", "CTRL1 retry timezone")
    auto_common(False)


def _retry_state_ctrl2(env):
    ensure_battery("CTRL2 retry battery", level=0, charging=False)
    auto_common(True)
    if env["TEST_TYPE"].startswith("reen"):
        ensure_tracking(True)   # REEN 不驗 opt-out（互斥），見 _phase_ctrl2 註解
    else:
        ensure_tracking(False)
    ensure_vpn(True)
    ensure_timezone("America/New_York", "CTRL2 retry timezone")


def _retry_state_ctrl3():
    set_and_verify("Battery Saver", ("shell", "cmd", "power", "set-mode", "0"),
                      ("shell", "settings", "get", "global", "low_power"), "0")
    ensure_battery("CTRL3 retry charging", charging=True)
    ensure_timezone("UTC", "CTRL3 retry timezone")


def retry_failed_rounds(env):
    """Retry every failed Signal TC in a matching state capture."""

    max_retries = int(os.environ.get("MAX_FAILED_RETRIES", "1"))
    for attempt in range(1, max_retries + 1):
        failed = failed_signal_tcs(env)
        if not failed:
            print("[Retry] 沒有失敗的 Signal TC。")
            return
        print(f"\n===== 自動 Retry {attempt}/{max_retries}：{','.join(sorted(failed))} =====")
        phases = [
            ("CURRENT", sorted(failed & CURRENT_TCS)),
            ("CTRL1", sorted(failed & CTRL1_TCS)),
            ("CTRL2", sorted(failed & CTRL2_TCS)),
            ("CTRL3", sorted(failed & CTRL3_TCS)),
        ]
        for phase, tcs in phases:
            if not tcs:
                continue
            try:
                if phase == "CURRENT":
                    # CURRENT_TCS 應在標準狀態下抓；否則（如 STOP_AFTER=CTRL2 後）會沾 CTRL2 殘留。
                    restore_standard_state()
                elif phase == "CTRL1":
                    _retry_state_ctrl1()
                elif phase == "CTRL2":
                    _retry_state_ctrl2(env)
                elif phase == "CTRL3":
                    _retry_state_ctrl3()
            except StateError as exc:
                print(f"\n[{phase} retry 跳過] {exc}")
                continue
            run_capture(f"{phase}_RETRY{attempt}", tcs, env,
                        dwell=35 if phase == "CTRL2" else 0,
                        fgbg=phase == "CTRL2",
                        action=f"Auto {phase} retry {attempt}：{','.join(tcs)}")


# ── 入口（由 run_qa.py 呼叫）───────────────────────────────────────────────────

def run_round(env, scope="all"):
    """跑一輪；回傳 exit code（0＝所有批次都完成）。

    scope="all"     Signal + E2E 全驗（預設）
    scope="signal"  子 capture 一律關掉 privacy 點擊與 E2E 流程
    scope="e2e"     只跑 CURRENT 批次（E2E 掛在它上面），不佈狀態、不補跑
    """
    # 預設從 PHASE_ORDER 第一個開始（CURRENT）。這裡寫死批次名的話，一旦順序調整就會
    # 靜默切掉排在前面的批次——那批的 TC 會全落「本輪未執行」而沒人發現。
    start_at = os.environ.get("START_AT", PHASE_ORDER[0]).upper()
    if start_at not in PHASE_ORDER:
        sys.exit("START_AT 必須是 CURRENT、CTRL1、CTRL2、CTRL3 或 SD")
    stop_after = os.environ.get("STOP_AFTER", "").upper()
    phase_names = PHASE_ORDER[PHASE_ORDER.index(start_at):]
    if stop_after in PHASE_ORDER:
        phase_names = [p for p in phase_names
                       if PHASE_ORDER.index(p) <= PHASE_ORDER.index(stop_after)]
    if scope == "e2e":
        # E2E 15 條是廣告生命週期、與裝置狀態無關 → 只需要 CURRENT 那一次 capture。
        # 這裡蓋掉 START_AT/STOP_AFTER：--e2e-only 的語意就是「只跑 CURRENT」。
        phase_names = ["CURRENT"]
    elif scope == "signal":
        # 設在 parent 的 env → run_capture 建 run_env 時會傳給每個子 capture
        os.environ["DO_PRIVACY_CLICK"] = "0"
        os.environ["DO_E2E_FLOW"] = "0"

    timeout = capture_timeout()
    scope_label = {"all": "Signal + E2E", "signal": "只驗 Signal（跳過 E2E）",
                   "e2e": "只驗 E2E（只跑 CURRENT 批次）"}[scope]
    print(f"[範圍  ] {scope_label}")
    print(f"[round ] 批次：{' → '.join(phase_names)}")
    print(f"[上限  ] 每個 capture {timeout / 60:.0f} 分鐘"
          if timeout else "[上限  ] 每個 capture 無上限（PHASE_TIMEOUT_SEC=0）")
    # 中途 Ctrl-C／例外離開時也要把手機還原成可讀的標準狀態
    atexit.register(restore_standard_state)
    # 單一狀態批次失敗（狀態建不起來 / capture 沒命中）不擋同 round 其他批次；
    # 缺的批次可用 START_AT 單獨補、STOP_AFTER 提前收尾。
    incomplete = []
    for name in phase_names:
        try:
            rc = PHASES[name](env)
        except StateError as exc:
            print(f"\n[{name} 跳過] {exc}")
            incomplete.append(name)
            continue
        if rc:
            incomplete.append(name)
    if scope == "e2e":
        print("\n[Retry] --e2e-only：不補跑 Signal TC。")
    else:
        try:
            retry_failed_rounds(env)
        except StateError as exc:
            print(f"\n[Retry 中斷] {exc}")
    if incomplete:
        print(f"\n完整 TC round 部分完成：{'、'.join(incomplete)} 狀態批次未完成，其餘已合併。"
              "root/emulator/SIM 另列硬體輪次。")
    else:
        print("\n完整 TC round 已完成並合併；root/emulator/SIM 另列硬體輪次。")
    # 整輪只發佈一次（每個 capture 都發＝7~11 次 clone+重產 9MB 平台+push）。
    # auto_publish 仍尊重使用者自己設的 AUTO_PUBLISH=0。
    auto_publish()
    return 1 if incomplete else 0


# ════════════════════════════════════════════════════════════════════════════
# 實機 capture（Appium／logcat／證據落地）與入口
#   （原 run_qa.py）
# ════════════════════════════════════════════════════════════════════════════

sys.path.insert(0, str(Path(__file__).parent))


FLAG_FILE    = "/tmp/appier_hit"
BID_FILE     = "/tmp/appier_bid.json"
FIRST_BID_FILE = "/tmp/appier_first_bid.json"
BID_STATUS_FILE   = "/tmp/appier_bid_status"
BID_RESPONSE_FILE = "/tmp/appier_bid_response.json"
TRAFFIC_FILE = "/tmp/appier_traffic.jsonl"  # detector 的全流量 log（E2E 驗證用）
NETWORK_FILE      = "/tmp/current_networks"
LOGCAT_TMP   = "/tmp/appier_logcat.txt"

LOGCAT_PROC = None
APPIUM_URL   = "http://127.0.0.1:4723"
BID_TIMEOUT  = 12.0
EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR", Path(__file__).parent / "evidence"))

APP_PACKAGE  = os.environ.get("APP_PACKAGE", "").strip()
APP_ACTIVITY = os.environ.get("APP_ACTIVITY")
TRIGGER_TEXT = os.environ.get("TRIGGER_TEXT", "Native - basic format")
def _round_label(value):
    """round 標籤會變成資料夾名的一段：只留英數與 -_，長度上限 24；未給時用 R<日期>。"""
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", (value or "").strip()).strip("-_")[:24]
    return label or "R" + datetime.now().strftime("%Y%m%d")


TEST_ROUND   = _round_label(os.environ.get("TEST_ROUND", ""))
VALID_TYPES  = ("aibid", "reen-static", "reen-dynamic")
VALID_MODES  = ("standalone", "admob-mediation", "applovin-mediation")
TEST_TYPE    = os.environ.get("TEST_TYPE", "").strip().lower()  # 這輪測什麼
TEST_MODE    = os.environ.get("TEST_MODE", "").strip().lower()  # SDK 整合模式
TEST_CID     = os.environ.get("TEST_CID", "").strip()
TEST_EXECUTOR = os.environ.get("TEST_EXECUTOR", "").strip() or getpass.getuser()

MODE_TAB = {
    "standalone": "Appier SDK",
    "admob-mediation": "AdMob Mediation",
    "applovin-mediation": "AppLovin Mediation",
}


def find_onscreen_text(driver, text):
    """Find a visible element by text, ignoring case and off-screen ViewPager pages."""
    width = driver.get_window_size()["width"]
    elements = driver.find_elements(
        AppiumBy.ANDROID_UIAUTOMATOR,
        f'new UiSelector().textMatches("(?i){re.escape(text)}")',
    )
    for element in elements:
        location = element.location
        size = element.size
        center_x = location["x"] + size["width"] // 2
        if 0 <= center_x < width:
            return element
    return None


def select_test_mode_tab(driver):
    """Switch the sample app to the tab represented by TEST_MODE."""
    tab_name = MODE_TAB[TEST_MODE]
    for attempt in range(1, 5):
        tab = find_onscreen_text(driver, tab_name)
        if tab is not None:
            tab.click()
            time.sleep(0.8)
            if find_onscreen_text(driver, TRIGGER_TEXT) is not None:
                print(f"[tab   ] {tab_name}")
                return
        if attempt < 4:
            driver.back()
            time.sleep(0.8)
    raise RuntimeError(
        f"無法切換到 {tab_name} 或找不到版位 '{TRIGGER_TEXT}'；"
        "請確認 sample app 版本及 TRIGGER_TEXT。"
    )


def tap_trigger(driver):
    """Tap only the trigger belonging to the currently visible mode tab."""
    trigger = find_onscreen_text(driver, TRIGGER_TEXT)
    if trigger is None:
        return False
    trigger.click()
    return True


def resolve_test_type():
    """依序詢問 AIBID/REEN；REEN 再詢問 Static/Dynamic。"""
    global TEST_TYPE
    if TEST_TYPE in VALID_TYPES:
        return TEST_TYPE
    if TEST_TYPE:
        print(f"[warn] TEST_TYPE='{TEST_TYPE}' 非法，應為 {VALID_TYPES}")
    if not sys.stdin.isatty():
        sys.exit("非互動執行必須設定 TEST_TYPE=aibid|reen-static|reen-dynamic")

    while True:
        goal = input("整個流程的目標是？ 1) AIBID  2) REEN: ").strip().lower()
        goal = {"1": "aibid", "2": "reen"}.get(goal, goal)
        if goal == "aibid":
            TEST_TYPE = "aibid"
            break
        if goal == "reen":
            while True:
                creative = input("REEN 現在測的是？ 1) Static  2) Dynamic: ").strip().lower()
                creative = {"1": "static", "2": "dynamic"}.get(creative, creative)
                if creative in ("static", "dynamic"):
                    TEST_TYPE = f"reen-{creative}"
                    break
                print("請輸入 1/2、Static 或 Dynamic。")
            break
        print("請輸入 1/2、AIBID 或 REEN。")
    return TEST_TYPE


def resolve_test_mode():
    """取得 SDK 整合模式；與 AIBID/REEN 投放目的為獨立維度。"""
    global TEST_MODE
    aliases = {
        "1": "standalone", "2": "admob-mediation", "3": "applovin-mediation",
        "admob": "admob-mediation", "applovin": "applovin-mediation",
        "mediation": "admob-mediation",
    }
    TEST_MODE = aliases.get(TEST_MODE, TEST_MODE)
    if TEST_MODE in VALID_MODES:
        return TEST_MODE
    if TEST_MODE:
        print(f"[warn] TEST_MODE='{TEST_MODE}' 非法，應為 {VALID_MODES}")
    if not sys.stdin.isatty():
        sys.exit("非互動執行必須設定 TEST_MODE=standalone|admob-mediation|applovin-mediation")
    while True:
        value = input(
            "SDK 整合模式是？ 1) Standalone  2) AdMob Mediation  "
            "3) AppLovin Mediation: "
        ).strip().lower()
        value = aliases.get(value, value)
        if value in VALID_MODES:
            TEST_MODE = value
            return TEST_MODE
        print("請輸入 1/2/3、Standalone、AdMob 或 AppLovin。")


def resolve_test_cid():
    """取得本輪測試 CID；互動模式必問，非互動模式要求 TEST_CID。"""
    global TEST_CID
    if TEST_CID:
        return TEST_CID
    if not sys.stdin.isatty():
        sys.exit("非互動執行必須設定 TEST_CID")
    while not TEST_CID:
        TEST_CID = input("你的測試用 CID 是什麼？ ").strip()
        if not TEST_CID:
            print("CID 不可空白。")
    return TEST_CID
STATE_ACTION = os.environ.get("STATE_ACTION")       # 本次實際做了什麼（實機/模擬）
CAPTURE_LABEL = os.environ.get("CAPTURE_LABEL", "").strip()
DO_FGBG      = os.environ.get("DO_FGBG", "0") == "1"
DWELL_SEC    = float(os.environ.get("DWELL_SEC", "0"))  # 觸發廣告前先前景停留秒數
AD_RETRY_DELAY = float(os.environ.get("AD_RETRY_DELAY", "2"))
MAX_AD_ATTEMPTS = int(os.environ.get("MAX_AD_ATTEMPTS", "0"))  # 0 = retry without limit
# 牆鐘上限（秒，0＝不限）：刷不到指定 CID 時不設次數上限會無限重試。單獨跑一次
# capture 時預設不限（人在旁邊看，要刷到命中就讓它刷）；完整 round 由 ssp_round
# 帶入 PHASE_TIMEOUT_SEC（預設 20 分鐘），到點乾淨收尾（exit 5），
# 該批 TC 落「本輪未執行」，不讓無人值守的一輪卡死在某一批。
PHASE_TIMEOUT_SEC = float(os.environ.get("PHASE_TIMEOUT_SEC", "0"))
# SAVE_ON_BID=1：偵測到 bid request 即入庫，不要求 200/CID 命中。
# 用於只驗 request payload 的 TC（如 AND-12 emulator / AND-10 非 root），
# 這類環境（模擬器新 GAID、opt-out）REEN campaign 本來就不出價
SAVE_ON_BID = os.environ.get("SAVE_ON_BID", "0") == "1"

# SESSION_CASE=1/2/3：user.session_duration 三情境（AND-47-1/2/3）。
# session_duration＝使用者 App 在前景的累積時間（毫秒），不是廣告 session 載入時間。
# 流程：命中 bid A → 情境動作 → 再觸發 bid B → 對照寫 session_case.json。
#   1 = 只關廣告頁（App 全程前景）→ 預期累進（B > A）
#   2 = force-stop 關整個 App 重開   → 預期重置（B < A）
#   3 = 退背景數秒再切回前景         → 預期累進（B > A）
SESSION_CASE = os.environ.get("SESSION_CASE", "").strip()
SESSION_GAP_SEC = float(os.environ.get("SESSION_GAP_SEC", "8"))  # 動作後累積前景秒數
SESSION_CASE_FILE  = "/tmp/appier_session_case.json"
SESSION_BID_A_FILE = "/tmp/appier_session_bid_a.json"
SESSION_LOGCAT_A   = "/tmp/appier_session_logcat_a.txt"

# 對外介面：不帶參數＝跑一輪；帶 TC 清單＝補跑那幾條；--signal-only／--e2e-only 收窄範圍。
# SSP_CAPTURE=current 是 ssp_round 給子行程的**內部**訊號（CURRENT 批次＝不佈狀態、驗全部
# checks 的那次 capture）；不對外文件化，使用者不需要知道有這個模式。
# 工具模式：不碰實機，只重算報告或離線驗 bid。必須在「跑一輪」的旗標檢查之前
# 分流，否則 --report 這種帶值的參數會被當成不認得的旗標。
TOOL_MODES = ("--report", "--inspect", "--inspect-round")
TOOL_MODE = next((m for m in TOOL_MODES if m in sys.argv[1:]), None)

_FLAGS = {"--signal-only", "--e2e-only", "--help", "-h"}
_argv = [] if TOOL_MODE else sys.argv[1:]
_unknown = [a for a in _argv if a.startswith("-") and a not in _FLAGS]
if _unknown:
    sys.exit(f"不認得的參數：{' '.join(_unknown)}\n{__doc__}")
if {"--help", "-h"} & set(_argv):
    print(__doc__)
    sys.exit(0)
SIGNAL_ONLY = "--signal-only" in _argv
E2E_ONLY = "--e2e-only" in _argv
if SIGNAL_ONLY and E2E_ONLY:
    sys.exit("--signal-only 與 --e2e-only 互斥：要兩者都驗就不要帶旗標。")
_POS = [a.strip() for a in _argv if not a.startswith("-")]
_ARG1 = _POS[0] if _POS else ""
_CURRENT_CAPTURE = os.environ.get("SSP_CAPTURE", "").strip().lower() == "current"
if E2E_ONLY and _ARG1:
    sys.exit("--e2e-only 不能跟指定 TC 併用：E2E 掛在 CURRENT 批次，不是單條 TC。")
FULL_ROUND = not _ARG1 and not _CURRENT_CAPTURE
TC_ID = "CURRENT" if not _ARG1 else _ARG1
if FULL_ROUND:
    TC_ID = "ROUND"   # 本 process 不 capture，只做排程；用於 round_timing 標籤
if SESSION_CASE and TC_ID == "CURRENT":
    TC_ID = SESSION_TCS.get(SESSION_CASE) or TC_ID   # 補跑單一 session 情境時自動掛對 TC

# E2E 完整流程（點擊 + landing）與 privacy icon 點擊：CURRENT 批次一律開（它本來就該跑
# 完整 E2E 生命週期），狀態類 TC 預設關，--signal-only 一律關；環境變數可覆蓋。
# 需在 TC_ID 決定後才判斷。
_FLOW_DEFAULT = "0" if SIGNAL_ONLY else ("1" if TC_ID == "CURRENT" else "0")
DO_PRIVACY_CLICK = os.environ.get("DO_PRIVACY_CLICK", _FLOW_DEFAULT) == "1"
DO_E2E_FLOW = os.environ.get("DO_E2E_FLOW", _FLOW_DEFAULT) == "1"
# argv 優先；未帶時吃 UDID 環境變數（round 排程就是用 env 傳給每個 capture；
# 多裝置在線時必要）
UDID  = _POS[1] if len(_POS) > 1 else (os.environ.get("UDID", "").strip() or None)


def resolve_round_dir():
    """同 round 標籤重複執行時歸入既有資料夾；沒有才用當下時間戳開新的。"""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    safe_cid = re.sub(r"[^A-Za-z0-9_-]+", "-", TEST_CID).strip("-")
    type_label = TEST_TYPE.upper().replace("-", "_")
    mode_label = TEST_MODE.upper().replace("-", "_")
    prefix = f"{mode_label}_{type_label}_CID_{safe_cid}_{TEST_ROUND}"
    existing = sorted(d for d in EVIDENCE_DIR.glob(f"{prefix}_*") if d.is_dir())
    if existing:
        return existing[-1]
    return EVIDENCE_DIR / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


# ── adb helpers ───────────────────────────────────────────────────────────────

def adb(*args):
    cmd = ["adb"]
    if UDID:
        cmd += ["-s", UDID]
    cmd += list(args)
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception as e:
        return f"[err: {e}]"


def detect_udid():
    if UDID:
        return UDID
    out = subprocess.check_output(["adb", "devices"], text=True)
    devices = [
        line.split()[0]
        for line in out.splitlines()
        if line.strip() and not line.startswith("List") and line.split()[-1] == "device"
    ]
    if not devices:
        sys.exit("No Android device found. Connect device or start emulator.")
    if len(devices) > 1:
        sys.exit(f"Multiple devices: {devices}\nSpecify: python run_qa.py {TC_ID} <UDID>")
    return devices[0]


# ── logcat capture (session-concurrent) ───────────────────────────────────────

def start_logcat():
    """從 app 啟動前開始錄 logcat，bid capture 時整段收進 evidence。"""
    global LOGCAT_PROC
    adb("logcat", "-c")
    cmd = ["adb"]
    if UDID:
        cmd += ["-s", UDID]
    cmd += ["logcat", "-v", "time"]
    out = open(LOGCAT_TMP, "w")
    LOGCAT_PROC = subprocess.Popen(cmd, stdout=out, stderr=subprocess.DEVNULL)


def stop_logcat():
    global LOGCAT_PROC
    if LOGCAT_PROC is not None:
        LOGCAT_PROC.terminate()
        try:
            LOGCAT_PROC.wait(timeout=3)
        except subprocess.TimeoutExpired:
            LOGCAT_PROC.kill()
        LOGCAT_PROC = None


# SDK logs the full bid body + result to logcat, so field validation needs no
# proxy. 兩種格式都吃：舊 [AdRequestJSON] {...}；新 [Appier SDK] Ad request body: {...}
ADREQ_RE = re.compile(r"(?:\[AdRequestJSON\]|Ad request body:)\s*(\{.*\})\s*$")
LOADED_RE = re.compile(r"onAdLoaded\(\)")
NOBID_RE = re.compile(r"onAdNoBid\(\)")
LOADFAIL_RE = re.compile(r"onAdLoadFail\(\)")
IMPRESSION_RE = re.compile(r"Requesting impression tracker:.*?[?&]cid=([^&\s]+).*?[&]crid=([^&\s]+)")


def scan_logcat_bid():
    """從側錄的 logcat 抓最後一筆 bid body + 結果狀態。

    回傳 (bid_dict, status) — status 200=onAdLoaded / 204=onAdNoBid / None=未定。
    無 bid body 時回 (None, None)。純靠 SDK log，不需要 proxy/TLS 攔截。
    """
    if not os.path.exists(LOGCAT_TMP):
        return None, None
    bid = None
    status = None
    for line in open(LOGCAT_TMP, errors="ignore"):
        m = ADREQ_RE.search(line)
        if m:
            try:
                bid = json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        elif LOADED_RE.search(line):
            status = "200"
        elif NOBID_RE.search(line):
            status = "204"
        elif LOADFAIL_RE.search(line):
            status = "loadfail"
    return bid, status


def scan_logcat_ad_identity():
    """Return the identity of the ad that actually loaded, not merely requested."""
    if not os.path.exists(LOGCAT_TMP):
        return None
    identity = None
    for line in open(LOGCAT_TMP, errors="ignore"):
        match = IMPRESSION_RE.search(line)
        if match:
            identity = {"cid": match.group(1), "crid": match.group(2)}
    return identity


# ── no-ad diagnosis ───────────────────────────────────────────────────────────
# 刷不到廣告時分辨：沒廣告可刷（no-bid）vs 連線鏈路哪一段有問題。

NET_ERR_RE = re.compile(
    r"SSLHandshakeException|CertPathValidatorException|UnknownHostException|"
    r"ConnectException|SocketTimeoutException|Failed to connect|ERR_PROXY|"
    r"NO_FILL|network error",
    re.IGNORECASE,
)


def _mac_port_listening(port):
    """Mac 本機是否有人在聽該 port（Charles 8888 / mitmdump 8081）。None=無法判定。"""
    try:
        out = subprocess.run(
            ["/usr/sbin/lsof", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        return bool(out.stdout.strip())
    except Exception:
        return None


def diagnose_no_ad():
    """Timeout 後解析 logcat + proxy 鏈路，印出「沒廣告」或「哪段連線斷了」的判定。"""
    print("\n[診斷] 解析刷不到廣告的原因 ...")

    log_txt = ""
    if os.path.exists(LOGCAT_TMP):
        log_txt = open(LOGCAT_TMP, errors="ignore").read()
    sdk_active = bool(re.search(r"appier", log_txt, re.IGNORECASE))
    bid_sent = bool(ADREQ_RE.search(log_txt))
    nobid = bool(NOBID_RE.search(log_txt))
    proxy_status = (open(BID_STATUS_FILE).read().strip()
                    if os.path.exists(BID_STATUS_FILE) else None)
    net_err = NET_ERR_RE.search(log_txt)

    # proxy 鏈路狀態
    phone_proxy = adb("shell", "settings", "get", "global", "http_proxy")
    charles_up = _mac_port_listening(8888)
    mitm_up = _mac_port_listening(8081)
    traffic_age = None
    if os.path.exists(NETWORK_FILE):
        traffic_age = time.time() - os.path.getmtime(NETWORK_FILE)

    def yn(v):
        return "?" if v is None else ("yes" if v else "NO")

    print(f"  SDK 有動靜 (logcat appier)   : {yn(sdk_active)}")
    print(f"  SDK 有送 bid (AdRequestJSON) : {yn(bid_sent)}")
    print(f"  proxy 看到 bid response      : {proxy_status or '(無)'}")
    print(f"  手機 http_proxy              : {phone_proxy or '(未設)'}")
    print(f"  Charles 在聽 8888            : {yn(charles_up)}")
    print(f"  mitmdump 在聽 8081           : {yn(mitm_up)}")
    if traffic_age is not None:
        print(f"  proxy 最近看到 ad 流量       : {traffic_age:.0f}s 前")

    # 判定（由具體到廣泛）
    if proxy_status == "204" or nobid:
        print("\n[判定] 沒有廣告可刷 — bid 有送出、server 回 204 no-bid，連線正常。")
        print("       是 campaign / fill 的問題，不是環境問題（等有量再刷、或換 zone）。")
    elif bid_sent or proxy_status:
        print("\n[判定] bid 已送出但 response 沒回來 — server 端或 TLS 中斷，"
              "檢查 mitmdump terminal 有無錯誤。")
    elif net_err:
        print(f"\n[判定] 連線問題 — device 端網路錯誤：{net_err.group(0)}")
        print(f"       logcat: {next(l for l in log_txt.splitlines() if net_err.group(0) in l).strip()[:160]}")
        print("       常見原因：手機沒裝/沒信任 Charles CA、proxy IP 過期、Wi-Fi 換網段。")
    elif not sdk_active:
        print("\n[判定] app 根本沒觸發廣告 — 不是連線問題。")
        print("       檢查：TRIGGER_TEXT 有沒有點到、Appier SDK log verbose 是否開啟、app 是否正確版本。")
    else:
        # SDK 有動但沒送 bid、proxy 也沒看到
        broken = []
        if phone_proxy in (None, "", "null") :
            broken.append("手機 http_proxy 未設")
        if charles_up is False:
            broken.append("Charles(8888) 沒開")
        if mitm_up is False:
            broken.append("mitmdump(8081) 沒開")
        if broken:
            print(f"\n[判定] 連線鏈路斷在：{'、'.join(broken)}")
        else:
            print("\n[判定] SDK 有載入但沒發 bid — 多半是 ad placement 沒觸發成功"
                  "（TRIGGER_TEXT 點錯頁）或 SDK 內部擋下（見 logcat_appier）。")


def extract_bid_ids(logtext):
    """從 logcat 的 impression/click tracker URL 解出本次 bid 的識別碼。
    比「每次都差不多的廣告截圖」有意義——bidobjid 唯一標識這次 bid/曝光。"""
    ids = {}
    for key in ("bidobjid", "cid", "crid", "crpid", "oid"):
        m = re.search(key + r"=([A-Za-z0-9_-]+)", logtext)
        if m:
            ids[key] = m.group(1)
    return ids


def _volume_music():
    """STREAM_MUSIC 音量 / 最大值。`media volume` 在部分機型不存在，改解 dumpsys audio。"""
    out = adb("shell", "cmd", "media_session", "volume", "--stream", "3", "--get")
    m = re.search(r"volume is\s+(\d+)\s+in range\s+\[(\d+)\.\.(\d+)\]", out, re.I)
    if m:
        return f"{m.group(1)}/{m.group(3)}"
    dump = adb("shell", "dumpsys", "audio")
    m = re.search(r"STREAM_MUSIC:.*?\n(?:.*\n)*?.*?Muted:", dump)
    seg = m.group(0) if m else dump
    cur = re.search(r"[Ss]treamVolume:\s*(\d+)", seg)
    mx = re.search(r"[Mm]ax(?:imum)?:\s*(\d+)", seg)
    if cur:
        return f"{cur.group(1)}" + (f"/{mx.group(1)}" if mx else "")
    return "(unavailable)"


def detect_root():
    """偵測裝置實際 root 狀態（ground truth），供對照 SDK 回報的 device.ext.jailbreak。

    不硬編「這台是不是 root」——每次 capture 實際查 Magisk / su binary，
    回傳 (is_rooted: bool|None, detail: str)。None = 查不出來。
    """
    signals = []
    pkgs = adb("shell", "pm", "list", "packages")
    if "[err" in pkgs or not pkgs.strip():
        return None, "無法判定 root（adb 查詢失敗：裝置未連或無授權）"
    if "topjohnwu.magisk" in pkgs:
        signals.append("Magisk app")
    for p in ("/system_ext/bin/su", "/sbin/su", "/system/bin/su", "/system/xbin/su"):
        ls = adb("shell", "ls", "-l", p)
        if "No such file" not in ls and "[err" not in ls and "Permission denied" not in ls:
            signals.append(f"su@{p}" + (" → magisk" if "magisk" in ls else ""))
    build_type = adb("shell", "getprop", "ro.build.type")
    debuggable = adb("shell", "getprop", "ro.debuggable")
    if signals:
        return True, "rooted (" + ", ".join(signals) + ")"
    if build_type == "userdebug" or debuggable == "1":
        return True, f"likely rooted (build.type={build_type}, debuggable={debuggable})"
    return False, f"not rooted (build.type={build_type}, no su/Magisk)"


# ── device state snapshot ─────────────────────────────────────────────────────

def snapshot_device_state():
    """capture 當下的裝置狀態文字快照 → `device_state.txt`（骨架）。

    與 `environment.json` 的分工：environment.json 是**結構化**的，報告會讀去做
    ground-truth 對照；device_state.txt 是**給人看的**全文快照（原版含 battery
    dumpsys 原文、前景 activity、受測 app 版本、VPN 介面），出事時翻它。

    刻意留空：要記什麼取決於 TC 怎麼定義，跟 TC 目錄一起重新設計。
    原版：`git show 8307b56:qa_aos.py`
    """
    return ""


def detect_device_kind():
    """實體機 or 模擬機（ground truth，來自 adb prop，不靠 SDK 的 device.ext.emulator）。

    AVD 判定訊號：qemu prop、goldfish/ranchu hardware、sdk_gphone 型號、
    fingerprint 含 emu/generic/sdk。任一命中即模擬機。
    """
    qemu = (adb("shell", "getprop", "ro.kernel.qemu") == "1"
            or adb("shell", "getprop", "ro.boot.qemu") == "1")
    hardware = adb("shell", "getprop", "ro.hardware").lower()
    model = adb("shell", "getprop", "ro.product.model").lower()
    fp = adb("shell", "getprop", "ro.build.fingerprint").lower()
    is_emu = (qemu
              or hardware in ("goldfish", "ranchu")
              or "sdk_gphone" in model
              or any(tok in fp for tok in ("emu", "sdk_gphone", "/generic")))
    return "模擬機" if is_emu else "實體機"


def collect_environment():
    """capture 當下的裝置／APK 環境快照 → `environment.json`（骨架）。

    這是報告的 **ground truth 來源**：卡片背面的「INDEPENDENT DEVICE / APP EVIDENCE」
    與 `capture_state_eligible()` 的前置門檻都讀這裡，用來**獨立對照** bid 送的值 ——
    bid 說 `darkmode=true`，環境快照也說 `dark_mode=yes`，才算真的驗到；只看 bid
    自己說什麼不構成證據。

    刻意留空：要記哪些欄位取決於 TC 怎麼定義，一起重新設計。
    原版（約 50 個 adb 查詢）：`git show 8307b56:qa_aos.py`

    讀取側目前預期的 key（`ground_truth_for()` / `device_kind_of()` /
    `provenance_label()` / 報告頂端的環境與前置狀態面板）：

      package version_name version_code first_install_time
      device device_kind android build_fingerprint
      timezone dark_mode battery_saver brightness font_scale
      locale app_locale media_volume
      battery battery_source battery_raw
      root vpn_active vpn_source
      location_permission location_fine location_coarse location_source
      public_ipv6 ipv6_source

    少了某個 key 只會讓對應的 ground-truth 區塊消失，不會爆 —— 但那條 TC 也就只剩
    bid 自己的說法、沒有獨立佐證。
    """
    return {}


# ── state-proof screenshot ──────────────────────────────────────────────────
# 狀態證據：把「看得見該狀態的畫面」叫出來截圖，讓人肉眼驗證，不是拍廣告頁。

# 單一 state TC → 互斥組
# TC → 互斥狀態組（決定 capture_state_proof 要截哪個設定頁）。
# 註：這與 STATE[tc][0] 是同一件事的兩份來源，重建目錄時建議只留一份。
STATE_GROUP = {}

# 組 → (kind, arg, 說明該截圖證明什麼)
#   intent    : am start -a <arg>
#   component : am start -n <pkg/activity>（指定 activity，用於沒有 action 的頁）
#   app       : 啟動某 app（monkey launcher）— 例：Magisk 當 root 佐證
#   appdetails: App 詳情頁（權限 / 廣告 ID）
#   qs        : 下拉快捷面板（亮度滑桿）
#   volpanel  : 只顯示媒體音量面板，不改變音量
#   notif     : 展開狀態列（充電 / VPN / 電量圖示）
#   None      : 截圖無法證明，改寫說明檔
STATE_SURFACE = {
    "darkmode":     ("intent", "android.settings.DISPLAY_SETTINGS", "Display 設定的 Dark theme 開關"),
    "batterysaver": ("intent", "android.settings.BATTERY_SAVER_SETTINGS", "省電模式開關狀態"),
    "batterylevel": ("intent", "android.intent.action.POWER_USAGE_SUMMARY", "電池頁面電量百分比"),
    "charging":     ("intent", "android.intent.action.POWER_USAGE_SUMMARY", "電池頁面充電狀態"),
    "tz":           ("intent", "android.settings.DATE_SETTINGS", "日期時間設定的時區（GMT offset）"),
    "locale":       ("applocale", None, "Sample App 的 App language 設定頁"),
    "geo":          ("appdetails", None, "Sample App 詳情頁的 Permissions 摘要（Location 允許/拒絕）"),
    "vpn":          ("intent", "android.settings.VPN_SETTINGS", "VPN 連線狀態"),
    "fontscale":    ("intent", "android.settings.TEXT_READING_SETTINGS", "Display size and text 頁的 Font size 滑桿"),
    "volume":       ("volpanel", None, "媒體音量面板（僅顯示，不改變 Capture 值）"),
    "screenbright": ("qs", None, "快捷面板亮度滑桿位置"),
    # 完整廣告 ID 頁（Reset/Delete/Get new advertising ID）；GMS 的 ADS_PRIVACY action 只開精簡頁
    "tracking":     ("component", "com.google.android.gms/.adsidentity.settings.AdsIdentitySettingsActivity",
                     "系統廣告 ID 頁：opt-in 顯示 ID+Reset/Delete、opt-out 顯示 Get new advertising ID"),
    # root 機用 Magisk 畫面當佐證（su binary 由 Magisk 提供）
    "jailbreak":    ("app", "com.topjohnwu.magisk", "Magisk app 畫面（root 佐證：版本 / package）"),
    "emulator":     (None, None, "實機 / AVD 需外部佐證（截圖不足以證明）"),
    "session":      (None, None, "session 時長無對應設定頁，靠 bid 值 + 操作時序佐證"),
    "fgbg":         (None, None, "前景/背景切換靠操作時序佐證"),
    # 裝置固有欄位：一頁涵蓋多條，讓實機每條都盡量有截圖
    "deviceinfo":   ("intent", "android.settings.DEVICE_INFO_SETTINGS",
                     "關於手機：品牌 Google / 型號 Pixel 10a / Android 版本 16"),
    "language":     ("intent", "android.settings.LOCALE_SETTINGS",
                     "語言與地區：語言 en、地區 tw、locale en_US"),
    "storage":      ("intent", "android.settings.INTERNAL_STORAGE_SETTINGS",
                     "儲存空間：總容量 / 可用空間（RAM 情境）"),
    "apps":         ("intent", "android.settings.MANAGE_APPLICATIONS",
                     "已安裝應用程式清單（applist 對照）"),
    "network":      ("intent", "android.settings.WIFI_SETTINGS",
                     "連線類型：Wi-Fi（conntype；IP 情境）"),
    "appinfo":      ("appdetails", None, "Sample App 詳情頁：app 版本 1.4.0 / 套件名"),
}


def adb_screencap(path):
    cmd = ["adb"]
    if UDID:
        cmd += ["-s", UDID]
    cmd += ["exec-out", "screencap", "-p"]
    try:
        with open(path, "wb") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.DEVNULL, timeout=15, check=True)
        return os.path.getsize(path) > 0
    except Exception:
        return False


def capture_state_proof(folder):
    """把「看得見該狀態的系統畫面」叫出來截圖 → `state_proof_<group>.png`（骨架）。

    這是狀態類 TC 的**肉眼證據**：不是拍廣告頁，而是拍設定頁 —— 驗 darkmode 就開
    Display 設定拍那個開關。bid 已經在正確狀態下送出（本函式在 capture 之後才跑），
    這裡只負責把畫面叫出來。回傳 `{group: caption}`。

    刻意留空：哪條 TC 要哪張證據，跟 TC 定義一起重新設計。驅動它的 `STATE_GROUP`
    （TC → 互斥組）本來就已經是空的，所以原版留著也不會動。
    `STATE_SURFACE`（組 → 用哪個 intent／component 開哪一頁）**保留未清** ——
    那是裝置知識（哪個 action 開哪個設定頁）而不是 TC 定義。
    原版（含 tracking／volume／brightness 那些拿不到畫面的特例處理）：
    `git show 8307b56:qa_aos.py`

    重建時的形狀：
      capture_state_proof(folder)         -> {group: caption}
        由 TC_ID 經 STATE_GROUP 推出要截哪幾組，逐組呼叫下面那支
      _capture_group_proof(folder, group) -> caption | None
        依 STATE_SURFACE[group] 的 kind（intent / component / app / appdetails /
        qs / volpanel / notif / None）把畫面帶出來，`adb_screencap()` 存成
        state_proof_<group>.png，附 _meta.json；截圖不足以證明的組（emulator、
        session、fgbg）改寫 state_proof_<group>.txt 說明為什麼。
    """
    return {}


def _capture_group_proof(folder, group):
    """單一互斥組的狀態證據（骨架）。見 `capture_state_proof()` 的說明。"""
    return None


# ── evidence bundle ───────────────────────────────────────────────────────────

# ── TC-11: privacy icon 自動點擊 ─────────────────────────────────────────────

def _traffic_line_count():
    if not os.path.exists(TRAFFIC_FILE):
        return 0
    with open(TRAFFIC_FILE) as f:
        return sum(1 for _ in f)


def do_privacy_click(driver, folder):
    """點 privacy information icon → 等 adpolicy.appier.com 流量 → 截落地畫面。

    必須在 phone.png / ad_ui.xml 保存後、TRAFFIC_FILE 歸檔前呼叫：
    點擊會離開廣告畫面，落地流量要趕在 traffic.jsonl 複製前寫入。
    結束時把 app 拉回前景，不影響後續 state proof。
    """
    result = {"tapped": False, "adpolicy": None, "focus_after": ""}
    icon_id = f"{APP_PACKAGE}:id/native_privacy_information_icon_image"
    elem = None
    for locator in ((AppiumBy.ID, icon_id),
                    (AppiumBy.ANDROID_UIAUTOMATOR,
                     'new UiSelector().resourceIdMatches(".*privacy_information_icon.*")')):
        try:
            elem = driver.find_element(*locator)
            break
        except Exception:
            continue
    if elem is None:
        print("  [privacy] icon 不在畫面上，略過 TC-11 點擊")
        return result

    before = _traffic_line_count()
    elem.click()
    result["tapped"] = True
    print("  [privacy] icon 已點擊，等待 adpolicy.appier.com 流量 ...")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if os.path.exists(TRAFFIC_FILE):
            new_rows = open(TRAFFIC_FILE).read().splitlines()[before:]
            hit = next((r for r in new_rows if "adpolicy.appier.com" in r), None)
            if hit:
                try:
                    result["adpolicy"] = json.loads(hit)
                except Exception:
                    result["adpolicy"] = {"raw": hit}
                break
        time.sleep(0.5)

    time.sleep(2.0)  # 落地頁 render
    adb_screencap(str(folder / "privacy_landing.png"))
    focus = adb("shell", "dumpsys", "window")
    m = re.search(r"(mCurrentFocus|mFocusedApp)=.*", focus)
    result["focus_after"] = m.group(0).strip() if m else ""
    with open(folder / "privacy_click.json", "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if result["adpolicy"]:
        print(f"  [privacy] adpolicy → {result['adpolicy'].get('status')}；privacy_landing.png 已存")
    else:
        print("  [privacy] proxy 未錄到 adpolicy 流量（可能 TLS passthrough／瀏覽器不走代理），"
              "靠 privacy_landing.png 人工核對")

    # privacy icon 開的是 Appier 內建瀏覽器（AppierBrowserActivity）：BACK 出瀏覽器、
    # 返回「同一個廣告」頁（NativeBasicActivity），後續 E2E 點擊才有版位可點。
    # 不能用 am start MainActivity（回選單廣告就沒了）；也不能停在 Browser。
    # mediation 的 privacy 連結可能開 Appier 內建瀏覽器或「外部 Chrome」
    # （com.android.chrome/ChromeTabbedActivity）；兩者都要 BACK 退出才回得到廣告頁。
    for _ in range(5):
        focus_line = next((l for l in adb("shell", "dumpsys", "window").splitlines()
                           if "mCurrentFocus" in l), "")
        on_browser = any(b in focus_line for b in
                         ("BrowserActivity", "ChromeTabbedActivity", "com.android.chrome"))
        on_ad = APP_PACKAGE in focus_line and not on_browser and "MainActivity" not in focus_line
        if on_ad:
            break
        adb("shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(1.0)
    return result


# ── E2E 流程逐步截圖 + 點擊手勢 ──────────────────────────────────────────────

E2E_INIT_TMP = "/tmp/appier_e2e_init.png"   # app 啟動當下截圖（在 folder 建立前先落地）


def do_e2e_flow(driver, folder):
    """跑完整 E2E 流程並逐步截圖：③ 渲染 → ⑤ 點擊手勢 → ⑥ 落地。

    ① init 截圖在 main() app 啟動時已存到 E2E_INIT_TMP，這裡搬進 folder。
    測試廣告環境，點擊直接執行（不設核准 gate）；點擊會打 xclk，
    detector 已把流量寫進 traffic.jsonl，此處負責截圖與前景記錄。
    """
    result = {"clicked": False, "xclk": None, "focus_after": "", "steps": []}

    # ① init：把 app 啟動截圖搬進本 capture
    if os.path.exists(E2E_INIT_TMP):
        shutil.copy(E2E_INIT_TMP, folder / "e2e_step_init.png")
        result["steps"].append("init")

    # ③ render：廣告渲染畫面（點擊前）
    adb_screencap(str(folder / "e2e_step_render.png"))
    result["steps"].append("render")

    # ⑤ click：tap 廣告主圖／CTA 觸發 xclk。
    # 可點元素在 sample app 自己的 native_ad_view 內，standalone 與 admob/applovin
    # mediation 共用同一組 resource-id（2026-07-21 對 admob ad_ui.xml 確認）。
    before = _traffic_line_count()
    click_ids = ("native_main_image", "native_cta", "native_ad_view",
                 "native_icon_image", "native_title")

    def _find_ad_target():
        for rid in click_ids:
            try:
                return driver.find_element(AppiumBy.ID, f"{APP_PACKAGE}:id/{rid}")
            except Exception:
                continue
        return None

    target = _find_ad_target()
    # 前一步 privacy click（TC-11）可能把畫面留在瀏覽器/他處（mediation 尤其）；
    # BACK 退回廣告頁再找，最多 4 次。
    for _ in range(4):
        if target is not None:
            break
        adb("shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(1.0)
        target = _find_ad_target()
    if target is None:
        try:
            present = sorted(set(re.findall(r'resource-id="([^"]*native[^"]*)"',
                                            driver.page_source)))
        except Exception:
            present = []
        print(f"  [e2e] 找不到廣告可點元素，略過點擊步驟（目前畫面 native_* id：{present}）")
        with open(folder / "e2e_flow.json", "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return result

    target.click()
    result["clicked"] = True
    print("  [e2e] 已點擊廣告，等待 xclk 點擊鏈 ...")
    time.sleep(1.5)
    adb_screencap(str(folder / "e2e_step_click.png"))     # 點擊當下
    result["steps"].append("click")

    # ⑥ landing：等 deeplink 直開 target app / 落地頁 render
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if os.path.exists(TRAFFIC_FILE):
            new_rows = open(TRAFFIC_FILE).read().splitlines()[before:]
            hit = next((r for r in new_rows if "/xclk" in r), None)
            if hit:
                try:
                    result["xclk"] = json.loads(hit)
                except Exception:
                    result["xclk"] = {"raw": hit}
                break
        time.sleep(0.5)
    time.sleep(2.5)  # 落地頁／target app render
    adb_screencap(str(folder / "e2e_step_landing.png"))
    result["steps"].append("landing")

    focus = adb("shell", "dumpsys", "window")
    m = re.search(r"(mCurrentFocus|mFocusedApp|topResumedActivity)=.*", focus)
    result["focus_after"] = m.group(0).strip() if m else ""
    with open(folder / "e2e_flow.json", "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  [e2e] xclk={'✓' if result['xclk'] else '未錄到'}；"
          f"落地前景={result['focus_after'][:80]}")

    # 拉回 app 前景收尾
    adb("shell", "am", "start", "-n", f"{APP_PACKAGE}/{APP_ACTIVITY}")
    time.sleep(1.5)
    return result


# ── user.session_duration 三情境（AND-47-1/2/3）────────────────────────────────
# session_duration＝使用者 App 在前景的累積時間（iOS 實作即此語意），
# 不是廣告 session 載入時間。每個 bid request 都帶此值 → 用「bid A → 動作 → bid B」
# 的相對變化驗行為；bid B 不要求命中 TEST_CID（204 no-bid 的 request 也帶 session）。

# session_duration 情境 → (動作說明, 通過標準)。這是該 TC 的通過標準，重建時填回。
SESSION_CASE_SPEC = {}


def _session_value(bid):
    """從 bid payload 取 user.session_duration（毫秒）。"""
    value, found = get_field(_unwrap(bid), "user.session_duration")
    return value if found else None


def do_session_case(driver, case):
    """bid A 已在 BID_FILE：做情境動作 → 再觸發 bid B → 對照寫 SESSION_CASE_FILE。"""
    action_desc, expected = SESSION_CASE_SPEC[case]
    with open(BID_FILE) as f:
        bid_a = json.load(f)
    session_a = _session_value(bid_a)
    shutil.copy(BID_FILE, SESSION_BID_A_FILE)
    print(f"\n[session case {case}] bid A session_duration={session_a}；動作：{action_desc}")

    # bid A 階段 logcat 另存；重啟側錄，bid B 掃描才不會撈到 A 的 payload
    stop_logcat()
    if os.path.exists(LOGCAT_TMP):
        shutil.copy(LOGCAT_TMP, SESSION_LOGCAT_A)
    for f in (FLAG_FILE, BID_FILE, BID_STATUS_FILE, BID_RESPONSE_FILE):
        if os.path.exists(f):
            os.remove(f)
    start_logcat()

    if case == 1:
        driver.back()                                     # 只關廣告頁
        print(f"    前景停留 {SESSION_GAP_SEC:.0f}s 累積 session ...")
        time.sleep(SESSION_GAP_SEC)
    elif case == 2:
        adb("shell", "am", "force-stop", APP_PACKAGE)     # 關整個 App
        time.sleep(2)
        adb("shell", "am", "start", "-n", f"{APP_PACKAGE}/{APP_ACTIVITY}")
        time.sleep(3)                                     # 重開後盡快觸發，session 應接近 0
    else:
        adb("shell", "input", "keyevent", "KEYCODE_HOME")  # 退背景
        time.sleep(SESSION_GAP_SEC)
        adb("shell", "monkey", "-p", APP_PACKAGE,
            "-c", "android.intent.category.LAUNCHER", "1")  # 切回前景
        time.sleep(2)

    session_b = None
    for attempt in range(1, 4):
        print(f"[session case {case}] 觸發 bid B attempt {attempt} ...")
        tapped = False
        for _ in range(3):
            try:
                if tap_trigger(driver):
                    tapped = True
                    break
            except Exception:
                pass
            driver.back()
            time.sleep(0.8)
        if not tapped:
            adb("shell", "am", "start", "-n", f"{APP_PACKAGE}/{APP_ACTIVITY}")
            time.sleep(AD_RETRY_DELAY)
            continue
        deadline = time.monotonic() + BID_TIMEOUT
        bid_b = None
        while time.monotonic() < deadline:
            bid_b, _ = scan_logcat_bid()
            if bid_b is not None:
                break
            time.sleep(0.2)
        if bid_b is not None:
            with open(BID_FILE, "w") as f:      # bid B 即本 capture 的 bid_request.json
                json.dump(bid_b, f, indent=2)
            session_b = _session_value(bid_b)
            break
        driver.back()
        time.sleep(AD_RETRY_DELAY)

    if session_b is None and os.path.exists(SESSION_BID_A_FILE):
        # bid B 沒抓到：還原 bid A 當本 capture 的 bid_request.json，
        # results.json 才會落地（報告端靠它配對 capture），判定記無法對照
        shutil.copy(SESSION_BID_A_FILE, BID_FILE)

    passed = None
    if isinstance(session_a, (int, float)) and isinstance(session_b, (int, float)):
        passed = (session_b < session_a) if case == 2 else (session_b > session_a)
    payload = {
        "case": case, "tc": SESSION_TCS.get(case),
        "action": action_desc, "expected": expected,
        "gap_sec": SESSION_GAP_SEC, "unit": "ms",
        "session_a": session_a, "session_b": session_b,
        "passed": passed,
    }
    with open(SESSION_CASE_FILE, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    verdict_txt = ("PASS" if passed else "FAIL") if passed is not None else "無法判定（缺 session 值）"
    print(f"[session case {case}] A={session_a} → B={session_b}（預期 {expected}）→ {verdict_txt}")
    return payload


def save_evidence(driver, ts):
    """把這一次 capture 的證據落地（骨架）。

    刻意留空：每條 TC 該有什麼證據，跟 TC 定義一起重新設計。
    這裡只保留結構 —— **一次 capture ＝ 一個資料夾**，資料夾名＝批次名＋timestamp
    （`CURRENT_<ts>` / `CTRL1_<ts>` / `AND-04+AND-06_<ts>` / `*_RETRY<n>`）。
    原版：`git show 8307b56:qa_aos.py`

    ⚠️ **檔名是雙邊契約，改了要兩邊一起改。** 讀取側 —— `load_captures()`、
    `capture_candidates()`、`build()`、`_json_file()`、`_traffic()`、`_logcat()`、
    `read_round_elapsed()` —— 仍然硬編下面這些檔名。只改寫入側的話，報告會一條都
    對不上、全部被判 BLOCKED（而且看起來像「這輪沒做」，不像「檔名改了」）。

    原版寫進 capture 資料夾的東西（重新設計時的對照表）：

      判定必需（少了報告就對不上）
        results.json    {tc_id, captured_at, app, test_type, test_cid, test_mode,
                        test_executor, environment, results[]}
                        ← `load_captures()` 靠 glob `*/results.json` 找 capture；
                          `tc_id` 宣告這批跑了哪些 TC（`declared()` 讀它）；
                          `captured_at` 是時間敏感 check 的基準（**不可**退回檔案
                          mtime，搬過 evidence 就失真）
        bid_request.json  原始 bid。報告端一律用當前規則重算，不信 results.json
                          裡的舊判定值
      bid 相關
        first_bid_request.json  冷啟第一發（`FIRST_BID_TCS` 的 TC 用這份）
        bid_response.json       200 才有
        bid_ids.json            {bidobjid, cid, crid, crpid}
        ext_enc_raw.txt / ext_enc_decoded.json / ext_enc_all_fields.json /
        ext_enc_compare.txt     由 `apr_xorenc.write_evidence()` 產出
      裝置與環境
        environment.json  `collect_environment()` 的輸出；報告的 ground truth 來源
        device_state.txt  `snapshot_device_state()` 的文字快照
      畫面
        phone.png                     bid 當下的 app 畫面
        ad_ui.xml                     廣告渲染的 UI dump（用 driver.page_source，
                                      不能用外部 uiautomator dump —— Appium session
                                      活著時會搶不到 accessibility）
        state_proof_<group>.png/.txt/.xml/_meta.json
        state_proof_captions.json     {group: 這張截圖證明什麼}
      流程
        privacy_landing.png / privacy_click.json      `do_privacy_click()`
        e2e_step_render.png / e2e_step_click.png / e2e_step_landing.png /
        e2e_flow.json                                 `do_e2e_flow()`
        session_case.json / session_bid_a.json / logcat_session_a.txt
                                                      `do_session_case()`
        state_action.txt   本次實際做了什麼（實機設定／adb 模擬 real→mock）
      log
        logcat.txt / logcat_appier.txt
        traffic.jsonl      detector 的全流量
      round 層級（寫在 round_dir，不在 capture 資料夾裡）
        round_report.txt   `aggregate_round()` + `format_round_report()`
        e2e_results.json   `evaluate()` 的輸出
        round_timing.txt   由 `__main__` 累寫

    重建時要保留的兩個順序約束（原版踩出來的）：
      1. privacy 點擊與 E2E 點擊都必須在 traffic.jsonl 歸檔**之前**做，否則點擊
         產生的流量不會進證據。
      2. mediation 模式**先跑 E2E 點擊再跑 privacy** —— mediation 的 privacy 連結會
         開外部 Chrome，round-trip 回來後廣告 view 不再 render，之後就找不到版位
         可點。standalone 相反（privacy 先、E2E 後）。
    """
    round_dir = resolve_round_dir()
    capture_name = (CAPTURE_LABEL or
                    ("CURRENT" if TC_ID == "CURRENT" else TC_ID.replace(",", "+")))
    folder = round_dir / f"{capture_name}_{ts}"
    folder.mkdir(parents=True, exist_ok=True)
    print("  [骨架] 證據落地未實作：只建了 capture 資料夾，沒寫入任何檔案。")
    print("         沒有 results.json，報告端不會把它當成 capture（見本函式 docstring）。")
    return folder


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if not APP_PACKAGE or not APP_ACTIVITY:
        sys.exit(
            "Required env vars not set:\n"
            "  export APP_PACKAGE=com.appier.ssp.sample\n"
            "  export APP_ACTIVITY=com.appier.ssp.MainActivity"
        )

    global UDID
    t_start = time.monotonic()      # PHASE_TIMEOUT_SEC 的起點（含 Appium 啟動時間）
    UDID = udid = detect_udid()
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    test_type = resolve_test_type()
    test_mode = resolve_test_mode()
    test_cid = resolve_test_cid()

    if FULL_ROUND:
        # 完整 round：本 process 只排程，每個 capture 由 ssp_round 另開 process／
        # 新 Appium session（狀態與 logcat 不互相污染）。
        os.environ["UDID"] = udid          # 讓佈狀態與子 capture 都鎖同一台
        os.environ["TEST_ROUND"] = TEST_ROUND
        return run_round({"TEST_TYPE": test_type, "TEST_MODE": test_mode,
                              "TEST_CID": test_cid, "TEST_ROUND": TEST_ROUND},
                             scope=("signal" if SIGNAL_ONLY else
                                    "e2e" if E2E_ONLY else "all"))

    print(f"[device] {udid}")
    print(f"[type  ] {test_type}")
    print(f"[mode  ] {test_mode}")
    print(f"[CID   ] {test_cid}")
    print(f"[by    ] {TEST_EXECUTOR}")
    print(f"[round ] {TEST_ROUND}")
    print(f"[TC    ] {TC_ID}")
    print(f"[app   ] {APP_PACKAGE}")
    if TRIGGER_TEXT:
        print(f"[tap   ] '{TRIGGER_TEXT}'")
    print()

    # clear previous flags（TRAFFIC_FILE 只在 session 開始清一次，
    # 讓 app 啟動到 capture 的完整流量都留在 log 裡）
    for f in (FLAG_FILE, BID_FILE, FIRST_BID_FILE, BID_STATUS_FILE,
              BID_RESPONSE_FILE, TRAFFIC_FILE,
              SESSION_CASE_FILE, SESSION_BID_A_FILE, SESSION_LOGCAT_A):
        if os.path.exists(f):
            os.remove(f)

    # force-stop before Appium connects — Appium will launch on connect
    print("[→] force-stop ...")
    adb("shell", "am", "force-stop", APP_PACKAGE)
    time.sleep(0.5)

    print("[→] logcat recording ...")
    start_logcat()

    options = UiAutomator2Options()
    options.app_package  = APP_PACKAGE
    options.app_activity = APP_ACTIVITY
    options.no_reset     = True
    options.udid         = udid

    print("[→] launching via Appium ...")
    driver = webdriver.Remote(APPIUM_URL, options=options)
    time.sleep(2.0)

    # 佈狀態與 state proof 會把 Settings / Tailscale 等 app 留在前景；即使
    # Appium session 已建立，也要顯式拉回受測 app，避免在外部 app 找 tab。
    driver.activate_app(APP_PACKAGE)
    time.sleep(1.0)

    # TEST_MODE 不只寫入報告：先切到對應 SDK integration tab，之後所有同名
    # trigger 也都以畫面座標過濾，避免點到 ViewPager 預載的相鄰分頁。
    select_test_mode_tab(driver)

    # E2E ① init：app 剛啟動的畫面（folder 尚未建立，先落地到暫存，save_evidence 再搬入）
    if DO_E2E_FLOW:
        if adb_screencap(E2E_INIT_TMP):
            print(f"  [e2e] init 截圖 → {E2E_INIT_TMP}")

    try:
        # 前景停留（session_duration / app_duration 類 TC 需累積使用時間）
        if DWELL_SEC > 0:
            print(f"[→] 前景停留 {DWELL_SEC:.0f}s ...")
            time.sleep(DWELL_SEC)

        if DO_FGBG:
            print("[→] 自動執行背景 → 前景切換 ...")
            adb("shell", "input", "keyevent", "KEYCODE_HOME")
            time.sleep(2)
            adb("shell", "monkey", "-p", APP_PACKAGE,
                "-c", "android.intent.category.LAUNCHER", "1")
            time.sleep(2)

        attempt = 0
        while True:
            attempt += 1
            waited = time.monotonic() - t_start
            if PHASE_TIMEOUT_SEC and waited > PHASE_TIMEOUT_SEC:
                print(f"\n[逾時] 已跑 {waited / 60:.1f} 分鐘（上限 "
                      f"{PHASE_TIMEOUT_SEC / 60:.0f} 分鐘）仍未命中指定 CID：{TEST_CID}")
                print("       乾淨收尾，不建立 Capture；此批 TC 會標「本輪未執行」。"
                      "調整上限用 PHASE_TIMEOUT_SEC（0＝不限）。")
                return 5
            if MAX_AD_ATTEMPTS and attempt > MAX_AD_ATTEMPTS:
                print(f"\n[停止] 已刷 {MAX_AD_ATTEMPTS} 次，仍未命中指定 CID：{TEST_CID}")
                print("       請檢查廣告流量／campaign 狀態、CID 投遞條件、"
                      "Tailscale 台灣 Office VPN（tpe-exit-3）與 GAID 狀態後再試。")
                return 4

            if attempt > 1:
                stop_logcat()
                for f in (FLAG_FILE, BID_FILE, BID_STATUS_FILE, BID_RESPONSE_FILE):
                    if os.path.exists(f):
                        os.remove(f)
                start_logcat()
                driver.back()
                time.sleep(1.2)

            print(f"[→] 刷廣告 attempt {attempt}：tap '{TRIGGER_TEXT}' ...")
            tapped = False
            for _ in range(3):
                try:
                    if tap_trigger(driver):
                        tapped = True
                        break
                except Exception:
                    pass
                driver.back()
                time.sleep(0.8)
            if not tapped:
                print("    [retry] 找不到指定版位，重新拉回 app 前景後重試。")
                # 其他 app（如 Tailscale）搶走前景時，back 無法復原；直接重新帶起主畫面
                adb("shell", "am", "start", "-n", f"{APP_PACKAGE}/{APP_ACTIVITY}")
                time.sleep(AD_RETRY_DELAY)
                continue

            print(f"[→] waiting for bid request (timeout {BID_TIMEOUT}s) ...")
            deadline = time.monotonic() + BID_TIMEOUT
            bid = None
            while time.monotonic() < deadline:
                if os.path.exists(FLAG_FILE):
                    break
                bid, _ = scan_logcat_bid()
                if bid is not None:
                    break
                time.sleep(0.2)

            if os.path.exists(FLAG_FILE):
                hit = open(FLAG_FILE).read().strip()
                time.sleep(1.0)
                status = (open(BID_STATUS_FILE).read().strip()
                          if os.path.exists(BID_STATUS_FILE) else "?")
                source = "proxy"
            else:
                time.sleep(1.0)
                bid, status = scan_logcat_bid()
                if bid is None:
                    print("    [retry] 沒偵測到 bid request。")
                    time.sleep(AD_RETRY_DELAY)
                    continue
                with open(BID_FILE, "w") as f:
                    json.dump(bid, f, indent=2)
                if status:
                    with open(BID_STATUS_FILE, "w") as f:
                        f.write(status)
                hit = "POST /v2/sdk/aos/ad (from logcat)"
                source = "logcat"

            if attempt == 1 and os.path.exists(BID_FILE):
                shutil.copy(BID_FILE, FIRST_BID_FILE)

            ad_identity = scan_logcat_ad_identity()
            if SAVE_ON_BID and os.path.exists(BID_FILE):
                # request payload 即證據；response/CID 不作為入庫條件
                if not ad_identity:
                    ad_identity = {"cid": "(no-win)", "crid": "(no-win)"}
                print(f"    [SAVE_ON_BID] bid request 已取得（response={status or 'unknown'}），入庫。")
                break
            if status != "200":
                print(f"    [retry] response={status or 'unknown'}，未命中廣告。")
            elif not ad_identity:
                print("    [retry] loaded ad identity 不明，不能 Capture。")
            elif TEST_CID and ad_identity["cid"] != TEST_CID:
                print(f"    [retry] CID 不符：expected={TEST_CID}, actual={ad_identity['cid']}")
            else:
                break
            time.sleep(AD_RETRY_DELAY)

        print(f"\n[CAPTURED via {source}] {hit}  (response: {status or 'unknown'}, "
              f"cid={ad_identity['cid']}, crid={ad_identity['crid']})\n")
        if status == "204":
            print("[判定] server 回 204 no-bid — 連線正常，目前沒有廣告可刷"
                  "（campaign 沒投遞 / 沒 fill）；bid request 仍已留存可驗欄位。\n")

        # session_duration 三情境：bid A 到手後做情境動作、抓 bid B 對照
        if SESSION_CASE in ("1", "2", "3"):
            try:
                do_session_case(driver, int(SESSION_CASE))
            except Exception as exc:
                print(f"[warn] session case 執行失敗（bid A 證據仍保留）：{exc}")

        # save evidence
        print("[→] saving evidence ...")
        folder = save_evidence(driver, ts)
        print(f"\n[DONE] {folder}/")
        result_code = 3 if status == "204" else 0

    finally:
        stop_logcat()
        try:
            driver.quit()
        except Exception as exc:
            # session 逾時/已死時 quit 會丟例外；證據已存完，不能讓收尾失敗把
            # 本 round 標成未完成
            print(f"[warn] driver.quit() 失敗（不影響已存證據）：{exc}")

    # 發布可能耗時超過 Appium newCommandTimeout，必須在 quit 後才做；否則
    # session 會在 git push 期間過期，污染 round 的下一個 capture。
    auto_publish()
    return result_code


# ════════════════════════════════════════════════════════════════════════════
# 入口（單一 CLI：跑一輪／補跑 TC／重算報告／離線驗證）
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if TOOL_MODE == "--report":
        _cli_report(sys.argv[1:])
        sys.exit(0)
    if TOOL_MODE in ("--inspect", "--inspect-round"):
        _cli_inspect(sys.argv[1:])
        sys.exit(0)

    _t0 = time.monotonic()
    _rc = main() or 0
    _elapsed = time.monotonic() - _t0
    _mins, _secs = divmod(int(_elapsed), 60)
    _hms = f"{_mins}m{_secs:02d}s"
    print(f"\n[整體耗時] 本次 {TC_ID} 共 {_hms}（{_elapsed:.1f}s），exit={_rc}")
    try:
        with open(resolve_round_dir() / "round_timing.txt", "a") as _tf:
            _tf.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {TC_ID}  {_hms}  exit={_rc}\n")
    except Exception:
        pass
    sys.exit(_rc)
