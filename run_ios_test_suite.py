#!/usr/bin/env python3
"""Single iOS suite entrypoint; independent from the Android runner."""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import qa_ios
from campaign_profiles import campaign_profile
from campaign_testcases import supports
from testcases.ios_signal_testcases import ROUND_DEFINITIONS
from testcases.e2e.ios_e2e_baseline import TESTCASES as BASELINE_E2E_TESTCASES
from testcases.e2e.ios_admob_mediation_extensions import TESTCASES as ADMOB_E2E_EXTENSIONS
from testcases.ipv6_refresh_testcases import TESTCASES as IPV6_TESTCASES


ROOT = Path(__file__).resolve().parent
MODE_MAP = {"standalone": "standalone", "mediation": "admob-mediation"}
ADMOB_TEST_DEVICE_PAGE = "https://admob.google.com/v2/settings/test-devices/list"
ADMOB_TEST_DEVICE_GUIDE = "https://developers.google.com/admob/ios/test-ads"


@dataclass(frozen=True)
class PlannedRound:
    name: str
    decision: str
    testcase_keys: tuple
    reason: str = ""


def execution_plan(test_type, integration_mode, signal_only=False, selected_rounds=(), ipv6_ready=False):
    campaign_profile(test_type)
    if integration_mode not in MODE_MAP:
        raise ValueError(f"Unsupported iOS integration mode: {integration_mode!r}")
    rounds = []
    for name in ("R1", "R2", "R3"):
        keys = tuple(key for key in ROUND_DEFINITIONS[name].testcase_keys if supports(test_type, key))
        rounds.append(PlannedRound(name, "RUN", keys))
    ipv6_keys = tuple(IPV6_TESTCASES)
    rounds.append(PlannedRound(
        "R4", "RUN" if ipv6_ready else "NOT_EXECUTABLE", ipv6_keys,
        "iPhone IPv6 network was confirmed before execution" if ipv6_ready else
        "Current iPhone IPv6 capability was not confirmed; pass --ipv6-ready only after verifying the network",
    ))
    r5_keys = tuple(key for key in ROUND_DEFINITIONS["R5"].testcase_keys if supports(test_type, key))
    rounds.append(PlannedRound("R5", "RUN", r5_keys))
    if not signal_only:
        e2e_keys = tuple(key for key in BASELINE_E2E_TESTCASES if supports(test_type, key))
        if integration_mode == "mediation":
            e2e_keys += tuple(key for key in ADMOB_E2E_EXTENSIONS if supports(test_type, key))
        rounds.append(PlannedRound(
            "E2E-ADMOB" if integration_mode == "mediation" else "E2E-STANDALONE",
            "RUN", e2e_keys,
        ))
    selected = {str(name).strip().upper() for name in selected_rounds if str(name).strip()}
    known = {item.name for item in rounds}
    unknown = sorted(selected - known)
    if unknown:
        raise ValueError("Unknown iOS Round(s): " + ", ".join(unknown))
    if selected:
        rounds = [
            item if item.name in selected else PlannedRound(
                item.name, "SKIP", item.testcase_keys,
                "Not selected in this suite's Test Scope",
            )
            for item in rounds
        ]
    return tuple(rounds)


def print_plan(plan, args, run_id):
    print("\niOS ExecutionPlan")
    print(f"  Run ID:      {run_id}")
    print(f"  Campaign:    {args.test_type}")
    print(f"  Integration: {args.integration_mode}")
    print(f"  CID:         {args.test_cid}")
    for item in plan:
        keys = ", ".join(item.testcase_keys) or "—"
        suffix = f" · {item.reason}" if item.reason else ""
        print(f"  [{item.decision:15}] {item.name}: {keys}{suffix}")


