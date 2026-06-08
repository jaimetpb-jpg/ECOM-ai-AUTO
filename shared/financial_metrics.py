"""
shared/financial_metrics.py — Métricas financieras a nivel PORTAFOLIO

Por qué este módulo es crítico:
  - El DevGuide promete kill-switches por MER < 1.4 y TACOS > 30%
  - El código actual NO calcula MER ni TACOS en ningún lado (grep lo confirma)
  - ads_decision_engine.py solo evalúa ROAS por CAMPAÑA individual
  - Una campaña puede tener ROAS 2.0 (✅) mientras el portafolio entero sangra

Diferencias clave:
  - ROAS:  por campaña, mide eficiencia de UN ad
  - MER:   global, mide eficiencia de TODO el marketing del producto/cuenta
  - TACOS: % del revenue que se va en ads (sostenibilidad)
  - Margen Neto: lo que SOBRA después de COGS+envío+fees+devoluciones

Uso típico:
    from shared.financial_metrics import PortfolioMetrics, should_emergency_brake

    metrics = PortfolioMetrics(
        revenue_7d=4500.0,
        ad_spend_7d=2800.0,
        cogs_7d=1100.0,
        shipping_7d=350.0,
        platform_fees_7d=180.0,
        refunds_7d=540.0,
    )

    if should_emergency_brake(metrics).triggered:
        await slack.alert_critical(should_emergency_brake(metrics).reason)
        await pause_all_campaigns()
"""

from dataclasses import dataclass, field
from typing import NamedTuple


# ─── Thresholds — deberían vivir en shared/constants.py ──────────────────────
# Sugerencia: mover estos valores a constants.py para mantener single source of truth.

MER_HARD_FLOOR        = 1.4     # MER < 1.4 → emergency brake (kill all)
MER_WARNING           = 1.7     # MER < 1.7 → warning Slack (no kill)
MER_HEALTHY           = 2.0     # MER >= 2.0 → portafolio sano

TACOS_HARD_CEILING_PCT = 30.0   # TACOS > 30% → emergency brake
TACOS_WARNING_PCT      = 25.0   # TACOS > 25% → warning

NET_MARGIN_FLOOR_PCT   = 25.0   # Margen neto < 25% → producto no escalable
NET_MARGIN_HEALTHY_PCT = 40.0   # Margen neto >= 40% → producto premium


# ─── Datos de entrada ────────────────────────────────────────────────────────

@dataclass
class PortfolioMetrics:
    """Métricas agregadas a nivel portafolio en una ventana de N días."""
    revenue_7d:       float = 0.0
    ad_spend_7d:      float = 0.0
    cogs_7d:          float = 0.0  # Cost of Goods Sold
    shipping_7d:      float = 0.0
    platform_fees_7d: float = 0.0  # Stripe + marketplace fees
    refunds_7d:       float = 0.0
    window_days:      int   = 7

    def __post_init__(self) -> None:
        # Nunca permitir valores negativos (errores de input)
        for f in ("revenue_7d", "ad_spend_7d", "cogs_7d", "shipping_7d",
                  "platform_fees_7d", "refunds_7d"):
            if getattr(self, f) < 0:
                raise ValueError(f"{f} cannot be negative")


# ─── Cálculos puros (testeable, sin side effects) ────────────────────────────

def calc_mer(revenue: float, ad_spend: float) -> float:
    """
    Marketing Efficiency Ratio = revenue / ad_spend.

    Si ad_spend == 0, devuelve infinito (sin gasto = eficiencia perfecta o no medible).
    Usa clamp mínimo para evitar div-by-zero.
    """
    return revenue / max(ad_spend, 0.01)


def calc_tacos(ad_spend: float, revenue: float) -> float:
    """TACOS = (ad_spend / revenue) × 100. Devuelve % entre 0-100+."""
    return (ad_spend / max(revenue, 0.01)) * 100


def calc_net_margin_pct(metrics: PortfolioMetrics) -> float:
    """Margen neto % = (revenue - todos los costos) / revenue × 100."""
    total_costs = (
        metrics.cogs_7d
        + metrics.shipping_7d
        + metrics.platform_fees_7d
        + metrics.refunds_7d
        + metrics.ad_spend_7d
    )
    net = metrics.revenue_7d - total_costs
    return (net / max(metrics.revenue_7d, 0.01)) * 100


