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
/* ── 報告版面（骨架）─────────────────────────────────────────────────────────
   刻意留白。這裡只留「版面契約」：三種判定狀態的顏色 token，加上
   render_card / 平台 render_html 會產出的 class 名稱詞彙表。設計從 0 重建。

   要把原版整段拉回來（316 行：深/淺色 token、翻面卡、lightbox、
   Signal/E2E 分頁、tile 篩選、mock 指令區塊…）：
       git show 8307b56:verdict.py

   class 詞彙表 —— render_card / render_html 產出這些，CSS 要認得：
     版面骨架   .top .top-in .brand .sig .kicker .meta .progress-banner
     分頁       .tabbar .tabbtn .tabbtn-n .tab-pane
     統計       .tiles .tile .tile-n .tile-l  ／ .e2e-scorecard .e2e-tile
     分類       .cat .cat-h .cat-k .cat-n .grid
     卡片正面   .card .card-inner .face .card-front
                .card-top .tc .tier .pill .field .signal
                .result-kv .result-block .golden-block .actual-block .result-label
                .status-result .status-pass .status-fail .status-blocked
                .absent-why .mock-cmd .schema-ref .review-edit .ovr .ovr-note
     卡片背面   .card-back .back-head .back-scroll .flip-btn .flip-open .flip-close
                .capture-id .bid-evidence .bid-identity .proof-state .proof-why
                .note .note-rd .note-bl .action .repro .tc-detail
     E2E 時間軸 .e2e-step .e2e-step-head .e2e-step-rows .e2e-row .e2e-row-shot
                .e2e-kv .e2e-block .e2e-expect .e2e-lbl .e2e-val .e2e-badge
     截圖       .shot .shot-cap .lightbox .lb-x
     其他面板   .lead .setup-cards .setup-grid .chklist .mtable .mtc .mtag
                .con .con-row .con-ok .con-lab .con-msg .con-val .manlist
   ────────────────────────────────────────────────────────────────────────── */
