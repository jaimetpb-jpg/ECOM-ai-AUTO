"""
core/decision_engine.py — Central Decision Engine V5.1

Motor de decisiones central que evalúa productos individuales y portfolios
completos usando Thompson Sampling V5.0 con tie-breaking inteligente.

Sinergia:
  - Safe math:          ChatGPT (reward = 0.6*conv + 0.3*rev + 0.1*eng)
  - Portfolio theory:   ChatGPT (diversificación por nicho)
  - Thompson allocator: V5.0 (tie-breaking + stable softmax)
  - Models:             shared/models.py (ProductEvaluation typed output)
  - Logging:            V4.5 (structured logs para Metabase)

Lógica de decisión:
  score < 55  → reject (no hay suficiente señal)
  55-69       → manual_review (Slack gate — bajo riesgo)
  70-84       → launch_test ($50 TikTok test con Thompson allocation)
  >= 85       → launch_test AUTO_GO ($50 test inmediato)

Reward económico real (ChatGPT fix):
  reward = 0.6 * conversion_rate
         + 0.3 * (revenue_per_click / max_revenue)
         + 0.1 * engagement_score
  Más realista que usar solo conversion_rate.

Diversificación de portfolio:
  Máx 2 productos por nicho activos simultáneamente
  (evitar sobreconcentración en un solo mercado)
"""

import logging
from typing import Dict, Any, List, Optional

from intelligence.thompson_sampling import ThompsonSamplingAllocator, ProductStats
from shared.models import ProductEvaluation
from shared.logging_utils import log_info, log_error
from shared.constants import (
    SCORE_AUTO_GO, SCORE_MANUAL_REVIEW,
    TIKTOK_TEST_BUDGET, FAILFAST_CAP_USD,
)

logger = logging.getLogger(__name__)

# Thresholds de decisión
SCORE_REJECT_THRESHOLD = 55.0   # < 55 → rechazar sin más análisis
MAX_PRODUCTS_PER_NICHE = 2      # máx simultáneos por nicho (diversificación)


