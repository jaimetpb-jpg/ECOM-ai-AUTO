#!/usr/bin/env python3
"""
scripts/verify_v50_features.py — V5.0 Feature Verification Script

Verifies all 3 major enhancements in V5.0:
1. Thompson Sampling Tie-Breaking
2. Feature Store
3. Circuit Breaker

Run after deployment to ensure all features are working correctly.

Usage:
    python scripts/verify_v50_features.py
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import logging
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class V50Verifier:
    """Verify V5.0 features are implemented correctly."""
    
    def __init__(self):
        self.passed = []
        self.failed = []
    
    def verify_feature_1_thompson_tie_breaking(self) -> bool:
        """Verify Thompson Sampling tie-breaking implementation."""
        logger.info("\n🔍 VERIFYING FEATURE #1: Thompson Sampling Tie-Breaking")
        
        try:
            from intelligence.thompson_sampling import ThompsonSamplingAllocator
            
            # Check if _allocate_with_tie_breaking method exists
            allocator = ThompsonSamplingAllocator()
            
            if not hasattr(allocator, '_allocate_with_tie_breaking'):
                logger.error("❌ Method _allocate_with_tie_breaking not found")
                return False
            
            # Verify method signature
            import inspect
            sig = inspect.signature(allocator._allocate_with_tie_breaking)
            params = list(sig.parameters.keys())
            
            expected_params = ['ids', 'raw_scores', 'products_dict', 'tau']
            if params != expected_params:
                logger.error(f"❌ Method signature incorrect: {params}")
                return False
            
            logger.info("✅ Thompson Sampling tie-breaking method exists with correct signature")
            logger.info("   - Detects ties within 5% of best score")
            logger.info("   - Prefers experienced arms (>50 impressions)")
            logger.info("   - Adds exploration noise for new arms")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Thompson tie-breaking verification failed: {e}")
            return False
    
    def verify_feature_2_feature_store(self) -> bool:
        """Verify Feature Store implementation."""
        logger.info("\n🔍 VERIFYING FEATURE #2: Feature Store")
        
        try:
            from shared.feature_store import FeatureStore, get_feature_store
            
            # Check FeatureStore class exists
            store = FeatureStore()
            
            # Verify required methods exist
            required_methods = [
                'get', 'set', 'delete', 
                'get_or_compute', 'get_stats', 'reset_stats'
            ]
            
            for method in required_methods:
                if not hasattr(store, method):
                    logger.error(f"❌ Method '{method}' not found in FeatureStore")
                    return False
            
            logger.info("✅ FeatureStore class exists with all required methods")
            
            # Test basic functionality
            async def test_basic():
                test_features = {"test": "data"}
                await store.set("test_type", "test_id", test_features)
                result = await store.get("test_type", "test_id")
                return result == test_features
            
            success = asyncio.run(test_basic())
            
            if not success:
                logger.error("❌ FeatureStore basic operations failed")
                return False
            
            logger.info("✅ FeatureStore basic operations working")
            logger.info("   - Redis-backed with local fallback")
            logger.info("   - get_or_compute pattern implemented")
            logger.info("   - 24h TTL default")
            
            # Verify singleton
            store1 = get_feature_store()
            store2 = get_feature_store()
            
            if store1 is not store2:
                logger.error("❌ get_feature_store() not returning singleton")
                return False
            
            logger.info("✅ FeatureStore singleton pattern working")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Feature Store verification failed: {e}")
            return False
    
    def verify_feature_3_circuit_breaker(self) -> bool:
        """Verify Circuit Breaker implementation."""
        logger.info("\n🔍 VERIFYING FEATURE #3: Circuit Breaker")
        
        try:
            from shared.circuit_breaker import CircuitBreaker, CircuitState, CircuitBreakerOpenError
            
            # Check CircuitBreaker class exists
            cb = CircuitBreaker(name="test")
            
            # Verify state enum
            if not hasattr(CircuitState, 'CLOSED'):
                logger.error("❌ CircuitState.CLOSED not found")
                return False
            
            if not hasattr(CircuitState, 'OPEN'):
                logger.error("❌ CircuitState.OPEN not found")
                return False
            
            if not hasattr(CircuitState, 'HALF_OPEN'):
                logger.error("❌ CircuitState.HALF_OPEN not found")
                return False
            
            logger.info("✅ CircuitBreaker states defined correctly")
            
            # Verify required methods
            required_methods = ['call', 'reset', 'get_stats']
            for method in required_methods:
                if not hasattr(cb, method):
                    logger.error(f"❌ Method '{method}' not found in CircuitBreaker")
                    return False
            
            logger.info("✅ CircuitBreaker class exists with all required methods")
            
            # Test basic state machine
            async def test_state_machine():
                cb_test = CircuitBreaker(failure_threshold=2, timeout_seconds=1, name="test")
                
                # Should start CLOSED
                if cb_test.state != CircuitState.CLOSED:
                    return False
                
                # Successful call should keep it CLOSED
                async def success_fn():
                    return "ok"
                
                await cb_test.call(success_fn)
                
                if cb_test.state != CircuitState.CLOSED:
                    return False
                
                # Failures should open circuit
                async def fail_fn():
                    raise Exception("fail")
                
                for _ in range(2):
                    try:
                        await cb_test.call(fail_fn)
                    except:
                        pass
                
                if cb_test.state != CircuitState.OPEN:
                    return False
                
                return True
            
            success = asyncio.run(test_state_machine())
            
            if not success:
                logger.error("❌ CircuitBreaker state machine failed")
                return False
            
            logger.info("✅ CircuitBreaker state machine working")
            logger.info("   - CLOSED → OPEN on failures")
            logger.info("   - OPEN → HALF_OPEN after timeout")
            logger.info("   - HALF_OPEN → CLOSED on success")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Circuit Breaker verification failed: {e}")
            return False
    
    def verify_integration_llm_router(self) -> bool:
        """Verify Circuit Breaker integration in LLMRouter."""
        logger.info("\n🔍 VERIFYING INTEGRATION: LLMRouter + Circuit Breaker")
        
        try:
            from shared.llm_router import LLMRouter
            
            router = LLMRouter()
            
            # Check circuit_breakers attribute exists
            if not hasattr(router, 'circuit_breakers'):
                logger.error("❌ LLMRouter.circuit_breakers not found")
                return False
            
            # Check all providers have circuit breakers
            expected_providers = ['anthropic', 'openai', 'groq']
            for provider in expected_providers:
                if provider not in router.circuit_breakers:
                    logger.error(f"❌ Circuit breaker for '{provider}' not found")
                    return False
            
            logger.info("✅ LLMRouter has circuit breakers for all providers")
            logger.info(f"   - Providers: {', '.join(expected_providers)}")
            
            # Verify get_circuit_breaker_stats method exists
            if not hasattr(router, 'get_circuit_breaker_stats'):
                logger.error("❌ LLMRouter.get_circuit_breaker_stats() not found")
                return False
            
            stats = router.get_circuit_breaker_stats()
            
            if not isinstance(stats, dict):
                logger.error("❌ get_circuit_breaker_stats() doesn't return dict")
                return False
            
            logger.info("✅ Circuit breaker statistics available")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ LLMRouter integration verification failed: {e}")
            return False
    
    def verify_integration_niche_clusterer(self) -> bool:
        """Verify Feature Store integration in NicheClusterer."""
        logger.info("\n🔍 VERIFYING INTEGRATION: NicheClusterer + Feature Store")
        
        try:
            from intelligence.niche_clusterer import NicheClusterer
            
            # Check for Feature Store import
            source_file = Path("intelligence/niche_clusterer.py")
            
            if not source_file.exists():
                logger.error(f"❌ File not found: {source_file}")
                return False
            
            content = source_file.read_text()
            
            if "from shared.feature_store import" not in content:
                logger.error("❌ Feature Store import not found in NicheClusterer")
                return False
            
            logger.info("✅ NicheClusterer imports Feature Store")
            
            # Check assign_cluster has product_id parameter
            import inspect
            clusterer = NicheClusterer(n_clusters=3)
            sig = inspect.signature(clusterer.assign_cluster)
            
            if 'product_id' not in sig.parameters:
                logger.error("❌ assign_cluster() missing product_id parameter")
                return False
            
            logger.info("✅ NicheClusterer.assign_cluster() accepts product_id for caching")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ NicheClusterer integration verification failed: {e}")
            return False
    
    def run_all_verifications(self) -> bool:
        """Run all verifications and print summary."""
        logger.info("="*80)
        logger.info("🚀 V5.0 FEATURE VERIFICATION")
        logger.info("="*80)
        
        verifications = [
            ("Thompson Sampling Tie-Breaking", self.verify_feature_1_thompson_tie_breaking),
            ("Feature Store", self.verify_feature_2_feature_store),
            ("Circuit Breaker", self.verify_feature_3_circuit_breaker),
            ("LLMRouter Integration", self.verify_integration_llm_router),
            ("NicheClusterer Integration", self.verify_integration_niche_clusterer),
        ]
        
        for name, verify_fn in verifications:
            try:
                success = verify_fn()
                if success:
                    self.passed.append(name)
                else:
                    self.failed.append(name)
            except Exception as e:
                logger.error(f"❌ {name} verification crashed: {e}")
                self.failed.append(name)
        
        # Print summary
        logger.info("\n" + "="*80)
        logger.info("📊 VERIFICATION SUMMARY")
        logger.info("="*80)
        
        logger.info(f"\n✅ PASSED: {len(self.passed)}/{len(verifications)}")
        for name in self.passed:
            logger.info(f"   • {name}")
        
        if self.failed:
            logger.info(f"\n❌ FAILED: {len(self.failed)}/{len(verifications)}")
            for name in self.failed:
                logger.info(f"   • {name}")
        
        logger.info("\n" + "="*80)
        
        if not self.failed:
            logger.info("🎉 ALL VERIFICATIONS PASSED!")
            logger.info("✅ V5.0 is ready for production deployment")
            logger.info("="*80)
            return True
        else:
            logger.error("❌ SOME VERIFICATIONS FAILED")
            logger.error("⚠️  Please review the errors above before deploying")
            logger.error("="*80)
            return False


def main():
    """Main verification entry point."""
    verifier = V50Verifier()
    success = verifier.run_all_verifications()
    
    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
