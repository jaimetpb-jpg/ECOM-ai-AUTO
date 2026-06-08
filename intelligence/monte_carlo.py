"""
intelligence/monte_carlo.py — Monte Carlo Simulation Engine V1.0

Simulación de 1,000+ trayectorias para validar:
  1. Estabilidad del sistema bajo condiciones extremas
  2. Calibración de thresholds: stoploss_roas, min_budget_per_arm
  3. Drawdown esperado del portafolio bajo diferentes regímenes de mercado
  4. Sensibilidad de parámetros (τ, risk_aversion, prior_alpha/beta)

Modelos de mercado simulados:
  - Bull Market:     ROAS estable alto (μ=3.5, σ=0.5)
  - Bear Market:     ROAS bajo con alta volatilidad (μ=1.5, σ=1.0)
  - Trending Up:     ROAS con drift positivo
  - Regime Change:   Cambio abrupto mid-simulation
  - Fat Tails:       Distribución ROAS con colas pesadas (Cauchy)

Uso:
    from intelligence.monte_carlo import MonteCarloSimulator

    sim = MonteCarloSimulator(n_trajectories=1000, n_periods=90)
    results = sim.run_full_analysis()
    print(results.summary())
    results.export("monte_carlo_results.json")
"""

import math
import random
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ─── Modelos de mercado ───────────────────────────────────────────────────────

class MarketRegime:
    """Genera secuencias de ROAS según diferentes regímenes de mercado."""

    @staticmethod
    def bull_market(n: int, mu: float = 3.5, sigma: float = 0.5) -> List[float]:
        """Mercado favorable: ROAS estable alto."""
        return [max(0.1, random.gauss(mu, sigma)) for _ in range(n)]

    @staticmethod
    def bear_market(n: int, mu: float = 1.5, sigma: float = 1.0) -> List[float]:
        """Mercado adverso: ROAS bajo con alta volatilidad."""
        return [max(0.1, random.gauss(mu, sigma)) for _ in range(n)]

    @staticmethod
    def trending_up(n: int, mu_start: float = 1.8, drift: float = 0.02,
                    sigma: float = 0.4) -> List[float]:
        """ROAS con tendencia creciente (escala de producto nuevo)."""
        return [max(0.1, random.gauss(mu_start + i * drift, sigma)) for i in range(n)]

    @staticmethod
    def trending_down(n: int, mu_start: float = 4.0, drift: float = -0.03,
                      sigma: float = 0.5) -> List[float]:
        """ROAS con tendencia decreciente (saturación del nicho)."""
        return [max(0.1, random.gauss(max(1.0, mu_start + i * drift), sigma)) for i in range(n)]

    @staticmethod
    def regime_change(n: int, change_at: float = 0.5,
                      mu_before: float = 3.5, mu_after: float = 1.5,
                      sigma: float = 0.6) -> List[float]:
        """Cambio abrupto de régimen (ej. iOS update, competidor agresivo)."""
        result = []
        for i in range(n):
            if i < int(n * change_at):
                result.append(max(0.1, random.gauss(mu_before, sigma)))
            else:
                result.append(max(0.1, random.gauss(mu_after, sigma)))
        return result

    @staticmethod
    def fat_tails(n: int, mu: float = 2.5, scale: float = 0.8) -> List[float]:
        """
        ROAS con distribución de colas pesadas.
        Modela días excepcionales (Black Friday) y crashes.
        Usa distribución de Cauchy truncada.
        """
        result = []
        for _ in range(n):
            # Cauchy: median + scale * tan(π*(U - 0.5))
            u = random.random()
            u = max(0.01, min(0.99, u))
            sample = mu + scale * math.tan(math.pi * (u - 0.5))
            result.append(max(0.1, min(20.0, sample)))  # Truncar outliers extremos
        return result

    @staticmethod
    def seasonal(n: int, mu: float = 3.0, amplitude: float = 1.0,
                 period: int = 30, sigma: float = 0.3) -> List[float]:
        """ROAS con estacionalidad (ciclos mensuales)."""
        return [
            max(0.1, random.gauss(
                mu + amplitude * math.sin(2 * math.pi * i / period), sigma
            ))
            for i in range(n)
        ]


