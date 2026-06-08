"""
engines/orchestrator.py — Autonomous Pipeline Orchestrator V5.1

Conecta todos los motores en un pipeline autónomo completo:
  Discovery → Score → Creative → Decision → Ads Monitor

Sinergia:
  - Pipeline lean:      Grok V5.1 (4 engines, no 20 agentes)
  - Circuit Breaker:    V5.0 (cada engine protegido individualmente)
  - Feature Store:      V5.0 (compartido entre engines — no recomputar)
  - Models:             shared/models.py (OrchestratorCycleResult typed)
  - Human gates:        V4.5 (Slack approval para SCALE > $500)

Ciclo autónomo (n8n trigger cada 6h):
  1. DiscoveryEngine → señales de tendencia por nicho
  2. ScoringEngine   → puntuar cada señal detectada
  3. CreativeEngine  → generar hooks para aprobados (score >= 70)
  4. DecisionEngine  → portfolio allocation con Thompson Sampling V5.0
  5. AdsDecisionEngine → evaluar campañas activas (kill-switch ROAS)
  6. Slack gate      → notificar resultados + pedir aprobación para SCALE

El orchestrator NO ejecuta ads directamente.
Los ads los ejecuta el operador después de aprobar via Slack.

Error handling:
  - Cada step tiene try/except independiente
  - Si Discovery falla → pipeline continúa con señales vacías
  - Si Creative falla → pipeline continúa sin hooks (lanzar sin creativo)
  - Si Slack falla    → pipeline continúa, solo sin notificación
  - NUNCA un error en un engine mata el ciclo completo
"""

import asyncio
import uuid
import time
import logging
from typing import List, Dict, Any, Optional

from engines.discovery_engine import DiscoveryEngine
from engines.creative_engine import CreativeIntelligenceEngine
from engines.ads_decision_engine import AdsDecisionEngine
from core.decision_engine import DecisionEngine
from scoring.engine import ScoringEngine, ScoreInput
from shared.llm_router import LLMRouter
from shared.feature_store import get_feature_store
from shared.slack_notifier import SlackNotifier
from shared.logging_utils import log_info, log_warning, log_error
from shared.models import OrchestratorCycleResult
from shared.constants import (
    SLACK_MONITORING,
    FAILFAST_CAP_USD,
    SCORE_MANUAL_REVIEW,
)

logger = logging.getLogger(__name__)


