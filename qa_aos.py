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
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from appium import webdriver
from appium.options.android.uiautomator2.base import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from evidence_aos import collect as collect_evidence, capture_ads_settings
from evidence_bundle import decoded_bid, finalize_bundle
from testcases.android_signal_testcases import (
    ROUND_DEFINITIONS, TC_DEFINITIONS, R5_PRIVACY_KEYS, R5_ALTERNATE_KEYS,
    R5_DISPLAY_AUDIO_HIGH_KEYS, R5_TIMEZONE_KEYS, R5_LOCATION_DENIED_KEYS,
)
from testcases.ipv6_refresh_testcases import TESTCASES as IPV6_TESTCASES
from testcases.ipv6_refresh_testcases import validate_sequence as validate_ipv6_sequence
from testcases.e2e.android_e2e_baseline import TESTCASES as BASELINE_E2E_TESTCASES
from testcases.e2e.android_e2e_baseline import validate_bundle as validate_baseline_e2e
from testcases.e2e.android_admob_mediation_extensions import TESTCASES as ADMOB_E2E_EXTENSIONS
from testcases.e2e.android_admob_mediation_extensions import validate_bundle as validate_admob_extensions
from verdict import blocked


APPIUM_URL = "http://127.0.0.1:4723"
CHARLES_PORT = 8888
MITMDUMP_PORT = 8081
MITMDUMP_LOG = Path("/tmp/lazyadfinder2_mitmdump.log")
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
NET_PROBE_RESPONSE_FILE = Path("/tmp/appier_net_probe_response.json")
ADMOB_RAW_FILES = (
    Path("/tmp/admob_pubsetting_request.bin"),
    Path("/tmp/admob_pubsetting_response.bin"),
    Path("/tmp/admob_gma_request.bin"),
    Path("/tmp/admob_gma_response.bin"),
)
LOGCAT_FILE = Path("/tmp/appier_aos_logcat.txt")
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
class ScenarioPlan:
    label: str
    testcase_keys: tuple
    decision: str = "RUN"
    reason: str = ""
    checks: object = None


@dataclass(frozen=True)
class ExecutionPlan:
    round_name: str
    test_mode: str
    test_type: str
    scenarios: tuple


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


def _restore_setting(udid, namespace, key, value):
    if value in {"", "null", None}:
        adb(udid, "shell", "settings", "delete", namespace, key, check=False)
    else:
        adb(udid, "shell", "settings", "put", namespace, key, str(value), check=False)


def lock_portrait(config):
    """Lock Android to portrait before any automation and verify the active viewport."""
    state = {
        "accelerometer_rotation": adb(
            config.udid, "shell", "settings", "get", "system", "accelerometer_rotation",
            check=False,
        ).strip(),
        "user_rotation": adb(
            config.udid, "shell", "settings", "get", "system", "user_rotation",
            check=False,
        ).strip(),
    }
    try:
        adb(config.udid, "shell", "settings", "put", "system", "accelerometer_rotation", "0")
        adb(config.udid, "shell", "settings", "put", "system", "user_rotation", "0")
        time.sleep(0.8)
        current = adb(config.udid, "shell", "dumpsys", "input", check=False)
        active_viewports = re.findall(
            r"Viewport INTERNAL:.*?orientation=(\d).*?isActive=\[1\]",
            current,
        )
        if not active_viewports or any(value != "0" for value in active_viewports):
            raise CaptureError(
                "Automation requires portrait orientation (ROTATION_0); "
                f"active viewport rotations={active_viewports or ['unknown']}"
            )
        print("[orientation] portrait locked (ROTATION_0)")
        return state
    except Exception:
        restore_orientation(config, state)
        raise


def restore_orientation(config, state):
    if not state:
        return
    _restore_setting(
        config.udid, "system", "user_rotation", state.get("user_rotation")
    )
    _restore_setting(
        config.udid, "system", "accelerometer_rotation", state.get("accelerometer_rotation")
    )
    print("[orientation] restored original rotation settings")


def require_device_unlocked(config):
    """Fail before automation when Android is waiting for a secure unlock."""
    adb(config.udid, "shell", "input", "keyevent", "KEYCODE_WAKEUP", check=False)
    adb(config.udid, "shell", "wm", "dismiss-keyguard", check=False)
    time.sleep(0.5)
    trust = adb(config.udid, "shell", "dumpsys", "trust", check=False)
    policy = adb(config.udid, "shell", "dumpsys", "window", "policy", check=False)
    device_locked = bool(re.search(r"\bdeviceLocked=1\b", trust))
    keyguard_showing = bool(re.search(r"\bshowing=true\b", policy))
    input_restricted = bool(re.search(r"\binputRestricted=true\b", policy))
    if device_locked or (keyguard_showing and input_restricted):
        raise CaptureError(
            "Device requires manual unlock: Android is showing the Enter PIN "
            "screen. Unlock the device before starting Automation."
        )
    print("[device preflight] unlocked; no Enter PIN screen")


def keep_screen_awake(config):
    """Prevent long unattended Rounds from losing Settings behind screen-off."""
    original_timeout = adb(
        config.udid, "shell", "settings", "get", "system", "screen_off_timeout",
        check=False,
    ).strip()
    original_stay_on = adb(
        config.udid, "shell", "settings", "get", "global", "stay_on_while_plugged_in",
        check=False,
    ).strip()
    try:
        stay_on = int(original_stay_on) | 3
    except (TypeError, ValueError):
        stay_on = 3
    adb(config.udid, "shell", "input", "keyevent", "KEYCODE_WAKEUP", check=False)
    adb(config.udid, "shell", "wm", "dismiss-keyguard", check=False)
    adb(
        config.udid, "shell", "settings", "put", "global",
        "stay_on_while_plugged_in", str(stay_on),
    )
    adb(config.udid, "shell", "settings", "put", "system", "screen_off_timeout", "1800000")
    time.sleep(2)
    print("[screen] awake while plugged in; timeout temporarily set to 30 minutes")
    return {
        "screen_off_timeout": original_timeout,
        "stay_on_while_plugged_in": original_stay_on,
    }