# ─── Resultados de simulación ─────────────────────────────────────────────────

@dataclass
class TrajectoryResult:
    """Resultado de una trayectoria individual."""
    trajectory_id: int
    regime: str
    roas_series: List[float]
    budget_series: List[float]      # Budget asignado en cada período
    revenue_series: List[float]
    cumulative_roas: List[float]
    max_drawdown: float
    final_roas: float
    triggered_stoploss: bool
    periods_active: int             # Períodos antes de posible stop


@dataclass
class SimulationResults:
    """Resultados agregados de todas las trayectorias."""
    regime: str
    n_trajectories: int
    n_periods: int
    # Estadísticas de ROAS
    mean_final_roas: float
    p5_final_roas: float            # Percentil 5 (peor caso)
    p25_final_roas: float
    p50_final_roas: float
    p75_final_roas: float
    p95_final_roas: float           # Percentil 95 (mejor caso)
    # Estadísticas de drawdown
    mean_max_drawdown: float
    p95_max_drawdown: float         # Worst-case drawdown
    # Riesgo operacional
    stoploss_rate: float            # % de trayectorias que activaron stoploss
    ruin_rate: float                # % con ROAS < 1.0 al final
    # Parámetros usados
    stoploss_threshold: float
    min_budget: float
    raw_trajectories: List[TrajectoryResult] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"\n{'='*60}",
            f"  MONTE CARLO SIMULATION — {self.regime.upper()}",
            f"{'='*60}",
            f"  Trayectorias: {self.n_trajectories:,} | Períodos: {self.n_periods}",
            f"",
            f"  ROAS FINAL (distribución):",
            f"    P5  (worst 5%):  {self.p5_final_roas:.3f}",
            f"    P25:            {self.p25_final_roas:.3f}",
            f"    Median:         {self.p50_final_roas:.3f}",
            f"    Mean:           {self.mean_final_roas:.3f}",
            f"    P75:            {self.p75_final_roas:.3f}",
            f"    P95 (best 5%):  {self.p95_final_roas:.3f}",
            f"",
            f"  DRAWDOWN:",
            f"    Mean max drawdown: {self.mean_max_drawdown:.1%}",
            f"    P95 max drawdown:  {self.p95_max_drawdown:.1%}",
            f"",
            f"  RIESGO OPERACIONAL:",
            f"    Stop-loss rate: {self.stoploss_rate:.1%}",
            f"    Ruin rate (<1x): {self.ruin_rate:.1%}",
            f"",
            f"  PARÁMETROS:",
            f"    Stop-loss threshold: {self.stoploss_threshold}",
            f"    Min budget/arm: ${self.min_budget:.2f}",
            f"{'='*60}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "regime": self.regime,
            "n_trajectories": self.n_trajectories,
            "n_periods": self.n_periods,
            "roas_distribution": {
                "p5": self.p5_final_roas,
                "p25": self.p25_final_roas,
                "p50": self.p50_final_roas,
                "mean": self.mean_final_roas,
                "p75": self.p75_final_roas,
                "p95": self.p95_final_roas,
            },
            "drawdown": {
                "mean": self.mean_max_drawdown,
                "p95": self.p95_max_drawdown,
            },
            "risk": {
                "stoploss_rate": self.stoploss_rate,
                "ruin_rate": self.ruin_rate,
            },
            "parameters": {
                "stoploss_threshold": self.stoploss_threshold,
                "min_budget": self.min_budget,
            }
        }