:root{
  --bg:#f4f6f8; --panel:#ffffff; --ink:#131a21; --ink-soft:#4a5761; --line:#dde3e9;
  --accent:#0e7c86; --accent-soft:#e3f0f1;
  --pass:#2f7d3a; --pass-bg:#e6f2e8; --fail:#c0392b; --fail-bg:#fbe9e7;
  --block:#b5761a; --block-bg:#fbf0dd; --pend:#5b6b78; --pend-bg:#eceff2;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans TC",sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0f1519; --panel:#161e24; --ink:#e7edf1; --ink-soft:#9fb0bc; --line:#26313a;
  --accent:#38bdc9; --accent-soft:#123037;
  --pass:#5cc46a; --pass-bg:#16281a; --fail:#f0766a; --fail-bg:#2c1613;
  --block:#e0a94a; --block-bg:#2a2011; --pend:#9fb0bc; --pend-bg:#1c252c;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);line-height:1.5}
main{max-width:1180px;margin:0 auto;padding:22px 24px 80px}
.cat-h{display:flex;gap:10px;align-items:center;border-bottom:1px solid var(--line);padding-bottom:8px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(min(100%,340px),1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.tc{font:700 14px var(--mono)}
.field{font:12px var(--mono);color:var(--accent);word-break:break-all}
.result-kv{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.result-block{border:1px solid var(--line);border-radius:9px;padding:9px 11px;min-width:0}
.result-label{font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-soft)}
.result-block strong{font:12px var(--mono);word-break:break-word}
.pill{margin-left:auto;font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px}
.pill-pass{color:var(--pass);background:var(--pass-bg)}
.pill-fail{color:var(--fail);background:var(--fail-bg)}
.pill-blocked{color:var(--block);background:var(--block-bg)}
/* TODO(版面): 以上只夠讓骨架頁可讀。卡片正/反面、分頁、tile 篩選、E2E 時間軸、
   lightbox、人工覆寫 UI 全部待重建——詞彙表在本註解開頭。 */
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


# ── check 詞彙表（契約）──────────────────────────────────────────────────────
# 平台 TC 目錄的 `check` 欄位只能用這裡登記過的名稱。要加新 check：**先在這裡加一行**，
# 再到 run_validator 實作。反過來（TC 直接寫沒登記的名字）會落到 unknown check，
# 整條 TC 靜默變 FAIL —— 那是「看起來像產品壞了」的假失敗。
#
#   name                  TC 需附帶的欄位   通過標準（待實作）
CHECKS = {
    # 容忍「欄位不存在」的 check —— 實作時必須排在通用 missing gate 之前
    "absent":              (None,        "欄位不存在或為 null"),
    "absent_or_empty":     (None,        "不存在／null／空字串"),
    "value_or_absent":     ("expected",  "等於 expected，或不存在"),
    "int_zero_or_absent":  (None,        "整數 0，或不存在"),
    "falsy":               (None,        "falsy（含空陣列）或不存在"),
    "present":             (None,        "欄位存在即可，值可空（例：applist 能拿多少算多少）"),
    # 值比對
    "value":               ("expected",  "等於 expected，且型別完全相同"),
    "one_of_typed":        ("expected",  "屬於 expected 清單之一，且型別相同"),
    "equals_field":        ("ref_field", "等於另一欄位的值（同型別）"),
    "leq_field":           ("ref_field", "整數且 0 ≤ 值 ≤ ref_field"),
    # 格式
    "regex":               ("pattern",   "字串符合 pattern"),
    "uuid_nonzero":        (None,        "小寫 UUID(8-4-4-4-12) 且非全零"),
    "ipv4_nonzero":        (None,        "合法 IPv4 且非 0.0.0.0"),
    "vpn_active":          (None,        "非空協定字串（backend 型別是 string，boolean 算型別錯）"),
    # 數值
    "range":               ("min/max",   "數值落在 [min, max]"),
    "int_range":           ("min/max",   "整數且落在 [min, max]"),
    "nonzero_range":       ("min/max",   "落在 [min, max] 且不為 0"),
    "positive_int":        (None,        "正整數（bool 不算）"),
    "positive_float":      (None,        "> 0 的數值"),
    # 非空
    "nonempty":            (None,        "非空、非純空白"),
    "nonempty_notunknown": (None,        '非空且不是 "unknown"'),
    "truthy":              (None,        "truthy"),
    # 陣列（actual 一律回實際內容讓報告看得到值，數量寫進 message）
    "array":               (None,        "是陣列（可空）"),
    "array_nonempty":      (None,        "非空陣列"),
    "array_number":        (None,        "非空陣列，元素皆為數值"),
    "array_regex":         ("pattern",   "非空陣列，元素皆為符合 pattern 的字串"),
    "array_timestamp":     (None,        "非空陣列，元素皆為 13 位整數 ms 時間戳"),
    "array_impression":    (None,        "非空陣列，元素皆含 impression 必要 key"),
    # 時間
    "timestamp_recent":    (None,        "13 位 ms 時間戳，且接近 reference_ms"),
    # 跨 bid 對照：單一 bid 驗不出來，run_inspection 會跳過這種 check，
    # 判定由 runner 在 capture 當下寫進 session_case.json（見 qa_aos._phase_sd）
    "session_case":        (None,        "由 runner 於 capture 當下判定，不走這裡"),
}


def run_validator(bid, v, reference_ms=None):
    """Returns (passed: bool, actual, message: str)。**唯一實作**，兩平台共用。

    骨架狀態：每個 check 的通過標準都還沒填。這一層是「值符不符合期望」，
    刻意留空從 0 重建；上面的 CHECKS 是詞彙表（有哪些 check、各需要 TC 帶什麼欄位）。
    原版實作在 baseline commit：`git show 8307b56:verdict.py`

    實作時的兩條硬規則（原版踩過，別再踩）：

      1. **ABSENT_CHECKS 這類容忍欄位不存在的 check，必須排在通用 missing gate
         之前。** 也就是這行之前：
             if not found or value is None: return False, None, "field missing"
         排在後面的話，所有 absent 類 TC 會先被 missing gate 攔掉 → 整批假 FAIL。

      2. **型別要嚴格比對**：int 1 不等於 bool true。「送錯型別」在 test plan 裡是
         獨立的失敗模式，用 `==` 會放過。唯一例外是 float 期望值要接受數值相等的
         int —— org.json 把 1.0f 序列化成 "1"。
    """
    field = v["field"]
    check = v["check"]
    value, _found = get_field(bid, field)
    if check not in CHECKS:
        # 沒登記的 check 要講出來，不可靜默併入一般 FAIL
        return False, value, f"unknown check '{check}'（未登記於 verdict.CHECKS）"
    return False, value, f"check '{check}' 尚未實作（骨架）"


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
    """報告頁的行為層（骨架）。

    契約（版面靠這兩個變數，重建時不要改名）：
      SHOTS = {shot_key: data-uri}  卡片只帶 data-shot key，圖片存這裡共享一份
                                    （52 張卡各自內嵌會讓 HTML 爆掉）
      ROUND = round 名稱            人工覆寫存 localStorage 的 key 前綴

    待重建的行為（原版：`git show 8307b56:verdict.py`）：
      1. 縮圖填入：.shot img[data-src] / [data-shot] → SHOTS[key]
      2. tile 篩選：點 Pass/Fail/Blocked 只留該狀態的卡；某分類整段空掉就隱藏該段；
         篩不到任何卡時展開「未完成項目」面板，不要留一頁白
      3. lightbox：點截圖放大，Esc / 點背景關閉
      4. 卡片翻面：正面判定 ↔ 背面證據
      5. 人工覆寫判定：select ＋ 理由存 localStorage（key = 'appier-qa-ovr:'+ROUND），
         覆寫後必須 recount() —— tile 數字要跟著動，且**以 TC 為單位**去重計數，
         同一 TC 多欄位時取最嚴重：FAIL > BLOCKED > PASS
      6. Signal / E2E 分頁切換（切到 E2E 要把 Signal 的 .tiles 隱藏，兩邊計分不同套）
    """
    return """
const SHOTS = %s;
const ROUND = %s;
// TODO(行為層): 見 verdict.js_block docstring 的 1–6，從 0 重建
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
    """一條 (TC, field) 判定 → 一張卡片 HTML（骨架）。**唯一實作**，兩平台共用。

    卡片 dict 的欄位契約 —— 兩平台的組卡片程式都要產出這個形狀
    （`qa_aos.build()` 與 `qa_ios._card_from_result()`）：

      判定       tc field cat tier status_cls status_label
      期望/實際   signal expected actual provenance
      schema     schema_type schema_format schema_note
      說明       condition evidence_explanation
      限制       rd_note blocked_reason type_na absent_reason
      狀態重現    set shows mock_cmd mock_reset action
      證據       capture shot shot_data shot_caption shot_matched
                 bid_ids ground_truth attempts

    版面分兩面：
      正面＝判定（應有值 / 實際 / RESULT / 人工覆寫；自動 Blocked 的原因也放正面，
             不要逼人翻面才知道為什麼）
      背面＝證據（來源 capture、狀態截圖、bid 實際值、獨立裝置證據、retry 歷史、
             「如何證明」說明、TC 判定條件）

    原版版面：`git show 8307b56:verdict.py`
    """
    return (
        f'<article class="card" data-status="{esc(c["status_cls"])}" '
        f'data-auto="{esc(c["status_cls"])}" '
        f'data-key="{esc(c["tc"])}|{esc(c["field"])}">'
        f'<div class="card-top"><span class="tc">{esc(c["tc"])}</span>'
        f'<span class="tier">{esc(c.get("tier") or "—")}</span>'
        f'<span class="pill pill-{esc(c["status_cls"])}">{esc(c["status_label"])}</span></div>'
        f'<div class="field">{esc(c["field"])}</div>'
        f'<div class="signal">{esc(c.get("signal") or "")}</div>'
        f'<div class="result-kv">'
        f'<div class="result-block golden-block">'
        f'<span class="result-label">應有值</span><strong>{esc(c["expected"])}</strong></div>'
        f'<div class="result-block actual-block">'
        f'<span class="result-label">CAPTURE · 實際收到</span><strong>{esc(c["actual"])}</strong></div>'
        f'</div>'
        f'<!-- TODO(版面): 卡片背面（證據/截圖/如何證明）、人工覆寫、翻面互動 -->'
        f'</article>'
    )
