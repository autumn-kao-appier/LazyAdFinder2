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
import base64
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from appium import webdriver
from appium.options.ios.xcuitest.base import XCUITestOptions
from evidence_ios import collect as collect_evidence
from evidence_bundle import finalize_bundle
from testcases.ios_signal_testcases import TC_DEFINITIONS, ROUND_DEFINITIONS, R5_SCENARIOS
from testcases.e2e.ios_e2e_baseline import TESTCASES as BASELINE_E2E_TESTCASES
from testcases.e2e.ios_e2e_baseline import validate_bundle as validate_baseline_e2e
from testcases.e2e.ios_admob_mediation_extensions import TESTCASES as ADMOB_E2E_EXTENSIONS
from testcases.e2e.ios_admob_mediation_extensions import validate_bundle as validate_admob_extensions
from testcases.ipv6_refresh_testcases import ROUND_DEFINITIONS as IPV6_ROUNDS
from testcases.ipv6_refresh_testcases import TESTCASES as IPV6_TESTCASES
from testcases.ipv6_refresh_testcases import validate_sequence as validate_ipv6_sequence
from verdict import blocked


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
CHARLES_PORT = 8888
MITMDUMP_PORT = 8081
MITMDUMP_LOG = Path("/tmp/lazyadfinder2_mitmdump.log")
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
    target_app_bundle_id: str = ""
    selected_scenarios: tuple[str, ...] = ()

    @property
    def app_package(self):
        return self.bundle_id

    @property
    def target_app_package(self):
        return self.target_app_bundle_id


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


def create_driver(config, bundle_id=None):
    options = XCUITestOptions()
    options.bundle_id = bundle_id or config.bundle_id
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


def capture_tracking_settings(driver, config):
    """Read and visibly preserve the native iOS Tracking switch without mutating it."""
    state_path = Path(os.environ.get("IOS_SETTINGS_STATE_FILE", "/tmp/laf2-ios-settings-state.json"))
    screenshot_path = Path(os.environ.get("IOS_SETTINGS_SCREENSHOT", "/tmp/laf2-ios-settings-state.png"))
    state = {"scenario": "TRACKING-ALLOWED", "confirmed_by_operator": False,
             "screenshot_saved": False, "att": {"authorization": None}, "switches": []}
    try:
        driver.activate_app("com.apple.Preferences")
        time.sleep(1)
        privacy = _first_element(driver, (
            ("accessibility id", "Privacy & Security"),
            ("accessibility id", "Privacy"),
        ))
        if privacy is None:
            for _ in range(5):
                driver.swipe(180, 650, 180, 250, 500)
                privacy = _first_element(driver, (("accessibility id", "Privacy & Security"), ("accessibility id", "Privacy")))
                if privacy is not None:
                    break
        if privacy is None:
            raise CaptureError("native Settings does not expose Privacy & Security")
        privacy.click()
        time.sleep(.8)
        tracking = _first_element(driver, (("accessibility id", "Tracking"),))
        if tracking is None:
            raise CaptureError("native Privacy & Security does not expose Tracking")
        tracking.click()
        time.sleep(.8)
        switches = driver.find_elements("class name", "XCUIElementTypeSwitch")
        for switch in switches:
            state["switches"].append({
                "name": switch.get_attribute("name") or switch.get_attribute("label"),
                "value": switch.get_attribute("value"),
            })
        selected = next(
            (item for item in state["switches"] if item.get("name") == config.bundle_id),
            None,
        )
        value = str((selected or {}).get("value") or "").lower()
        if value in {"1", "true", "on"}:
            state["att"]["authorization"] = "authorized"
        elif value in {"0", "false", "off"}:
            state["att"]["authorization"] = "denied"
        driver.save_screenshot(str(screenshot_path))
        state["screenshot_saved"] = screenshot_path.is_file() and screenshot_path.stat().st_size > 0
        state["confirmed_by_operator"] = bool(selected and state["screenshot_saved"])
    except Exception as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        state["captured_at"] = datetime.now().astimezone().isoformat()
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        driver.activate_app(config.bundle_id)
        time.sleep(1)
    return state


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


def _tcp_listening(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _listener_commands(port):
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        text=True, capture_output=True,
    )
    commands = []
    for pid in (line.strip() for line in result.stdout.splitlines()):
        if not pid.isdigit():
            continue
        command = subprocess.run(
            ["ps", "-p", pid, "-o", "command="], text=True, capture_output=True,
        ).stdout.strip()
        if command:
            commands.append(command)
    return commands


