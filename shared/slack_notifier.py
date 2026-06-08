"""
shared/slack_notifier.py — Human Gates + Slack Alerts

All human gates go through this module. A gate timeout = safe default = no action.
Slack SDK is lazy-loaded so module imports without the package installed.
"""

import os
import asyncio
import time
import logging
from shared.logging_utils import log_info, log_warning, log_error
from typing import Optional

logger = logging.getLogger(__name__)

SLACK_OPPORTUNITIES = "#opportunities"
SLACK_APPROVALS     = "#approvals"
SLACK_ALERTS        = "#alerts"
SLACK_MONITORING    = "#monitoring"


class SlackNotifier:
    """Slack notifications and interactive human gates."""

    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            token = os.getenv("SLACK_BOT_TOKEN")
            if not token:
                logger.warning("SLACK_BOT_TOKEN not set — Slack notifications disabled")
                return None
            try:
                from slack_sdk import WebClient
                self._client = WebClient(token=token)
            except ImportError:
                logger.warning("slack_sdk not installed — install with: pip install slack-sdk")
                return None
        return self._client

    async def request_approval(
        self,
        title: str,
        details: str,
        timeout_minutes: int = 30,
        channel: str = SLACK_APPROVALS,
        metadata: Optional[dict] = None,
    ) -> bool:
        """
        Send interactive approval request. Returns True if approved.
        Timeout = safe default = False (no action taken).
        NEVER bypass this for any spend decision.
        
        Uses asyncio.wait_for to ensure timeout is enforced even if polling hangs.
        """
        approval_id = f"approval_{int(time.time())}"
        client = self._get_client()
        if not client:
            from shared.logging_utils import log_warning
            log_warning(logger, "approval_skipped_no_slack", title=title)
            return False

        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": f"🔔 {title}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": details}},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"⏱ Timeout: *{timeout_minutes} min*. No response = NO ACTION (safe default)."}},
            {"type": "actions", "block_id": approval_id, "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ APPROVE"},
                 "style": "primary", "value": "approved",
                 "action_id": f"{approval_id}_approve"},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ REJECT"},
                 "style": "danger", "value": "rejected",
                 "action_id": f"{approval_id}_reject"},
            ]},
        ]

        try:
            client.chat_postMessage(channel=channel, blocks=blocks, text=title)
            logger.info(f"Approval requested: {title} (timeout={timeout_minutes}min)")
        except Exception as e:
            logger.error(f"Approval send failed: {e}")
            return False

        # Wrap polling with asyncio.wait_for for guaranteed timeout
        try:
            return await asyncio.wait_for(
                self._poll_for_response(approval_id, timeout_minutes),
                timeout=timeout_minutes * 60 + 5  # +5s grace period
            )
        except asyncio.TimeoutError:
            from shared.logging_utils import log_warning
            log_warning(logger, "approval_timeout", title=title, timeout_minutes=timeout_minutes)
            self._post(channel, f"⏱ Approval timed out: *{title}* — no action taken (safe default)")
            return False

    async def _poll_for_response(self, approval_id: str, timeout_minutes: int) -> bool:
        """Poll for approval response with hard deadline."""
        deadline = time.time() + timeout_minutes * 60
        while time.time() < deadline:
            await asyncio.sleep(15)
            response = self._check_response(approval_id)
            if response is not None:
                logger.info(f"Approval response: {approval_id} → {'APPROVED' if response else 'REJECTED'}")
                return response
        # If we hit deadline without response, return False
        return False

    def _check_response(self, approval_id: str) -> Optional[bool]:
        """
        Check if user clicked approve/reject.
        TODO: Implement Slack Bolt socket mode for real-time callbacks.
        Production pattern: store response in Redis, check here.
        """
        return None  # None = still waiting

    def notify_opportunity(self, name: str, score: float, breakdown: dict, url: str = ""):
        score_bar = "🟢" * min(10, int(score / 10)) + "⬜" * max(0, 10 - int(score / 10))
        text = (
            f"*🎯 New Opportunity!* Score: *{score:.1f}/100* {score_bar}\n"
            f"Product: *{name}*\n"
            f"D={breakdown.get('demand', 0):.0f} | C={breakdown.get('competition', 0):.0f} | "
            f"M={breakdown.get('margin', 0):.0f} | V={breakdown.get('viral', 0):.0f}"
        )
        if url:
            text += f"\n<{url}|View details>"
        self._post(SLACK_OPPORTUNITIES, text)

    def notify_roas_decision(self, product: str, roas: float, spend: float, action: str, platform: str):
        emoji = {"KILL": "🔴", "VALIDATED": "🟢", "HOLD": "🟡", "SCALE_META": "🚀",
                 "SCALE_GOOGLE": "🔍", "SCALE_AMAZON": "🌎"}.get(action, "⚪")
        text = (
            f"{emoji} *{action}* — {product}\n"
            f"Platform: {platform} | ROAS: *{roas:.2f}x* | Spend: *${spend:.2f}*"
        )
        self._post(SLACK_MONITORING, text)

    def notify_saturation(self, campaign: str, hazard_prob: float, action: str):
        self._post(SLACK_ALERTS,
            f"⚠️ *Saturation Signal* — {campaign}\n"
            f"Hazard P30d: *{hazard_prob:.0%}* → Action: *{action}*")

    def notify_failfast_warning(self, current_spend: float, cap: float, tenant_id: str):
        pct = current_spend / cap * 100
        self._post(SLACK_ALERTS,
            f"💸 *Fail-Fast Warning* (tenant: {tenant_id})\n"
            f"Portfolio spend: *${current_spend:.0f} / ${cap:.0f}* ({pct:.0f}%)\n"
            f"No winners yet. Review strategy if this reaches $800.")

    def notify_alert(self, message: str, channel: str = SLACK_ALERTS):
        self._post(channel, message)

    def _post(self, channel: str, text: str):
        client = self._get_client()
        if not client:
            logger.info(f"[Slack disabled] {channel}: {text[:100]}")
            return
        try:
            client.chat_postMessage(channel=channel, text=text, mrkdwn=True)
        except Exception as e:
            logger.error(f"Slack post failed on {channel}: {e}")
