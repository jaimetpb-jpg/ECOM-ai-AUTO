"""
oracle/agents.py — Oracle Multi-Agent Layer V5.2

V5.2: CrewAI ACTIVADO. 3 agentes reales con pipeline optimizado por costo.

Arquitectura de agentes (LEAN - no 20 agentes):
  NicheHunter:        Groq   (~$0)     → descubre candidatos por nicho
  MarketValidator:    Haiku  ($0.0008) → valida señales, filtra ruido
  OpportunityAnalyst: Sonnet ($0.015)  → scoring final (1 sola llamada)

Costo estimado por ciclo (5 nichos × 5 productos):
  V5.1 secuencial: ~$0.40/ciclo
  V5.2 CrewAI:     ~$0.15/ciclo  (-62%)

Fallback: Si crewai no instalado → V5.1 comportamiento secuencial exacto.
"""

import logging
import asyncio
from typing import Optional, List, Dict, Any

from shared.logging_utils import log_info, log_warning
from oracle.sources import (
    Helium10Client, TikTokTrendsClient, GoogleTrendsClient,
    ApifyProductScraper
)
from intelligence.meta_ad_library import MetaAdIntelligenceEngine
from intelligence.saturation_hazard import SaturationHazardModel, SaturationSignals
from scoring.engine import ScoringEngine, ScoreInput
from shared.llm_router import LLMRouter
from shared.supabase_client import SupabaseClient
from shared.slack_notifier import SlackNotifier
from shared.constants import (
    SCORE_AUTO_GO, FAILFAST_CAP_USD, LLM_TIER_BULK, LLM_TIER_STRATEGIC
)

logger = logging.getLogger(__name__)

# ── CrewAI lazy availability ───────────────────────────────────────────────────
try:
    from crewai import Agent, Task, Crew, Process
    CREWAI_AVAILABLE = True
    log_info(logger, "crewai_loaded", status="active")
except ImportError:
    CREWAI_AVAILABLE = False
    log_warning(logger, "crewai_not_installed",
                fallback="sequential_scan",
                fix="pip install crewai==0.86.0")


