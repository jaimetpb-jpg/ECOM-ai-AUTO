"""
scaling/niche_swarm.py — Niche Swarm Strategy (V4.0 Enhanced)

Dominates a niche completely before jumping to the next.
Strategy: 1 anchor product validates → find 8 complementary products →
$20 micro-tests each → best 3-5 form brand store → ONE Meta audience serves ALL →
CAC -40-60%, LTV +3x, CPA consolidated.
"""
import logging
from typing import Optional
from shared.llm_router import LLMRouter
from shared.supabase_client import SupabaseClient
from shared.slack_notifier import SlackNotifier
from shared.logging_utils import log_info
from shared.constants import (
    LLM_TIER_STRATEGIC, LLM_TIER_BULK,
    NICHE_MICROTEST_BUDGET, NICHE_MAX_COMPLEMENTS,
)

logger = logging.getLogger(__name__)


class NicheSwarmEngine:
    """
    Niche Swarm: once anchor product validates (ROAS ≥ 1.5),
    automatically identifies and micro-tests complementary products.
    """

    def __init__(self, llm_router=None, db=None, slack=None):
        self.router = llm_router or LLMRouter()
        self.db     = db or SupabaseClient()
        self.slack  = slack or SlackNotifier()

    async def launch_swarm(self, anchor_opportunity: dict, tenant_id: str) -> dict:
        niche        = anchor_opportunity.get("niche", "")
        anchor_name  = anchor_opportunity.get("name", "")
        anchor_id    = anchor_opportunity.get("id")

        log_info(logger, "niche_swarm_started", anchor=anchor_name, niche=niche)

        # 1. Find complementary products (Sonnet — strategic decision)
        complements = await self._find_complementary_products(anchor_name, niche)
        log_info(logger, "complements_found", count=len(complements), niche=niche)

        # 2. Save niche profile
        niche_profile = self.db.save_niche_profile({
            "tenant_id": tenant_id,
            "anchor_opportunity_id": anchor_id,
            "niche_name": niche,
            "anchor_product": anchor_name,
            "complementary_products": complements,
            "swarm_status": "in_progress",
        })

        # 3. Slack notification with swarm plan
        complement_list = "\n".join(
            f"  {i+1}. {c['name']} (margin ~{c.get('estimated_margin',40):.0f}%, "
            f"diff score {c.get('differentiation_score',50):.0f})"
            for i, c in enumerate(complements[:5])
        )
        self.slack.notify_alert(
            f"🐝 *Niche Swarm Launched* — {niche.title()}\n"
            f"Anchor: *{anchor_name}* (ROAS validated ✅)\n"
            f"Complementary products to micro-test (${NICHE_MICROTEST_BUDGET} each):\n"
            f"{complement_list}\n"
            f"Budget for swarm phase: ${len(complements) * NICHE_MICROTEST_BUDGET:.0f} total"
        )

        return {
            "niche_profile_id": niche_profile.get("id"),
            "anchor": anchor_name,
            "niche": niche,
            "complements": complements,
            "swarm_budget_usd": len(complements) * NICHE_MICROTEST_BUDGET,
            "next_action": "launch_complement_microtests",
        }

    async def evaluate_swarm(self, niche_profile: dict, complement_results: list[dict]) -> dict:
        """
        After micro-tests, select winners for the brand store.
        complement_results: [{name, roas, spend}, ...]
        """
        winners = [r for r in complement_results if r.get("roas", 0) >= 1.5]
        winners.sort(key=lambda x: x.get("roas", 0), reverse=True)
        top_winners = winners[:5]

        niche = niche_profile.get("niche_name", "")
        anchor = niche_profile.get("anchor_product", "")

        analysis = await self._analyze_swarm_results(anchor, niche, top_winners)

        self.slack.notify_alert(
            f"🏆 *Niche Swarm Results* — {niche.title()}\n"
            f"Winners: {len(top_winners)}/{len(complement_results)} products validated\n"
            f"Top ROAS: {top_winners[0].get('roas', 0):.2f}x — {top_winners[0].get('name', '')}\n\n"
            f"{analysis}\n\n"
            f"→ Proceed to branding for full niche store with {len(top_winners)+1} products"
        )

        return {
            "swarm_winners": top_winners,
            "total_products_for_store": len(top_winners) + 1,  # +1 for anchor
            "analysis": analysis,
            "next_action": "create_brand_for_niche_store",
        }

    async def _find_complementary_products(self, anchor_name: str, niche: str) -> list[dict]:
        """Use Sonnet to identify the best complementary products for the niche."""
        prompt = f"""Anchor product: {anchor_name} | Niche: {niche}

Identify {NICHE_MAX_COMPLEMENTS} complementary products that:
1. Same target audience as the anchor product
2. Not direct competitors — fill different use cases
3. High margin (≥40%), lightweight, easy to source from AliExpress
4. Safe (no FDA/trademark issues)
5. Can share the SAME Meta/TikTok audience as the anchor

For each product provide:
NAME: [name] | REASON: [why same audience buys it] | MARGIN: [%] | DIFF: [0-100] | RISK: [0.0-1.0]

Output exactly {NICHE_MAX_COMPLEMENTS} products, one per line."""

        response = await self.router.route(LLM_TIER_STRATEGIC, prompt)
        return self._parse_complements(response)

    async def _analyze_swarm_results(self, anchor: str, niche: str, winners: list) -> str:
        """Sonnet analysis of swarm test results."""
        winners_str = "\n".join(
            f"- {w['name']}: ROAS {w.get('roas',0):.2f}x"
            for w in winners
        )
        prompt = f"""Niche Swarm results for {niche} (anchor: {anchor}):
Winning products:
{winners_str}

Briefly analyze (3 sentences max):
1. Do these products form a coherent brand store?
2. What combined theme/positioning would unify them?
3. Expected synergy: will ONE Meta audience convert across all products?"""

        return await self.router.route(LLM_TIER_STRATEGIC, prompt, max_tokens=300)

    def _parse_complements(self, response: str) -> list[dict]:
        products = []
        for line in response.strip().split("\n"):
            if "NAME:" not in line:
                continue
            parts = {}
            for segment in line.split("|"):
                if ":" in segment:
                    k, v = segment.split(":", 1)
                    parts[k.strip().upper()] = v.strip()
            if parts.get("NAME"):
                try:
                    products.append({
                        "name": parts["NAME"],
                        "reason": parts.get("REASON", ""),
                        "estimated_margin": float(parts.get("MARGIN", "40").replace("%", "")),
                        "differentiation_score": float(parts.get("DIFF", "50")),
                        "legal_risk": float(parts.get("RISK", "0.1")),
                        "budget_usd": NICHE_MICROTEST_BUDGET,
                    })
                except (ValueError, KeyError):
                    continue
        return products[:NICHE_MAX_COMPLEMENTS]
