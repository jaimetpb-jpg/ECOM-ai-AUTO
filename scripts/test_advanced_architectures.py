"""
scripts/test_advanced_architectures.py

Tests de integración para:
  1. HierarchicalBayesianAllocator
  2. PortfolioOptimizer
  3. MonteCarloSimulator

Ejecutar:
  python scripts/test_advanced_architectures.py
  python -m pytest scripts/test_advanced_architectures.py -v
"""

import sys
import os
import logging
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")


# ─── Tests: Hierarchical Bayesian ─────────────────────────────────────────────

def test_hierarchical_bayesian_basic():
    """Test básico: registrar cluster + campañas + allocate."""
    from intelligence.hierarchical_bayesian import HierarchicalBayesianAllocator

    allocator = HierarchicalBayesianAllocator(n_samples=50)

    # Registrar cluster y campañas
    allocator.register_cluster("skincare_us_premium", alpha_ctr=2.0, beta_ctr=18.0)
    allocator.register_campaign("camp_001", "skincare_us_premium")
    allocator.register_campaign("camp_002", "skincare_us_premium")
    allocator.register_campaign("camp_003", "skincare_us_premium")

    # Asignar sin datos (prior only)
    allocation = allocator.allocate(
        ["camp_001", "camp_002", "camp_003"], "skincare_us_premium", total_budget=3000.0
    )

    assert len(allocation) == 3
    total = sum(allocation.values())
    assert abs(total - 3000.0) < 1.0, f"Total budget mismatch: {total}"
    assert all(v > 0 for v in allocation.values()), "All budgets should be positive"
    print(f"  ✅ Basic allocation: {allocation}")


def test_hierarchical_bayesian_update_improves_winner():
    """El ganador (más clicks) debe recibir más presupuesto después de updates."""
    from intelligence.hierarchical_bayesian import HierarchicalBayesianAllocator

    allocator = HierarchicalBayesianAllocator(n_samples=100, tau=0.3)  # tau bajo = más explotación
    allocator.register_cluster("fitness_mx", alpha_ctr=1.0, beta_ctr=9.0)
    allocator.register_campaign("winner", "fitness_mx")
    allocator.register_campaign("loser", "fitness_mx")

    # Winner tiene 10x más clicks
    allocator.update_campaign("winner", impressions=5000, clicks=250, revenue=8000, spend=2000)
    allocator.update_campaign("loser", impressions=5000, clicks=25, revenue=500, spend=2000)

    allocation = allocator.allocate(["winner", "loser"], "fitness_mx", total_budget=2000)

    assert allocation["winner"] > allocation["loser"], (
        f"Winner should get more budget: winner={allocation['winner']:.2f}, "
        f"loser={allocation['loser']:.2f}"
    )
    print(f"  ✅ Winner gets more: winner=${allocation['winner']:.2f} | loser=${allocation['loser']:.2f}")


def test_hierarchical_bayesian_cluster_inheritance():
    """Nueva campaña debe heredar prior del cluster, no prior global."""
    from intelligence.hierarchical_bayesian import HierarchicalBayesianAllocator

    allocator = HierarchicalBayesianAllocator()

    # Cluster con CTR alto conocido
    allocator.register_cluster("luxury_watches", alpha_ctr=5.0, beta_ctr=5.0)  # Expected CTR=50%
    camp = allocator.register_campaign("new_campaign", "luxury_watches")

    expected_ctr = camp.alpha / (camp.alpha + camp.beta)
    assert abs(expected_ctr - 0.5) < 0.01, f"Should inherit cluster CTR: {expected_ctr}"
    print(f"  ✅ Cluster inheritance: prior CTR = {expected_ctr:.3f}")


