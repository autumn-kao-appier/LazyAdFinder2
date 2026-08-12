"""iOS-owned AdMob Mediation E2E extensions."""

import gzip
import json
import re
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from verdict import evaluate
from .e2e_shared_contracts import E2ETestCase, definitions


TESTCASES = definitions(
    E2ETestCase("admob-pubsetting", "AdMob Pubsetting Mediation Config", "Serving", "P0"),
    E2ETestCase("admob-gma-request", "AdMob GMA Request and Mediation Routing", "Serving", "P0"),
    E2ETestCase("admob-appier-ad-request", "Appier Adapter Ad Request", "Serving", "P0"),
    E2ETestCase("admob-impression", "AdMob Impression Reporting", "Tracking", "P0"),
    E2ETestCase("admob-fill-result", "Mediation Fill Result", "Tracking", "P2"),
    E2ETestCase("admob-click", "AdMob Click Reporting", "Tracking", "P0"),
)


def _events(folder):
    rows = []
    path = Path(folder) / "proxy-events.jsonl"
    if path.is_file():
        for line in path.read_text(errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _select(events, kind, phase):
    return [row for row in events if row.get("kind") == kind and row.get("phase") == phase]


def _ok(rows, redirects=True):
    statuses = {200, 204}
    if redirects:
        statuses |= {301, 302, 303, 307, 308}
    return bool(rows) and rows[-1].get("status") in statuses


def _body(path):
    path = Path(path)
    if not path.is_file():
        return {"saved": False, "contains_appier": False, "zones": [], "status": None, "mediation": []}
    raw = path.read_bytes()
    variants = [raw]
    for decoder in (gzip.decompress, zlib.decompress):
        try:
            variants.append(decoder(raw))
        except Exception:
            pass
    text = b"\n".join(variants).decode(errors="ignore")
    parsed = None
    for variant in variants:
        try:
            parsed = json.loads(variant)
            break
        except Exception:
            pass
    mediation = []
    class_names = []
    if isinstance(parsed, dict):
        for setting in parsed.get("ad_unit_settings", []):
            if not isinstance(setting, dict):
                continue
            mediation.append(setting.get("is_mediation"))
            for network in ((setting.get("mediation_config") or {}).get("ad_networks") or []):
                data = network.get("data") if isinstance(network, dict) else None
                if isinstance(data, dict) and data.get("class_name"):
                    class_names.append(data["class_name"])
    return {"saved": True, "bytes": len(raw), "contains_appier": bool(re.search(r"Appier|APRAdAdapter", text, re.I)),
            "zones": sorted(set(re.findall(r"\b\d{4,8}\b", text)))[:30],
            "status": parsed.get("status") if isinstance(parsed, dict) else None,
            "mediation": mediation, "class_names": class_names}


def _row(key, expected, actual, passed, success, failure):
    reason = success if passed else failure
    row = evaluate(key, expected=expected, actual=actual,
                   evidence="mediation-network-evidence.json",
                   compare=lambda _e, _a: passed, reason=reason).to_dict()
    row.update({"layer": "E2E", "title": TESTCASES[key].title, "description": reason})
    return row


def validate_bundle(folder):
    folder = Path(folder)
    events = _events(folder)
    pub_req, pub_res = _select(events, "admob-pubsetting", "request"), _select(events, "admob-pubsetting", "response")
    gma_req, gma_res = _select(events, "admob-gma", "request"), _select(events, "admob-gma", "response")
    bid_req, bid_res = _select(events, "bid", "request"), _select(events, "bid", "response")
    imp = _select(events, "admob-impression", "response")
    fill = _select(events, "admob-fill-result", "response")
    clicks = _select(events, "admob-click", "response")
    pub_body = _body(folder / "admob-pubsetting-response.bin")
    gma_body = _body(folder / "admob-gma-response.bin")
    pub_ok = _ok(pub_res, False) and pub_body["contains_appier"] and pub_body["status"] == 1 and True in pub_body["mediation"] and bool(pub_body["zones"])
    gma_ok = _ok(gma_res, False) and gma_body["contains_appier"]
    gma_time = gma_req[-1].get("timestamp") if gma_req else None
    later_bid = [row for row in bid_req if not gma_time or row.get("timestamp", "") >= gma_time]
    try:
        raw = json.loads((folder / "bid_raw.json").read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    zone = str(raw.get("zone_id") or "")
    appier_ok = bool(gma_req and later_bid and _ok(bid_res, False) and zone in set(pub_body["zones"]))
    fill_queries = [parse_qs(urlsplit(str(row.get("url", ""))).query) for row in fill]
    evidence = {"pubsetting": {"requests": pub_req, "responses": pub_res, "body": pub_body},
                "gma": {"requests": gma_req, "responses": gma_res, "body": gma_body},
                "appier_after_gma": {"requests": later_bid, "responses": bid_res, "zone": zone},
                "impressions": imp, "fill_results": fill, "fill_queries": fill_queries, "clicks": clicks}
    (folder / "mediation-network-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    return [
        _row("admob-pubsetting", {"HTTP": 200, "status": 1, "Appier adapter": True, "mediation": True, "zone": True}, evidence["pubsetting"], pub_ok, "Pubsetting proves the Appier iOS adapter and zone configuration.", "FAILED: Pubsetting does not prove a complete Appier iOS mediation configuration."),
        _row("admob-gma-request", {"successful GMA flow": True, "Appier routing": True}, evidence["gma"], gma_ok, "The GMA response proves Appier mediation routing.", "FAILED: no successful GMA response with Appier routing was preserved."),
        _row("admob-appier-ad-request", {"ordered GMA to Appier flow": True, "zone matches": True}, evidence["appier_after_gma"], appier_ok, "The timeline proves GMA invoked the Appier iOS adapter with the configured zone.", "FAILED: no ordered GMA-to-Appier iOS adapter flow was proven."),
        _row("admob-impression", {"successful Google impression": True}, imp, _ok(imp), "Google impression reporting succeeded.", "FAILED: no successful Google impression event was captured."),
        _row("admob-fill-result", {"successful fill result": True}, {"events": fill, "queries": fill_queries}, _ok(fill), "Mediation fill-result reporting succeeded.", "FAILED: no successful mediation fill-result event was captured."),
        _row("admob-click", {"successful Google click": True}, clicks, _ok(clicks), "Google click reporting succeeded.", "FAILED: no successful Google click event was captured."),
    ]
