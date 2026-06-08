"""
branding/brand_creator.py — AI Brand Generation Pipeline

Generates a complete brand identity in <2h for a validated product.
  - Sonnet  → Brand strategy, positioning, naming
  - GPT-Mini → Copy, descriptions, ad scripts, email sequences
  - Flux.1  → Logo, lifestyle images via Replicate

Called after: TikTok validation ROAS ≥ 1.5 AND Niche Swarm completes.
Human gate: Brand approval in Slack #approvals (10 min timeout).
"""

import os
import json
import re
try:
    import replicate
    _REPLICATE_AVAILABLE = True
except ImportError:
    replicate = None
    _REPLICATE_AVAILABLE = False
import logging
from shared.logging_utils import log_info, log_warning, log_error
from typing import Optional
from shared.llm_router import LLMRouter
from shared.constants import LLM_TIER_STRATEGIC, LLM_TIER_CREATIVE, GATE_BRANDING_MIN
from shared.supabase_client import SupabaseClient
from shared.slack_notifier import SlackNotifier, SLACK_APPROVALS

logger = logging.getLogger(__name__)


class BrandCreator:
    """
    Creates a complete brand identity for a validated product.

    Output:
      - Brand strategy (name, positioning, values, voice)
      - Visual identity (logo URLs, color palette, typography)
      - Product page copy (title, bullets, description)
      - 3 TikTok ad scripts
      - 3 email sequences (welcome, cart recovery, upsell)
    """

    def __init__(self, llm_router=None, db=None, slack=None):
        self.router = llm_router or LLMRouter()
        self.db     = db or SupabaseClient()
        self.slack  = slack or SlackNotifier()

    async def create_brand(self, opportunity: dict, niche_profile: dict = None) -> dict:
        product_name = opportunity.get("name", "")
        niche        = opportunity.get("niche", "")
        tenant_id    = opportunity.get("tenant_id")
        opp_id       = opportunity.get("id")

        log_info(logger, "brand_creation_started", product=product_name)

        strategy  = await self._create_strategy(opportunity, niche_profile)
        visual    = await self._create_visual_identity(strategy)
        copy_pkg  = await self._create_copy_package(strategy, opportunity)
        emails    = await self._create_email_sequences(strategy)

        # Human gate — Slack approval (10 min)
        brand_preview = (
            f"*Brand:* {strategy.get('name')}\n"
            f"*Tagline:* _{strategy.get('tagline')}_\n"
            f"*Positioning:* {strategy.get('positioning')}\n"
            f"*Voice:* {strategy.get('voice')}\n"
            f"*Colors:* {' '.join(strategy.get('color_palette', []))}\n"
            f"*Logo variants:* {len(visual.get('logo_urls', []))} generated\n"
            f"*Copy preview:* {copy_pkg.get('product_title', '')}"
        )
        approved = await self.slack.request_approval(
            title=f"Brand Approval: {strategy.get('name', product_name)}",
            details=brand_preview,
            timeout_minutes=GATE_BRANDING_MIN,
            channel=SLACK_APPROVALS,
        )

        status = "approved" if approved else "pending_revision"

        saved = self.db.save_brand({
            "tenant_id": tenant_id,
            "opportunity_id": opp_id,
            "name": strategy.get("name", product_name),
            "strategy": strategy,
            "visual_identity": visual,
            "status": status,
        })

        log_info(logger, "brand_created", brand_name=strategy.get("name"), status=status)
        return {
            "brand_id": saved.get("id"),
            "brand_name": strategy.get("name"),
            "strategy": strategy,
            "visual_identity": visual,
            "copy_package": copy_pkg,
            "email_sequences": emails,
            "status": status,
            "next_action": "deploy_medusa_store" if approved else "revise_branding",
        }

    async def _create_strategy(self, opportunity: dict, niche_profile: dict = None) -> dict:
        """Sonnet: brand strategy and naming."""
        product = opportunity.get("name", "")
        niche   = opportunity.get("niche", "")
        score   = opportunity.get("score_breakdown", {})
        niche_ctx = ""
        if niche_profile:
            products = niche_profile.get("complementary_products", [])
            niche_ctx = f"\nNiche Swarm context — complementary products: {', '.join(p.get('name','') for p in products[:5])}"

        prompt = f"""Product: {product} | Niche: {niche}
Score: {score.get('final', 0):.0f}/100 | Margin score: {score.get('margin', 0):.0f} | Viral: {score.get('viral', 0):.0f}
{niche_ctx}

Create a DTC brand strategy. Respond ONLY with valid JSON:
{{
  "name": "Brand name (2 words max, memorable)",
  "tagline": "Max 7 words",
  "values": ["value1", "value2", "value3"],
  "voice": "casual/expert/playful + 2 adjectives",
  "positioning": "1 sentence: who + what + why different",
  "target_audience": "age, lifestyle, problem",
  "color_palette": ["#hex1", "#hex2", "#hex3"],
  "typography": {{"heading": "Syne", "body": "Inter"}},
  "logo_prompt": "Flux.1 prompt for minimalist logo icon, no text",
  "usp": "1 sentence unique value prop",
  "price_tier": "budget|mid|premium"
}}"""

        raw = await self.router.route(LLM_TIER_STRATEGIC, prompt, temperature=0.8)
        try:
            # Robust JSON extraction with regex
            json_match = re.search(r'\{[\s\S]*\}', raw)
            if json_match:
                json_str = json_match.group(0)
                strategy = json.loads(json_str)
                log_info(logger, "brand_strategy_parsed", name=strategy.get("name"))
                return strategy
            else:
                log_warning(logger, "json_extraction_failed_no_braces", raw_preview=raw[:200])
                raise ValueError("No JSON object found in LLM response")
        except (json.JSONDecodeError, ValueError) as e:
            log_warning(logger, "brand_strategy_fallback_used", error=str(e), raw_preview=raw[:200])
            return {"name": product, "tagline": f"Best {niche}", "values": ["quality", "value", "trust"],
                    "voice": "friendly, direct", "positioning": f"The best {niche} product for everyone",
                    "target_audience": niche, "color_palette": ["#1A1A2E", "#16213E", "#E94560"],
                    "typography": {"heading": "Syne", "body": "Inter"},
                    "logo_prompt": f"minimalist icon for {product}", "usp": f"Best {niche}",
                    "price_tier": "mid"}

    async def _create_visual_identity(self, strategy: dict) -> dict:
        """Flux.1: logo and visual assets."""
        logo_urls = []
        prompt_base = (
            f"{strategy.get('logo_prompt', 'minimalist brand logo')}, "
            f"colors {' '.join(strategy.get('color_palette', ['#1A1A2E'])[:2])}, "
            f"vector clean professional, white background, no text"
        )
        if not _REPLICATE_AVAILABLE:
            logger.warning("replicate not installed — logo generation skipped")
        else:
            try:
                for _ in range(2):
                    output = replicate.run(
                        "black-forest-labs/flux-dev",
                        input={"prompt": prompt_base, "num_outputs": 1, "aspect_ratio": "1:1"}
                    )
                    if output:
                        logo_urls.append(output[0] if isinstance(output, list) else str(output))
            except Exception as e:
                log_warning(logger, "flux_logo_failed", error=str(e))
        return {
            "logo_urls": logo_urls,
            "color_palette": strategy.get("color_palette", []),
            "typography": strategy.get("typography", {}),
        }

    async def _create_copy_package(self, strategy: dict, opportunity: dict) -> dict:
        """GPT-4o Mini: full copy for product page + TikTok scripts."""
        prompt = f"""Brand: {strategy.get('name')} | Product: {opportunity.get('name')} | Niche: {opportunity.get('niche')}
Voice: {strategy.get('voice')} | USP: {strategy.get('usp')} | Target: {strategy.get('target_audience')}

Generate:
TITLE: (70 chars max, SEO)
BULLET1: (main benefit)
BULLET2: (pain point solved)
BULLET3: (social proof angle)
BULLET4: (uniqueness)
BULLET5: (CTA urgency)
DESCRIPTION: (150 words)
META: (160 chars)
SCRIPT1 [fear hook]: 30-sec TikTok script
SCRIPT2 [transformation hook]: 30-sec TikTok script
SCRIPT3 [curiosity hook]: 30-sec TikTok script
HERO_HEADLINE: (max 8 words)
HERO_SUB: (max 20 words)
CTA_BUTTON: (max 4 words)"""

        raw = await self.router.route(LLM_TIER_CREATIVE, prompt, max_tokens=2000)
        # Parse key fields
        lines = raw.split("\n")
        result = {"raw": raw}
        for line in lines:
            if line.startswith("TITLE:"):
                result["product_title"] = line.replace("TITLE:", "").strip()
            elif line.startswith("META:"):
                result["meta_description"] = line.replace("META:", "").strip()
            elif line.startswith("HERO_HEADLINE:"):
                result["hero_headline"] = line.replace("HERO_HEADLINE:", "").strip()
        return result

    async def _create_email_sequences(self, strategy: dict) -> dict:
        """GPT-4o Mini: 3 email sequences."""
        prompt = f"""Brand: {strategy.get('name')} | Voice: {strategy.get('voice')}

Write 3 email sequences (concise):

WELCOME_1 [Day 0 - subject + 120 words]: Welcome + brand story
WELCOME_2 [Day 2 - subject + 100 words]: Product tips
WELCOME_3 [Day 5 - subject + 90 words]: Social proof + 10% off code WELCOME10

CART_1 [1h - subject + 60 words]: Warm reminder
CART_2 [24h - subject + 60 words]: Urgency + social proof
CART_3 [48h - subject + 60 words]: Last chance + SAVE10

POSTBUY_1 [Day 3 - subject + 80 words]: Tips + review request
POSTBUY_2 [Day 14 - subject + 80 words]: Upsell complementary product"""

        raw = await self.router.route(LLM_TIER_CREATIVE, prompt, max_tokens=2000)
        return {"emails_raw": raw}
