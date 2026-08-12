#!/usr/bin/env python3
"""iOS automation and raw-evidence capture for LazyAdFinder2.

This runner has the same responsibility boundary as ``qa_aos.py`` but a fully
independent implementation based on XCUITest, WebDriverAgent and
libimobiledevice.  It contains no testcase expectations and produces no
PASS/FAILED/BLOCKED verdicts.

Examples:
    python3 qa_ios.py capture
    python3 qa_ios.py capture --accept-request --max-attempts 1
    python3 qa_ios.py list-rounds
    python3 qa_ios.py round <round-name>

Required values: BUNDLE_ID, TEST_MODE, TEST_TYPE and TEST_CID.  TEST_CID may be
omitted only with --accept-request.
"""

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from appium import webdriver
from appium.options.ios.xcuitest.base import XCUITestOptions
from evidence_ios import collect as collect_evidence
from evidence_bundle import finalize_bundle
from testcases.ios_signal_testcases import TC_DEFINITIONS, ROUND_DEFINITIONS
from testcases.ipv6_refresh_testcases import ROUND_DEFINITIONS as IPV6_ROUNDS
from testcases.ipv6_refresh_testcases import TESTCASES as IPV6_TESTCASES
from testcases.ipv6_refresh_testcases import validate_sequence as validate_ipv6_sequence


APPIUM_URL = "http://127.0.0.1:4723"
MODE_TABS = {
    "standalone": "Appier Direct",
    "admob-mediation": "AdMob Mediation",
}
MODE_TRIGGERS = {
    "standalone": "direct (AppierAds SDK)",
    "admob-mediation": "mediation (AdMob + Appier)",
}

FLAG_FILE = Path("/tmp/appier_hit")
BID_FILE = Path("/tmp/appier_bid.json")
BID_STATUS_FILE = Path("/tmp/appier_bid_status")
BID_RESPONSE_FILE = Path("/tmp/appier_bid_response.json")
IMPRESSION_FILE = Path("/tmp/appier_impression.json")
EVENTS_FILE = Path("/tmp/appier_proxy_events.jsonl")
NET_PROBE_RESPONSE_FILE = Path("/tmp/appier_net_probe_response.json")
ADMOB_RAW_FILES = (
    Path("/tmp/admob_pubsetting_request.bin"),
    Path("/tmp/admob_pubsetting_response.bin"),
    Path("/tmp/admob_gma_request.bin"),
    Path("/tmp/admob_gma_response.bin"),
)
SYSLOG_FILE = Path("/tmp/appier_ios_syslog.txt")
DETECTOR_FILES = (
    FLAG_FILE,
    BID_FILE,
    BID_STATUS_FILE,
    BID_RESPONSE_FILE,
    IMPRESSION_FILE,
    EVENTS_FILE,
    NET_PROBE_RESPONSE_FILE,
    *ADMOB_RAW_FILES,
)


@dataclass(frozen=True)
class CaptureConfig:
    bundle_id: str
    test_mode: str
    test_type: str
    test_cid: str
    test_round: str
    trigger_label: str
    tab_name: str
    udid: str
    executor: str
    evidence_dir: Path
    bid_timeout: float
    retry_delay: float
    max_attempts: int
    phase_timeout: float
    accept_request: bool
    xcode_org_id: str
    wda_bundle_id: str
    test_run_id: str
    test_run_started_at: str


@dataclass(frozen=True)
class RoundStep:
    """One future iOS setup followed by one raw capture."""

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


def _run(command, check=True):
    result = subprocess.run(command, text=True, capture_output=True)
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise CaptureError(f"{' '.join(command)}: {detail}")
    return result.stdout.strip()


def connected_udids():
    if shutil.which("idevice_id"):
        return [line.strip() for line in _run(["idevice_id", "-l"]).splitlines() if line.strip()]

    output = _run(["xcrun", "xctrace", "list", "devices"])
    if "== Devices ==" not in output:
        return []
    section = output.split("== Devices ==", 1)[1].split("==", 1)[0]
    return re.findall(
        r"\(([0-9A-Fa-f]{40}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16})\)",
        section,
    )


def detect_udid(requested=""):
    devices = connected_udids()
    if requested:
        if devices and requested not in devices:
            raise CaptureError(f"iOS device {requested!r} is not connected")
        return requested
    if not devices:
        raise CaptureError("No connected iPhone found")
    if len(devices) > 1:
        raise CaptureError(f"Multiple iPhones found: {devices}; specify --udid")
    return devices[0]