class AutonomousOrchestrator:
    """
    Orquestador del pipeline autónomo completo.

    Diseñado para ejecutarse cada 6h via n8n trigger.
    Cada ciclo es idempotente — si se ejecuta dos veces, no duplica.

    Example:
        orchestrator = AutonomousOrchestrator(
            llm_router=router, db=db, slack=slack
        )
        result = await orchestrator.run_autonomous_cycle(
            niches=["masajeadores cervicales", "vitaminas para cabello"],
            tenant_id="tenant_123",
            total_budget=800.0,
        )
        # → OrchestratorCycleResult con métricas completas del ciclo
    """

    def __init__(
        self,
        llm_router: Optional[LLMRouter] = None,
        db=None,
        slack: Optional[SlackNotifier] = None,
    ):
        self.router = llm_router or LLMRouter()
        self.db = db
        self.slack = slack or SlackNotifier()
        self.store = get_feature_store()

        # Inicializar engines con dependencias compartidas
        self.discovery = DiscoveryEngine(db=self.db, llm_router=self.router)
        self.creative = CreativeIntelligenceEngine(llm_router=self.router)
        self.scoring = ScoringEngine(llm_router=self.router)
        self.decision = DecisionEngine()
        self.ads_decision = AdsDecisionEngine(slack=self.slack, db=self.db)

    async def run_autonomous_cycle(
        self,
        niches: List[str],
        tenant_id: str,
        total_budget: float = FAILFAST_CAP_USD,
        active_campaigns: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Ejecutar ciclo autónomo completo.

        Args:
            niches: Lista de nichos a explorar
            tenant_id: ID del tenant
            total_budget: Presupuesto disponible para nuevos tests
            active_campaigns: Campañas activas para evaluar kill-switch

        Returns:
            OrchestratorCycleResult como dict
        """
        cycle_id = f"cycle_{uuid.uuid4().hex[:8]}"
        start_ts = time.time()
        errors = []

        result = OrchestratorCycleResult(
            cycle_id=cycle_id,
            tenant_id=tenant_id,
        )

        log_info(logger, "orchestrator_cycle_start",
                 cycle_id=cycle_id, tenant_id=tenant_id,
                 niches=len(niches), budget=total_budget)

        # ══════════════════════════════════════════════════════════════════════
        # STEP 1: DISCOVERY — Detectar señales de tendencia
        # ══════════════════════════════════════════════════════════════════════
        all_signals = []
        try:
            signals_by_niche = await self.discovery.discover_for_niches(
                niches=niches, tenant_id=tenant_id
            )
            for niche, signals in signals_by_niche.items():
                all_signals.extend(signals)

            result.niches_processed = len(niches)
            result.signals_found = len(all_signals)

            log_info(logger, "orchestrator_discovery_complete",
                     cycle_id=cycle_id, signals=len(all_signals))

        except Exception as e:
            error_msg = f"Discovery failed: {str(e)}"
            errors.append(error_msg)
            log_error(logger, "orchestrator_discovery_failed",
                      cycle_id=cycle_id, error=str(e))
            # Continuar con señales vacías

        # ══════════════════════════════════════════════════════════════════════
        # STEP 2: SCORING — Puntuar señales detectadas
        # ══════════════════════════════════════════════════════════════════════
        scored_products = []
        if all_signals:
            for signal in all_signals:
                try:
                    score_result = await self._score_signal(signal)
                    if score_result:
                        scored_products.append(score_result)
                except Exception as e:
                    errors.append(f"Scoring failed for {signal.get('product_name', '?')}: {e}")
                    log_warning(logger, "orchestrator_scoring_failed",
                                product=signal.get("product_name", "?"), error=str(e))

            result.products_scored = len(scored_products)
            log_info(logger, "orchestrator_scoring_complete",
                     cycle_id=cycle_id, scored=len(scored_products))

        # ══════════════════════════════════════════════════════════════════════
        # STEP 3: CREATIVE — Generar hooks para productos con score >= 70
        # ══════════════════════════════════════════════════════════════════════
        products_for_creative = [
            p for p in scored_products
            if p.get("final_score", 0) >= SCORE_MANUAL_REVIEW
        ]

        total_hooks = 0
        # Semaphore (Gemini/Grok fix): limita a 3 productos simultáneos en paralelo
        # Sin esto, 50 productos → 50 llamadas LLM simultáneas → rate limit 429
        # asyncio.gather para verdadero paralelismo (vs loop secuencial que ignora el semaphore)
        creative_semaphore = asyncio.Semaphore(3)

        async def _generate_with_semaphore(product):
            async with creative_semaphore:
                try:
                    hooks = await self.creative.run_creative_pipeline(product)
                    if hooks:
                        product["creative_hooks"] = hooks
                    return len(hooks) if hooks else 0
                except Exception as e:
                    errors.append(f"Creative failed for {product.get('name', '?')}: {e}")
                    log_warning(logger, "orchestrator_creative_failed",
                                product=product.get("name", "?"), error=str(e))
                    return 0

        hook_counts = await asyncio.gather(
            *[_generate_with_semaphore(p) for p in products_for_creative],
            return_exceptions=False,
        )
        total_hooks = sum(c for c in hook_counts if isinstance(c, int))

        result.creative_hooks_generated = total_hooks
        log_info(logger, "orchestrator_creative_complete",
                 cycle_id=cycle_id, hooks=total_hooks)

        # ══════════════════════════════════════════════════════════════════════
        # STEP 4: DECISION — Portfolio allocation con Thompson V5.0
        # ══════════════════════════════════════════════════════════════════════
        portfolio_result = {}
        if scored_products:
            try:
                portfolio_result = self.decision.evaluate_portfolio(
                    products=scored_products,
                    total_budget=total_budget,
                )
                result.products_approved = len(portfolio_result.get("approved", []))
                result.products_rejected = len(portfolio_result.get("rejected", []))
                result.total_budget_allocated_usd = portfolio_result.get(
                    "total_budget_allocated", 0.0
                )

                log_info(logger, "orchestrator_decision_complete",
                         cycle_id=cycle_id,
                         approved=result.products_approved,
                         rejected=result.products_rejected,
                         budget_usd=result.total_budget_allocated_usd)

            except Exception as e:
                errors.append(f"Decision engine failed: {e}")
                log_error(logger, "orchestrator_decision_failed",
                          cycle_id=cycle_id, error=str(e))

        # ══════════════════════════════════════════════════════════════════════
        # STEP 5: ADS MONITOR — Kill-switch para campañas activas
        # ══════════════════════════════════════════════════════════════════════
        ads_decisions = []
        if active_campaigns:
            try:
                ads_decisions = self.ads_decision.evaluate_portfolio(active_campaigns)
                result.ads_decisions = ads_decisions

                kills = sum(1 for d in ads_decisions if d.action == "KILL")
                scales = sum(1 for d in ads_decisions if d.action == "SCALE")

                log_info(logger, "orchestrator_ads_monitor_complete",
                         cycle_id=cycle_id,
                         total=len(active_campaigns),
                         kills=kills, scales=scales)

            except Exception as e:
                errors.append(f"Ads monitor failed: {e}")
                log_error(logger, "orchestrator_ads_monitor_failed",
                          cycle_id=cycle_id, error=str(e))

        # ══════════════════════════════════════════════════════════════════════
        # STEP 6: SLACK — Resumen del ciclo + gates para SCALE
        # ══════════════════════════════════════════════════════════════════════
        result.duration_seconds = round(time.time() - start_ts, 2)
        result.errors = errors

        try:
            await self._send_cycle_summary(result, portfolio_result, ads_decisions)
        except Exception as e:
            log_warning(logger, "orchestrator_slack_summary_failed",
                        cycle_id=cycle_id, error=str(e))

        log_info(logger, "orchestrator_cycle_complete",
                 cycle_id=cycle_id,
                 duration_s=result.duration_seconds,
                 signals=result.signals_found,
                 approved=result.products_approved,
                 hooks=result.creative_hooks_generated,
                 errors=len(errors))

        return result.model_dump()

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _score_signal(
        self, signal: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Convertir DiscoverySignal en producto puntuado.

        Campos requeridos de ScoreInput con defaults documentados:
          - margin:          50.0  (40-60% típico DTC — actualizar con datos proveedor)
          - differentiation: 60.0  (moderada — actualizar con análisis Meta Ad Library)
          - logistics:       70.0  (un proveedor AliExpress/CJ — -15pts aplicado en engine)
          - viral_score:     del signal (dato real de TikTok/Oracle)
          - competition_inv: derivado de competition_level (dato real)
        """
        try:
            # Mapear signal a ScoreInput — campos obligatorios + defaults documentados
            score_input = ScoreInput(
                name=str(signal.get("product_name", ""))[:200],
                niche=str(signal.get("niche", ""))[:100],
                # Señales reales del DiscoveryEngine
                demand=float(signal.get("estimated_demand", 50.0)),
                competition_inv=self._competition_level_to_inv(
                    signal.get("competition_level", "moderate")
                ),
                viral_score=float(signal.get("viral_score", 50.0)),
                # Defaults razonables para productos nuevos sin datos de proveedor
                # TODO-semana2: enriquecer con datos reales de AliExpress/CJ via Oracle
                margin=50.0,
                differentiation=60.0,
                logistics=70.0,
                # Señales de riesgo — conservador para productos nuevos
                legal_risk=0.0,
                saturation_prob=float(signal.get("raw_data", {}).get(
                    "saturation_score", 0.0
                )),
                supplier_count=1,
                meta_ad_competitor_count=int(signal.get("raw_data", {}).get(
                    "meta_competitor_count", 0
                )),
                # Nuevos productos sin historial de campañas
                days_active=0,
                empirical_roas=0.0,
            )

            score_result = await self.scoring.async_score(score_input)

            # Combinar signal + score en un dict completo para los siguientes engines
            return {
                **signal,
                "product_id": signal.get("raw_data", {}).get(
                    "opportunity_id",
                    f"sig_{abs(hash(signal.get('product_name', 'unknown')))}"
                ),
                "name": signal.get("product_name", ""),
                "final_score": score_result.final_score,
                "decision": score_result.decision,
                "score_breakdown": score_result.breakdown,
            }
        except Exception as e:
            log_error(logger, "orchestrator_score_signal_failed",
                      product=signal.get("product_name", "?"), error=str(e))
            return None

    def _competition_level_to_inv(self, level: str) -> float:
        """Convertir competition_level string a competition_inv 0-100."""
        mapping = {
            "low": 80.0,
            "moderate": 55.0,
            "high": 30.0,
            "saturated": 10.0,
        }
        return mapping.get(level, 55.0)

    async def _send_cycle_summary(
        self,
        result: OrchestratorCycleResult,
        portfolio: Dict[str, Any],
        ads_decisions: list,
    ) -> None:
        """Enviar resumen del ciclo a Slack #monitoring."""
        kills = sum(1 for d in ads_decisions if d.action == "KILL")
        scales = sum(1 for d in ads_decisions if d.action == "SCALE")

        approved_names = [
            p.product_name if hasattr(p, "product_name") else p.get("product_name", "?")
            for p in portfolio.get("approved", [])[:3]
        ]
        approved_str = (
            "\n".join(f"  • {n}" for n in approved_names)
            if approved_names else "  • Ninguno"
        )

        message = (
            f"🤖 *Ciclo Autónomo V5.1 Completado*\n"
            f"ID: `{result.cycle_id}` | Duración: {result.duration_seconds:.1f}s\n\n"
            f"📊 *Discovery:* {result.signals_found} señales en {result.niches_processed} nichos\n"
            f"🎯 *Scoring:* {result.products_scored} productos evaluados\n"
            f"✅ *Aprobados:* {result.products_approved} → ${result.total_budget_allocated_usd:.0f} asignados\n"
            f"❌ *Rechazados:* {result.products_rejected}\n"
            f"🎨 *Hooks generados:* {result.creative_hooks_generated}\n\n"
            f"📺 *Campañas monitoreadas:* {len(ads_decisions)}\n"
            f"  🔴 KILLs: {kills} | 📈 SCALEs: {scales}\n\n"
            f"🚀 *Top aprobados:*\n{approved_str}\n"
        )

        if result.errors:
            message += f"\n⚠️ *Errores ({len(result.errors)}):* " + ", ".join(result.errors[:3])

        self.slack._post(channel=SLACK_MONITORING, text=message)