def ensure_e2e_proxy_ready():
    """Fail before UI actions unless the complete Charles→mitmdump path exists."""
    if not _tcp_listening("127.0.0.1", CHARLES_PORT):
        raise CaptureError(f"E2E proxy preflight failed: Charles is not listening on :{CHARLES_PORT}")
    charles = _listener_commands(CHARLES_PORT)
    if not any("Charles.app/Contents/MacOS/Charles" in command for command in charles):
        raise CaptureError(f"E2E proxy preflight failed: :{CHARLES_PORT} is not owned by Charles")
    if not _tcp_listening("127.0.0.1", MITMDUMP_PORT):
        executable = shutil.which("mitmdump")
        if not executable:
            raise CaptureError("E2E proxy preflight failed: mitmdump is unavailable")
        addon = Path(__file__).with_name("mitmdump_addon.py").resolve()
        stream = MITMDUMP_LOG.open("a")
        try:
            subprocess.Popen(
                [executable, "-s", str(addon), "--listen-port", str(MITMDUMP_PORT)],
                cwd=addon.parent, stdout=stream, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            stream.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _tcp_listening("127.0.0.1", MITMDUMP_PORT):
            time.sleep(.2)
    if not _tcp_listening("127.0.0.1", MITMDUMP_PORT):
        raise CaptureError(f"E2E proxy preflight failed: mitmdump is not listening on :{MITMDUMP_PORT}")
    addon = str(Path(__file__).with_name("mitmdump_addon.py").resolve())
    if not any("mitmdump" in command and addon in command for command in _listener_commands(MITMDUMP_PORT)):
        raise CaptureError("E2E proxy preflight failed: mitmdump is not using this repo's addon")
    print(f"[proxy preflight] READY: iPhone → Charles :{CHARLES_PORT} → mitmdump :{MITMDUMP_PORT}")


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


def _active_app(driver):
    try:
        info = driver.execute_script("mobile: activeAppInfo") or {}
    except Exception:
        info = {}
    return {"bundle_id": info.get("bundleId") or info.get("bundle_id"),
            "pid": info.get("pid"), "name": info.get("name")}


def _first_element(driver, queries):
    for strategy, value in queries:
        try:
            elements = driver.find_elements(strategy, value)
        except Exception:
            continue
        for element in elements:
            try:
                if element.is_displayed() and element.is_enabled():
                    return element
            except Exception:
                continue
    return None


def _capture_e2e_interactions(driver, config, folder):
    """Record one visible iOS journey and preserve every interaction outcome."""
    folder = Path(folder)
    result = {
        "sequence": ["rendered-ad", "privacy", "return-to-ad", "click", "landing"],
        "timeline": [],
        "privacy": {"attempted": False, "opened": False},
        "click": {"attempted": False, "opened": False},
        "errors": [],
    }
    timeline_started = time.monotonic()

    def mark(stage, outcome="STARTED", **details):
        result["timeline"].append({
            "stage": stage,
            "outcome": outcome,
            "timestamp": datetime.now().astimezone().isoformat(),
            "offset_seconds": round(time.monotonic() - timeline_started, 3),
            **details,
        })

    recording_started = False
    try:
        driver.start_recording_screen(video_type="h264", video_quality="medium")
        recording_started = True
        mark("recording", "STARTED")
    except Exception as exc:
        result["errors"].append(f"recording-start: {exc}")
        mark("recording", "FAILED", error=str(exc))
    try:
        time.sleep(1)
        driver.save_screenshot(str(folder / "ad-before-interactions.png"))
        mark("rendered-ad", "CAPTURED", screenshot="ad-before-interactions.png")
        source = driver.page_source or ""
        (folder / "rendered-page-source.xml").write_text(source)
        response = _read_json(BID_RESPONSE_FILE) or {}
        expected_text = []
        try:
            native = response["adUnits"][0]["ad"]["native"]
            for key in ("title", "text", "ctaText"):
                value = native.get(key)
                if isinstance(value, dict):
                    value = value.get("text") or value.get("value")
                if isinstance(value, str) and value.strip():
                    expected_text.append(value.strip())
        except (KeyError, IndexError, TypeError):
            pass
        missing_text = [value for value in expected_text if value not in source]
        visual = {
            "platform": "ios", "expected_text": expected_text,
            "missing_text": missing_text,
            "screenshot_saved": (folder / "ad-before-interactions.png").is_file(),
            "passed": bool(expected_text and not missing_text),
            "human_review": "Inspect screenshot for clipping, broken images, Ad label, and privacy icon.",
        }
        (folder / "visual-review.json").write_text(json.dumps(visual, ensure_ascii=False, indent=2) + "\n")

        privacy = _first_element(driver, (
            ("xpath", "//*[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'privacy')]"),
            ("xpath", "//*[contains(translate(@label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'adchoices')]"),
        ))
        if privacy is not None:
            before = _active_app(driver)
            result["privacy"]["attempted"] = True
            mark("privacy", "TAPPED")
            privacy.click()
            time.sleep(2)
            after = _active_app(driver)
            result["privacy"].update({"before": before, "destination": after,
                                      "opened": after != before})
            driver.save_screenshot(str(folder / "privacy-landing.png"))
            mark("privacy-destination", "CAPTURED", screenshot="privacy-landing.png", destination=after)
            driver.back()
            time.sleep(1)
            mark("return-to-ad", "COMPLETED")
        else:
            result["errors"].append("privacy: visible Privacy/AdChoices control not found")
            mark("privacy", "FAILED", error="visible Privacy/AdChoices control not found")

        driver.save_screenshot(str(folder / "ad-before-click.png"))
        cta = _first_element(driver, (
            ("xpath", "//XCUIElementTypeButton[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'install')]"),
            ("xpath", "//XCUIElementTypeButton[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'open')]"),
            ("xpath", "//XCUIElementTypeButton[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'learn')]"),
            ("xpath", "//XCUIElementTypeButton[contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'shop')]"),
        ))
        if cta is not None:
            before = _active_app(driver)
            result["click"]["attempted"] = True
            mark("cta", "TAPPED")
            cta.click()
            time.sleep(3)
            after = _active_app(driver)
            result["click"].update({"before": before, "destination": after,
                                    "opened": after != before})
            driver.save_screenshot(str(folder / "click-landing.png"))
            mark("landing", "CAPTURED", screenshot="click-landing.png", destination=after)
        else:
            result["errors"].append("click: visible CTA control not found")
            mark("cta", "FAILED", error="visible CTA control not found")
    except Exception as exc:
        result["errors"].append(f"interaction: {type(exc).__name__}: {exc}")
        try:
            driver.save_screenshot(str(folder / "interaction-failure.png"))
        except Exception:
            pass
    finally:
        if recording_started:
            try:
                encoded = driver.stop_recording_screen()
                payload = base64.b64decode(encoded) if encoded else b""
                if payload:
                    (folder / "e2e-interactions.mp4").write_bytes(payload)
                    mark("recording", "SAVED", bytes=len(payload))
                else:
                    result["errors"].append("recording-stop: empty video")
            except Exception as exc:
                result["errors"].append(f"recording-stop: {exc}")
        result["recording"] = {
            "saved": (folder / "e2e-interactions.mp4").is_file(),
            "bytes": (folder / "e2e-interactions.mp4").stat().st_size if (folder / "e2e-interactions.mp4").is_file() else 0,
            "valid_mp4": (
                (folder / "e2e-interactions.mp4").is_file()
                and b"moov" in (folder / "e2e-interactions.mp4").read_bytes()
            ),
        }
        (folder / "e2e-interactions.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


def capture(config, capture_name="MANUAL", setup=None, warmup_ads=0, strategy="standard"):
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
            if config.test_round == "R1":
                print("[evidence] capture native iOS Settings → Privacy & Security → Tracking")
                capture_tracking_settings(driver, config)
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
                    if strategy == "e2e":
                        print("[e2e] record ad → Privacy → return → CTA → destination")
                        _capture_e2e_interactions(driver, config, folder)
                        time.sleep(2)
                    try:
                        (folder / "app-page-source.xml").write_text(driver.page_source or "")
                    except Exception as exc:
                        print(f"[warn] iOS App-language UI Evidence was not saved: {exc}", file=sys.stderr)
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
        error = exc if isinstance(exc, CaptureError) else CaptureError(str(exc))
        error.evidence_folder = folder
        raise error from exc
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


def _decoded_user(folder):
    decoded = _read_json(Path(folder) / "bid_decoded.json") or {}
    return (((decoded.get("ext") or {}).get("plaintext") or {}).get("user") or {})


def _write_failed_verdicts(folder, keys, reason, layer="Signal"):
    folder = Path(folder)
    rows = []
    for key in keys:
        testcase = TC_DEFINITIONS.get(key) or BASELINE_E2E_TESTCASES.get(key) or ADMOB_E2E_EXTENSIONS.get(key)
        rows.append({
            "tc": key, "status": "FAILED", "reason": reason,
            "expected": "The declared iOS operation completes and produces comparable Evidence",
            "actual": {"error": reason},
            "evidence": "summary.json", "layer": layer,
            "title": testcase.title if testcase else key,
            "description": reason,
        })
    (folder / "verdicts.json").write_text(json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n")


def _perform_background_resume(config, seconds=5):
    driver = create_driver(config)
    try:
        driver.activate_app(config.bundle_id)
        driver.execute_script("mobile: pressButton", {"name": "home"})
        time.sleep(seconds)
        driver.activate_app(config.bundle_id)
        time.sleep(1)
    finally:
        driver.quit()


def _terminate_app(config):
    _run(["xcrun", "devicectl", "device", "process", "terminate",
          "--device", config.udid, config.bundle_id], check=False)
    time.sleep(2)


def run_lifecycle_round(config):
    """Execute R3 as one causal sequence and compare values across captures."""
    folders = []
    try:
        first = capture(config, "LIFECYCLE-START")
        folders.append(first)
        time.sleep(10)
        continuous = capture(config, "LIFECYCLE-CONTINUOUS")
        folders.append(continuous)
        _perform_background_resume(config)
        background = capture(config, "LIFECYCLE-BACKGROUND")
        folders.append(background)
        _terminate_app(config)
        cold = capture(config, "LIFECYCLE-TERMINATED")
        folders.append(cold)
    except Exception as exc:
        failed_folder = getattr(exc, "evidence_folder", None)
        if failed_folder:
            folders.append(Path(failed_folder))
            _write_failed_verdicts(
                failed_folder, ROUND_DEFINITIONS["R3"].testcase_keys,
                f"iOS R3 lifecycle sequence stopped at an executed step: {type(exc).__name__}: {exc}",
            )
        raise

    users = [_decoded_user(folder) for folder in folders]
    def number(index, key):
        value = users[index].get(key)
        return value if type(value) in (int, float) else None
    first_session, continuous_session = number(0, "session_duration"), number(1, "session_duration")
    background_session, cold_session = number(2, "session_duration"), number(3, "session_duration")
    init_values = [number(index, "app_init_time") for index in range(4)]
    duration_values = [number(index, "app_duration") for index in range(4)]
    checks = {
        "session-duration-continuous": {
            "executed": True, "values": [first_session, continuous_session],
            "passed": first_session is not None and continuous_session is not None and continuous_session > first_session,
            "reason": "Continuous foreground session_duration must increase.",
        },
        "session-duration-background": {
            "executed": True, "values": [continuous_session, background_session],
            "passed": continuous_session is not None and background_session is not None and background_session > continuous_session,
            "reason": "session_duration must continue after Home and resume.",
        },
        "session-duration-termination": {
            "executed": True, "values": [background_session, cold_session],
            "passed": background_session is not None and cold_session is not None and cold_session < background_session,
            "reason": "session_duration must reset after process termination.",
        },
        "app-initialization-time": {
            "executed": True, "values": init_values,
            "passed": all(value is not None for value in init_values) and len(set(init_values[:3])) == 1 and init_values[3] != init_values[2],
            "reason": "app_init_time must remain stable in one process and renew after termination.",
        },
        "app-duration-today": {
            "executed": True, "values": duration_values,
            "passed": all(value is not None for value in duration_values) and duration_values == sorted(duration_values),
            "reason": "app_duration must be monotonic across foreground, background and process restart.",
        },
    }
    sequence = {"platform": "ios", "captures": [str(folder) for folder in folders], **checks}
    result_folder = Path(folders[-1])
    (result_folder / "ios-lifecycle-sequence.json").write_text(json.dumps(sequence, ensure_ascii=False, indent=2) + "\n")
    rows = [TC_DEFINITIONS[key].validate(result_folder) for key in ROUND_DEFINITIONS["R3"].testcase_keys]
    (result_folder / "verdicts.json").write_text(json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n")
    return folders


def _attribute(element, name):
    try:
        return element.get_attribute(name)
    except Exception:
        return None


def _setting_element(driver, label, element_type=None):
    queries = []
    if element_type:
        queries.append(("xpath", f"//{element_type}[@name={json.dumps(label)} or @label={json.dumps(label)}]"))
    queries.extend((("accessibility id", label), ("xpath", f"//*[@name={json.dumps(label)} or @label={json.dumps(label)}]")))
    return _first_element(driver, queries)


def _settings_home(driver):
    driver.terminate_app("com.apple.Preferences")
    time.sleep(.5)
    driver.activate_app("com.apple.Preferences")
    time.sleep(1)


def _settings_search_open(driver, query, result_label=None):
    """Open a native Settings result via its searchable, visible label."""
    _settings_home(driver)
    search = _first_element(driver, (("class name", "XCUIElementTypeSearchField"),))
    if search is None:
        raise CaptureError("iOS Settings Search field is unavailable")
    search.click()
    try:
        search.clear()
    except Exception:
        pass
    search.send_keys(query)
    time.sleep(2)
    target = _setting_element(driver, result_label or query)
    if target is None:
        candidates = driver.find_elements(
            "xpath", f"//*[contains(translate(@label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),{json.dumps((result_label or query).lower())})]",
        )
        target = next((item for item in candidates if item.is_displayed() and item.is_enabled()), None)
    if target is None:
        raise CaptureError(f"iOS Settings search did not expose {result_label or query!r}")
    target.click()
    time.sleep(1)


def _switch_state(element):
    return str(_attribute(element, "value") or "").strip().lower() in {"1", "true", "on"}


def _set_switch(driver, element, desired):
    before = _switch_state(element)
    if before != desired:
        element.click()
        time.sleep(.8)
    after = _switch_state(element)
    if after != desired:
        rect = element.rect
        driver.execute_script("mobile: tap", {
            "x": rect["x"] + rect["width"] / 2,
            "y": rect["y"] + rect["height"] / 2,
        })
        time.sleep(.8)
        after = _switch_state(element)
    if after != desired:
        raise CaptureError(f"iOS switch read-back is {after}, expected {desired}")
    return before, after


def _slider_fraction(element):
    value = str(_attribute(element, "value") or "").strip().replace("%", "")
    try:
        number = float(value)
    except ValueError as exc:
        raise CaptureError(f"Cannot read iOS slider value {value!r}") from exc
    return number / 100 if number > 1 else number


def _set_slider(driver, element, desired):
    before = _slider_fraction(element)
    rect = element.rect
    start_x = rect["x"] + max(2, min(rect["width"] - 2, rect["width"] * before))
    target_x = rect["x"] + max(2, min(rect["width"] - 2, rect["width"] * desired))
    y = rect["y"] + rect["height"] / 2
    driver.execute_script("mobile: dragFromToForDuration", {
        "duration": .8, "fromX": start_x, "fromY": y, "toX": target_x, "toY": y,
    })
    time.sleep(.8)
    after = _slider_fraction(element)
    tolerance = .03
    if abs(after - desired) > tolerance:
        raise CaptureError(f"iOS slider read-back is {after:.3f}, expected {desired:.3f}")
    return before, after


def _r5_open_control(driver, config, label):
    """Navigate and return (kind, control, desired); raises before any mutation."""
    if label in {"DISPLAY-DARK", "DISPLAY-LOW", "DISPLAY-HIGH"}:
        _settings_search_open(driver, "Display & Brightness")
        if label == "DISPLAY-DARK":
            return "choice", _setting_element(driver, "Dark"), "Dark"
        sliders = driver.find_elements("class name", "XCUIElementTypeSlider")
        if not sliders:
            raise CaptureError("Display & Brightness has no visible brightness slider")
        return "slider", sliders[0], 0.0 if label == "DISPLAY-LOW" else 1.0
    if label == "TEXT-MAX":
        _settings_search_open(driver, "Larger Text")
        sliders = driver.find_elements("class name", "XCUIElementTypeSlider")
        if not sliders:
            raise CaptureError("Larger Text has no visible text-size slider")
        return "slider", sliders[-1], 1.0
    if label == "LOW-POWER":
        _settings_search_open(driver, "Low Power Mode")
        control = _setting_element(driver, "Low Power Mode", "XCUIElementTypeSwitch")
        if control is None:
            raise CaptureError("Low Power Mode switch is unavailable")
        return "switch", control, True
    if label in {"AUDIO-MUTED", "AUDIO-HIGH"}:
        raise CaptureError(
            "iOS Settings does not expose current/max media output volume; "
            "hardware-button mutation cannot be independently read back or safely restored"
        )
    if label == "LOCATION-DENIED":
        _settings_search_open(driver, "Location Services")
        app = _setting_element(driver, "AppierAdsSwiftSample") or _setting_element(driver, "Random")
        if app is None:
            raise CaptureError("Sample App is absent from iOS Location Services")
        app.click(); time.sleep(.8)
        return "choice", _setting_element(driver, "Never"), "Never"
    if label == "PRIVACY-DENIED":
        _settings_home(driver)
        privacy = _setting_element(driver, "Privacy & Security") or _setting_element(driver, "Privacy")
        if privacy is None:
            for _ in range(5):
                driver.swipe(180, 650, 180, 250, 500)
                privacy = _setting_element(driver, "Privacy & Security") or _setting_element(driver, "Privacy")
                if privacy is not None:
                    break
        if privacy is None:
            raise CaptureError("native Settings does not expose Privacy & Security")
        privacy.click(); time.sleep(.8)
        tracking = _setting_element(driver, "Tracking")
        if tracking is None:
            raise CaptureError("native Privacy & Security does not expose Tracking")
        tracking.click(); time.sleep(.8)
        control = _setting_element(driver, config.bundle_id, "XCUIElementTypeSwitch")
        if control is None:
            control = _setting_element(driver, "AppierAdsSwiftSample", "XCUIElementTypeSwitch")
        if control is None:
            raise CaptureError(f"Tracking switch for {config.bundle_id} is unavailable")
        return "switch", control, False
    if label == "TIMEZONE-ALT":
        raise CaptureError("Automated iOS timezone mutation is intentionally unavailable until a deterministic city picker contract is reviewed")
    raise CaptureError(f"Unknown iOS R5 Scenario {label}")


def _mutate_ios_state(config, label):
    driver = create_driver(config, bundle_id="com.apple.Preferences")
    screenshot = Path(os.environ.get("IOS_SETTINGS_SCREENSHOT", "/tmp/laf2-ios-settings-state.png"))
    before_screenshot = Path(os.environ.get("IOS_SETTINGS_BEFORE_SCREENSHOT", "/tmp/laf2-ios-settings-before.png"))
    state_path = Path(os.environ.get("IOS_SETTINGS_STATE_FILE", "/tmp/laf2-ios-settings-state.json"))
    try:
        kind, control, desired = _r5_open_control(driver, config, label)
        driver.save_screenshot(str(before_screenshot))
        if kind == "switch":
            before, after = _set_switch(driver, control, desired)
        elif kind == "slider":
            before, after = _set_slider(driver, control, desired)
        elif kind == "choice":
            if control is None:
                raise CaptureError(f"iOS Settings choice {desired!r} is unavailable")
            if label == "DISPLAY-DARK":
                choices = ("Light", "Dark")
            else:
                choices = ("Never", "Ask Next Time Or When I Share", "While Using the App", "Always")
            before = next((name for name in choices if (
                (item := _setting_element(driver, name)) is not None
                and ("selected" in str(_attribute(item, "traits") or "").lower()
                     or str(_attribute(item, "value") or "").lower() in {"1", "selected", "true"})
            )), None)
            control.click(); time.sleep(.8)
            traits = str(_attribute(control, "traits") or "")
            value = str(_attribute(control, "value") or "")
            after = {"traits": traits, "value": value}
        else:
            raise CaptureError(f"Unsupported iOS control kind {kind}")
        driver.save_screenshot(str(screenshot))
        state = {
            "scenario": label, "automation": "Appium XCUITest native Settings UI",
            "confirmed_by_operator": False, "screenshot_saved": screenshot.is_file() and screenshot.stat().st_size > 0,
            "control_kind": kind, "before": before, "desired": desired, "after": after,
            "stages": {
                "before": {"value": before, "screenshot": "ios-settings-before.png"},
                "mutated": {"value": after, "screenshot": "ios-settings-state.png"},
                "restored": {"status": "PENDING"},
            },
            "att": {"authorization": "denied" if label == "PRIVACY-DENIED" else None},
            "captured_at": datetime.now().astimezone().isoformat(),
        }
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        if not state["screenshot_saved"]:
            raise CaptureError("iOS state changed but native Settings screenshot was not saved")
        return state
    finally:
        driver.quit()


def _restore_ios_state(config, label, state, evidence_folder=None):
    """Restore the exact readable baseline and verify it; unsupported restoration fails closed."""
    kind, before = state.get("control_kind"), state.get("before")
    if kind == "choice" and before is None:
        # Appearance/permission/volume do not expose a deterministic original
        # value through the selected control.  A later scenario must not run on
        # an unverified baseline.
        reason = f"{label} original state was not independently readable, so automatic restore cannot be claimed"
        if evidence_folder:
            state_file = Path(evidence_folder) / "ios-settings-state.json"
            document = _read_json(state_file) or dict(state)
            document.setdefault("stages", {})["restored"] = {
                "status": "FAILED", "value": None, "screenshot": None,
                "reason": reason, "captured_at": datetime.now().astimezone().isoformat(),
            }
            state_file.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        return False, reason
    driver = create_driver(config, bundle_id="com.apple.Preferences")
    try:
        current_kind, control, _desired = _r5_open_control(driver, config, label)
        if current_kind != kind:
            return False, f"{label} control changed from {kind} to {current_kind}"
        if kind == "switch":
            _set_switch(driver, control, bool(before))
            restored = _switch_state(control) == bool(before)
        elif kind == "slider":
            _set_slider(driver, control, float(before))
            restored = abs(_slider_fraction(control) - float(before)) <= .03
        elif kind == "choice":
            original = _setting_element(driver, str(before))
            if original is None:
                return False, f"Original iOS choice {before!r} is unavailable"
            original.click(); time.sleep(.8)
            restored = (
                "selected" in str(_attribute(original, "traits") or "").lower()
                or str(_attribute(original, "value") or "").lower() in {"1", "selected", "true"}
            )
        else:
            restored = False
        restored_screenshot = Path(os.environ.get("IOS_SETTINGS_RESTORED_SCREENSHOT", "/tmp/laf2-ios-settings-restored.png"))
        driver.save_screenshot(str(restored_screenshot))
        reason = "" if restored else f"{label} restore read-back did not match its original value"
        if evidence_folder:
            evidence_folder = Path(evidence_folder)
            if restored_screenshot.is_file() and restored_screenshot.stat().st_size:
                shutil.copy2(restored_screenshot, evidence_folder / "ios-settings-restored.png")
            state_file = evidence_folder / "ios-settings-state.json"
            document = _read_json(state_file) or dict(state)
            document.setdefault("stages", {})["restored"] = {
                "status": "VERIFIED" if restored else "FAILED",
                "value": before if restored else None,
                "screenshot": "ios-settings-restored.png" if restored_screenshot.is_file() else None,
                "reason": reason or None,
                "captured_at": datetime.now().astimezone().isoformat(),
            }
            state_file.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        return restored, reason
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        driver.quit()


def _record_blocked(config, round_name, label, keys, reason):
    folder = create_capture_folder(config, label)
    now = datetime.now().astimezone().isoformat()
    summary = {
        "result": "BLOCKED", "platform": "ios", "app_package": config.bundle_id,
        "test_mode": config.test_mode, "test_type": config.test_type,
        "test_cid": config.test_cid, "target_app_package": config.target_app_bundle_id,
        "test_round": round_name, "test_run_id": config.test_run_id,
        "test_run_started_at": config.test_run_started_at, "capture_name": label,
        "started_at": now, "finished_at": now, "device": device_evidence(config),
    }
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    rows = []
    for key in keys:
        row = blocked(key, reason).to_dict()
        testcase = TC_DEFINITIONS[key]
        row.update({"layer": "Signal", "title": testcase.title, "description": reason})
        rows.append(row)
    (folder / "verdicts.json").write_text(json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n")
    return folder


def run_r5_round(config):
    """Run independent iOS alternate-state Scenarios; one failure never cancels another."""
    folders = []
    restore_failed = False
    for index, (label, keys) in enumerate(R5_SCENARIOS):
        if config.selected_scenarios and label not in config.selected_scenarios:
            continue
        # REEN does not require the privacy-denied identity contract.
        if label == "PRIVACY-DENIED" and config.test_type != "aibid":
            continue
        if restore_failed:
            folders.append(_record_blocked(
                config, "R5", label, keys,
                "Not executed because the previous iOS Scenario did not restore its original state.",
            ))
            continue
        try:
            state = _mutate_ios_state(config, label)
        except Exception as exc:
            folders.append(_record_blocked(
                config, "R5", label, keys,
                f"iOS native Settings automation could not establish and read back this state: {type(exc).__name__}: {exc}",
            ))
            continue
        scenario_config = config
        if label == "PRIVACY-DENIED" and config.test_mode == "admob-mediation":
            scenario_config = replace(
                config, test_mode="standalone",
                tab_name=MODE_TABS["standalone"], trigger_label=MODE_TRIGGERS["standalone"],
            )
            print("[R5 PRIVACY-DENIED] safety override: capture exactly one Standalone request; no Mediation request is allowed after ATT denial")
        testcases = [TC_DEFINITIONS[key] for key in keys]
        required = tuple(item for testcase in testcases for item in testcase.evidence)
        try:
            folder = collect_evidence(scenario_config, required, lambda setup: capture(scenario_config, label, setup=setup))
            rows = [testcase.validate(folder) for testcase in testcases]
            (Path(folder) / "verdicts.json").write_text(json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n")
            folders.append(Path(folder))
        except Exception as exc:
            evidence_folder = getattr(exc, "evidence_folder", None)
            if evidence_folder:
                folders.append(Path(evidence_folder))
                _write_failed_verdicts(
                    evidence_folder, keys,
                    f"iOS R5 {label} failed after execution began: {type(exc).__name__}: {exc}",
                )
            print(f"[warn] R5 {label} failed independently: {exc}", file=sys.stderr)
        finally:
            evidence_folder = folders[-1] if folders else None
            restored, restore_reason = _restore_ios_state(config, label, state, evidence_folder)
            if not restored:
                restore_failed = True
                target = Path(folders[-1]) if folders else None
                if target and target.is_dir():
                    (target / "restore-error.txt").write_text(restore_reason + "\n")
                print(f"[warn] R5 {label} restore failed independently: {restore_reason}", file=sys.stderr)
    return folders


def _run_e2e_round(config, name, validators):
    folder = None
    try:
        folder = collect_evidence(config, ("bid",), lambda setup: capture(
            config, name, setup=setup, strategy="e2e",
        ))
    except Exception as exc:
        folder = getattr(exc, "evidence_folder", None)
        if folder and (Path(folder) / "summary.json").is_file():
            rows = [row for validator in validators for row in validator(folder)]
            (Path(folder) / "verdicts.json").write_text(json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n")
        raise
    rows = [row for validator in validators for row in validator(folder)]
    (Path(folder) / "verdicts.json").write_text(json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n")
    return [Path(folder)]


def run_round(config, name):
    if name == "R4":
        return run_ipv6_refresh_round(config)
    if name == "R3":
        return run_lifecycle_round(config)
    if name == "R5":
        return run_r5_round(config)
    if name == "E2E-STANDALONE":
        if config.test_mode != "standalone":
            raise CaptureError("E2E-STANDALONE requires TEST_MODE=standalone")
        return _run_e2e_round(config, name, (validate_baseline_e2e,))
    if name == "E2E-ADMOB":
        if config.test_mode != "admob-mediation":
            raise CaptureError("E2E-ADMOB requires TEST_MODE=admob-mediation")
        return _run_e2e_round(config, name, (validate_baseline_e2e, validate_admob_extensions))
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
                    f"iOS {name} reached the {config.max_attempts}-attempt limit without the target CID "
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
    round_parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="run only the named R5 Scenario; repeat to select more than one",
    )
    subparsers.add_parser("list-rounds", help="list declared rounds without touching a device")

    for target in (capture_parser, round_parser):
        target.add_argument("--bundle-id", default=_env("BUNDLE_ID"))
        target.add_argument("--test-mode", default=_env("TEST_MODE"))
        target.add_argument("--test-type", default=_env("TEST_TYPE"))
        target.add_argument("--test-cid", default=_env("TEST_CID"))
        target.add_argument("--target-app-bundle-id", default=_env("TARGET_APP_BUNDLE_ID"))
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
    selected_scenarios = tuple(
        str(item).strip().upper() for item in getattr(args, "scenario", ()) if str(item).strip()
    )
    if selected_scenarios:
        if args.name.strip().upper() != "R5":
            raise CaptureError("--scenario is only valid with round R5")
        known_scenarios = {label for label, _keys in R5_SCENARIOS}
        unknown_scenarios = sorted(set(selected_scenarios) - known_scenarios)
        if unknown_scenarios:
            raise CaptureError(
                "Unknown R5 Scenario(s): " + ", ".join(unknown_scenarios)
            )

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
        target_app_bundle_id=args.target_app_bundle_id.strip(),
        selected_scenarios=selected_scenarios,
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
        print("E2E-STANDALONE: " + ", ".join(BASELINE_E2E_TESTCASES))
        print("E2E-ADMOB: " + ", ".join((*BASELINE_E2E_TESTCASES, *ADMOB_E2E_EXTENSIONS)))
        return 0

    config = config_from_args(args)
    print(f"[device] {config.udid}")
    print(f"[app]    {config.bundle_id}")
    print(f"[mode]   {config.test_mode} ({config.tab_name})")
    print(f"[type]   {config.test_type}")
    print(f"[cid]    {config.test_cid or '(any request)'}")
    print(f"[round]  {config.test_round}")

    if args.command == "round" and args.name.strip().upper().startswith("E2E-"):
        ensure_e2e_proxy_ready()

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
