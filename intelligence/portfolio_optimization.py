"""
intelligence/portfolio_optimization.py — Portfolio Optimization Engine V1.0

Aplica teoría de portafolio de Markowitz a asignación de presupuesto publicitario.

Core Math:
  Objetivo: max E[ROAS] - λ * Var[ROAS]    (Sharpe-like ratio)
  Restricciones:
    - Σ w_i = 1  (presupuesto total)
    - w_i ≥ w_min (mínimo por campaña)
    - w_i ≤ w_max (máximo por campaña, anti-concentración)
    - Drawdown(portfolio) ≤ max_drawdown_pct
    - Volatility(portfolio) ≤ max_volatility

Capas de riesgo:
  1. Stop-loss dinámico por campaña
  2. Portfolio drawdown limit (kill-switch total)
  3. Volatility cap por campaña
  4. Concentration limit (ninguna campaña > max_weight)

Diferencia clave vs. Thompson Sampling puro:
  - TS maximiza retorno esperado por campaña independiente
  - Portfolio Opt. considera correlaciones entre campañas
  - Reduce riesgo sistémico cuando varias campañas están correlacionadas

Uso:
    from intelligence.portfolio_optimization import PortfolioOptimizer, CampaignMetrics

    opt = PortfolioOptimizer(risk_aversion=2.0, max_drawdown_pct=0.15)
    metrics = [
        CampaignMetrics("c1", roas_history=[3.1, 2.8, 3.5], spend=500),
        CampaignMetrics("c2", roas_history=[2.0, 4.0, 1.5], spend=500),  # alta volatilidad
    ]
    allocation = opt.optimize(metrics, total_budget=2000)
"""

import math
import random
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ─── Estructuras de datos ─────────────────────────────────────────────────────

@dataclass
class CampaignMetrics:
    """
    Métricas de una campaña para optimización de portafolio.
    """
    campaign_id: str
    roas_history: List[float] = field(default_factory=list)  # ROAS por período
    spend: float = 0.0                    # Gasto total acumulado
    revenue: float = 0.0                  # Revenue total
    impressions: int = 0
    clicks: int = 0
    ctr: float = 0.0
    # Restricciones individuales
    min_weight: float = 0.0              # Mínima fracción del presupuesto
    max_weight: float = 1.0              # Máxima fracción del presupuesto
    is_active: bool = True
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def expected_roas(self) -> float:
        """ROAS esperado (media histórica)."""
        if not self.roas_history:
            return self.revenue / max(self.spend, 1e-6) if self.spend > 0 else 1.0
        return sum(self.roas_history) / len(self.roas_history)

    def roas_variance(self) -> float:
        """Varianza del ROAS (medida de riesgo)."""
        if len(self.roas_history) < 2:
            return 1.0  # Alta incertidumbre si pocos datos
        mu = self.expected_roas()
        return sum((r - mu) ** 2 for r in self.roas_history) / (len(self.roas_history) - 1)

    def roas_std(self) -> float:
        return math.sqrt(max(0, self.roas_variance()))

    def sharpe_ratio(self, risk_free_rate: float = 1.0) -> float:
        """ROAS ajustado por riesgo (análogo a Sharpe ratio)."""
        excess = self.expected_roas() - risk_free_rate
        std = self.roas_std()
        return excess / max(std, 1e-6)

    def max_drawdown(self) -> float:
        """Máximo drawdown en historial de ROAS."""
        if len(self.roas_history) < 2:
            return 0.0
        peak = self.roas_history[0]
        max_dd = 0.0
        for roas in self.roas_history:
            if roas > peak:
                peak = roas
            dd = (peak - roas) / max(peak, 1e-6)
            max_dd = max(max_dd, dd)
        return max_dd


@dataclass
class PortfolioState:
    """Estado del portafolio de campañas."""
    total_budget: float
    weights: Dict[str, float]            # Fracción del presupuesto por campaña
    allocations: Dict[str, float]        # Monto en $ por campaña
    expected_roas: float
    portfolio_variance: float
    portfolio_sharpe: float
    risk_violations: List[str]           # Restricciones que se violaron
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "total_budget": self.total_budget,
            "weights": self.weights,
            "allocations": self.allocations,
            "expected_roas": self.expected_roas,
            "portfolio_variance": self.portfolio_variance,
            "portfolio_sharpe": self.portfolio_sharpe,
            "risk_violations": self.risk_violations,
            "timestamp": self.timestamp,
        }


# ─── Optimizador ─────────────────────────────────────────────────────────────