def confirm_mediation_test_device(integration_mode, input_fn=input, open_page=None):
    if integration_mode != "mediation":
        return True
    if open_page is None:
        open_page = lambda url: subprocess.run(["open", "-na", "Google Chrome", "--args", "--incognito", url], check=False)
    open_page(ADMOB_TEST_DEVICE_PAGE)
    print("\n⚠️  iOS MEDIATION TEST DEVICE WARNING")
    print("請先在 Google AdMob 將這台 iPhone 登記為 Test Device；未登記前不得開始廣告 Automation。")
    print(f"Test Device 說明：{ADMOB_TEST_DEVICE_GUIDE}")
    try:
        answer = input_fn("已確認這台 iPhone 出現在 AdMob Test devices 清單？[y/N] ").strip().lower()
    except EOFError:
        answer = ""
    return answer in {"y", "yes"}


def suite_preflight(args, plan):
    """Gate the complete selected suite before the first Round creates Evidence."""
    runnable = [item for item in plan if item.decision == "RUN"]
    if not runnable:
        return None
    arguments = [
        "round", runnable[0].name,
        "--bundle-id", args.bundle_id,
        "--test-mode", MODE_MAP[args.integration_mode],
        "--test-type", args.test_type,
        "--test-cid", args.test_cid,
        "--evidence-dir", args.evidence_dir,
    ]
    if args.udid:
        arguments += ["--udid", args.udid]
    if args.target_app_bundle_id:
        arguments += ["--target-app-bundle-id", args.target_app_bundle_id]
    parsed = qa_ios.build_parser().parse_args(arguments)
    config = qa_ios.config_from_args(parsed)
    required_bundles = []
    if any(item.name == "R1" for item in runnable):
        required_bundles.append(os.environ.get("IOS_IDFA_APP_BUNDLE_ID", "com.pag3dev.GetMyIDFA"))
    if args.target_app_bundle_id and any(item.name.startswith("E2E-") for item in runnable):
        required_bundles.append(args.target_app_bundle_id)
    qa_ios.ensure_ios_automation_ready(
        config,
        tuple(bundle_id for bundle_id in required_bundles if str(bundle_id).strip()),
    )
    capabilities = {"smoke": qa_ios.smoke_ios_suite_capabilities(config)}
    if any(item.name == "R4" for item in runnable):
        capabilities["r4"] = qa_ios.probe_ios_r4_capability()
    if any(item.name == "R5" for item in runnable):
        capabilities["r5"] = qa_ios.probe_ios_r5_capabilities(config, args.test_type)
        unavailable = capabilities["r5"]["unavailable"]
        if unavailable:
            print("[suite preflight] R5 unavailable TC: " + ", ".join(sorted(unavailable)))
    print("[suite preflight] READY: complete selected iOS scope")
    return config, capabilities


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", default=os.environ.get("BUNDLE_ID", ""))
    parser.add_argument("--integration-mode", choices=tuple(MODE_MAP), required=True)
    parser.add_argument("--test-type", choices=("aibid", "reen-static", "reen-dynamic"), required=True)
    parser.add_argument("--test-cid", required=True)
    parser.add_argument("--target-app-bundle-id", default=os.environ.get("TARGET_APP_BUNDLE_ID", ""))
    parser.add_argument("--udid", default=os.environ.get("UDID", ""))
    parser.add_argument("--signal-only", action="store_true")
    parser.add_argument(
        "--round", action="append", default=[],
        help="run only this Round; repeat for multiple Rounds (default: complete suite)",
    )
    parser.add_argument(
        "--ipv6-ready", action="store_true",
        help="confirm before execution that the iPhone network supports the R4 IPv6 scenarios",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the complete plan without touching a device")
    parser.add_argument("--yes", action="store_true", help="confirm the displayed allowlisted suite plan")
    parser.add_argument("--evidence-dir", default=str(ROOT / "evidence"))
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.bundle_id:
        raise SystemExit("BUNDLE_ID/--bundle-id is required")
    if args.test_type.startswith("reen-") and not args.target_app_bundle_id:
        raise SystemExit("REEN requires TARGET_APP_BUNDLE_ID/--target-app-bundle-id")
    run_id = f"ios-{datetime.now().strftime('%Y%m%dT%H%M%S%z')}"
    started_at = datetime.now().astimezone().isoformat()
    plan = execution_plan(
        args.test_type, args.integration_mode, args.signal_only,
        selected_rounds=args.round, ipv6_ready=args.ipv6_ready,
    )
    print_plan(plan, args, run_id)
    if args.dry_run:
        return 0
    if not confirm_mediation_test_device(args.integration_mode):
        raise SystemExit("iOS Mediation cancelled before any ad request because Test Device registration was not confirmed")
    if not args.yes:
        try:
            answer = input("\n確認執行以上完整 Test Scope？[y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            print("[suite] cancelled before execution")
            return 2

    environment = os.environ.copy()
    environment.update({
        "BUNDLE_ID": args.bundle_id,
        "TEST_MODE": MODE_MAP[args.integration_mode],
        "TEST_TYPE": args.test_type,
        "TEST_CID": args.test_cid,
        "TARGET_APP_BUNDLE_ID": args.target_app_bundle_id,
        "UDID": args.udid,
        "EVIDENCE_DIR": args.evidence_dir,
        "TEST_RUN_ID": run_id,
        "TEST_RUN_STARTED_AT": started_at,
        "AUTO_PUBLISH": "0",
    })
    try:
        _preflight_config, capabilities = suite_preflight(args, plan)
        environment["IOS_SUITE_PREFLIGHT_JSON"] = json.dumps(capabilities, ensure_ascii=False)
        environment["SUITE_CAPABILITY_PREFLIGHT_READY"] = "1"
    except (qa_ios.CaptureError, OSError, subprocess.SubprocessError) as exc:
        print(f"[suite preflight] FAILED: {exc}", file=sys.stderr)
        return 2
    failures = []
    for item in plan:
        if item.decision != "RUN":
            record_skip(args, item, run_id, started_at)
    for item in plan:
        if item.decision != "RUN":
            continue
        command = [sys.executable, str(ROOT / "qa_ios.py"), "round", item.name]
        print(f"\n[suite {run_id}] {item.name}")
        result = subprocess.run(command, env=environment)
        if result.returncode:
            failures.append(item.name)
            break
    publish = subprocess.run([
        sys.executable, str(ROOT / "page.py"), "--evidence", args.evidence_dir, "--publish",
    ])
    if publish.returncode:
        failures.append("publish")
    if failures:
        print(json.dumps({"run_id": run_id, "failed": failures}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


def record_skip(args, item, run_id, started_at):
    """Write the pre-confirmed non-run decision so Report cards stay explicit."""
    mode = MODE_MAP[args.integration_mode].upper()
    kind = args.test_type.upper()
    cid = args.test_cid.replace("/", "-")
    round_dir = Path(args.evidence_dir) / f"IOS_{mode}_{kind}_CID_{cid}_{item.name}_{run_id}"
    folder = round_dir / f"{item.name}-{item.decision}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    folder.mkdir(parents=True, exist_ok=False)
    now = datetime.now().astimezone().isoformat()
    reason = item.reason or "Round was not selected in the confirmed Test Scope"
    (folder / "round-skip.json").write_text(json.dumps({
        "status": "SKIPPED", "decision": item.decision, "round": item.name,
        "reason": reason, "recorded_at": now, "testcases": list(item.testcase_keys),
        "policy": "No device mutation, capture, or verdict was produced.",
    }, ensure_ascii=False, indent=2) + "\n")
    (folder / "summary.json").write_text(json.dumps({
        "result": "SKIPPED", "platform": "ios", "test_mode": MODE_MAP[args.integration_mode],
        "test_type": args.test_type, "test_cid": args.test_cid,
        "target_app_package": args.target_app_bundle_id, "test_round": item.name,
        "test_run_id": run_id, "test_run_started_at": started_at,
        "capture_name": item.decision, "started_at": now, "finished_at": now,
        "skipped_testcases": list(item.testcase_keys), "skip_reason": reason,
        "execution_state": item.decision, "device": {},
    }, ensure_ascii=False, indent=2) + "\n")
    print(f"[suite {run_id}] {item.name} {item.decision}: {reason}")


if __name__ == "__main__":
    raise SystemExit(main())
