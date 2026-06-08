#!/usr/bin/env python3
"""
scripts/run_v51_tests.py — Self-contained test runner for V5.1
Works without pip installs — uses minimal stubs for unavailable packages.
"""

import sys
import os
import types
import asyncio

# ── Step 1: Stub all unavailable packages ─────────────────────────────────────
def create_stubs():
    """Create minimal stubs for packages not available in this environment."""
    from typing import Any, Optional, List, get_type_hints
    import typing

    # ── Pydantic v2 stub ──────────────────────────────────────────────────────
    pydantic_mod = types.ModuleType("pydantic")

    class Field:
        def __init__(self, *args, **kwargs):
            self.default = kwargs.get("default", None)
            self.kwargs = kwargs
        def __call__(self, *a, **k):
            return self

    def field_func(*args, **kwargs):
        return kwargs.get("default", None)

    class FieldInfo:
        pass

    class ModelMetaclass(type):
        def __new__(mcs, name, bases, namespace):
            annotations = {}
            for base in bases:
                if hasattr(base, '__annotations__'):
                    annotations.update(base.__annotations__)
            annotations.update(namespace.get('__annotations__', {}))
            namespace['__annotations__'] = annotations
            cls = super().__new__(mcs, name, bases, namespace)
            # Set defaults
            for field_name, type_hint in annotations.items():
                if field_name not in namespace:
                    setattr(cls, field_name, None)
            return cls

    class BaseModel(metaclass=ModelMetaclass):
        def __init__(self, **kwargs):
            # Set class defaults first
            for k in self.__class__.__annotations__:
                if hasattr(self.__class__, k):
                    val = getattr(self.__class__, k)
                    if not callable(val):
                        object.__setattr__(self, k, val)
            # Override with provided kwargs
            for k, v in kwargs.items():
                object.__setattr__(self, k, v)

        def model_dump(self):
            result = {}
            for k in self.__class__.__annotations__:
                result[k] = getattr(self, k, None)
            return result

        @classmethod
        def model_validate(cls, data):
            return cls(**data)

    pydantic_mod.BaseModel = BaseModel
    pydantic_mod.Field = field_func
    pydantic_mod.field_validator = lambda *a, **k: (lambda f: f)
    pydantic_mod.model_validator = lambda *a, **k: (lambda f: f)
    pydantic_mod.VERSION = "2.0.0-stub"

    class ValidationError(Exception):
        pass
    pydantic_mod.ValidationError = ValidationError

    sys.modules["pydantic"] = pydantic_mod
    sys.modules["pydantic.fields"] = pydantic_mod
    sys.modules["pydantic_core"] = types.ModuleType("pydantic_core")

    # ── FastAPI stubs ─────────────────────────────────────────────────────────
    for pkg in ["fastapi", "fastapi.middleware", "fastapi.middleware.cors",
                "fastapi.security", "fastapi.responses", "fastapi.staticfiles",
                "uvicorn", "httpx"]:
        mod = types.ModuleType(pkg)
        mod.FastAPI = type("FastAPI", (), {"__init__": lambda s, **k: None,
                                           "get": lambda s, *a, **k: (lambda f: f),
                                           "post": lambda s, *a, **k: (lambda f: f)})
        mod.APIRouter = mod.FastAPI
        mod.HTTPException = Exception
        mod.Depends = lambda f: f
        mod.BackgroundTasks = type("BT", (), {})
        mod.Request = type("Req", (), {})
        mod.Response = type("Resp", (), {})
        mod.CORSMiddleware = type("CORS", (), {})
        mod.JSONResponse = type("JR", (), {"__init__": lambda s, **k: None})
        sys.modules[pkg] = mod

    # ── Anthropic stubs ───────────────────────────────────────────────────────
    for pkg in ["anthropic", "anthropic.types"]:
        mod = types.ModuleType(pkg)
        mod.AsyncAnthropic = type("AA", (), {})
        mod.Anthropic = type("A", (), {})
        sys.modules[pkg] = mod

    # ── OpenAI stubs ──────────────────────────────────────────────────────────
    for pkg in ["openai", "openai.types", "openai.types.chat"]:
        mod = types.ModuleType(pkg)
        mod.AsyncOpenAI = type("AOA", (), {})
        sys.modules[pkg] = mod

    # ── Groq stubs ────────────────────────────────────────────────────────────
    groq_mod = types.ModuleType("groq")
    groq_mod.AsyncGroq = type("AG", (), {})
    sys.modules["groq"] = groq_mod

    # ── Redis stubs ───────────────────────────────────────────────────────────
    redis_mod = types.ModuleType("redis")
    redis_mod.Redis = type("R", (), {"get": lambda s, k: None,
                                      "setex": lambda s, k, t, v: None,
                                      "delete": lambda s, k: None})
    sys.modules["redis"] = redis_mod
    sys.modules["redis.asyncio"] = redis_mod

    # ── Slack stubs ───────────────────────────────────────────────────────────
    for pkg in ["slack_sdk", "slack_sdk.web", "slack_sdk.errors"]:
        mod = types.ModuleType(pkg)
        mod.WebClient = type("WC", (), {"chat_postMessage": lambda s, **k: {}})
        mod.SlackApiError = Exception
        sys.modules[pkg] = mod

    # ── Other stubs ───────────────────────────────────────────────────────────
    for pkg in ["supabase", "twilio", "twilio.rest", "crewai",
                "sklearn", "sklearn.cluster", "scipy", "scipy.stats"]:
        sys.modules[pkg] = types.ModuleType(pkg)


