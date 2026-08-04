#!/usr/bin/env python3
"""Android automation and raw-evidence capture for LazyAdFinder2.

This module deliberately contains no testcase expectations and produces no
PASS/FAIL verdicts.  Its responsibilities are limited to:

* configuring and driving an Android sample app through Appium;
* executing explicitly declared round setup steps;
* waiting for an Appier bid request/response; and
* saving the unmodified evidence needed by future testcase validators.

Examples:
    python3 qa_aos.py capture
    python3 qa_aos.py capture --accept-request --max-attempts 1
    python3 qa_aos.py list-rounds
    python3 qa_aos.py round <round-name>

Values may be supplied as flags or environment variables.  Required values:
APP_PACKAGE, APP_ACTIVITY, TEST_MODE, TEST_TYPE and TEST_CID.  TEST_CID may be
omitted only with --accept-request.
"""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from appium import webdriver
from appium.options.android.uiautomator2.base import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from evidence_bundle import finalize_bundle


# Testcases and their correct expectations will be added manually.  Keep these
# catalogs empty until each definition has been reviewed.  The capture engine
# below does not inspect either catalog.
TC_DEFINITIONS = {}
ROUND_DEFINITIONS = {}


APPIUM_URL = "http://127.0.0.1:4723"
DEFAULT_TRIGGER_TEXT = "Native - basic format"
MODE_TABS = {
    "standalone": "Appier SDK",
    "admob-mediation": "AdMob Mediation",
    "applovin-mediation": "AppLovin Mediation",
}

FLAG_FILE = Path("/tmp/appier_hit")
BID_FILE = Path("/tmp/appier_bid.json")
BID_STATUS_FILE = Path("/tmp/appier_bid_status")
BID_RESPONSE_FILE = Path("/tmp/appier_bid_response.json")
IMPRESSION_FILE = Path("/tmp/appier_impression.json")
LOGCAT_FILE = Path("/tmp/appier_aos_logcat.txt")
DETECTOR_FILES = (
    FLAG_FILE,
    BID_FILE,
    BID_STATUS_FILE,
    BID_RESPONSE_FILE,
    IMPRESSION_FILE,
)


@dataclass(frozen=True)
class CaptureConfig:
    app_package: str
    app_activity: str
    test_mode: str
    test_type: str
    test_cid: str
    test_round: str
    trigger_text: str
    tab_text: str
    udid: str
    executor: str
    evidence_dir: Path
    bid_timeout: float
    retry_delay: float
    max_attempts: int
    phase_timeout: float
    accept_request: bool


@dataclass(frozen=True)
class RoundStep:
    """One future round setup followed by one raw capture."""

    name: str
    setup: Optional[Callable[[CaptureConfig], None]] = None


class CaptureError(RuntimeError):
    pass


def _env(name, fallback=""):
    value = os.environ.get(name)
    return fallback if value is None else value


def _safe_label(value, fallback, limit=40):
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value).strip()).strip("-_")
    return label[:limit] or fallback


def _read_text(path):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return ""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _adb_command(udid, *args):
    command = ["adb"]
    if udid:
        command += ["-s", udid]
    return command + list(args)


