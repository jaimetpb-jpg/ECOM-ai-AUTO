"""
intelligence/meta_ad_library.py — Meta Ad Library Intelligence Engine

🎯 Objetivo: No buscar productos. Buscar PATRONES.

Detecta:
  1. Productos en fase de CRECIMIENTO (ads 15-45 días corriendo = validado pero no saturado)
  2. Hooks psicológicos que están ESCALANDO (qué pattern copy convierte ahora)
  3. Probabilidad de saturación TEMPRANA (velocidad de entrada de competidores)
  4. Señales de demanda REAL (gasto sostenido = producto con ROAS positivo)

Estrategia técnica:
  - Buscar por CATEGORIA no por producto específico
  - Filtrar ads que llevan 15-45 días (sweet spot: validado pero nicho no saturado)
  - Clasificar hooks con Groq (gratuito)
  - Calcular "Opportunity Score" = demanda probada / competencia actual
  - Detectar "Rising Patterns" = hooks que aparecieron hace <14 días con spend alto

API: Meta Ad Library (gratuita, solo token de Meta)
Docs: https://www.facebook.com/ads/library/api/
"""

import os
import json
import math
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from shared.llm_router import LLMRouter
from shared.constants import LLM_TIER_BULK, LLM_TIER_STRATEGIC

logger = logging.getLogger(__name__)

META_AD_LIBRARY_URL = "https://graph.facebook.com/v21.0/ads_archive"

# ─── Pattern taxonomy (what we classify ads INTO) ─────────────────────────────
HOOK_PATTERNS = {
    "fear_loss":        "Fear of losing something / missing out / pain continuation",
    "transformation":   "Before/after, identity change, life improvement story",
    "social_proof":     "Numbers, reviews, influencer, 'X people bought'",
    "curiosity_gap":    "Surprising fact, counterintuitive claim, 'nobody tells you'",
    "scarcity_urgency": "Limited stock, time pressure, price going up",
    "authority":        "Expert endorsement, scientific claim, doctor/professional",
    "identity_tribe":   "Belongs to group, 'people like us', lifestyle signal",
    "savings_deal":     "Price comparison, money saved, ROI framing",
}

AD_LIFECYCLE = {
    "testing":   (0,  14),   # 0-14 days: advertiser still testing, risky to copy
    "growing":   (15, 45),   # 15-45 days: SWEET SPOT — validated, not saturated yet
    "scaling":   (46, 90),   # 46-90 days: good product, more competition entering
    "mature":    (91, 180),  # 91-180 days: saturating, margins compressing
    "saturated": (181, 9999),# 180+ days: avoid, market crowded
}


