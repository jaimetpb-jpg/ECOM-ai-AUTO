"""
validation/creative_generator.py — TikTok-First Validation Pipeline

$50 TikTok test to validate product-market fit before any branding investment.

With V4.0 additions:
  1. Organic Pre-test (FREE): post organic TikTok first
     - If engagement ≥ 5% → proceed to $50 test
     - If engagement < 2% → change hook, don't spend
  2. Hook selection via Hook Intelligence Engine (best category for niche)
  3. 3 UGC-style hooks + Flux.1 product images
  4. $50 TikTok Ads campaign (48-72h)
  5. ROAS evaluation with Fail-Fast and ROAS rules
"""

import os
import asyncio
try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    httpx = None
    _HTTPX_AVAILABLE = False
try:
    import replicate
    _REPLICATE_AVAILABLE = True
except ImportError:
    replicate = None
    _REPLICATE_AVAILABLE = False
import logging
from typing import Optional
from shared.llm_router import LLMRouter
from shared.logging_utils import log_info, log_warning, log_error
from shared.constants import (
    LLM_TIER_CREATIVE, LLM_TIER_STRATEGIC,
    ROAS_KILL_THRESHOLD_1, ROAS_KILL_THRESHOLD_2,
    SPEND_KILL_1, SPEND_KILL_2, ROAS_VALIDATED, SPEND_VALIDATED,
    TIKTOK_TEST_BUDGET, ORGANIC_ENGAGEMENT_GREEN, ORGANIC_ENGAGEMENT_RED,
    FAILFAST_CAP_USD
)
from intelligence.hook_engine import HookIntelligenceEngine
from shared.supabase_client import SupabaseClient
from shared.slack_notifier import SlackNotifier

logger = logging.getLogger(__name__)


