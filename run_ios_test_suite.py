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

from campaign_profiles import campaign_profile
from campaign_testcases import supports
from testcases.ios_signal_testcases import ROUND_DEFINITIONS
from testcases.e2e.ios_e2e_baseline import TESTCASES as BASELINE_E2E_TESTCASES
from testcases.e2e.ios_admob_mediation_extensions import TESTCASES as ADMOB_E2E_EXTENSIONS


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


def execution_plan(test_type, integration_mode, signal_only=False):
    campaign_profile(test_type)
    if integration_mode not in MODE_MAP:
        raise ValueError(f"Unsupported iOS integration mode: {integration_mode!r}")
    rounds = []
    for name in ("R1", "R2", "R3"):
        keys = tuple(key for key in ROUND_DEFINITIONS[name].testcase_keys if supports(test_type, key))
        rounds.append(PlannedRound(name, "RUN", keys))
    rounds.append(PlannedRound(
        "R4", "RUN",
        ("ipv6-address", "ipv6-refresh-launch", "ipv6-refresh-wifi-switch", "ipv6-refresh-recovery", "ipv6-refresh-debounce", "ipv6-refresh-slow-network"),
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


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", default=os.environ.get("BUNDLE_ID", ""))
    parser.add_argument("--integration-mode", choices=tuple(MODE_MAP), required=True)
    parser.add_argument("--test-type", choices=("aibid", "reen-static", "reen-dynamic"), required=True)
    parser.add_argument("--test-cid", required=True)
    parser.add_argument("--target-app-bundle-id", default=os.environ.get("TARGET_APP_BUNDLE_ID", ""))
    parser.add_argument("--udid", default=os.environ.get("UDID", ""))
    parser.add_argument("--signal-only", action="store_true")
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
    plan = execution_plan(args.test_type, args.integration_mode, args.signal_only)
    print_plan(plan, args, run_id)
    if args.dry_run:
        return 0
    if not args.yes:
        raise SystemExit("Review the ExecutionPlan, then rerun with --yes to authorize this suite")
    if not confirm_mediation_test_device(args.integration_mode):
        raise SystemExit("iOS Mediation cancelled before any ad request because Test Device registration was not confirmed")

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
    failures = []
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


if __name__ == "__main__":
    raise SystemExit(main())
