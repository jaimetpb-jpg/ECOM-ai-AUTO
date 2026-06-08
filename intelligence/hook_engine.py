"""
intelligence/hook_engine.py — Hook Intelligence Engine (NEW V4.0)

Learns which hook categories perform best by niche.
Uses historical CTR data from the hooks table to prioritize generation.

7 hook categories:
  1. fear        — "Stop doing X, it's costing you..."
  2. curiosity   — "This product exists and nobody's talking about it..."
  3. scarcity    — "Last 48 hours at this price..."
  4. transformation — "Before vs After: how X changed my life"
  5. social_proof — "1,200 people bought this last week because..."
  6. identity    — "People who care about X know that..."
  7. savings     — "Stop paying $X for Y when you can..."
"""

import logging
from typing import Optional
from shared.llm_router import LLMRouter
from shared.logging_utils import log_info
from shared.constants import LLM_TIER_CREATIVE, LLM_TIER_BULK

logger = logging.getLogger(__name__)

HOOK_CATEGORIES = ["fear", "curiosity", "scarcity", "transformation", "social_proof", "identity", "savings"]

# Templates per category for GPT-4o Mini to use as inspiration
HOOK_TEMPLATES = {
    "fear":          "Stop [doing X], it's [causing problem]. Here's what actually works:",
    "curiosity":     "This [product] exists and nobody is talking about it. [Reason it works]:",
    "scarcity":      "Last [N] hours at this price. [N] people grabbed this today:",
    "transformation":"Before: [pain]. After [time] with [product]: [result]. Here's how:",
    "social_proof":  "[N] people bought this [time period] because [specific reason]:",
    "identity":      "People who [identity statement] already know that [product insight]:",
    "savings":       "Stop paying $[X] for [Y] every month when [product] does the same for $[Z]:",
}


class HookIntelligenceEngine:
    """
    Generates and learns from hook performance.

    Workflow:
    1. Check DB for best-performing hook categories for this niche
    2. Use top category as primary, generate 3 hooks in that style
    3. Generate 1 backup hook in the second-best category
    4. After test, update DB with CTR results → system learns over time
    """

    def __init__(self, llm_router: Optional[LLMRouter] = None, db=None):
        self.router = llm_router or LLMRouter()
        self.db = db  # SupabaseClient instance

    async def generate_hooks(
        self,
        product_name: str,
        niche: str,
        product_description: str,
        pain_points: list[str] = None,
        n_hooks: int = 3,
    ) -> list[dict]:
        """
        Generate N hooks for a product, prioritizing the historically best category for this niche.
        Returns: list of {hook_text, category, template_used}
        """
        # 1. Get best performing categories from DB
        best_categories = await self._get_best_categories(niche)
        primary_cat   = best_categories[0] if best_categories else "curiosity"
        secondary_cat = best_categories[1] if len(best_categories) > 1 else "fear"

        pain_str = "\n".join(f"- {p}" for p in (pain_points or [])) or "No specific pain points provided"

        prompt = f"""Product: {product_name}
Niche: {niche}
Description: {product_description}
Key pain points from customer reviews:
{pain_str}

Generate {n_hooks} TikTok UGC-style hooks. Requirements:
- Primary style: {primary_cat} (template: "{HOOK_TEMPLATES[primary_cat]}")
- 1 hook must also be in style: {secondary_cat} (template: "{HOOK_TEMPLATES[secondary_cat]}")
- Each hook: 1-2 sentences max. Direct, conversational, no hashtags.
- Format: HOOK 1 [category]: text | HOOK 2 [category]: text | etc.
- Use customer language from the pain points above.

Generate ONLY the hooks, no explanation."""

        response = await self.router.route(LLM_TIER_CREATIVE, prompt)
        hooks = self._parse_hooks(response, niche)

        log_info(logger, "hooks_generated", product=product_name, niche=niche,
                    primary_category=primary_cat, count=len(hooks))
        return hooks

    async def classify_hook(self, hook_text: str) -> str:
        """Classify an existing hook into one of the 7 categories."""
        prompt = f"""Classify this hook into ONE of these categories:
{", ".join(HOOK_CATEGORIES)}

Hook: "{hook_text}"

Respond with ONLY the category name, nothing else."""
        category = (await self.router.route(LLM_TIER_BULK, prompt)).strip().lower()
        return category if category in HOOK_CATEGORIES else "curiosity"

    async def update_hook_performance(self, hook_id: str, ctr: float, won: bool):
        """Update DB with performance data after test completion."""
        if self.db:
            self.db.update_hook_performance(hook_id, ctr, won)
            log_info(logger, "hook_performance_updated", hook_id=hook_id, ctr=ctr, won=won)

    async def _get_best_categories(self, niche: str) -> list[str]:
        """Fetch historically best hook categories for this niche from DB."""
        if not self.db:
            return ["curiosity", "fear"]  # Defaults if no DB
        best_hooks = self.db.get_best_hooks_for_niche(niche, limit=10)
        if not best_hooks:
            return ["curiosity", "fear"]
        # Rank categories by average CTR
        cat_performance: dict[str, list[float]] = {}
        for hook in best_hooks:
            cat = hook.get("category", "curiosity")
            ctr = hook.get("avg_ctr", 0.0)
            cat_performance.setdefault(cat, []).append(ctr)
        ranked = sorted(
            cat_performance.keys(),
            key=lambda c: sum(cat_performance[c]) / len(cat_performance[c]),
            reverse=True
        )
        return ranked

    def _parse_hooks(self, raw_response: str, niche: str) -> list[dict]:
        """Parse LLM hook output into structured format."""
        hooks = []
        for line in raw_response.strip().split("|"):
            line = line.strip()
            if not line:
                continue
            # Try to extract [category] from "HOOK N [category]: text"
            category = "curiosity"
            for cat in HOOK_CATEGORIES:
                if f"[{cat}]" in line.lower() or f"({cat})" in line.lower():
                    category = cat
                    break
            # Clean hook text
            text = line
            for prefix in ["hook 1", "hook 2", "hook 3", "hook 4", "hook 5"]:
                if text.lower().startswith(prefix):
                    text = text[len(prefix):]
            for cat in HOOK_CATEGORIES:
                text = text.replace(f"[{cat}]:", "").replace(f"({cat}):", "")
            text = text.strip().lstrip(":").strip()

            if len(text) > 10:
                hooks.append({"hook_text": text, "category": category, "niche": niche})
        return hooks