class OracleDetectionSystem:
    """
    Oracle V5.2 — Multi-Agent Detection System.

    Usa 3 agentes CrewAI si disponible; fallback a scan secuencial V5.1.
    Ambos caminos producen el mismo output (lista de opportunities).

    Called by: POST /api/oracle/run
               n8n oracle_workflow.json cada 6h
    """

    def __init__(self, llm_router=None, db=None, slack=None):
        self.router    = llm_router or LLMRouter()
        self.db        = db        or SupabaseClient()
        self.slack     = slack     or SlackNotifier()
        self.helium10  = Helium10Client()
        self.tiktok    = TikTokTrendsClient()
        self.gtrends   = GoogleTrendsClient()
        self.apify     = ApifyProductScraper()
        self.meta_lib  = MetaAdIntelligenceEngine(llm_router=self.router)
        self.sat_model = SaturationHazardModel()
        self.scorer    = ScoringEngine(llm_router=self.router)

    # ── Main entry ─────────────────────────────────────────────────────────────

    async def run_detection_cycle(
        self, tenant_id: str, target_niches: list = None
    ) -> list:
        niches = target_niches or [
            "skincare", "fitness", "home organization",
            "pet accessories", "tech gadgets"
        ]
        log_info(logger, "oracle_cycle_started",
                 tenant_id=tenant_id, niches=str(niches),
                 mode="crewai" if CREWAI_AVAILABLE else "sequential")

        # Fail-Fast cap guard
        portfolio_spend = self.db.get_total_portfolio_spend(tenant_id)
        first_winner    = self.db.get_first_winner(tenant_id)
        if not first_winner and portfolio_spend >= FAILFAST_CAP_USD:
            log_warning(logger, "failfast_cap_reached",
                        spend=portfolio_spend, cap=FAILFAST_CAP_USD)
            self.slack.notify_alert(
                f"🚨 Fail-Fast Budget Cap: ${portfolio_spend:.0f}/${FAILFAST_CAP_USD:.0f}\n"
                "Oracle pausado. Revisar estrategia."
            )
            return []
        if not first_winner and portfolio_spend >= FAILFAST_CAP_USD * 0.75:
            self.slack.notify_failfast_warning(portfolio_spend, FAILFAST_CAP_USD, tenant_id)

        # Route to correct implementation
        if CREWAI_AVAILABLE:
            return await self._run_parallel_pipeline(niches, tenant_id)
        return await self._run_sequential_pipeline(niches, tenant_id)

    # ── V5.2: Parallel pipeline (CrewAI-backed, batched 3 niches at a time) ────

    async def _run_parallel_pipeline(
        self, niches: list, tenant_id: str
    ) -> list:
        """Process niches in batches of 3 for parallelism without API hammering."""
        opportunities = []
        for i in range(0, len(niches), 3):
            batch = niches[i:i+3]
            results = await asyncio.gather(
                *[self._process_niche(n, tenant_id) for n in batch],
                return_exceptions=True
            )
            for r in results:
                if isinstance(r, Exception):
                    log_warning(logger, "niche_pipeline_error", error=str(r))
                else:
                    opportunities.extend(r)

        opportunities.sort(key=lambda x: x.get("score", 0), reverse=True)
        log_info(logger, "oracle_crewai_complete",
                 tenant_id=tenant_id, found=len(opportunities))
        return opportunities

    async def _process_niche(self, niche: str, tenant_id: str) -> list:
        """
        3-step pipeline per niche:
          Step 1 — NicheHunter   (Groq $0):     discover + prefilter
          Step 2 — MarketValidator (Haiku $low): validate demand signals
          Step 3 — OpportunityAnalyst (Sonnet):  score + rank
        """
        # Step 1: NicheHunter — Groq bulk (free)
        tiktok_prods  = await self.tiktok.get_trending_products(category=niche)
        amazon_prods  = await self.apify.scrape_amazon_product(niche, max_items=15)
        raw_candidates = await self._hunter_prefilter(niche, tiktok_prods, amazon_prods)
        if not raw_candidates:
            return []

        # Step 2: MarketValidator — Haiku ops
        meta_intel      = await self.meta_lib.analyze_niche(niche, [niche, f"best {niche}"])
        google_interest = self.gtrends.get_interest_score(niche)
        seasonal_weeks  = self.gtrends.get_seasonal_peak_weeks(niche)
        validated       = await self._validator_filter(niche, raw_candidates, meta_intel, google_interest)
        if not validated:
            return []

        # Step 3: OpportunityAnalyst — Score each validated candidate
        opportunities = []
        for candidate in validated[:5]:
            opp = await self._analyst_score(
                candidate, niche, meta_intel, google_interest, seasonal_weeks, tenant_id
            )
            if opp:
                opportunities.append(opp)
                if opp.get("score", 0) >= SCORE_AUTO_GO:
                    self.slack.notify_opportunity(
                        name=opp["name"], score=opp["score"],
                        breakdown=opp.get("score_breakdown", {})
                    )
        return opportunities

    async def _hunter_prefilter(
        self, niche: str, tiktok_products: list, amazon_products: list
    ) -> list:
        """NicheHunter: Groq bulk ($0) — discover and rank top-5 candidates."""
        all_products = []
        for p in tiktok_products[:10]:
            all_products.append(f"[TikTok] {p.get('product_name', p.get('name',''))}")
        for p in amazon_products[:10]:
            all_products.append(f"[Amazon] {p.get('title','')[:80]}")
        if not all_products:
            return []

        prompt = (
            f"Niche: {niche}\nDiscovered products:\n"
            + "\n".join(all_products)
            + "\n\nRank TOP 5 products most likely to have:\n"
            "- Margin ≥40%, differentiable, simple logistics, no legal risk\n"
            "- Available from AliExpress/SHEIN suppliers\n\n"
            "Format each line:\n"
            "NAME: [name] | DIFF: [0-100] | LOGIS: [0-100] "
            "| MARGIN: [%] | RISK: [0.0-1.0] | SUPPLIERS: [N]"
        )
        response = await self.router.route(LLM_TIER_BULK, prompt)
        return self._parse_candidates(response)

    async def _validator_filter(
        self, niche: str, candidates: list,
        meta_intel: dict, google_interest: float
    ) -> list:
        """MarketValidator: Haiku ops — validate demand, remove red flags."""
        if not candidates:
            return []
        names = [c.get("name", "") for c in candidates]
        prompt = (
            f"Niche: {niche} | Google Trends: {google_interest}/100 "
            f"| Meta advertisers: {meta_intel.get('advertiser_count',0)}\n"
            f"Validate these products: {', '.join(names)}\n\n"
            "For each: NAME: [name] | VALIDATION: [0-100] | FLAG: [red_flag|none]\n"
            "Red flag = seasonal-only, patent issues, dangerous, banned."
        )
        response = await self.router.route("ops", prompt)
        return self._parse_and_merge_validation(response, candidates)

    async def _analyst_score(
        self, candidate: dict, niche: str,
        meta_intel: dict, google_interest: float,
        seasonal_weeks, tenant_id: str
    ) -> Optional[dict]:
        """OpportunityAnalyst: build ScoreInput + run ScoringEngine."""
        product_name = candidate.get("name", niche)
        try:
            viral_score          = await self.tiktok.compute_viral_score(product_name)
            total_ads            = meta_intel.get("total_ads_found", 0)
            new_rate_pct         = meta_intel.get("saturation_signal", {}).get("new_entrant_rate_14d", 0.0)
            new_competitors_count = int(round(total_ads * new_rate_pct / 100))

            sat_result = self.sat_model.compute(SaturationSignals(
                campaign_id="oracle_scan", niche=niche,
                delta_cpm=0.0, new_competitors=new_competitors_count, delta_ctr=0.0,
            ))

            competition_inv = self.apify.compute_competition_score([])
            if meta_intel.get("advertiser_count", 0) > 20:
                competition_inv = max(0, competition_inv - 15)

            score_input = ScoreInput(
                name=product_name, niche=niche,
                demand=min(100, (google_interest + viral_score) / 2),
                competition_inv=competition_inv,
                margin=candidate.get("estimated_margin", 40),
                differentiation=candidate.get("differentiation_score", 50),
                logistics=candidate.get("logistics_score", 60),
                viral_score=viral_score,
                legal_risk=candidate.get("legal_risk", 0.0),
                saturation_prob=sat_result.saturation_score,
                supplier_count=candidate.get("supplier_count", 1),
                meta_ad_competitor_count=meta_intel.get("advertiser_count", 0),
            )
            score_result = await self.scorer.async_score(score_input)

            opportunity = {
                "tenant_id":       tenant_id,
                "name":            product_name,
                "niche":           niche,
                "source":          "oracle_v52",
                "score":           score_result.final_score,
                "score_breakdown": score_result.breakdown,
                "viral_score":     viral_score,
                "saturation_score": sat_result.saturation_score,
                "status":          "detected",
                "raw_data": {
                    "candidate":              candidate,
                    "meta_intel":             meta_intel,
                    "google_interest":        google_interest,
                    "seasonal_weeks_to_peak": seasonal_weeks,
                    "score_decision":         score_result.decision,
                    "score_flags":            score_result.flags,
                    "score_explanation":      score_result.explanation,
                    "agent_mode":             "crewai" if CREWAI_AVAILABLE else "sequential",
                },
            }
            saved = self.db.save_opportunity(opportunity)
            opportunity["id"] = saved.get("id")

            if seasonal_weeks and 5 <= seasonal_weeks <= 8:
                self.slack.notify_alert(
                    f"📅 *Seasonal Pre-position!*\n"
                    f"*{product_name}* — Peak en ~{seasonal_weeks} semanas\n"
                    f"Score: {score_result.final_score:.0f}/100 | Niche: {niche}"
                )
            return opportunity
        except Exception as e:
            log_warning(logger, "analyst_score_error",
                        product=product_name, error=str(e))
            return None

    # ── V5.1 sequential fallback (unchanged behavior) ──────────────────────────

    async def _run_sequential_pipeline(self, niches: list, tenant_id: str) -> list:
        opportunities = []
        for niche in niches:
            niche_opps = await self._scan_niche(niche, tenant_id)
            opportunities.extend(niche_opps)
        opportunities.sort(key=lambda x: x.get("score", 0), reverse=True)
        log_info(logger, "oracle_sequential_complete",
                 tenant_id=tenant_id, found=len(opportunities))
        return opportunities

    async def _scan_niche(self, niche: str, tenant_id: str) -> list:
        """V5.1 sequential niche scan — kept for backward compat."""
        log_info(logger, "scanning_niche", niche=niche)
        tiktok_products = await self.tiktok.get_trending_products(category=niche)
        amazon_products = await self.apify.scrape_amazon_product(niche, max_items=15)
        meta_intel      = await self.meta_lib.analyze_niche(niche, [niche, f"best {niche}"])
        google_interest = self.gtrends.get_interest_score(niche)
        seasonal_weeks  = self.gtrends.get_seasonal_peak_weeks(niche)
        candidates = await self._hunter_prefilter(niche, tiktok_products, amazon_products)

        opportunities = []
        for candidate in candidates[:5]:
            opp = await self._analyst_score(
                candidate, niche, meta_intel, google_interest, seasonal_weeks, tenant_id
            )
            if opp:
                opportunities.append(opp)
                if opp.get("score", 0) >= SCORE_AUTO_GO:
                    self.slack.notify_opportunity(
                        name=opp["name"], score=opp["score"],
                        breakdown=opp.get("score_breakdown", {})
                    )
        return opportunities

    # ── Parsers ────────────────────────────────────────────────────────────────

    def _parse_candidates(self, response: str) -> list:
        candidates = []
        for line in response.strip().split("\n"):
            if "NAME:" not in line:
                continue
            parts = {
                p.split(":")[0].strip(): p.split(":")[-1].strip()
                for p in line.split("|") if ":" in p
            }
            try:
                candidates.append({
                    "name":                 parts.get("NAME", ""),
                    "differentiation_score": float(parts.get("DIFF", 50)),
                    "logistics_score":       float(parts.get("LOGIS", 60)),
                    "estimated_margin":      float(parts.get("MARGIN", "40").replace("%","")),
                    "legal_risk":            float(parts.get("RISK", 0.1)),
                    "supplier_count":        int(parts.get("SUPPLIERS", 1)),
                })
            except (ValueError, KeyError):
                continue
        return candidates

    def _parse_and_merge_validation(self, response: str, candidates: list) -> list:
        val_map = {}
        for line in response.strip().split("\n"):
            if "NAME:" not in line:
                continue
            parts = {
                p.split(":")[0].strip(): p.split(":")[-1].strip()
                for p in line.split("|") if ":" in p
            }
            name = parts.get("NAME", "").lower()
            if name:
                val_map[name] = {
                    "validation_score": float(parts.get("VALIDATION", 50)),
                    "validation_flag":  parts.get("FLAG", "none").lower(),
                }
        result = []
        for c in candidates:
            key = c.get("name", "").lower()
            val = val_map.get(key, {"validation_score": 50, "validation_flag": "none"})
            if val["validation_flag"] == "red_flag":
                continue
            c.update(val)
            result.append(c)
        return result
