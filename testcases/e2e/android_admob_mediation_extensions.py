"""Android AdMob Mediation-only E2E extensions; S baseline is inherited."""

from .e2e_shared_contracts import E2ETestCase, definitions
from verdict import blocked

TESTCASES = definitions(
    E2ETestCase("admob-pubsetting", "AdMob Pubsetting Mediation Config", "Serving", "P0"),
    E2ETestCase("admob-gma-request", "AdMob GMA Request and Mediation Routing", "Serving", "P0"),
    E2ETestCase("admob-appier-ad-request", "Appier Adapter Ad Request", "Serving", "P0"),
    E2ETestCase("admob-impression", "AdMob Impression Reporting", "Tracking", "P0"),
    E2ETestCase("admob-fill-result", "Mediation Fill Result", "Tracking", "P2"),
    E2ETestCase("admob-click", "AdMob Click Reporting", "Tracking", "P0"),
)


def validate_bundle(_folder):
    """Return explicit gates until each mediation-only traffic validator is added."""
    rows = []
    for key, testcase in TESTCASES.items():
        row = blocked(
            key,
            "Mediation-only validator is not implemented yet; the shared S baseline is evaluated separately in the same Round",
        ).to_dict()
        row.update({
            "layer": "E2E",
            "title": testcase.title,
            "description": "Mediation-only extension; it does not replace the shared S baseline.",
        })
        rows.append(row)
    return rows
