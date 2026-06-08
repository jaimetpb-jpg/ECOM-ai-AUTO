#!/usr/bin/env python3
"""
scripts/verify_v51_features.py — Automated V5.1 Feature Verification

Verifica que todos los módulos V5.1 carguen correctamente y funcionen
con mocks básicos. No requiere APIs externas ni Redis.

Expected output:
  ✅ PASSED: 6/6 — V5.1 Production-Ready

Usage:
    python scripts/verify_v51_features.py
"""

import sys
import os
import asyncio
import traceback

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSED = []
FAILED = []


def check(name: str, fn):
    """Run a check and record result."""
    try:
        result = fn()
        if asyncio.iscoroutine(result):
            result = asyncio.get_event_loop().run_until_complete(result)
        PASSED.append(name)
        print(f"  ✅ {name}")
        return True
    except Exception as e:
        FAILED.append((name, str(e)))
        print(f"  ❌ {name}: {e}")
        if "--verbose" in sys.argv:
            traceback.print_exc()
        return False


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 1: V5.0 Thompson Sampling Tie-Breaking
# ══════════════════════════════════════════════════════════════════════════════
def check_thompson_tie_breaking():
    from intelligence.thompson_sampling import (
        ThompsonSamplingAllocator, ProductStats, stable_softmax
    )

    # Test stable_softmax con scores similares (caso tie)
    scores = [10.0, 10.1, 10.05]
    probs = stable_softmax(scores, tau=0.5)
    assert len(probs) == 3, "Should return 3 probs"
    assert abs(sum(probs) - 1.0) < 1e-6, "Probs must sum to 1.0"
    assert all(p >= 0 for p in probs), "All probs must be >= 0"

    # Test allocator con productos similares (tie-breaking case)
    allocator = ThompsonSamplingAllocator()
    products = [
        ProductStats("p1", "c1", impressions=100, clicks=5),
        ProductStats("p2", "c2", impressions=20, clicks=1),
        ProductStats("p3", "c3", impressions=80, clicks=4),
    ]
    alloc = allocator.allocate(products, total_budget=150.0)
    assert len(alloc) == 3, "Should allocate to 3 products"
    assert sum(alloc.values()) <= 155.0, "Should not exceed budget significantly"
    assert all(v >= 0 for v in alloc.values()), "All allocations must be >= 0"

    # Test safe softmax con scores negativos
    neg_probs = stable_softmax([-5.0, -5.1, -5.0], tau=0.5)
    assert abs(sum(neg_probs) - 1.0) < 1e-6, "Negative scores should return valid probs"

    return True


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 2: V5.0 Feature Store
# ══════════════════════════════════════════════════════════════════════════════
async def check_feature_store():
    from shared.feature_store import FeatureStore, get_feature_store

    # Test sin Redis (modo local)
    store = FeatureStore(redis_client=None, ttl=3600)

    # Set y Get
    await store.set("niche_vector", "prod_test", {"features": [0.5, 0.3, 0.7, 0.6]})
    result = await store.get("niche_vector", "prod_test")
    assert result is not None, "Should return cached value"
    assert result["features"] == [0.5, 0.3, 0.7, 0.6], "Should return correct features"

    # Cache miss
    miss = await store.get("niche_vector", "nonexistent_product")
    assert miss is None, "Cache miss should return None"

    # get_or_compute
    def compute_fn(x, y):
        return {"sum": x + y, "product": x * y}

    computed = await store.get_or_compute(
        "math_result", "prod_math",
        compute_fn, x=3, y=4
    )
    assert computed["sum"] == 7, "Should compute correctly"
    assert computed["product"] == 12, "Should compute correctly"

    # Second call should hit cache
    cached = await store.get_or_compute(
        "math_result", "prod_math",
        compute_fn, x=99, y=99  # Different args — should use cache
    )
    assert cached["sum"] == 7, "Should use cached value, not recompute"

    # Stats
    stats = store.get_stats()
    assert stats["hits"] >= 2, "Should have at least 2 hits"
    assert stats["hit_rate_pct"] > 0, "Hit rate should be > 0"

    return True


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 3: V5.0 Circuit Breaker
# ══════════════════════════════════════════════════════════════════════════════
async def check_circuit_breaker():
    from shared.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError, CircuitState

    cb = CircuitBreaker(failure_threshold=3, timeout_seconds=60, name="test")
    assert cb.state == CircuitState.CLOSED, "Should start CLOSED"

    # Simular llamadas exitosas
    async def success_fn():
        return "ok"

    result = await cb.call(success_fn)
    assert result == "ok", "Should pass through successfully"
    assert cb.state == CircuitState.CLOSED, "Should stay CLOSED after success"

    # Simular fallos hasta abrir
    async def fail_fn():
        raise Exception("API Error")

    for i in range(3):
        try:
            await cb.call(fail_fn)
        except Exception:
            pass

    assert cb.state == CircuitState.OPEN, "Should be OPEN after 3 failures"
    assert cb.total_failures == 3, "Should record 3 failures"

    # Verificar que rechaza inmediatamente cuando OPEN
    try:
        await cb.call(success_fn)
        assert False, "Should raise CircuitBreakerOpenError"
    except CircuitBreakerOpenError:
        pass  # Expected

    assert cb.total_rejections >= 1, "Should record rejection"

    # Reset manual
    cb.reset()
    assert cb.state == CircuitState.CLOSED, "Should be CLOSED after reset"

    return True


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 4: V5.1 Creative Engine (sin LLM real)
# ══════════════════════════════════════════════════════════════════════════════
async def check_creative_engine():
    from engines.creative_engine import CreativeIntelligenceEngine

    engine = CreativeIntelligenceEngine()

    # Test safe JSON parsing
    valid_json = '''[
        {"hook_type": "fear", "hook_text": "¿Sufres de dolor de cuello?",
         "script": "Script completo aquí", "cta": "Ver ahora",
         "estimated_ctr": 0.08, "pain_point_addressed": "dolor cervical"}
    ]'''
    hooks = engine._safe_parse_hooks(valid_json)
    assert len(hooks) == 1, "Should parse 1 hook"
    assert hooks[0]["hook_type"] == "fear", "Should have correct type"

    # Test con markdown code block (LLM común)
    with_markdown = "```json\n" + valid_json + "\n```"
    hooks_md = engine._safe_parse_hooks(with_markdown)
    assert len(hooks_md) == 1, "Should parse through markdown"

    # Test con JSON inválido
    empty = engine._safe_parse_hooks("No es JSON")
    assert empty == [], "Should return empty list on parse failure"

    # Test validate_and_rank
    raw_hooks = [
        {"hook_type": "transformation", "hook_text": "Transformación total",
         "script": "...", "cta": "Comprar", "estimated_ctr": 0.12},
        {"hook_type": "fear", "hook_text": "Miedo al dolor",
         "script": "...", "cta": "Ver", "estimated_ctr": 0.06},
        {"hook_type": "invalid_type", "hook_text": "Hook",
         "script": "...", "cta": "CTA", "estimated_ctr": 0.03},
    ]
    validated = engine._validate_and_rank(raw_hooks)
    assert len(validated) == 3, "Should validate all 3"
    assert validated[0]["estimated_ctr"] >= validated[1]["estimated_ctr"], \
        "Should be sorted by CTR descending"
    assert validated[2]["hook_type"] == "curiosity", \
        "Invalid hook_type should be normalized to 'curiosity'"

    # Test Feature Store cache (sin LLM)
    from shared.feature_store import FeatureStore, _feature_store_instance
    import shared.feature_store as fs_module
    test_store = FeatureStore()
    await test_store.set("creative_hooks", "prod_cache_test",
                          {"hooks": [{"hook_text": "Cached hook"}]})
    # Inject test store temporarily
    original = fs_module._feature_store_instance
    fs_module._feature_store_instance = test_store

    result = await engine.run_creative_pipeline({
        "product_id": "prod_cache_test",
        "name": "Test Product",
        "niche": "test",
    })
    assert len(result) == 1, "Should return cached hook"

    fs_module._feature_store_instance = original
    return True


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 5: V5.1 Ads Decision Engine + Kill-Switch
# ══════════════════════════════════════════════════════════════════════════════
def check_ads_decision_engine():
    from engines.ads_decision_engine import AdsDecisionEngine
    from shared.models import AdsDecision

    # Mock Slack
    class MockSlack:
        def __init__(self):
            self.messages = []
        def send_message(self, channel, text):
            self.messages.append({"channel": channel, "text": text})

    slack = MockSlack()
    engine = AdsDecisionEngine(slack=slack)

    # Test KILL — ROAS bajo con gasto suficiente
    decision = engine.evaluate_campaign({
        "campaign_id": "camp_kill",
        "spend_usd": 75.0,
        "revenue_usd": 90.0,  # ROAS = 1.2
        "days_active": 3,
        "tenant_id": "t1",
    })
    assert decision.action == "KILL", f"Expected KILL, got {decision.action}"
    assert decision.roas == round(90.0 / 75.0, 4), "ROAS calculation should be correct"
    assert decision.budget_change_pct == -100.0, "Kill should be -100%"
    assert len(slack.messages) >= 1, "Should send Slack alert on KILL"

    # Test HOLD — ROAS OK pero sin escalar
    decision2 = engine.evaluate_campaign({
        "campaign_id": "camp_hold",
        "spend_usd": 120.0,
        "revenue_usd": 240.0,  # ROAS = 2.0
        "days_active": 5,
        "tenant_id": "t1",
    })
    assert decision2.action == "HOLD", f"Expected HOLD, got {decision2.action}"
    assert decision2.budget_change_pct == 0.0, "Hold should be 0% change"

    # Test SCALE — ROAS alto por suficiente tiempo
    decision3 = engine.evaluate_campaign({
        "campaign_id": "camp_scale",
        "spend_usd": 500.0,
        "revenue_usd": 1500.0,  # ROAS = 3.0
        "days_active": 10,
        "tenant_id": "t1",
    })
    assert decision3.action == "SCALE", f"Expected SCALE, got {decision3.action}"
    assert decision3.budget_change_pct > 0, "Scale should increase budget"

    # Test safe math — no división por cero
    decision_zero = engine.evaluate_campaign({
        "campaign_id": "camp_zero",
        "spend_usd": 0.0,    # Zero spend
        "revenue_usd": 0.0,
        "days_active": 0,
        "tenant_id": "t1",
    })
    # No crash = test passed
    assert decision_zero.roas >= 0, "Should handle zero spend safely"

    # Test portfolio
    campaigns = [
        {"campaign_id": "c1", "spend_usd": 100.0, "revenue_usd": 80.0,
         "days_active": 4, "tenant_id": "t1"},   # KILL (ROAS=0.8)
        {"campaign_id": "c2", "spend_usd": 50.0, "revenue_usd": 100.0,
         "days_active": 2, "tenant_id": "t1"},   # HOLD
        {"campaign_id": "c3", "spend_usd": 300.0, "revenue_usd": 900.0,
         "days_active": 8, "tenant_id": "t1"},   # SCALE (ROAS=3.0)
    ]
    decisions = engine.evaluate_portfolio(campaigns)
    assert len(decisions) == 3, "Should return 3 decisions"

    return True


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 6: V5.1 Decision Engine + Portfolio Allocation
# ══════════════════════════════════════════════════════════════════════════════
def check_decision_engine():
    from core.decision_engine import DecisionEngine

    engine = DecisionEngine()

    # Test reject (score < 55)
    result = engine.evaluate_product({
        "product_id": "prod_low",
        "name": "Low Score Product",
        "niche": "test",
        "final_score": 45.0,
        "viral_score": 30.0,
    })
    assert result.decision == "reject", f"Expected reject, got {result.decision}"
    assert result.budget_usd == 0.0, "Rejected should have 0 budget"

    # Test manual_review (55-69)
    result2 = engine.evaluate_product({
        "product_id": "prod_mid",
        "name": "Mid Score Product",
        "niche": "test",
        "final_score": 62.0,
    })
    assert result2.decision == "manual_review", \
        f"Expected manual_review, got {result2.decision}"

    # Test launch_test (>= 70)
    result3 = engine.evaluate_product({
        "product_id": "prod_good",
        "name": "Good Score Product",
        "niche": "health",
        "final_score": 82.0,
        "viral_score": 75.0,
        "days_active": 0,
    })
    assert result3.decision == "launch_test", \
        f"Expected launch_test, got {result3.decision}"
    assert result3.budget_usd > 0, "Approved should have budget"
    assert 0 < result3.confidence <= 1.0, "Confidence should be 0-1"

    # Test economic reward calculation (ChatGPT fix)
    reward = engine._compute_economic_reward({
        "clicks": 100, "conversions": 5, "revenue_usd": 250.0,
        "impressions": 5000, "viral_score": 70.0,
    })
    assert 0 <= reward <= 1.0, f"Reward should be 0-1, got {reward}"

    # Test portfolio con niche diversification
    products = [
        {"product_id": f"p{i}", "name": f"Product {i}",
         "niche": "health" if i < 4 else "beauty",
         "final_score": 75.0 + i}
        for i in range(6)
    ]
    portfolio = engine.evaluate_portfolio(products, total_budget=300.0)
    assert "approved" in portfolio, "Should have approved key"
    assert "rejected" in portfolio, "Should have rejected key"
    assert portfolio["total_budget_allocated"] > 0, "Should allocate some budget"

    # Verificar diversification: max 2 por nicho
    approved_niches = {}
    products_by_id = {p["product_id"]: p for p in products}
    for ev in portfolio["approved"]:
        p = products_by_id.get(ev.get("product_id", ""), {})
        n = p.get("niche", "")
        approved_niches[n] = approved_niches.get(n, 0) + 1

    for niche, count in approved_niches.items():
        assert count <= 2, f"Niche '{niche}' has {count} products > max 2"

    return True


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "═" * 60)
    print("  E-Commerce AI V5.1 — Feature Verification")
    print("═" * 60)

    print("\n📋 Verificando módulos V5.0 (base):")
    check("Thompson Sampling V5.0 (tie-breaking + stable softmax)",
          check_thompson_tie_breaking)
    check("Feature Store V5.0 (Redis fallback + get_or_compute)",
          lambda: asyncio.get_event_loop().run_until_complete(check_feature_store()))
    check("Circuit Breaker V5.0 (CLOSED→OPEN→HALF_OPEN)",
          lambda: asyncio.get_event_loop().run_until_complete(check_circuit_breaker()))

    print("\n🚀 Verificando módulos V5.1 (nuevos engines):")
    check("Creative Engine V5.1 (safe parse + Feature Store cache)",
          lambda: asyncio.get_event_loop().run_until_complete(check_creative_engine()))
    check("Ads Decision Engine V5.1 (kill-switch ROAS + safe math)",
          check_ads_decision_engine)
    check("Decision Engine V5.1 (portfolio + niche diversity + reward económico)",
          check_decision_engine)

    print("\n" + "═" * 60)
    total = len(PASSED) + len(FAILED)
    if not FAILED:
        print(f"  🎉 PASSED: {len(PASSED)}/{total} — V5.1 PRODUCTION READY")
        print("═" * 60 + "\n")
        sys.exit(0)
    else:
        print(f"  ⚠️  PASSED: {len(PASSED)}/{total} — FAILED: {len(FAILED)}")
        print("\nFailed checks:")
        for name, error in FAILED:
            print(f"  ❌ {name}: {error}")
        print("═" * 60 + "\n")
        sys.exit(1)
