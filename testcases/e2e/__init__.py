"""E2E registries: shared S baseline plus integration-specific extensions."""

from .android_admob_mediation_extensions import TESTCASES as ANDROID_ADMOB_EXTENSIONS
from .android_e2e_baseline import TESTCASES as ANDROID_BASELINE
from .ios_admob_mediation_extensions import TESTCASES as IOS_ADMOB_EXTENSIONS
from .ios_e2e_baseline import TESTCASES as IOS_BASELINE


def _with_extensions(baseline, extensions):
    overlap = set(baseline) & set(extensions)
    if overlap:
        raise ValueError(f"E2E extensions duplicate baseline keys: {sorted(overlap)}")
    return {**baseline, **extensions}

REGISTRIES = {
    ("aos", "standalone"): ANDROID_BASELINE,
    ("aos", "admob-mediation"): _with_extensions(ANDROID_BASELINE, ANDROID_ADMOB_EXTENSIONS),
    ("ios", "standalone"): IOS_BASELINE,
    ("ios", "admob-mediation"): _with_extensions(IOS_BASELINE, IOS_ADMOB_EXTENSIONS),
}


def registry_for(platform, integration_mode):
    """Return S baseline, plus only the extensions required by the selected mode."""
    return REGISTRIES.get((platform, integration_mode), {})
