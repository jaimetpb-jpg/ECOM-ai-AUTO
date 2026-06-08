"""
shared/security.py — Input sanitization & security utilities.

Protects against:
  - Prompt injection (LLM inputs)
  - Webhook replay / spoofing (HMAC signature verification)
"""

import re
import hmac
import hashlib
import os
import logging
from shared.logging_utils import log_info, log_warning, log_error

logger = logging.getLogger(__name__)

# ── Prompt Injection Hardening ────────────────────────────────────────────────

_INJECTION_PATTERNS = re.compile(
    r"(ignore\s+(previous|all|above|prior)\s+(instructions?|prompts?|rules?)|"
    r"you\s+are\s+now|forget\s+everything|act\s+as|roleplay|jailbreak|"
    r"system\s*prompt|<\s*system\s*>|<\s*/?\s*inst\s*>|\[INST\]|\[\/INST\])",
    flags=re.IGNORECASE,
)


def sanitize_llm_input(text: str, max_length: int = 200, field_name: str = "input") -> str:
    """
    Sanitize a string before embedding it in an LLM prompt.

    Steps:
      1. Strip control characters (null bytes, escape sequences, etc.)
      2. Remove curly braces (template injection via f-string format)
      3. Collapse whitespace
      4. Detect and warn on known injection patterns
      5. Truncate to max_length

    Args:
        text:       Raw user-supplied string.
        max_length: Hard character limit after sanitization.
        field_name: Label used in warning logs for traceability.

    Returns:
        Sanitized string safe for LLM prompt interpolation.
    """
    if not text:
        return ""

    # 1. Remove control characters (0x00–0x1F, 0x7F–0x9F)
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text)

    # 2. Remove curly braces — prevent f-string / template injection
    text = text.replace("{", "").replace("}", "")

    # 3. Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # 4. Log (but don't block) suspected injection attempts
    if _INJECTION_PATTERNS.search(text):
        logger.warning(
            f"prompt_injection_attempt field={field_name} "
            f"snippet={text[:80]!r}"
        )

    # 5. Hard truncation
    return text[:max_length]


# ── Webhook HMAC Signature Verification ──────────────────────────────────────

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")


def verify_webhook_signature(payload: bytes, signature_header: str | None) -> bool:
    """
    Verify an HMAC-SHA256 webhook signature.

    Sender must include header:
        X-Signature: sha256=<hex_digest>

    Args:
        payload:          Raw request body bytes.
        signature_header: Value of the X-Signature header.

    Returns:
        True if signature is valid, False otherwise.
    """
    if not WEBHOOK_SECRET:
        logger.error("webhook_secret_missing — all webhooks will be REJECTED")
        return False

    if not signature_header:
        logger.warning("webhook_missing_signature_header")
        return False

    # Accept both "sha256=<hash>" and bare "<hash>" formats
    expected_hash = signature_header.removeprefix("sha256=")

    computed = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    valid = hmac.compare_digest(computed, expected_hash)
    if not valid:
        logger.warning(
            f"webhook_invalid_signature "
            f"received={expected_hash[:12]}... computed={computed[:12]}..."
        )
    return valid


def verify_slack_signature(
    payload: bytes,
    timestamp: str | None,
    signature_header: str | None
) -> bool:
    """
    Verify Slack webhook signature using official v0 scheme.
    
    Slack sends:
        X-Slack-Request-Timestamp: 1531420618
        X-Slack-Signature: v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503
    
    Base string format: v0:{timestamp}:{body}
    HMAC-SHA256 with SLACK_SIGNING_SECRET
    Anti-replay: reject if timestamp > 5 minutes old
    
    Args:
        payload:          Raw request body bytes
        timestamp:        X-Slack-Request-Timestamp header value
        signature_header: X-Slack-Signature header value
    
    Returns:
        True if signature is valid and fresh, False otherwise
        
    Reference: https://api.slack.com/authentication/verifying-requests-from-slack
    """
    if not SLACK_SIGNING_SECRET:
        logger.error("slack_signing_secret_missing — all Slack webhooks will be REJECTED")
        return False
    
    if not timestamp or not signature_header:
        logger.warning("slack_webhook_missing_headers")
        return False
    
    # Anti-replay: reject requests older than 5 minutes
    try:
        import time
        request_time = int(timestamp)
        current_time = int(time.time())
        if abs(current_time - request_time) > 300:  # 5 minutes
            logger.warning(f"slack_webhook_replay_attack timestamp={timestamp} age={current_time - request_time}s")
            return False
    except (ValueError, TypeError):
        logger.warning(f"slack_webhook_invalid_timestamp value={timestamp}")
        return False
    
    # Build base string: v0:timestamp:body
    base_string = f"v0:{timestamp}:{payload.decode('utf-8')}"
    
    # Compute HMAC-SHA256
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    
    # Timing-safe comparison
    valid = hmac.compare_digest(computed, signature_header)
    
    if not valid:
        logger.warning(
            f"slack_webhook_invalid_signature "
            f"received={signature_header[:20]}... computed={computed[:20]}..."
        )
    
    return valid
