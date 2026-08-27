"""Android AdMob Mediation-only E2E extensions; S baseline is inherited."""

import gzip
import json
import re
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from verdict import blocked, evaluate

from .e2e_shared_contracts import E2ETestCase, definitions

try:
    import brotli
except ImportError:  # pragma: no cover - environment dependency is reported in Evidence
    brotli = None


TESTCASES = definitions(
    E2ETestCase("admob-pubsetting", "AdMob Pubsetting Mediation Config", "Serving", "P0"),
    E2ETestCase("admob-gma-request", "AdMob GMA Request and Mediation Routing", "Serving", "P0"),
    E2ETestCase("admob-appier-ad-request", "Appier Adapter Ad Request", "Serving", "P0"),
    E2ETestCase("admob-impression", "AdMob Impression Reporting", "Tracking", "P0"),
    E2ETestCase("admob-fill-result", "Mediation Fill Result", "Tracking", "P2"),
    E2ETestCase("admob-click", "AdMob Click Reporting", "Tracking", "P0"),
)


def _events(folder):
    path = Path(folder) / "proxy-events.jsonl"
    rows = []
    if path.is_file():
        for line in path.read_text(errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _responses(events, kind):
    return [row for row in events if row.get("phase") == "response" and row.get("kind") == kind]


def _requests(events, kind):
    return [row for row in events if row.get("phase") == "request" and row.get("kind") == kind]


def _body_facts(path):
    path = Path(path)
    if not path.is_file():
        return {"saved": False, "bytes": 0, "contains_appier": False, "zone_ids": []}
    raw = path.read_bytes()
    variants = [raw]
    decoders = [gzip.decompress, zlib.decompress]
    if brotli is not None:
        decoders.append(brotli.decompress)
    for decoder in decoders:
        try:
            variants.append(decoder(raw))
        except Exception:
            pass
    searchable = b"\n".join(variants)
    text = searchable.decode("utf-8", errors="ignore")
    parsed = None
    for variant in variants:
        try:
            parsed = json.loads(variant)
            break
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    class_names = []
    is_mediation = []
    status = None
    if isinstance(parsed, dict):
        status = parsed.get("status")
        for setting in parsed.get("ad_unit_settings", []):
            if not isinstance(setting, dict):
                continue
            is_mediation.append(setting.get("is_mediation"))
            networks = (setting.get("mediation_config") or {}).get("ad_networks", [])
            for network in networks:
                data = network.get("data", {}) if isinstance(network, dict) else {}
                if data.get("class_name"):
                    class_names.append(data["class_name"])
    return {
        "saved": True,
        "bytes": len(raw),
        "contains_appier": bool(re.search(r"appier|APRAdAdapter|AppierAdsAdMobMediation", text, re.I)),
        "zone_ids": sorted(set(re.findall(r"\b\d{4,8}\b", text)))[:30],
        "json_decoded": isinstance(parsed, dict),
        "status": status,
        "is_mediation": is_mediation,
        "class_names": class_names,
    }


def _row(key, expected, actual, passed, evidence, success, failure):
    testcase = TESTCASES[key]
    reason = success if passed else failure
    row = evaluate(
        key,
        expected=expected,
        actual=actual,
        evidence=evidence,
        compare=lambda _expected, _actual: passed,
        reason=reason,
    ).to_dict()
    row.update({"layer": "E2E", "title": testcase.title, "description": reason})
    if key == "admob-pubsetting":
        body = actual.get("body", {}) if isinstance(actual, dict) else {}
        responses = actual.get("responses", []) if isinstance(actual, dict) else []
        status = responses[-1].get("status") if responses and isinstance(responses[-1], dict) else None
        class_names = body.get("class_names") or []
        zone_ids = body.get("zone_ids") or []
        row["comparison_view"] = {
            "kind": "rule",
            "criterion": "HTTP 200, config status 1, Appier adapter, mediation=true, and a zone ID are required.",
            "actual": {
                "label": "Pubsetting summary",
                "value": (
                    f"HTTP {status or '—'} · Appier {'yes' if body.get('contains_appier') else 'no'} · "
                    f"mediation {'yes' if True in (body.get('is_mediation') or []) else 'no'} · "
                    f"zone {', '.join(map(str, zone_ids)) or '—'}"
                ),
            },
        }
    return row


def _blocked_row(key, reason, actual, evidence):
    testcase = TESTCASES[key]
    row = blocked(key, reason).to_dict()
    row.update({
        "actual": actual,
        "evidence": evidence,
        "layer": "E2E",
        "title": testcase.title,
        "description": reason,
    })
    return row


def _ok_status(rows, redirects=True):
    accepted = {200, 204}
    if redirects:
        accepted.update({301, 302, 303, 307, 308})
    return bool(rows) and rows[-1].get("status") in accepted


def validate_bundle(folder):
    folder = Path(folder)
    events = _events(folder)
    pubsetting_requests = _requests(events, "admob-pubsetting")
    pubsetting_responses = _responses(events, "admob-pubsetting")
    gma_requests = _requests(events, "admob-gma")
    gma_responses = _responses(events, "admob-gma")
    bid_requests = _requests(events, "bid")
    bid_responses = _responses(events, "bid")
    admob_impressions = _responses(events, "admob-impression")
    fill_results = _responses(events, "admob-fill-result")
    admob_clicks = _responses(events, "admob-click")
    pubsetting_body = _body_facts(folder / "admob-pubsetting-response.bin")
    gma_body = _body_facts(folder / "admob-gma-response.bin")

    pubsetting_ok = bool(
        _ok_status(pubsetting_responses, redirects=False)
        and pubsetting_body["contains_appier"]
        and pubsetting_body["status"] == 1
        and True in pubsetting_body["is_mediation"]
        and pubsetting_body["zone_ids"]
    )
    gma_ok = _ok_status(gma_responses, redirects=False) and gma_body["contains_appier"]
    gma_time = gma_requests[-1].get("timestamp", "") if gma_requests else ""
    later_bids = [row for row in bid_requests if row.get("timestamp", "") >= gma_time] if gma_time else []
    try:
        bid_raw = json.loads((folder / "bid_raw.json").read_text())
    except (OSError, json.JSONDecodeError):
        bid_raw = {}
    configured_zones = set(pubsetting_body["zone_ids"])
    request_zone = str(bid_raw.get("zone_id") or "")
    eligible_bid_requests = later_bids if gma_time else bid_requests
    successful_bid_pairs = []
    for request in eligible_bid_requests:
        flow_id = request.get("flow_id")
        response = next(
            (
                row for row in bid_responses
                if flow_id and row.get("flow_id") == flow_id and row.get("status") in (200, 204)
            ),
            None,
        )
        if response:
            successful_bid_pairs.append({"request": request, "response": response})
    appier_ok = bool(
        pubsetting_ok and successful_bid_pairs
        and request_zone and request_zone in configured_zones
    )

    impression_ok = _ok_status(admob_impressions)
    fill_ok = _ok_status(fill_results)
    fill_queries = [parse_qs(urlsplit(str(row.get("url", ""))).query) for row in fill_results]
    click_ok = _ok_status(admob_clicks)

    evidence = {
        "pubsetting": {"requests": pubsetting_requests, "responses": pubsetting_responses, "body": pubsetting_body},
        "gma": {
            "requests": gma_requests,
            "responses": gma_responses,
            "body": gma_body,
            "direct_transaction_observable": bool(gma_requests or gma_responses),
        },
        "appier_after_gma": {
            "proof_path": "explicit-gma-to-appier" if gma_time else "pubsetting-to-appier-bid",
            "bid_requests": eligible_bid_requests,
            "bid_responses": bid_responses,
            "successful_bid_pairs": successful_bid_pairs,
            "request_zone": request_zone,
            "configured_zones": sorted(configured_zones),
        },
        "admob_impressions": admob_impressions,
        "fill_results": fill_results,
        "fill_queries": fill_queries,
        "admob_clicks": admob_clicks,
    }
    (folder / "mediation-network-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    )

    if gma_requests or gma_responses or gma_body["saved"]:
        gma_row = _row(
            "admob-gma-request",
            {"http_success": True, "response_body_saved": True, "contains_appier_routing": True},
            evidence["gma"], gma_ok, "mediation-network-evidence.json",
            "The captured GMA transaction succeeded and its response contains Appier routing evidence.",
            "FAILED: the captured GMA transaction or raw response does not prove Appier mediation routing.",
        )
    else:
        gma_row = _blocked_row(
            "admob-gma-request",
            "Evidence unavailable: this run emitted no observable /mads/gma transaction, so the direct GMA response contract cannot be evaluated.",
            evidence["gma"],
            "mediation-network-evidence.json",
        )

    return [
        _row(
            "admob-pubsetting",
            {"http_success": True, "response_body_saved": True, "contains_appier_adapter": True},
            evidence["pubsetting"], pubsetting_ok, "mediation-network-evidence.json",
            "The captured pubsetting response succeeded and contains Appier mediation configuration.",
            "FAILED: pubsetting transport or raw response evidence does not prove the Appier mediation configuration.",
        ),
        gma_row,
        _row(
            "admob-appier-ad-request",
            {"mediation_configuration_proven": True, "appier_bid_flow_success": True, "zone_matches": True},
            evidence["appier_after_gma"], appier_ok, "mediation-network-evidence.json",
            "The proxy evidence proves the configured Appier adapter emitted a successful, zone-matched bid flow.",
            "FAILED: no successful, zone-matched Appier adapter request/response flow was captured for the mediation configuration.",
        ),
        _row(
            "admob-impression",
            {"google_impression_event_success": True}, admob_impressions, impression_ok,
            "mediation-network-evidence.json",
            "The Google mediation impression event was captured successfully.",
            "FAILED: the executed Mediation round has no successful Google impression event evidence.",
        ),
        _row(
            "admob-fill-result",
            {"fill_result_success": True, "status_matches_actual_fill": True},
            {"events": fill_results, "queries": fill_queries}, fill_ok,
            "mediation-network-evidence.json",
            "The mediation fill-result event was captured successfully.",
            "FAILED: the executed Mediation round has no successful fill-result event evidence.",
        ),
        _row(
            "admob-click",
            {"admob_click_event_success": True}, admob_clicks, click_ok,
            "mediation-network-evidence.json",
            "The AdMob click-reporting event was captured successfully.",
            "FAILED: the executed Mediation round has no successful AdMob click-reporting evidence.",
        ),
    ]
