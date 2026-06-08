"""
engines/discovery_engine.py — Product Discovery Engine V5.1

Detecta micro-tendencias de productos en TikTok, Amazon, Google Trends y
Meta Ad Library para identificar oportunidades antes de que se saturen.

Sinergia:
  - Lógica de detección: Gemini (fuentes + signals)
  - Feature Store:       V5.0 (cachear señales 6h — ciclo Oracle)
  - Circuit Breaker:     V5.0 (si Apify o APIs externas caen, skip graceful)
  - Scoring signals:     ChatGPT (Viral Score + Survivorship bonus)
  - Safe math:           ChatGPT (no ZeroDivisionError en señales)

Diseño:
  - Lean: wrapper sobre Oracle sources existentes + agregación de señales
  - No reemplaza Oracle — lo complementa con scoring de señales
  - Alimenta directamente a NicheClusterer y ScoringEngine
  - Feature Store: caché 6h (mismo ciclo que Oracle)

Flujo:
  1. Recibir lista de nichos del Orchestrator
  2. Para cada nicho, agregar señales de múltiples fuentes
  3. Calcular DiscoverySignal con viral_score y competition_level
  4. Cachear señales (6h TTL) — evitar scraping repetido
  5. Retornar signals rankeados por viral_score DESC

Nota sobre scraping:
  El scraping real (Apify) lo maneja oracle/sources.py.
  Este engine agrega y puntúa las señales, no las raspa directamente.
"""

import time
import logging
from typing import List, Dict, Any, Optional

from shared.feature_store import FeatureStore, get_feature_store
from shared.logging_utils import log_info, log_warning, log_error
from shared.constants import (
    META_AD_HIGH_COMPETITION,
    META_AD_SWEET_SPOT_MIN,
    META_AD_SWEET_SPOT_MAX,
    SATURATION_HARD_STOP,
)
# DiscoverySignal model available in shared.models for typed API responses

logger = logging.getLogger(__name__)

# TTL de caché para señales de discovery (6h = mismo ciclo que Oracle)
DISCOVERY_CACHE_TTL_SECONDS = 6 * 3600