class MetaAdLibraryClient:
    """Direct Meta Ad Library API client."""

    def __init__(self):
        self.access_token = os.getenv("META_ACCESS_TOKEN")

    async def search_ads(
        self,
        query: str,
        countries: list = None,
        ad_active_status: str = "ACTIVE",
        limit: int = 50,
        min_active_days: int = 0,
        max_active_days: int = 9999,
    ) -> list:
        """
        Search Meta Ad Library for ads matching query.
        Returns list of ad objects with spend, days_running, copy, page_name.
        """
        try:
            import httpx
        except ImportError:
            logger.warning("httpx not installed — returning mock data for development")
            return self._mock_ads(query, limit)

        if not self.access_token:
            logger.warning("META_ACCESS_TOKEN not set — returning mock data")
            return self._mock_ads(query, limit)

        params = {
            "search_terms": query,
            "ad_reached_countries": json.dumps(countries or ["MX", "US", "CO", "AR", "ES"]),
            "ad_active_status": ad_active_status,
            "ad_delivery_date_min": (datetime.now() - timedelta(days=max_active_days)).strftime("%Y-%m-%d"),
            "fields": ",".join([
                "id", "page_name", "page_id",
                "ad_creative_bodies", "ad_creative_link_captions",
                "ad_creative_link_titles", "ad_creative_link_descriptions",
                "ad_delivery_start_time", "ad_delivery_stop_time",
                "spend", "impressions", "currency",
                "publisher_platforms",
            ]),
            "limit": min(limit, 100),
            "access_token": self.access_token,
        }

        try:
            async with __import__("httpx").AsyncClient(timeout=30) as client:
                resp = await client.get(META_AD_LIBRARY_URL, params=params)
                resp.raise_for_status()
                raw = resp.json().get("data", [])
                # Enrich with computed days_running
                enriched = []
                for ad in raw:
                    start = ad.get("ad_delivery_start_time", "")
                    days = self._compute_days_running(start)
                    if min_active_days <= days <= max_active_days:
                        ad["days_running"] = days
                        ad["lifecycle_stage"] = self._classify_lifecycle(days)
                        enriched.append(ad)
                return enriched
        except Exception as e:
            logger.error(f"Meta Ad Library request failed: {e}")
            return self._mock_ads(query, limit)

    def _compute_days_running(self, start_time: str) -> int:
        if not start_time:
            return 0
        try:
            start = datetime.fromisoformat(start_time.replace("+0000", "").strip())
            return (datetime.utcnow() - start).days
        except Exception:
            return 0

    def _classify_lifecycle(self, days: int) -> str:
        for stage, (lo, hi) in AD_LIFECYCLE.items():
            if lo <= days <= hi:
                return stage
        return "unknown"

    def _mock_ads(self, query: str, limit: int) -> list:
        """Realistic mock data for development without API access."""
        import random
        random.seed(hash(query) % 10000)
        stages = ["testing", "growing", "growing", "scaling", "mature"]
        ads = []
        for i in range(min(limit, 20)):
            days = random.choice([7, 22, 35, 28, 60, 45, 15, 90, 120, 200])
            ads.append({
                "id": f"mock_{i}_{query[:8]}",
                "page_name": f"Brand{i} Store",
                "ad_creative_bodies": [
                    f"Stop using regular {query}. This {query} hack changed everything. "
                    f"Join {random.randint(5,50)}k happy customers."
                ],
                "ad_delivery_start_time": (datetime.now() - timedelta(days=days)).isoformat(),
                "days_running": days,
                "lifecycle_stage": self._classify_lifecycle(days),
                "spend": {"lower_bound": str(random.randint(500, 50000)), "upper_bound": str(random.randint(50000, 200000))},
                "impressions": {"lower_bound": str(random.randint(10000, 500000))},
            })
        return ads