def adb(udid, *args, check=True):
    result = subprocess.run(
        _adb_command(udid, *args),
        text=True,
        capture_output=True,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown adb error"
        raise CaptureError(f"adb {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def detect_udid(requested=""):
    if requested:
        state = adb(requested, "get-state", check=False)
        if state != "device":
            raise CaptureError(f"Android device {requested!r} is not available")
        return requested

    result = subprocess.run(["adb", "devices"], text=True, capture_output=True)
    if result.returncode:
        raise CaptureError(result.stderr.strip() or "adb devices failed")
    devices = [
        line.split()[0]
        for line in result.stdout.splitlines()[1:]
        if len(line.split()) >= 2 and line.split()[1] == "device"
    ]
    if not devices:
        raise CaptureError("No authorized Android device found")
    if len(devices) > 1:
        raise CaptureError(f"Multiple Android devices found: {devices}; specify --udid")
    return devices[0]


class LogcatRecorder:
    def __init__(self, udid, output=LOGCAT_FILE):
        self.udid = udid
        self.output = Path(output)
        self.process = None
        self.stream = None

    def start(self):
        adb(self.udid, "logcat", "-c")
        self.stream = self.output.open("w")
        self.process = subprocess.Popen(
            _adb_command(self.udid, "logcat", "-v", "time"),
            stdout=self.stream,
            stderr=subprocess.DEVNULL,
        )
        return self

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()


def clear_detector_state():
    for path in DETECTOR_FILES:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def create_driver(config):
    options = UiAutomator2Options()
    options.app_package = config.app_package
    options.app_activity = config.app_activity
    options.no_reset = True
    options.udid = config.udid
    return webdriver.Remote(APPIUM_URL, options=options)


def find_visible_text(driver, text):
    width = driver.get_window_size()["width"]
    selector = f'new UiSelector().textMatches("(?i){re.escape(text)}")'
    for element in driver.find_elements(AppiumBy.ANDROID_UIAUTOMATOR, selector):
        center_x = element.location["x"] + element.size["width"] // 2
        if 0 <= center_x < width:
            return element
    return None


def select_tab(driver, tab_text, trigger_text):
    for attempt in range(4):
        tab = find_visible_text(driver, tab_text)
        if tab is not None:
            tab.click()
            time.sleep(0.8)
            if find_visible_text(driver, trigger_text) is not None:
                return
        if attempt < 3:
            driver.back()
            time.sleep(0.8)
    raise CaptureError(
        f"Cannot open tab {tab_text!r} or find placement {trigger_text!r}"
    )


def tap_placement(driver, config):
    element = find_visible_text(driver, config.trigger_text)
    if element is None:
        return False
    element.click()
    return True


AD_REQUEST_RE = re.compile(
    r"(?:\[AdRequestJSON\]|Ad request body:)\s*(\{.*\})\s*$"
)
LOADED_RE = re.compile(r"onAdLoaded\(\)")
NO_BID_RE = re.compile(r"onAdNoBid\(\)")
IMPRESSION_RE = re.compile(
    r"Requesting impression tracker:.*?[?&]cid=([^&\s]+).*?[&]crid=([^&\s]+)"
)


def scan_logcat():
    request = None
    status = None
    identity = None
    try:
        lines = LOGCAT_FILE.read_text(errors="replace").splitlines()
    except OSError:
        return request, status, identity

    for line in lines:
        match = AD_REQUEST_RE.search(line)
        if match:
            try:
                request = json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        if LOADED_RE.search(line):
            status = "200"
        elif NO_BID_RE.search(line):
            status = "204"
        match = IMPRESSION_RE.search(line)
        if match:
            identity = {"cid": match.group(1), "crid": match.group(2)}
    return request, status, identity


def observe_bid():
    request = _read_json(BID_FILE)
    status = _read_text(BID_STATUS_FILE) or None
    identity = _read_json(IMPRESSION_FILE)
    source = "proxy" if request is not None else None

    log_request, log_status, log_identity = scan_logcat()
    if request is None and log_request is not None:
        request = log_request
        source = "logcat"
    status = status or log_status
    identity = identity or log_identity
    return request, status, identity, source


def wait_for_bid(config):
    deadline = time.monotonic() + config.bid_timeout
    while time.monotonic() < deadline:
        request, status, identity, source = observe_bid()
        if eligible(config, request, status, identity):
            return request, status, identity, source
        time.sleep(0.2)
    return observe_bid()


def eligible(config, request, status, identity):
    """AOS always needs a request; an impression proves which CID actually rendered."""
    if request is None:
        return False
    if config.accept_request:
        return True
    return bool(identity and identity.get("cid") == config.test_cid)


def round_directory(config):
    config.evidence_dir.mkdir(parents=True, exist_ok=True)
    mode = _safe_label(config.test_mode, "mode").upper()
    kind = _safe_label(config.test_type, "type").upper()
    cid = _safe_label(config.test_cid, "ANY")
    label = _safe_label(config.test_round, "MANUAL")
    prefix = f"AOS_{mode}_{kind}_CID_{cid}_{label}"
    existing = sorted(path for path in config.evidence_dir.glob(f"{prefix}_*") if path.is_dir())
    if existing:
        return existing[-1]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return config.evidence_dir / f"{prefix}_{timestamp}"


def create_capture_folder(config, capture_name):
    round_dir = round_directory(config)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = round_dir / f"{_safe_label(capture_name, 'CAPTURE')}_{timestamp}"
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def device_evidence(config):
    return {
        "model": adb(config.udid, "shell", "getprop", "ro.product.model", check=False),
        "manufacturer": adb(config.udid, "shell", "getprop", "ro.product.manufacturer", check=False),
        "android_version": adb(config.udid, "shell", "getprop", "ro.build.version.release", check=False),
        "sdk": adb(config.udid, "shell", "getprop", "ro.build.version.sdk", check=False),
        "wm_size": adb(config.udid, "shell", "wm", "size", check=False),
        "locale": adb(config.udid, "shell", "getprop", "persist.sys.locale", check=False),
        "timezone": adb(config.udid, "shell", "getprop", "persist.sys.timezone", check=False),
    }


def save_evidence(driver, config, folder, started_at, request, status, identity, source):
    return finalize_bundle(
        folder,
        driver=driver,
        platform="aos",
        config=config,
        device=device_evidence(config),
        started_at=started_at,
        request=request,
        status=status,
        identity=identity,
        source=source,
        capture_log=LOGCAT_FILE,
    )


def capture(config, capture_name="MANUAL", setup=None):
    started_at = datetime.now().astimezone().isoformat()
    folder = create_capture_folder(config, capture_name)
    clear_detector_state()
    driver = None
    request = status = identity = source = None
    failed_step = "setup"
    started = time.monotonic()
    try:
        if setup is not None:
            setup(config)
        failed_step = "launch-app"
        adb(config.udid, "shell", "am", "force-stop", config.app_package)
        with LogcatRecorder(config.udid):
            driver = create_driver(config)
            time.sleep(2)
            driver.activate_app(config.app_package)
            time.sleep(1)
            failed_step = "select-placement"
            select_tab(driver, config.tab_text, config.trigger_text)

            attempt = 0
            while True:
                attempt += 1
                if config.max_attempts and attempt > config.max_attempts:
                    raise CaptureError(f"No eligible bid after {config.max_attempts} attempts")
                if config.phase_timeout and time.monotonic() - started > config.phase_timeout:
                    raise CaptureError(f"Capture timed out after {config.phase_timeout:g} seconds")

                if attempt > 1:
                    clear_detector_state()
                    driver.back()
                    time.sleep(1)

                print(f"[capture] attempt {attempt}: tap {config.trigger_text!r}")
                failed_step = f"capture-attempt-{attempt}"
                if not tap_placement(driver, config):
                    driver.activate_app(config.app_package)
                    time.sleep(config.retry_delay)
                    continue

                request, status, identity, source = wait_for_bid(config)
                if eligible(config, request, status, identity):
                    save_evidence(
                        driver, config, folder, started_at, request, status, identity, source
                    )
                    print(f"[captured] {folder}")
                    return folder

                actual_cid = identity.get("cid") if identity else None
                print(f"[retry] status={status or 'unknown'}, cid={actual_cid or 'unknown'}")
                time.sleep(config.retry_delay)
    except Exception as exc:
        try:
            finalize_bundle(
                folder,
                driver=driver,
                platform="aos",
                config=config,
                device=device_evidence(config),
                started_at=started_at,
                request=request,
                status=status,
                identity=identity,
                source=source,
                result="INTERRUPTED",
                failed_step=failed_step,
                error=str(exc),
                capture_log=LOGCAT_FILE,
            )
        except Exception as bundle_exc:
            print(f"[warn] failed to finalize interrupted evidence: {bundle_exc}", file=sys.stderr)
        raise
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception as exc:
                print(f"[warn] Appium session cleanup failed: {exc}", file=sys.stderr)


def run_round(config, name):
    steps = ROUND_DEFINITIONS.get(name)
    if not steps:
        available = ", ".join(sorted(ROUND_DEFINITIONS)) or "none"
        raise CaptureError(f"Round {name!r} is not defined; available rounds: {available}")
    folders = []
    for step in steps:
        if not isinstance(step, RoundStep):
            raise CaptureError(f"Round {name!r} contains an invalid step: {step!r}")
        print(f"\n[round {name}] {step.name}")
        try:
            folders.append(capture(config, capture_name=step.name, setup=step.setup))
        except Exception as exc:
            raise CaptureError(
                f"Round {name!r} failed at step {step.name!r}: {exc}"
            ) from exc
    return folders


def publish_completed_round(evidence_dir):
    """Publish once, only after every capture in a Round has completed."""
    if _env("AUTO_PUBLISH", "1") == "0":
        print("[publish] AUTO_PUBLISH=0; skipped")
        return None
    try:
        return subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parent / "page.py"),
                "--evidence",
                str(evidence_dir),
                "--publish",
            ],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"[warn] Round completed, but report publishing failed: {exc}",
            file=sys.stderr,
        )
        return None


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture_parser = subparsers.add_parser("capture", help="capture one raw bid evidence bundle")
    round_parser = subparsers.add_parser("round", help="execute a declared round")
    round_parser.add_argument("name")
    subparsers.add_parser("list-rounds", help="list declared rounds without touching a device")

    for target in (capture_parser, round_parser):
        target.add_argument("--app-package", default=_env("APP_PACKAGE"))
        target.add_argument("--app-activity", default=_env("APP_ACTIVITY"))
        target.add_argument("--test-mode", default=_env("TEST_MODE"))
        target.add_argument("--test-type", default=_env("TEST_TYPE"))
        target.add_argument("--test-cid", default=_env("TEST_CID"))
        target.add_argument("--test-round", default=_env("TEST_ROUND", "MANUAL"))
        target.add_argument("--trigger-text", default=_env("TRIGGER_TEXT", DEFAULT_TRIGGER_TEXT))
        target.add_argument("--tab-text", default=_env("TAB_TEXT"))
        target.add_argument("--udid", default=_env("UDID"))
        target.add_argument("--evidence-dir", default=_env("EVIDENCE_DIR", str(Path(__file__).parent / "evidence")))
        target.add_argument("--bid-timeout", type=float, default=float(_env("BID_TIMEOUT", "12")))
        target.add_argument("--retry-delay", type=float, default=float(_env("AD_RETRY_DELAY", "2")))
        target.add_argument("--max-attempts", type=int, default=int(_env("MAX_AD_ATTEMPTS", "0")))
        target.add_argument("--phase-timeout", type=float, default=float(_env("PHASE_TIMEOUT_SEC", "0")))
        target.add_argument("--accept-request", action="store_true", default=_env("SAVE_ON_BID", "0") == "1")
        target.add_argument("--capture-name", default="MANUAL")
    return parser


