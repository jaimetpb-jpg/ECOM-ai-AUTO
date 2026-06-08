"""
engines/ads_decision_engine.py — Ads Decision Engine + Kill-Switch V5.1

Guardián financiero del sistema. Evalúa campañas activas y toma decisiones
automáticas basadas en ROAS con thresholds calibrados para DTC ecommerce.

Sinergia:
  - Reglas de negocio:  Gemini (ROAS thresholds + scaling logic + DB write-back)
  - Safe math:          ChatGPT (zero-division guard en todos los cálculos)
  - Circuit Breaker:    V5.0 (Slack no bloquea si está caído)
  - Bulkhead:           N/A (evaluate_campaign es síncrono — bulkhead no aplica en sync)
  - Constants:          shared/constants.py (single source of truth para thresholds)
  - Models:             shared/models.py (AdsDecision typed output)

Reglas del Kill-Switch (de constants.py):
  ROAS < 1.5 AND spend >= $50   → AUTO KILL (capital en pérdida)
  ROAS < 2.0 AND spend >= $200  → AUTO KILL (gasto alto sin retorno suficiente)
  ROAS >= 1.5 AND spend >= $40  → VALIDATED (funciona, mantener)
  ROAS >= 2.5 por 7+ días       → escalable a Meta full
  ROAS >= 2.5 por 14+ días      → escalable a Google
  ROAS >= 3.0 por 30+ días      → escalable a Amazon/ML

Human Gate:
  - Todas las decisiones de SCALE > $500 requieren aprobación Slack
  - KILL automático bajo $500 diarios (capital bajo, riesgo manejable)
  - KILL con alerta Slack siempre (visibilidad completa)

DB Write-Back (Gemini fix):
  - Toda decisión KILL/SCALE se persiste en decision_log (auditoría financiera)
  - KILL actualiza campaigns.status = "killed" (trail completo)
"""

import time
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

from shared.models import AdsDecision
from shared.slack_notifier import SlackNotifier
from shared.supabase_client import SupabaseClient
from shared.logging_utils import log_info, log_warning, log_error
from shared.constants import (
    ROAS_KILL_THRESHOLD_1, ROAS_KILL_THRESHOLD_2,
    SPEND_KILL_1, SPEND_KILL_2,
    ROAS_VALIDATED, SPEND_VALIDATED,
    ROAS_SCALE_META, DAYS_SCALE_META,
    # ROAS_SCALE_GOOGLE/AMAZON used in future scaling tiers (V5.2)
    SLACK_ALERTS, SLACK_APPROVALS,
)

logger = logging.getLogger(__name__)