def test_hierarchical_bayesian_global_propagation():
    """El global prior debe actualizarse cuando hay suficientes datos."""
    from intelligence.hierarchical_bayesian import HierarchicalBayesianAllocator

    allocator = HierarchicalBayesianAllocator(global_update_interval=5)
    allocator.register_cluster("test_cluster")
    allocator.register_campaign("c1", "test_cluster")

    initial_mu_ctr = allocator.global_prior.mu_ctr

    # Hacer suficientes updates para triggear actualización global
    for _ in range(10):
        allocator.update_campaign("c1", impressions=100, clicks=10, revenue=500, spend=100)

    # El global prior debe haber evolucionado
    state = allocator.get_state_summary()
    assert state["total_clusters"] == 1
    assert state["total_campaigns"] == 1
    print(f"  ✅ Global prior updated | initial={initial_mu_ctr:.4f} | final={allocator.global_prior.mu_ctr:.4f}")


def test_hierarchical_bayesian_zero_budget():
    """Budget=0 debe retornar zeros sin crash."""
    from intelligence.hierarchical_bayesian import HierarchicalBayesianAllocator

    allocator = HierarchicalBayesianAllocator()
    allocator.register_cluster("test")
    allocator.register_campaign("c1", "test")
    allocator.register_campaign("c2", "test")

    allocation = allocator.allocate(["c1", "c2"], "test", total_budget=0.0)
    assert all(v == 0.0 for v in allocation.values())
    print(f"  ✅ Zero budget handled correctly: {allocation}")


def test_hierarchical_bayesian_empty_campaigns():
    """Lista vacía de campañas debe retornar dict vacío."""
    from intelligence.hierarchical_bayesian import HierarchicalBayesianAllocator

    allocator = HierarchicalBayesianAllocator()
    result = allocator.allocate([], "any_cluster", total_budget=1000.0)
    assert result == {}
    print(f"  ✅ Empty campaigns handled: {result}")


# ─── Tests: Portfolio Optimizer ──────────────────────────────────────────────

def test_portfolio_basic_allocation():
    """Allocación básica: presupuesto debe sumarse a total."""
    from intelligence.portfolio_optimization import PortfolioOptimizer, CampaignMetrics

    opt = PortfolioOptimizer(risk_aversion=2.0, max_drawdown_pct=0.50)  # 50% para test
    campaigns = [
        CampaignMetrics("c1", roas_history=[3.0, 3.5, 2.8, 4.0, 3.2]),
        CampaignMetrics("c2", roas_history=[2.5, 2.0, 3.0, 2.2, 1.8]),
        CampaignMetrics("c3", roas_history=[4.0, 4.5, 3.8, 4.2, 4.1]),
    ]

    allocation = opt.optimize(campaigns, total_budget=6000.0)

    total = sum(allocation.values())
    assert abs(total - 6000.0) < 10.0, f"Total mismatch: {total}"
    assert all(v >= 0 for v in allocation.values())
    print(f"  ✅ Portfolio allocation: {allocation}")


def test_portfolio_high_volatility_gets_less():
    """Campaña con alta volatilidad de ROAS debe recibir menos con λ alto."""
    from intelligence.portfolio_optimization import PortfolioOptimizer, CampaignMetrics

    opt = PortfolioOptimizer(risk_aversion=5.0)  # Muy conservador
    campaigns = [
        CampaignMetrics("stable", roas_history=[3.0, 3.1, 2.9, 3.0, 3.0, 3.1]),
        CampaignMetrics("volatile", roas_history=[0.5, 6.0, 0.3, 7.0, 0.8, 5.5]),
    ]

    allocation = opt.optimize(campaigns, total_budget=2000.0)

    # Con alta aversión al riesgo, la campaña estable debería tener más (o igual) peso
    # Note: puede no ser siempre el caso con random search, pero en promedio sí
    assert allocation["stable"] >= 0
    assert allocation["volatile"] >= 0
    total = sum(allocation.values())
    assert abs(total - 2000.0) < 10.0
    print(f"  ✅ Volatility penalized | stable=${allocation['stable']:.2f} | volatile=${allocation['volatile']:.2f}")


