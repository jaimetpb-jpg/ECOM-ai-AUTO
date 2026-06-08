"""
tests/unit/test_feature_store.py — Feature Store Unit Tests V5.0

Tests for the Feature Store caching system.
"""

import pytest
import asyncio
from shared.feature_store import FeatureStore, get_feature_store


class TestFeatureStore:
    """Unit tests for FeatureStore."""
    
    @pytest.fixture
    def store(self):
        """Create FeatureStore instance without Redis."""
        return FeatureStore(redis_client=None)  # Local cache only
    
    @pytest.mark.asyncio
    async def test_get_set_basic(self, store):
        """Test basic get/set operations."""
        # Set features
        features = {"price_band": 0.5, "roas": 3.2, "vector": [0.1, 0.2, 0.3]}
        result = await store.set("niche_vector", "product_123", features)
        assert result is True
        
        # Get features
        cached = await store.get("niche_vector", "product_123")
        assert cached == features
    
    @pytest.mark.asyncio
    async def test_get_missing_key(self, store):
        """Test get with non-existent key returns None."""
        result = await store.get("niche_vector", "nonexistent")
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_or_compute_cache_hit(self, store):
        """Test get_or_compute with cache hit."""
        # Pre-populate cache
        features = {"computed": True, "value": 42}
        await store.set("test_feature", "product_1", features)
        
        # Define compute function (should NOT be called)
        compute_called = False
        def compute_fn(**kwargs):
            nonlocal compute_called
            compute_called = True
            return {"computed": False, "value": 0}
        
        # Get or compute (should hit cache)
        result = await store.get_or_compute(
            "test_feature",
            "product_1",
            compute_fn
        )
        
        assert result == features
        assert compute_called is False  # Compute fn NOT called
        assert store.hits == 1
        assert store.misses == 0
    
    @pytest.mark.asyncio
    async def test_get_or_compute_cache_miss(self, store):
        """Test get_or_compute with cache miss."""
        # Define compute function (SHOULD be called)
        def compute_fn(price, roas):
            return {"price_band": price / 100, "roas_norm": roas / 5}
        
        # Get or compute (cache miss)
        result = await store.get_or_compute(
            "niche_vector",
            "product_2",
            compute_fn,
            price=50.0,
            roas=3.5
        )
        
        assert result == {"price_band": 0.5, "roas_norm": 0.7}
        assert store.hits == 0
        assert store.misses == 1
        
        # Verify it was cached
        cached = await store.get("niche_vector", "product_2")
        assert cached == result
    
    @pytest.mark.asyncio
    async def test_delete(self, store):
        """Test delete removes cached features."""
        # Set features
        features = {"data": "test"}
        await store.set("feature_type", "product_3", features)
        
        # Verify cached
        assert await store.get("feature_type", "product_3") == features
        
        # Delete
        await store.delete("feature_type", "product_3")
        
        # Verify removed
        assert await store.get("feature_type", "product_3") is None
    
    @pytest.mark.asyncio
    async def test_stats(self, store):
        """Test statistics tracking."""
        # Generate some hits and misses
        await store.set("f1", "p1", {"v": 1})
        await store.get("f1", "p1")  # Hit
        await store.get("f2", "p2")  # Miss
        await store.get("f1", "p1")  # Hit
        
        stats = store.get_stats()
        
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["total_requests"] == 3
        assert stats["hit_rate_pct"] == pytest.approx(66.67, rel=0.1)
    
    @pytest.mark.asyncio
    async def test_reset_stats(self, store):
        """Test resetting statistics."""
        await store.set("f1", "p1", {"v": 1})
        await store.get("f1", "p1")
        
        assert store.hits == 1
        
        store.reset_stats()
        
        assert store.hits == 0
        assert store.misses == 0
    
    def test_singleton(self):
        """Test get_feature_store returns singleton."""
        store1 = get_feature_store()
        store2 = get_feature_store()
        
        assert store1 is store2  # Same instance


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
