"""Platform- and integration-specific E2E TestCase registries."""

from .android_admob_mediation_e2e import TESTCASES as ANDROID_ADMOB_MEDIATION_E2E
from .android_standalone_e2e import TESTCASES as ANDROID_STANDALONE_E2E
from .ios_admob_mediation_e2e import TESTCASES as IOS_ADMOB_MEDIATION_E2E
from .ios_standalone_e2e import TESTCASES as IOS_STANDALONE_E2E

REGISTRIES = {
    ("aos", "standalone"): ANDROID_STANDALONE_E2E,
    ("aos", "admob-mediation"): ANDROID_ADMOB_MEDIATION_E2E,
    ("ios", "standalone"): IOS_STANDALONE_E2E,
    ("ios", "admob-mediation"): IOS_ADMOB_MEDIATION_E2E,
}


def registry_for(platform, integration_mode):
    """Return one exact implementation registry; validators never branch on mode."""
    return REGISTRIES.get((platform, integration_mode), {})
