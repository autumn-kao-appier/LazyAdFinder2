"""Android Appier Standalone E2E definitions."""

import json
from pathlib import Path

from verdict import blocked, evaluate

from .e2e_shared_contracts import E2ETestCase, definitions

TESTCASES = definitions(
    E2ETestCase("standalone-sdk-init", "SDK Initialization", "Serving", "P0"),
    E2ETestCase("standalone-appier-ad-request", "Appier Direct Ad Request", "Serving", "P0"),
    E2ETestCase("standalone-creative-assets", "Creative Asset Loading", "Serving", "P1"),
    E2ETestCase("standalone-native-render", "Native Ad Rendering", "Serving", "P0"),
    E2ETestCase("standalone-impression", "Appier Impression Tracking", "Tracking", "P0"),
    E2ETestCase("standalone-click", "Appier Click Tracking", "Tracking", "P0"),
    E2ETestCase("standalone-landing", "Landing Behavior", "Tracking", "P1"),
    E2ETestCase("standalone-privacy", "Privacy Information", "Tracking", "P2"),
    E2ETestCase("standalone-install-attribution", "AIBID Install Attribution", "Attribution", "P2", ("aibid",)),
    E2ETestCase("standalone-attribution-reconciliation", "Backend Attribution Reconciliation", "Attribution", "P2", ("aibid",)),
)


def _blocked(key, reason):
    testcase = TESTCASES[key]
    row = blocked(key, reason).to_dict()
    row.update({"layer": "E2E", "title": testcase.title, "description": reason})
    return row


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


def validate_bundle(folder):
    """Validate network facts and preserve manual gates for visual/external facts."""
    folder = Path(folder)
    summary = json.loads((folder / "summary.json").read_text())
    events = _read_events(folder)
    decoded = _read_json(folder / "bid_decoded.json", {}) or {}
    response = _read_json(folder / "bid_response.json")
    raw_exists = (folder / "bid_raw.json").is_file()
    status = str(summary.get("http_status") or "")
    cid = summary.get("cid")
    req_plain = decoded.get("req", {}).get("plaintext", {})
    app = req_plain.get("app", {}) if isinstance(req_plain, dict) else {}
    request_actual = {
        "http_status": status,
        "cid": cid,
        "zone_id": (_read_json(folder / "bid_raw.json", {}) or {}).get("zone_id"),
        "bundle": app.get("bundle"),
        "sdk_version": app.get("sdk_version"),
        "raw_request_saved": raw_exists,
        "response_saved": response is not None,
    }
    request_minimum = status == "200" and bool(cid) and raw_exists and all(
        request_actual.get(field) for field in ("zone_id", "bundle", "sdk_version")
    )
    if response is None:
        request_row = _blocked(
            "standalone-appier-ad-request",
            "Capture limitation: request and onAdLoaded were observed, but the HTTP ad response body was not preserved; response completeness cannot be compared",
        )
    else:
        request_row = _evaluated(
            "standalone-appier-ad-request",
            {"http_status": "200", "request_fields_complete": True, "response_non_empty": True},
            request_actual,
            request_minimum and bool(response),
            "e2e-network-evidence.json",
            "Direct Appier request, decoded identifiers, and response body were captured.",
        )

    init_responses = _response_events(events, "sdk-init")
    if init_responses:
        init_actual = init_responses[-1]
        init_row = _evaluated(
            "standalone-sdk-init",
            {"http_status": 200, "init_request_observed": True},
            init_actual,
            init_actual.get("status") == 200,
            "e2e-network-evidence.json",
            "The SDK init endpoint was observed in the proxy session.",
        )
    else:
        init_row = _blocked(
            "standalone-sdk-init",
            "Capture limitation: no Android SDK init endpoint was observed; confirm whether Android has a separate init request before defining PASS",
        )

    asset_responses = _response_events(events, "asset")
    asset_transport_ok = bool(asset_responses) and all(
        row.get("status") in (200, 304)
        and str(row.get("content_type", "")).lower().startswith("image/")
        and (
            row.get("method") == "HEAD"
            or row.get("status") == 304
            or int(row.get("content_length") or 0) > 0
        )
        for row in asset_responses
    )
    if not asset_responses:
        asset_row = _blocked(
            "standalone-creative-assets",
            "Capture limitation: no image response metadata was preserved; asset HTTP and MIME checks could not run",
        )
    elif not asset_transport_ok:
        asset_row = _evaluated(
            "standalone-creative-assets",
            {"all_assets_http_200_or_cached_304": True, "image_mime": True, "GET_200_non_empty": True},
            asset_responses,
            False,
            "e2e-network-evidence.json",
            "At least one captured creative asset failed its transport check.",
        )
    else:
        asset_row = _blocked(
            "standalone-creative-assets",
            "Manual visual review remains: all captured image responses passed transport checks, but the screenshot must confirm that the rendered ad has no broken or mismatched asset",
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
        impression_row = _blocked(
            "standalone-impression",
            "Capture limitation: the proxy session does not yet contain both show_cb and winshowimg responses",
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
        "standalone-native-render": _blocked("standalone-native-render", "Manual visual review required: screenshot.png must be compared with bid_response.json for text, CTA, images, privacy icon, ad label, clipping, and layout"),
        "standalone-impression": impression_row,
        "standalone-click": _blocked("standalone-click", "Not executed: a cost-bearing real ad click requires explicit manual confirmation"),
        "standalone-landing": _blocked("standalone-landing", "Not executed: landing validation requires the confirmed click step"),
        "standalone-privacy": _blocked("standalone-privacy", "Not executed: privacy icon interaction is not automated in this capture"),
        "standalone-install-attribution": _blocked("standalone-install-attribution", "Not executed: install attribution requires a coordinated attribution window"),
        "standalone-attribution-reconciliation": _blocked("standalone-attribution-reconciliation", "Not executed: backend reconciliation requires completed install attribution and internal-system access"),
    }
    return [rows[key] for key in TESTCASES]
