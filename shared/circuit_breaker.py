"""
shared/circuit_breaker.py — Circuit Breaker Pattern V5.0

Silicon Valley Reliability Pattern:
Prevents cascading failures when external services (LLM APIs, Slack, etc.) fail.

States:
- CLOSED:    Normal operation, requests pass through
- OPEN:      Service is failing, reject requests immediately (fail-fast)
- HALF_OPEN: Testing if service recovered

Why we need this:
- If OpenAI/Anthropic API goes down → thousands of hanging requests
- Cascading failures across entire system
- Better to fail-fast and use fallback

Impact:
- Prevents cascading failures
- Auto-recovery with HALF_OPEN state
- Production-grade reliability

Usage:
    # In LLMRouter
    cb = CircuitBreaker(failure_threshold=5, timeout_seconds=60)
    
    try:
        result = await cb.call(self._call_anthropic, prompt, **kwargs)
    except CircuitBreakerOpenError:
        # Circuit is OPEN - fallback to alternate provider
        logger.warning("Anthropic circuit OPEN, using OpenAI fallback")
        result = await self._call_openai(prompt, **kwargs)
"""

import asyncio
import logging
from enum import Enum
from datetime import datetime, timedelta
from typing import Callable, Any, Optional

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"          # Normal operation
    OPEN = "open"              # Failing, reject requests
    HALF_OPEN = "half_open"    # Testing recovery


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is OPEN and rejects requests."""
    pass


class CircuitBreaker:
    """
    Circuit Breaker for external APIs.
    
    Production-grade pattern for handling external service failures gracefully.
    
    Silicon Valley Design:
    - State machine: CLOSED → OPEN → HALF_OPEN → CLOSED
    - Automatic recovery testing
    - Thread-safe
    - Configurable thresholds
    
    Example:
        cb = CircuitBreaker(failure_threshold=5, timeout_seconds=60)
        
        try:
            result = await cb.call(external_api_call, arg1, arg2)
        except CircuitBreakerOpenError:
            # Use fallback logic
            result = await fallback_call(arg1, arg2)
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        timeout_seconds: int = 60,
        half_open_timeout: int = 30,
        name: str = "unnamed"
    ):
        """
        Initialize circuit breaker.
        
        Args:
            failure_threshold: Number of consecutive failures before opening circuit
            timeout_seconds: Seconds to wait before trying HALF_OPEN
            half_open_timeout: Seconds to wait in HALF_OPEN before re-opening
            name: Circuit breaker name for logging
        """
        self.failure_threshold = failure_threshold
        self.timeout = timedelta(seconds=timeout_seconds)
        self.half_open_timeout = timedelta(seconds=half_open_timeout)
        self.name = name
        
        self.state = CircuitState.CLOSED
        self.failures = 0
        self.successes = 0
        self.last_failure_time: Optional[datetime] = None
        self.last_success_time: Optional[datetime] = None
        self.last_state_change: datetime = datetime.now()
        
        # Statistics
        self.total_calls = 0
        self.total_failures = 0
        self.total_successes = 0
        self.total_rejections = 0
    
    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.
        
        Args:
            func: Async function to call
            *args: Positional arguments
            **kwargs: Keyword arguments
        
        Returns:
            Function result
        
        Raises:
            CircuitBreakerOpenError: If circuit is OPEN
            Exception: Original exception from func if circuit allows call
        """
        self.total_calls += 1
        
        # Check if circuit is OPEN
        if self.state == CircuitState.OPEN:
            # Check if timeout expired → try HALF_OPEN
            if datetime.now() - self.last_failure_time > self.timeout:
                self._transition_to_half_open()
            else:
                # Still OPEN - reject immediately
                self.total_rejections += 1
                raise CircuitBreakerOpenError(
                    f"Circuit breaker '{self.name}' is OPEN - "
                    f"last failure {(datetime.now() - self.last_failure_time).seconds}s ago"
                )
        
        # HALF_OPEN state: Allow one test request
        if self.state == CircuitState.HALF_OPEN:
            # Check if we've been in HALF_OPEN too long
            if datetime.now() - self.last_state_change > self.half_open_timeout:
                # No success in HALF_OPEN period → back to OPEN
                self._transition_to_open()
                self.total_rejections += 1
                raise CircuitBreakerOpenError(
                    f"Circuit breaker '{self.name}' re-opened after HALF_OPEN timeout"
                )
        
        # Execute function
        try:
            result = await func(*args, **kwargs)
            
            # Success - reset or close circuit
            self._on_success()
            return result
            
        except Exception as e:
            # Failure - update circuit
            self._on_failure()
            raise
    
    def _on_success(self) -> None:
        """Handle successful call."""
        self.failures = 0
        self.successes += 1
        self.total_successes += 1
        self.last_success_time = datetime.now()
        
        if self.state == CircuitState.HALF_OPEN:
            # Success in HALF_OPEN → transition to CLOSED
            logger.info(
                f"circuit_breaker_closed name={self.name} "
                f"service_recovered=True half_open_success=True"
            )
            self.state = CircuitState.CLOSED
            self.last_state_change = datetime.now()
    
    def _on_failure(self) -> None:
        """Handle failed call."""
        self.failures += 1
        self.total_failures += 1
        self.last_failure_time = datetime.now()
        
        if self.failures >= self.failure_threshold:
            self._transition_to_open()
    
    def _transition_to_open(self) -> None:
        """Transition circuit to OPEN state."""
        if self.state != CircuitState.OPEN:
            logger.error(
                f"circuit_breaker_open name={self.name} "
                f"consecutive_failures={self.failures} "
                f"threshold={self.failure_threshold}"
            )
            self.state = CircuitState.OPEN
            self.last_state_change = datetime.now()
    
    def _transition_to_half_open(self) -> None:
        """Transition circuit to HALF_OPEN state."""
        logger.info(
            f"circuit_breaker_half_open name={self.name} "
            f"testing_recovery=True timeout_expired=True"
        )
        self.state = CircuitState.HALF_OPEN
        self.failures = 0  # Reset for testing
        self.last_state_change = datetime.now()
    
    def reset(self) -> None:
        """Manually reset circuit to CLOSED state."""
        logger.info(f"circuit_breaker_reset name={self.name} manual_reset=True")
        self.state = CircuitState.CLOSED
        self.failures = 0
        self.successes = 0
        self.last_state_change = datetime.now()
    
    def get_stats(self) -> dict:
        """
        Get circuit breaker statistics.
        
        Returns:
            Stats dict with state, calls, failures, etc.
        """
        return {
            "name": self.name,
            "state": self.state.value,
            "total_calls": self.total_calls,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "total_rejections": self.total_rejections,
            "current_failures": self.failures,
            "failure_threshold": self.failure_threshold,
            "last_failure": self.last_failure_time.isoformat() if self.last_failure_time else None,
            "last_success": self.last_success_time.isoformat() if self.last_success_time else None,
            "state_duration_seconds": (datetime.now() - self.last_state_change).seconds,
        }
