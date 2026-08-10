"""Campaign capabilities shared by planning, validation, and reporting."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CampaignProfile:
    key: str
    privacy_denied_identity: bool
    landing_contract: str
    mmp_click_action: str
    attribution_action: str


CAMPAIGN_PROFILES = {
    "aibid": CampaignProfile(
        key="aibid",
        privacy_denied_identity=True,
        landing_contract="store-or-install-destination",
        mmp_click_action="install-click",
        attribution_action="install",
    ),
    "reen-static": CampaignProfile(
        key="reen-static",
        privacy_denied_identity=False,
        landing_contract="target-app-deeplink",
        mmp_click_action="re-engagement-click",
        attribution_action="re-engagement",
    ),
    "reen-dynamic": CampaignProfile(
        key="reen-dynamic",
        privacy_denied_identity=False,
        landing_contract="target-app-deeplink",
        mmp_click_action="re-engagement-click",
        attribution_action="re-engagement",
    ),
}


def campaign_profile(test_type):
    key = str(test_type or "").strip().lower()
    try:
        return CAMPAIGN_PROFILES[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported campaign type: {test_type!r}") from exc