class AdsDecisionEngine:
    """
    Motor de decisiones para campañas activas.

    Implementa el kill-switch financiero automático y lógica de escalado
    basados en ROAS observado + gasto acumulado.

    Safe Math (ChatGPT fix):
    - Toda división tiene max(valor, 0.01) en denominador
    - Nunca ZeroDivisionError en producción

    Example:
        engine = AdsDecisionEngine()
        decision = engine.evaluate_campaign({
            "campaign_id": "camp_123",
            "spend_usd": 75.0,
            "revenue_usd": 90.0,  # ROAS = 1.2 → KILL
            "days_active": 3,
            "tenant_id": "tenant_abc",
            "opportunity_id": "opp_xyz",
        })
        # → AdsDecision(action='KILL', reason='ROAS 1.20 < 1.5 con spend $75')
    """

    def __init__(
        self,
        slack: Optional[SlackNotifier] = None,
        db: Optional[SupabaseClient] = None,
    ):
        self.slack = slack or SlackNotifier()
        self.db = db or SupabaseClient()

    def evaluate_campaign(self, campaign: Dict[str, Any]) -> AdsDecision:
        """
        Evaluar una campaña y tomar decisión automática.

        Args:
            campaign: Dict con keys:
                campaign_id (str)
                spend_usd (float)    — gasto acumulado
                revenue_usd (float)  — revenue atribuido
                days_active (int)    — opcional; si omitido se calcula de started_at
                started_at (str)     — ISO datetime de inicio (para calcular days_active)
                impressions (int)    — optional, para logging
                clicks (int)         — optional, para CTR log
                tenant_id (str)      — para alerta Slack contextualizada
                opportunity_id (str) — para traceability

        Returns:
            AdsDecision con action, reason, roas, budget_change_pct
        """
        campaign_id = str(campaign.get("campaign_id") or campaign.get("id") or "unknown")
        tenant_id = str(campaign.get("tenant_id", "unknown"))
        opportunity_id = str(campaign.get("opportunity_id", ""))

        # ── Safe math: nunca dividir por cero ─────────────────────────────────
        spend = float(campaign.get("spend_usd", 0.0))
        revenue = float(campaign.get("revenue_usd", 0.0))
        impressions = int(campaign.get("impressions", 0))
        clicks = int(campaign.get("clicks", 0))

        # ── days_active: usar campo directo o calcular de started_at (Gemini/Grok fix) ──
        days_active = int(campaign.get("days_active", 0))
        if days_active == 0:
            started_at_str = campaign.get("started_at")
            if started_at_str:
                try:
                    started_at = datetime.fromisoformat(
                        started_at_str.replace("Z", "+00:00")
                    )
                    days_active = max(
                        0, (datetime.now(timezone.utc) - started_at).days
                    )
                except (ValueError, TypeError):
                    log_warning(logger, "ads_invalid_started_at",
                                campaign_id=campaign_id, value=started_at_str)

        # ROAS con safe division (ChatGPT fix)
        roas = revenue / max(spend, 0.01)

        # CTR real con safe division
        ctr = clicks / max(impressions, 1) if impressions > 0 else 0.0

        decision = self._apply_kill_switch_rules(
            campaign_id=campaign_id,
            roas=roas,
            spend=spend,
            revenue=revenue,
            days_active=days_active,
            ctr=ctr,
            tenant_id=tenant_id,
            opportunity_id=opportunity_id,
        )

        # ── Notificar Slack para KILL y SCALE ────────────────────────────────
        self._send_slack_notification(decision, campaign)

        # ── Persistir en DB: auditoría financiera (Gemini fix) ───────────────
        # Solo persistir KILL y SCALE — los HOLD no requieren audit trail
        if decision.action in ("KILL", "SCALE"):
            self._persist_decision(decision, tenant_id, roas, spend, revenue)

        return decision

    def evaluate_portfolio(
        self, campaigns: List[Dict[str, Any]]
    ) -> List[AdsDecision]:
        """
        Evaluar múltiples campañas del portfolio.

        Returns lista de decisiones, una por campaña.
        Útil para el ciclo de monitoreo de 6h.
        """
        decisions = []
        kills = 0
        scales = 0

        for campaign in campaigns:
            try:
                decision = self.evaluate_campaign(campaign)
                decisions.append(decision)
                if decision.action == "KILL":
                    kills += 1
                elif decision.action == "SCALE":
                    scales += 1
            except Exception as e:
                campaign_id = campaign.get("campaign_id", "unknown")
                log_error(logger, "ads_portfolio_eval_failed",
                          campaign_id=campaign_id, error=str(e))

        log_info(logger, "ads_portfolio_evaluated",
                 total=len(campaigns), kills=kills, scales=scales,
                 holds=len(campaigns) - kills - scales)

        return decisions

    # ── Private: kill-switch rules ────────────────────────────────────────────

    def _apply_kill_switch_rules(
        self,
        campaign_id: str,
        roas: float,
        spend: float,
        revenue: float,
        days_active: int,
        ctr: float,
        tenant_id: str,
        opportunity_id: str,
    ) -> AdsDecision:
        """
        Aplicar reglas de kill-switch en orden de prioridad.

        Orden de evaluación (de más crítico a menos):
        1. KILL por pérdida confirmada (ROAS < 1.5 con gasto suficiente)
        2. KILL por gasto alto sin retorno (ROAS < 2.0 con gasto > $200)
        3. SCALE si ROAS alto y tiempo suficiente
        4. HOLD (caso por defecto)
        """
        now = datetime.now(timezone.utc)

        # ── REGLA 1: KILL — pérdida confirmada ───────────────────────────────
        # ROAS < 1.5 significa que por cada $1 gastado, solo se recupera $1.50
        # Con márgenes típicos del 40%, esto es pérdida neta
        if roas < ROAS_KILL_THRESHOLD_1 and spend >= SPEND_KILL_1:
            reason = (
                f"ROAS {roas:.2f} < {ROAS_KILL_THRESHOLD_1} "
                f"con spend ${spend:.0f} (mínimo ${SPEND_KILL_1:.0f}) — "
                f"pérdida neta confirmada"
            )
            log_error(logger, "ads_kill_switch_activated",
                      campaign_id=campaign_id, roas=round(roas, 2),
                      spend=round(spend, 2), rule="roas_below_1_5",
                      tenant_id=tenant_id)
            return AdsDecision(
                action="KILL",
                reason=reason,
                roas=round(roas, 4),
                spend_usd=spend,
                revenue_usd=revenue,
                budget_change_pct=-100.0,
                campaign_id=campaign_id,
                decided_at=now,
            )

        # ── REGLA 2: KILL — gasto alto sin retorno suficiente ─────────────────
        if roas < ROAS_KILL_THRESHOLD_2 and roas < ROAS_VALIDATED and spend >= SPEND_KILL_2:
            reason = (
                f"ROAS {roas:.2f} < {ROAS_KILL_THRESHOLD_2} "
                f"con spend ${spend:.0f} (mínimo ${SPEND_KILL_2:.0f}) — "
                f"gasto elevado sin retorno adecuado"
            )
            log_error(logger, "ads_kill_switch_activated",
                      campaign_id=campaign_id, roas=round(roas, 2),
                      spend=round(spend, 2), rule="roas_below_2_0_high_spend",
                      tenant_id=tenant_id)
            return AdsDecision(
                action="KILL",
                reason=reason,
                roas=round(roas, 4),
                spend_usd=spend,
                revenue_usd=revenue,
                budget_change_pct=-100.0,
                campaign_id=campaign_id,
                decided_at=now,
            )

        # ── REGLA 3: SCALE — ROAS excelente y tiempo validado ────────────────
        if roas >= ROAS_SCALE_META and days_active >= DAYS_SCALE_META:
            pct = 20.0  # Escalar 20% presupuesto por ciclo (conservador)
            reason = (
                f"ROAS {roas:.2f} >= {ROAS_SCALE_META} "
                f"por {days_active} días (mínimo {DAYS_SCALE_META}) — "
                f"escalar presupuesto +{pct:.0f}%"
            )
            log_info(logger, "ads_scale_triggered",
                     campaign_id=campaign_id, roas=round(roas, 2),
                     days_active=days_active, budget_change_pct=pct)
            return AdsDecision(
                action="SCALE",
                reason=reason,
                roas=round(roas, 4),
                spend_usd=spend,
                revenue_usd=revenue,
                budget_change_pct=pct,
                campaign_id=campaign_id,
                decided_at=now,
            )

        # ── REGLA 4: HOLD — esperar más datos o ROAS en rango aceptable ───────
        if roas >= ROAS_VALIDATED and spend >= SPEND_VALIDATED:
            reason = (
                f"ROAS {roas:.2f} validado (>= {ROAS_VALIDATED}) "
                f"con spend ${spend:.0f} — mantener presupuesto actual"
            )
        elif spend < SPEND_VALIDATED:
            reason = (
                f"Gasto ${spend:.0f} < ${SPEND_VALIDATED:.0f} — "
                f"insuficiente para evaluar ROAS con significancia estadística"
            )
        else:
            reason = (
                f"ROAS {roas:.2f} por debajo del umbral de escala "
                f"({ROAS_SCALE_META}) — monitorear próximo ciclo (6h)"
            )

        log_info(logger, "ads_hold_decision",
                 campaign_id=campaign_id, roas=round(roas, 2),
                 spend=round(spend, 2), days_active=days_active)

        return AdsDecision(
            action="HOLD",
            reason=reason,
            roas=round(roas, 4),
            spend_usd=spend,
            revenue_usd=revenue,
            budget_change_pct=0.0,
            campaign_id=campaign_id,
            decided_at=now,
        )

    def _send_slack_notification(
        self,
        decision: AdsDecision,
        campaign: Dict[str, Any],
    ) -> None:
        """
        Enviar notificación Slack para KILL y SCALE.
        HOLD no genera notificación (demasiado ruido).

        Circuit Breaker de Anthropic no aplica aquí — Slack es separado.
        Si Slack falla, se loguea pero NO bloquea la decisión.
        """
        if decision.action == "HOLD":
            return  # No notificar HOLDs — demasiado ruido

        try:
            if decision.action == "KILL":
                channel = SLACK_ALERTS
                message = (
                    f"🚨 *KILL-SWITCH ACTIVADO*\n"
                    f"Campaign: `{decision.campaign_id}`\n"
                    f"ROAS: *{decision.roas:.2f}* | "
                    f"Spend: ${decision.spend_usd:.0f} | "
                    f"Revenue: ${decision.revenue_usd:.0f}\n"
                    f"Razón: {decision.reason}\n"
                    f"Acción: Campaña *PAUSADA AUTOMÁTICAMENTE*"
                )
                decision.alert_sent = True

            elif decision.action == "SCALE":
                channel = SLACK_APPROVALS
                message = (
                    f"📈 *SCALE OPPORTUNITY*\n"
                    f"Campaign: `{decision.campaign_id}`\n"
                    f"ROAS: *{decision.roas:.2f}* | "
                    f"Spend: ${decision.spend_usd:.0f} | "
                    f"Revenue: ${decision.revenue_usd:.0f}\n"
                    f"Razón: {decision.reason}\n"
                    f"Acción propuesta: +{decision.budget_change_pct:.0f}% presupuesto\n"
                    f"_Requiere aprobación para escalar > $500_"
                )
                decision.alert_sent = True
            else:
                return

            self.slack._post(channel=channel, text=message)

        except Exception as e:
            # Slack falla → NO bloquear la decisión, solo loguear
            log_warning(logger, "ads_slack_notification_failed",
                        campaign_id=decision.campaign_id,
                        action=decision.action,
                        error=str(e))

    def _persist_decision(
        self,
        decision: AdsDecision,
        tenant_id: str,
        roas: float,
        spend: float,
        revenue: float,
    ) -> None:
        """
        Persistir decisión en DB para auditoría financiera (Gemini fix).

        - Registra en decision_log (quién, qué, cuándo, por qué)
        - Si KILL: actualiza campaigns.status = "killed"
        - Si falla DB: solo loguea — NO bloquea la decisión
        """
        try:
            # 1. Registrar en decision_log (auditoría completa)
            self.db.log_decision({
                "tenant_id": tenant_id,
                "entity_type": "campaign",
                "entity_id": decision.campaign_id,
                "action": decision.action.lower(),
                "trigger": "auto_roas_evaluator",
                "reason": decision.reason,
                "data": {
                    "roas": roas,
                    "spend_usd": spend,
                    "revenue_usd": revenue,
                    "budget_change_pct": decision.budget_change_pct,
                },
            })

            # 2. Si KILL: actualizar status de la campaña
            if decision.action == "KILL":
                self.db.update_campaign_metrics(
                    decision.campaign_id,
                    {
                        "status": "killed",
                        "ended_at": decision.decided_at.isoformat()
                        if decision.decided_at else None,
                    },
                )

        except Exception as e:
            # DB falla → NO bloquear — solo loguear (sistema continúa)
            log_warning(logger, "ads_persist_decision_failed",
                        campaign_id=decision.campaign_id,
                        action=decision.action,
                        error=str(e))
