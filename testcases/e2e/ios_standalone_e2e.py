"""iOS Appier Standalone E2E definitions."""

from .e2e_shared_contracts import E2ETestCase, definitions

TESTCASES = definitions(
    E2ETestCase("standalone-sdk-init", "SDK Initialization", "Serving", "P0"),
    E2ETestCase("standalone-appier-ad-request", "Appier Direct Ad Request", "Serving", "P0"),
    E2ETestCase("standalone-creative-assets", "Creative Asset Loading", "Serving", "P1"),
    E2ETestCase("standalone-native-render", "Native Ad Rendering", "Serving", "P0"),
    E2ETestCase("standalone-impression", "Appier Impression Tracking", "Tracking", "P0"),
    E2ETestCase("standalone-click", "Appier Click Tracking", "Tracking", "P0"),
    E2ETestCase("standalone-landing", "Landing Behavior", "Tracking", "P1"),
    E2ETestCase("standalone-privacy", "Privacy Information", "Tracking", "P2"),
    E2ETestCase("standalone-install-attribution", "AIBID Install Attribution", "Attribution", "P2", ("aibid",)),
    E2ETestCase("standalone-attribution-reconciliation", "Backend Attribution Reconciliation", "Attribution", "P2", ("aibid",)),
)
