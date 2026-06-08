"""
scoring/engine.py — Scoring Engine V4.4

Formula:
  S = (D×0.25 + C×0.18 + M×0.22 + O×0.12 + L×0.08 + V×0.15)
      − (R×20) − (Sr×10) − MetaCompPenalty + ViralAdj + SurvivorshipAdj

  D  = Demand 0-100         (Helium10 + Google Trends)
  C  = Competition⁻¹ 0-100  (lower competition → higher score)
  M  = Margin 0-100         (gross margin %)
  O  = Differentiation 0-100
  L  = Logistics 0-100      (includes Supplier Risk Score penalty)
  V  = Viral Score 0-100    (TikTok viral signal)
  R  = Legal risk 0-1       (HARD STOP if R ≥ 0.6)
  Sr = Saturation prob 0-1  (from Saturation Hazard Model)
  MetaCompPenalty: 2pts (>20 ads) | 5pts (>30 ads) | 10pts (>50 ads)
  SurvivorshipAdj:
    days_active=0          → -2pts NEW_PRODUCT_CAUTION
    surv ≥ 8.0  (≈30d+2.5x) → +3pts SURVIVORSHIP_VALIDATED
    surv ≥ 15.0 (≈90d+3.5x) → +6pts SURVIVORSHIP_PROVEN

Thresholds:  ≥85 AUTO_GO  |  70–84 MANUAL_REVIEW  |  <70 SKIP

Fixes v4.1:
  [FIX 5] saturation_prob ≥ 0.8 → forced SKIP regardless of numeric score.
  [FIX 6] meta_ad_competitor_count now penalizes final score (not just a flag).

V4.4 additions:
  [V4.4-A] SurvivorshipBonus — products with real track record (days_active + empirical_roas)
           earn a score adjustment. New products get a small caution penalty.
           Math: survivorship = log1p(days_active) * empirical_roas
           Does NOT override HARD_STOP or SATURATION_FORCED_SKIP.
  [V4.4-B] days_active and empirical_roas added to ScoreInput (optional, default 0).
           Source: campaigns table in Supabase.
"""

import math
import logging
from dataclasses import dataclass, field
from typing import Optional
from shared.constants import (
    SCORE_WEIGHTS, SCORE_RISK_MULTIPLIER, SCORE_SATURATION_MULTIPLIER,
    SCORE_AUTO_GO, SCORE_MANUAL_REVIEW, LEGAL_RISK_HARD_STOP,
    VIRAL_PENALTY_THRESHOLD, VIRAL_BONUS_THRESHOLD, VIRAL_PENALTY_PTS,
    SURVIVORSHIP_VALIDATED_THRESHOLD, SURVIVORSHIP_PROVEN_THRESHOLD,
    SURVIVORSHIP_VALIDATED_BONUS, SURVIVORSHIP_PROVEN_BONUS,
    NEW_PRODUCT_CAUTION_PENALTY,
)

logger = logging.getLogger(__name__)


# ─── Survivorship math ────────────────────────────────────────────────────────

def compute_survivorship_score(days_active: int, empirical_roas: float) -> float:
    """
    survivorship = log1p(days_active) * empirical_roas

    Examples:
       3d  × ROAS 2.0 →  2.8   (new, unproven)
      14d  × ROAS 2.5 →  6.8   (early signal)
      30d  × ROAS 2.5 →  8.6   → SURVIVORSHIP_VALIDATED (+3pts)
      60d  × ROAS 3.0 → 12.5   (strong)
      90d  × ROAS 3.5 → 16.0   → SURVIVORSHIP_PROVEN (+6pts)
    """
    if days_active <= 0 or empirical_roas <= 0:
        return 0.0
    return math.log1p(float(days_active)) * float(empirical_roas)


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class ScoreInput:
    """All inputs to the V4.4 scoring formula."""
    name:  str
    niche: str

    # Core dimensions (0-100)
    demand:          float  # search volume + growth velocity
    competition_inv: float  # 100 = no competition, 0 = saturated
    margin:          float  # (price - cogs) / price * 100
    differentiation: float  # unique angle vs competitors
    logistics:       float  # logistics ease (raw, before supplier penalty)

    # V4.0 additions
    viral_score:     float = 50.0  # TikTok viral signal 0-100
    legal_risk:      float = 0.0   # 0=safe, 1=forbidden → HARD STOP if >=0.6
    saturation_prob: float = 0.0   # from SaturationHazardModel (0-1)

    # Modifiers
    supplier_count:           int   = 1    # 1 supplier → -15 pts on logistics
    meta_ad_competitor_count: int   = 0    # from Meta Ad Library scan

    # V4.4 — Survivorship inputs [V4.4-B]
    # Source: campaigns table (days since first ad spend, average observed ROAS)
    # Leave at defaults (0) for brand-new / untested products.
    days_active:    int   = 0    # calendar days since first ad spend
    empirical_roas: float = 0.0  # observed ROAS from real campaign data (0 = unknown)

    # Context (not used in formula, kept for DB/reporting)
    price_usd: Optional[float] = None
    cogs_usd:  Optional[float] = None


