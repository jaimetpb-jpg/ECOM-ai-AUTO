"""
scaling/meta_ads.py — Meta Ads Automation

Launches and manages Meta (Facebook/Instagram) campaigns.
Only called after human gate approval in Slack.
Triggered when TikTok ROAS ≥ 2.5 sustained 7 days.
"""
import os
try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    httpx = None
    _HTTPX_AVAILABLE = False
import logging
from typing import Optional
from shared.constants import ROAS_SCALE_META
from shared.logging_utils import log_error, log_info

logger = logging.getLogger(__name__)

META_API_BASE = "https://graph.facebook.com/v21.0"


class MetaAdsManager:
    def __init__(self):
        self.access_token  = os.getenv("META_ACCESS_TOKEN")
        self.ad_account_id = os.getenv("META_AD_ACCOUNT_ID")

    async def launch_campaign(self, brand: dict, opportunity: dict, initial_budget: float = 200.0) -> Optional[str]:
        """
        Launch Meta campaign for a validated product.
        Must only be called AFTER human gate approval.
        """
        if not self.access_token:
            logger.warning("META_ACCESS_TOKEN not set — skipping Meta campaign")
            return None

        headers = {"Authorization": f"Bearer {self.access_token}"}
        campaign_name = f"SCALE_{brand.get('name', '')}_{opportunity.get('id', '')[:8]}"

        if not _HTTPX_AVAILABLE:
            raise RuntimeError("httpx not installed — run: pip install httpx")
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                # 1. Create campaign
                camp_resp = await client.post(
                    f"{META_API_BASE}/act_{self.ad_account_id}/campaigns",
                    headers=headers,
                    json={
                        "name": campaign_name,
                        "objective": "OUTCOME_SALES",
                        "status": "PAUSED",  # Start paused, operator activates
                        "special_ad_categories": [],
                        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                    }
                )
                camp_resp.raise_for_status()
                campaign_id = camp_resp.json().get("id")

                # 2. Create Ad Set (Advantage+ audience)
                adset_resp = await client.post(
                    f"{META_API_BASE}/act_{self.ad_account_id}/adsets",
                    headers=headers,
                    json={
                        "name": f"AdSet_{campaign_name}",
                        "campaign_id": campaign_id,
                        "billing_event": "IMPRESSIONS",
                        "daily_budget": int(initial_budget * 100),  # cents
                        "optimization_goal": "OFFSITE_CONVERSIONS",
                        "targeting": {
                            "geo_locations": {"countries": ["MX", "US", "CO", "AR"]},
                            "age_min": 18, "age_max": 55,
                        },
                        "status": "PAUSED",
                    }
                )
                adset_resp.raise_for_status()
                adset_id = adset_resp.json().get("id")

                log_info(logger, "meta_campaign_created",
                           campaign_id=campaign_id, adset_id=adset_id, name=campaign_name)
                return campaign_id

            except Exception as e:
                log_error(logger, "meta_campaign_failed", error=str(e))
                return None

    async def get_campaign_metrics(self, campaign_id: str) -> dict:
        """Fetch performance metrics for a running campaign."""
        if not self.access_token:
            return {}
        if not _HTTPX_AVAILABLE:
            raise RuntimeError("httpx not installed — run: pip install httpx")
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get(
                    f"{META_API_BASE}/{campaign_id}/insights",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    params={
                        "fields": "impressions,clicks,spend,conversions,ctr,cpc,roas,reach",
                        "date_preset": "last_7d",
                    }
                )
                resp.raise_for_status()
                data = resp.json().get("data", [{}])
                return data[0] if data else {}
            except Exception as e:
                log_error(logger, "meta_metrics_failed", campaign_id=campaign_id, error=str(e))
                return {}
