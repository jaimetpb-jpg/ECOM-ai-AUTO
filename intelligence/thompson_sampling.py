"""
intelligence/thompson_sampling.py — Thompson Sampling Bandit Allocator V4.1

Fixes aplicados (revisión crítica):
  [1] CRÍTICO: Softmax numéricamente estable — temperatura fija τ, restar max, clip overflow
  [2] CRÍTICO: Edge cases / división por cero — guards en todos los paths
  [3] MEDIO:   Priors configurables por productStats (prior_alpha, prior_beta)
  [4] MEDIO:   Thread-safety en ProductStats.update() con Lock
  [5] ALTO:    Logging estructurado con componentes del score para calibración

Math:
  Prior: Beta(α_0, β_0) — configurable, default (1, 1) uniforme
  Update: α = α_0 + clicks; β = β_0 + (impressions - clicks)
  Sample: CTR ~ Beta(α, β) — n_samples Monte Carlo
  Score: E[profit/1k impr] = sampled_CTR × CR × AOV × margin − cost
  Allocate: stable_softmax(scores, τ=0.5) → probabilities → budgets
"""

import math
import random
import logging
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Numerically stable softmax ───────────────────────────────────────────────
def stable_softmax(scores: list, tau: float = 0.5) -> list:
    """
    Numerically stable softmax with temperature τ.

    Fixes [1]:
    - Temperatura FIJA τ (no scores.std() que puede ser ~0)
    - Subtract max before exp to prevent overflow
    - eps denominator to prevent division by zero
    - Returns uniform if all scores zero/negative

    τ < 1.0 → more exploitation (winner gets more budget)
    τ > 1.0 → more exploration (more uniform distribution)
    τ = 0.5 → good default for DTC ecommerce (favor winners but keep exploring)
    """
    if not scores:
        return []

    n = len(scores)
    tau = max(1e-6, tau)

    # Scale by temperature
    scaled = [s / tau for s in scores]

    # If all non-positive: return uniform (no winner yet, explore equally)
    if all(s <= 0 for s in scaled):
        return [1.0 / n] * n

    # Subtract max for numerical stability (prevents exp overflow)
    max_s  = max(scaled)
    exp_s  = [math.exp(min(700.0, s - max_s)) for s in scaled]  # clip at 700 to prevent inf
    total  = sum(exp_s) + 1e-12  # eps prevents div/0
    return [e / total for e in exp_s]


