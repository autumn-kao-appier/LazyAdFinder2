"""iOS-owned E2E baseline validator for Standalone and Mediation."""

import json
import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from campaign_profiles import campaign_profile
from verdict import blocked, evaluate
from .e2e_shared_contracts import E2ETestCase, definitions


TESTCASES = definitions(
    E2ETestCase("standalone-sdk-init", "SDK Initialization", "Serving", "P0"),
    E2ETestCase("standalone-appier-ad-request", "Appier Direct Ad Request", "Serving", "P0"),
    E2ETestCase("standalone-creative-assets", "Creative Asset Loading", "Serving", "P1"),
    E2ETestCase("standalone-native-render", "Native Ad Rendering", "Serving", "P0"),
    E2ETestCase("standalone-impression", "Appier Impression Tracking", "Tracking", "P0"),
    E2ETestCase("standalone-click", "Appier Click Tracking", "Tracking", "P0"),
    E2ETestCase("standalone-landing", "Campaign Destination", "Tracking", "P1"),
    E2ETestCase("standalone-privacy", "Privacy Information", "Tracking", "P2"),
    E2ETestCase("standalone-install-attribution", "MMP Click Action", "Attribution", "P2"),
    E2ETestCase("standalone-attribution-reconciliation", "Attribution Recognition", "Attribution", "P2"),
)


def _json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


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


def _row(key, expected, actual, passed, evidence, success, failure):
    reason = success if passed else failure
    row = evaluate(key, expected=expected, actual=actual, evidence=evidence,
                   compare=lambda _e, _a: passed, reason=reason).to_dict()
    row.update({"layer": "E2E", "title": TESTCASES[key].title, "description": reason})
    return row


def _blocked(key, reason, actual=None, evidence="attribution-query.json"):
    row = blocked(key, reason).to_dict()
    row.update({"layer": "E2E", "title": TESTCASES[key].title,
                "description": reason, "actual": actual, "evidence": evidence})
    return row


def _responses(events, kind):
    return [row for row in events if row.get("phase") == "response" and row.get("kind") == kind]


def _requests(events, kind):
    return [row for row in events if row.get("phase") == "request" and row.get("kind") == kind]


def _ok(rows, redirects=True):
    statuses = {200, 204}
    if redirects:
        statuses |= {301, 302, 303, 307, 308}
    return bool(rows) and rows[-1].get("status") in statuses


def _ids(url):
    query = parse_qs(urlsplit(str(url or "")).query)
    return {key: values[0] for key in ("bidobjid", "cid", "crid", "crpid")
            if (values := query.get(key)) and values[0]}