@dataclass
class ScoreResult:
    """Output of the scoring engine."""
    final_score:  float
    decision:     str           # AUTO_GO | MANUAL_REVIEW | SKIP | HARD_STOP
    breakdown:    dict
    explanation:  str
    flags:        list = field(default_factory=list)


# ─── Engine ───────────────────────────────────────────────────────────────────

class ScoringEngine:
    """
    Computes V4.4 product opportunity score.
    Uses LLM only for MANUAL_REVIEW borderline cases (Sonnet strategic analysis).
    """

    def __init__(self, llm_router=None):
        self.router = llm_router  # Optional; injected by callers

    def score(self, inp: ScoreInput) -> ScoreResult:
        """Synchronous score — no LLM calls."""
        flags = []

        # ── HARD STOP ─────────────────────────────────────────────────────────
        if inp.legal_risk >= LEGAL_RISK_HARD_STOP:
            logger.warning("hard_stop: %s legal_risk=%.2f", inp.name, inp.legal_risk)
            return ScoreResult(
                final_score=0.0, decision="HARD_STOP",
                breakdown={"legal_risk": inp.legal_risk},
                explanation=(
                    f"Legal risk {inp.legal_risk:.2f} >= {LEGAL_RISK_HARD_STOP} threshold. "
                    f"Possible trademark/FDA/IP issue. Never proceed."
                ),
                flags=["LEGAL_HARD_STOP"],
            )

        # ── Logistics: supplier penalty ────────────────────────────────────────
        logistics = inp.logistics
        if inp.supplier_count == 1:
            logistics = max(0.0, logistics - 15.0)
            flags.append("SINGLE_SUPPLIER_PENALTY: logistics -15pts (stock-out risk)")

        # ── Viral: bonus / penalty ─────────────────────────────────────────────
        viral_adj = 0.0
        if inp.viral_score < VIRAL_PENALTY_THRESHOLD:
            viral_adj = -VIRAL_PENALTY_PTS
            flags.append(
                f"VIRAL_PENALTY: V={inp.viral_score:.0f} < {VIRAL_PENALTY_THRESHOLD}"
                f" -> -{VIRAL_PENALTY_PTS}pts"
            )
        elif inp.viral_score >= VIRAL_BONUS_THRESHOLD:
            flags.append(
                f"VIRAL_STRONG: V={inp.viral_score:.0f} >= {VIRAL_BONUS_THRESHOLD}"
                f" -- strong TikTok signal"
            )

        # ── Meta Ad Library competition signal [FIX 6] ───────────────────────
        meta_competition_penalty = 0.0
        if inp.meta_ad_competitor_count > 50:
            meta_competition_penalty = 10.0
            flags.append(
                f"HIGH_META_COMPETITION: {inp.meta_ad_competitor_count} advertisers"
                f" -> -{meta_competition_penalty:.0f}pts (extreme saturation)"
            )
        elif inp.meta_ad_competitor_count > 30:
            meta_competition_penalty = 5.0
            flags.append(
                f"HIGH_META_COMPETITION: {inp.meta_ad_competitor_count} advertisers"
                f" -> -{meta_competition_penalty:.0f}pts (very competitive)"
            )
        elif inp.meta_ad_competitor_count > 20:
            meta_competition_penalty = 2.0
            flags.append(
                f"HIGH_META_COMPETITION: {inp.meta_ad_competitor_count} advertisers"
                f" -> -{meta_competition_penalty:.0f}pts (competitive)"
            )

        # ── Survivorship bonus/penalty [V4.4-A] ──────────────────────────────
        survivorship_adj = 0.0
        surv_score = compute_survivorship_score(inp.days_active, inp.empirical_roas)

        if inp.days_active == 0:
            # No real data yet: apply caution penalty to favor products with track record
            survivorship_adj = -NEW_PRODUCT_CAUTION_PENALTY
            flags.append(
                f"NEW_PRODUCT_CAUTION: days_active=0, no empirical ROAS"
                f" -> -{NEW_PRODUCT_CAUTION_PENALTY}pts (unproven)"
            )
        elif surv_score >= SURVIVORSHIP_PROVEN_THRESHOLD:
            survivorship_adj = SURVIVORSHIP_PROVEN_BONUS
            flags.append(
                f"SURVIVORSHIP_PROVEN: {inp.days_active}d x ROAS {inp.empirical_roas:.2f}"
                f" = {surv_score:.1f} >= {SURVIVORSHIP_PROVEN_THRESHOLD}"
                f" -> +{SURVIVORSHIP_PROVEN_BONUS}pts (battle-tested)"
            )
        elif surv_score >= SURVIVORSHIP_VALIDATED_THRESHOLD:
            survivorship_adj = SURVIVORSHIP_VALIDATED_BONUS
            flags.append(
                f"SURVIVORSHIP_VALIDATED: {inp.days_active}d x ROAS {inp.empirical_roas:.2f}"
                f" = {surv_score:.1f} >= {SURVIVORSHIP_VALIDATED_THRESHOLD}"
                f" -> +{SURVIVORSHIP_VALIDATED_BONUS}pts (validated)"
            )
        elif inp.days_active > 0 and inp.empirical_roas > 0:
            # Has real data but below validation threshold — informational only
            flags.append(
                f"SURVIVORSHIP_EARLY: {inp.days_active}d x ROAS {inp.empirical_roas:.2f}"
                f" = {surv_score:.1f} (accumulating data, threshold={SURVIVORSHIP_VALIDATED_THRESHOLD})"
            )

        # ── Core formula ──────────────────────────────────────────────────────
        w = SCORE_WEIGHTS
        weighted_sum = (
            inp.demand          * w["demand"]          +
            inp.competition_inv * w["competition_inv"] +
            inp.margin          * w["margin"]          +
            inp.differentiation * w["differentiation"] +
            logistics           * w["logistics"]       +
            inp.viral_score     * w["viral"]
        )

        risk_pen = inp.legal_risk      * SCORE_RISK_MULTIPLIER
        sat_pen  = inp.saturation_prob * SCORE_SATURATION_MULTIPLIER
        final    = max(0.0, min(100.0,
            weighted_sum
            - risk_pen
            - sat_pen
            + viral_adj
            - meta_competition_penalty
            + survivorship_adj
        ))

        # ── Saturation flags & forced SKIP [FIX 5] ───────────────────────────
        # >= 0.8 saturation overrides any numeric score → always SKIP.
        if inp.saturation_prob >= 0.8:
            flags.append(
                f"SATURATION_FORCED_SKIP: {inp.saturation_prob:.0%} -- market is dying,"
                f" do not enter regardless of score"
            )
            decision = "SKIP"
            logger.warning(
                "saturation_forced_skip product=%r saturation=%.0f%% raw_score=%.1f",
                inp.name, inp.saturation_prob * 100, final
            )
        elif inp.saturation_prob >= 0.6:
            flags.append(f"SATURATION_EXIT_SIGNAL: {inp.saturation_prob:.0%} -- begin wind-down")
            if final >= SCORE_AUTO_GO:
                decision = "AUTO_GO"
            elif final >= SCORE_MANUAL_REVIEW:
                decision = "MANUAL_REVIEW"
            else:
                decision = "SKIP"
        else:
            if inp.saturation_prob >= 0.4:
                flags.append(f"SATURATION_CAUTION: {inp.saturation_prob:.0%} -- watch closely")
            if final >= SCORE_AUTO_GO:
                decision = "AUTO_GO"
            elif final >= SCORE_MANUAL_REVIEW:
                decision = "MANUAL_REVIEW"
            else:
                decision = "SKIP"

        # ── Breakdown ─────────────────────────────────────────────────────────
        breakdown = {
            "demand":                   round(inp.demand          * w["demand"],          2),
            "competition":              round(inp.competition_inv * w["competition_inv"], 2),
            "margin":                   round(inp.margin          * w["margin"],          2),
            "differentiation":          round(inp.differentiation * w["differentiation"], 2),
            "logistics":                round(logistics           * w["logistics"],       2),
            "viral":                    round(inp.viral_score     * w["viral"],           2),
            "weighted_sum":             round(weighted_sum, 2),
            "risk_penalty":             round(risk_pen, 2),
            "sat_penalty":              round(sat_pen, 2),
            "viral_adj":                round(viral_adj, 2),
            "meta_competition_penalty": round(meta_competition_penalty, 2),
            "survivorship_adj":         round(survivorship_adj, 2),
            "survivorship_score_raw":   round(surv_score, 3),
            "final":                    round(final, 2),
        }

        explanation = (
            f"Score {final:.1f}/100 -> {decision} | "
            f"Top: demand({breakdown['demand']:.1f})"
            f" + margin({breakdown['margin']:.1f})"
            f" + viral({breakdown['viral']:.1f})"
        )
        if risk_pen > 0:
            explanation += f" | Risk penalty -{risk_pen:.1f}"
        if sat_pen > 0:
            explanation += f" | Saturation penalty -{sat_pen:.1f}"
        if meta_competition_penalty > 0:
            explanation += f" | Meta competition penalty -{meta_competition_penalty:.1f}"
        if survivorship_adj != 0:
            sign = "+" if survivorship_adj > 0 else ""
            explanation += f" | Survivorship {sign}{survivorship_adj:.1f}"

        logger.info("scored: %s score=%.2f decision=%s", inp.name, final, decision)
        return ScoreResult(
            final_score=round(final, 2), decision=decision,
            breakdown=breakdown, explanation=explanation, flags=flags,
        )

    async def async_score(self, inp: ScoreInput) -> ScoreResult:
        """
        Score with optional LLM assist for MANUAL_REVIEW cases.
        Groq pre-screens; Sonnet only for borderline 70-84 scores.
        Inputs sanitized before LLM interpolation [FIX 3].
        """
        from shared.security import sanitize_llm_input  # [FIX 3] prompt injection guard

        result = self.score(inp)
        if result.decision == "MANUAL_REVIEW" and self.router:
            try:
                clean_name  = sanitize_llm_input(inp.name,  max_length=120, field_name="product_name")
                clean_niche = sanitize_llm_input(inp.niche, max_length=80,  field_name="product_niche")
                surv_ctx = (
                    f"days_active={inp.days_active} empirical_roas={inp.empirical_roas:.2f}"
                    if inp.days_active > 0 else "new product, no empirical data"
                )
                analysis = await self.router.route(
                    "strategic",
                    f"""Product: {clean_name} | Niche: {clean_niche}
Score: {result.final_score:.1f}/100 (MANUAL_REVIEW -- borderline)
Breakdown: demand={result.breakdown['demand']:.1f} competition={result.breakdown['competition']:.1f} margin={result.breakdown['margin']:.1f} viral={result.breakdown['viral']:.1f}
Survivorship: {surv_ctx} (adj={result.breakdown['survivorship_adj']:+.1f}pts)
Flags: {', '.join(result.flags) if result.flags else 'none'}

Should we APPROVE or REJECT this product for a $50 TikTok test?
Consider: margin sustainability with ads, competition manageability, viral signal quality, track record.
Answer: APPROVE or REJECT + 2-sentence reasoning.""",
                    system="Ecommerce investment expert. Be direct and brief.",
                )
                result.explanation += f"\n\nLLM: {analysis.strip()}"
            except Exception as e:
                logger.warning("LLM scoring assist failed: %s", e)
        return result