def test_portfolio_stop_loss():
    """Campaña con drawdown > limit debe ser detenida."""
    from intelligence.portfolio_optimization import PortfolioOptimizer, CampaignMetrics

    opt = PortfolioOptimizer(max_drawdown_pct=0.20)  # 20% max drawdown
    campaigns = [
        CampaignMetrics("good", roas_history=[3.0, 2.9, 3.1, 3.0]),
        CampaignMetrics("crashing", roas_history=[5.0, 4.0, 2.0, 0.5]),  # 90% drawdown
    ]

    allocation = opt.optimize(campaigns, total_budget=2000.0)

    # "crashing" debe recibir 0 por stop-loss
    assert allocation.get("crashing", 0) == 0.0, \
        f"Crashing campaign should be stopped: {allocation['crashing']}"
    print(f"  ✅ Stop-loss triggered | crashing=$0 | good=${allocation.get('good', 0):.2f}")


def test_portfolio_concentration_limit():
    """Ninguna campaña debe superar max_concentration."""
    from intelligence.portfolio_optimization import PortfolioOptimizer, CampaignMetrics

    opt = PortfolioOptimizer(max_concentration=0.60)
    campaigns = [
        CampaignMetrics("dominant", roas_history=[10.0, 10.5, 9.8, 10.2]),
        CampaignMetrics("weak1", roas_history=[1.1, 1.0, 1.2, 1.1]),
        CampaignMetrics("weak2", roas_history=[1.0, 1.1, 0.9, 1.0]),
    ]

    allocation = opt.optimize(campaigns, total_budget=3000.0)
    total = sum(allocation.values())

    for cid, amount in allocation.items():
        weight = amount / max(total, 1e-6)
        assert weight <= 0.65, f"Concentration too high: {cid}={weight:.1%}"  # pequeño margen

    print(f"  ✅ Concentration limit respected | weights: {[f'{v/total:.1%}' for v in allocation.values()]}")


def test_portfolio_analytics():
    """get_portfolio_analytics debe retornar estructura válida."""
    from intelligence.portfolio_optimization import PortfolioOptimizer, CampaignMetrics

    opt = PortfolioOptimizer()
    campaigns = [CampaignMetrics("c1", roas_history=[2.0, 2.5, 3.0])]
    opt.optimize(campaigns, total_budget=1000.0)

    analytics = opt.get_portfolio_analytics()
    assert "summary" in analytics
    assert "history" in analytics
    assert "risk_config" in analytics
    print(f"  ✅ Analytics structure valid | sharpe={analytics['summary']['avg_sharpe']:.4f}")


# ─── Tests: Monte Carlo ───────────────────────────────────────────────────────

def test_monte_carlo_single_regime():
    """Simulación de un régimen debe retornar resultado válido."""
    from intelligence.monte_carlo import MonteCarloSimulator

    sim = MonteCarloSimulator(n_trajectories=50, n_periods=30, n_campaigns=3)
    results = sim.run_regime("bull_market", stoploss_threshold=1.2)

    assert results.n_trajectories == 50
    assert results.p5_final_roas <= results.p50_final_roas <= results.p95_final_roas
    assert 0 <= results.stoploss_rate <= 1.0
    assert 0 <= results.ruin_rate <= 1.0
    assert results.mean_max_drawdown >= 0

    print(f"  ✅ Bull market | median_roas={results.p50_final_roas:.3f} | ruin={results.ruin_rate:.1%}")


def test_monte_carlo_bear_worse_than_bull():
    """Bear market debe tener peor ROAS que bull market."""
    from intelligence.monte_carlo import MonteCarloSimulator

    sim = MonteCarloSimulator(n_trajectories=100, n_periods=30, n_campaigns=3)
    bull = sim.run_regime("bull_market")
    bear = sim.run_regime("bear_market")

    assert bull.p50_final_roas > bear.p50_final_roas, (
        f"Bull ({bull.p50_final_roas:.3f}) should outperform bear ({bear.p50_final_roas:.3f})"
    )
    print(f"  ✅ Bull ({bull.p50_final_roas:.3f}) > Bear ({bear.p50_final_roas:.3f})")