class SyslogRecorder:
    def __init__(self, config, output=SYSLOG_FILE):
        self.config = config
        self.output = Path(output)
        self.process = None
        self.stream = None

    def start(self):
        if not shutil.which("idevicesyslog"):
            print("[warn] idevicesyslog is unavailable; syslog evidence will be omitted")
            return self
        command = ["idevicesyslog", "-u", self.config.udid, "-p", self.config.bundle_id]
        self.stream = self.output.open("w")
        self.process = subprocess.Popen(
            command,
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


def clear_syslog_state():
    try:
        SYSLOG_FILE.unlink()
    except FileNotFoundError:
        pass


def create_driver(config):
    options = XCUITestOptions()
    options.bundle_id = config.bundle_id
    options.automation_name = "XCUITest"
    options.no_reset = True
    options.udid = config.udid
    options.set_capability("autoAcceptAlerts", True)
    if config.xcode_org_id:
        options.set_capability("xcodeOrgId", config.xcode_org_id)
        options.set_capability("xcodeSigningId", "Apple Development")
        options.set_capability("allowProvisioningDeviceRegistration", True)
    if config.wda_bundle_id:
        options.set_capability("updatedWDABundleId", config.wda_bundle_id)
    return webdriver.Remote(APPIUM_URL, options=options)


def dismiss_system_alert(driver):
    try:
        driver.execute_script("mobile: alert", {"action": "accept"})
        return True
    except Exception:
        return False


def select_tab(driver, tab_name):
    if not tab_name:
        return
    try:
        driver.find_element("accessibility id", tab_name).click()
        time.sleep(0.6)
    except Exception as exc:
        raise CaptureError(f"Cannot open iOS tab {tab_name!r}: {exc}") from exc


def tap_placement(driver, trigger_label):
    try:
        driver.find_element("accessibility id", trigger_label).click()
        return True
    except Exception:
        return False


def observe_bid():
    request = _read_json(BID_FILE)
    status = _read_text(BID_STATUS_FILE) or None
    identity = _read_json(IMPRESSION_FILE)
    if request is not None:
        source = "proxy"
    elif identity is not None:
        source = "impression"
    else:
        source = None
    return request, status, identity, source


def eligible(config, request, status, identity):
    if config.accept_request:
        return request is not None
    return bool(identity and identity.get("cid") == config.test_cid)


def wait_for_bid(config):
    deadline = time.monotonic() + config.bid_timeout
    while time.monotonic() < deadline:
        observation = observe_bid()
        if eligible(config, *observation[:3]):
            return observation
        time.sleep(0.2)
    return observe_bid()


def round_directory(config):
    config.evidence_dir.mkdir(parents=True, exist_ok=True)
    mode = _safe_label(config.test_mode, "mode").upper()
    kind = _safe_label(config.test_type, "type").upper()
    cid = _safe_label(config.test_cid, "ANY")
    label = _safe_label(config.test_round, "MANUAL")
    run_label = _safe_label(config.test_run_id, "RUN", 64)
    return config.evidence_dir / f"IOS_{mode}_{kind}_CID_{cid}_{label}_{run_label}"


def ideviceinfo(config, key):
    if not shutil.which("ideviceinfo"):
        return ""
    return _run(["ideviceinfo", "-u", config.udid, "-k", key], check=False)


def create_capture_folder(config, capture_name):
    round_dir = round_directory(config)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = round_dir / f"{_safe_label(capture_name, 'CAPTURE')}_{timestamp}"
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def device_evidence(config):
    return {
        "name": ideviceinfo(config, "DeviceName"),
        "product_type": ideviceinfo(config, "ProductType"),
        "product_version": ideviceinfo(config, "ProductVersion"),
        "build_version": ideviceinfo(config, "BuildVersion"),
        "locale": ideviceinfo(config, "Locale"),
        "timezone": ideviceinfo(config, "TimeZone"),
    }


def save_evidence(driver, config, folder, started_at, request, status, identity, source):
    return finalize_bundle(
        folder,
        driver=driver,
        platform="ios",
        config=config,
        device=device_evidence(config),
        started_at=started_at,
        request=request,
        status=status,
        identity=identity,
        source=source,
        capture_log=SYSLOG_FILE,
    )


def capture(config, capture_name="MANUAL", setup=None, warmup_ads=0):
    started_at = datetime.now().astimezone().isoformat()
    folder = create_capture_folder(config, capture_name)
    clear_detector_state()
    clear_syslog_state()
    driver = None
    request = status = identity = source = None
    failed_step = "setup"
    started = time.monotonic()
    try:
        if setup is not None:
            setup(config)
        failed_step = "launch-app"
        with SyslogRecorder(config):
            driver = create_driver(config)
            time.sleep(2)
            dismiss_system_alert(driver)
            failed_step = "select-placement"
            select_tab(driver, config.tab_name)

            attempt = 0
            completed_warmups = 0
            while True:
                attempt += 1
                if config.max_attempts and attempt > config.max_attempts:
                    raise CaptureError(f"No eligible bid after {config.max_attempts} attempts")
                if config.phase_timeout and time.monotonic() - started > config.phase_timeout:
                    raise CaptureError(f"Capture timed out after {config.phase_timeout:g} seconds")

                if attempt > 1:
                    clear_detector_state()
                    try:
                        driver.back()
                    except Exception:
                        pass
                    driver.activate_app(config.bundle_id)
                    time.sleep(1)
                    select_tab(driver, config.tab_name)

                print(f"[capture] attempt {attempt}: tap {config.trigger_label!r}")
                failed_step = f"capture-attempt-{attempt}"
                if not tap_placement(driver, config.trigger_label):
                    driver.activate_app(config.bundle_id)
                    time.sleep(config.retry_delay)
                    continue

                request, status, identity, source = wait_for_bid(config)
                if eligible(config, request, status, identity):
                    if completed_warmups < warmup_ads:
                        completed_warmups += 1
                        print(f"[warmup] completed {completed_warmups}/{warmup_ads}")
                        time.sleep(config.retry_delay)
                        continue
                    save_evidence(
                        driver, config, folder, started_at, request, status, identity, source
                    )
                    print(f"[captured] {folder}")
                    return folder

                actual_cid = identity.get("cid") if identity else None
                print(
                    f"[retry] request={'yes' if request is not None else 'no'}, "
                    f"status={status or 'unknown'}, cid={actual_cid or 'unknown'}"
                )
                time.sleep(config.retry_delay)
    except Exception as exc:
        try:
            finalize_bundle(
                folder,
                driver=driver,
                platform="ios",
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
                capture_log=SYSLOG_FILE,
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


def _r4_checkpoint(label, instruction):
    print(f"\n[R4 checkpoint: {label}]\n{instruction}")
    if not sys.stdin.isatty():
        print("[R4] non-interactive terminal; remaining operator-controlled steps are BLOCKED")
        return False
    answer = input("完成後按 Enter；輸入 skip 停止本輪：").strip().lower()
    return answer not in {"skip", "s", "stop", "q", "quit"}


def _r4_capture_in_session(driver, config, name, wait_seconds):
    folder = create_capture_folder(config, name)
    started_at = datetime.now().astimezone().isoformat()
    clear_detector_state()
    request = status = identity = source = None
    try:
        driver.activate_app(config.bundle_id)
        dismiss_system_alert(driver)
        select_tab(driver, config.tab_name)
        print(f"[R4] wait {wait_seconds}s before {name}")
        time.sleep(wait_seconds)
        if not tap_placement(driver, config.trigger_label):
            raise CaptureError(f"cannot tap {config.trigger_label!r}")
        request, status, identity, source = wait_for_bid(config)
        if not eligible(config, request, status, identity):
            raise CaptureError("no eligible ad request after the network transition")
        save_evidence(driver, config, folder, started_at, request, status, identity, source)
        return folder
    except Exception as exc:
        finalize_bundle(
            folder,
            driver=driver,
            platform="ios",
            config=config,
            device=device_evidence(config),
            started_at=started_at,
            request=request,
            status=status,
            identity=identity,
            source=source,
            result="INTERRUPTED",
            failed_step=name,
            error=str(exc),
            capture_log=SYSLOG_FILE,
        )
        return folder


def run_ipv6_refresh_round(config):
    """Run R4 in one live App session; humans control network transitions."""
    folders = []
    context = {"platform": "ios", "same_appium_session": True, "slow_network_confirmed": False}
    driver = None
    try:
        clear_syslog_state()
        with SyslogRecorder(config):
            driver = create_driver(config)
            driver.terminate_app(config.bundle_id)
            time.sleep(1)
            driver.activate_app(config.bundle_id)
            folders.append(_r4_capture_in_session(driver, config, "IOS-NET-01", 10))

            preflight = validate_ipv6_sequence(folders, context)
            if preflight and preflight[0].get("status") == "BLOCKED":
                print("[R4] current network has no valid IPv6; block the entire Round")
            elif _r4_checkpoint(
                "IOS-NET-02",
                "App 保持開啟。請從公司 Wi-Fi 切到另一個 Wi-Fi／hotspot，確認已連線。",
            ):
                folders.append(_r4_capture_in_session(driver, config, "IOS-NET-02", 10))
                if _r4_checkpoint(
                    "IOS-NET-03",
                    "App 保持開啟。關閉 Wi-Fi，等待 10 秒，再開啟並重新連線；確認可上網。",
                ):
                    folders.append(_r4_capture_in_session(driver, config, "IOS-NET-03", 10))
                    if _r4_checkpoint(
                        "IOS-NET-04",
                        "快速切換：公司 Wi-Fi → 第二個 Wi-Fi → 公司 Wi-Fi → 第二個 Wi-Fi；最後停在第二個 Wi-Fi。",
                    ):
                        folders.append(_r4_capture_in_session(driver, config, "IOS-NET-04", 10))
                        if _r4_checkpoint(
                            "IOS-NET-05",
                            "啟用 1～2 秒延遲的 Network Link Conditioner／Charles throttle，保持 App 開啟並切換 Wi-Fi。",
                        ):
                            context["slow_network_confirmed"] = True
                            folders.append(_r4_capture_in_session(driver, config, "IOS-NET-05", 15))
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception as exc:
                print(f"[warn] Appium session cleanup failed: {exc}", file=sys.stderr)

    if not folders:
        raise CaptureError("R4 did not create any Evidence folder")
    result_folder = folders[-1]
    sequence = {
        "round": "R4",
        "same_appium_session": context["same_appium_session"],
        "slow_network_confirmed": context["slow_network_confirmed"],
        "captures": [str(folder) for folder in folders],
    }
    (result_folder / "r4-network-sequence.json").write_text(
        json.dumps(sequence, ensure_ascii=False, indent=2) + "\n"
    )
    rows = validate_ipv6_sequence(folders, context)
    (result_folder / "verdicts.json").write_text(
        json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n"
    )
    return folders


def run_round(config, name):
    if name == "R4":
        return run_ipv6_refresh_round(config)
    definition = ROUND_DEFINITIONS.get(name)
    if not definition:
        available = ", ".join(sorted(ROUND_DEFINITIONS)) or "none"
        raise CaptureError(f"Round {name!r} is not defined; available rounds: {available}")
    testcases = [TC_DEFINITIONS[key] for key in definition.testcase_keys]
    required = tuple(item for testcase in testcases for item in testcase.evidence)
    print(f"\n[round {name}] {definition.capture_name}")
    try:
        folder = collect_evidence(
            config,
            required,
            lambda setup: capture(
                config, capture_name=definition.capture_name, setup=setup,
                warmup_ads=definition.warmup_ads,
            ),
        )
    except Exception as exc:
        round_dir = round_directory(config)
        candidates = sorted(path for path in round_dir.iterdir() if path.is_dir()) if round_dir.is_dir() else []
        if candidates:
            folder = candidates[-1]
            summary = _read_json(folder / "summary.json") or {}
            actual_cid = summary.get("cid")
            reason = str(exc)
            if "No eligible bid" in reason:
                reason = (
                    f"iOS R1 reached the {config.max_attempts}-attempt limit without the target CID "
                    f"{config.test_cid}; the last captured CID was {actual_cid or 'unknown'}"
                )
            rows = []
            for testcase in testcases:
                row = blocked(testcase.key, reason).to_dict()
                row.update({"layer": "Signal", "title": testcase.title, "description": testcase.description})
                rows.append(row)
            (folder / "verdicts.json").write_text(json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n")
        raise
    rows = []
    for testcase in testcases:
        try:
            rows.append(testcase.validate(folder))
        except Exception as exc:
            row = {
                "tc": testcase.key, "status": "FAILED",
                "reason": f"iOS validator could not compare captured Evidence: {type(exc).__name__}: {exc}",
                "expected": "The iOS validator completes with its platform Evidence",
                "actual": {"error_type": type(exc).__name__},
                "evidence": "evidence-errors.json" if (Path(folder) / "evidence-errors.json").is_file() else "summary.json",
                "layer": "Signal", "title": testcase.title, "description": testcase.description,
            }
            rows.append(row)
    (Path(folder) / "verdicts.json").write_text(json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n")
    return [Path(folder)]


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

    capture_parser = subparsers.add_parser("capture", help="capture one raw iOS evidence bundle")
    round_parser = subparsers.add_parser("round", help="execute a declared iOS round")
    round_parser.add_argument("name")
    subparsers.add_parser("list-rounds", help="list declared rounds without touching a device")

    for target in (capture_parser, round_parser):
        target.add_argument("--bundle-id", default=_env("BUNDLE_ID"))
        target.add_argument("--test-mode", default=_env("TEST_MODE"))
        target.add_argument("--test-type", default=_env("TEST_TYPE"))
        target.add_argument("--test-cid", default=_env("TEST_CID"))
        target.add_argument("--trigger-label", default=_env("TRIGGER_LABEL", _env("AD_LABEL")))
        target.add_argument("--tab-name", default=_env("TAB"))
        target.add_argument("--udid", default=_env("UDID"))
        target.add_argument("--evidence-dir", default=_env("EVIDENCE_DIR", str(Path(__file__).parent / "evidence")))
        target.add_argument("--bid-timeout", type=float, default=float(_env("BID_TIMEOUT", "12")))
        target.add_argument("--retry-delay", type=float, default=float(_env("AD_RETRY_DELAY", "2")))
        target.add_argument("--max-attempts", type=int, default=int(_env("MAX_AD_ATTEMPTS", "20")))
        target.add_argument("--phase-timeout", type=float, default=float(_env("PHASE_TIMEOUT_SEC", "0")))
        target.add_argument("--accept-request", action="store_true", default=_env("SAVE_ON_BID", "0") == "1")
        target.add_argument("--capture-name", default="MANUAL")
        target.add_argument("--xcode-org-id", default=_env("XCODE_ORG_ID"))
        target.add_argument("--wda-bundle-id", default=_env("WDA_BUNDLE_ID"))
    return parser


def config_from_args(args):
    missing = [
        name
        for name, value in (
            ("BUNDLE_ID/--bundle-id", args.bundle_id),
            ("TEST_MODE/--test-mode", args.test_mode),
            ("TEST_TYPE/--test-type", args.test_type),
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
    tab_name = args.tab_name.strip() or MODE_TABS.get(mode, "")
    trigger_label = args.trigger_label.strip() or MODE_TRIGGERS.get(mode, "")
    if not tab_name:
        raise CaptureError(f"No tab mapping for TEST_MODE={mode!r}; specify --tab-name")
    if not trigger_label:
        raise CaptureError(f"No placement mapping for TEST_MODE={mode!r}; specify --trigger-label")
    udid = detect_udid(args.udid.strip())
    return CaptureConfig(
        bundle_id=args.bundle_id.strip(),
        test_mode=mode,
        test_type=args.test_type.strip().lower(),
        test_cid=args.test_cid.strip(),
        test_round=_safe_label(args.name if args.command == "round" else "MANUAL", "MANUAL", 24),
        trigger_label=trigger_label,
        tab_name=tab_name,
        udid=udid,
        executor=_env("TEST_EXECUTOR", getpass.getuser()).strip() or getpass.getuser(),
        evidence_dir=Path(args.evidence_dir).expanduser(),
        bid_timeout=args.bid_timeout,
        retry_delay=args.retry_delay,
        max_attempts=args.max_attempts,
        phase_timeout=args.phase_timeout,
        accept_request=args.accept_request,
        xcode_org_id=args.xcode_org_id.strip(),
        wda_bundle_id=args.wda_bundle_id.strip(),
        test_run_id=_env("TEST_RUN_ID", f"ios-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{os.getpid()}").strip(),
        test_run_started_at=_env("TEST_RUN_STARTED_AT", datetime.now().astimezone().isoformat()).strip(),
    )


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "list-rounds":
        all_rounds = {**ROUND_DEFINITIONS, **IPV6_ROUNDS}
        if not all_rounds:
            print("No rounds defined.")
            return 0
        for name, definition in sorted(all_rounds.items()):
            if name in ROUND_DEFINITIONS:
                print(f"{name}: {definition.capture_name} [{', '.join(definition.testcase_keys)}]")
            else:
                print(f"{name}: {', '.join(IPV6_TESTCASES[key].title for key in definition)}")
        return 0

    config = config_from_args(args)
    print(f"[device] {config.udid}")
    print(f"[app]    {config.bundle_id}")
    print(f"[mode]   {config.test_mode} ({config.tab_name})")
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
