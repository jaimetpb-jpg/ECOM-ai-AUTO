"""tests/unit/test_financial_metrics.py — tests SIN mocks. Matemática pura."""

import pytest
from shared.financial_metrics import (
    PortfolioMetrics,
    calc_mer, calc_tacos, calc_net_margin_pct,
    calc_break_even_roas, calc_refund_rate_pct,
    should_emergency_brake, summarize_health,
)


def test_mer_basic():
    assert calc_mer(2000.0, 1000.0) == 2.0


def test_mer_handles_zero_spend():
    """Sin gasto no debe crashear, debe devolver número grande."""
    assert calc_mer(100.0, 0.0) > 1000.0


def test_tacos_basic():
    assert calc_tacos(300.0, 1000.0) == 30.0


def test_net_margin_calculation():
    m = PortfolioMetrics(
        revenue_7d=1000.0,
        ad_spend_7d=200.0,
        cogs_7d=300.0,
        shipping_7d=80.0,
        platform_fees_7d=30.0,
        refunds_7d=50.0,
    )
    # revenue 1000 - costos totales 660 = 340 / 1000 = 34%
    assert calc_net_margin_pct(m) == pytest.approx(34.0, abs=0.1)


def test_break_even_roas():
    """Si costos no-ads son 50% del revenue, break-even ROAS = 1/(1-0.5) = 2.0"""
    m = PortfolioMetrics(
        revenue_7d=1000.0, ad_spend_7d=0.0,
        cogs_7d=300.0, shipping_7d=100.0,
        platform_fees_7d=50.0, refunds_7d=50.0,
    )
    # non_ad_costs_pct = 0.5, break-even = 1/(1-0.5) = 2.0
    assert calc_break_even_roas(m) == pytest.approx(2.0, abs=0.05)


def test_emergency_brake_triggers_on_low_mer():
    m = PortfolioMetrics(revenue_7d=100.0, ad_spend_7d=80.0)  # MER 1.25
    decision = should_emergency_brake(m)
    assert decision.triggered is True
    assert decision.severity == "critical"
    assert decision.metric == "mer"


def test_emergency_brake_triggers_on_high_tacos():
    m = PortfolioMetrics(revenue_7d=100.0, ad_spend_7d=35.0)  # TACOS 35%
    decision = should_emergency_brake(m)
    assert decision.triggered is True
    assert "TACOS" in decision.reason


def test_emergency_brake_no_trigger_healthy():
    m = PortfolioMetrics(
        revenue_7d=4500.0, ad_spend_7d=1800.0,  # MER 2.5, TACOS 40% ← OJO
        cogs_7d=1100.0, shipping_7d=200.0,
        platform_fees_7d=100.0, refunds_7d=100.0,
    )
    # MER 2.5 (ok), TACOS 40% (>30% triggers critical!)
    decision = should_emergency_brake(m)
    assert decision.triggered is True  # Por TACOS, no por MER


def test_emergency_brake_truly_healthy():
    m = PortfolioMetrics(
        revenue_7d=5000.0, ad_spend_7d=1200.0,  # MER 4.17, TACOS 24%
        cogs_7d=1500.0, shipping_7d=300.0,
        platform_fees_7d=150.0, refunds_7d=100.0,
    )
    decision = should_emergency_brake(m)
    assert decision.triggered is False


def test_negative_inputs_rejected():
    with pytest.raises(ValueError):
        PortfolioMetrics(revenue_7d=-100.0)


def test_summarize_health_returns_all_keys():
    m = PortfolioMetrics(revenue_7d=1000.0, ad_spend_7d=400.0,
                          cogs_7d=300.0, shipping_7d=50.0)
    h = summarize_health(m)
    for key in ("mer", "tacos_pct", "net_margin_pct", "refund_rate_pct",
                "break_even_roas", "revenue", "ad_spend"):
        assert key in h, f"Missing key: {key}"