def restore_screen_timeout(config, original):
    if not original:
        return
    _restore_setting(
        config.udid, "system", "screen_off_timeout",
        original.get("screen_off_timeout"),
    )
    _restore_setting(
        config.udid, "global", "stay_on_while_plugged_in",
        original.get("stay_on_while_plugged_in"),
    )
    print("[screen] restored original timeout and stay-awake setting")


def media_volume_state(config):
    raw = adb(
        config.udid, "shell", "cmd", "media_session", "volume",
        "--stream", "3", "--get",
    )
    match = re.search(r"volume is (\d+) in range \[0\.\.(\d+)\]", raw)
    if not match:
        raise CaptureError(f"cannot read Android media volume current/max: {raw!r}")
    return tuple(map(int, match.groups()))


def set_media_volume(config, target):
    """Use real volume key events; Pixel ignores media_session --set."""
    current, maximum = media_volume_state(config)
    target = max(0, min(int(target), maximum))
    for _ in range(30):
        if current == target or (target == maximum and current >= maximum):
            return current, maximum
        key = "KEYCODE_VOLUME_UP" if current < target else "KEYCODE_VOLUME_DOWN"
        adb(config.udid, "shell", "input", "keyevent", key)
        time.sleep(0.15)
        updated, maximum = media_volume_state(config)
        if updated == current:
            continue
        current = updated
    if current != target:
        raise CaptureError(f"media volume did not reach {target}; current={current}, max={maximum}")
    return current, maximum


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
    driver = webdriver.Remote(APPIUM_URL, options=options)
    if str(driver.orientation).upper() != "PORTRAIT":
        driver.orientation = "PORTRAIT"
    return driver


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


def _visible_url(driver):
    for resource_id in (
        "com.android.chrome:id/url_bar",
        "com.google.android.webview:id/url_bar",
    ):
        for element in driver.find_elements(AppiumBy.ID, resource_id):
            value = element.get_attribute("text") or element.get_attribute("content-desc")
            if value:
                return value
    return ""


def _screen_state(driver):
    return {
        "package": driver.current_package,
        "activity": driver.current_activity,
        "url": _visible_url(driver),
    }