class PortfolioOptimizer:
    """
    Optimizador de portafolio publicitario basado en teoría de Markowitz.

    En lugar de optimizar campañas individualmente, trata el presupuesto
    como un portafolio de activos con retornos correlacionados.

    Parámetros clave:
        risk_aversion (λ): Mayor → más conservador (reduce varianza)
                           Menor → más agresivo (maximiza retorno)
                           Recomendado: 1.5–3.0 para e-commerce DTC

        max_drawdown_pct: Si el portfolio cae más de este % → kill-switch
        max_concentration: Ninguna campaña puede superar esta fracción
    """

    def __init__(
        self,
        risk_aversion: float = 2.0,
        max_drawdown_pct: float = 0.15,
        max_concentration: float = 0.50,
        min_weight: float = 0.02,
        risk_free_roas: float = 1.0,
        n_iterations: int = 1000,
    ):
        self.risk_aversion = risk_aversion
        self.max_drawdown_pct = max_drawdown_pct
        self.max_concentration = max_concentration
        self.min_weight = min_weight
        self.risk_free_roas = risk_free_roas
        self.n_iterations = n_iterations
        self._portfolio_history: List[PortfolioState] = []

        logger.info(
            "PortfolioOptimizer initialized | λ=%.2f | max_dd=%.1f%% | max_conc=%.0f%%",
            risk_aversion, max_drawdown_pct * 100, max_concentration * 100
        )

    # ── Optimización principal ─────────────────────────────────────────────────

    def optimize(
        self,
        campaigns: List[CampaignMetrics],
        total_budget: float,
        force_active_only: bool = True,
    ) -> Dict[str, float]:
        """
        Optimiza la asignación de presupuesto entre campañas.

        Algoritmo:
        1. Filtrar campañas activas sin stop-loss activo
        2. Calcular matrix de covarianza estimada
        3. Optimizar via gradient-free (random search + hill climbing)
        4. Aplicar restricciones de riesgo
        5. Retornar allocations en $

        Returns:
            Dict[campaign_id → presupuesto_asignado]
        """
        if not campaigns:
            logger.warning("optimize called with empty campaign list")
            return {}

        if total_budget <= 0:
            return {c.campaign_id: 0.0 for c in campaigns}

        # Filtrar campañas activas
        active = [c for c in campaigns if c.is_active] if force_active_only else campaigns
        if not active:
            logger.warning("No active campaigns after filtering")
            return {c.campaign_id: 0.0 for c in campaigns}

        # Verificar stop-losses
        active, stopped = self._apply_stop_losses(active)
        if stopped:
            logger.warning("Stop-loss triggered | campaigns=%s", [c.campaign_id for c in stopped])

        if not active:
            logger.error("All campaigns stopped by risk rules")
            return {c.campaign_id: 0.0 for c in campaigns}

        n = len(active)

        # Calcular expected returns y covariance matrix
        expected_roas = [c.expected_roas() for c in active]
        cov_matrix = self._estimate_covariance(active)

        # Optimizar weights
        optimal_weights = self._optimize_weights(
            expected_roas=expected_roas,
            cov_matrix=cov_matrix,
            n_campaigns=n,
            constraints=[
                (c.min_weight, min(c.max_weight, self.max_concentration))
                for c in active
            ]
        )

        # Calcular métricas del portafolio
        port_roas, port_var = self._portfolio_metrics(optimal_weights, expected_roas, cov_matrix)
        port_sharpe = (port_roas - self.risk_free_roas) / math.sqrt(max(port_var, 1e-9))

        # Asignar presupuesto
        allocations = {
            c.campaign_id: w * total_budget
            for c, w in zip(active, optimal_weights)
        }

        # Campañas detenidas reciben 0
        for c in stopped:
            allocations[c.campaign_id] = 0.0

        # Verificar restricciones de riesgo del portafolio
        violations = self._check_portfolio_risk(active, optimal_weights, port_var)

        # Guardar estado
        state = PortfolioState(
            total_budget=total_budget,
            weights={c.campaign_id: w for c, w in zip(active, optimal_weights)},
            allocations=allocations,
            expected_roas=port_roas,
            portfolio_variance=port_var,
            portfolio_sharpe=port_sharpe,
            risk_violations=violations,
        )
        self._portfolio_history.append(state)

        logger.info(
            "Portfolio optimized | campaigns=%d | E[ROAS]=%.3f | Var=%.4f | "
            "Sharpe=%.3f | violations=%d | budget=%.2f",
            len(active), port_roas, port_var, port_sharpe, len(violations), total_budget
        )

        return allocations

    # ── Optimización de weights ───────────────────────────────────────────────

    def _optimize_weights(
        self,
        expected_roas: List[float],
        cov_matrix: List[List[float]],
        n_campaigns: int,
        constraints: List[Tuple[float, float]],
    ) -> List[float]:
        """
        Optimiza los weights del portafolio via random search + refinamiento.

        Objetivo: max { E[ROAS] - λ * Var[ROAS] }
        Sujeto a: Σw = 1, w_min ≤ w_i ≤ w_max

        Usamos random search porque:
        - No requiere derivadas (compatible con constraints discretas)
        - Suficientemente rápido para n ≤ 50 campañas
        - Evita mínimos locales del gradiente
        """
        best_weights = self._uniform_weights(n_campaigns, constraints)
        best_score = self._objective(best_weights, expected_roas, cov_matrix)

        for _ in range(self.n_iterations):
            # Generar candidato: perturbar un weight aleatorio
            candidate = list(best_weights)
            i = random.randint(0, n_campaigns - 1)
            j = random.randint(0, n_campaigns - 1)
            if i == j:
                continue

            # Transferir una fracción de i → j
            transfer = random.uniform(0, candidate[i] * 0.5)
            w_min_j, w_max_j = constraints[j]
            w_min_i, w_max_i = constraints[i]

            new_i = candidate[i] - transfer
            new_j = candidate[j] + transfer

            if new_i < w_min_i or new_j > w_max_j:
                continue

            candidate[i] = new_i
            candidate[j] = new_j

            score = self._objective(candidate, expected_roas, cov_matrix)
            if score > best_score:
                best_score = score
                best_weights = candidate

        # Normalizar para que sumen exactamente 1
        total = sum(best_weights)
        if total > 1e-9:
            best_weights = [w / total for w in best_weights]

        return best_weights

    def _objective(
        self,
        weights: List[float],
        expected_roas: List[float],
        cov_matrix: List[List[float]],
    ) -> float:
        """
        Función objetivo: E[ROAS] - λ * Var[ROAS]
        Análogo al ratio de Sharpe pero con penalización explícita de varianza.
        """
        port_roas, port_var = self._portfolio_metrics(weights, expected_roas, cov_matrix)
        return port_roas - self.risk_aversion * port_var

    def _portfolio_metrics(
        self,
        weights: List[float],
        expected_roas: List[float],
        cov_matrix: List[List[float]],
    ) -> Tuple[float, float]:
        """
        Calcula E[ROAS] y Var[ROAS] del portafolio.

        E[ROAS_p] = Σ w_i * E[ROAS_i]
        Var[ROAS_p] = Σ_i Σ_j w_i * w_j * Cov(i,j)
        """
        n = len(weights)
        port_roas = sum(w * r for w, r in zip(weights, expected_roas))
        port_var = 0.0
        for i in range(n):
            for j in range(n):
                port_var += weights[i] * weights[j] * cov_matrix[i][j]
        return port_roas, port_var

    def _uniform_weights(
        self,
        n: int,
        constraints: List[Tuple[float, float]],
    ) -> List[float]:
        """Distribución inicial: igual peso con constraints mínimos respetados."""
        weights = [max(c[0], self.min_weight) for c in constraints]
        total = sum(weights)
        if total > 1.0:
            weights = [w / total for w in weights]
        else:
            # Distribuir sobrante uniformemente
            remaining = 1.0 - total
            weights = [w + remaining / n for w in weights]
        return weights

    # ── Estimación de covarianza ──────────────────────────────────────────────

    def _estimate_covariance(self, campaigns: List[CampaignMetrics]) -> List[List[float]]:
        """
        Estima la matrix de covarianza de ROAS entre campañas.

        Con pocos datos históricos, usamos:
        - Varianza individual (diagonal): estimada de historial
        - Correlación off-diagonal: correlación por defecto (shrinkage hacia 0.3)

        Esta es la aproximación de Ledoit-Wolf simplificada.
        """
        n = len(campaigns)
        variances = [max(c.roas_variance(), 0.01) for c in campaigns]

        # Default: correlación moderada (0.3) entre campañas del mismo portfolio
        # Esto es conservador: asume que las campañas no son independientes
        default_corr = 0.30

        cov = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i == j:
                    cov[i][j] = variances[i]
                else:
                    # Shrinkage: mezcla correlación empírica (si existe) con default
                    empirical_corr = self._empirical_correlation(
                        campaigns[i].roas_history,
                        campaigns[j].roas_history
                    )
                    n_obs = min(len(campaigns[i].roas_history), len(campaigns[j].roas_history))
                    shrink = min(0.8, n_obs / (n_obs + 10))  # más datos → menos shrinkage
                    corr = shrink * empirical_corr + (1 - shrink) * default_corr
                    cov[i][j] = corr * math.sqrt(variances[i]) * math.sqrt(variances[j])
        return cov

    def _empirical_correlation(self, hist_a: List[float], hist_b: List[float]) -> float:
        """Correlación de Pearson entre dos series. Retorna 0.3 si insuficientes datos."""
        n = min(len(hist_a), len(hist_b))
        if n < 3:
            return 0.30

        a = hist_a[-n:]
        b = hist_b[-n:]
        mu_a = sum(a) / n
        mu_b = sum(b) / n

        cov = sum((a[i] - mu_a) * (b[i] - mu_b) for i in range(n)) / max(n - 1, 1)
        std_a = math.sqrt(sum((x - mu_a) ** 2 for x in a) / max(n - 1, 1))
        std_b = math.sqrt(sum((x - mu_b) ** 2 for x in b) / max(n - 1, 1))

        if std_a < 1e-9 or std_b < 1e-9:
            return 0.30
        return max(-1.0, min(1.0, cov / (std_a * std_b)))

    # ── Risk management ───────────────────────────────────────────────────────

    def _apply_stop_losses(
        self,
        campaigns: List[CampaignMetrics],
    ) -> Tuple[List[CampaignMetrics], List[CampaignMetrics]]:
        """
        Aplica stop-losses individuales.
        Detiene campañas con drawdown > max_drawdown_pct.
        """
        active = []
        stopped = []
        for c in campaigns:
            dd = c.max_drawdown()
            if dd > self.max_drawdown_pct and len(c.roas_history) >= 3:
                logger.warning(
                    "Stop-loss | campaign=%s | drawdown=%.1f%% > limit=%.1f%%",
                    c.campaign_id, dd * 100, self.max_drawdown_pct * 100
                )
                stopped.append(c)
            else:
                active.append(c)
        return active, stopped

    def _check_portfolio_risk(
        self,
        campaigns: List[CampaignMetrics],
        weights: List[float],
        port_var: float,
    ) -> List[str]:
        """Verifica restricciones de riesgo del portafolio. Retorna lista de violaciones."""
        violations = []

        # Concentración
        for c, w in zip(campaigns, weights):
            if w > self.max_concentration:
                violations.append(
                    f"CONCENTRATION: {c.campaign_id} weight={w:.1%} > limit={self.max_concentration:.1%}"
                )

        # Volatilidad del portafolio
        port_std = math.sqrt(max(port_var, 0))
        if port_std > 2.0:
            violations.append(f"HIGH_VOLATILITY: portfolio std={port_std:.3f}")

        if violations:
            logger.warning("Portfolio risk violations | %s", violations)
        return violations

    # ── Portfolio kill-switch ─────────────────────────────────────────────────

    def should_kill_portfolio(self, window: int = 5) -> bool:
        """
        Verifica si el portafolio completo debe detenerse.
        Se activa si el ROAS promedio reciente está por debajo del umbral.
        """
        if len(self._portfolio_history) < window:
            return False
        recent = self._portfolio_history[-window:]
        avg_roas = sum(p.expected_roas for p in recent) / window
        if avg_roas < self.risk_free_roas * 0.8:  # 20% por debajo del break-even
            logger.critical(
                "PORTFOLIO KILL SWITCH | avg_roas=%.3f < threshold=%.3f",
                avg_roas, self.risk_free_roas * 0.8
            )
            return True
        return False

    # ── Diagnóstico ───────────────────────────────────────────────────────────

    def get_portfolio_analytics(self) -> dict:
        """Retorna analytics del portafolio para Metabase/Grafana."""
        if not self._portfolio_history:
            return {"history": [], "summary": {}}

        recent = self._portfolio_history[-10:]
        avg_roas = sum(p.expected_roas for p in recent) / len(recent)
        avg_sharpe = sum(p.portfolio_sharpe for p in recent) / len(recent)
        avg_var = sum(p.portfolio_variance for p in recent) / len(recent)

        return {
            "summary": {
                "avg_expected_roas": round(avg_roas, 4),
                "avg_sharpe": round(avg_sharpe, 4),
                "avg_variance": round(avg_var, 6),
                "total_allocations": len(self._portfolio_history),
                "kill_switch_active": self.should_kill_portfolio(),
            },
            "history": [p.to_dict() for p in recent],
            "risk_config": {
                "risk_aversion": self.risk_aversion,
                "max_drawdown_pct": self.max_drawdown_pct,
                "max_concentration": self.max_concentration,
            }
        }