def config_from_args(args):
    missing = [
        name
        for name, value in (
            ("APP_PACKAGE/--app-package", args.app_package),
            ("APP_ACTIVITY/--app-activity", args.app_activity),
            ("TEST_MODE/--test-mode", args.test_mode),
            ("TEST_TYPE/--test-type", args.test_type),
            ("TRIGGER_TEXT/--trigger-text", args.trigger_text),
        )
        if not value
    ]
    if not args.test_cid and not args.accept_request:
        missing.append("TEST_CID/--test-cid (or use --accept-request)")
    if missing:
        raise CaptureError("Missing required configuration: " + ", ".join(missing))
    if args.max_attempts < 0 or args.bid_timeout <= 0 or args.phase_timeout < 0:
        raise CaptureError("Timeouts must be positive and attempt limits cannot be negative")

    mode = args.test_mode.strip().lower()
    tab_text = args.tab_text.strip() or MODE_TABS.get(mode, "")
    if not tab_text:
        raise CaptureError(f"No tab mapping for TEST_MODE={mode!r}; specify --tab-text")
    udid = detect_udid(args.udid.strip())
    return CaptureConfig(
        app_package=args.app_package.strip(),
        app_activity=args.app_activity.strip(),
        test_mode=mode,
        test_type=args.test_type.strip().lower(),
        test_cid=args.test_cid.strip(),
        test_round=_safe_label(args.test_round, "MANUAL", 24),
        trigger_text=args.trigger_text.strip(),
        tab_text=tab_text,
        udid=udid,
        executor=_env("TEST_EXECUTOR", getpass.getuser()).strip() or getpass.getuser(),
        evidence_dir=Path(args.evidence_dir).expanduser(),
        bid_timeout=args.bid_timeout,
        retry_delay=args.retry_delay,
        max_attempts=args.max_attempts,
        phase_timeout=args.phase_timeout,
        accept_request=args.accept_request,
    )


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "list-rounds":
        if not ROUND_DEFINITIONS:
            print("No rounds defined.")
            return 0
        for name, steps in sorted(ROUND_DEFINITIONS.items()):
            print(f"{name}: {', '.join(step.name for step in steps)}")
        return 0

    config = config_from_args(args)
    print(f"[device] {config.udid}")
    print(f"[app]    {config.app_package}/{config.app_activity}")
    print(f"[mode]   {config.test_mode} ({config.tab_text})")
    print(f"[type]   {config.test_type}")
    print(f"[cid]    {config.test_cid or '(any request)'}")
    print(f"[round]  {config.test_round}")

    if args.command == "capture":
        capture(config, capture_name=args.capture_name)
    else:
        try:
            run_round(config, args.name)
        finally:
            publish_completed_round(config.evidence_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CaptureError, OSError, subprocess.SubprocessError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)