def calc_refund_rate_pct(refunds: float, revenue: float) -> float:
    """Tasa de devoluciones %. >10% es red flag, >15% es crítico."""
    return (refunds / max(revenue, 0.01)) * 100


def calc_break_even_roas(metrics: PortfolioMetrics) -> float:
    """
    ROAS mínimo para no perder dinero, dados COGS+envío+fees+devoluciones.
    Si ROAS observado < break_even_roas → cada venta pierde dinero.
    """
    revenue = max(metrics.revenue_7d, 0.01)
    cogs_pct      = metrics.cogs_7d / revenue
    shipping_pct  = metrics.shipping_7d / revenue
    fees_pct      = metrics.platform_fees_7d / revenue
    refunds_pct   = metrics.refunds_7d / revenue
    # ROAS break-even = 1 / (1 - costo_no_ads_como_pct_revenue)
    non_ad_costs_pct = cogs_pct + shipping_pct + fees_pct + refunds_pct
    return 1.0 / max(1.0 - non_ad_costs_pct, 0.01)


# ─── Decisiones automáticas ──────────────────────────────────────────────────

class BrakeDecision(NamedTuple):
    triggered: bool
    severity:  str           # "info" | "warning" | "critical"
    reason:    str
    metric:    str           # "mer" | "tacos" | "margin"
    value:     float
    threshold: float


def should_emergency_brake(metrics: PortfolioMetrics) -> BrakeDecision:
    """
    Decide si activar freno de emergencia (pausa TODAS las campañas).

    Reglas (de más a menos severa):
      1. MER < 1.4               → CRITICAL, brake on
      2. TACOS > 30%             → CRITICAL, brake on
      3. Margen neto < 25%       → WARNING, brake off (revisar pero no detener)
      4. MER < 1.7               → WARNING
      5. Todo bien               → no triggered
    """
    mer    = calc_mer(metrics.revenue_7d, metrics.ad_spend_7d)
    tacos  = calc_tacos(metrics.ad_spend_7d, metrics.revenue_7d)
    margin = calc_net_margin_pct(metrics)

    if mer < MER_HARD_FLOOR:
        return BrakeDecision(True, "critical",
                             f"MER {mer:.2f} < {MER_HARD_FLOOR} — portfolio sangra",
                             "mer", mer, MER_HARD_FLOOR)

    if tacos > TACOS_HARD_CEILING_PCT:
        return BrakeDecision(True, "critical",
                             f"TACOS {tacos:.1f}% > {TACOS_HARD_CEILING_PCT}% — gasto insostenible",
                             "tacos", tacos, TACOS_HARD_CEILING_PCT)

    if margin < NET_MARGIN_FLOOR_PCT:
        return BrakeDecision(False, "warning",
                             f"Margen neto {margin:.1f}% < {NET_MARGIN_FLOOR_PCT}% — revisar",
                             "margin", margin, NET_MARGIN_FLOOR_PCT)

    if mer < MER_WARNING:
        return BrakeDecision(False, "warning",
                             f"MER {mer:.2f} < {MER_WARNING} — vigilar",
                             "mer", mer, MER_WARNING)

    return BrakeDecision(False, "info",
                         f"MER {mer:.2f}, TACOS {tacos:.1f}%, margin {margin:.1f}%",
                         "ok", 0.0, 0.0)


def summarize_health(metrics: PortfolioMetrics) -> dict:
    """Reporte de una línea para Slack / dashboard."""
    return {
        "window_days":     metrics.window_days,
        "mer":             round(calc_mer(metrics.revenue_7d, metrics.ad_spend_7d), 2),
        "tacos_pct":       round(calc_tacos(metrics.ad_spend_7d, metrics.revenue_7d), 1),
        "net_margin_pct":  round(calc_net_margin_pct(metrics), 1),
        "refund_rate_pct": round(calc_refund_rate_pct(metrics.refunds_7d, metrics.revenue_7d), 1),
        "break_even_roas": round(calc_break_even_roas(metrics), 2),
        "revenue":         round(metrics.revenue_7d, 2),
        "ad_spend":        round(metrics.ad_spend_7d, 2),
    }
