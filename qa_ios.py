#!/usr/bin/env python3
"""qa_ios.py — iOS SSP Signal QA：單檔完整流程。

用法：
    python qa_ios.py [IOS-04[,IOS-06]] [UDID]     實機 capture（不帶 TC＝驗全部）
    python qa_ios.py --report <round_dir> [--out x.html] [--meta m.json]
    python qa_ios.py --inspect <bid.json> / --inspect-round <dir>

架構規則（勿違反）：
  * 本檔與 qa_aos.py **零 import**，兩平台完全獨立、可各自單獨執行完畢
  * 本檔只 import apr_xorenc（SDK 的 ae1 加解密＝規格，兩平台必須一致）
  * 跨平台整合頁與發佈在 page.py；本檔用 subprocess 呼叫它，不 import
  * 下方「從 AOS 複製」區塊是刻意的重複，換來平台獨立；改動時請評估另一平台

已知落差：iOS 目前**沒有**完整 round 排程（AOS 的 M1/M2/M3/SC/AUTO 狀態批次），
單次執行只覆蓋不需佈狀態的欄位；狀態類 TC 需逐條人工佈。待補。

前置服務：
    mitmdump -s mitmdump_addon.py --listen-port 8081
    appium（WebDriverAgent 已簽：設 XCODE_ORG_ID）
    手機 Wi-Fi proxy → Mac IP:8888（Charles），Charles upstream → 127.0.0.1:8081
"""

import argparse
import getpass
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

from appium import webdriver
from appium.options.ios.xcuitest.base import XCUITestOptions
from datetime import datetime
from pathlib import Path

# 下方「從 AOS 複製」區塊需要的標準庫（encode_shot 用 PIL/io/base64、esc 用 html、
# _deep_merge 用 copy）
import base64
import copy
import html
import io

try:
    from PIL import Image
except Exception:
    Image = None