def _capture_e2e_interactions(driver, config, folder):
    """Exercise Privacy first, then CTA, preserving human-readable evidence."""
    folder = Path(folder)
    result = {
        "sequence": ["rendered-ad", "privacy", "return-to-ad", "click", "landing"],
        "ad_state": _screen_state(driver),
        "privacy": {"attempted": False, "opened": False},
        "click": {"attempted": False, "opened": False},
        "errors": [],
    }
    recording_path = "/sdcard/laf2-e2e-interactions.mp4"
    recorder = subprocess.Popen(
        _adb_command(config.udid, "shell", "screenrecord", "--time-limit", "120", recording_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        driver.save_screenshot(str(folder / "ad-before-interactions.png"))
        privacy = driver.find_element(
            AppiumBy.ID,
            f"{config.app_package}:id/native_privacy_information_icon_image",
        )
        result["privacy"]["attempted"] = True
        privacy.click()
        time.sleep(4)
        result["privacy"]["destination"] = _screen_state(driver)
        result["privacy"]["opened"] = (
            driver.current_package != result["ad_state"]["package"]
            or driver.current_activity != result["ad_state"]["activity"]
        )
        driver.save_screenshot(str(folder / "privacy-landing.png"))

        driver.back()
        time.sleep(2)
        result["returned_to_ad"] = driver.current_package == config.app_package
        driver.save_screenshot(str(folder / "ad-before-click.png"))

        cta = driver.find_element(
            AppiumBy.ID,
            f"{config.app_package}:id/native_cta",
        )
        result["click"]["attempted"] = True
        cta.click()
        time.sleep(5)
        result["click"]["destination"] = _screen_state(driver)
        result["click"]["opened"] = driver.current_package != config.app_package
        driver.save_screenshot(str(folder / "click-landing.png"))
        time.sleep(2)
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        try:
            result["failure_state"] = _screen_state(driver)
            driver.save_screenshot(str(folder / "interaction-failure.png"))
        except Exception as evidence_exc:
            result["errors"].append(f"evidence: {type(evidence_exc).__name__}: {evidence_exc}")
    finally:
        recorder.terminate()
        try:
            recorder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            recorder.kill()
            recorder.wait()
        adb(config.udid, "pull", recording_path, str(folder / "e2e-interactions.mp4"), check=False)
        adb(config.udid, "shell", "rm", recording_path, check=False)
        if EVENTS_FILE.is_file():
            shutil.copyfile(EVENTS_FILE, folder / "proxy-events.jsonl")
        (folder / "e2e-interactions.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
    return result


AD_REQUEST_RE = re.compile(
    r"(?:\[AdRequestJSON\]|Ad request body:)\s*(\{.*\})\s*$"
)
LOADED_RE = re.compile(r"onAdLoaded\(\)")
NO_BID_RE = re.compile(r"onAdNoBid\(\)")
IMPRESSION_URL_RE = re.compile(r"Requesting impression tracker:\s*(\S+)")


def scan_logcat(offset=0):
    request = None
    status = None
    identity = None
    try:
        with LOGCAT_FILE.open("rb") as stream:
            stream.seek(offset)
            lines = stream.read().decode(errors="replace").splitlines()
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
        match = IMPRESSION_URL_RE.search(line)
        if match:
            values = parse_qs(urlsplit(match.group(1)).query)
            identity = {
                key: values[key][0]
                for key in ("bidobjid", "cid", "crid")
                if values.get(key) and values[key][0]
            }
    return request, status, identity


def observe_bid(logcat_offset=0):
    request = _read_json(BID_FILE)
    status = _read_text(BID_STATUS_FILE) or None
    identity = _read_json(IMPRESSION_FILE)
    source = "proxy" if request is not None else None

    log_request, log_status, log_identity = scan_logcat(logcat_offset)
    if request is None and log_request is not None:
        request = log_request
        source = "logcat"
    status = status or log_status
    identity = identity or log_identity
    return request, status, identity, source


def wait_for_bid(config, proxy_only=False, logcat_offset=0):
    deadline = time.monotonic() + config.bid_timeout
    while time.monotonic() < deadline:
        if proxy_only:
            request = _read_json(BID_FILE)
            status = _read_text(BID_STATUS_FILE) or None
            identity = _read_json(IMPRESSION_FILE)
            source = "proxy" if request is not None else None
        else:
            request, status, identity, source = observe_bid(logcat_offset)
        if eligible(config, request, status, identity):
            return request, status, identity, source
        time.sleep(0.2)
    if proxy_only:
        request = _read_json(BID_FILE)
        return request, _read_text(BID_STATUS_FILE) or None, _read_json(IMPRESSION_FILE), "proxy" if request is not None else None
    return observe_bid(logcat_offset)


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


def record_skip(config, round_name, scenario, reason, checks=None, testcase_keys=()):
    """Record an unmet precondition without manufacturing a verdict."""
    folder = create_capture_folder(config, f"{round_name}-{scenario}-SKIPPED")
    recorded_at = datetime.now().astimezone().isoformat()
    document = {
        "status": "SKIPPED",
        "round": round_name,
        "scenario": scenario,
        "reason": reason,
        "checks": checks or {},
        "recorded_at": recorded_at,
        "testcases": list(testcase_keys),
        "policy": "No device mutation, capture, or verdict was produced.",
    }
    (folder / "round-skip.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    )
    (folder / "summary.json").write_text(json.dumps({
        "result": "SKIPPED",
        "platform": "aos",
        "test_mode": config.test_mode,
        "test_type": config.test_type,
        "test_cid": config.test_cid,
        "test_round": round_name,
        "capture_name": scenario,
        "started_at": recorded_at,
        "finished_at": recorded_at,
        "skipped_testcases": list(testcase_keys),
        "skip_reason": reason,
        "device": {},
    }, ensure_ascii=False, indent=2) + "\n")
    print(f"[{round_name} {scenario}] SKIPPED: {reason}")
    return folder


def ipv6_preflight(config):
    """Check Android IPv6 prerequisites without relying on ICMP reachability."""
    addresses = adb(
        config.udid, "shell", "ip", "-6", "addr", "show", "scope", "global",
        check=False,
    )
    routes = adb(
        config.udid, "shell", "ip", "-6", "route", "show", "default",
        check=False,
    )
    global_addresses = re.findall(r"\binet6\s+([^\s/]+)/\d+", addresses)
    usable = [
        address for address in global_addresses
        if address != "::" and not address.lower().startswith("fe80:")
    ]
    checks = {
        "global_ipv6_addresses": usable,
        "default_ipv6_route": routes.strip() or None,
    }
    if not usable:
        return False, "Android has no usable global IPv6 address on the current network", checks
    if not routes.strip():
        return False, "Android has no default IPv6 route on the current network", checks
    return True, "IPv6 address and route are available", checks


def privacy_scenario_preflight(config):
    """Privacy-denied identity validation is an AIBID-only Scenario."""
    campaign_type = config.test_type.strip().lower()
    if campaign_type in {"reen-static", "reen-dynamic"}:
        return False, (
            "REEN cannot validate the tracking-denied identity flow because "
            "the advertising identifier is unavailable"
        ), {
            "test_type": campaign_type,
            "required_identity": "advertising identifier",
        }
    return True, "AIBID Privacy Scenario is required", {"test_type": campaign_type}


def location_permission_preflight(config):
    package = adb(config.udid, "shell", "dumpsys", "package", config.app_package, check=False)
    permissions = [
        permission for permission in (
            "android.permission.ACCESS_FINE_LOCATION",
            "android.permission.ACCESS_COARSE_LOCATION",
        ) if permission in package
    ]
    available = bool(permissions)
    return available, (
        "Sample App declares a location permission"
        if available else "Sample App declares no location permission to revoke"
    ), {"declared_permissions": permissions}


def _tcp_listening(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _listener_command(port):
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        text=True,
        capture_output=True,
    )
    pids = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
    commands = []
    for pid in pids:
        command = subprocess.run(
            ["ps", "-p", pid, "-o", "command="], text=True, capture_output=True
        ).stdout.strip()
        if command:
            commands.append(command)
    return commands


def _verify_charles_external_proxy():
    candidates = (
        Path.home() / "Library/Preferences/com.xk72.charles.config",
        Path.home() / "Library/Application Support/Charles/profiles/default.cfg.xml",
    )
    for path in candidates:
        if not path.is_file():
            continue
        document = path.read_text(errors="replace")
        block_match = re.search(
            r"<externalProxyConfiguration>(.*?)</externalProxyConfiguration>",
            document,
            re.S,
        )
        block = block_match.group(1) if block_match else ""
        valid = all(
            re.search(
                rf"<string>{scheme}</string>.*?<active>true</active>.*?"
                rf"<host>127\.0\.0\.1</host>.*?<port>{MITMDUMP_PORT}</port>",
                block,
                re.S,
            )
            for scheme in ("http", "https")
        )
        if valid:
            return str(path)
    raise CaptureError(
        "Proxy preflight failed: Charles HTTP/HTTPS External Proxy must be active at 127.0.0.1:8081"
    )


def _mac_lan_address():
    """Return the Mac address used for the default route without sending data."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def ensure_proxy_capture_ready(config):
    """Make the Charles -> mitmdump evidence path a hard precondition."""
    if not _tcp_listening("127.0.0.1", CHARLES_PORT):
        raise CaptureError(
            f"Proxy preflight failed: Charles is not listening on :{CHARLES_PORT}"
        )
    charles_commands = _listener_command(CHARLES_PORT)
    if not any("Charles.app/Contents/MacOS/Charles" in command for command in charles_commands):
        raise CaptureError(
            f"Proxy preflight failed: :{CHARLES_PORT} is not owned by Charles ({charles_commands or 'unknown owner'})"
        )
    charles_config = _verify_charles_external_proxy()

    started_mitmdump = False
    if not _tcp_listening("127.0.0.1", MITMDUMP_PORT):
        executable = shutil.which("mitmdump")
        if not executable:
            raise CaptureError(
                "Proxy preflight failed: mitmdump is not installed or is not on PATH"
            )
        addon = Path(__file__).with_name("mitmdump_addon.py").resolve()
        log_stream = MITMDUMP_LOG.open("a")
        try:
            subprocess.Popen(
                [executable, "-s", str(addon), "--listen-port", str(MITMDUMP_PORT)],
                cwd=addon.parent,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_stream.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _tcp_listening("127.0.0.1", MITMDUMP_PORT):
            time.sleep(0.2)
        if not _tcp_listening("127.0.0.1", MITMDUMP_PORT):
            raise CaptureError(
                f"Proxy preflight failed: mitmdump could not listen on :{MITMDUMP_PORT}; "
                f"see {MITMDUMP_LOG}"
            )
        started_mitmdump = True

    addon = str(Path(__file__).with_name("mitmdump_addon.py").resolve())
    mitmdump_commands = _listener_command(MITMDUMP_PORT)
    if not any("mitmdump" in command and addon in command for command in mitmdump_commands):
        raise CaptureError(
            f"Proxy preflight failed: :{MITMDUMP_PORT} is not using {addon} ({mitmdump_commands or 'unknown owner'})"
        )

    phone_proxy = adb(
        config.udid, "shell", "settings", "get", "global", "http_proxy", check=False
    ).strip()
    proxy_missing = not phone_proxy or phone_proxy.lower() in {"null", ":0", "0.0.0.0:0"}
    if proxy_missing or not phone_proxy.endswith(f":{CHARLES_PORT}"):
        current_host = phone_proxy.rsplit(":", 1)[0] if ":" in phone_proxy else ""
        proxy_host = current_host if current_host not in {"", "0.0.0.0"} else _mac_lan_address()
        if not proxy_host:
            raise CaptureError(
                "Proxy preflight failed: Android proxy is missing and the Mac LAN address could not be determined"
            )
        expected_proxy = f"{proxy_host}:{CHARLES_PORT}"
        adb(config.udid, "shell", "settings", "put", "global", "http_proxy", expected_proxy)
        phone_proxy = adb(
            config.udid, "shell", "settings", "get", "global", "http_proxy", check=False
        ).strip()
        if phone_proxy != expected_proxy:
            raise CaptureError(
                f"Proxy preflight failed: could not configure Android proxy as {expected_proxy!r}; got {phone_proxy!r}"
            )
        print(f"[proxy preflight] configured Android proxy: {expected_proxy}")

    result = {
        "android_proxy": phone_proxy,
        "charles": f"127.0.0.1:{CHARLES_PORT}",
        "mitmdump": f"127.0.0.1:{MITMDUMP_PORT}",
        "mitmdump_started": started_mitmdump,
        "charles_config": charles_config,
    }
    print(
        "[proxy preflight] READY: "
        f"Android {phone_proxy} -> Charles :{CHARLES_PORT} -> mitmdump :{MITMDUMP_PORT}"
    )
    return result


def resolve_execution_plan(args):
    """Resolve Round, Scenarios, and TestCases without touching a device."""
    mode = args.test_mode.strip().lower()
    test_type = args.test_type.strip().lower()
    if mode not in MODE_TABS:
        raise CaptureError(f"Unsupported TEST_MODE={mode!r}")
    if test_type not in {"aibid", "reen-static", "reen-dynamic"}:
        raise CaptureError(f"Unsupported TEST_TYPE={test_type!r}")
    if args.command == "capture":
        return ExecutionPlan(
            round_name="MANUAL",
            test_mode=mode,
            test_type=test_type,
            scenarios=(ScenarioPlan(args.capture_name, ()),),
        )

    name = args.name.strip().upper()
    if name in ROUND_DEFINITIONS and name != "R5":
        definition = ROUND_DEFINITIONS[name]
        scenarios = (ScenarioPlan(definition.capture_name, tuple(definition.testcase_keys)),)
    elif name == "R4":
        scenarios = (ScenarioPlan("IPV6-REFRESH", tuple(IPV6_TESTCASES)),)
    elif name == "R5":
        scenarios = (
            ScenarioPlan("PRIVACY-DENIED", R5_PRIVACY_KEYS),
            ScenarioPlan("ALTERNATE-DEVICE-STATE", R5_ALTERNATE_KEYS),
            ScenarioPlan("DISPLAY-AUDIO-HIGH", R5_DISPLAY_AUDIO_HIGH_KEYS),
            ScenarioPlan("TIMEZONE-CHANGED", R5_TIMEZONE_KEYS),
            ScenarioPlan("LOCATION-PERMISSION-DENIED", R5_LOCATION_DENIED_KEYS),
        )
    elif name == "E2E-STANDALONE":
        if mode != "standalone":
            raise CaptureError("E2E-STANDALONE requires TEST_MODE=standalone")
        scenarios = (ScenarioPlan(name, tuple(BASELINE_E2E_TESTCASES)),)
    elif name == "E2E-ADMOB":
        if mode != "admob-mediation":
            raise CaptureError("E2E-ADMOB requires TEST_MODE=admob-mediation")
        scenarios = (ScenarioPlan(name, tuple(BASELINE_E2E_TESTCASES) + tuple(ADMOB_E2E_EXTENSIONS)),)
    else:
        available = sorted(set(ROUND_DEFINITIONS) | {"R4", "E2E-STANDALONE", "E2E-ADMOB"})
        raise CaptureError(f"Round {name!r} is not defined; available rounds: {', '.join(available)}")
    known_testcases = set(TC_DEFINITIONS) | set(IPV6_TESTCASES) | set(BASELINE_E2E_TESTCASES) | set(ADMOB_E2E_EXTENSIONS)
    unknown = sorted({key for scenario in scenarios for key in scenario.testcase_keys if key not in known_testcases})
    if unknown:
        raise CaptureError(
            f"Execution Plan {name} references unknown TestCases: {', '.join(unknown)}"
        )
    return ExecutionPlan(name, mode, test_type, tuple(scenarios))


def preflight_execution_plan(plan, config):
    """Finalize RUN/SKIP decisions using read-only device probes."""
    resolved = []
    for scenario in plan.scenarios:
        probe = None
        if plan.round_name == "R4" and scenario.label == "IPV6-REFRESH":
            probe = ipv6_preflight
        elif plan.round_name == "R5" and scenario.label == "PRIVACY-DENIED":
            probe = privacy_scenario_preflight
        elif plan.round_name == "R5" and scenario.label == "LOCATION-PERMISSION-DENIED":
            probe = location_permission_preflight
        if probe is None:
            resolved.append(scenario)
            continue
        ready, reason, checks = probe(config)
        resolved.append(replace(
            scenario,
            decision="RUN" if ready else "SKIP",
            reason=reason,
            checks=checks,
        ))
    return replace(plan, scenarios=tuple(resolved))


def print_execution_plan(plan, config):
    print("\n[execution plan]")
    print(f"  platform: AOS")
    print(f"  mode:     {plan.test_mode}")
    print(f"  type:     {plan.test_type}")
    print(f"  CID:      {config.test_cid or '(any request)'}")
    print(f"  Round:    {plan.round_name}")
    for scenario in plan.scenarios:
        print(f"  [{scenario.decision}] {scenario.label}")
        if scenario.reason:
            print(f"         reason: {scenario.reason}")
        print(f"         TC ({len(scenario.testcase_keys)}): {', '.join(scenario.testcase_keys) or '(raw capture)'}")


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
    logcat_offset = LOGCAT_FILE.stat().st_size if LOGCAT_FILE.exists() else 0
    if not tap_placement(driver, config):
        raise CaptureError(f"R3 {label}: cannot tap {config.trigger_text!r}")
    request, status, identity, source = wait_for_bid(config, logcat_offset=logcat_offset)
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
    termination_deadline = time.monotonic() + 10
    terminated_pid_confirmed = False
    while time.monotonic() < termination_deadline:
        if _app_pid(config) is None:
            terminated_pid_confirmed = True
            break
        time.sleep(0.5)
    if not terminated_pid_confirmed:
        print(
            "[R3 termination] App process remained alive after the Recents task was dismissed; "
            "continuing so termination-dependent TestCases can compare the actual PID and payload",
            file=sys.stderr,
        )
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


def capture(config, capture_name="MANUAL", setup=None, warmup_ads=0, strategy="standard", settle_delay=0):
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
                logcat_offset = LOGCAT_FILE.stat().st_size if LOGCAT_FILE.exists() else 0
                if not tap_placement(driver, config):
                    driver.activate_app(config.app_package)
                    time.sleep(config.retry_delay)
                    continue

                request, status, identity, source = wait_for_bid(
                    config,
                    proxy_only=strategy == "e2e",
                    logcat_offset=logcat_offset,
                )
                if eligible(config, request, status, identity):
                    if completed_warmups < warmup_ads:
                        if not identity or not identity.get("bidobjid"):
                            print("[warmup] bid received without a confirmed bidobjid; retrying")
                            time.sleep(config.retry_delay)
                            continue
                        warmup_impression = identity
                        completed_warmups += 1
                        print(f"[warmup] confirmed ad {completed_warmups}/{warmup_ads}; continuing in the same app session")
                        time.sleep(config.retry_delay)
                        continue
                    if warmup_impression is not None:
                        previous_bidobjid = warmup_impression.get("bidobjid")
                        current_bidobjid = identity.get("bidobjid") if identity else None
                        if not current_bidobjid or current_bidobjid == previous_bidobjid:
                            print(
                                "[retry] waiting for a distinct second bidobjid "
                                f"(previous={previous_bidobjid!r}, current={current_bidobjid!r})"
                            )
                            time.sleep(config.retry_delay)
                            continue
                    if settle_delay:
                        print(f"[capture] waiting {settle_delay:g}s for trailing E2E events")
                        time.sleep(settle_delay)
                    if strategy == "e2e":
                        print("[e2e] privacy → return → click → landing")
                        _capture_e2e_interactions(driver, config, folder)
                        print(f"[e2e] waiting {max(settle_delay, 2):g}s for trailing interaction events")
                        time.sleep(max(settle_delay, 2))
                    save_evidence(
                        driver, config, folder, started_at, request, status, identity, source
                    )
                    if warmup_impression is not None:
                        (folder / "previous-impression.json").write_text(
                            json.dumps(warmup_impression, ensure_ascii=False, indent=2) + "\n"
                        )
                        (folder / "current-impression.json").write_text(
                            json.dumps(identity, ensure_ascii=False, indent=2) + "\n"
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
        driver.activate_app(config.app_package)
        if find_visible_text(driver, config.trigger_text) is None:
            select_tab(driver, config.tab_text, config.trigger_text)
        print(f"[R4] wait {wait_seconds}s for the Appier IPv6 probe before {name}")
        time.sleep(wait_seconds)
        if not tap_placement(driver, config):
            raise CaptureError(f"cannot tap {config.trigger_text!r}")
        request, status, identity, source = wait_for_bid(config, proxy_only=True)
        if not eligible(config, request, status, identity):
            raise CaptureError("no eligible ad request after the network transition")
        save_evidence(driver, config, folder, started_at, request, status, identity, source)
        _return_to_placement(driver, config)
        return folder
    except Exception as exc:
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
            failed_step=name,
            error=str(exc),
            capture_log=LOGCAT_FILE,
        )
        return folder


def run_ipv6_refresh_round(config):
    """Run AOS R4 in one Appium session while the operator changes networks."""
    folders = []
    context = {
        "platform": "aos",
        "same_appium_session": True,
        "slow_network_confirmed": False,
    }
    driver = None
    try:
        with LogcatRecorder(config.udid):
            adb(config.udid, "shell", "am", "force-stop", config.app_package)
            driver = create_driver(config)
            driver.activate_app(config.app_package)
            folders.append(_r4_capture_in_session(driver, config, "AOS-NET-01", 10))

            preflight = validate_ipv6_sequence(folders, context)
            if preflight and preflight[0].get("status") == "BLOCKED":
                print("[R4] IPv6 preflight passed, but the executed Appier probe was unavailable; stop remaining transitions")
            elif _r4_checkpoint(
                "AOS-NET-02",
                "App 保持開啟。請從 network A 切到 network B（另一個 Wi-Fi／hotspot），確認已連線。",
            ):
                folders.append(_r4_capture_in_session(driver, config, "AOS-NET-02", 10))
                if _r4_checkpoint(
                    "AOS-NET-03",
                    "App 保持開啟。關閉 Wi-Fi，等待 10 秒，再開啟並重新連線；確認可上網。",
                ):
                    folders.append(_r4_capture_in_session(driver, config, "AOS-NET-03", 10))
                    if _r4_checkpoint(
                        "AOS-NET-04",
                        "快速切換 network A → B → A → B；最後停在 network B。",
                    ):
                        folders.append(_r4_capture_in_session(driver, config, "AOS-NET-04", 10))
                        if _r4_checkpoint(
                            "AOS-NET-05",
                            "啟用 1～2 秒延遲的 Charles throttle，保持 App 開啟並切換 Wi-Fi。",
                        ):
                            context["slow_network_confirmed"] = True
                            folders.append(_r4_capture_in_session(driver, config, "AOS-NET-05", 15))
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
        "platform": "aos",
        "same_appium_session": True,
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
    return [result_folder]


def _run_e2e_round(config, capture_name, validators):
    """Always finalize E2E verdicts, including an interrupted capture."""
    folder = None
    try:
        folder = collect_evidence(
            config,
            ("bid",),
            lambda setup: capture(
                config,
                capture_name=capture_name,
                setup=setup,
                settle_delay=2,
                strategy="e2e",
            ),
        )
    except Exception as exc:
        folder = getattr(exc, "evidence_folder", None)
        if folder is not None and (Path(folder) / "summary.json").is_file():
            rows = []
            for validator in validators:
                rows.extend(validator(folder))
            (Path(folder) / "verdicts.json").write_text(
                json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n"
            )
        error = exc if isinstance(exc, CaptureError) else CaptureError(str(exc))
        error.evidence_folder = folder
        raise error from exc

    rows = []
    for validator in validators:
        rows.extend(validator(folder))
    (Path(folder) / "verdicts.json").write_text(
        json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n"
    )
    return [folder]


def run_round(config, plan):
    name = plan.round_name
    if name == "R4":
        scenario = plan.scenarios[0]
        if scenario.decision == "SKIP":
            return [record_skip(config, name, scenario.label, scenario.reason, scenario.checks, scenario.testcase_keys)]
        return run_ipv6_refresh_round(config)
    if name == "E2E-STANDALONE":
        if config.test_mode != "standalone":
            raise CaptureError("E2E-STANDALONE requires TEST_MODE=standalone")
        return _run_e2e_round(
            config, "E2E-STANDALONE", (validate_baseline_e2e,),
        )
    if name == "E2E-ADMOB":
        if config.test_mode != "admob-mediation":
            raise CaptureError("E2E-ADMOB requires TEST_MODE=admob-mediation")
        return _run_e2e_round(
            config,
            "E2E-ADMOB",
            (validate_baseline_e2e, validate_admob_extensions),
        )
    if name == "R5":
        folders = []
        round_errors = []

        scenario_plans = {scenario.label: scenario for scenario in plan.scenarios}

        def run_scenario(label, keys, mutate, restore):
            testcases = [TC_DEFINITIONS[key] for key in keys]
            required = tuple(evidence for testcase in testcases for evidence in testcase.evidence)
            if label == "ALTERNATE-DEVICE-STATE":
                # Pixel's Battery usage Activity may stay blank after the
                # Display/Quick Settings capture. All providers observe the
                # same already-mutated state, so capture Battery first.
                priority = {"battery-status": 0, "display-status": 1, "volume-status": 2, "bid": 3}
                required = tuple(sorted(required, key=lambda item: priority.get(item, 10)))
            scenario = scenario_plans[label]
            if scenario.decision == "SKIP":
                folders.append(record_skip(config, "R5", label, scenario.reason, scenario.checks, scenario.testcase_keys))
                return
            phase = "state mutation"
            scenario_error = None
            scenario_folder = None
            try:
                mutate()
                phase = "Evidence capture"
                folder = collect_evidence(
                    config, required,
                    lambda setup: capture(config, capture_name=label, setup=setup),
                )
                scenario_folder = Path(folder)
                phase = "TestCase validation"
                rows = []
                validator_errors = []
                for testcase in testcases:
                    try:
                        rows.append(testcase.validate(folder))
                    except Exception as exc:
                        row = {
                            "tc": testcase.key,
                            "status": "FAILED",
                            "reason": f"Validator error after execution: {exc}",
                            "expected": "Validator completes and compares captured Evidence",
                            "actual": f"{type(exc).__name__}: {exc}",
                            "evidence": "bid_decoded.json and captured Evidence artifacts",
                        }
                        row.update({"layer": "Signal", "title": testcase.title, "description": testcase.description})
                        rows.append(row)
                        validator_errors.append(f"{testcase.key}: {exc}")
                (folder / "verdicts.json").write_text(json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n")
                folders.append(folder)
                if validator_errors:
                    scenario_error = CaptureError("; ".join(validator_errors))
            except Exception as exc:
                scenario_error = exc
                evidence_folder = getattr(exc, "evidence_folder", None)
                if evidence_folder is not None:
                    scenario_folder = Path(evidence_folder)
                    rows = []
                    for testcase in testcases:
                        row = blocked(testcase.key, f"R5 {label} failed at {phase}: {exc}").to_dict()
                        row.update({"layer": "Signal", "title": testcase.title, "description": testcase.description})
                        rows.append(row)
                    (scenario_folder / "verdicts.json").write_text(json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n")
                    if scenario_folder not in folders:
                        folders.append(scenario_folder)
            finally:
                try:
                    restore()
                except Exception as restore_exc:
                    print(f"[R5 {label}] restore failed: {restore_exc}", file=sys.stderr)
                    if scenario_folder is None:
                        scenario_folder = create_capture_folder(config, f"{label}-RESTORE-FAILED")
                        now = datetime.now().astimezone().isoformat()
                        finalize_bundle(
                            scenario_folder,
                            driver=None,
                            platform="aos",
                            config=config,
                            device={},
                            started_at=now,
                            result="INTERRUPTED",
                            failed_step=f"R5 {label} restore",
                            error=str(restore_exc),
                        )
                        rows = []
                        for testcase in testcases:
                            row = blocked(testcase.key, f"R5 {label} restore failed: {restore_exc}").to_dict()
                            row.update({"layer": "Signal", "title": testcase.title, "description": testcase.description})
                            rows.append(row)
                        (scenario_folder / "verdicts.json").write_text(
                            json.dumps({"verdicts": rows}, ensure_ascii=False, indent=2) + "\n"
                        )
                        folders.append(scenario_folder)
                    (scenario_folder / "restore-error.txt").write_text(str(restore_exc) + "\n")
                    round_errors.append(f"{label} restore: {restore_exc}")
            if scenario_error is not None:
                print(f"[R5 {label}] failed: {scenario_error}", file=sys.stderr)
                round_errors.append(f"{label}: {scenario_error}")

        def privacy_mutate():
            print("[R5 Privacy] Opt out will be enabled by direct Ads Settings evidence capture")

        def privacy_restore():
            print("[R5 Privacy] restoring tracking-allowed baseline")
            capture_ads_settings(config)

        original = {}
        def alternate_mutate():
            original.update({
                "dark": adb(config.udid, "shell", "cmd", "uimode", "night").strip(),
                "font": adb(config.udid, "shell", "settings", "get", "system", "font_scale").strip(),
                "brightness": adb(config.udid, "shell", "settings", "get", "system", "screen_brightness").strip(),
                "low_power": adb(config.udid, "shell", "settings", "get", "global", "low_power").strip(),
            })
            original["volume"] = str(media_volume_state(config)[0])
            adb(config.udid, "shell", "cmd", "uimode", "night", "yes")
            adb(config.udid, "shell", "settings", "put", "system", "font_scale", "1.5")
            adb(config.udid, "shell", "settings", "put", "system", "screen_brightness", "0")
            set_media_volume(config, 0)
            adb(config.udid, "shell", "dumpsys", "battery", "unplug")
            original["battery_simulated"] = True
            adb(config.udid, "shell", "settings", "put", "global", "low_power", "1")

        def alternate_restore():
            if not original:
                return
            if "dark" in original:
                dark = "yes" if original["dark"].lower().endswith("yes") else "no"
                adb(config.udid, "shell", "cmd", "uimode", "night", dark, check=False)
            if "font" in original:
                adb(config.udid, "shell", "settings", "put", "system", "font_scale", original["font"], check=False)
            if "brightness" in original:
                adb(config.udid, "shell", "settings", "put", "system", "screen_brightness", original["brightness"], check=False)
            if "volume" in original:
                try:
                    set_media_volume(config, int(original["volume"]))
                except Exception as exc:
                    print(f"[R5 Alternate] volume restore failed: {exc}", file=sys.stderr)
            if original.get("battery_simulated"):
                adb(config.udid, "shell", "dumpsys", "battery", "reset", check=False)
            if "low_power" in original:
                adb(config.udid, "shell", "settings", "put", "global", "low_power", original["low_power"], check=False)
            print("[R5 Alternate] restored original display/audio/power state")

        high_original = {}
        def display_audio_high_mutate():
            high_original["brightness"] = adb(config.udid, "shell", "settings", "get", "system", "screen_brightness").strip()
            current, maximum = media_volume_state(config)
            high_original["volume"] = str(current)
            adb(config.udid, "shell", "settings", "put", "system", "screen_brightness", "255")
            set_media_volume(config, maximum)

        def display_audio_high_restore():
            if "brightness" in high_original:
                adb(config.udid, "shell", "settings", "put", "system", "screen_brightness", high_original["brightness"], check=False)
            if "volume" in high_original:
                try:
                    set_media_volume(config, int(high_original["volume"]))
                except Exception as exc:
                    print(f"[R5 Display/Audio High] volume restore failed: {exc}", file=sys.stderr)
            print("[R5 Display/Audio High] restored original brightness and volume")

        timezone_original = {}
        def timezone_mutate():
            timezone_original["timezone"] = adb(config.udid, "shell", "getprop", "persist.sys.timezone").strip() or "Asia/Taipei"
            adb(config.udid, "shell", "cmd", "alarm", "set-timezone", "America/New_York")

        def timezone_restore():
            if timezone_original.get("timezone"):
                adb(config.udid, "shell", "cmd", "alarm", "set-timezone", timezone_original["timezone"], check=False)
            print("[R5 Timezone] restored original timezone")

        location_original = {}
        def location_denied_mutate():
            permission = adb(
                config.udid, "shell", "cmd", "package", "check-permission",
                "android.permission.ACCESS_FINE_LOCATION", config.app_package, "0", check=False,
            ).strip().lower()
            location_original["fine_granted"] = "granted" in permission
            coarse = adb(
                config.udid, "shell", "cmd", "package", "check-permission",
                "android.permission.ACCESS_COARSE_LOCATION", config.app_package, "0", check=False,
            ).strip().lower()
            location_original["coarse_granted"] = "granted" in coarse
            adb(config.udid, "shell", "pm", "revoke", config.app_package, "android.permission.ACCESS_FINE_LOCATION", check=False)
            adb(config.udid, "shell", "pm", "revoke", config.app_package, "android.permission.ACCESS_COARSE_LOCATION", check=False)

        def location_denied_restore():
            if location_original.get("coarse_granted"):
                adb(config.udid, "shell", "pm", "grant", config.app_package, "android.permission.ACCESS_COARSE_LOCATION", check=False)
            if location_original.get("fine_granted"):
                adb(config.udid, "shell", "pm", "grant", config.app_package, "android.permission.ACCESS_FINE_LOCATION", check=False)
            print("[R5 Location] restored original location permissions")

        run_scenario("PRIVACY-DENIED", R5_PRIVACY_KEYS, privacy_mutate, privacy_restore)
        run_scenario("ALTERNATE-DEVICE-STATE", R5_ALTERNATE_KEYS, alternate_mutate, alternate_restore)
        run_scenario("DISPLAY-AUDIO-HIGH", R5_DISPLAY_AUDIO_HIGH_KEYS, display_audio_high_mutate, display_audio_high_restore)
        run_scenario("TIMEZONE-CHANGED", R5_TIMEZONE_KEYS, timezone_mutate, timezone_restore)
        run_scenario("LOCATION-PERMISSION-DENIED", R5_LOCATION_DENIED_KEYS, location_denied_mutate, location_denied_restore)
        if round_errors:
            error = CaptureError("R5 completed with errors: " + " | ".join(round_errors))
            error.evidence_folders = tuple(folders)
            error.evidence_folder = folders[-1] if folders else None
            raise error
        return folders
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
                row = {
                    "tc": testcase.key,
                    "status": "FAILED",
                    "reason": f"Validator error after execution: {exc}",
                    "expected": "Validator completes and compares captured Evidence",
                    "actual": f"{type(exc).__name__}: {exc}",
                    "evidence": "bid_decoded.json and captured Evidence artifacts",
                }
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


def publish_completed_round(evidence_dir, folders, automation_started_at=None):
    """Publish only after this Round's finalized verdict files are on disk."""
    folders = [Path(folder) for folder in folders if folder is not None]
    if not folders:
        print("[publish] skipped; this Round produced no Evidence folder", file=sys.stderr)
        return None
    completed = [folder for folder in folders if (folder / "verdicts.json").is_file()]
    skipped = [folder for folder in folders if (folder / "round-skip.json").is_file()]
    missing = [folder for folder in folders if folder not in completed and folder not in skipped]
    if missing:
        joined = ", ".join(str(folder) for folder in missing)
        print(f"[publish] skipped; verdicts.json is not finalized: {joined}", file=sys.stderr)
        return None
    if not completed and skipped:
        print(f"[publish] publishing {len(skipped)} skipped Scenario(s) as the latest unexecuted state")
    if skipped:
        print(f"[publish] {len(skipped)} Scenario(s) skipped; publishing {len(completed)} completed result set(s)")
    if _env("AUTO_PUBLISH", "1") == "0":
        print("[publish] AUTO_PUBLISH=0; skipped")
        return None
    summary_paths = []
    if automation_started_at:
        for folder in folders:
            summary_path = folder / "summary.json"
            if not summary_path.is_file():
                continue
            summary = json.loads(summary_path.read_text())
            summary["automation_started_at"] = automation_started_at
            summary.pop("automation_finished_at", None)
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
            )
            summary_paths.append(summary_path)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parent / "page.py"),
                "--evidence",
                str(evidence_dir),
                "--publish",
            ],
            check=True,
        )
        return result
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"[warn] Round completed, but report publishing failed: {exc}",
            file=sys.stderr,
        )
        return None
    finally:
        automation_finished_at = datetime.now().astimezone().isoformat()
        for summary_path in summary_paths:
            summary = json.loads(summary_path.read_text())
            summary["automation_finished_at"] = automation_finished_at
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
            )


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


