#!/usr/bin/env python3
"""Build one human-readable evidence bundle from a capture."""

import json
import shutil
from datetime import datetime
from pathlib import Path

from apr_xorenc import decrypt


PROXY_ARTIFACTS = {
    Path("/tmp/appier_bid_response.json"): "bid_response.json",
    Path("/tmp/appier_impression.json"): "impression.json",
    Path("/tmp/appier_proxy_events.jsonl"): "proxy-events.jsonl",
    Path("/tmp/appier_net_probe_response.json"): "ipv6-net-probe-response.json",
    Path("/tmp/admob_pubsetting_request.bin"): "admob-pubsetting-request.bin",
    Path("/tmp/admob_pubsetting_response.bin"): "admob-pubsetting-response.bin",
    Path("/tmp/admob_gma_request.bin"): "admob-gma-request.bin",
    Path("/tmp/admob_gma_response.bin"): "admob-gma-response.bin",
}


def _json_or_text(plaintext):
    try:
        return json.loads(plaintext)
    except json.JSONDecodeError:
        return plaintext


def decoded_bid(request):
    """Decode req_enc and ext_enc independently without changing the raw bid."""
    decoded = {}
    for name in ("req", "ext"):
        field = f"{name}_enc"
        encrypted = request.get(field)
        if not isinstance(encrypted, str):
            decoded[name] = {"encrypted_field": field, "error": "field is missing"}
            continue
        try:
            decoded[name] = {
                "encrypted_field": field,
                "plaintext": _json_or_text(decrypt(encrypted)),
            }
        except (TypeError, ValueError) as exc:
            decoded[name] = {"encrypted_field": field, "error": str(exc)}
    return decoded


def finalize_bundle(
    folder,
    *,
    driver,
    platform,
    config,
    device,
    started_at,
    request=None,
    status=None,
    identity=None,
    source=None,
    result="CAPTURED",
    failed_step=None,
    error=None,
    capture_log=None,
):
    """Finish a capture folder. Raw and derived evidence remain separate."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    capture_log = Path(capture_log) if capture_log else None
    if capture_log and capture_log.exists():
        shutil.copy2(capture_log, folder / "traffic.log")
    else:
        (folder / "traffic.log").write_text("")

    if request is not None:
        (folder / "bid_raw.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n"
        )
        (folder / "bid_decoded.json").write_text(
            json.dumps(decoded_bid(request), ensure_ascii=False, indent=2) + "\n"
        )

    for source_path, filename in PROXY_ARTIFACTS.items():
        if source_path.is_file():
            shutil.copy2(source_path, folder / filename)

    screenshot_error = None
    if driver is not None:
        try:
            driver.get_screenshot_as_file(str(folder / "screenshot.png"))
        except Exception as exc:
            screenshot_error = str(exc)

    finished_at = datetime.now().astimezone().isoformat()
    summary = {
        "result": result,
        "platform": platform,
        "test_mode": config.test_mode,
        "test_type": config.test_type,
        "test_cid": config.test_cid,
        "test_round": config.test_round,
        "capture_name": folder.name.rsplit("_", 2)[0],
        "started_at": started_at,
        "finished_at": finished_at,
        "source": source,
        "http_status": status,
        "cid": identity.get("cid") if identity else None,
        "creative_id": identity.get("crid") if identity else None,
        "failed_step": failed_step,
        "error": error,
        "screenshot_error": screenshot_error,
        "device": device,
    }
    (folder / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    return folder