@dataclass
class FullAnalysisResults:
    """Resultados completos de análisis multi-régimen."""
    results_by_regime: Dict[str, SimulationResults]
    optimal_stoploss: float
    optimal_min_budget: float
    recommended_tau: float
    sensitivity_analysis: Dict[str, dict]
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def summary(self) -> str:
        lines = [
            "\n" + "=" * 70,
            "  ANÁLISIS MONTE CARLO COMPLETO — RECOMENDACIONES ÓPTIMAS",
            "=" * 70,
            f"",
            f"  ✅ Stop-loss óptimo:      ROAS < {self.optimal_stoploss:.2f} por 48h",
            f"  ✅ Budget mínimo/arm:     ${self.optimal_min_budget:.2f}",
            f"  ✅ Temperatura τ (softmax): {self.recommended_tau:.2f}",
            f"",
            "  RESULTADOS POR RÉGIMEN:",
        ]
        for regime, res in self.results_by_regime.items():
            lines.append(
                f"    {regime:20s} | Median ROAS: {res.p50_final_roas:.2f} | "
                f"P95 Drawdown: {res.p95_max_drawdown:.1%} | "
                f"Stop rate: {res.stoploss_rate:.1%}"
            )
        lines.append("=" * 70)
        return "\n".join(lines)

    def export(self, path: str):
        """Exporta resultados a JSON."""
        data = {
            "timestamp": self.timestamp,
            "recommendations": {
                "optimal_stoploss_roas": self.optimal_stoploss,
                "optimal_min_budget": self.optimal_min_budget,
                "recommended_tau": self.recommended_tau,
            },
            "results_by_regime": {
                k: v.to_dict() for k, v in self.results_by_regime.items()
            },
            "sensitivity_analysis": self.sensitivity_analysis,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Monte Carlo results exported to %s", path)


# ─── Simulador principal ──────────────────────────────────────────────────────

class MonteCarloSimulator:
    """
    Simulador Monte Carlo para validar parámetros del sistema de allocación.

    Ejecuta miles de trayectorias bajo diferentes condiciones de mercado
    para encontrar los parámetros óptimos de riesgo antes de ir a producción.

    Esto es lo que separa un sistema cuantitativo serio de uno ad-hoc:
    validar los parámetros fuera de producción antes de exponerlos a dinero real.

    Uso:
        sim = MonteCarloSimulator(n_trajectories=1000, n_periods=90)
        results = sim.run_full_analysis()
        print(results.summary())
        results.export("validation/monte_carlo_v1.json")
    """

    def __init__(
        self,
        n_trajectories: int = 1000,
        n_periods: int = 90,           # Días de simulación
        n_campaigns: int = 5,          # Campañas en el portafolio simulado
        base_budget: float = 2000.0,   # Presupuesto diario total
    ):
        self.n_trajectories = n_trajectories
        self.n_periods = n_periods
        self.n_campaigns = n_campaigns
        self.base_budget = base_budget

        logger.info(
            "MonteCarloSimulator initialized | n=%d | periods=%d | campaigns=%d",
            n_trajectories, n_periods, n_campaigns
        )

    # ── Simulación individual ─────────────────────────────────────────────────

    def simulate_trajectory(
        self,
        trajectory_id: int,
        regime: str,
        stoploss_threshold: float = 1.2,
        min_budget_per_arm: float = 50.0,
        tau: float = 0.5,
    ) -> TrajectoryResult:
        """
        Simula una trayectoria completa bajo un régimen de mercado dado.

        Modela el comportamiento del sistema de allocation a lo largo del tiempo:
        - Thompson Sampling simplificado para asignar budget
        - Stop-loss si ROAS < threshold durante 3 períodos consecutivos
        - Seguimiento de drawdown acumulado
        """
        # Generar series de ROAS por campaña según régimen
        roas_per_campaign = []
        for _ in range(self.n_campaigns):
            noise_factor = random.uniform(0.7, 1.3)  # Variación entre campañas
            if regime == "bull_market":
                series = MarketRegime.bull_market(
                    self.n_periods, mu=3.5 * noise_factor, sigma=0.5
                )
            elif regime == "bear_market":
                series = MarketRegime.bear_market(
                    self.n_periods, mu=1.5 * noise_factor, sigma=1.0
                )
            elif regime == "trending_up":
                series = MarketRegime.trending_up(
                    self.n_periods, mu_start=1.8 * noise_factor, drift=0.02
                )
            elif regime == "trending_down":
                series = MarketRegime.trending_down(
                    self.n_periods, mu_start=4.0 * noise_factor, drift=-0.03
                )
            elif regime == "regime_change":
                series = MarketRegime.regime_change(
                    self.n_periods, change_at=0.5,
                    mu_before=3.5 * noise_factor, mu_after=1.5 * noise_factor
                )
            elif regime == "fat_tails":
                series = MarketRegime.fat_tails(self.n_periods, mu=2.5 * noise_factor)
            elif regime == "seasonal":
                series = MarketRegime.seasonal(
                    self.n_periods, mu=3.0 * noise_factor, amplitude=1.0, period=30
                )
            else:
                series = MarketRegime.bull_market(self.n_periods)
            roas_per_campaign.append(series)

        # Variables de estado del portafolio simulado
        alphas = [1.0] * self.n_campaigns   # Priors Beta por campaña
        betas = [9.0] * self.n_campaigns

        roas_series = []
        budget_series = []
        revenue_series = []
        cumulative_revenue = 0.0
        cumulative_spend = 0.0
        peak_portfolio_roas = 0.0
        max_drawdown = 0.0
        triggered_stoploss = False
        stoploss_consecutive = 0
        periods_active = 0

        for t in range(self.n_periods):
            # Thompson Sampling simplificado: sample de Beta por campaña
            samples = []
            for i in range(self.n_campaigns):
                g1 = random.gammavariate(max(alphas[i], 1e-6), 1.0)
                g2 = random.gammavariate(max(betas[i], 1e-6), 1.0)
                total = g1 + g2
                samples.append(g1 / total if total > 1e-12 else 0.5)

            # Softmax para asignar budget
            max_s = max(samples)
            exp_s = [math.exp(min(700.0, (s - max_s) / tau)) for s in samples]
            total_exp = sum(exp_s) + 1e-12
            probs = [e / total_exp for e in exp_s]

            # Presupuesto con mínimos garantizados
            min_total = min_budget_per_arm * self.n_campaigns
            remaining = max(0, self.base_budget - min_total)
            budgets = [min_budget_per_arm + p * remaining for p in probs]
            period_spend = sum(budgets)

            # Calcular revenue del período
            period_revenue = sum(
                budgets[i] * roas_per_campaign[i][t]
                for i in range(self.n_campaigns)
            )
            period_roas = period_revenue / max(period_spend, 1e-6)

            # Actualizar posteriors Bayesianos (simplified)
            for i in range(self.n_campaigns):
                campaign_roas = roas_per_campaign[i][t]
                simulated_clicks = int(budgets[i] * 0.01)  # Simplified CTR
                simulated_conv = int(simulated_clicks * 0.03 * (campaign_roas / 3.0))
                alphas[i] += simulated_conv
                betas[i] += max(0, simulated_clicks - simulated_conv)

            # Acumular métricas
            cumulative_revenue += period_revenue
            cumulative_spend += period_spend
            cum_roas = cumulative_revenue / max(cumulative_spend, 1e-6)

            roas_series.append(period_roas)
            budget_series.append(period_spend)
            revenue_series.append(period_revenue)
            cumulative_roas = [
                sum(revenue_series[:j+1]) / max(sum(budget_series[:j+1]), 1e-6)
                for j in range(len(revenue_series))
            ]

            # Calcular drawdown
            if cum_roas > peak_portfolio_roas:
                peak_portfolio_roas = cum_roas
            if peak_portfolio_roas > 1e-6:
                current_dd = (peak_portfolio_roas - cum_roas) / peak_portfolio_roas
                max_drawdown = max(max_drawdown, current_dd)

            # Stop-loss check
            if period_roas < stoploss_threshold:
                stoploss_consecutive += 1
            else:
                stoploss_consecutive = 0

            if stoploss_consecutive >= 3:  # 3 períodos consecutivos bajo threshold
                triggered_stoploss = True
                periods_active = t + 1
                logger.debug(
                    "Stop-loss triggered | traj=%d | period=%d | roas=%.3f",
                    trajectory_id, t, period_roas
                )
                break

            periods_active = t + 1

        final_roas = cumulative_revenue / max(cumulative_spend, 1e-6)

        return TrajectoryResult(
            trajectory_id=trajectory_id,
            regime=regime,
            roas_series=roas_series,
            budget_series=budget_series,
            revenue_series=revenue_series,
            cumulative_roas=[
                sum(revenue_series[:j+1]) / max(sum(budget_series[:j+1]), 1e-6)
                for j in range(len(revenue_series))
            ],
            max_drawdown=max_drawdown,
            final_roas=final_roas,
            triggered_stoploss=triggered_stoploss,
            periods_active=periods_active,
        )

    # ── Análisis por régimen ──────────────────────────────────────────────────

    def run_regime(
        self,
        regime: str,
        stoploss_threshold: float = 1.2,
        min_budget: float = 50.0,
        tau: float = 0.5,
    ) -> SimulationResults:
        """Ejecuta todas las trayectorias para un régimen dado."""
        logger.info(
            "Running regime | regime=%s | n=%d | stoploss=%.2f | min_budget=%.2f",
            regime, self.n_trajectories, stoploss_threshold, min_budget
        )

        trajectories = []
        for i in range(self.n_trajectories):
            traj = self.simulate_trajectory(
                trajectory_id=i,
                regime=regime,
                stoploss_threshold=stoploss_threshold,
                min_budget_per_arm=min_budget,
                tau=tau,
            )
            trajectories.append(traj)

        # Calcular estadísticas
        final_roas_list = sorted([t.final_roas for t in trajectories])
        drawdowns = sorted([t.max_drawdown for t in trajectories])
        n = len(trajectories)

        def percentile(sorted_list: list, p: float) -> float:
            idx = int(len(sorted_list) * p)
            return sorted_list[min(idx, len(sorted_list) - 1)]

        return SimulationResults(
            regime=regime,
            n_trajectories=n,
            n_periods=self.n_periods,
            mean_final_roas=sum(final_roas_list) / n,
            p5_final_roas=percentile(final_roas_list, 0.05),
            p25_final_roas=percentile(final_roas_list, 0.25),
            p50_final_roas=percentile(final_roas_list, 0.50),
            p75_final_roas=percentile(final_roas_list, 0.75),
            p95_final_roas=percentile(final_roas_list, 0.95),
            mean_max_drawdown=sum(drawdowns) / n,
            p95_max_drawdown=percentile(drawdowns, 0.95),
            stoploss_rate=sum(1 for t in trajectories if t.triggered_stoploss) / n,
            ruin_rate=sum(1 for t in trajectories if t.final_roas < 1.0) / n,
            stoploss_threshold=stoploss_threshold,
            min_budget=min_budget,
            raw_trajectories=trajectories,
        )

    # ── Análisis completo ─────────────────────────────────────────────────────

    def run_full_analysis(
        self,
        stoploss_thresholds: Optional[List[float]] = None,
        min_budgets: Optional[List[float]] = None,
        taus: Optional[List[float]] = None,
    ) -> FullAnalysisResults:
        """
        Análisis completo: todos los regímenes + búsqueda de parámetros óptimos.

        Encuentra automáticamente:
        - stoploss_roas óptimo (minimiza ruin_rate sin parar demasiado pronto)
        - min_budget óptimo (equilibra exploración vs. burn)
        - τ óptimo (explora vs. explota)
        """
        if stoploss_thresholds is None:
            stoploss_thresholds = [1.0, 1.2, 1.5, 2.0]
        if min_budgets is None:
            min_budgets = [20.0, 50.0, 100.0, 200.0]
        if taus is None:
            taus = [0.3, 0.5, 0.8, 1.0]

        regimes = [
            "bull_market", "bear_market", "trending_up",
            "trending_down", "regime_change", "fat_tails", "seasonal"
        ]

        # Correr todos los regímenes con parámetros default
        results_by_regime = {}
        for regime in regimes:
            results_by_regime[regime] = self.run_regime(
                regime=regime,
                stoploss_threshold=1.2,
                min_budget=50.0,
                tau=0.5,
            )
            logger.info("Regime %s done | median_roas=%.3f | ruin_rate=%.1f%%",
                        regime, results_by_regime[regime].p50_final_roas,
                        results_by_regime[regime].ruin_rate * 100)

        # Sensibilidad de stoploss_threshold
        stoploss_sensitivity = {}
        for threshold in stoploss_thresholds:
            res = self.run_regime("bear_market", stoploss_threshold=threshold)
            stoploss_sensitivity[str(threshold)] = {
                "median_roas": res.p50_final_roas,
                "ruin_rate": res.ruin_rate,
                "stoploss_rate": res.stoploss_rate,
                "p95_drawdown": res.p95_max_drawdown,
            }

        # Sensibilidad de min_budget
        budget_sensitivity = {}
        for mb in min_budgets:
            res = self.run_regime("regime_change", min_budget=mb)
            budget_sensitivity[str(mb)] = {
                "median_roas": res.p50_final_roas,
                "ruin_rate": res.ruin_rate,
                "stoploss_rate": res.stoploss_rate,
            }

        # Sensibilidad de tau
        tau_sensitivity = {}
        for tau in taus:
            res = self.run_regime("bull_market", tau=tau)
            tau_sensitivity[str(tau)] = {
                "median_roas": res.p50_final_roas,
                "p95_roas": res.p95_final_roas,
            }

        # Encontrar parámetros óptimos
        # Stoploss óptimo: mínimo ruin_rate con stoploss_rate < 30%
        optimal_stoploss = 1.2
        best_score = float("inf")
        for threshold_str, metrics in stoploss_sensitivity.items():
            if metrics["stoploss_rate"] < 0.30:  # No más del 30% de trayectorias paradas
                score = metrics["ruin_rate"]
                if score < best_score:
                    best_score = score
                    optimal_stoploss = float(threshold_str)

        # Min budget óptimo: maximiza median_roas en regime_change
        optimal_min_budget = 50.0
        best_roas = 0.0
        for mb_str, metrics in budget_sensitivity.items():
            if metrics["median_roas"] > best_roas:
                best_roas = metrics["median_roas"]
                optimal_min_budget = float(mb_str)

        # Tau óptimo: maximiza P95 ROAS en bull (captura upside)
        optimal_tau = 0.5
        best_p95 = 0.0
        for tau_str, metrics in tau_sensitivity.items():
            if metrics["p95_roas"] > best_p95:
                best_p95 = metrics["p95_roas"]
                optimal_tau = float(tau_str)

        return FullAnalysisResults(
            results_by_regime=results_by_regime,
            optimal_stoploss=optimal_stoploss,
            optimal_min_budget=optimal_min_budget,
            recommended_tau=optimal_tau,
            sensitivity_analysis={
                "stoploss_threshold": stoploss_sensitivity,
                "min_budget": budget_sensitivity,
                "tau": tau_sensitivity,
            },
        )


# ─── CLI entry point ──────────────────────────────────────────────────────────

def run_quick_validation(
    n_trajectories: int = 200,
    output_path: str = "monte_carlo_results.json",
) -> FullAnalysisResults:
    """
    Validación rápida (~200 trayectorias) para CI/CD o notebooks.
    Para análisis completo, usar n_trajectories=1000.
    """
    sim = MonteCarloSimulator(
        n_trajectories=n_trajectories,
        n_periods=60,
        n_campaigns=5,
        base_budget=2000.0,
    )
    results = sim.run_full_analysis()
    results.export(output_path)
    print(results.summary())
    return results


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    output = sys.argv[2] if len(sys.argv) > 2 else "monte_carlo_results.json"

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")
    run_quick_validation(n_trajectories=n, output_path=output)
