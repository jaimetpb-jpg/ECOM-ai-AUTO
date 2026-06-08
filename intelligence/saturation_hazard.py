"""
intelligence/saturation_hazard.py — Saturation Hazard Model V4.1

Fixes aplicados (revisión crítica):
  [1] MEDIO:  Parámetros logísticos configurables (logistic_k, logistic_x0)
  [2] MEDIO:  Coeficientes del hazard configurables (desde env/config)
  [3] ALTO:   Documentación de ventanas temporales exactas
  [4] ALTO:   Logging estructurado con todos los componentes (para calibración)
  [5] MEDIO:  signal_from_db_rows con cálculo correcto de delta_cpm y delta_ctr

Definición exacta de señales [FIX 3]:
  delta_cpm  = (CPM_last_7d - CPM_prev_7d) / CPM_prev_7d
               Ejemplo: CPM subió de $10 → $12 → delta_cpm = +0.20 (+20%)
               Fuente: TikTok Ads / Meta Ads Manager, ventana 7d vs 7d anterior

  new_competitors = COUNT(advertisers_in_meta_ad_library WHERE first_seen <= 14d)
               Fuente: MetaAdLibraryClient.search_ads() filtrando days_running <= 14

  delta_ctr  = (CTR_last_7d - CTR_prev_7d) / CTR_prev_7d
               Ejemplo: CTR bajó de 2% → 1.8% → delta_ctr = -0.10 (-10%)
               Negativo = audiencia se cansa de los creativos (signal fuerte)

Calibración futura:
  Los coeficientes son teóricos. Recalibrar con LightGBM/logistic regression
  después de 3-6 meses de datos reales (outcomes de saturación registrados).
  Exponer via SATURATION_COEFS env var o config.yaml.
"""

import os
import json
import math
import logging
from dataclasses import dataclass
from typing import Optional
from shared.constants import (
    SATURATION_WATCH, SATURATION_CAUTION,
    SATURATION_EXIT, SATURATION_HARD_STOP,
)

logger = logging.getLogger(__name__)


@dataclass
class SaturationSignals:
    """
    Raw market signals for one campaign.
    Updated every 6h by the monitoring cycle.

    All signals are RELATIVE (ratios), not absolute values.
    See module docstring for exact calculation windows.
    """
    campaign_id:      str
    niche:            str
    delta_cpm:        float   # relative CPM change week-over-week
    new_competitors:  int     # new Meta advertisers in last 14 days
    delta_ctr:        float   # relative CTR change week-over-week (negative = bad)

    def validate(self):
        """Sanity-check signal ranges. Warns on suspicious values."""
        if not -1.0 <= self.delta_cpm <= 5.0:
            logger.warning(f"delta_cpm={self.delta_cpm:.2f} out of normal range [-1,5] — check calculation")
        if self.new_competitors < 0:
            logger.warning(f"new_competitors={self.new_competitors} is negative — check query")
        if not -1.0 <= self.delta_ctr <= 2.0:
            logger.warning(f"delta_ctr={self.delta_ctr:.2f} out of normal range [-1,2] — check calculation")


@dataclass
class SaturationResult:
    """Output of the hazard model for one campaign."""
    saturation_score:   float   # 0–1 composite logistic score
    hazard_prob_30d:    float   # P(saturate within 30 days)
    hazard_prob_14d:    float   # P(saturate within 14 days)
    action:             str     # SAFE | WATCH | CAUTION | EXIT | HARD_STOP
    reduce_budget_pct:  float   # 0.0 = no change, 0.5 = reduce 50%, 1.0 = stop
    explanation:        str
    signal_components:  dict    # [FIX 4]: all intermediate values for calibration


def _load_coefs_from_env() -> Optional[dict]:
    """
    Load hazard coefficients from SATURATION_COEFS env var (JSON).
    Allows hot-recalibration without code deploy.

    Example env:
      SATURATION_COEFS='{"delta_cpm":2.5,"new_competitors":0.18,"neg_delta_ctr":3.2}'
    """
    raw = os.getenv("SATURATION_COEFS")
    if raw:
        try:
            coefs = json.loads(raw)
            logger.info(f"saturation_coefs_loaded_from_env coefs={coefs}")
            return coefs
        except json.JSONDecodeError as e:
            logger.error(f"SATURATION_COEFS env var invalid JSON: {e}")
    return None


