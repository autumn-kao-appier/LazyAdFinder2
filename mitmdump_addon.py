"""
mitmproxy addon: 偵測 Appier bid request/response，capture bid 資料。

Bid endpoints:
    POST https://ad3.apx.appier.net/v2/sdk/aos/ad      (production)
    POST https://adx-stg.apx.appier.net/v2/sdk/aos/ad  (staging)
    POST https://ad3.apx.appier.net/v2/sdk/ios/ad      (iOS production)
    request body 為未壓縮 UTF-8 JSON；response 200 = bid、204 = no-bid

只有 bid request 會寫 FLAG_FILE。Impression callback 另寫自己的事件檔；其他流量
直接忽略，避免不同事件共用同一個狀態語意。

用法（terminal 1）:
    mitmdump -s ~/LazyAdFinder2/mitmdump_addon.py --listen-port 8081
"""

import gzip
import json as _json
from datetime import datetime, timezone
import zlib
from urllib.parse import parse_qs, urlsplit
from mitmproxy import ctx, http

FLAG_FILE = "/tmp/appier_hit"
BID_FILE = "/tmp/appier_bid.json"
BID_STATUS_FILE = "/tmp/appier_bid_status"
BID_RESPONSE_FILE = "/tmp/appier_bid_response.json"
IMPRESSION_FILE = "/tmp/appier_impression.json"
EVENTS_FILE = "/tmp/appier_proxy_events.jsonl"
BID_HOST_SUFFIX = "apx.appier.net"
BID_PATHS = ("/v2/sdk/aos/ad", "/v2/sdk/ios/ad")
# 「已展示」callback（非 bid 端點，未被 cert pinning 排除，明碼 GET）：
# 2026-07-20 實機觀察到 iOS standalone/mediation 中獎後都會打這支，帶 cid/crid/
# crpid/bidobjid/idfa 等識別碼在 query string——bid 端點本身因 pinning 看不到內容時，
# 這是唯一能拿到「這輪確實中獎、中的是哪個 creative」證據的地方。
IMPRESSION_PATH = "/callback/show_cb"


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _event_kind(flow):
    host = flow.request.host.lower()
    path = flow.request.path.lower()
    if _is_bid(flow):
        return "bid"
    if path.startswith(IMPRESSION_PATH) and (
        host.endswith(".c.appier.net") or host == "c.appier.net"
    ):
        return "impression"
    if "winshowimg" in path or "winshowimg" in host:
        return "impression-win"
    if "xclk" in path or host.startswith("tw.c.appier.net"):
        return "click"
    if "init" in path and "appier" in host:
        return "sdk-init"
    return None


def _append_event(flow, phase):
    kind = _event_kind(flow)
    response = flow.response
    content_type = response.headers.get("content-type", "") if response else ""
    if kind is None and response is not None and content_type.lower().startswith("image/"):
        kind = "asset"
    if kind is None:
        return
    row = {
        "timestamp": _timestamp(),
        "phase": phase,
        "kind": kind,
        "method": flow.request.method,
        "url": flow.request.pretty_url,
    }
    if response is not None:
        row.update({
            "status": response.status_code,
            "content_type": content_type,
            "content_length": len(response.raw_content or b""),
            "location": response.headers.get("location"),
        })
    with open(EVENTS_FILE, "a") as f:
        f.write(_json.dumps(row, ensure_ascii=False) + "\n")

def _parse_body(content):
    """Try JSON parse with gzip/deflate fallback."""
    for attempt in (
        lambda b: _json.loads(b),
        lambda b: _json.loads(gzip.decompress(b)),
        lambda b: _json.loads(zlib.decompress(b)),
        lambda b: _json.loads(zlib.decompress(b, -15)),
    ):
        try:
            return attempt(content)
        except Exception:
            continue
    return None


def _is_bid(flow: http.HTTPFlow) -> bool:
    return (
        flow.request.host.endswith(BID_HOST_SUFFIX)
        and any(flow.request.path.startswith(path) for path in BID_PATHS)
        and flow.request.method == "POST"
    )


