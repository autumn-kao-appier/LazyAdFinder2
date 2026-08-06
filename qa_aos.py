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

from appium import webdriver
from appium.options.android.uiautomator2.base import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from evidence_aos import collect as collect_evidence
from evidence_bundle import decoded_bid, finalize_bundle
from testcases.android_signal_testcases import ROUND_DEFINITIONS, TC_DEFINITIONS
from testcases.e2e.android_standalone_e2e import TESTCASES as STANDALONE_E2E_TESTCASES
from testcases.e2e.android_standalone_e2e import validate_bundle as validate_standalone_e2e
from verdict import blocked


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
EVENTS_FILE = Path("/tmp/appier_proxy_events.jsonl")
LOGCAT_FILE = Path("/tmp/appier_aos_logcat.txt")
DETECTOR_FILES = (
    FLAG_FILE,
    BID_FILE,
    BID_STATUS_FILE,
    BID_RESPONSE_FILE,
    IMPRESSION_FILE,
    EVENTS_FILE,
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


class CaptureError(RuntimeError):
    evidence_folder = None


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


def wait_for_bid(config, proxy_only=False):
    deadline = time.monotonic() + config.bid_timeout
    while time.monotonic() < deadline:
        if proxy_only:
            request = _read_json(BID_FILE)
            status = _read_text(BID_STATUS_FILE) or None
            identity = _read_json(IMPRESSION_FILE)
            source = "proxy" if request is not None else None
        else:
            request, status, identity, source = observe_bid()
        if eligible(config, request, status, identity):
            return request, status, identity, source
        time.sleep(0.2)
    if proxy_only:
        request = _read_json(BID_FILE)
        return request, _read_text(BID_STATUS_FILE) or None, _read_json(IMPRESSION_FILE), "proxy" if request is not None else None
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


def _app_pid(config):
    return adb(config.udid, "shell", "pidof", config.app_package, check=False).strip() or None


def _return_to_placement(driver, config):
    driver.back()
    time.sleep(1)
    if find_visible_text(driver, config.trigger_text) is None:
        driver.activate_app(config.app_package)
        time.sleep(1)
    if find_visible_text(driver, config.trigger_text) is None:
        select_tab(driver, config.tab_text, config.trigger_text)


def _sequence_request(driver, config, folder, number, label):
    clear_detector_state()
    if not tap_placement(driver, config):
        raise CaptureError(f"R3 {label}: cannot tap {config.trigger_text!r}")
    request, status, identity, source = wait_for_bid(config, proxy_only=True)
    if not eligible(config, request, status, identity):
        raise CaptureError(f"R3 {label}: no eligible bid/impression")
    decoded = decoded_bid(request)
    user = decoded.get("ext", {}).get("plaintext", {}).get("user", {})
    value = user.get("session_duration") if isinstance(user, dict) else None
    app_init_time = user.get("app_init_time") if isinstance(user, dict) else None
    app_duration = user.get("app_duration") if isinstance(user, dict) else None
    prefix = f"{number:02d}-{label}"
    (folder / f"{prefix}-bid-raw.json").write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n")
    (folder / f"{prefix}-bid-decoded.json").write_text(json.dumps(decoded, ensure_ascii=False, indent=2) + "\n")
    driver.get_screenshot_as_file(str(folder / f"{prefix}.png"))
    return {
        "step": number,
        "label": label,
        "session_duration": value,
        "app_init_time": app_init_time,
        "app_duration": app_duration,
        "captured_epoch_ms": round(time.time() * 1000),
        "pid": _app_pid(config),
        "http_status": status,
        "cid": identity.get("cid") if identity else None,
        "request_file": f"{prefix}-bid-raw.json",
        "decoded_file": f"{prefix}-bid-decoded.json",
        "screenshot": f"{prefix}.png",
    }, request, status, identity, source


def _capture_session_duration_sequence(driver, config, folder, started_at):
    steps = []
    last = (None, None, None, None)

    step, *last = _sequence_request(driver, config, folder, 1, "cold-start")
    steps.append(step)
    time.sleep(2)
    _return_to_placement(driver, config)

    step, *last = _sequence_request(driver, config, folder, 2, "continuous")
    steps.append(step)
    _return_to_placement(driver, config)
    pid_before_background = _app_pid(config)
    adb(config.udid, "shell", "input", "keyevent", "KEYCODE_HOME")
    time.sleep(3)
    if _app_pid(config) != pid_before_background:
        raise CaptureError("R3 background: App process did not remain alive")
    driver.activate_app(config.app_package)
    time.sleep(1)
    if find_visible_text(driver, config.trigger_text) is None:
        select_tab(driver, config.tab_text, config.trigger_text)

    step, *last = _sequence_request(driver, config, folder, 3, "after-background")
    steps.append(step)
    _return_to_placement(driver, config)
    terminated_pid = _app_pid(config)
    adb(config.udid, "shell", "input", "keyevent", "KEYCODE_HOME")
    adb(config.udid, "shell", "input", "keyevent", "KEYCODE_APP_SWITCH")
    time.sleep(1)
    size = driver.get_window_size()
    driver.swipe(size["width"] // 2, int(size["height"] * 0.75), size["width"] // 2, int(size["height"] * 0.12), 700)
    time.sleep(2)
    terminated_pid_confirmed = _app_pid(config) is None
    if not terminated_pid_confirmed:
        raise CaptureError("R3 termination: swiping the App from Recents did not stop its process")
    relaunch_requested_epoch_ms = round(time.time() * 1000)
    driver.activate_app(config.app_package)
    time.sleep(2)
    select_tab(driver, config.tab_text, config.trigger_text)

    step, *last = _sequence_request(driver, config, folder, 4, "after-termination")
    steps.append(step)
    document = {
        "strategy": "four-request session-duration sequence",
        "terminated_pid": terminated_pid,
        "terminated_pid_confirmed": terminated_pid_confirmed,
        "relaunch_requested_epoch_ms": relaunch_requested_epoch_ms,
        "steps": steps,
    }
    (folder / "session-duration-sequence.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    request, status, identity, source = last
    save_evidence(driver, config, folder, started_at, request, status, identity, source)
    return folder


def capture(config, capture_name="MANUAL", setup=None, warmup_ads=0, strategy="standard"):
    started_at = datetime.now().astimezone().isoformat()
    folder = create_capture_folder(config, capture_name)
    clear_detector_state()
    driver = None
    request = status = identity = source = None
    warmup_impression = None
    completed_warmups = 0
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

            if strategy == "session-duration":
                failed_step = "session-duration-sequence"
                result = _capture_session_duration_sequence(driver, config, folder, started_at)
                print(f"[captured] {result}")
                return result

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

                request, status, identity, source = wait_for_bid(config, proxy_only=completed_warmups > 0)
                if eligible(config, request, status, identity):
                    if completed_warmups < warmup_ads:
                        if not identity:
                            print("[warmup] bid received without a confirmed impression; retrying")
                            time.sleep(config.retry_delay)
                            continue
                        warmup_impression = identity
                        completed_warmups += 1
                        print(f"[warmup] confirmed ad {completed_warmups}/{warmup_ads}; continuing in the same app session")
                        time.sleep(config.retry_delay)
                        continue
                    save_evidence(
                        driver, config, folder, started_at, request, status, identity, source
                    )
                    if warmup_impression is not None:
                        (folder / "previous-impression.json").write_text(
                            json.dumps(warmup_impression, ensure_ascii=False, indent=2) + "\n"
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
        error = exc if isinstance(exc, CaptureError) else CaptureError(str(exc))
        error.evidence_folder = folder
        raise error from exc
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception as exc:
                print(f"[warn] Appium session cleanup failed: {exc}", file=sys.stderr)


def run_round(config, name):
    if name == "E2E-STANDALONE":
        if config.test_mode != "standalone":
            raise CaptureError("E2E-STANDALONE requires TEST_MODE=standalone")
        folder = collect_evidence(
            config,
            ("bid",),
            lambda setup: capture(config, capture_name="E2E-STANDALONE", setup=setup),
        )
        rows = validate_standalone_e2e(folder)
        (folder / "verdicts.json").write_text(
            json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n"
        )
        return [folder]
    round_definition = ROUND_DEFINITIONS.get(name)
    if not round_definition:
        available = ", ".join(sorted(ROUND_DEFINITIONS)) or "none"
        raise CaptureError(f"Round {name!r} is not defined; available rounds: {available}")
    try:
        testcases = [TC_DEFINITIONS[key] for key in round_definition.testcase_keys]
    except KeyError as exc:
        raise CaptureError(f"Round {name!r} references unknown TestCase {exc.args[0]!r}") from exc
    required_evidence = tuple(
        evidence_key for testcase in testcases for evidence_key in testcase.evidence
    )
    print(f"\n[round {name}] {round_definition.capture_name}")
    phase = "Evidence capture"
    try:
        folder = collect_evidence(
            config,
            required_evidence,
            lambda setup: capture(
                config,
                capture_name=round_definition.capture_name,
                setup=setup,
                warmup_ads=round_definition.warmup_ads,
                strategy=round_definition.strategy,
            ),
        )
        phase = "TestCase validation"
        verdicts = []
        validator_errors = []
        for testcase in testcases:
            try:
                verdicts.append(testcase.validate(folder))
            except Exception as exc:
                row = blocked(testcase.key, str(exc)).to_dict()
                row.update({"layer": "Signal", "title": testcase.title, "description": testcase.description})
                verdicts.append(row)
                validator_errors.append(f"{testcase.key}: {exc}")
        (folder / "verdicts.json").write_text(
            json.dumps({"verdicts": verdicts}, ensure_ascii=False, indent=2) + "\n"
        )
        if validator_errors:
            error = CaptureError("; ".join(validator_errors))
            error.evidence_folder = folder
            raise error
        return [folder]
    except Exception as exc:
        evidence_folder = getattr(exc, "evidence_folder", None)
        if evidence_folder is not None:
            verdict_path = Path(evidence_folder) / "verdicts.json"
            if not verdict_path.exists():
                rows = []
                for testcase in testcases:
                    row = blocked(testcase.key, str(exc)).to_dict()
                    row.update({"layer": "Signal", "title": testcase.title, "description": testcase.description})
                    rows.append(row)
                verdict_path.write_text(
                    json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n"
                )
        error = CaptureError(
            f"Round {name!r} failed at {phase} {round_definition.capture_name!r}: {exc}"
        )
        error.evidence_folder = evidence_folder
        raise error from exc


def publish_completed_round(evidence_dir, folders):
    """Publish only after this Round's finalized verdict files are on disk."""
    folders = [Path(folder) for folder in folders if folder is not None]
    if not folders:
        print("[publish] skipped; this Round produced no Evidence folder", file=sys.stderr)
        return None
    missing = [folder for folder in folders if not (folder / "verdicts.json").is_file()]
    if missing:
        joined = ", ".join(str(folder) for folder in missing)
        print(f"[publish] skipped; verdicts.json is not finalized: {joined}", file=sys.stderr)
        return None
    if _env("AUTO_PUBLISH", "1") == "0":
        print("[publish] AUTO_PUBLISH=0; skipped")
        return None
    sys.stdout.flush()
    sys.stderr.flush()
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
        for name, definition in sorted(ROUND_DEFINITIONS.items()):
            tc_ids = ", ".join(definition.testcase_keys)
            print(f"{name}: {definition.capture_name} [{tc_ids}]")
        print("E2E-STANDALONE: E2E-STANDALONE [" + ", ".join(STANDALONE_E2E_TESTCASES) + "]")
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
            folders = run_round(config, args.name)
        except Exception as exc:
            evidence_folder = getattr(exc, "evidence_folder", None)
            publish_completed_round(
                config.evidence_dir,
                [evidence_folder] if evidence_folder is not None else [],
            )
            raise
        else:
            publish_completed_round(config.evidence_dir, folders)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CaptureError, OSError, subprocess.SubprocessError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)
