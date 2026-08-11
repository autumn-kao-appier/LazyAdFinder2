"""Single source of truth for campaign-to-TestCase membership.

Add every new TestCase to exactly one or more sets in this file.  Catalog,
platform validators, report rendering, and execution planning must not declare
their own campaign applicability.
"""


SHARED_TESTCASES = frozenset({
    "dark-mode-enabled",
    "font-scale-maximum", "screen-brightness-minimum", "output-volume-muted",
    "battery-saver-enabled", "screen-brightness-maximum", "output-volume-maximum",
    "timezone-changed", "location-permission-denied", "advertising-id",
    "app-set-id", "installed-app-list", "in-app-purchase-history",
    "boot-timestamps", "sdk-version", "ram-total", "ram-available",
    "disk-total", "disk-free", "battery-level", "charging-status",
    "battery-saver", "screen-width", "screen-height", "screen-ppi",
    "pixel-ratio", "screen-brightness", "font-scale", "dark-mode",
    "gyroscope", "accelerometer", "tracking-allowed", "output-volume",
    "device-make", "device-model", "default-timezone", "default-language-iso",
    "default-language-bcp47", "keyboard-languages", "root-status",
    "emulator-detection", "ipv6-address", "ipv6-refresh-launch",
    "ipv6-refresh-wifi-switch", "ipv6-refresh-recovery", "ipv6-refresh-debounce",
    "ipv6-refresh-slow-network", "connection-type", "carrier", "mcc-mnc",
    "precise-gps-latitude", "precise-gps-longitude",
    "session-duration-continuous", "session-duration-background",
    "session-duration-termination", "last-foreground-times",
    "last-background-times", "impression-history", "vpn-status",
    "argus-sdk-version", "network-latency", "app-initialization-time",
    "app-duration-today", "connection-type-cellular", "force-gdpr-override",
    "coppa-applies", "standalone-sdk-init", "standalone-appier-ad-request",
    "standalone-creative-assets", "standalone-native-render",
    "standalone-impression", "standalone-click", "standalone-landing",
    "standalone-privacy", "standalone-install-attribution",
    "standalone-attribution-reconciliation", "admob-pubsetting",
    "admob-gma-request", "admob-appier-ad-request", "admob-impression",
    "admob-fill-result", "admob-click",
})

# Campaign-specific sets are intentionally separate even while empty.  A new
# TC is assigned here instead of adding flags to Catalog or validator files.
AIBID_ONLY_TESTCASES = frozenset({
    "advertising-id-opt-out",
    "tracking-denied",
})
REEN_STATIC_ONLY_TESTCASES = frozenset()
REEN_DYNAMIC_ONLY_TESTCASES = frozenset()


CAMPAIGN_TESTCASES = {
    "aibid": SHARED_TESTCASES | AIBID_ONLY_TESTCASES,
    "reen-static": SHARED_TESTCASES | REEN_STATIC_ONLY_TESTCASES,
    "reen-dynamic": SHARED_TESTCASES | REEN_DYNAMIC_ONLY_TESTCASES,
}


def supports(test_type, testcase_key):
    return str(testcase_key) in CAMPAIGN_TESTCASES.get(str(test_type).strip().lower(), frozenset())