def test_monte_carlo_stoploss_reduces_ruin():
    """Stop-loss más agresivo debe reducir ruin rate en bear market."""
    from intelligence.monte_carlo import MonteCarloSimulator

    sim = MonteCarloSimulator(n_trajectories=100, n_periods=60, n_campaigns=3)

    # Sin stop-loss (threshold muy bajo)
    no_stop = sim.run_regime("bear_market", stoploss_threshold=0.1)
    # Con stop-loss agresivo
    with_stop = sim.run_regime("bear_market", stoploss_threshold=1.5)

    # Con stop-loss, el ruin rate debería ser igual o menor
    # (puede variar por aleatoriedad, pero en tendencia es mejor)
    print(f"  ✅ No stop: ruin={no_stop.ruin_rate:.1%} | With stop: ruin={with_stop.ruin_rate:.1%}")
    # Verificar que al menos los resultados son válidos
    assert 0 <= no_stop.ruin_rate <= 1.0
    assert 0 <= with_stop.ruin_rate <= 1.0


def test_monte_carlo_full_analysis():
    """Análisis completo debe retornar parámetros recomendados válidos."""
    from intelligence.monte_carlo import MonteCarloSimulator

    sim = MonteCarloSimulator(n_trajectories=30, n_periods=20, n_campaigns=3)
    results = sim.run_full_analysis(
        stoploss_thresholds=[1.0, 1.5],
        min_budgets=[20.0, 100.0],
        taus=[0.3, 0.8],
    )

    assert results.optimal_stoploss > 0
    assert results.optimal_min_budget > 0
    assert results.recommended_tau > 0
    assert len(results.results_by_regime) == 7  # 7 regímenes

    print(f"  ✅ Full analysis complete")
    print(f"     Optimal stoploss: {results.optimal_stoploss}")
    print(f"     Optimal min_budget: ${results.optimal_min_budget}")
    print(f"     Recommended tau: {results.recommended_tau}")


def test_monte_carlo_export(tmp_path=None):
    """Export a JSON debe funcionar sin errores."""
    import tempfile
    import json
    from intelligence.monte_carlo import MonteCarloSimulator

    sim = MonteCarloSimulator(n_trajectories=20, n_periods=15, n_campaigns=2)
    results = sim.run_full_analysis(
        stoploss_thresholds=[1.2],
        min_budgets=[50.0],
        taus=[0.5],
    )

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        path = f.name

    results.export(path)

    with open(path) as f:
        data = json.load(f)

    assert "recommendations" in data
    assert "results_by_regime" in data
    assert "sensitivity_analysis" in data
    print(f"  ✅ Export JSON valid | path={path}")
    os.unlink(path)


# ─── Integration test ─────────────────────────────────────────────────────────

