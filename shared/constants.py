"""
shared/constants.py — Single source of truth for all system constants.
NEVER use magic numbers in business logic. Import from here.

V4.4 additions:
  SURVIVORSHIP_* — thresholds and bonuses for the survivorship scoring signal.
  NEW_PRODUCT_CAUTION_PENALTY — penalty for untested products (days_active=0).
"""

# ─── SCORING V4.0 ─────────────────────────────────────────────────────────────
SCORE_WEIGHTS = {
    "demand":          0.25,
    "competition_inv": 0.18,
    "margin":          0.22,
    "differentiation": 0.12,
    "logistics":       0.08,
    "viral":           0.15,   # NEW V4.0 — TikTok Viral Score
}
# weights must sum to 1.0 → 0.25+0.18+0.22+0.12+0.08+0.15 = 1.00 ✓

SCORE_RISK_MULTIPLIER       = 20   # legal_risk × 20 penalty
SCORE_SATURATION_MULTIPLIER = 10   # saturation_prob × 10 penalty (NEW V4.0)

SCORE_AUTO_GO       = 85.0   # ≥85 → AUTO_GO (immediate test)
SCORE_MANUAL_REVIEW = 70.0   # 70–84 → MANUAL_REVIEW (Slack gate)
# <70 → SKIP

LEGAL_RISK_HARD_STOP = 0.6   # risk ≥ 0.6 → HARD STOP, never proceed

VIRAL_PENALTY_THRESHOLD = 40.0   # V < 40 → -5 pts
VIRAL_BONUS_THRESHOLD   = 80.0   # V ≥ 80 → flag as strong signal
VIRAL_PENALTY_PTS       = 5.0

# ─── ROAS DECISION RULES (immutable) ─────────────────────────────────────────
ROAS_KILL_THRESHOLD_1 = 1.5    # ROAS < 1.5 AND spend ≥ $50   → AUTO KILL
ROAS_KILL_THRESHOLD_2 = 2.0    # ROAS < 2.0 AND spend ≥ $200  → AUTO KILL
SPEND_KILL_1          = 50.0
SPEND_KILL_2          = 200.0
ROAS_VALIDATED        = 1.5    # ROAS ≥ 1.5 AND spend ≥ $40   → VALIDATED
SPEND_VALIDATED       = 40.0
ROAS_SCALE_META       = 2.5    # + sustained 7 days  → human gate → Meta
DAYS_SCALE_META       = 7
ROAS_SCALE_GOOGLE     = 2.5    # + sustained 14 days → human gate → Google
DAYS_SCALE_GOOGLE     = 14
ROAS_SCALE_AMAZON     = 3.0    # + sustained 30 days → human gate → Amazon/ML
DAYS_SCALE_AMAZON     = 30

# ─── FAIL-FAST BUDGET ────────────────────────────────────────────────────────
FAILFAST_CAP_USD       = 800.0   # Max portfolio spend before first winner
TIKTOK_TEST_BUDGET     = 50.0    # Per-product TikTok validation test
NICHE_MICROTEST_BUDGET = 20.0    # Per complementary product micro-test
NICHE_MAX_COMPLEMENTS  = 8       # Max complementary products per niche

# ─── SATURATION HAZARD (V4.0) ─────────────────────────────────────────────────
SATURATION_WATCH    = 0.20   # 20% hazard → reduce new budget 30%
SATURATION_CAUTION  = 0.40   # 40% hazard → freeze budget increase
SATURATION_EXIT     = 0.60   # 60% hazard → begin exit strategy
SATURATION_HARD_STOP= 0.80   # 80% hazard → stop all new investment

# ─── V4.0 FEATURES ────────────────────────────────────────────────────────────
COMMENT_MINING_CYCLE_DAYS   = 15
COMMENT_MINING_MIN_SALES    = 50
ORGANIC_ENGAGEMENT_GREEN    = 0.05   # ≥5% → proceed to $50 TikTok test
ORGANIC_ENGAGEMENT_RED      = 0.02   # <2% → change hook before spending
PRICE_AB_VARIANTS           = 3
PRICE_AB_DURATION_HOURS     = 72
PRICE_AB_MARGIN_BANDS       = [-0.15, 0.0, +0.15]  # variant multipliers

