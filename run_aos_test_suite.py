#!/usr/bin/env python3
"""Plan, confirm, and run one complete AOS campaign suite."""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import qa_aos
from campaign_profiles import CAMPAIGN_PROFILES


ROOT = Path(__file__).parent


def _value(flag, explicit, environment):
    value = str(explicit or environment.get(flag, "")).strip()
    if not value:
        raise SystemExit(f"Missing required configuration: {flag}")
    return value


def _round_arguments(round_name, config):
    arguments = [
        "round", round_name,
        "--app-package", config["app_package"],
        "--app-activity", config["app_activity"],
        "--test-mode", config["test_mode"],
        "--test-type", config["test_type"],
        "--test-cid", config["test_cid"],
        "--trigger-text", config["trigger_text"],
    ]
    if config["tab_text"]:
        arguments += ["--tab-text", config["tab_text"]]
    if config["udid"]:
        arguments += ["--udid", config["udid"]]
    if config["target_app_package"]:
        arguments += ["--target-app-package", config["target_app_package"]]
    return arguments


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-e2e", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--yes", action="store_true", help="run the printed scope without an interactive confirmation")
    parser.add_argument("--test-mode", choices=tuple(qa_aos.MODE_TABS), default=os.environ.get("TEST_MODE", ""))
    parser.add_argument("--test-type", choices=tuple(CAMPAIGN_PROFILES), default=os.environ.get("TEST_TYPE", ""))
    parser.add_argument("--test-cid", default=os.environ.get("TEST_CID", ""))
    parser.add_argument("--target-app-package", default=os.environ.get("TARGET_APP_PACKAGE", ""))
    parser.add_argument("--app-package", default=os.environ.get("APP_PACKAGE", ""))
    parser.add_argument("--app-activity", default=os.environ.get("APP_ACTIVITY", ""))
    parser.add_argument("--udid", default=os.environ.get("UDID", ""))
    parser.add_argument("--trigger-text", default=os.environ.get("TRIGGER_TEXT", qa_aos.DEFAULT_TRIGGER_TEXT))
    parser.add_argument("--tab-text", default=os.environ.get("TAB_TEXT", ""))
    args = parser.parse_args(argv)

    environment = os.environ.copy()
    config = {
        "app_package": _value("APP_PACKAGE", args.app_package, environment),
        "app_activity": _value("APP_ACTIVITY", args.app_activity, environment),
        "test_mode": _value("TEST_MODE", args.test_mode, environment).lower(),
        "test_type": _value("TEST_TYPE", args.test_type, environment).lower(),
        "test_cid": _value("TEST_CID", args.test_cid, environment),
        "target_app_package": args.target_app_package.strip(),
        "trigger_text": args.trigger_text.strip(),
        "tab_text": args.tab_text.strip(),
        "udid": args.udid.strip(),
    }
    if config["test_mode"] not in qa_aos.MODE_TABS:
        raise SystemExit(f"Unsupported TEST_MODE={config['test_mode']!r}")
    if config["test_type"] not in CAMPAIGN_PROFILES:
        raise SystemExit(f"Unsupported TEST_TYPE={config['test_type']!r}")
    if args.include_e2e and config["test_mode"] not in {"standalone", "admob-mediation"}:
        raise SystemExit("E2E is currently implemented only for standalone and admob-mediation")

    if not qa_aos.confirm_mediation_test_device(config["test_mode"], environment=environment):
        return 2

    started = datetime.now().astimezone()
    run_id = f"aos-{started.strftime('%Y%m%dT%H%M%S%z')}"
    rounds = ["R1", "R2", "R3", "R4", "R5"]
    if args.include_e2e:
        rounds.append("E2E-STANDALONE" if config["test_mode"] == "standalone" else "E2E-ADMOB")

    plans = []
    for round_name in rounds:
        parsed = qa_aos.build_parser().parse_args(_round_arguments(round_name, config))
        plan = qa_aos.resolve_execution_plan(parsed)
        capture_config = qa_aos.config_from_args(parsed, plan)
        plan = qa_aos.preflight_execution_plan(plan, capture_config)
        plans.append((plan, capture_config))

    print(f"\n[suite scope] {run_id}")
    for plan, capture_config in plans:
        qa_aos.print_execution_plan(plan, capture_config)
    if not args.yes:
        answer = input("\n確認執行以上完整 Test Scope？[y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("[suite] cancelled before execution")
            return 2

    environment.update({
        "TEST_RUN_ID": run_id,
        "TEST_RUN_STARTED_AT": started.isoformat(),
        "AUTO_PUBLISH": "0",
    })
    failures = []
    for plan, _capture_config in plans:
        print(f"\n[suite {run_id}] {plan.round_name}", flush=True)
        result = subprocess.run(
            [sys.executable, str(ROOT / "qa_aos.py"), *_round_arguments(plan.round_name, config)],
            cwd=ROOT,
            env=environment,
        )
        if result.returncode:
            failures.append(plan.round_name)

    if args.publish:
        try:
            subprocess.run(
                [sys.executable, str(ROOT / "page.py"), "--publish"],
                cwd=ROOT,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            failures.append(f"publish(exit={exc.returncode})")
    if failures:
        print(f"[suite] completed with failures: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"[suite] completed: {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