class DecisionEngine:
    """
    Motor de decisiones central del sistema.

    Evalúa productos usando scoring + Thompson Sampling para determinar
    si lanzar un test de $50, enviar a revisión humana o rechazar.

    Example:
        engine = DecisionEngine()

        # Evaluar un producto
        result = engine.evaluate_product({
            "product_id": "prod_123",
            "name": "Masajeador cervical",
            "niche": "salud",
            "final_score": 78.5,
            "viral_score": 82.0,
            "days_active": 0,
            "empirical_roas": 0.0,
        })
        # → ProductEvaluation(decision='launch_test', budget_usd=50.0, ...)

        # Evaluar portfolio completo
        results = engine.evaluate_portfolio(products_list, total_budget=500.0)
    """

    def __init__(
        self,
        allocator: Optional[ThompsonSamplingAllocator] = None,
        default_test_budget: float = TIKTOK_TEST_BUDGET,
    ):
        # V5.0 Thompson Sampling con tie-breaking integrado
        self.allocator = allocator or ThompsonSamplingAllocator(
            default_cr=0.02,
            default_aov=39.99,
            default_margin=0.40,
            default_cpc=0.25,
            stoploss_roas=1.0,
            min_budget=5.0,
            n_samples=30,
            softmax_tau=0.5,
        )
        self.default_test_budget = default_test_budget

    def evaluate_product(self, product: Dict[str, Any]) -> ProductEvaluation:
        """
        Evaluar un producto individual y decidir su destino.

        Args:
            product: Dict con keys:
                product_id (str)       — identificador único
                name (str)             — nombre del producto
                niche (str)            — nicho de mercado
                final_score (float)    — score del ScoringEngine (0-100)
                viral_score (float)    — señal viral TikTok (0-100)
                days_active (int)      — días con campañas activas
                empirical_roas (float) — ROAS observado (0 si nuevo)
                impressions (int)      — impressiones acumuladas
                clicks (int)           — clicks acumulados
                conversions (int)      — conversiones

        Returns:
            ProductEvaluation con decision, budget_usd, confidence
        """
        product_id = str(product.get("product_id", ""))
        name = str(product.get("name", "unknown"))
        niche = str(product.get("niche", ""))
        score = float(product.get("final_score", 0.0))

        # ── Regla 1: Reject bajo score ────────────────────────────────────────
        if score < SCORE_REJECT_THRESHOLD:
            log_info(logger, "product_rejected_low_score",
                     product_id=product_id, name=name, score=score)
            return ProductEvaluation(
                decision="reject",
                reason=f"Score {score:.1f} < {SCORE_REJECT_THRESHOLD} — señal insuficiente",
                score=score,
                budget_usd=0.0,
                confidence=score / 100.0,
                product_id=product_id,
                product_name=name,
            )

        # ── Regla 2: Manual review en zona gris ───────────────────────────────
        if SCORE_REJECT_THRESHOLD <= score < SCORE_MANUAL_REVIEW:
            log_info(logger, "product_manual_review",
                     product_id=product_id, name=name, score=score)
            return ProductEvaluation(
                decision="manual_review",
                reason=(
                    f"Score {score:.1f} en zona MANUAL_REVIEW "
                    f"({SCORE_REJECT_THRESHOLD}-{SCORE_MANUAL_REVIEW}) — "
                    f"requiere aprobación Slack"
                ),
                score=score,
                budget_usd=self.default_test_budget,  # Presupuesto tentativo
                confidence=score / 100.0,
                product_id=product_id,
                product_name=name,
            )

        # ── Regla 3: Launch test (score >= 70) ───────────────────────────────
        budget = self._compute_test_budget(product, score)

        # Calcular reward económico real (ChatGPT fix)
        reward = self._compute_economic_reward(product)

        confidence = min(1.0, score / 100.0)

        auto_go = score >= SCORE_AUTO_GO

        reason = (
            f"Score {score:.1f} >= {SCORE_AUTO_GO} — AUTO_GO inmediato"
            if auto_go else
            f"Score {score:.1f} en rango LAUNCH_TEST ({SCORE_MANUAL_REVIEW}-{SCORE_AUTO_GO}) — test ${budget:.0f}"
        )

        log_info(logger, "product_approved_for_test",
                 product_id=product_id, name=name, score=score,
                 budget_usd=budget, auto_go=auto_go,
                 economic_reward=round(reward, 4))

        return ProductEvaluation(
            decision="launch_test",
            reason=reason,
            score=score,
            budget_usd=budget,
            confidence=confidence,
            product_id=product_id,
            product_name=name,
        )

    def evaluate_portfolio(
        self,
        products: List[Dict[str, Any]],
        total_budget: float = FAILFAST_CAP_USD,
    ) -> Dict[str, Any]:
        """
        Evaluar múltiples productos y distribuir presupuesto.

        Aplica:
        1. Evaluación individual de cada producto
        2. Filtro de diversificación (máx 2 por nicho)
        3. Thompson Sampling para distribución de presupuesto
           entre los aprobados

        Args:
            products: Lista de productos a evaluar
            total_budget: Presupuesto total disponible (default $800 failfast)

        Returns:
            Dict con:
              approved: List[ProductEvaluation]
              rejected: List[ProductEvaluation]
              manual_review: List[ProductEvaluation]
              budget_allocation: {product_id: budget_usd}
              total_budget_allocated: float
              summary: str
        """
        if not products:
            return {
                "approved": [], "rejected": [], "manual_review": [],
                "budget_allocation": {}, "total_budget_allocated": 0.0,
                "summary": "No products to evaluate",
            }

        # ── Paso 1: Evaluación individual ─────────────────────────────────────
        evaluations = []
        for p in products:
            try:
                ev = self.evaluate_product(p)
                evaluations.append(ev)
            except Exception as e:
                log_error(logger, "portfolio_product_eval_failed",
                          product_id=p.get("product_id", "?"), error=str(e))

        approved = [e for e in evaluations if e.decision == "launch_test"]
        rejected = [e for e in evaluations if e.decision == "reject"]
        manual = [e for e in evaluations if e.decision == "manual_review"]

        # ── Paso 2: Diversificación por nicho ────────────────────────────────
        products_by_id = {str(p.get("product_id", "")): p for p in products}
        approved_diversified = self._apply_niche_diversification(
            approved, products_by_id
        )

        # ── Paso 3: Thompson Sampling para distribución de presupuesto ────────
        budget_allocation = {}
        total_allocated = 0.0

        if approved_diversified and total_budget > 0:
            # Construir ProductStats para los aprobados
            product_stats = []
            for ev in approved_diversified:
                p = products_by_id.get(ev.product_id, {})
                ps = ProductStats(
                    product_id=ev.product_id,
                    campaign_id=p.get("campaign_id", ev.product_id),
                    impressions=int(p.get("impressions", 0)),
                    clicks=int(p.get("clicks", 0)),
                    conversions=int(p.get("conversions", 0)),
                    spend=float(p.get("spend_usd", 0.0)),
                    revenue=float(p.get("revenue_usd", 0.0)),
                    prior_alpha=1.0,
                    prior_beta=1.0,
                )
                product_stats.append(ps)

            try:
                budget_allocation = self.allocator.allocate(
                    products=product_stats,
                    total_budget=total_budget,
                )
                total_allocated = sum(budget_allocation.values())
            except Exception as e:
                log_error(logger, "portfolio_thompson_allocation_failed", error=str(e))
                # Fallback: distribuir uniformemente
                per_product = total_budget / len(approved_diversified)
                budget_allocation = {e.product_id: per_product for e in approved_diversified}
                total_allocated = total_budget

        summary = (
            f"{len(approved_diversified)} aprobados / "
            f"{len(rejected)} rechazados / "
            f"{len(manual)} revisión manual — "
            f"${total_allocated:.0f} asignados de ${total_budget:.0f}"
        )

        log_info(logger, "portfolio_evaluated",
                 total=len(products),
                 approved=len(approved_diversified),
                 rejected=len(rejected),
                 manual_review=len(manual),
                 budget_allocated=round(total_allocated, 2))

        return {
            "approved": [e.model_dump() for e in approved_diversified],
            "rejected": [e.model_dump() for e in rejected],
            "manual_review": [e.model_dump() for e in manual],
            "budget_allocation": budget_allocation,
            "total_budget_allocated": round(total_allocated, 2),
            "summary": summary,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    def _compute_test_budget(
        self, product: Dict[str, Any], score: float
    ) -> float:
        """
        Calcular presupuesto de test basado en score y experiencia previa.

        Nuevo producto (days_active=0): $50 (failfast mínimo)
        Producto validado (ROAS > 0):   $50 base + scaling por ROAS
        """
        days_active = int(product.get("days_active", 0))
        empirical_roas = float(product.get("empirical_roas", 0.0))

        base = self.default_test_budget  # $50

        if days_active > 0 and empirical_roas >= 2.5:
            # Producto probado con buen ROAS → test más generoso
            return min(base * 2, 100.0)  # Máx $100 para test

        return base

    def _compute_economic_reward(self, product: Dict[str, Any]) -> float:
        """
        Reward económico real (ChatGPT fix).

        reward = 0.6 * conversion_rate
               + 0.3 * (revenue_per_click / max_revenue_per_click)
               + 0.1 * engagement_score

        Más robusto que usar solo conversion_rate como proxy de valor.
        """
        clicks = float(product.get("clicks", 0))
        conversions = float(product.get("conversions", 0))
        revenue = float(product.get("revenue_usd", 0.0))
        impressions = float(product.get("impressions", 0))
        viral_score = float(product.get("viral_score", 50.0))

        # Conversion rate (safe division)
        cr = conversions / max(clicks, 1) if clicks > 0 else 0.0

        # Revenue per click normalizado (safe division, cap en $50/click)
        rpc = revenue / max(clicks, 1) if clicks > 0 else 0.0
        rpc_normalized = min(rpc / 50.0, 1.0)  # Normalizar al rango 0-1

        # Engagement score (CTR + viral signal)
        ctr = clicks / max(impressions, 1) if impressions > 0 else 0.0
        engagement = min(1.0, (ctr * 50) + (viral_score / 200))

        # Reward combinado (pesos de ChatGPT)
        reward = (0.6 * cr) + (0.3 * rpc_normalized) + (0.1 * engagement)

        return reward

    def _apply_niche_diversification(
        self,
        approved: List[ProductEvaluation],
        products_by_id: Dict[str, Dict],
    ) -> List[ProductEvaluation]:
        """
        Limitar a MAX_PRODUCTS_PER_NICHE por nicho entre los aprobados.

        Dentro del mismo nicho, prioriza los de mayor score.
        Evita sobreconcentración de capital en un único mercado.
        """
        niche_counts: Dict[str, int] = {}
        diversified = []

        # Ordenar por score DESC para priorizar los mejores dentro del nicho
        sorted_approved = sorted(approved, key=lambda e: e.score, reverse=True)

        for ev in sorted_approved:
            p = products_by_id.get(ev.product_id, {})
            niche = str(p.get("niche", "unknown"))

            count = niche_counts.get(niche, 0)
            if count < MAX_PRODUCTS_PER_NICHE:
                diversified.append(ev)
                niche_counts[niche] = count + 1
            else:
                log_info(logger, "portfolio_niche_cap_applied",
                         product_id=ev.product_id,
                         niche=niche,
                         cap=MAX_PRODUCTS_PER_NICHE)

        return diversified