def _is_impression_win(flow: http.HTTPFlow) -> bool:
    host = flow.request.host.lower()
    return (
        flow.request.path.startswith(IMPRESSION_PATH)
        and (host.endswith(".c.appier.net") or host == "c.appier.net")
    )


def _save_json(path, content):
    parsed = _parse_body(content)
    if parsed is None:
        return False
    with open(path, "w") as f:
        _json.dump(parsed, f, indent=2)
    return True


class AppierDetector:
    def running(self):
        # SSL passthrough 白名單，對齊 Charles 的 sslExcludeLocations，避免攔截破壞：
        #   - apple / mzstatic / icloud：Apple 服務會 pin
        #   - *google.com / *googleapis.com：Google 服務 + Android App Links 驗證
        #     （digitalassetlinks 走 googleapis）；攔截會讓 deeplink 驗不過 → 退回瀏覽器、不開 app
        #   - approov：API 防護/pinning 服務，攔截必失敗
        #   - dcard：Charles 既有排除
        # TEST 2026-07-20：apx.appier.net 原本也在此清單（理由：「cert pinning，
        # mitmproxy 特有需求」）。查 Charles 設定檔（com.xk72.charles.config）
        # 發現 Charles 的 sslExcludeLocations 完全沒有排除這個 host（include list
        # 是萬用字元 *），代表當初排除的其實只有 mitmdump 自己的 ignore_hosts，
        # 不是 Charles/SDK 層級的必然限制。先拿掉這行實測：如果是真 pinning，
        # 這支手機下一次觸發廣告會直接連線失敗/TLS handshake error；如果不是，
        # 就能直接看到 bid_request.json 的真實內容。視結果決定要不要留著這行。
        #   - adpolicy.appier.com：privacy icon 落地頁（TC-11），開在 WebView/Chrome，
        #     不信任 mitmproxy CA → 攔截會 TLS 失敗、頁面被擋。passthrough 讓它正常載入
        #     （Charles 的 sslExcludeLocations 也已排除它）；驗證改看 privacy_landing.png。
        ctx.options.ignore_hosts = [
            r".*\.apple\.com", r".*\.mzstatic\.com", r".*\.icloud\.com",
            r".*google\.com", r".*googleapis\.com",
            r".*approov.*", r".*dcard.*",
            r".*adpolicy\.appier\.com",
        ]

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.host
        entry = f"{flow.request.method} https://{host}{flow.request.path}"
        _append_event(flow, "request")

        if _is_bid(flow):
            if flow.request.content:
                as_json = _save_json(BID_FILE, flow.request.content)
                if as_json:
                    print(f">>> BID SAVED → {BID_FILE}")
                else:
                    print(">>> BID REQUEST body is not valid JSON; evidence not written")
            with open(FLAG_FILE, "w") as f:
                f.write(entry)
            print(f"\n>>> APPIER BID REQUEST: {entry}\n")
        elif _is_impression_win(flow):
            # bid 端點本身因 cert pinning 看不到內容（BID_FILE 不會產生）；
            # 用這支明碼 callback 的 query string 當作等效 win 信號 + 識別碼來源。
            qs = parse_qs(urlsplit(flow.request.path).query)
            ids = {k: v[0] for k, v in qs.items() if v and v[0]}
            with open(IMPRESSION_FILE, "w") as f:
                _json.dump(ids, f, indent=2)
            print(f">>> IMPRESSION WIN (from tracker callback, bid body unavailable) "
                  f"→ {IMPRESSION_FILE}  cid={ids.get('cid')} crid={ids.get('crid')}")

    def response(self, flow: http.HTTPFlow) -> None:
        _append_event(flow, "response")
        if not _is_bid(flow) or flow.response is None:
            return
        status = flow.response.status_code
        with open(BID_STATUS_FILE, "w") as f:
            f.write(str(status))
        if status == 200 and flow.response.content:
            if _save_json(BID_RESPONSE_FILE, flow.response.content):
                print(f">>> BID RESPONSE 200 → {BID_RESPONSE_FILE}")
            else:
                print(">>> BID RESPONSE 200 body is not valid JSON; evidence not written")
        else:
            print(f">>> BID RESPONSE {status}" + (" (no-bid)" if status == 204 else ""))


addons = [AppierDetector()]