def config_from_args(args, plan):
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
        test_round=_safe_label(plan.round_name, "MANUAL", 24),
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
    automation_started_at = datetime.now().astimezone().isoformat()
    args = build_parser().parse_args(argv)
    if args.command == "list-rounds":
        if not ROUND_DEFINITIONS:
            print("No rounds defined.")
            return 0
        for name, definition in sorted(ROUND_DEFINITIONS.items()):
            tc_ids = ", ".join(definition.testcase_keys)
            print(f"{name}: {definition.capture_name} [{tc_ids}]")
        print("R4: IPv6 network refresh [" + ", ".join(IPV6_TESTCASES) + "]")
        print("E2E-STANDALONE: S baseline [" + ", ".join(BASELINE_E2E_TESTCASES) + "]")
        print("E2E-ADMOB: S baseline + M extensions [" + ", ".join(ADMOB_E2E_EXTENSIONS) + "]")
        return 0

    plan = resolve_execution_plan(args)
    config = config_from_args(args, plan)
    plan = preflight_execution_plan(plan, config)
    print(f"[device] {config.udid}")
    print(f"[app]    {config.app_package}/{config.app_activity}")
    print(f"[mode]   {config.test_mode} ({config.tab_text})")
    print(f"[type]   {config.test_type}")
    print(f"[cid]    {config.test_cid or '(any request)'}")
    print_execution_plan(plan, config)
    sys.stdout.flush()
    if any(scenario.decision == "RUN" for scenario in plan.scenarios):
        require_device_unlocked(config)
        ensure_proxy_capture_ready(config)

    if args.command == "round" and all(scenario.decision == "SKIP" for scenario in plan.scenarios):
        folders = run_round(config, plan)
        publish_completed_round(config.evidence_dir, folders, automation_started_at)
        return 0

    screen_timeout = keep_screen_awake(config)
    orientation_state = None
    try:
        orientation_state = lock_portrait(config)
        if args.command == "capture":
            capture(config, capture_name=args.capture_name)
        else:
            try:
                folders = run_round(config, plan)
            except Exception as exc:
                evidence_folders = list(getattr(exc, "evidence_folders", ()))
                evidence_folder = getattr(exc, "evidence_folder", None)
                if not evidence_folders and evidence_folder is not None:
                    evidence_folders = [evidence_folder]
                publish_completed_round(
                    config.evidence_dir,
                    evidence_folders,
                    automation_started_at,
                )
                raise
            else:
                publish_completed_round(config.evidence_dir, folders, automation_started_at)
    finally:
        restore_orientation(config, orientation_state)
        restore_screen_timeout(config, screen_timeout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CaptureError, OSError, subprocess.SubprocessError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)
