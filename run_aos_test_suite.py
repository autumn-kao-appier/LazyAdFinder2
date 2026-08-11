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
INTEGRATION_MODE_ALIASES = {
    "standalone": "standalone",
    "mediation": "admob-mediation",
}


def _value(flag, explicit, environment):
    value = str(explicit or environment.get(flag, "")).strip()
    if not value:
        raise SystemExit(f"Missing required configuration: {flag}")
    return value


def _round_arguments(round_name, config, *, test_mode=None):
    arguments = [
        "round", round_name,
        "--app-package", config["app_package"],
        "--app-activity", config["app_activity"],
        "--test-mode", test_mode or config["test_mode"],
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


def suite_rounds(test_mode, *, signal_only=False):
    rounds = ["R1", "R2", "R3", "R4", "R5"]
    if not signal_only:
        rounds.append("E2E-STANDALONE" if test_mode == "standalone" else "E2E-ADMOB")
    return rounds


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signal-only", action="store_true", help="run R1-R5 without E2E")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--yes", action="store_true", help="run the printed scope without an interactive confirmation")
    parser.add_argument(
        "--privacy-verification",
        choices=("standalone", "manual"),
        default=os.environ.get("PRIVACY_VERIFICATION", "standalone"),
        help="Mediation AIBID privacy policy: run Standalone R5-1 last (default), or leave BLOCKED for manual review",
    )
    parser.add_argument(
        "--integration-mode", dest="integration_mode",
        choices=tuple(INTEGRATION_MODE_ALIASES), default=os.environ.get("TEST_MODE", ""),
        help="integration entry: standalone or mediation (currently Google AdMob)",
    )
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
        "test_mode": INTEGRATION_MODE_ALIASES[_value("TEST_MODE", args.integration_mode, environment).lower()],
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
    if config["test_mode"] == "admob-mediation":
        safety_udid = qa_aos.detect_udid(config["udid"])
        if not qa_aos.confirm_mediation_test_device(
            config["test_mode"], environment=environment, udid=safety_udid,
        ):
            return 2

    started = datetime.now().astimezone()
    run_id = f"aos-{started.strftime('%Y%m%dT%H%M%S%z')}"
    rounds = suite_rounds(config["test_mode"], signal_only=args.signal_only)

    plans = []
    for round_name in rounds:
        parsed = qa_aos.build_parser().parse_args(_round_arguments(round_name, config))
        plan = qa_aos.resolve_execution_plan(parsed)
        capture_config = qa_aos.config_from_args(parsed, plan)
        plan = qa_aos.preflight_execution_plan(plan, capture_config)
        plans.append((plan, capture_config, config["test_mode"]))
    append_privacy_round = bool(
        config["test_mode"] == "admob-mediation"
        and config["test_type"] == "aibid"
        and args.privacy_verification == "standalone"
    )
    if append_privacy_round:
        privacy_arguments = _round_arguments("R5-1", config, test_mode="standalone")
        parsed = qa_aos.build_parser().parse_args(privacy_arguments)
        plan = qa_aos.resolve_execution_plan(parsed)
        capture_config = qa_aos.config_from_args(parsed, plan)
        plan = qa_aos.preflight_execution_plan(plan, capture_config)
        plans.append((plan, capture_config, "standalone"))

    print(f"\n[suite scope] {run_id}")
    print(
        "[privacy verification] "
        + (
            "AUTO — Standalone R5-1 runs last; no Mediation request follows GAID renewal"
            if append_privacy_round else
            "MANUAL — automation does not delete GAID; Mediation privacy cards remain BLOCKED"
            if config["test_mode"] == "admob-mediation" and config["test_type"] == "aibid" else
            "campaign default"
        )
    )
    for plan, capture_config, _execution_mode in plans:
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
    for plan, _capture_config, execution_mode in plans:
        print(f"\n[suite {run_id}] {plan.round_name} ({execution_mode})", flush=True)
        child_environment = environment.copy()
        if append_privacy_round and plan.round_name == "R5-1":
            child_environment["PRIVACY_COVERAGE_ONLY"] = "1"
        result = subprocess.run(
            [
                sys.executable, str(ROOT / "qa_aos.py"),
                *_round_arguments(plan.round_name, config, test_mode=execution_mode),
            ],
            cwd=ROOT,
            env=child_environment,
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