from verdict import (                                          # 共用判定/報告契約
    classify,
    tier_of,
    render_card,
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

# ════════════════════════════════════════════════════════════════════════════
# 仍需與 AOS 對齊的區塊（無法純參數化者）
#
#   **刻意複製**，不是疏漏。架構決定：qa_aos.py 與本檔零 import，兩平台
#   完全獨立、可各自單獨執行完畢，改一邊不會靜默弄壞另一邊。
#   代價：這些定義有兩份。改動任何一份時，請一併評估另一個平台是否也要改
#   （尤其 run_validator 的 check 實作與 render_card/CSS 的版面）。
#   唯一不複製的是 apr_xorenc（SDK 的 ae1 加解密＝規格，兩平台必須一致）。
# ════════════════════════════════════════════════════════════════════════════



def _decode_ext_enc(bid):
    """Decode the real AOS Signal payload embedded in the request, if present."""
    if not isinstance(bid, dict) or not bid.get("ext_enc"):
        return None
    from apr_xorenc import decode_ext_enc
    _raw, decoded = decode_ext_enc(bid)
    return decoded






















































# iOS 覆寫版 run_validator 會呼叫這個別名取用上面的 AOS 基礎實作
_base_run_validator = run_validator


# ── 發佈（交給 page.py，維持平台檔零 import）──────────────────────────────────

def auto_publish():
    """重產跨平台整合頁並推上 gh-pages；用 subprocess 以維持平台檔零 import。"""
    if os.environ.get("AUTO_PUBLISH", "1") == "0":
        print("[publish] AUTO_PUBLISH=0，略過 GitHub Pages")
        return None
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.py")
    result = subprocess.run([sys.executable, script, "--publish"], check=False)
    if result.returncode:
        print(f"[warn] GitHub Pages 發佈失敗（evidence 已保存，exit {result.returncode}）")
    return None



# ════════════════════════════════════════════════════════════════════════════
# iOS Signal validator（IOS-xx 規則、ext_enc/req_enc normalize）
#   （原 ios_bid_inspector.py）
# ════════════════════════════════════════════════════════════════════════════

# 重用 AOS 引擎的純元件（平台無關）

# ── iOS 專用 regex ────────────────────────────────────────────────────────────
# IDFA / IDFV 在 iOS 是大寫 hex UUID（e.g. AEBE52E7-03EE-455A-B3C4-...），
# AOS 的 UUID_RE 只吃小寫，故 iOS 用不分大小寫版本。
UUID_CI_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
IOS_OS_RE = re.compile(r"^ios$", re.IGNORECASE)


# ── check 擴充：IDFA/IDFV 大小寫不敏感的非零 UUID ──────────────────────────────
def run_validator(bid, v, reference_ms=None):
    """iOS 專用 check 先攔，其餘一律委派給 AOS 引擎（run_validator）。"""
    check = v["check"]
    if check == "uuid_ci_nonzero":
        value, found = get_field(bid, v["field"])
        if not found or value is None:
            return False, None, "field missing"
        ok = (isinstance(value, str) and bool(UUID_CI_RE.fullmatch(value))
              and value.upper() != ZERO_UUID.upper())
        return ((True, value, "valid non-zero UUID ✓") if ok
                else (False, value, "expected non-zero UUID (IDFA/IDFV，不分大小寫)"))
    return _base_run_validator(bid, v, reference_ms=reference_ms)


# ── iOS TC validator 表 ───────────────────────────────────────────────────────
# cal=True → 待實機校準（路徑或期望值）。disp = 對照 AOS 的處置。
# iOS Signal validator（IOS-xx）：欄位路徑、check 類型、期望值。從 0 逐條填回。
IOS_VALIDATORS = []

# 狀態切換類 TC → (互斥組, 如何設定, 截圖該證明什麼)。對照 AOS STATE，iOS 用詞。
# 跑這些 TC 時 run_qa_ios 會導航到對應 iOS 設定頁截圖（state_proof_<group>.png），
# 報告端讓該卡用這張「設定當下」截圖，而非廣告畫面。
# 狀態切換類 TC → (互斥組, 怎麼設定, 截圖該證明什麼)。
IOS_STATE = {}


# baseline capture 該自動驗的 TC（非狀態切換類；對照 AOS AUTO_TCS 翻譯 + iOS 新增）
# 不需佈狀態即可驗的 TC（對應 AOS 的 CURRENT_TCS）。
AUTO_TCS = set()


# ── iOS 加密包解碼（2026-07-21 對真實 bid 確認結構）──────────────────────────────
# iOS bid 明文 body = {zone_id, req_ver, ext_enc, req_enc}：
#   ext_enc → {device, user}                  ← data-signal payload（多數 Signal TC 驗這裡）
#   req_enc → {compliance, app, device, skadn} ← ads SDK 的 req 區塊
# 都用 apr_xorenc 的 ae1 XOR 解碼。normalize 成 bid_inspector 認得的形狀：
#   ext（signal root）＋ req（raw root 的 req.*）＋頂層 skadn / zone_id / req_ver。
def normalize_ios_bid(body):
    if not isinstance(body, dict) or "ext_enc" not in body:
        return body
    from apr_xorenc import decode_bid          # 單一解密入口（與 AOS 共用）
    out = {}
    for k in ("zone_id", "req_ver", "test_mode"):
        if k in body:
            out[k] = body[k]
    parts = decode_bid(body)
    ext, req = parts.get("ext"), parts.get("req")   # ext={device,user}／req={compliance,app,device,skadn}
    if isinstance(ext, dict):
        out["ext"] = ext
    if isinstance(req, dict):
        out["req"] = req
        if isinstance(req.get("skadn"), dict):
            out["skadn"] = req["skadn"]        # skadn 提到頂層供 IOS-81（root:raw）驗
    return out


# ── inspection / aggregation（引用 IOS_VALIDATORS）─────────────────────────────
def run_inspection(bid, tc_filter=None, reference_ms=None):
    bid = normalize_ios_bid(bid)               # iOS 加密包先解碼展開
    root = _unwrap(bid)
    results = []
    for v in IOS_VALIDATORS:
        if tc_filter and v["tc"] not in tc_filter:
            continue
        if v["check"] == "session_case":
            continue   # 跨 bid 對照，單一 bid 無法判定（同 AOS）
        source = bid if v.get("root") == "raw" else root
        passed, actual, msg = run_validator(source, v, reference_ms=reference_ms)
        note = v.get("note", "")
        if v.get("cal"):
            note = ("[待校準] " + note) if note else "[待校準]"
        results.append({
            "tc": v["tc"], "field": v["field"], "passed": passed,
            "actual": actual, "msg": msg, "note": note,
        })
    return results


def aggregate_round(round_dir):
    """彙總 round 內每個 capture 的 results.json，最新 capture 覆蓋同 (tc,field)。"""
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
    for v in IOS_VALIDATORS:
        row = entries.get((v["tc"], v["field"]))
        if row is not None and row not in ordered:
            ordered.append(row)
    return ordered


def format_round_report(rows, round_name=""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    W = 104
    lines = [
        "=" * W,
        f"  iOS SSP Bid Round Report — {round_name}  —  generated {ts}",
        "  每條 check 取該 round 內最新一次 capture 的結果（[待校準]＝欄位/期望值需實機確認）",
        "=" * W, "",
        f"{'TC':<9}  {'Field':<32}  {'Actual':<22}  {'Result':<7}  Capture",
        f"{'─'*9}  {'─'*32}  {'─'*22}  {'─'*7}  {'─'*24}",
    ]
    # cal=True 的欄位＝尚待實機校準，判定等同平台的「待校準/CAL」（BLOCKED-equiv），
    # 不算 FAIL——與 build_artifact_ios._card_from_result 一致，避免 round_report 與平台數字打架。
    cal_fields = {(v["tc"], v["field"]) for v in IOS_VALIDATORS if v.get("cal")}
    passed = failed = caln = 0
    for r in rows:
        if r["passed"]:
            status = "PASS ✓"
            passed += 1
        elif (r["tc"], r["field"]) in cal_fields:
            status = "CAL ⚑"          # 待校準：不計入 failed（與平台 CAL 一致）
            caln += 1
        else:
            status = "FAIL ✗"
            failed += 1
        lines.append(
            f"{r['tc']:<9}  {r['field']:<32}  {_trunc(r['actual'], 20):<22}  {status:<7}  {r['capture']}")
        if not r["passed"] and r.get("note"):
            lines.append(f"{'':9}  ↳ {r['note']}")
    covered = {(r["tc"], r["field"]) for r in rows}
    missing = sorted({v["tc"] for v in IOS_VALIDATORS if (v["tc"], v["field"]) not in covered})
    cal = sorted({v["tc"] for v in IOS_VALIDATORS if v.get("cal")})
    lines += [
        "─" * W,
        f"  {passed} passed  /  {failed} failed  /  {caln} 待校準  /  {len(rows)} checked  /  {len(missing)} 未擷取",
        f"  待校準 TC（{len(cal)}）: {', '.join(cal)}",
    ]
    if missing:
        lines.append(f"  未擷取: {', '.join(missing)}")
    lines.append("=" * W)
    return "\n".join(lines)


def _cli_inspect(_ARGV):
    import argparse
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tc_ids", nargs="*", help="TC IDs（e.g. IOS-04）。省略＝全跑")
    p.add_argument("--inspect", dest="file", nargs="?", default="/tmp/appier_bid.json", help="bid request JSON")
    p.add_argument("--out", help="report 存檔路徑")
    p.add_argument("--inspect-round", dest="round", help="round 資料夾 — 彙總所有 capture")
    args = p.parse_args(_ARGV)

    if args.round:
        rows = aggregate_round(args.round)
        if not rows:
            sys.exit(f"no capture results.json under {args.round}")
        report = format_round_report(rows, os.path.basename(args.round.rstrip("/")))
        print(report)
        with open(os.path.join(args.round, "round_report.txt"), "w") as f:
            f.write(report + "\n")
        return

    try:
        with open(args.file) as f:
            bid = json.load(f)
    except FileNotFoundError:
        sys.exit(f"bid file not found: {args.file}")
    except json.JSONDecodeError as e:
        sys.exit(f"invalid JSON in {args.file}: {e}")

    tc_filter = set(args.tc_ids) if args.tc_ids else None
    results = run_inspection(bid, tc_filter)
    report = format_report(results, args.file)
    print(report)
    if args.out:
        with open(args.out, "w") as f:
            f.write(report + "\n")
        print(f"\n→ saved: {args.out}")


# ════════════════════════════════════════════════════════════════════════════
# iOS 單輪 HTML 報告
#   （原 build_artifact_ios.py）
# ════════════════════════════════════════════════════════════════════════════

# iOS 專屬欄位（不在 Android FIELD_SCHEMA）的使用者導向 schema：
#   field -> (signal 名稱, 型別, 格式, 備註)
# iOS 專屬欄位的使用者導向說明（欄位名/型別/格式/備註）。
IOS_FIELD_SCHEMA = {}
_SCHEMA = {**FIELD_SCHEMA, **IOS_FIELD_SCHEMA}

# IOS-xx → 分類字母（沿用 AND-xx 號碼對應）

# iOS 自己的 TC → 分類字母。原本是從 AOS 的 CAT_OF 推導（隱性跨平台相依），
# 拆檔後改成本平台自有資料。
# iOS 的 TC → 分類字母（A–N，分類名在 verdict.CATEGORIES）。
CAT_OF_IOS = {}

# 交給共用判定契約的「清楚的限制」集合：iOS 用 validator 的 cal 旗標表達「待校準」，
# 語意等同 AOS 的 RD_GAP／硬體不可得 —— 恆 block，不算產品 FAIL。
IOS_BLOCKED_ALL = frozenset(v["tc"] for v in IOS_VALIDATORS if v.get("cal"))

STATUS = {"PASS": ("pass", "PASS"), "FAIL": ("fail", "FAIL"), "CAL": ("blocked", "待校準")}


def _ts_ms(ts):
    try:
        return int(datetime.strptime(ts, "%Y%m%d_%H%M%S").timestamp() * 1000)
    except Exception:
        return None


def load_captures(round_dir):
    caps = {}
    for entry in sorted(os.scandir(round_dir), key=lambda e: e.name):
        if not entry.is_dir() or not os.path.exists(os.path.join(entry.path, "results.json")):
            continue
        try:
            meta = json.load(open(os.path.join(entry.path, "results.json")))
        except Exception:
            continue
        cap = {"meta": meta, "dir": entry.path, "bid": None, "shots": {}}
        bidp = os.path.join(entry.path, "bid_request.json")
        if os.path.exists(bidp):
            try:
                cap["bid"] = json.load(open(bidp))
            except Exception:
                pass
        # 收集所有截圖：phone.png（廣告當下）+ state_proof_<group>.png（設定當下）
        for fn in sorted(os.listdir(entry.path)):
            if fn == "phone.png":
                key = "phone"
            elif fn.startswith("state_proof_") and fn.endswith(".png"):
                key = "proof::" + fn[len("state_proof_"):-len(".png")]
            else:
                continue
            try:
                cap["shots"][key] = encode_shot(os.path.join(entry.path, fn))
            except Exception:
                pass
        caps[entry.name] = cap
    return caps


# ── validator 中繼資料查找（check/expected/note/cal…）─────────────────────────
_VMETA = {(v["tc"], v["field"]): v for v in IOS_VALIDATORS}


def _expected_disp(v):
    chk = v.get("check", "")
    if chk == "value_or_absent":
        return f"{fmt_val(v.get('expected'))} 或缺席"
    if "expected" in v:
        return fmt_val(v["expected"])
    return {
        "uuid_ci_nonzero": "非零 UUID（大寫，IDFA/IDFV）",
        "regex": "符合格式" + (f"（{v['pattern'].pattern}）" if v.get("pattern") else ""),
        "nonempty": "非空", "nonempty_notunknown": "非空、非 unknown",
        "present": "欄位存在", "array": "陣列", "array_nonempty": "非空陣列",
        "array_timestamp": "13-digit ms 時間戳陣列", "array_number": "數值陣列",
        "array_impression": "impression 結構陣列", "array_regex": "字串陣列（符合格式）",
        "int_range": f"整數 {v.get('min')}–{v.get('max')}", "range": f"{v.get('min')}–{v.get('max')}",
        "positive_int": "正整數", "ipv4_nonzero": "合法非零 IPv4",
        "int_zero_or_absent": "整數 0 或缺席", "absent": "缺席", "absent_or_empty": "缺席/空",
        "falsy": "缺席或空陣列", "leq_field": f"≤ {v.get('ref_field')}",
        "vpn_active": "非空 VPN 協定字串", "timestamp_recent": "近期 13-digit ms 時間戳",
    }.get(chk, chk)


def _provenance(field):
    if field.startswith(("device.", "user.")):
        return "ext_enc（data-signal，已解碼）"
    if field.startswith("req.") or field.startswith("skadn"):
        return "req_enc（已解碼）"
    return "明文 body"


def _clean(note):
    """去掉內部校準標記（[待校準]、RD gap…），留給使用者看的乾淨文字。"""
    s = (note or "").replace("[待校準] ", "")
    s = re.sub(r"[，,、]?\s*RD gap\s*", "", s)      # 去 "RD gap" 及前置分隔
    s = s.replace("（）", "").replace("()", "").replace("（，", "（").strip()
    return s


def _card_from_result(r, capture_name, cap_shots):
    """把一條 IOS-xx 驗證結果組成 render_card 需要的卡片 dict（使用者導向，無內部標籤）。
    截圖不內嵌到每張卡（會 52× 重複）；只放 shot key，圖片存 SHOTS map 由 JS 共享填入。
    狀態切換類 TC 優先用該狀態的設定截圖（state_proof_<group>），否則用廣告當下 phone.png。"""
    field = r["field"]
    tc = r["tc"]
    # 選截圖：狀態 TC 且該 capture 有對應 group 的設定截圖 → 用它（附「設定當下」說明）
    st = IOS_STATE.get(tc)
    shot_key, shot_cap = "", ""
    if st and ("proof::" + st[0]) in cap_shots:
        shot_key = f"{capture_name}::proof::{st[0]}"
        shot_cap = st[2]                       # 這狀態的截圖該證明什麼
    elif "phone" in cap_shots:
        shot_key = f"{capture_name}::phone"
        shot_cap = "bid 當下 app 畫面"
    v = _VMETA.get((r["tc"], field), {})
    if r["passed"]:
        status = "PASS"
    elif v.get("cal"):
        status = "CAL"
    else:
        status = "FAIL"
    status_cls, status_label = STATUS[status]
    # signal 名稱 / 型別 / 格式 / schema 備註：跟 AOS 同一份 FIELD_SCHEMA（+iOS 補充）
    signal, schema_type, schema_format, schema_note = _SCHEMA.get(
        field, (field, "—", "—", ""))
    expected = _expected_disp(v)
    actual_disp = fmt_val(r["actual"])
    if status == "PASS":
        explanation = f"bid request 的 {field} = {actual_disp}，符合預期「{expected}」，判定 Pass。"
    elif status == "FAIL":
        explanation = f"bid request 的 {field} = {actual_disp}，不符合預期「{expected}」，判定 Fail。"
    else:
        explanation = _clean(r.get("note", "")) or "本欄位本輪暫時無法驗證，條件補齊後可重測。"
    return {
        "tc": r["tc"], "field": field, "cat": CAT_OF_IOS.get(r["tc"], "D"),
        "tier": tier_of(v.get("check", ""), r["tc"]),
        "shot": shot_key,
        "shot_matched": True,
        "shot_caption": shot_cap,
        "shot_data": "",   # 不內嵌；由 SHOTS map + JS 依 data-shot 填入（去重）
        "set": "", "shows": "",
        "rd_note": "",
        "blocked_reason": (_clean(r.get("note", "")) if status == "CAL" else ""),
        "action": "",
        "bid_ids": {},
        "capture": capture_name,
        "actual": actual_disp,
        "ground_truth": None,
        "attempts": [],
        "status_cls": status_cls, "status_label": status_label,
        "expected": expected,
        "signal": signal,
        "schema_type": schema_type,
        "schema_format": schema_format,
        "schema_note": schema_note,
        "absent_reason": None, "mock_cmd": None, "mock_reset": None,
        "provenance": _provenance(field),
        "evidence_explanation": explanation,
        "condition": schema_note or "以 bid request 實際值比對 Golden 期望。",
    }


def evaluate_round(caps):
    """每個 capture 依宣告範圍重算、latest-wins 合併，回傳 (cards, counts, not_run)。"""
    merged = {}   # (tc, field) -> card
    for name in sorted(caps, key=lambda n: caps[n]["meta"].get("captured_at", "")):
        cap = caps[name]
        if cap["bid"] is None:
            continue
        tc_id = cap["meta"].get("tc_id", "AUTO")
        # 舊 round 的該批次寫成 "BASELINE"，一併認
        tc_filter = (AUTO_TCS if tc_id in ("AUTO", "BASELINE")
                     else set(tc_id.split(",")))
        ref_ms = _ts_ms(cap["meta"].get("captured_at", ""))
        for r in run_inspection(cap["bid"], tc_filter, reference_ms=ref_ms):
            merged[(r["tc"], r["field"])] = _card_from_result(r, name, cap["shots"])

    cards = []
    for v in IOS_VALIDATORS:
        if v["check"] == "session_case":
            continue
        c = merged.get((v["tc"], v["field"]))
        if c is not None and c not in cards:
            cards.append(c)
    counts = {"pass": 0, "fail": 0, "blocked": 0}
    for c in cards:
        counts[c["status_cls"]] = counts.get(c["status_cls"], 0) + 1
    # 未 capture 的 validator (tc,field) 也計入 blocked（本輪未執行），讓分母＝完整 IOS
    # validator scope——與 Android 一致（Android 對無 capture 的 TC 也計 BLOCKED），否則
    # 兩平台分母不同、iOS 會少報總數。
    captured_keys = set(merged.keys())
    counts["blocked"] += sum(
        1 for v in IOS_VALIDATORS
        if v["check"] != "session_case" and (v["tc"], v["field"]) not in captured_keys)
    covered = {c["tc"] for c in cards}
    not_run = sorted({v["tc"] for v in IOS_VALIDATORS
                      if v["check"] != "session_case" and v["tc"] not in covered})
    return cards, counts, not_run


def render_html(round_name, cards, counts, not_run, caps, environment, meta):
    """單輪 iOS 報告 HTML（骨架）。

    版面與 AOS 共用同一份 CSS / render_card / js_block（共同語義只有一份）。
    差別只有兩處：iOS 沒有 E2E 分頁；tile 第三格叫「待補 / 未送」而不是 Blocked。

    分母口徑：total = pass + fail + blocked，**含本輪未執行**，與 AOS 一致。
    只算「有 capture 的 TC」會讓 iOS 少報總數、兩平台分母不同、平台頁沒法比。

    待重建：tiles、測試環境面板、未擷取 TC 清單（not_run，狀態切換類需單獨 capture）、
    lightbox、以及把 SHOTS 依 data-shot 填回每張卡的去重腳本。
    原版版面：`git show 8307b56:qa_ios.py`
    """
    title = "SDK_AUTOMATION iOS — " + " · ".join(
        x.upper() for x in (meta["test_mode"], meta["test_type"]) if x)
    total = counts.get("pass", 0) + counts.get("fail", 0) + counts.get("blocked", 0)
    # 所有截圖（phone + state_proof）各存一份，卡片依 key 共享（不內嵌到每張卡）
    shots = {f"{name}::{key}": data
             for name, cap in caps.items()
             for key, data in cap.get("shots", {}).items()}
    by_cat = {}
    for c in cards:
        by_cat.setdefault(c["cat"], []).append(c)
    sections = "\n".join(
        f'<section class="cat" id="cat-{letter}" data-cat="{letter}">'
        f'<h2 class="cat-h"><span class="cat-k">Cat {letter}</span>'
        f'{esc(CATEGORIES[letter])}<span class="cat-n">{len(by_cat[letter])}</span></h2>'
        f'<div class="grid">{"".join(render_card(c) for c in by_cat[letter])}</div></section>'
        for letter in CATEGORIES if letter in by_cat)
    return f"""<meta charset="utf-8">
<title>{esc(title)}</title>
<style>{CSS}</style>
<header class="top"><div class="top-in">
  <div class="brand"><div class="sig" aria-hidden="true"></div>
    <div><div class="kicker">Appier SDK 開發案 · 自動化測試（iOS）</div><h1>{esc(title)}</h1></div>
  </div>
  <dl class="meta">
    <div><dt>Round</dt><dd>{esc(round_name)}</dd></div>
    <div><dt>類型</dt><dd>{esc(meta['test_type'] or '—')}</dd></div>
    <div><dt>整合模式</dt><dd>{esc(meta['test_mode'] or '—')}</dd></div>
    <div><dt>Test CID</dt><dd>{esc(meta['test_cid'] or '—')}</dd></div>
    <div><dt>執行人</dt><dd>{esc(meta['test_executor'] or '—')}</dd></div>
    <div><dt>Device</dt><dd>iOS · {esc(meta['model'])}</dd></div>
    <div><dt>Signal TC</dt><dd>{total}</dd></div>
    <div><dt>Generated</dt><dd>{esc(meta['generated'])}</dd></div>
  </dl>
</div></header>
<main>{sections}</main>
<!-- TODO(版面): tiles / 測試環境面板 / 未擷取 TC 清單（{len(not_run)} 條）/ lightbox -->
<script>{js_block(json.dumps(shots), json.dumps(round_name))}</script>
"""


def _meta_return(out_path, round_name, meta, counts, ncaps):
    return {
        "out": out_path, "round_name": round_name,
        "test_type": meta["test_type"], "test_mode": meta["test_mode"],
        "test_cid": meta["test_cid"], "test_executor": meta["test_executor"],
        "model": meta["model"], "elapsed": None,
        "signal_total": counts.get("pass", 0) + counts.get("fail", 0) + counts.get("blocked", 0),
        # iOS「待校準/未執行」對齊 Android，一律歸 BLOCKED 桶（不要放 PENDING），
        # build_platform 卡片才能與 Android 同口徑比較。
        "signal_counts": {"PASS": counts.get("pass", 0), "FAIL": counts.get("fail", 0),
                          "BLOCKED": counts.get("blocked", 0)},
        "e2e_total": 0, "e2e_score": {"PASS": 0, "FAILED": 0, "BLOCKED": 0},
    }


def build(round_dir, out_path, e2e_round=None):
    caps = load_captures(round_dir)
    if not caps:
        sys.exit(f"no capture (results.json) found under {round_dir}")
    cards, counts, not_run = evaluate_round(caps)
    round_name = os.path.basename(round_dir.rstrip("/"))
    latest = caps[max(caps, key=lambda n: caps[n]["meta"].get("captured_at", ""))]
    environment = latest["meta"].get("environment", {})
    meta = {
        "test_type": latest["meta"].get("test_type", ""),
        "test_mode": latest["meta"].get("test_mode", ""),
        "test_cid": latest["meta"].get("test_cid", ""),
        "test_executor": latest["meta"].get("test_executor", ""),
        "model": environment.get("device") or "iPhone",
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    if not cards:
        # 沒有 bid body（極端：只有 impression 識別碼）→ 仍出一頁精簡說明
        html_out = (f"<title>{esc('SDK_AUTOMATION iOS — ' + round_name)}</title>"
                    f"<style>{CSS}</style><main style='padding:24px'>"
                    f"<h1>iOS · {esc(meta['test_type'])}</h1>"
                    f"<p class='lead'>本輪未取得可驗證的 bid body（僅有 impression 識別碼）。</p></main>")
        Path(out_path).write_text(html_out, encoding="utf-8")
        print(f"→ {out_path}\n  0 checks（無 bid body）")
        return _meta_return(out_path, round_name, meta, {}, len(caps))

    html_out = render_html(round_name, cards, counts, not_run, caps, environment, meta)
    Path(out_path).write_text(html_out, encoding="utf-8")
    print(f"→ {out_path}")
    print(f"  {len(cards)} checks: {counts.get('pass',0)} pass / {counts.get('fail',0)} fail "
          f"/ {counts.get('blocked',0)} 待校準 / {len(not_run)} TC 未擷取")
    return _meta_return(out_path, round_name, meta, counts, len(caps))


def _cli_report(_ARGV):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", dest="round_dir", required=True,
                    help="iOS evidence round 資料夾")
    ap.add_argument("--out", help="輸出 HTML 路徑（預設 <round_dir>/report.html）")
    ap.add_argument("--meta", help="把該輪計數/中繼資料另存成 JSON（供 page.py 讀取）")
    args = ap.parse_args(_ARGV)
    meta = build(args.round_dir, args.out or os.path.join(args.round_dir, "report.html"))
    if args.meta:
        with open(args.meta, "w") as f:
            json.dump(meta, f, ensure_ascii=False, default=str)


# ════════════════════════════════════════════════════════════════════════════
# 實機 capture（Appium/XCUITest、syslog、證據落地）與入口
#   （原 run_qa_ios.py）
# ════════════════════════════════════════════════════════════════════════════

# ── detector 產出的檔案（跨平台，與 run_qa.py 共用協定）─────────────────────────
FLAG_FILE         = "/tmp/appier_hit"
BID_FILE          = "/tmp/appier_bid.json"
FIRST_BID_FILE    = "/tmp/appier_first_bid.json"
BID_STATUS_FILE   = "/tmp/appier_bid_status"
BID_RESPONSE_FILE = "/tmp/appier_bid_response.json"
IMPRESSION_FILE   = "/tmp/appier_impression.json"
TRAFFIC_FILE      = "/tmp/appier_traffic.jsonl"
NETWORK_FILE      = "/tmp/current_networks"
SYSLOG_TMP        = "/tmp/appier_ios_syslog.txt"

SYSLOG_PROC = None


# ── 終端機進度條 ──────────────────────────────────────────────────────────────
def progress(cur, total, label="", width=26):
    """單行原地更新的進度條（\\r）。cur>=total 時收尾換行。"""
    total = max(int(total), 1)
    cur = max(0, min(int(cur), total))
    filled = round(width * cur / total)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(100 * cur / total)
    line = f"\r  ▕{bar}▏ {cur}/{total} {pct:3d}%  {label}"
    sys.stdout.write(line[:110].ljust(112))
    sys.stdout.flush()
    if cur >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


def wait_with_countdown(deadline, ready, label="等待 bid response"):
    """等到 ready() 為真或 deadline 到；期間顯示倒數進度條。回傳是否 ready。"""
    total = max(deadline - time.monotonic(), 0.01)
    while True:
        remain = deadline - time.monotonic()
        if ready():
            progress(total, total, label + "（已收到）")
            return True
        if remain <= 0:
            progress(total, total, label + "（逾時）")
            return False
        progress(total - remain, total, f"{label} … {remain:0.0f}s")
        time.sleep(0.2)


APPIUM_URL   = "http://127.0.0.1:4723"
BID_TIMEOUT  = 12.0
EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR", Path(__file__).parent / "evidence"))

BUNDLE_ID    = os.environ.get("BUNDLE_ID", "").strip()
# iOS sample app（AppierAdsSwiftSample）為分頁結構，跟 Android 版一樣要先選頁籤
# （2026-07-20 實機 dump 確認）：
#   Tab bar：「Appier Direct」/「AdMob Mediation」
#   可點擊版位文字（accessibility id）＝該頁籤下的副標題文字，不是 "basic"：
#     Appier Direct   → "direct (AppierAds SDK)"
#     AdMob Mediation → "mediation (AdMob + Appier)"
# 若之後 sample app 換版或加 AppLovin 頁籤，文字可能不同——先用
# ios_dump_labels() 或 --dump-labels 重新盤點再改這裡。
TAB_TRIGGER_LABEL = {
    "standalone": "direct (AppierAds SDK)",
    "admob-mediation": "mediation (AdMob + Appier)",
    "applovin-mediation": "mediation (AppLovin + Appier)",  # 待實機確認（尚未 dump 過此頁籤）
}
TAB_NAME = {
    "standalone": "Appier Direct",
    "admob-mediation": "AdMob Mediation",
    "applovin-mediation": "AppLovin Mediation",  # 待實機確認 tab 名稱
}
# 環境變數可覆蓋自動推斷（TEST_MODE 解析前先讀不到 TEST_MODE，故 trigger label
# 在 main() 內、resolve_test_mode() 之後才決定；這裡只放使用者顯式覆蓋值）
TRIGGER_LABEL_OVERRIDE = os.environ.get("TRIGGER_LABEL", os.environ.get("AD_LABEL", "")).strip()
TAB_OVERRIDE = os.environ.get("TAB", "").strip()
def _round_label(value):
    """round 標籤會變成資料夾名的一段：只留英數與 -_，長度上限 24；未給時用 R<日期>。"""
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", (value or "").strip()).strip("-_")[:24]
    return label or "R" + datetime.now().strftime("%Y%m%d")


TEST_ROUND   = _round_label(os.environ.get("TEST_ROUND", ""))
VALID_TYPES  = ("aibid", "reen-static", "reen-dynamic")
VALID_MODES  = ("standalone", "admob-mediation", "applovin-mediation")
TEST_TYPE    = os.environ.get("TEST_TYPE", "").strip().lower()
TEST_MODE    = os.environ.get("TEST_MODE", "").strip().lower()
TEST_CID     = os.environ.get("TEST_CID", "").strip()
TEST_EXECUTOR = os.environ.get("TEST_EXECUTOR", "").strip() or getpass.getuser()

# WebDriverAgent 自動簽名：設 XCODE_ORG_ID（Apple Developer Team ID）即可，不用
# 手動進 Xcode 設 signing；WDA bundle id 衝突時另設 WDA_BUNDLE_ID。
XCODE_ORG_ID  = os.environ.get("XCODE_ORG_ID")
WDA_BUNDLE_ID = os.environ.get("WDA_BUNDLE_ID")

DWELL_SEC       = float(os.environ.get("DWELL_SEC", "0"))
AD_RETRY_DELAY  = float(os.environ.get("AD_RETRY_DELAY", "2"))
MAX_AD_ATTEMPTS = int(os.environ.get("MAX_AD_ATTEMPTS", "150"))
SAVE_ON_BID     = os.environ.get("SAVE_ON_BID", "0") == "1"
CAPTURE_LABEL   = os.environ.get("CAPTURE_LABEL", "").strip()
STATE_ACTION    = os.environ.get("STATE_ACTION")

# 工具模式（不碰實機）：必須在 TC_ID 解析前分流，否則 --report 會被當成 TC 名稱
if {"--help", "-h"} & set(sys.argv[1:]):
    print(__doc__)
    sys.exit(0)
TOOL_MODES = ("--report", "--inspect", "--inspect-round")
TOOL_MODE = next((m for m in TOOL_MODES if m in sys.argv[1:]), None)
_POS = [] if TOOL_MODE else [a for a in sys.argv[1:] if not a.startswith("-")]

TC_ID = _POS[0] if _POS else "AUTO"
UDID  = _POS[1] if len(_POS) > 1 else (os.environ.get("UDID", "").strip() or None)


# ── 互動詢問（對照 run_qa.py 的 resolve_*）─────────────────────────────────────
def resolve_test_type():
    global TEST_TYPE
    if TEST_TYPE in VALID_TYPES:
        return TEST_TYPE
    if not sys.stdin.isatty():
        sys.exit(f"TEST_TYPE 必填且須為 {VALID_TYPES}（非互動環境請用環境變數帶入）")
    print("投放目的？ 1) AIBID  2) REEN")
    goal = input("選 [1/2]: ").strip()
    if goal == "1":
        TEST_TYPE = "aibid"
    elif goal == "2":
        creative = input("素材？ 1) Static  2) Dynamic [1/2]: ").strip()
        TEST_TYPE = "reen-static" if creative == "1" else "reen-dynamic"
    else:
        sys.exit("無效選擇。")
    return TEST_TYPE


def resolve_test_mode():
    global TEST_MODE
    if TEST_MODE in VALID_MODES:
        return TEST_MODE
    if not sys.stdin.isatty():
        return "standalone"
    print("SDK 整合模式？ 1) standalone  2) admob-mediation  3) applovin-mediation")
    sel = input("選 [1/2/3]（預設 1）: ").strip() or "1"
    TEST_MODE = {"1": "standalone", "2": "admob-mediation",
                 "3": "applovin-mediation"}.get(sel, "standalone")
    return TEST_MODE


def resolve_test_cid():
    global TEST_CID
    if TEST_CID:
        return TEST_CID
    if not sys.stdin.isatty():
        return ""
    TEST_CID = input("測試 CID（可留空）: ").strip()
    return TEST_CID


def resolve_round_dir():
    """同 run_qa.py，但 round 名加 IOS_ 前綴 → build_platform 認得是 iOS 入口。"""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    safe_cid = re.sub(r"[^A-Za-z0-9_-]+", "-", TEST_CID).strip("-")
    type_label = TEST_TYPE.upper().replace("-", "_")
    mode_label = TEST_MODE.upper().replace("-", "_")
    prefix = f"IOS_{mode_label}_{type_label}_CID_{safe_cid}_{TEST_ROUND}"
    existing = sorted(d for d in EVIDENCE_DIR.glob(f"{prefix}_*") if d.is_dir())
    if existing:
        return existing[-1]
    return EVIDENCE_DIR / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


# ── iOS 裝置層（取代 adb helpers）───────────────────────────────────────────────
def _have(tool):
    return shutil.which(tool) is not None


def detect_udid():
    """偵測唯一連接的 iPhone（解析 xctrace list devices）。"""
    if UDID:
        return UDID
    out = subprocess.check_output(["xcrun", "xctrace", "list", "devices"], text=True)
    devices_section = out.split("== Devices ==")[1].split("==")[0]
    udids = re.findall(r'\(([0-9A-Fa-f]{40}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16})\)',
                       devices_section)
    if not udids:
        sys.exit("找不到連接的 iPhone，請接上手機或手動指定 UDID。")
    if len(udids) > 1:
        sys.exit(f"偵測到多台裝置：{udids}\n請執行：python run_qa_ios.py {TC_ID} <UDID>")
    return udids[0]


def dismiss_system_alert(driver):
    """自動接受系統彈窗（App Tracking Transparency 授權詢問等）。

    全新裝置/app 重置後，SDK 第一次要 IDFA 會觸發 ATT 系統彈窗；headless 自動化
    沒有人可以點，會卡住等到 timeout。沒有彈窗時 accept 會丟例外，直接吞掉即可
    （不影響原本流程）。"""
    try:
        driver.execute_script("mobile: alert", {"action": "accept"})
        print("  [note] 已自動接受系統彈窗（可能是 App Tracking Transparency 授權）")
        return True
    except Exception:
        return False


def select_tab(driver, tab_name):
    """切到 sample app 的指定頁籤（Tab Bar 按鈕的 accessibility id＝頁籤名稱）。
    iOS 版靠 XCUITest tab bar button 直接命中，
    不需要 AOS 那種 ViewPager 預載座標過濾。"""
    if not tab_name:
        return True
    try:
        driver.find_element("accessibility id", tab_name).click()
        time.sleep(0.6)
        return True
    except Exception as exc:
        print(f"  [warn] 切頁籤 '{tab_name}' 失敗：{exc}")
        return False


# group → 從 Settings 根層依序要點的 row（可見文字；_tap_settings_row 會捲動尋找）。
# 對照 ios_bid_inspector.IOS_STATE 的 group。
IOS_SETTINGS_NAV = {
    "darkmode":    ["Display & Brightness"],
    "brightness":  ["Display & Brightness"],
    "textsize":    ["Display & Brightness", "Text Size"],
    "lowpower":    ["Battery"],
    "charging":    ["Battery"],
    "batterylevel": ["Battery"],
    "tracking":    ["Privacy & Security", "Tracking"],
    "geo":         ["Privacy & Security", "Location Services"],
    "tz":          ["General", "Date & Time"],
    "language":    ["General", "Language & Region"],
    "vpn":         ["General", "VPN & Device Management"],
    "deviceinfo":  ["General", "About"],
}


def _tap_settings_row(driver, label):
    """在 Settings 內找一列並點入（先 accessibility id，再可見文字，找不到就捲動）。"""
    for _ in range(8):
        for by, val in (("accessibility id", label),
                        ("-ios predicate string",
                         f'label == "{label}" OR name == "{label}"')):
            try:
                driver.find_element(by, val).click()
                time.sleep(0.8)
                return True
            except Exception:
                pass
        try:
            driver.execute_script("mobile: scroll", {"direction": "down"})
            time.sleep(0.3)
        except Exception:
            break
    return False


def capture_state_proof_ios(driver, folder, groups):
    """iOS 的狀態證據截圖（骨架）：導航到設定頁後截圖 → `state_proof_<group>.png`。

    與 AOS 的差別：iOS 沒有 adb 這種 CLI 可以直接開設定頁，只能用 Appium 一層層點
    設定 App（`IOS_SETTINGS_NAV` 是各組的導航路徑，`_tap_settings_row()` 負責點）。
    這兩個**保留未清** —— 那是裝置知識而不是 TC 定義。

    刻意留空，與 AOS 的 `capture_state_proof()` 一起重新設計。
    注意：原版**本來就沒有任何呼叫者**（iOS 沒有 round 排程，狀態類 TC 要人工佈），
    要用得先在 `save_evidence()` 裡接起來。
    原版：`git show 8307b56:qa_ios.py`
    """
    return {}


def ideviceinfo(key=None, domain=None):
    """讀 ideviceinfo 單一 key（或整個 domain）。查不到回 ''。需 libimobiledevice。"""
    if not _have("ideviceinfo"):
        return ""
    cmd = ["ideviceinfo"]
    if UDID:
        cmd += ["-u", UDID]
    if domain:
        cmd += ["-q", domain]
    if key:
        cmd += ["-k", key]
    try:
        return subprocess.check_output(cmd, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception as e:
        return f"[err: {e}]"


def start_syslog():
    """從 app 啟動前開始側錄 idevicesyslog（取代 adb logcat）。"""
    global SYSLOG_PROC
    if not _have("idevicesyslog"):
        print("  [warn] 找不到 idevicesyslog（brew install libimobiledevice）；跳過 syslog 側錄。")
        return
    cmd = ["idevicesyslog"]
    if UDID:
        cmd += ["-u", UDID]
    # 只留受測 app + Appier 相關行，避免整機 syslog 過大
    if BUNDLE_ID:
        cmd += ["-p", BUNDLE_ID]
    out = open(SYSLOG_TMP, "w")
    SYSLOG_PROC = subprocess.Popen(cmd, stdout=out, stderr=subprocess.DEVNULL)


def stop_syslog():
    global SYSLOG_PROC
    if SYSLOG_PROC is not None:
        SYSLOG_PROC.terminate()
        try:
            SYSLOG_PROC.wait(timeout=3)
        except subprocess.TimeoutExpired:
            SYSLOG_PROC.kill()
        SYSLOG_PROC = None


IMPRESSION_RE = re.compile(
    r"[?&]cid=([^&\s]+).*?[&]crid=([^&\s]+)")


def scan_syslog_ad_identity():
    """從 syslog 的 impression tracker URL 撈實際載入廣告的 cid/crid。查不到回 None。"""
    if not os.path.exists(SYSLOG_TMP):
        return None
    identity = None
    for line in open(SYSLOG_TMP, errors="ignore"):
        m = IMPRESSION_RE.search(line)
        if m:
            identity = {"cid": m.group(1), "crid": m.group(2)}
    return identity


def extract_bid_ids(logtext):
    ids = {}
    for key in ("bidobjid", "cid", "crid", "crpid", "oid"):
        m = re.search(key + r"=([A-Za-z0-9_-]+)", logtext)
        if m:
            ids[key] = m.group(1)
    return ids


def collect_environment_ios():
    """iOS capture 當下的環境快照 → `environment.json`（骨架）。

    刻意留空，與 AOS 的 `collect_environment()` 一起重新設計。
    原版走 `ideviceinfo`（需 libimobiledevice + 已配對），拿不到時退回 Appium
    capabilities，並用 `env_source` 記下這份快照是哪個來源 —— 這個誠實標註要保留，
    不然沒法分辨「值是真的」和「值是 Appium 猜的」。
    原版：`git show 8307b56:qa_ios.py`

    讀取側（`qa_ios.render_html()` 的測試環境面板）目前預期的 key：
      bundle_id device device_name os_version build_fingerprint
      timezone locale env_source
    """
    return {}


def snapshot_device_state_ios():
    """iOS capture 當下的裝置狀態文字快照 → `device_state.txt`（骨架）。

    對照 AOS 的 `snapshot_device_state()`：結構化的給報告讀（environment.json），
    這份全文的給人看。刻意留空，一起重新設計。
    原版：`git show 8307b56:qa_ios.py`
    """
    return ""


# ── evidence bundle（Phase 1：擷取＋摘要，不做 AND-xx 驗證）─────────────────────
def summarize_bid_fields(bid, prefix=""):
    """把 iOS bid 攤平成 dotted-path 清單，供之後建 iOS TC 目錄對照。"""
    rows = []
    if isinstance(bid, dict):
        for k, v in bid.items():
            rows += summarize_bid_fields(v, f"{prefix}{k}.")
    elif isinstance(bid, list):
        if bid:
            rows += summarize_bid_fields(bid[0], f"{prefix}0.")
        else:
            rows.append((prefix.rstrip("."), "[]"))
    else:
        val = str(bid)
        rows.append((prefix.rstrip("."), val[:80] + ("…" if len(val) > 80 else "")))
    return rows


SAVE_STEPS = ["環境快照", "app 截圖", "UI dump", "bid / 流量", "裝置狀態", "syslog", "TC 驗證 + 報告"]


def save_evidence(driver, ts):
    """把這一次 iOS capture 的證據落地（骨架）。

    刻意留空，與 AOS 的 `save_evidence()` 一起重新設計；結構保留：
    一次 capture ＝ 一個資料夾（掛在 `IOS_` 前綴的 round 底下）。
    原版：`git show 8307b56:qa_ios.py`

    ⚠️ 檔名是雙邊契約：讀取側 `qa_ios.load_captures()` 硬編 `results.json`（靠它認
    capture）、`bid_request.json`、`phone.png`、`state_proof_<group>.png`。
    完整檔名對照表寫在 `qa_aos.save_evidence()` 的 docstring，兩平台共用同一套命名。

    iOS 專屬的差異（重建時別漏）：
      ios_syslog.txt        取代 AOS 的 logcat.txt
      impression_ids.json   iOS 常常只抓到 impression callback 而拿不到 bid body
                            （cert pinning／時序）。那種情況原版**仍然**會把
                            cid/crid 這些識別碼落地，讓這一輪不算白跑 —— 這個行為
                            要保留，`qa_ios.build()` 也有對應的「無 bid body」分支。
      bid_fields.txt        `summarize_bid_fields()` 的攤平清單。重建 IOS_VALIDATORS
                            時就是靠它看 bid 到底有哪些欄位可驗。

    `SAVE_STEPS`（環境快照 / app 截圖 / UI dump / bid·流量 / 裝置狀態 / syslog /
    TC 驗證·報告）保留著，是原版的 7 個落地步驟，可以當重建的檢查表。
    """
    round_dir = resolve_round_dir()
    capture_name = (CAPTURE_LABEL or
                    ("AUTO" if TC_ID == "AUTO" else TC_ID.replace(",", "+")))
    folder = round_dir / f"{capture_name}_{ts}"
    folder.mkdir(parents=True, exist_ok=True)
    print("  [骨架] 證據落地未實作：只建了 capture 資料夾，沒寫入任何檔案。")
    print("         沒有 results.json，報告端不會把它當成 capture（見本函式 docstring）。")
    return folder


# ── main ────────────────────────────────────────────────────────────────────
def main():
    if not BUNDLE_ID:
        sys.exit("必填環境變數未設定：\n  export BUNDLE_ID=com.appier.ssp.sample")

    global UDID
    UDID = udid = detect_udid()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    test_type = resolve_test_type()
    test_mode = resolve_test_mode()
    test_cid = resolve_test_cid()

    # 頁籤 + 觸發版位：優先用使用者顯式覆蓋（TAB / TRIGGER_LABEL），否則依
    # TEST_MODE 從實機盤點過的對照表推斷（見 TAB_TRIGGER_LABEL 定義處註解）。
    tab_name = TAB_OVERRIDE or TAB_NAME.get(test_mode, "")
    trigger_label = TRIGGER_LABEL_OVERRIDE or TAB_TRIGGER_LABEL.get(test_mode, "")
    if not trigger_label:
        sys.exit(f"無法判定 TEST_MODE={test_mode!r} 的觸發版位，"
                 "請手動指定 TRIGGER_LABEL（或 AD_LABEL）環境變數。")

    print(f"[device] {udid}")
    print(f"[type  ] {test_type}")
    print(f"[mode  ] {test_mode}")
    print(f"[CID   ] {test_cid or '(none)'}")
    print(f"[by    ] {TEST_EXECUTOR}")
    print(f"[round ] {TEST_ROUND}")
    print(f"[TC    ] {TC_ID}")
    print(f"[app   ] {BUNDLE_ID}")
    print(f"[tab   ] '{tab_name or '(none)'}'")
    print(f"[tap   ] '{trigger_label}'")
    print()

    for f in (FLAG_FILE, BID_FILE, FIRST_BID_FILE, BID_STATUS_FILE,
              BID_RESPONSE_FILE, IMPRESSION_FILE, TRAFFIC_FILE, NETWORK_FILE):
        if os.path.exists(f):
            os.remove(f)

    print("[→] syslog recording ...")
    start_syslog()

    options = XCUITestOptions()
    options.bundle_id = BUNDLE_ID
    options.automation_name = "XCUITest"
    options.no_reset = True
    options.udid = udid
    # 全新裝置/app 重置後第一次要 IDFA 會跳 App Tracking Transparency 系統彈窗；
    # headless 自動化沒人可以點，交給 WDA 自動接受，不必等 dismiss_system_alert()
    # 剛好在對的時間點被呼叫到。
    options.set_capability("autoAcceptAlerts", True)
    if XCODE_ORG_ID:
        options.set_capability("xcodeOrgId", XCODE_ORG_ID)
        options.set_capability("xcodeSigningId", "Apple Development")
        options.set_capability("allowProvisioningDeviceRegistration", True)
        if WDA_BUNDLE_ID:
            options.set_capability("updatedWDABundleId", WDA_BUNDLE_ID)

    print("[→] launching via Appium ...")
    driver = webdriver.Remote(APPIUM_URL, options=options)
    time.sleep(2.0)

    try:
        dismiss_system_alert(driver)   # 保險：launch 當下若已有彈窗，先清掉
        if tab_name:
            print(f"[→] 切到頁籤 '{tab_name}' ...")
            select_tab(driver, tab_name)

        if DWELL_SEC > 0:
            print(f"[→] 前景停留 {DWELL_SEC:.0f}s ...")
            time.sleep(DWELL_SEC)

        attempt = 0
        status = None
        ad_identity = None
        hit = None
        source = None
        while True:
            attempt += 1
            if MAX_AD_ATTEMPTS and attempt > MAX_AD_ATTEMPTS:
                print(f"\n[停止] 已刷 {MAX_AD_ATTEMPTS} 次仍未命中指定 CID：{TEST_CID}")
                return 4

            if attempt > 1:
                stop_syslog()
                for f in (FLAG_FILE, BID_FILE, BID_STATUS_FILE, BID_RESPONSE_FILE, IMPRESSION_FILE):
                    if os.path.exists(f):
                        os.remove(f)
                start_syslog()
                try:
                    driver.back()
                except Exception:
                    pass
                time.sleep(1.2)
                if tab_name:
                    select_tab(driver, tab_name)

            progress(attempt - 1, MAX_AD_ATTEMPTS or attempt,
                     f"刷廣告 attempt {attempt}：tap '{trigger_label}'")
            tapped = False
            for _ in range(3):
                try:
                    driver.find_element("accessibility id", trigger_label).click()
                    tapped = True
                    break
                except Exception:
                    try:
                        driver.back()
                    except Exception:
                        pass
                    time.sleep(0.8)
                    if tab_name:
                        select_tab(driver, tab_name)
            if not tapped:
                print("    [retry] 找不到指定版位，重新啟動 app 後重試。")
                try:
                    driver.activate_app(BUNDLE_ID)
                    time.sleep(1.0)
                    if tab_name:
                        select_tab(driver, tab_name)
                except Exception:
                    pass
                time.sleep(AD_RETRY_DELAY)
                continue

            deadline = time.monotonic() + BID_TIMEOUT
            wait_with_countdown(deadline, lambda: os.path.exists(FLAG_FILE),
                                f"attempt {attempt} 等 bid request")

            if not os.path.exists(FLAG_FILE):
                print("    [retry] 沒偵測到 bid request（detector 未攔到 /v2/sdk/ios/ad）。")
                time.sleep(AD_RETRY_DELAY)
                continue

            hit = open(FLAG_FILE).read().strip()
            time.sleep(1.0)
            status = (open(BID_STATUS_FILE).read().strip()
                      if os.path.exists(BID_STATUS_FILE) else "?")
            source = "proxy"

            if attempt == 1 and os.path.exists(BID_FILE):
                shutil.copy(BID_FILE, FIRST_BID_FILE)

            # bid 端點本身因 cert pinning 看不到內容，SDK 也未見於 syslog（2026-07-20
            # 實機確認：syslog 裡沒有任何 Appier 自訂 subsystem，只有系統框架）；
            # 實際可用的識別碼來源是 mitmdump_addon.py 從「已展示」callback URL 解出的
            # IMPRESSION_FILE。scan_syslog_ad_identity() 保留當作未來備援，目前預期
            # 恆回 None。
            ad_identity = None
            if os.path.exists(IMPRESSION_FILE):
                try:
                    ad_identity = json.load(open(IMPRESSION_FILE))
                except Exception:
                    ad_identity = None
            if ad_identity is None:
                ad_identity = scan_syslog_ad_identity()
            if SAVE_ON_BID and (os.path.exists(BID_FILE) or ad_identity):
                if not ad_identity:
                    ad_identity = {"cid": "(no-win)", "crid": "(no-win)"}
                print(f"    [SAVE_ON_BID] bid request 已取得（response={status}），入庫。")
                break
            if status != "200":
                print(f"    [retry] response={status}，未命中廣告。")
            elif TEST_CID and (not ad_identity or ad_identity.get("cid") != TEST_CID):
                got = ad_identity.get("cid") if ad_identity else "(unknown)"
                print(f"    [retry] CID 不符：expected={TEST_CID}, actual={got}")
            else:
                break
            time.sleep(AD_RETRY_DELAY)

        cid_disp = ad_identity.get("cid") if ad_identity else "(unknown)"
        crid_disp = ad_identity.get("crid") if ad_identity else "(unknown)"
        print(f"\n[CAPTURED via {source}] {hit}  (response: {status}, "
              f"cid={cid_disp}, crid={crid_disp})\n")
        if status == "204":
            print("[判定] server 回 204 no-bid — 連線正常，目前沒有廣告可刷；"
                  "bid request 仍已留存。\n")

        print("[→] saving evidence ...")
        folder = save_evidence(driver, ts)
        print(f"\n[DONE] {folder}/")
        result_code = 3 if status == "204" else 0

    finally:
        stop_syslog()
        try:
            driver.quit()
        except Exception as exc:
            print(f"[warn] driver.quit() 失敗（不影響已存證據）：{exc}")

    auto_publish()
    return result_code


# ════════════════════════════════════════════════════════════════════════════
# 入口（單一 CLI：實機 capture／重算報告／離線驗證）
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if TOOL_MODE == "--report":
        _cli_report(sys.argv[1:])
        sys.exit(0)
    if TOOL_MODE in ("--inspect", "--inspect-round"):
        _cli_inspect(sys.argv[1:])
        sys.exit(0)
    sys.exit(main() or 0)