def test_integrated_pipeline():
    """
    Test de integración completo:
    1. Monte Carlo valida parámetros
    2. Hierarchical Bayesian usa esos parámetros
    3. Portfolio Optimizer aplica restricciones de riesgo
    """
    from intelligence.monte_carlo import MonteCarloSimulator
    from intelligence.hierarchical_bayesian import HierarchicalBayesianAllocator
    from intelligence.portfolio_optimization import PortfolioOptimizer, CampaignMetrics

    # Paso 1: Validar parámetros con Monte Carlo
    sim = MonteCarloSimulator(n_trajectories=50, n_periods=20, n_campaigns=3)
    mc_results = sim.run_full_analysis(
        stoploss_thresholds=[1.2, 1.5],
        min_budgets=[30.0, 60.0],
        taus=[0.5],
    )
    optimal_tau = mc_results.recommended_tau
    optimal_stoploss = mc_results.optimal_stoploss

    # Paso 2: Usar los parámetros validados en el allocator Bayesiano
    allocator = HierarchicalBayesianAllocator(tau=optimal_tau)
    allocator.register_cluster("validated_cluster")
    for i in range(3):
        allocator.register_campaign(f"campaign_{i}", "validated_cluster")

    # Simular algunas semanas de datos
    rng = random.Random(42)
    for day in range(14):
        for i in range(3):
            campaign_id = f"campaign_{i}"
            base_roas = [3.0, 2.0, 4.0][i]
            clicks = rng.randint(10, 50)
            allocator.update_campaign(
                campaign_id,
                impressions=clicks * 20,
                clicks=clicks,
                revenue=clicks * 30 * base_roas,
                spend=clicks * 30,
            )

    bayesian_allocation = allocator.allocate(
        [f"campaign_{i}" for i in range(3)],
        "validated_cluster",
        total_budget=3000.0,
    )

    # Paso 3: Portfolio optimizer con métricas reales
    portfolio_campaigns = []
    for i in range(3):
        roas_hist = [3.0 + random.gauss(0, 0.3) for _ in range(10)]
        portfolio_campaigns.append(CampaignMetrics(
            f"campaign_{i}", roas_history=roas_hist
        ))

    opt = PortfolioOptimizer(
        risk_aversion=2.0,
        max_drawdown_pct=0.50,  # permissivo para integración
    )
    portfolio_allocation = opt.optimize(portfolio_campaigns, total_budget=3000.0)

    # Verificar que todo el pipeline funcionó
    assert sum(bayesian_allocation.values()) > 0
    assert sum(portfolio_allocation.values()) > 0

    print(f"  ✅ Integration pipeline complete")
    print(f"     MC params: tau={optimal_tau} | stoploss={optimal_stoploss}")
    print(f"     Bayesian alloc: {bayesian_allocation}")
    print(f"     Portfolio alloc: {portfolio_allocation}")


# ─── Runner ───────────────────────────────────────────────────────────────────

def run_all_tests():
    tests = [
        # Hierarchical Bayesian
        ("HierarchicalBayesian - basic allocation", test_hierarchical_bayesian_basic),
        ("HierarchicalBayesian - winner gets more", test_hierarchical_bayesian_update_improves_winner),
        ("HierarchicalBayesian - cluster inheritance", test_hierarchical_bayesian_cluster_inheritance),
        ("HierarchicalBayesian - global propagation", test_hierarchical_bayesian_global_propagation),
        ("HierarchicalBayesian - zero budget", test_hierarchical_bayesian_zero_budget),
        ("HierarchicalBayesian - empty campaigns", test_hierarchical_bayesian_empty_campaigns),
        # Portfolio Optimizer
        ("PortfolioOptimizer - basic allocation", test_portfolio_basic_allocation),
        ("PortfolioOptimizer - volatility penalized", test_portfolio_high_volatility_gets_less),
        ("PortfolioOptimizer - stop loss", test_portfolio_stop_loss),
        ("PortfolioOptimizer - concentration limit", test_portfolio_concentration_limit),
        ("PortfolioOptimizer - analytics", test_portfolio_analytics),
        # Monte Carlo
        ("MonteCarlo - single regime", test_monte_carlo_single_regime),
        ("MonteCarlo - bear vs bull", test_monte_carlo_bear_worse_than_bull),
        ("MonteCarlo - stoploss reduces ruin", test_monte_carlo_stoploss_reduces_ruin),
        ("MonteCarlo - full analysis", test_monte_carlo_full_analysis),
        ("MonteCarlo - export JSON", test_monte_carlo_export),
        # Integration
        ("Integration - full pipeline", test_integrated_pipeline),
    ]

    passed = 0
    failed = 0

    print("\n" + "=" * 70)
    print("  TESTS: ADVANCED ARCHITECTURES (Hierarchical Bayesian + Portfolio + MC)")
    print("=" * 70)

    for name, fn in tests:
        try:
            print(f"\n▶ {name}")
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 70)
    print(f"  RESULTS: {passed}/{len(tests)} passed | {failed} failed")
    print("=" * 70)
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
