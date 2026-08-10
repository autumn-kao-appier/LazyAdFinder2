#!/usr/bin/env python3
"""Run one AOS test suite and group all Rounds under one report run ID."""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-e2e", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)

    started = datetime.now().astimezone()
    run_id = f"aos-{started.strftime('%Y%m%dT%H%M%S%z')}"
    rounds = ["R1", "R2", "R3", "R4", "R5"]
    if args.include_e2e:
        mode = os.environ.get("TEST_MODE", "").strip().lower()
        rounds.append("E2E-STANDALONE" if mode == "standalone" else "E2E-ADMOB")

    environment = os.environ.copy()
    environment.update({
        "TEST_RUN_ID": run_id,
        "TEST_RUN_STARTED_AT": started.isoformat(),
        "AUTO_PUBLISH": "0",
    })
    failures = []
    for round_name in rounds:
        print(f"\n[suite {run_id}] {round_name}", flush=True)
        result = subprocess.run(
            [sys.executable, str(ROOT / "qa_aos.py"), "round", round_name],
            cwd=ROOT,
            env=environment,
        )
        if result.returncode:
            failures.append(round_name)

    if args.publish:
        subprocess.run(
            [sys.executable, str(ROOT / "page.py"), "--publish"],
            cwd=ROOT,
            check=False,
        )
    if failures:
        print(f"[suite] completed with failed Rounds: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"[suite] completed: {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