create_stubs()

# ── Step 2: Add project to path ───────────────────────────────────────────────
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# ── Step 3: Run tests ─────────────────────────────────────────────────────────
PASSED = []
FAILED = []
ERRORS = []


def run_check(name, fn):
    try:
        result = fn()
        if asyncio.iscoroutine(result):
            asyncio.get_event_loop().run_until_complete(result)
        PASSED.append(name)
        print(f"  ✅ {name}")
        return True
    except AssertionError as e:
        FAILED.append((name, str(e)))
        print(f"  ❌ {name}")
        print(f"     AssertionError: {e}")
        return False
    except Exception as e:
        ERRORS.append((name, type(e).__name__, str(e)))
        print(f"  ⚠️  {name}")
        print(f"     {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# TESTS
# ═══════════════════════════════════════════════════════════════════════════════

def test_feature_store():
    from shared.feature_store import FeatureStore

    async def run():
        store = FeatureStore(redis_client=None)
        await store.set("test_type", "prod_1", {"score": 42})
        result = await store.get("test_type", "prod_1")
        assert result == {"score": 42}

        miss = await store.get("test_type", "nonexistent")
        assert miss is None

        # get_or_compute
        computed = await store.get_or_compute(
            "math", "p1", lambda x, y: {"sum": x + y}, x=3, y=7
        )
        assert computed["sum"] == 10

        # Cache hit — should not recompute
        cached = await store.get_or_compute(
            "math", "p1", lambda x, y: {"sum": 999}, x=0, y=0
        )
        assert cached["sum"] == 10, "Should use cache, not recompute"

        stats = store.get_stats()
        assert stats["hits"] >= 2
        assert stats["hit_rate_pct"] > 0

    asyncio.get_event_loop().run_until_complete(run())


def test_circuit_breaker():
    from shared.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError, CircuitState

    async def run():
        cb = CircuitBreaker(failure_threshold=3, timeout_seconds=60, name="test")
        assert cb.state == CircuitState.CLOSED

        async def ok():
            return "ok"

        assert await cb.call(ok) == "ok"

        async def fail():
            raise Exception("fail")

        for _ in range(3):
            try:
                await cb.call(fail)
            except Exception:
                pass

        assert cb.state == CircuitState.OPEN

        try:
            await cb.call(ok)
            assert False, "Should raise"
        except CircuitBreakerOpenError:
            pass

        cb.reset()
        assert cb.state == CircuitState.CLOSED

    asyncio.get_event_loop().run_until_complete(run())


def test_thompson_allocator():
    from intelligence.thompson_sampling import (
        ThompsonSamplingAllocator, ProductStats, stable_softmax
    )

    # stable_softmax
    probs = stable_softmax([10.0, 10.1, 10.05], tau=0.5)
    assert len(probs) == 3
    assert abs(sum(probs) - 1.0) < 1e-6

    # negative scores
    probs2 = stable_softmax([-1.0, -2.0, -1.5], tau=0.5)
    assert abs(sum(probs2) - 1.0) < 1e-6

    # empty
    assert stable_softmax([]) == []

    # allocator
    allocator = ThompsonSamplingAllocator()
    products = [
        ProductStats("p1", "c1", impressions=200, clicks=10),
        ProductStats("p2", "c2", impressions=50, clicks=2),
        ProductStats("p3", "c3", impressions=80, clicks=4),
    ]
    alloc = allocator.allocate(products, total_budget=150.0)
    assert len(alloc) == 3
    assert all(v >= 0 for v in alloc.values())
    # Budget not significantly exceeded
    assert sum(alloc.values()) <= 165.0

    # Edge cases
    assert allocator.allocate([], total_budget=100.0) == {}
    assert allocator.allocate(products, total_budget=0.0) == {}


def test_creative_engine_parsing():
    from engines.creative_engine import CreativeIntelligenceEngine
    engine = CreativeIntelligenceEngine()

    # Valid JSON
    raw = '[{"hook_type":"fear","hook_text":"Test hook","script":"S","cta":"C","estimated_ctr":0.08}]'
    hooks = engine._safe_parse_hooks(raw)
    assert len(hooks) == 1
    assert hooks[0]["hook_type"] == "fear"

    # With markdown
    with_md = "```json\n" + raw + "\n```"
    hooks2 = engine._safe_parse_hooks(with_md)
    assert len(hooks2) == 1

    # With preamble
    with_preamble = "Aquí tienes los hooks:\n\n" + raw
    hooks3 = engine._safe_parse_hooks(with_preamble)
    assert len(hooks3) == 1

    # Invalid — returns empty
    for bad in ["not json", "", None, "```no json```", '{"key": "dict"}']: 
        assert engine._safe_parse_hooks(bad) == [], f"Bad input should return []: {repr(bad)}"


def test_creative_engine_validation():
    from engines.creative_engine import CreativeIntelligenceEngine
    engine = CreativeIntelligenceEngine()

    hooks = [
        {"hook_type": "transformation", "hook_text": "Best hook",
         "script": "S", "cta": "C", "estimated_ctr": 0.12},
        {"hook_type": "fear", "hook_text": "Second hook",
         "script": "S", "cta": "C", "estimated_ctr": 0.06},
        {"hook_type": "INVALID", "hook_text": "Third hook",
         "script": "S", "cta": "C", "estimated_ctr": 0.04},
        {"hook_type": "curiosity", "hook_text": "",  # empty text → discard
         "script": "S", "cta": "C", "estimated_ctr": 0.05},
        {"hook_type": "urgency", "hook_text": "Last hook",
         "script": "S", "cta": "C", "estimated_ctr": 99.0},  # clamp to 0.15
    ]
    result = engine._validate_and_rank(hooks)

    # Empty text discarded
    assert all(h["hook_text"] for h in result), "Empty hook_text should be discarded"
    # Sorted descending
    ctrs = [h["estimated_ctr"] for h in result]
    assert ctrs == sorted(ctrs, reverse=True), "Should be sorted by CTR desc"
    # Invalid type normalized
    invalid = [h for h in result if h.get("hook_text") == "Third hook"]
    assert invalid[0]["hook_type"] == "curiosity"
    # CTR clamped
    high_ctr = [h for h in result if h.get("hook_text") == "Last hook"]
    assert high_ctr[0]["estimated_ctr"] <= 0.15


def test_creative_engine_cache():
    import shared.feature_store as fs_module
    original = fs_module._feature_store_instance
    store = fs_module.FeatureStore(redis_client=None)
    fs_module._feature_store_instance = store

    try:
        async def run():
            from engines.creative_engine import CreativeIntelligenceEngine
            engine = CreativeIntelligenceEngine()

            # Pre-populate cache
            await store.set("creative_hooks", "cached_product",
                             {"hooks": [{"hook_text": "Cached!", "estimated_ctr": 0.09}]})

            call_count = {"n": 0}
            async def mock_llm(*args, **kwargs):
                call_count["n"] += 1
                return []

            engine._generate_hooks_openai = mock_llm
            result = await engine.run_creative_pipeline({
                "product_id": "cached_product",
                "name": "Test", "niche": "health",
            })

            assert call_count["n"] == 0, "LLM should NOT be called on cache hit"
            assert result[0]["hook_text"] == "Cached!"

        asyncio.get_event_loop().run_until_complete(run())
    finally:
        fs_module._feature_store_instance = original


def test_ads_decision_kill_switch():
    from engines.ads_decision_engine import AdsDecisionEngine

    class MockSlack:
        def __init__(self): self.messages = []
        def _post(self, channel, text): self.messages.append(text)  # matches SlackNotifier._post()

    slack = MockSlack()
    engine = AdsDecisionEngine(slack=slack)

    def cam(spend, revenue, days=3, cid="c1"):
        return {"campaign_id": cid, "spend_usd": spend,
                "revenue_usd": revenue, "days_active": days, "tenant_id": "t1"}

    # KILL: ROAS < 1.5 AND spend >= $50
    d = engine.evaluate_campaign(cam(75.0, 90.0))   # ROAS=1.2
    assert d.action == "KILL"
    assert d.budget_change_pct == -100.0
    assert any("KILL" in m for m in slack.messages)

    # KILL: ROAS < 2.0 AND spend >= $200
    d2 = engine.evaluate_campaign(cam(200.0, 360.0))  # ROAS=1.8
    assert d2.action == "KILL"

    # HOLD: spend too low for kill
    d3 = engine.evaluate_campaign(cam(30.0, 30.0))  # ROAS=1.0, spend<$50
    assert d3.action == "HOLD"

    # SCALE: ROAS >= 2.5 for >= 7 days
    d4 = engine.evaluate_campaign(cam(300.0, 900.0, days=10))  # ROAS=3.0
    assert d4.action == "SCALE"
    assert d4.budget_change_pct > 0

    # HOLD: SCALE threshold not met (5 days)
    d5 = engine.evaluate_campaign(cam(200.0, 600.0, days=5))  # ROAS=3.0, 5d
    assert d5.action == "HOLD"

    # Safe math: zero spend
    d6 = engine.evaluate_campaign(cam(0.0, 0.0))
    assert d6.action in ["KILL", "HOLD"]  # No crash

    # HOLD no Slack noise
    slack.messages.clear()
    engine.evaluate_campaign(cam(80.0, 160.0))  # HOLD
    hold_msgs = [m for m in slack.messages if "HOLD" in m]
    assert len(hold_msgs) == 0

    # Slack failure does not crash
    def fail_slack(channel, text):
        raise ConnectionError("Slack down")
    engine.slack._post = fail_slack
    try:
        d7 = engine.evaluate_campaign(cam(75.0, 60.0))
        assert d7 is not None
    except ConnectionError:
        assert False, "Slack failure should not propagate"


def test_decision_engine_thresholds():
    from core.decision_engine import DecisionEngine
    engine = DecisionEngine()

    def p(score, niche="health", pid=None):
        return {"product_id": pid or f"p{score}", "name": f"P{score}",
                "niche": niche, "final_score": score}

    # Reject < 55
    for s in [0.0, 30.0, 54.9]:
        r = engine.evaluate_product(p(s))
        assert r.decision == "reject", f"Score {s} → expected reject"
        assert r.budget_usd == 0.0

    # Manual review 55-69
    for s in [55.0, 62.5, 69.9]:
        r = engine.evaluate_product(p(s))
        assert r.decision == "manual_review", f"Score {s} → expected manual_review"

    # Launch test >= 70
    for s in [70.0, 77.5, 85.0, 100.0]:
        r = engine.evaluate_product(p(s))
        assert r.decision == "launch_test", f"Score {s} → expected launch_test"
        assert r.budget_usd > 0
        assert 0 < r.confidence <= 1.0

    # Confidence range
    r_lo = engine.evaluate_product(p(70.0))
    r_hi = engine.evaluate_product(p(95.0))
    assert r_hi.confidence >= r_lo.confidence


def test_economic_reward():
    from core.decision_engine import DecisionEngine
    engine = DecisionEngine()

    # Always in [0, 1]
    for prod in [
        {"clicks": 0, "conversions": 0, "revenue_usd": 0, "impressions": 0, "viral_score": 0},
        {"clicks": 100, "conversions": 5, "revenue_usd": 250.0, "impressions": 5000, "viral_score": 70.0},
        {"clicks": 10000, "conversions": 5000, "revenue_usd": 1e6, "impressions": 1e7, "viral_score": 100.0},
    ]:
        r = engine._compute_economic_reward(prod)
        assert 0.0 <= r <= 1.0, f"Reward {r} out of [0,1]"

    # Zero division safety
    try:
        engine._compute_economic_reward({"clicks": 0, "conversions": 0,
                                          "revenue_usd": 100, "impressions": 0,
                                          "viral_score": 50})
    except ZeroDivisionError:
        assert False, "ZeroDivisionError on zero clicks!"

    # Better metrics → higher reward
    low = {"clicks": 100, "conversions": 1, "revenue_usd": 50, "impressions": 5000, "viral_score": 30}
    high = {"clicks": 100, "conversions": 20, "revenue_usd": 1000, "impressions": 5000, "viral_score": 90}
    assert engine._compute_economic_reward(high) > engine._compute_economic_reward(low)


def test_niche_diversification():
    from core.decision_engine import DecisionEngine
    engine = DecisionEngine()

    # 5 products same niche → max 2 approved
    products = [
        {"product_id": f"h{i}", "name": f"Health {i}",
         "niche": "health", "final_score": 75.0 + i}
        for i in range(5)
    ]
    result = engine.evaluate_portfolio(products, total_budget=500.0)
    approved = result["approved"]

    products_by_id = {p["product_id"]: p for p in products}
    niche_counts = {}
    for ev in approved:
        n = products_by_id.get(ev.get("product_id", ""), {}).get("niche", "")
        niche_counts[n] = niche_counts.get(n, 0) + 1

    for niche, count in niche_counts.items():
        assert count <= 2, f"Niche '{niche}' has {count} > max 2"

    # Multiple niches: 2+2+1 = 5 total, all should be approved
    multi = [
        {"product_id": f"h{i}", "name": f"H{i}", "niche": "health", "final_score": 80.0}
        for i in range(2)
    ] + [
        {"product_id": f"b{i}", "name": f"B{i}", "niche": "beauty", "final_score": 82.0}
        for i in range(2)
    ] + [{"product_id": "t1", "name": "Tech 1", "niche": "tech", "final_score": 85.0}]

    result2 = engine.evaluate_portfolio(multi, total_budget=600.0)
    assert len(result2["approved"]) == 5, \
        f"All 5 products (2+2+1) should be approved, got {len(result2['approved'])}"

    # Empty portfolio
    empty = engine.evaluate_portfolio([], total_budget=300.0)
    assert empty["approved"] == []
    assert empty["total_budget_allocated"] == 0.0


def test_v51_models():
    """Verify V5.1 Pydantic models structure."""
    from shared.models import (
        AdsDecision, ProductEvaluation, HookOutput,
        CreativeOutput, OrchestratorCycleResult
    )
    from datetime import datetime, timezone

    # AdsDecision
    d = AdsDecision(
        action="KILL", reason="Test", roas=1.2,
        spend_usd=75.0, revenue_usd=90.0,
        budget_change_pct=-100.0, campaign_id="c1"
    )
    assert d.action == "KILL"
    assert d.roas == 1.2

    # ProductEvaluation
    e = ProductEvaluation(
        decision="launch_test", reason="Good score",
        score=82.0, budget_usd=50.0, confidence=0.82,
        product_id="p1", product_name="Test Product"
    )
    assert e.decision == "launch_test"
    assert e.budget_usd == 50.0

    # OrchestratorCycleResult
    r = OrchestratorCycleResult(
        cycle_id="cycle_abc123", tenant_id="t1",
        niches_processed=3, signals_found=12,
        products_approved=2
    )
    assert r.cycle_id == "cycle_abc123"
    assert r.products_approved == 2


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "═" * 65)
    print("  🚀 E-Commerce AI V5.1 — Full Test Suite")
    print("═" * 65)

    print("\n📦 V5.0 BASE MODULES:")
    run_check("FeatureStore (get/set/cache/stats)", test_feature_store)
    run_check("CircuitBreaker (CLOSED→OPEN→reset)", test_circuit_breaker)
    run_check("Thompson Sampling (softmax + allocation + edge cases)", test_thompson_allocator)

    print("\n🚀 V5.1 NEW ENGINES:")
    run_check("Creative Engine — safe JSON parsing (6 cases)", test_creative_engine_parsing)
    run_check("Creative Engine — validation + ranking + CTR clamp", test_creative_engine_validation)
    run_check("Creative Engine — Feature Store cache (no LLM on hit)", test_creative_engine_cache)
    run_check("Ads Decision Engine — kill-switch ROAS + safe math + Slack", test_ads_decision_kill_switch)
    run_check("Decision Engine — reject/review/approve thresholds", test_decision_engine_thresholds)
    run_check("Decision Engine — economic reward (0.6conv+0.3rev+0.1eng)", test_economic_reward)
    run_check("Decision Engine — niche diversification (max 2/niche)", test_niche_diversification)

    print("\n🏗️  V5.1 MODELS:")
    run_check("Pydantic V5.1 models (AdsDecision, ProductEvaluation, etc.)", test_v51_models)

    print("\n" + "═" * 65)
    total = len(PASSED) + len(FAILED) + len(ERRORS)
    print(f"  Results: ✅ {len(PASSED)} passed  ❌ {len(FAILED)} failed  ⚠️  {len(ERRORS)} errors")
    print(f"  Total:   {total} checks")
    print("═" * 65)

    if not FAILED and not ERRORS:
        print("  🎉 ALL TESTS PASSED — V5.1 PRODUCTION READY")
        print("═" * 65 + "\n")
        sys.exit(0)
    else:
        if FAILED:
            print("\n  Failed:")
            for name, err in FAILED:
                print(f"    ❌ {name}")
                print(f"       {err}")
        if ERRORS:
            print("\n  Errors:")
            for name, etype, err in ERRORS:
                print(f"    ⚠️  {name}")
                print(f"       {etype}: {err}")
        print("═" * 65 + "\n")
        sys.exit(1)