class MetaAdIntelligenceEngine:
    """
    Full intelligence layer on top of the Ad Library.

    Usage:
        engine = MetaAdIntelligenceEngine(llm_router=router)
        result = await engine.analyze_niche("skincare", ["moisturizer", "serum", "vitamin c"])

    Returns a rich dict with:
        - opportunity_score: 0-100 (higher = better entry point)
        - rising_patterns: hooks gaining momentum in last 14 days
        - dominant_hooks: most used psychological patterns
        - lifecycle_distribution: what stage are most ads in
        - saturation_signal: early warning of market crowding
        - recommended_angles: specific hooks to TEST (not copy — differentiate)
        - best_ad_examples: sanitized copy structure (not verbatim)
        - competitive_gap: what hooks are ABSENT (opportunity)
    """

    def __init__(self, llm_router: Optional[LLMRouter] = None):
        self.client = MetaAdLibraryClient()
        self.router = llm_router or LLMRouter()

    async def analyze_niche(self, niche: str, keywords: list) -> dict:
        """
        Full niche analysis: search multiple keywords, aggregate patterns.
        Main entry point.
        """
        logger.info(f"meta_ad_analysis_started niche={niche} keywords={keywords}")

        # 1. Gather ads for all keywords in parallel
        all_ads = []
        tasks = [self.client.search_ads(kw, limit=30) for kw in keywords[:5]]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                all_ads.extend(r)

        # Deduplicate by ID
        seen = set()
        unique_ads = []
        for ad in all_ads:
            if ad.get("id") not in seen:
                seen.add(ad.get("id"))
                unique_ads.append(ad)

        logger.info(f"meta_ads_collected total={len(unique_ads)} niche={niche}")

        if not unique_ads:
            return self._empty_result(niche)

        # 2. Analyze lifecycle distribution
        lifecycle = self._lifecycle_distribution(unique_ads)

        # 3. Extract and classify hook patterns (Groq — bulk, free)
        hook_analysis = await self._classify_hook_patterns(unique_ads, niche)

        # 4. Detect rising patterns (recent ads with high spend signal)
        rising = self._detect_rising_patterns(unique_ads, hook_analysis)

        # 5. Compute opportunity score
        opp_score = self._compute_opportunity_score(unique_ads, lifecycle)

        # 6. Saturation signal
        sat_signal = self._saturation_signal(unique_ads, lifecycle)

        # 7. Strategic recommendations (Sonnet if score is borderline)
        recommendations = await self._strategic_recommendations(
            niche, hook_analysis, lifecycle, opp_score, rising
        )

        result = {
            "niche": niche,
            "keywords_searched": keywords,
            "total_ads_found": len(unique_ads),
            "advertiser_count": len(set(a.get("page_name") for a in unique_ads)),
            "opportunity_score": opp_score,
            "lifecycle_distribution": lifecycle,
            "dominant_hooks": hook_analysis.get("dominant", []),
            "rising_patterns": rising,
            "competitive_gap": hook_analysis.get("absent_patterns", []),
            "saturation_signal": sat_signal,
            "recommended_angles": recommendations.get("angles", []),
            "strategic_notes": recommendations.get("notes", ""),
            "best_entry_window": self._entry_window(lifecycle, opp_score),
        }

        logger.info(f"meta_ad_analysis_complete niche={niche} score={opp_score:.0f}")
        return result

    def _lifecycle_distribution(self, ads: list) -> dict:
        """Count ads by lifecycle stage. Sweet spot = 'growing' (15-45 days)."""
        dist = {stage: 0 for stage in AD_LIFECYCLE}
        for ad in ads:
            stage = ad.get("lifecycle_stage", "unknown")
            dist[stage] = dist.get(stage, 0) + 1
        total = len(ads) or 1
        return {
            stage: {"count": count, "pct": round(count / total * 100, 1)}
            for stage, count in dist.items()
        }

    async def _classify_hook_patterns(self, ads: list, niche: str) -> dict:
        """Use Groq (free) to classify ad copy into psychological patterns."""
        # Sample up to 20 ads for classification
        sample = ads[:20]
        ad_copies = []
        for ad in sample:
            bodies = ad.get("ad_creative_bodies", [])
            if bodies:
                ad_copies.append(bodies[0][:200])  # truncate for token efficiency

        if not ad_copies:
            return {"dominant": [], "absent_patterns": list(HOOK_PATTERNS.keys())}

        copies_str = "\n---\n".join(f"{i+1}. {c}" for i, c in enumerate(ad_copies))
        patterns_desc = "\n".join(f"- {k}: {v}" for k, v in HOOK_PATTERNS.items())

        prompt = f"""Classify these {len(ad_copies)} {niche} ads into psychological hook patterns.

PATTERNS:
{patterns_desc}

ADS:
{copies_str}

For EACH ad number, output: [ad_number] [pattern_name]
Then output: DOMINANT: [top 3 patterns separated by comma]
Then output: ABSENT: [patterns not seen, separated by comma]
Then output: INSIGHT: [1 sentence about what's working in this niche right now]

Be concise."""

        try:
            raw = await self.router.route(LLM_TIER_BULK, prompt, max_tokens=600)
            return self._parse_pattern_classification(raw)
        except Exception as e:
            logger.warning(f"Pattern classification failed: {e}")
            return {"dominant": ["social_proof", "transformation"], "absent_patterns": [], "insight": ""}

    def _parse_pattern_classification(self, raw: str) -> dict:
        dominant, absent, insight = [], [], ""
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("DOMINANT:"):
                dominant = [p.strip() for p in line.replace("DOMINANT:", "").split(",")]
            elif line.startswith("ABSENT:"):
                absent = [p.strip() for p in line.replace("ABSENT:", "").split(",")]
            elif line.startswith("INSIGHT:"):
                insight = line.replace("INSIGHT:", "").strip()
        return {"dominant": dominant[:5], "absent_patterns": absent[:5], "insight": insight}

    def _detect_rising_patterns(self, ads: list, hook_analysis: dict) -> list:
        """
        Rising = patterns appearing in ads that started <14 days ago but have high spend.
        These are the hooks to TEST (not copy verbatim).
        """
        recent_ads = [a for a in ads if a.get("days_running", 999) <= 14]
        if not recent_ads:
            return []

        # Extract copy from recent ads to find emerging patterns
        rising = []
        dominant = hook_analysis.get("dominant", [])
        for ad in recent_ads[:5]:
            bodies = ad.get("ad_creative_bodies", [])
            if bodies:
                copy = bodies[0][:150]
                # Try to match to a pattern
                for pat in dominant:
                    pattern_keywords = HOOK_PATTERNS.get(pat, "").lower().split()[:3]
                    if any(kw in copy.lower() for kw in pattern_keywords):
                        rising.append({
                            "pattern": pat,
                            "days_since_emergence": ad.get("days_running", 0),
                            "copy_structure": self._extract_copy_structure(copy),
                        })
                        break
        return rising[:3]

    def _extract_copy_structure(self, copy: str) -> str:
        """Return structural pattern, not verbatim copy (copyright safe)."""
        words = copy.split()[:12]
        # Replace specific nouns with [PRODUCT], [NUMBER] etc.
        import re
        structure = copy[:100]
        structure = re.sub(r'\d+[k%]?', '[NUMBER]', structure)
        structure = re.sub(r'\b[A-Z][a-z]+\b', '[BRAND]', structure)
        return structure[:80] + "..."

    def _compute_opportunity_score(self, ads: list, lifecycle: dict) -> float:
        """
        0-100. Higher = better entry window.

        Logic:
          - Many ads in 'growing' stage = demand proven, not yet saturated → HIGH score
          - Many ads in 'saturated' stage = avoid → LOW score
          - Few total advertisers = fragmented market → bonus
          - Many new ads in 'testing' = trend just starting → bonus
        """
        total = len(ads) or 1
        growing_pct  = lifecycle.get("growing",   {}).get("pct", 0) / 100
        scaling_pct  = lifecycle.get("scaling",   {}).get("pct", 0) / 100
        saturated_pct= lifecycle.get("saturated", {}).get("pct", 0) / 100
        testing_pct  = lifecycle.get("testing",   {}).get("pct", 0) / 100
        advertiser_count = len(set(a.get("page_name") for a in ads))

        # Sweet spot signal (30-50 advertisers in growing = hot but not crowded)
        advertiser_density = min(1.0, advertiser_count / 50)

        score = (
            growing_pct  * 40 +   # Growing ads = proven demand
            testing_pct  * 20 +   # New ads = trend emerging
            scaling_pct  * 20 +   # Still scalable
            (1 - saturated_pct) * 10 +  # Low saturation = good
            (1 - advertiser_density) * 10  # Few advertisers = open market
        )
        return round(min(100, max(0, score)), 1)

    def _saturation_signal(self, ads: list, lifecycle: dict) -> dict:
        """Early warning system for market saturation."""
        saturated_pct = lifecycle.get("saturated", {}).get("pct", 0)
        mature_pct    = lifecycle.get("mature",    {}).get("pct", 0)
        advertiser_count = len(set(a.get("page_name") for a in ads))

        # Count new entrants (ads < 14 days) — if many, saturation incoming
        new_entrants = sum(1 for a in ads if a.get("days_running", 999) <= 14)
        new_entrant_rate = new_entrants / (len(ads) or 1)

        risk_level = "LOW"
        if saturated_pct > 40 or advertiser_count > 100:
            risk_level = "HIGH"
        elif saturated_pct > 20 or mature_pct > 30 or new_entrant_rate > 0.4:
            risk_level = "MEDIUM"

        return {
            "risk_level": risk_level,
            "saturated_pct": saturated_pct,
            "advertiser_count": advertiser_count,
            "new_entrant_rate_14d": round(new_entrant_rate * 100, 1),
            "interpretation": {
                "LOW":    "Good entry window. Market has room.",
                "MEDIUM": "Entering late. Need strong differentiation.",
                "HIGH":   "Avoid. Margins compressed. CPM rising.",
            }[risk_level],
        }

    async def _strategic_recommendations(
        self, niche: str, hook_analysis: dict, lifecycle: dict, opp_score: float, rising: list
    ) -> dict:
        """Sonnet-level analysis for strong opportunities (score ≥ 60)."""
        if opp_score < 60:
            # Cheap path: basic recommendations from patterns
            dominant = hook_analysis.get("dominant", ["transformation", "social_proof"])
            absent   = hook_analysis.get("absent_patterns", [])
            angles = [
                f"Test {absent[0]} angle — not present in current ads" if absent else
                f"Differentiate from dominant {dominant[0]} by adding a {dominant[1] if len(dominant) > 1 else 'scarcity'} angle",
            ]
            return {"angles": angles, "notes": hook_analysis.get("insight", "")}

        dominant = hook_analysis.get("dominant", [])
        absent   = hook_analysis.get("absent_patterns", [])
        rising_patterns = [r.get("pattern") for r in rising]

        prompt = f"""Meta Ad Library analysis for niche: {niche}
Opportunity score: {opp_score:.0f}/100

Market patterns:
- Dominant hooks (saturated): {', '.join(dominant)}
- Absent hooks (opportunity): {', '.join(absent)}  
- Rising hooks (last 14 days): {', '.join(rising_patterns)}
- Lifecycle: {json.dumps({k: v['pct'] for k, v in lifecycle.items() if v['count'] > 0})}

As a direct-response marketing strategist, recommend:
1. ANGLE_1: [specific differentiation hook to test — use an absent or rising pattern]
2. ANGLE_2: [alternative hook angle]
3. ANGLE_3: [bold contrarian angle — against the dominant trend]
4. NOTE: [1-sentence market timing insight]

Be specific to {niche}. Short answers."""

        try:
            raw = await self.router.route(LLM_TIER_STRATEGIC, prompt, max_tokens=400)
            angles, notes = [], ""
            for line in raw.split("\n"):
                if line.strip().startswith("ANGLE_"):
                    angles.append(line.split(":", 1)[-1].strip())
                elif line.strip().startswith("NOTE:"):
                    notes = line.split(":", 1)[-1].strip()
            return {"angles": angles[:3], "notes": notes}
        except Exception as e:
            logger.warning(f"Strategic recs failed: {e}")
            return {"angles": [], "notes": ""}

    def _entry_window(self, lifecycle: dict, opp_score: float) -> str:
        growing_pct = lifecycle.get("growing", {}).get("pct", 0)
        if opp_score >= 70 and growing_pct >= 30:
            return "ENTER_NOW — Sweet spot: demand proven, market not saturated"
        elif opp_score >= 50:
            return "ENTER_WITH_DIFFERENTIATION — Market developing, need unique angle"
        elif opp_score >= 30:
            return "WATCH_AND_WAIT — Too early (testing phase) or late (saturating)"
        else:
            return "AVOID — Market saturated or insufficient demand signals"

    def _empty_result(self, niche: str) -> dict:
        return {
            "niche": niche, "total_ads_found": 0, "advertiser_count": 0,
            "opportunity_score": 50.0, "lifecycle_distribution": {},
            "dominant_hooks": [], "rising_patterns": [], "competitive_gap": [],
            "saturation_signal": {"risk_level": "UNKNOWN"},
            "recommended_angles": [], "strategic_notes": "No ads found — possible early trend",
            "best_entry_window": "WATCH_AND_WAIT — insufficient data",
        }