# ─── HUMAN GATE TIMEOUTS (minutes) ───────────────────────────────────────────
GATE_OPPORTUNITY_MIN  = 30
GATE_BRANDING_MIN     = 10
GATE_SCALE_META_MIN   = 60
GATE_SCALE_GOOGLE_MIN = 60
GATE_SPEND_100_MIN    = 30

# ─── SLACK CHANNELS ───────────────────────────────────────────────────────────
SLACK_OPPORTUNITIES = "#opportunities"
SLACK_APPROVALS     = "#approvals"
SLACK_ALERTS        = "#alerts"
SLACK_MONITORING    = "#monitoring"

# ─── LLM TIERS ────────────────────────────────────────────────────────────────
LLM_TIER_BULK      = "bulk"       # Groq Llama 3.3 70B  (~$0 free)
LLM_TIER_OPS       = "ops"        # Claude Haiku 4.5    ($0.0008/1K)
LLM_TIER_CREATIVE  = "creative"   # GPT-4o Mini         ($0.0015/1K)
LLM_TIER_STRATEGIC = "strategic"  # Claude Sonnet 4     ($0.015/1K)

# ─── CYCLE TIMING ─────────────────────────────────────────────────────────────
MONITORING_CYCLE_HOURS = 6
ORACLE_CYCLE_HOURS     = 6

# ─── DUAL STORE A/B ──────────────────────────────────────────────────────────
DUAL_STORE_MIN_VISITS   = 300    # visits per variant before evaluation
DUAL_STORE_DURATION_H   = 96     # max test duration hours
DUAL_STORE_MIN_UPLIFT   = 0.08   # 8% minimum uplift to declare winner

# ─── HEYGEN AVATAR ────────────────────────────────────────────────────────────
HEYGEN_PLAN_MONTHLY_USD  = 29    # Creator plan cost
HEYGEN_VIDEOS_PER_MONTH  = 30    # ~30 videos of 30 sec per Creator plan
HEYGEN_COST_PER_VIDEO    = 0.97  # $29 / 30 videos
HEYGEN_CTR_UPLIFT_AVG    = 0.37  # +37% CTR vs static creatives (benchmark)

# ─── SAAS PRICING ─────────────────────────────────────────────────────────────
SAAS_COGS_PER_TENANT    = 45     # avg monthly infra+LLM cost per tenant
SAAS_STARTER_PRICE      = 97
SAAS_GROWTH_PRICE       = 297
SAAS_AGENCY_PRICE       = 997

# ─── META AD LIBRARY ──────────────────────────────────────────────────────────
META_AD_SWEET_SPOT_MIN  = 15     # days running — validated but not saturated
META_AD_SWEET_SPOT_MAX  = 45
META_AD_HIGH_COMPETITION = 50    # >50 advertisers = crowded market

# ─── SURVIVORSHIP SCORE (V4.4) ────────────────────────────────────────────────
# survivorship_score = log1p(days_active) * empirical_roas
# Reference values:
#    3d  * 2.0 ROAS  =  2.8   (new, no bonus)
#   14d  * 2.5 ROAS  =  6.8   (early, no bonus)
#   30d  * 2.5 ROAS  =  8.6   → VALIDATED
#   60d  * 3.0 ROAS  = 12.5   → VALIDATED (strong)
#   90d  * 3.5 ROAS  = 16.0   → PROVEN
SURVIVORSHIP_VALIDATED_THRESHOLD = 8.0    # surv >= 8.0  → +3pts
SURVIVORSHIP_PROVEN_THRESHOLD    = 15.0   # surv >= 15.0 → +6pts
SURVIVORSHIP_VALIDATED_BONUS     = 3.0    # pts added when VALIDATED
SURVIVORSHIP_PROVEN_BONUS        = 6.0    # pts added when PROVEN
NEW_PRODUCT_CAUTION_PENALTY      = 2.0    # pts deducted when days_active=0
