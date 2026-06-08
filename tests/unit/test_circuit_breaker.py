"""
tests/unit/test_circuit_breaker.py — Circuit Breaker Unit Tests V5.0

Tests for the Circuit Breaker reliability pattern.
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from shared.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    CircuitBreakerOpenError
)


class TestCircuitBreaker:
    """Unit tests for CircuitBreaker."""
    
    @pytest.fixture
    def cb(self):
        """Create CircuitBreaker with low thresholds for testing."""
        return CircuitBreaker(
            failure_threshold=3,
            timeout_seconds=1,
            half_open_timeout=1,
            name="test_circuit"
        )
    
    @pytest.mark.asyncio
    async def test_closed_state_success(self, cb):
        """Test successful calls in CLOSED state."""
        async def success_fn():
            return "success"
        
        result = await cb.call(success_fn)
        
        assert result == "success"
        assert cb.state == CircuitState.CLOSED
        assert cb.failures == 0
        assert cb.successes == 1
    
    @pytest.mark.asyncio
    async def test_closed_to_open_on_failures(self, cb):
        """Test transition from CLOSED to OPEN after threshold failures."""
        async def fail_fn():
            raise Exception("API failure")
        
        # Generate failures up to threshold
        for i in range(cb.failure_threshold):
            with pytest.raises(Exception):
                await cb.call(fail_fn)
        
        # Circuit should now be OPEN
        assert cb.state == CircuitState.OPEN
        assert cb.failures == cb.failure_threshold
    
    @pytest.mark.asyncio
    async def test_open_state_rejects_immediately(self, cb):
        """Test OPEN state rejects requests immediately."""
        async def fail_fn():
            raise Exception("API failure")
        
        # Trip circuit to OPEN
        for _ in range(cb.failure_threshold):
            with pytest.raises(Exception):
                await cb.call(fail_fn)
        
        assert cb.state == CircuitState.OPEN
        
        # Next call should be rejected immediately
        async def any_fn():
            return "should not be called"
        
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            await cb.call(any_fn)
        
        assert "is OPEN" in str(exc_info.value)
        assert cb.total_rejections == 1
    
    @pytest.mark.asyncio
    async def test_open_to_half_open_after_timeout(self, cb):
        """Test transition from OPEN to HALF_OPEN after timeout."""
        async def fail_fn():
            raise Exception("API failure")
        
        # Trip circuit
        for _ in range(cb.failure_threshold):
            with pytest.raises(Exception):
                await cb.call(fail_fn)
        
        assert cb.state == CircuitState.OPEN
        
        # Wait for timeout
        await asyncio.sleep(cb.timeout.seconds + 0.1)
        
        # Next call should transition to HALF_OPEN
        async def success_fn():
            return "recovery"
        
        result = await cb.call(success_fn)
        
        assert result == "recovery"
        assert cb.state == CircuitState.CLOSED  # Success in HALF_OPEN closes circuit
    
    @pytest.mark.asyncio
    async def test_half_open_success_closes_circuit(self, cb):
        """Test successful call in HALF_OPEN closes circuit."""
        # Trip circuit
        async def fail_fn():
            raise Exception("fail")
        
        for _ in range(cb.failure_threshold):
            with pytest.raises(Exception):
                await cb.call(fail_fn)
        
        # Wait and test recovery
        await asyncio.sleep(cb.timeout.seconds + 0.1)
        
        async def success_fn():
            return "recovered"
        
        result = await cb.call(success_fn)
        
        assert cb.state == CircuitState.CLOSED
        assert cb.failures == 0
    
    @pytest.mark.asyncio
    async def test_half_open_failure_reopens_circuit(self, cb):
        """Test failure in HALF_OPEN re-opens circuit."""
        # Trip circuit
        async def fail_fn():
            raise Exception("still failing")
        
        for _ in range(cb.failure_threshold):
            with pytest.raises(Exception):
                await cb.call(fail_fn)
        
        # Wait for timeout
        await asyncio.sleep(cb.timeout.seconds + 0.1)
        
        # Fail in HALF_OPEN
        with pytest.raises(Exception):
            await cb.call(fail_fn)
        
        # Should re-open
        assert cb.state == CircuitState.OPEN
    
    @pytest.mark.asyncio
    async def test_statistics_tracking(self, cb):
        """Test statistics are tracked correctly."""
        async def success_fn():
            return "ok"
        
        async def fail_fn():
            raise Exception("error")
        
        # 2 successes
        await cb.call(success_fn)
        await cb.call(success_fn)
        
        # 3 failures (trips circuit)
        for _ in range(3):
            with pytest.raises(Exception):
                await cb.call(fail_fn)
        
        # 1 rejection
        with pytest.raises(CircuitBreakerOpenError):
            await cb.call(success_fn)
        
        stats = cb.get_stats()
        
        assert stats["total_successes"] == 2
        assert stats["total_failures"] == 3
        assert stats["total_rejections"] == 1
        assert stats["total_calls"] == 6
    
    @pytest.mark.asyncio
    async def test_manual_reset(self, cb):
        """Test manual reset to CLOSED state."""
        async def fail_fn():
            raise Exception("fail")
        
        # Trip circuit
        for _ in range(cb.failure_threshold):
            with pytest.raises(Exception):
                await cb.call(fail_fn)
        
        assert cb.state == CircuitState.OPEN
        
        # Manual reset
        cb.reset()
        
        assert cb.state == CircuitState.CLOSED
        assert cb.failures == 0
        assert cb.successes == 0
    
    @pytest.mark.asyncio
    async def test_get_stats_structure(self, cb):
        """Test get_stats returns correct structure."""
        stats = cb.get_stats()
        
        assert "name" in stats
        assert "state" in stats
        assert "total_calls" in stats
        assert "total_successes" in stats
        assert "total_failures" in stats
        assert "total_rejections" in stats
        assert "current_failures" in stats
        assert "failure_threshold" in stats
        
        assert stats["name"] == "test_circuit"
        assert stats["state"] == "closed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