class SaturationHazardModel:
    """
    Cox proportional hazard model for niche saturation prediction.

    Instantiate once per application. Reuse across campaigns.
    All parameters configurable via constructor or SATURATION_COEFS env var.
    """

    def __init__(
        self,
        base_rate:   float = 0.01,
        coefs:       Optional[dict] = None,
        score_weights: Optional[dict] = None,
        logistic_k:  float = 6.0,    # [FIX 1]: steepness of sigmoid
        logistic_x0: float = 0.33,   # [FIX 1]: midpoint of sigmoid
    ):
        # Env var overrides constructor coefs [FIX 2]
        env_coefs = _load_coefs_from_env()
        self.base_rate = base_rate
        self.coefs = env_coefs or coefs or {
            "delta_cpm":       2.0,   # CPM increase weight
            "new_competitors": 0.15,  # per new advertiser
            "neg_delta_ctr":   3.0,   # CTR drop weight (strongest signal)
        }
        self.score_weights = score_weights or {
            "delta_cpm":       0.40,
            "new_competitors": 0.30,
            "neg_delta_ctr":   0.30,
        }
        self.logistic_k  = logistic_k   # [FIX 1]
        self.logistic_x0 = logistic_x0  # [FIX 1]

    def compute(self, signals: SaturationSignals) -> SaturationResult:
        """Main entry point. Validate signals then compute all outputs."""
        signals.validate()

        # Intermediate components [FIX 4]
        norm_cpm  = min(1.0, max(0.0, signals.delta_cpm / 0.50))
        norm_comp = 1.0 - math.exp(-signals.new_competitors / 5.0)
        norm_ctr  = min(1.0, max(0.0, -signals.delta_ctr / 0.30))

        w   = self.score_weights
        raw = (w["delta_cpm"]       * norm_cpm  +
               w["new_competitors"] * norm_comp +
               w["neg_delta_ctr"]   * norm_ctr)

        score    = self._logistic(raw)
        prob_30d = self._hazard_probability(signals, 30)
        prob_14d = self._hazard_probability(signals, 14)
        action, reduce_pct = self._determine_action(prob_30d, score)

        # [FIX 4]: Full component breakdown for logging / calibration
        components = {
            "norm_cpm":    round(norm_cpm,  4),
            "norm_comp":   round(norm_comp, 4),
            "norm_ctr":    round(norm_ctr,  4),
            "raw":         round(raw,       4),
            "logistic_k":  self.logistic_k,
            "logistic_x0": self.logistic_x0,
            "base_rate":   self.base_rate,
            "coefs":       self.coefs,
        }

        explanation = (
            f"ΔCPM={signals.delta_cpm:+.0%} | "
            f"NewComp={signals.new_competitors} | "
            f"ΔCTR={signals.delta_ctr:+.0%} | "
            f"Score={score:.3f} | P30d={prob_30d:.0%} | P14d={prob_14d:.0%} → {action}"
        )

        # [FIX 5]: Structured log for Grafana / calibration pipeline
        logger.debug(
            f"saturation_computed campaign={signals.campaign_id} niche={signals.niche} "
            f"score={score:.4f} hazard_30d={prob_30d:.4f} hazard_14d={prob_14d:.4f} "
            f"action={action} components={components}"
        )

        if action in ("EXIT", "HARD_STOP"):
            logger.warning(
                f"saturation_alert campaign={signals.campaign_id} "
                f"action={action} score={score:.3f} prob_30d={prob_30d:.0%}"
            )

        return SaturationResult(
            saturation_score  = round(score,    4),
            hazard_prob_30d   = round(prob_30d, 4),
            hazard_prob_14d   = round(prob_14d, 4),
            action            = action,
            reduce_budget_pct = reduce_pct,
            explanation       = explanation,
            signal_components = components,
        )

    def _logistic(self, raw: float) -> float:
        """Sigmoid with configurable k and x0. [FIX 1]"""
        return 1.0 / (1.0 + math.exp(-self.logistic_k * (raw - self.logistic_x0)))

    def _hazard_probability(self, s: SaturationSignals, horizon: int) -> float:
        """P(saturation within horizon days) via exponential survival model."""
        c   = self.coefs
        lin = (c["delta_cpm"]       * s.delta_cpm      +
               c["new_competitors"] * s.new_competitors +
               c["neg_delta_ctr"]   * (-s.delta_ctr))
        # Clip linear predictor to prevent exp overflow (lin > 700 → h = ∞ effectively)
        lin  = min(700.0, lin)
        h    = self.base_rate * math.exp(lin)
        prob = 1.0 - math.exp(-h * horizon)
        return float(min(1.0, max(0.0, prob)))

    def _determine_action(self, hazard_30d: float, score: float) -> tuple:
        if score >= SATURATION_HARD_STOP:
            return "HARD_STOP", 1.0
        elif hazard_30d >= SATURATION_EXIT or score >= SATURATION_EXIT:
            return "EXIT", 0.80
        elif hazard_30d >= SATURATION_CAUTION or score >= SATURATION_CAUTION:
            return "CAUTION", 0.50
        elif hazard_30d >= SATURATION_WATCH or score >= SATURATION_WATCH:
            return "WATCH", 0.30
        return "SAFE", 0.0

    def signals_from_db_rows(
        self, rows: list, campaign_id: str, niche: str
    ) -> SaturationSignals:
        """
        Build SaturationSignals from saturation_logs DB records.

        [FIX 5]: rows should contain pre-computed delta_cpm and delta_ctr
        (calculated as week-over-week ratios before insertion).
        If only raw CPM values exist, compute deltas here.
        """
        if not rows:
            logger.debug(f"signals_from_db_rows no_rows campaign={campaign_id} — using zeros")
            return SaturationSignals(
                campaign_id=campaign_id, niche=niche,
                delta_cpm=0.0, new_competitors=0, delta_ctr=0.0,
            )

        latest = rows[0]

        # Support both pre-computed deltas AND raw CPM/CTR values
        if "delta_cpm" in latest:
            delta_cpm = latest.get("delta_cpm", 0.0)
        elif "cpm_current" in latest and "cpm_previous" in latest:
            prev = latest["cpm_previous"]
            curr = latest["cpm_current"]
            delta_cpm = (curr - prev) / prev if prev > 0 else 0.0
        else:
            delta_cpm = 0.0

        if "delta_ctr" in latest:
            delta_ctr = latest.get("delta_ctr", 0.0)
        elif "ctr_current" in latest and "ctr_previous" in latest:
            prev = latest["ctr_previous"]
            curr = latest["ctr_current"]
            delta_ctr = (curr - prev) / prev if prev > 0 else 0.0
        else:
            delta_ctr = 0.0

        return SaturationSignals(
            campaign_id=campaign_id, niche=niche,
            delta_cpm=delta_cpm,
            new_competitors=latest.get("new_competitors", 0),
            delta_ctr=delta_ctr,
        )
