#!/usr/bin/env python3
"""Run the shared REEN suite through an explicit Static or Dynamic entry."""

import argparse

import run_aos_test_suite


DEFAULT_APP_PACKAGE = "com.appier.android.sample"
DEFAULT_APP_ACTIVITY = ".MainActivity"


def build_runner_arguments(args):
    command = [
        "--test-type", f"reen-{args.creative}",
        "--test-mode", args.mode,
        "--test-cid", args.cid,
        "--target-app-package", args.target_app_package,
        "--app-package", args.app_package,
        "--app-activity", args.app_activity,
    ]
    if args.udid:
        command += ["--udid", args.udid]
    if args.trigger_text:
        command += ["--trigger-text", args.trigger_text]
    if args.tab_text:
        command += ["--tab-text", args.tab_text]
    if args.include_e2e:
        command.append("--include-e2e")
    if args.publish:
        command.append("--publish")
    if args.yes:
        command.append("--yes")
    return command


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("creative", choices=("static", "dynamic"), help="REEN creative type")
    parser.add_argument("--mode", choices=("standalone", "admob-mediation", "applovin-mediation"), required=True)
    parser.add_argument("--cid", required=True)
    parser.add_argument("--target-app-package", required=True)
    parser.add_argument("--app-package", default=DEFAULT_APP_PACKAGE)
    parser.add_argument("--app-activity", default=DEFAULT_APP_ACTIVITY)
    parser.add_argument("--udid", default="")
    parser.add_argument("--trigger-text", default="")
    parser.add_argument("--tab-text", default="")
    parser.add_argument("--include-e2e", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return run_aos_test_suite.main(build_runner_arguments(args))


if __name__ == "__main__":
    raise SystemExit(main())