class TikTokValidationPipeline:
    """
    Runs the full TikTok-first validation for a product opportunity.

    Flow:
      1. Organic pre-test (V4.0 — no spend)
      2. Hook selection via Hook Intelligence
      3. Generate hooks (GPT-4o Mini)
      4. Generate product images (Flux.1 via Replicate)
      5. Launch $50 TikTok campaign
      6. Monitor 48-72h
      7. Apply ROAS decision rules
    """

    def __init__(
        self,
        llm_router: Optional[LLMRouter] = None,
        db: Optional[SupabaseClient] = None,
        slack: Optional[SlackNotifier] = None,
    ):
        self.router     = llm_router or LLMRouter()
        self.db         = db or SupabaseClient()
        self.slack      = slack or SlackNotifier()
        self.hook_engine = HookIntelligenceEngine(llm_router=self.router, db=self.db)
        self.tiktok_token = os.getenv("TIKTOK_ACCESS_TOKEN")
        self.advertiser_id = os.getenv("TIKTOK_ADVERTISER_ID")

    async def run_validation(self, opportunity: dict) -> dict:
        """
        Full validation pipeline for an opportunity.
        Returns: {status, roas, spend, decision, next_action}
        """
        product_name  = opportunity.get("name", "Product")
        niche         = opportunity.get("niche", "general")
        opportunity_id = opportunity.get("id")
        tenant_id     = opportunity.get("tenant_id")

        log_info(logger, "validation_started", product=product_name)

        # Step 1: Generate creative assets
        hooks  = await self._generate_hooks(product_name, niche, opportunity)
        images = await self._generate_product_images(product_name, niche)

        # Step 2: Organic pre-test (V4.0 — free, saves $40/failed test)
        organic_engagement = await self._organic_pretest(product_name, niche, hooks, images)
        log_info(logger, "organic_pretest_result",
                    product=product_name, engagement=organic_engagement)

        if organic_engagement < ORGANIC_ENGAGEMENT_RED:
            log_info(logger, "organic_pretest_failed_refresh_hook", product=product_name)
            self.slack.notify_alert(
                f"🔄 Organic Pre-test FAILED for *{product_name}*\n"
                f"Engagement: {organic_engagement:.1%} < {ORGANIC_ENGAGEMENT_RED:.0%}\n"
                f"Refreshing hooks before $50 spend..."
            )
            # Regenerate hooks with different category
            hooks = await self._generate_hooks(product_name, niche, opportunity, fallback=True)
            organic_engagement = await self._organic_pretest(product_name, niche, hooks, images)

        if organic_engagement < ORGANIC_ENGAGEMENT_RED:
            log_warning(logger, "organic_pretest_double_failed", product=product_name)
            self.db.update_opportunity_status(opportunity_id, "killed",
                                              {"kill_reason": "organic_pretest_failed_twice"})
            return {"status": "killed", "reason": "organic_pretest_failed_twice",
                    "spend": 0.0, "roas": 0.0}

        # Step 3: Launch $50 TikTok campaign
        if organic_engagement >= ORGANIC_ENGAGEMENT_GREEN:
            log_info(logger, "organic_pretest_passed_launching_paid", product=product_name, engagement=organic_engagement)
        else:
            log_info(logger, "organic_pretest_borderline_proceeding", product=product_name, engagement=organic_engagement)

        campaign_id = await self._launch_tiktok_campaign(
            product_name, niche, hooks, images, opportunity_id, tenant_id
        )

        if not campaign_id:
            return {"status": "error", "reason": "campaign_launch_failed", "spend": 0.0, "roas": 0.0}

        # Step 4: Wait 48h and evaluate (in production, n8n handles the wait)
        log_info(logger, "tiktok_campaign_launched", campaign_id=campaign_id, waiting_48h=True)
        return {
            "status": "testing",
            "campaign_id": campaign_id,
            "opportunity_id": opportunity_id,
            "hooks_generated": len(hooks),
            "images_generated": len(images),
            "organic_engagement": organic_engagement,
            "next_action": "Monitor in n8n monitoring_workflow.json — evaluate at 48h"
        }

    async def evaluate_roas(self, campaign: dict) -> dict:
        """
        Evaluate ROAS from a running campaign and apply decision rules.
        Called by n8n monitoring workflow after 48-72h.
        """
        roas    = campaign.get("roas", 0.0)
        spend   = campaign.get("spend_usd", 0.0)
        name    = campaign.get("name", "Product")
        platform = campaign.get("platform", "tiktok")

        # Apply ROAS rules (immutable)
        if roas < ROAS_KILL_THRESHOLD_1 and spend >= SPEND_KILL_1:
            decision = "KILL"
            next_action = "next_product"
        elif roas < ROAS_KILL_THRESHOLD_2 and spend >= SPEND_KILL_2:
            decision = "KILL"
            next_action = "next_product"
        elif roas >= ROAS_VALIDATED and spend >= SPEND_VALIDATED:
            decision = "VALIDATED"
            next_action = "niche_swarm_then_branding"
        elif spend < SPEND_KILL_1:
            decision = "WAIT"
            next_action = "continue_monitoring"
        else:
            decision = "HOLD"
            next_action = "continue_monitoring"

        self.slack.notify_roas_decision(
            product=name, roas=roas, spend=spend, action=decision, platform=platform
        )
        log_info(logger, "roas_evaluated", product=name, roas=roas, spend=spend, decision=decision)

        return {"decision": decision, "roas": roas, "spend": spend, "next_action": next_action}

    async def _generate_hooks(self, product_name: str, niche: str, opportunity: dict, fallback: bool = False) -> list[dict]:
        """Generate 3 hooks using Hook Intelligence Engine."""
        pain_points = opportunity.get("raw_data", {}).get("pain_points", [])
        hooks = await self.hook_engine.generate_hooks(
            product_name=product_name,
            niche=niche,
            product_description=opportunity.get("raw_data", {}).get("description", ""),
            pain_points=pain_points,
            n_hooks=3,
        )
        # Save hooks to DB
        for hook in hooks:
            hook_data = {**hook, "tenant_id": opportunity.get("tenant_id")}
            saved = self.db.save_hook(hook_data)
            hook["id"] = saved.get("id")
        return hooks

    async def _generate_product_images(self, product_name: str, niche: str) -> list[str]:
        """Generate 3 product images using Flux.1 Dev via Replicate."""
        prompts = [
            f"Professional product photo of {product_name}, white background, high-resolution, e-commerce style",
            f"Lifestyle photo of {product_name} in {niche} context, natural lighting, aspirational",
            f"Before and after comparison showing {product_name} solving a common {niche} problem",
        ]
        image_urls = []
        for prompt in prompts:
            try:
                if not _REPLICATE_AVAILABLE:
                    logger.warning("replicate not installed — image generation skipped")
                    break
                output = replicate.run(
                    "black-forest-labs/flux-dev",
                    input={"prompt": prompt, "num_outputs": 1, "aspect_ratio": "1:1"}
                )
                if output:
                    image_urls.append(output[0] if isinstance(output, list) else str(output))
            except Exception as e:
                log_warning(logger, "flux_generation_failed", error=str(e))
        log_info(logger, "product_images_generated", product=product_name, count=len(image_urls))
        return image_urls

    async def _organic_pretest(self, product_name: str, niche: str,
                                hooks: list[dict], images: list[str]) -> float:
        """
        V4.0: Organic pre-test before spending $50.
        Posts a TikTok video via TikTok Content Posting API, waits 2 hours,
        then reads engagement metrics (views, likes, comments, shares).
        Returns engagement_rate (likes+comments+shares / views), or 0.0 on failure.

        API docs: https://developers.tiktok.com/doc/content-posting-api-get-started
        Required env vars: TIKTOK_ACCESS_TOKEN, TIKTOK_ADVERTISER_ID
        Required OAuth scope: video.upload, video.publish
        """
        if not self.tiktok_token:
            log_warning(logger, "organic_pretest_skipped",
                        reason="TIKTOK_ACCESS_TOKEN not set — skipping organic gate",
                        product=product_name)
            return 0.0

        if not _HTTPX_AVAILABLE:
            log_warning(logger, "organic_pretest_skipped",
                        reason="httpx not installed", product=product_name)
            return 0.0

        # Pick the best hook text and first image for the organic post
        hook_text = hooks[0].get("text", product_name) if hooks else product_name
        cover_url = images[0] if images else None

        # Guard: TikTok API requires a valid video/image URL — skip call if none available
        if not cover_url:
            log_warning(logger, "organic_pretest_skipped_no_image",
                        product_name=product_name, tenant_id=tenant_id)
            return 0.0

        headers = {
            "Authorization": f"Bearer {self.tiktok_token}",
            "Content-Type": "application/json",
        }

        video_id: Optional[str] = None

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                # ── Step 1: Initialize video upload ───────────────────────
                init_payload: dict = {
                    "post_info": {
                        "title": f"{hook_text[:100]} #{niche.replace(' ', '')}",
                        "privacy_level": "PUBLIC_TO_EVERYONE",
                        "disable_duet": False,
                        "disable_comment": False,
                        "disable_stitch": False,
                    },
                    "source_info": {
                        "source": "PULL_FROM_URL",
                        "video_url": cover_url,  # URL de video/imagen producto
                    },
                }
                init_resp = await client.post(
                    "https://open.tiktokapis.com/v2/post/publish/video/init/",
                    headers=headers,
                    json=init_payload,
                )
                init_resp.raise_for_status()
                init_data = init_resp.json()

                if init_data.get("error", {}).get("code") != "ok":
                    error_msg = init_data.get("error", {}).get("message", "unknown")
                    log_warning(logger, "organic_pretest_init_failed",
                                product=product_name, error=error_msg)
                    return 0.0

                publish_id = init_data.get("data", {}).get("publish_id")
                if not publish_id:
                    log_warning(logger, "organic_pretest_no_publish_id", product=product_name)
                    return 0.0

                log_info(logger, "organic_post_published",
                         product=product_name, publish_id=publish_id)

                # ── Step 2: Poll for video_id (max 60s) ───────────────────
                for attempt in range(12):
                    await asyncio.sleep(5)
                    status_resp = await client.post(
                        "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
                        headers=headers,
                        json={"publish_id": publish_id},
                    )
                    status_resp.raise_for_status()
                    status_data = status_resp.json()
                    status = status_data.get("data", {}).get("status", "")
                    if status == "PUBLISH_COMPLETE":
                        video_id = status_data.get("data", {}).get("publicaly_available_post_id", [None])[0]
                        break
                    if status in ("FAILED", "PUBLISH_FAILED"):
                        log_warning(logger, "organic_post_failed",
                                    product=product_name, status=status)
                        return 0.0

                if not video_id:
                    log_warning(logger, "organic_pretest_video_id_missing",
                                product=product_name, publish_id=publish_id)
                    return 0.0

                # ── Step 3: Wait 2 hours for organic reach ─────────────────
                log_info(logger, "organic_pretest_waiting",
                         product=product_name, video_id=video_id,
                         note="Waiting 2h for organic reach before reading metrics")
                await asyncio.sleep(7200)  # 2 hours

                # ── Step 4: Read engagement metrics ───────────────────────
                metrics_resp = await client.get(
                    "https://open.tiktokapis.com/v2/video/query/",
                    headers=headers,
                    params={
                        "fields": "id,view_count,like_count,comment_count,share_count",
                    },
                    json={"filters": {"video_ids": [video_id]}},
                )
                metrics_resp.raise_for_status()
                metrics_data = metrics_resp.json()

                video_info = metrics_data.get("data", {}).get("videos", [{}])[0]
                views    = int(video_info.get("view_count",   0))
                likes    = int(video_info.get("like_count",   0))
                comments = int(video_info.get("comment_count", 0))
                shares   = int(video_info.get("share_count",  0))

                if views == 0:
                    log_warning(logger, "organic_pretest_zero_views",
                                product=product_name, video_id=video_id)
                    return 0.0

                engagement_rate = (likes + comments + shares) / views
                log_info(logger, "organic_pretest_result",
                         product=product_name, video_id=video_id,
                         views=views, likes=likes, comments=comments,
                         shares=shares, engagement_rate=round(engagement_rate, 4))
                return engagement_rate

        except httpx.HTTPStatusError as e:
            log_error(logger, "organic_pretest_http_error",
                      product=product_name, status_code=e.response.status_code,
                      error=str(e))
            return 0.0
        except Exception as e:
            log_error(logger, "organic_pretest_unexpected_error",
                      product=product_name, error=str(e))
            return 0.0

    async def _launch_tiktok_campaign(
        self, product_name: str, niche: str,
        hooks: list[dict], images: list[str],
        opportunity_id: str, tenant_id: str
    ) -> Optional[str]:
        """Launch $50 TikTok Ads campaign via TikTok Marketing API."""
        if not self.tiktok_token:
            logger.warning("TIKTOK_ACCESS_TOKEN not set — campaign launch skipped")
            return f"mock_campaign_{opportunity_id[:8]}"

        headers = {
            "Access-Token": self.tiktok_token,
            "Content-Type": "application/json",
        }

        if not _HTTPX_AVAILABLE:
            raise RuntimeError("httpx not installed — run: pip install httpx")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Create campaign
                campaign_response = await client.post(
                    "https://business-api.tiktok.com/open_api/v1.3/campaign/create/",
                    headers=headers,
                    json={
                        "advertiser_id": self.advertiser_id,
                        "campaign_name": f"VAL_{product_name[:30]}_{opportunity_id[:8]}",
                        "objective_type": "CONVERSIONS",
                        "budget_mode": "BUDGET_MODE_TOTAL",
                        "budget": TIKTOK_TEST_BUDGET,
                    }
                )
                campaign_response.raise_for_status()
                campaign_data = campaign_response.json()
                campaign_id = campaign_data.get("data", {}).get("campaign_id")

                if campaign_id:
                    # Save campaign to DB
                    self.db.save_campaign({
                        "tenant_id": tenant_id,
                        "opportunity_id": opportunity_id,
                        "platform": "tiktok",
                        "external_campaign_id": str(campaign_id),
                        "budget_usd": TIKTOK_TEST_BUDGET,
                        "status": "active",
                    })
                    log_info(logger, "tiktok_campaign_launched", campaign_id=campaign_id)
                    return str(campaign_id)

        except Exception as e:
            log_error(logger, "tiktok_launch_failed", product=product_name, error=str(e))
            return None

        return None