@dataclass
class ProductStats:
    """
    Bayesian state for one product arm.

    Fixes [3] + [4]:
    - prior_alpha / prior_beta configurable (default Beta(1,1) = uniform)
    - update() is thread-safe via Lock
    - Use conservative prior Beta(1,9) for new niches with expected low CTR

    Example conservative priors:
      ProductStats("p1","c1", prior_alpha=1.0, prior_beta=9.0)  # expect ~10% CTR
      ProductStats("p1","c1", prior_alpha=1.0, prior_beta=1.0)  # uniform (default)
    """
    product_id:   str
    campaign_id:  str

    # Cumulative performance counters
    impressions:  int   = 0
    clicks:       int   = 0
    conversions:  int   = 0
    spend:        float = 0.0
    revenue:      float = 0.0

    # Configurable Bayesian priors [FIX 3]
    prior_alpha:  float = 1.0
    prior_beta:   float = 1.0

    # Derived Beta distribution params (recalculated on update)
    alpha:        float = field(init=False)
    beta:         float = field(init=False)

    # External signals
    saturation_score: float = 0.0
    supplier_risk:    float = 0.0

    # Internal
    _blocked: bool  = field(default=False, repr=False)
    _lock:    Lock  = field(default_factory=Lock, repr=False, compare=False)

    def __post_init__(self):
        self.alpha = self.prior_alpha
        self.beta  = self.prior_beta

    def update(
        self,
        impressions: int   = 0,
        clicks:      int   = 0,
        conversions: int   = 0,
        spend:       float = 0.0,
        revenue:     float = 0.0,
    ):
        """
        Incremental Bayesian update. Thread-safe via Lock [FIX 4].
        Call after each 6h monitoring data refresh.
        """
        with self._lock:
            self.impressions  += max(0, impressions)
            self.clicks       += max(0, min(clicks, impressions))  # clicks can't exceed impressions
            self.conversions  += max(0, conversions)
            self.spend        += max(0.0, spend)
            self.revenue      += max(0.0, revenue)
            # Posterior update: Beta(α_0 + clicks, β_0 + non-clicks)
            non_clicks = max(0, self.impressions - self.clicks)
            self.alpha = self.prior_alpha + self.clicks
            self.beta  = self.prior_beta  + non_clicks

    @property
    def empirical_ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions > 0 else 0.0

    @property
    def empirical_cr(self) -> float:
        return self.conversions / self.clicks if self.clicks > 0 else 0.0

    @property
    def empirical_roas(self) -> float:
        return self.revenue / self.spend if self.spend > 0 else 0.0

    @property
    def posterior_mean_ctr(self) -> float:
        """E[CTR] from Beta posterior = α / (α + β)."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def data_confidence(self) -> float:
        """0–1. Low = explore more. Calibrated to 2000 impressions = full confidence."""
        return min(1.0, self.impressions / 2000.0)


class ThompsonSamplingAllocator:
    """
    Multi-armed bandit budget allocator with all critical fixes applied.

    Calibrate after 10+ campaigns:
      default_cr    = 0.02   (2% conversion rate)
      default_aov   = $39.99 (average order value)
      default_cpc   = $0.25  (cost per click)
      stoploss_roas = 1.0    (block products below ROAS)
      min_budget    = $5.00  (minimum per arm)
      softmax_tau   = 0.5    (temperature: lower = more exploitation)
    """

    def __init__(
        self,
        default_cr:     float = 0.02,
        default_aov:    float = 39.99,
        default_margin: float = 0.40,
        default_cpc:    float = 0.25,
        stoploss_roas:  float = 1.0,
        min_budget:     float = 5.0,
        n_samples:      int   = 30,
        softmax_tau:    float = 0.5,   # [FIX 1]: fixed temperature, not scores.std()
    ):
        self.default_cr     = default_cr
        self.default_aov    = default_aov
        self.default_margin = default_margin
        self.default_cpc    = default_cpc
        self.stoploss_roas  = stoploss_roas
        self.min_budget     = min_budget
        self.n_samples      = n_samples
        self.softmax_tau    = softmax_tau

    def allocate(
        self,
        products: list,
        total_budget: float,
    ) -> dict:
        """
        Compute optimal budget allocation across products.
        Returns: {product_id: allocated_usd}

        Fix [2]: All edge cases handled — no crashes on empty/all-blocked input.
        """
        # [FIX 2]: Guard empty input
        if not products:
            logger.warning("allocate called with empty products list")
            return {}

        if total_budget <= 0:
            logger.warning(f"allocate called with invalid budget={total_budget}")
            return {}

        # ── 1. Apply stop-loss ─────────────────────────────────────────────────
        feasible, blocked_ids = [], []
        for p in products:
            if p.empirical_roas > 0 and p.empirical_roas < self.stoploss_roas:
                p._blocked = True
                blocked_ids.append(p.product_id)
                logger.info(
                    f"stop_loss_blocked product={p.product_id} "
                    f"roas={p.empirical_roas:.3f} threshold={self.stoploss_roas}"
                )
            else:
                p._blocked = False
                feasible.append(p)

        if not feasible:
            # [FIX 2]: All blocked — allocate nothing (don't crash)
            logger.warning(
                f"all_products_blocked count={len(blocked_ids)} "
                f"returning empty allocation — caller should review campaigns"
            )
            return {p.product_id: 0.0 for p in products}

        # ── 2. Score each arm ──────────────────────────────────────────────────
        ids, raw_scores, score_components = [], [], []
        for p in feasible:
            # [FIX 5]: Collect all components for structured logging
            sampled_ctrs = [random.betavariate(p.alpha, p.beta) for _ in range(self.n_samples)]
            sampled_ctr  = sum(sampled_ctrs) / self.n_samples

            cr     = p.empirical_cr   if p.empirical_cr   > 0 else self.default_cr
            margin = p.empirical_roas if p.empirical_roas > 0 else self.default_margin

            clicks_per_1k = sampled_ctr * 1000
            revenue_1k    = clicks_per_1k * cr * self.default_aov * margin
            cost_1k       = clicks_per_1k * self.default_cpc
            profit_1k     = revenue_1k - cost_1k

            # Variance penalty: encourages exploration of new arms
            # [FIX 2]: max(1, ...) prevents sqrt(0)
            variance_pen  = 1.0 / (math.sqrt(max(1, p.impressions)) + 1.0)
            sat_pen       = p.saturation_score * 10.0

            score = profit_1k - variance_pen * 5.0 - sat_pen
            # Note: don't clip to 0 here — let softmax handle negative scores naturally

            ids.append(p.product_id)
            raw_scores.append(score)
            score_components.append({
                "product_id":   p.product_id,
                "sampled_ctr":  round(sampled_ctr, 5),
                "profit_1k":    round(profit_1k, 2),
                "variance_pen": round(variance_pen, 4),
                "sat_pen":      round(sat_pen, 4),
                "final_score":  round(score, 4),
                "data_pts":     p.impressions,
                "post_mean_ctr":round(p.posterior_mean_ctr, 5),
            })

        # [FIX 5]: Structured log for calibration / Grafana / Metabase
        logger.debug(
            f"thompson_scores product_count={len(ids)} "
            f"budget={total_budget:.0f} components={score_components}"
        )

        # ── 3. V5.0 ENHANCEMENT: Tie-Breaking ─────────────────────────────────
        # When multiple arms have similar scores (within 5%), prefer:
        # 1. Arms with more data (higher confidence)
        # 2. Random exploration among inexperienced arms
        probs = self._allocate_with_tie_breaking(
            ids, raw_scores, products_dict=products, tau=self.softmax_tau
        )

        # [FIX 2]: Verify probs sum is valid
        prob_sum = sum(probs)
        if prob_sum <= 0 or not all(math.isfinite(p) for p in probs):
            logger.warning("softmax_degenerate — falling back to equal split")
            probs = [1.0 / len(ids)] * len(ids)

        alloc = {pid: total_budget * prob for pid, prob in zip(ids, probs)}

        # ── 4. Enforce minimum budget ──────────────────────────────────────────
        for pid in list(alloc):
            if alloc[pid] < self.min_budget:
                alloc[pid] = self.min_budget

        # Re-normalize to stay within budget
        alloc_total = sum(alloc.values())
        if alloc_total > total_budget:
            # [FIX 2]: alloc_total guaranteed > 0 (at least min_budget * 1 arm)
            factor = total_budget / alloc_total
            alloc  = {k: round(v * factor, 2) for k, v in alloc.items()}

        # Zero-out blocked products
        for pid in blocked_ids:
            alloc[pid] = 0.0

        logger.info(
            f"allocation_complete total={total_budget:.0f} "
            f"feasible={len(feasible)} blocked={len(blocked_ids)} "
            f"allocations={[(k, round(v,2)) for k,v in alloc.items()]}"
        )
        return alloc
    
    def _allocate_with_tie_breaking(
        self, 
        ids: list, 
        raw_scores: list, 
        products_dict: list,
        tau: float
    ) -> list:
        """
        V5.0 ENHANCEMENT: Thompson Sampling with intelligent tie-breaking.
        
        Problem: When arms have similar scores (within 5%), pure softmax is volatile.
        Solution: 
        - Detect ties (scores within 5% of best)
        - Among ties, prefer arms with MORE data (higher confidence)
        - If all tied arms are inexperienced (<50 impressions), explore randomly
        
        Args:
            ids: Product IDs
            raw_scores: Raw profit scores per product
            products_dict: List of ProductStats objects
            tau: Softmax temperature
        
        Returns:
            probs: Allocation probabilities (sum to 1.0)
        """
        if not raw_scores:
            return []
        
        # Find best score
        max_score = max(raw_scores)
        
        # Detect ties (within 5% of best)
        tie_threshold = 0.05
        ties_indices = [
            i for i, score in enumerate(raw_scores)
            if abs(score - max_score) / max(abs(max_score), 0.01) < tie_threshold
        ]
        
        if len(ties_indices) <= 1:
            # No ties - use standard softmax
            return stable_softmax(raw_scores, tau)
        
        # Multiple ties detected
        # Build product lookup
        product_map = {p.product_id: p for p in products_dict}
        
        # Among tied arms, check experience levels
        tied_products = [product_map[ids[i]] for i in ties_indices]
        experienced_ties = [
            p for p in tied_products
            if p.impressions >= 50  # Min threshold for "experienced"
        ]
        
        if experienced_ties:
            # Prefer most experienced among ties
            most_experienced = max(experienced_ties, key=lambda p: p.impressions)
            
            # Boost the most experienced arm's score slightly
            for i in ties_indices:
                if ids[i] == most_experienced.product_id:
                    raw_scores[i] *= 1.1  # 10% boost
                    logger.debug(
                        f"tie_breaking_boost product={most_experienced.product_id} "
                        f"impressions={most_experienced.impressions} among {len(ties_indices)} ties"
                    )
                    break
        else:
            # All tied arms are inexperienced - add random exploration noise
            logger.debug(
                f"tie_breaking_explore tied_arms={len(ties_indices)} all_inexperienced=True"
            )
            for i in ties_indices:
                # Add small random noise to break symmetry
                raw_scores[i] += random.uniform(-0.5, 0.5)
        
        # Now apply softmax with adjusted scores
        return stable_softmax(raw_scores, tau)