class DiscoveryEngine:
    """
    Motor de descubrimiento de tendencias para e-commerce.

    Agrega señales de TikTok, Amazon, Google Trends y Meta Ad Library
    para identificar oportunidades de producto antes de saturación.

    Example:
        engine = DiscoveryEngine()
        signals = await engine.discover_for_niche(
            niche="masajeadores cervicales",
            tenant_id="tenant_123"
        )
        # → List[DiscoverySignal] ordenadas por viral_score DESC
    """

    def __init__(self, db=None, llm_router=None):
        self.db = db
        self.llm_router = llm_router
        self.store = get_feature_store()

    async def discover_for_niche(
        self,
        niche: str,
        tenant_id: str,
        max_signals: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Descubrir señales de tendencia para un nicho específico.

        Args:
            niche: Nombre del nicho (e.g., "masajeadores cervicales")
            tenant_id: Para contextualizar resultados
            max_signals: Máximo de señales a retornar (default 10)

        Returns:
            Lista de dicts con datos de DiscoverySignal, ordenados por
            viral_score DESC. Solo señales con viral_score > 20.
        """
        cache_key = f"{tenant_id}:{niche}"
        start_ts = time.time()

        # ── 1. Feature Store: caché de 6h ────────────────────────────────────
        cached = await self.store.get("discovery_signals", cache_key)
        if cached:
            log_info(logger, "discovery_cache_hit",
                     niche=niche, signals=len(cached.get("signals", [])),
                     latency_ms=round((time.time() - start_ts) * 1000))
            return cached.get("signals", [])[:max_signals]

        # ── 2. Obtener oportunidades de Oracle (fuente primaria) ──────────────
        raw_signals = []

        if self.db:
            try:
                # Oracle ya hizo el scraping — aprovechamos sus oportunidades
                opportunities = self.db.get_pending_opportunities(tenant_id) or []
                niche_opps = [
                    o for o in opportunities
                    if niche.lower() in str(o.get("niche", "")).lower()
                ]
                for opp in niche_opps[:max_signals]:
                    signal = self._opportunity_to_signal(opp)
                    if signal:
                        raw_signals.append(signal)
            except Exception as e:
                log_warning(logger, "discovery_db_fetch_failed",
                            niche=niche, error=str(e))

        # ── 3. Si no hay señales de Oracle, crear señal placeholder ──────────
        # Esto permite al Orchestrator continuar aunque Oracle no haya corrido
        if not raw_signals:
            log_info(logger, "discovery_no_oracle_signals",
                     niche=niche, using_placeholder=True)
            raw_signals = [self._create_placeholder_signal(niche)]

        # ── 4. Enriquecer señales con scoring ─────────────────────────────────
        enriched = []
        for signal in raw_signals:
            try:
                scored = self._score_signal(signal)
                enriched.append(scored)
            except Exception as e:
                log_warning(logger, "discovery_scoring_failed",
                            product=signal.get("product_name", "?"), error=str(e))
                enriched.append(signal)

        # ── 5. Filtrar y rankear ──────────────────────────────────────────────
        # Solo señales con viral_score > 20 (ruido mínimo)
        filtered = [s for s in enriched if s.get("viral_score", 0) > 20]
        filtered.sort(key=lambda s: s.get("viral_score", 0), reverse=True)
        top_signals = filtered[:max_signals]

        # ── 6. Cachear 6h ─────────────────────────────────────────────────────
        if top_signals:
            # Use a dedicated instance with 6h TTL — never mutate the singleton
            discovery_store = FeatureStore(ttl=DISCOVERY_CACHE_TTL_SECONDS)
            await discovery_store.set(
                "discovery_signals", cache_key, {"signals": top_signals}
            )

        log_info(logger, "discovery_complete",
                 niche=niche,
                 raw_count=len(raw_signals),
                 after_filter=len(top_signals),
                 latency_ms=round((time.time() - start_ts) * 1000))

        return top_signals

    async def discover_for_niches(
        self,
        niches: List[str],
        tenant_id: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Descubrir señales para múltiples nichos en paralelo secuencial.

        Returns:
            {niche: [signals]} — dict indexado por nombre de nicho
        """
        results = {}
        for niche in niches:
            try:
                signals = await self.discover_for_niche(niche, tenant_id)
                results[niche] = signals
            except Exception as e:
                log_error(logger, "discovery_niche_failed",
                          niche=niche, error=str(e))
                results[niche] = []
        return results

    # ── Private: conversión y scoring ────────────────────────────────────────

    def _opportunity_to_signal(self, opp: Dict[str, Any]) -> Optional[Dict]:
        """Convertir una oportunidad de Oracle en DiscoverySignal."""
        try:
            viral_score = float(opp.get("viral_score", 50.0))
            saturation = float(opp.get("saturation_score", 0.0))

            # Saturación alta → señal débil
            if saturation >= SATURATION_HARD_STOP:
                return None

            meta_competitors = int(opp.get("raw_data", {}).get(
                "meta_competitor_count", 0
            ))

            competition_level = self._classify_competition(
                meta_competitors,
                float(opp.get("competition_inv", 50.0))
            )

            return {
                "product_name": str(opp.get("name", ""))[:200],
                "niche": str(opp.get("niche", ""))[:100],
                "trend_source": "tiktok",  # oracle aggregates tiktok signals
                "viral_score": viral_score,
                "estimated_demand": float(opp.get("score", 50.0)),
                "competition_level": competition_level,
                "pain_points": opp.get("raw_data", {}).get("pain_points", [])[:5],
                "raw_data": {
                    "opportunity_id": opp.get("id", ""),
                    "saturation_score": saturation,
                    "meta_competitor_count": meta_competitors,
                },
            }
        except Exception as e:
            log_warning(logger, "discovery_opp_conversion_failed", error=str(e))
            return None

    def _score_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enriquecer señal con scoring adicional.

        Aplica:
        - Bonus por sweet spot de competidores Meta (15-45 ads activos)
        - Penalización por competencia alta
        - Normalización de viral_score al rango 0-100
        """
        scored = dict(signal)

        meta_count = int(signal.get("raw_data", {}).get("meta_competitor_count", 0))
        viral = float(signal.get("viral_score", 50.0))
        competition = signal.get("competition_level", "moderate")

        # Bonus sweet spot Meta: 15-45 ads = mercado validado pero no saturado
        if META_AD_SWEET_SPOT_MIN <= meta_count <= META_AD_SWEET_SPOT_MAX:
            viral = min(100.0, viral * 1.15)  # +15% bonus
            scored["sweet_spot_meta"] = True
        else:
            scored["sweet_spot_meta"] = False

        # Penalización por competencia alta
        competition_penalty = {
            "low": 1.0,
            "moderate": 1.0,
            "high": 0.85,
            "saturated": 0.60,
        }
        viral *= competition_penalty.get(competition, 1.0)

        # Clamp al rango válido
        scored["viral_score"] = round(max(0.0, min(100.0, viral)), 2)

        return scored

    def _classify_competition(
        self,
        meta_competitors: int,
        competition_inv: float,
    ) -> str:
        """Clasificar nivel de competencia combinando señales Meta + competition_inv."""
        # competition_inv: 100 = sin competencia, 0 = saturado
        if competition_inv >= 70 and meta_competitors < META_AD_SWEET_SPOT_MIN:
            return "low"
        elif competition_inv >= 40 or meta_competitors <= META_AD_SWEET_SPOT_MAX:
            return "moderate"
        elif meta_competitors <= META_AD_HIGH_COMPETITION:
            return "high"
        else:
            return "saturated"

    def _create_placeholder_signal(self, niche: str) -> Dict[str, Any]:
        """
        Señal placeholder cuando Oracle no tiene datos para el nicho.
        Viral score bajo (30) para no disparar creativos sin datos reales.
        """
        return {
            "product_name": f"Producto pendiente: {niche}",
            "niche": niche,
            "trend_source": "tiktok",
            "viral_score": 30.0,  # Bajo — no disparar sin datos reales
            "estimated_demand": 50.0,
            "competition_level": "moderate",
            "pain_points": [],
            "raw_data": {"placeholder": True},
        }