def validate_bundle(folder):
    folder = Path(folder)
    summary = _json(folder / "summary.json", {}) or {}
    response = _json(folder / "bid_response.json", {}) or {}
    raw = _json(folder / "bid_raw.json", {}) or {}
    decoded = _json(folder / "bid_decoded.json", {}) or {}
    interactions = _json(folder / "e2e-interactions.json", {}) or {}
    visual = _json(folder / "visual-review.json", {}) or {}
    events = _events(folder)
    traffic_path = folder / "proxy-events.jsonl"
    traffic_bytes = traffic_path.read_bytes() if traffic_path.is_file() else b""
    traffic_session = {
        "saved": bool(traffic_bytes),
        "bytes": len(traffic_bytes),
        "event_count": len(events),
        "sha256": hashlib.sha256(traffic_bytes).hexdigest() if traffic_bytes else None,
        "first_timestamp": events[0].get("timestamp") if events else None,
        "last_timestamp": events[-1].get("timestamp") if events else None,
    }
    (folder / "traffic-session.json").write_text(json.dumps(traffic_session, ensure_ascii=False, indent=2) + "\n")
    profile = campaign_profile(summary.get("test_type"))

    bid_requests = [row for row in _requests(events, "bid")
                    if urlsplit(str(row.get("url", ""))).path == "/v2/sdk/ios/ad"]
    bid_responses = _responses(events, "bid")
    bid_request = bid_requests[-1] if bid_requests else None
    bid_response = next((row for row in reversed(bid_responses)
                         if bid_request and row.get("flow_id") == bid_request.get("flow_id")), None)
    app = (((decoded.get("req") or {}).get("plaintext") or {}).get("app") or {})
    ad_units = response.get("adUnits") if isinstance(response, dict) else None
    request_actual = {
        "request": bid_request, "response": bid_response, "cid": summary.get("cid"),
        "zone_id": raw.get("zone_id"), "bundle": app.get("bundle"),
        "sdk_version": app.get("sdk_version"),
        "request_body_saved": (folder / "bid_raw.json").is_file(),
        "response_body_saved": (folder / "bid_response.json").is_file(),
        "valid_ad_unit": bool(ad_units and isinstance(ad_units[0], dict) and ad_units[0].get("ad")),
    }
    request_ok = bool(traffic_session["saved"] and bid_request and bid_response and bid_response.get("status") == 200
                      and request_actual["request_body_saved"] and request_actual["response_body_saved"]
                      and request_actual["bundle"] and request_actual["sdk_version"]
                      and request_actual["zone_id"] and request_actual["valid_ad_unit"])
    (folder / "appier-ad-flow.json").write_text(json.dumps(request_actual, ensure_ascii=False, indent=2) + "\n")

    init = _responses(events, "sdk-init")
    init_ok = _ok(init, redirects=False)
    init_row = _row(
        "standalone-sdk-init", {"GET iOS init": "HTTP 200"}, init, init_ok,
        "e2e-network-evidence.json", "The iOS SDK initialization transaction succeeded."
        , "FAILED: no successful iOS SDK initialization transaction was preserved.")

    assets = _responses(events, "asset")
    asset_urls = []
    try:
        native = response["adUnits"][0]["ad"]["native"]
        asset_urls = [str((native.get(key) or {}).get("url") or "")
                      for key in ("iconImage", "mainImage", "privacyInformationIcon")]
        asset_urls = [url for url in asset_urls if url]
    except (KeyError, IndexError, TypeError):
        pass
    seen = {str(row.get("url") or "") for row in assets
            if row.get("status") in (200, 304) and str(row.get("content_type", "")).startswith("image/")}
    rendered_source = (folder / "rendered-page-source.xml").read_text(errors="replace") if (folder / "rendered-page-source.xml").is_file() else ""
    visible_image_count = len(re.findall(r'<XCUIElementTypeImage\b[^>]*\bvisible="true"', rendered_source))
    asset_network_ok = bool(asset_urls and set(asset_urls).issubset(seen))
    asset_visible_cache_ok = bool(
        asset_urls and (folder / "ad-before-interactions.png").is_file()
        and visible_image_count >= len(asset_urls)
    )
    assets_ok = asset_network_ok or asset_visible_cache_ok
    asset_actual = {
        "expected": asset_urls, "seen": sorted(seen),
        "proof_mode": "NETWORK" if asset_network_ok else "VISIBLE_CACHE" if asset_visible_cache_ok else "MISSING",
        "visible_image_count": visible_image_count,
        "screenshot": "ad-before-interactions.png" if (folder / "ad-before-interactions.png").is_file() else None,
    }

    impressions = _responses(events, "impression")
    wins = _responses(events, "impression-win")
    impression_ok = _ok(impressions) and _ok(wins, redirects=False)
    click_events = _responses(events, "click")
    impression_ids = [_ids(row.get("url")) for row in impressions]
    matching_clicks = [row for row in click_events if _ids(row.get("url")) in impression_ids and _ids(row.get("url"))]
    click_state = interactions.get("click") if isinstance(interactions, dict) else {}
    click_ok = bool(_ok(matching_clicks))

    privacy_state = interactions.get("privacy") if isinstance(interactions, dict) else {}
    privacy_ok = bool(privacy_state and privacy_state.get("attempted") and privacy_state.get("opened")
                      and (folder / "privacy-landing.png").is_file())
    destination = click_state.get("destination", {}) if isinstance(click_state, dict) else {}
    target = str(summary.get("target_app_package") or summary.get("target_app_bundle_id") or "")
    destination_id = str(destination.get("bundle_id") or destination.get("package") or "")
    destination_ok = bool(click_ok and click_state.get("opened") and (folder / "click-landing.png").is_file()
                          and (destination_id == target if profile.landing_contract == "target-app-deeplink" else destination_id))

    lookup_ids = _ids(matching_clicks[-1].get("url")) if matching_clicks else (impression_ids[-1] if impression_ids else {})
    lookup = {**lookup_ids, "cid": lookup_ids.get("cid") or summary.get("cid"),
              "bid_requested_at": bid_request.get("timestamp") if bid_request else None,
              "ad_clicked_at": matching_clicks[-1].get("timestamp") if matching_clicks else None}
    (folder / "attribution-query.json").write_text(json.dumps(lookup, ensure_ascii=False, indent=2) + "\n")
    network = {"init": init, "bid": request_actual, "assets": assets,
               "impressions": impressions, "wins": wins, "clicks": click_events,
               "matching_clicks": matching_clicks}
    (folder / "e2e-network-evidence.json").write_text(json.dumps(network, ensure_ascii=False, indent=2) + "\n")

    recording = interactions.get("recording", {}) if isinstance(interactions, dict) else {}
    recording_ok = bool(recording.get("saved") and recording.get("valid_mp4"))
    timeline = interactions.get("timeline", []) if isinstance(interactions, dict) else []
    completed_stages = {row.get("stage") for row in timeline if row.get("outcome") in {"CAPTURED", "COMPLETED", "SAVED"}}
    render_timeline_ok = "rendered-ad" in completed_stages
    privacy_timeline_ok = {"privacy-destination", "return-to-ad"}.issubset(completed_stages)
    click_timeline_ok = {"landing"}.issubset(completed_stages)
    render_ok = bool((folder / "ad-before-interactions.png").is_file() and response and visual.get("passed"))
    return [
        init_row,
        _row("standalone-appier-ad-request", {"POST /v2/sdk/ios/ad": "same-flow HTTP 200 with bodies"}, request_actual, request_ok, "appier-ad-flow.json", "The same proxy flow proves the iOS Appier request and response.", "FAILED: no complete same-flow iOS Appier ad transaction was preserved."),
        _row("standalone-creative-assets", {"all response assets": "network responses or visible cached rendering"}, asset_actual, assets_ok, "e2e-network-evidence.json", "All response-specified creative assets were proven by network responses or visible cached rendering.", "FAILED: one or more response-specified creative assets were neither captured in traffic nor visibly rendered."),
        _row("standalone-native-render", {"visible screenshot": True, "response-to-view comparison": True, "full valid recording": True, "timeline stage": "rendered-ad"}, {"visual_review": visual, "recording": recording, "timeline": timeline}, render_ok and recording_ok and render_timeline_ok, "ad-before-interactions.png", "The visible ad, response comparison, full recording and timeline prove rendering.", "FAILED: rendered-ad screenshot, objective visual comparison, valid full recording, or timeline stage is missing."),
        _row("standalone-impression", {"show_cb": True, "winshowimg": True}, {"show_cb": impressions, "winshowimg": wins}, impression_ok, "e2e-network-evidence.json", "The complete Appier impression chain was captured.", "FAILED: show_cb and winshowimg were not both captured successfully."),
        _row("standalone-click", {"matching xclk": "successful HTTP response or redirect"}, {"matching_xclk": matching_clicks}, click_ok, "e2e-network-evidence.json", "A successful xclk matching the visible impression was captured.", "FAILED: no successful xclk matching the visible impression was captured."),
        _row("standalone-landing", {"campaign destination": profile.landing_contract, "visible screenshot": True, "timeline": True}, {"destination": destination, "target": target, "timeline": timeline}, destination_ok and click_timeline_ok and recording_ok, "click-landing.png", "The tracked click, timeline and valid full recording preserved the campaign destination.", "FAILED: the tracked click did not preserve the required campaign destination in the timeline and valid full recording."),
        _row("standalone-privacy", {"privacy interaction": True, "visible destination": True, "return to ad": True, "timeline": True}, {"interaction": privacy_state, "timeline": timeline}, privacy_ok and privacy_timeline_ok and recording_ok, "privacy-landing.png", "The Privacy interaction, return step, timeline and valid full recording preserved its destination.", "FAILED: Privacy interaction, return step, visible destination, timeline or valid full recording Evidence is incomplete."),
        _blocked("standalone-install-attribution", f"Traffic lookup data is ready; query the MMP {profile.mmp_click_action} action.", lookup),
        _blocked("standalone-attribution-reconciliation", "Traffic lookup data is ready; backend attribution reconciliation still requires the authorized data query.", lookup),
    ]
