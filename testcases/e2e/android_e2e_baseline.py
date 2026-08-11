"""Android E2E S baseline shared by Standalone and Mediation."""

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from campaign_profiles import campaign_profile
from campaign_testcases import supports as campaign_supports
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


def _blocked(key, reason, *, actual=None, evidence=None):
    testcase = TESTCASES[key]
    row = blocked(key, reason).to_dict()
    if actual is not None:
        row["actual"] = actual
    if evidence:
        row["evidence"] = evidence
    row.update({"layer": "E2E", "title": testcase.title, "description": reason})
    return row


def _local_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return str(value)


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _read_events(folder):
    rows = []
    path = Path(folder) / "proxy-events.jsonl"
    if not path.is_file():
        return rows
    for line in path.read_text(errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _evaluated(key, expected, actual, passed, evidence, reason=""):
    row = evaluate(
        key,
        expected=expected,
        actual=actual,
        evidence=evidence,
        compare=lambda _expected, _actual: passed,
        reason=reason,
    ).to_dict()
    row.update({
        "layer": "E2E",
        "title": TESTCASES[key].title,
        "description": reason or "The captured evidence was compared with the reviewed E2E contract.",
    })
    return row


def _response_events(events, kind):
    return [row for row in events if row.get("phase") == "response" and row.get("kind") == kind]


def _bid_flow(events):
    requests = [
        row for row in events
        if row.get("kind") == "bid"
        and row.get("phase") == "request"
        and row.get("method") == "POST"
        and urlsplit(str(row.get("url", ""))).path == "/v2/sdk/aos/ad"
    ]
    responses = [row for row in events if row.get("kind") == "bid" and row.get("phase") == "response"]
    for request in reversed(requests):
        flow_id = request.get("flow_id")
        if not flow_id:
            continue
        response = next((row for row in reversed(responses) if row.get("flow_id") == flow_id), None)
        if response is not None:
            return request, response
    return None, None


def _creative_contract(response):
    try:
        ad = response["adUnits"][0]["ad"]
        native = ad["native"]
    except (KeyError, IndexError, TypeError):
        return {"click_url": "", "privacy_url": "", "asset_urls": {}}
    return {
        "click_url": str(ad.get("clk") or ""),
        "privacy_url": str((native.get("privacyInformationLink") or {}).get("url") or ""),
        "asset_urls": {
            key: str((native.get(key) or {}).get("url") or "")
            for key in ("iconImage", "mainImage", "privacyInformationIcon")
            if str((native.get(key) or {}).get("url") or "")
        },
    }


def _tracking_ids(url):
    query = parse_qs(urlsplit(str(url or "")).query)
    return {
        key: values[0]
        for key in ("bidobjid", "cid", "crid", "crpid")
        if (values := query.get(key)) and values[0]
    }


def validate_bundle(folder):
    """Validate network facts and preserve manual gates for visual/external facts."""
    folder = Path(folder)
    summary = json.loads((folder / "summary.json").read_text())
    profile = campaign_profile(summary.get("test_type"))
    events = _read_events(folder)
    decoded = _read_json(folder / "bid_decoded.json", {}) or {}
    response = _read_json(folder / "bid_response.json")
    contract = _creative_contract(response)
    interactions = _read_json(folder / "e2e-interactions.json", {}) or {}
    visual_review = _read_json(folder / "visual-review.json", {}) or {}
    bid_request_event, bid_response_event = _bid_flow(events)
    raw_exists = (folder / "bid_raw.json").is_file()
    status = str(summary.get("http_status") or "")
    cid = summary.get("cid")
    req_plain = decoded.get("req", {}).get("plaintext", {})
    app = req_plain.get("app", {}) if isinstance(req_plain, dict) else {}
    request_actual = {
        "flow_id": bid_request_event.get("flow_id") if bid_request_event else None,
        "method": bid_request_event.get("method") if bid_request_event else None,
        "endpoint": bid_request_event.get("url") if bid_request_event else None,
        "captured_at": bid_request_event.get("timestamp") if bid_request_event else None,
        "http_status": bid_response_event.get("status") if bid_response_event else None,
        "cid": cid,
        "zone_id": (_read_json(folder / "bid_raw.json", {}) or {}).get("zone_id"),
        "bundle": app.get("bundle"),
        "sdk_version": app.get("sdk_version"),
        "raw_request_saved": raw_exists,
        "response_saved": response is not None,
        "ad_units": len(response.get("adUnits", [])) if isinstance(response, dict) and isinstance(response.get("adUnits"), list) else 0,
    }
    ad_units = response.get("adUnits", []) if isinstance(response, dict) else []
    valid_ad_unit = bool(ad_units) and isinstance(ad_units[0], dict) and bool(ad_units[0].get("ad"))
    request_minimum = bid_response_event is not None and bid_response_event.get("status") == 200 and bool(cid) and raw_exists and all(
        request_actual.get(field) for field in ("flow_id", "zone_id", "bundle", "sdk_version")
    ) and valid_ad_unit
    flow_evidence = {
        "expected": {
            "method": "POST",
            "path": "/v2/sdk/aos/ad",
            "same_request_response_flow": True,
            "http_status": 200,
            "request_body_saved": True,
            "response_has_valid_ad_unit": True,
        },
        "actual": request_actual,
        "note": "This record is derived from the captured proxy traffic flow; bid_raw.json and bid_response.json preserve the bodies from that same transaction.",
    }
    (folder / "appier-ad-flow.json").write_text(
        json.dumps(flow_evidence, ensure_ascii=False, indent=2) + "\n"
    )
    if bid_request_event is None or bid_response_event is None:
        request_row = _evaluated(
            "standalone-appier-ad-request",
            {"method": "POST", "path": "/v2/sdk/aos/ad", "same_flow": True, "http_status": 200},
            request_actual,
            False,
            "appier-ad-flow.json",
            "FAILED: the round ran, but no preserved proxy transaction proves that the request and response belong to the same Appier ad flow.",
        )
    elif response is None or not raw_exists:
        request_row = _evaluated(
            "standalone-appier-ad-request",
            {"request_body_saved": True, "response_body_saved": True},
            request_actual,
            False,
            "appier-ad-flow.json",
            "FAILED: the proxy flow exists, but its request or response body was not preserved.",
        )
    else:
        request_row = _evaluated(
            "standalone-appier-ad-request",
            {"method": "POST", "path": "/v2/sdk/aos/ad", "same_flow": True, "http_status": 200, "request_fields_complete": True, "valid_ad_unit": True},
            request_actual,
            request_minimum and bool(response),
            "appier-ad-flow.json",
            "The proxy traffic session proves a POST /v2/sdk/aos/ad transaction and preserves both bodies from the same flow.",
        )

    init_responses = _response_events(events, "sdk-init")
    init_row = _blocked(
        "standalone-sdk-init",
        "Platform definition: Android Ads SDK has no standalone Init endpoint; keep BLOCKED while deciding whether AOS needs an equivalent contract aligned with the iOS Init flow",
    )

    creative_urls = set(contract["asset_urls"].values())
    asset_responses = [
        row for row in _response_events(events, "asset")
        if row.get("method") in {"GET", "HEAD"} and str(row.get("url") or "") in creative_urls
    ]
    screenshot_exists = (folder / "screenshot.png").is_file() and (folder / "screenshot.png").stat().st_size > 0
    observed_transport_ok = all(
        row.get("status") in (200, 304)
        and str(row.get("content_type", "")).lower().startswith("image/")
        and (
            row.get("method") == "HEAD"
            or row.get("status") == 304
            or int(row.get("content_length") or 0) > 0
        )
        for row in asset_responses
    )
    rendered_assets_ok = bool(
        visual_review.get("passed")
        and visual_review.get("checks", {}).get("returned_images_have_rendered_views")
    )
    asset_transport_ok = bool(creative_urls and observed_transport_ok and rendered_assets_ok)
    asset_actual = {
        "response_asset_urls": contract["asset_urls"],
        "observed_asset_responses": asset_responses,
        "unobserved_urls_are_allowed_only_with_rendered_view_evidence": True,
        "rendered_assets_verified": rendered_assets_ok,
    }
    if not request_minimum:
        asset_row = _evaluated(
            "standalone-creative-assets",
            {"specified_cid_confirmed": True},
            {"cid": cid, "appier_request_passed": request_minimum},
            False,
            "appier-ad-flow.json",
            "FAILED: the round ran, but the specified CID was not proven by the Appier ad request flow.",
        )
    elif not screenshot_exists:
        asset_row = _evaluated(
            "standalone-creative-assets",
            {"rendered_ad_screenshot": True},
            {"cid": cid, "screenshot_saved": False},
            False,
            "summary.json",
            "FAILED: the specified CID was confirmed, but no rendered-ad screenshot was saved.",
        )
    elif not creative_urls:
        asset_row = _evaluated(
            "standalone-creative-assets",
            {"creative_asset_urls_in_response": True},
            asset_actual,
            False,
            "bid_response.json",
            "FAILED: the captured ad response does not specify any creative asset URL.",
        )
    elif not asset_transport_ok:
        asset_row = _evaluated(
            "standalone-creative-assets",
            {"observed_assets_http_200_or_cached_304": True, "image_mime": True, "rendered_assets_visible": True},
            asset_actual,
            False,
            "e2e-network-evidence.json",
            "At least one captured creative asset failed its transport check.",
        )
    else:
        asset_row = _evaluated(
            "standalone-creative-assets",
            {"specified_cid_confirmed": True, "rendered_ad_screenshot": True, "asset_transport_ok_or_rendered_cache": True},
            {"cid": cid, "screenshot": "screenshot.png", **asset_actual},
            True,
            "screenshot.png",
            "The response-specified creative assets either loaded successfully in traffic or were proven as rendered cached views in the saved screenshot.",
        )

    impressions = _response_events(events, "impression")
    wins = _response_events(events, "impression-win")
    if impressions and wins:
        impression_actual = {"show_cb": impressions[-1], "winshowimg": wins[-1]}
        impression_ok = impressions[-1].get("status") in (200, 301, 302, 303, 307, 308) and wins[-1].get("status") == 200
        impression_row = _evaluated(
            "standalone-impression",
            {"show_cb_redirect_or_success": True, "winshowimg_http_200": True},
            impression_actual,
            impression_ok,
            "e2e-network-evidence.json",
            "The complete Appier impression callback chain was captured.",
        )
    else:
        impression_row = _evaluated(
            "standalone-impression",
            {"show_cb_response": True, "winshowimg_response": True},
            {"show_cb_responses": len(impressions), "winshowimg_responses": len(wins)},
            False,
            "e2e-network-evidence.json",
            "FAILED: the round ran, but the proxy evidence does not contain both show_cb and winshowimg responses.",
        )

    click_responses = _response_events(events, "click")
    impression_ids = [_tracking_ids(row.get("url")) for row in impressions]
    matching_clicks = []
    for row in click_responses:
        click_ids = _tracking_ids(row.get("url"))
        if click_ids and any(click_ids == ids for ids in impression_ids):
            matching_clicks.append(row)
    click_state = interactions.get("click", {}) if isinstance(interactions, dict) else {}
    click_screenshot = (folder / "click-landing.png").is_file()
    click_ok = bool(
        click_state.get("attempted")
        and matching_clicks
        and matching_clicks[-1].get("status") in (200, 301, 302, 303, 307, 308)
        and click_screenshot
    )
    click_row = _evaluated(
        "standalone-click",
        {"xclk_matches_visible_impression": True, "http_success_or_redirect": True, "landing_screenshot": True},
        {
            "attempted": bool(click_state.get("attempted")),
            "expected_clk": contract["click_url"],
            "visible_impression_ids": impression_ids,
            "matching_proxy_responses": matching_clicks,
            "landing_screenshot_saved": click_screenshot,
        },
        click_ok,
        "e2e-interactions.mp4" if (folder / "e2e-interactions.mp4").is_file() else "e2e-interactions.json",
        "The recorded CTA interaction emitted an xclk whose correlation IDs match the visible impression, and preserved its response." if click_ok else "FAILED: the E2E round does not prove the CTA click with visible interaction evidence and an xclk matching the visible impression.",
    )

    # Preserve the exact lookup key for the exposure that led to the tested
    # click.  A round may contain several bids, so using the last bid request
    # or CID alone is not sufficient for later Spark/MMP reconciliation.
    selected_click = matching_clicks[-1] if matching_clicks else None
    selected_ids = _tracking_ids(selected_click.get("url")) if selected_click else (
        impression_ids[-1] if impression_ids else {}
    )
    selected_impression = next(
        (
            row for row in reversed(impressions)
            if _tracking_ids(row.get("url")) == selected_ids
        ),
        impressions[-1] if impressions else None,
    )
    attribution_lookup = {
        "purpose": "Lookup key for E2E-S15 MMP click action and E2E-S16 attribution recognition",
        "bidobjid": selected_ids.get("bidobjid"),
        "cid": selected_ids.get("cid") or cid,
        "crid": selected_ids.get("crid"),
        "crpid": selected_ids.get("crpid"),
        "bid_requested_at": _local_time(bid_request_event.get("timestamp")) if bid_request_event else None,
        "ad_impression_at": _local_time(selected_impression.get("timestamp")) if selected_impression else None,
        "ad_clicked_at": _local_time(selected_click.get("timestamp")) if selected_click else None,
        "source": "proxy-events.jsonl",
        "note": "ad_impression_at is the test ad display time; query backend systems with bidobjid and this time window.",
    }
    (folder / "attribution-query.json").write_text(
        json.dumps(attribution_lookup, ensure_ascii=False, indent=2) + "\n"
    )

    destination = click_state.get("destination", {}) if isinstance(click_state, dict) else {}
    destination_package = str(destination.get("package") or "") if isinstance(destination, dict) else ""
    target_app_package = str(summary.get("target_app_package") or "")
    if profile.landing_contract == "target-app-deeplink":
        destination_matches = bool(target_app_package and destination_package == target_app_package)
    else:
        destination_matches = bool(destination_package and destination_package != summary.get("app_package"))
    landing_ok = bool(
        click_ok and click_state.get("opened") and click_screenshot and destination
        and destination_matches
    )
    landing_row = _evaluated(
        "standalone-landing",
        {
            "click_tracking_passed": True,
            "landing_contract": profile.landing_contract,
            "target_app_package": target_app_package or "external install destination",
            "landing_screenshot": True,
        },
        {
            "click_tracking_passed": click_ok,
            "opened": bool(click_state.get("opened")),
            "destination": destination,
            "destination_matches_campaign_contract": destination_matches,
            "landing_screenshot_saved": click_screenshot,
        },
        landing_ok,
        "click-landing.png" if click_screenshot else "e2e-interactions.json",
        "The tracked ad click opened the campaign's required destination and preserved it as visible evidence." if landing_ok else "FAILED: the tracked click did not prove the campaign's required destination; REEN must open the configured target App.",
    )

    privacy_state = interactions.get("privacy", {}) if isinstance(interactions, dict) else {}
    privacy_screenshot = (folder / "privacy-landing.png").is_file()
    privacy_destination = privacy_state.get("destination", {}) if isinstance(privacy_state, dict) else {}
    privacy_ok = bool(
        contract["privacy_url"]
        and privacy_state.get("attempted")
        and (
            privacy_state.get("opened")
            or str(privacy_destination.get("activity", "")).endswith("AppierBrowserActivity")
        )
        and privacy_destination
        and privacy_screenshot
    )
    privacy_row = _evaluated(
        "standalone-privacy",
        {"response_privacy_url": True, "privacy_icon_clicked": True, "external_destination_opened": True, "screenshot": True},
        {
            "expected_privacy_url": contract["privacy_url"],
            "attempted": bool(privacy_state.get("attempted")),
            "opened": bool(
                privacy_state.get("opened")
                or str(privacy_destination.get("activity", "")).endswith("AppierBrowserActivity")
            ),
            "destination": privacy_destination,
            "screenshot_saved": privacy_screenshot,
        },
        privacy_ok,
        "privacy-landing.png" if privacy_screenshot else "e2e-interactions.json",
        "The Privacy icon interaction opened an external destination and preserved the visible result alongside the response contract." if privacy_ok else "FAILED: the E2E round does not prove the Privacy icon destination with response data, an executed interaction, and a visible screenshot.",
    )

    network_evidence = {
        "request": request_actual,
        "init_responses": init_responses,
        "asset_responses": asset_responses,
        "impression_responses": impressions,
        "winshowimg_responses": wins,
    }
    (folder / "e2e-network-evidence.json").write_text(
        json.dumps(network_evidence, ensure_ascii=False, indent=2) + "\n"
    )
    rows = {
        "standalone-appier-ad-request": request_row,
        "standalone-sdk-init": init_row,
        "standalone-creative-assets": asset_row,
        "standalone-native-render": _evaluated(
            "standalone-native-render",
            {
                "screenshot_and_response_saved": True,
                "returned_text_and_assets_have_visible_counterparts": True,
                "ad_label_and_required_views_are_visible": True,
                "human_visual_review": "Confirm no broken image, clipping, or layout defect in the screenshot",
            },
            {
                "screenshot_saved": screenshot_exists,
                "bid_response_saved": response is not None,
                "visual_review": visual_review,
            },
            bool(screenshot_exists and response is not None and visual_review.get("passed")),
            "ad-before-interactions.png" if (folder / "ad-before-interactions.png").is_file() else "screenshot.png",
            "The response and screenshot were saved, and the rendered View tree matches every text or asset actually returned by the response. Pixel quality, clipping, and layout remain visible for human review." if visual_review.get("passed") else "FAILED: the saved screenshot or rendered View tree does not satisfy the objective response-to-UI contract; see visual-review.json for the exact failed check.",
        ),
        "standalone-impression": impression_row,
        "standalone-click": click_row,
        "standalone-landing": landing_row,
        "standalone-privacy": privacy_row,
        "standalone-install-attribution": _blocked(
            "standalone-install-attribution",
            f"The traffic lookup key was captured automatically. MMP {profile.mmp_click_action} verification still requires the MMP action query.",
            actual=attribution_lookup,
            evidence="attribution-query.json",
        ),
        "standalone-attribution-reconciliation": _blocked(
            "standalone-attribution-reconciliation",
            f"The traffic lookup key was captured automatically. {profile.attribution_action} attribution recognition still requires Spark/MMP reconciliation.",
            actual=attribution_lookup,
            evidence="attribution-query.json",
        ),
    }
    return [
        rows[key] for key, testcase in TESTCASES.items()
        if campaign_supports(profile.key, key)
    ]
