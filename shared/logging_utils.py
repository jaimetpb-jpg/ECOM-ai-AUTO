"""
shared/logging_utils.py — Structured Logging Utilities

Provides helpers for consistent, structured logging across the application.
Fixes the common anti-pattern of passing kwargs to logger methods that ignore them.
"""

import logging
import json
from typing import Any, Dict


def log_structured(logger: logging.Logger, level: str, event: str, **context: Any) -> None:
    """
    Log a structured event with context.
    
    Args:
        logger: Logger instance
        level: Log level (info, warning, error, debug)
        event: Event identifier (snake_case)
        **context: Additional context as key-value pairs
    
    Example:
        log_structured(logger, "warning", "insufficient_reviews", product="test", count=5)
        # Logs: "WARNING: insufficient_reviews | product=test count=5"
    """
    context_str = " ".join(f"{k}={v}" for k, v in context.items()) if context else ""
    message = f"{event} | {context_str}" if context_str else event
    
    log_method = getattr(logger, level.lower(), logger.info)
    log_method(message)


def log_info(logger: logging.Logger, event: str, **context: Any) -> None:
    """Convenience method for INFO level."""
    log_structured(logger, "info", event, **context)


def log_warning(logger: logging.Logger, event: str, **context: Any) -> None:
    """Convenience method for WARNING level."""
    log_structured(logger, "warning", event, **context)


def log_error(logger: logging.Logger, event: str, **context: Any) -> None:
    """Convenience method for ERROR level."""
    log_structured(logger, "error", event, **context)


def log_debug(logger: logging.Logger, event: str, **context: Any) -> None:
    """Convenience method for DEBUG level."""
    log_structured(logger, "debug", event, **context)
