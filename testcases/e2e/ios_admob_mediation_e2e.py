"""iOS AdMob Mediation E2E definitions."""

from .e2e_shared_contracts import E2ETestCase, definitions

TESTCASES = definitions(
    E2ETestCase("admob-sdk-init", "SDK Initialization", "Serving", "P0"),
    E2ETestCase("admob-pubsetting", "AdMob Pubsetting Mediation Config", "Serving", "P0"),
    E2ETestCase("admob-gma-request", "AdMob GMA Request and Mediation Routing", "Serving", "P0"),
    E2ETestCase("admob-appier-ad-request", "Appier Adapter Ad Request", "Serving", "P0"),
    E2ETestCase("admob-creative-render", "Mediation Creative Assets and Rendering", "Serving", "P0"),
    E2ETestCase("admob-impression", "AdMob and Appier Impression Tracking", "Tracking", "P0"),
    E2ETestCase("admob-fill-result", "Mediation Fill Result", "Tracking", "P2"),
    E2ETestCase("admob-click", "AdMob and Appier Click Tracking", "Tracking", "P0"),
    E2ETestCase("admob-landing-privacy", "Mediation Landing and Privacy", "Tracking", "P1"),
)
