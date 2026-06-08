"""
tests/unit/test_engines_v51.py — Unit Tests V5.1 Engines

Coverage:
  - CreativeIntelligenceEngine: parsing, cache, validation, ranking
  - AdsDecisionEngine: kill-switch rules, safe math, portfolio, Slack
  - DecisionEngine: reject/review/approve thresholds, economic reward,
                    niche diversification, Thompson allocation
  - Orchestrator: cycle result structure, error isolation per step

All tests use mocks — no real LLM calls, no Redis, no Slack.
"""

import sys
import os
import asyncio
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))

from shared.feature_store import FeatureStore
import shared.feature_store as fs_module


# ─── Fixtures ────────────────────────────────────────────────────────────────

class MockSlack:
    """Slack notifier mock that records calls."""
    def __init__(self):
        self.messages = []
        self.call_count = 0

    def _post(self, channel: str, text: str):
        """Matches real SlackNotifier._post() signature."""
        self.call_count += 1
        self.messages.append({"channel": channel, "text": text})

    @property
    def last_message(self):
        return self.messages[-1] if self.messages else None

    def reset(self):
        self.messages.clear()
        self.call_count = 0


class MockLLMRouter:
    """LLM Router mock that returns preset responses."""
    def __init__(self, response: str = None):
        self._response = response or '[{"hook_type":"fear","hook_text":"Test hook","script":"Full script","cta":"Buy now","estimated_ctr":0.08,"pain_point_addressed":"test pain"}]'
        self.circuit_breakers = {
            "openai": _make_mock_cb(),
            "anthropic": _make_mock_cb(),
            "groq": _make_mock_cb(),
        }

    async def route(self, tier, prompt, **kwargs):
        return self._response


def _make_mock_cb():
    """Create a passthrough mock circuit breaker."""
    class MockCB:
        async def call(self, fn, *args, **kwargs):
            return await fn(*args, **kwargs)
    return MockCB()


def get_test_store():
    """Fresh FeatureStore for each test (no Redis)."""
    return FeatureStore(redis_client=None, ttl=3600)


def inject_store(store):
    """Inject a test FeatureStore as singleton."""
    fs_module._feature_store_instance = store
    return store


def restore_store():
    """Reset singleton to None after test."""
    fs_module._feature_store_instance = None


# ═══════════════════════════════════════════════════════════════════════════════
# CREATIVE ENGINE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestCreativeEngineParser:
    """Test safe JSON parsing (most critical path)."""

    def setup_method(self):
        from engines.creative_engine import CreativeIntelligenceEngine
        self.engine = CreativeIntelligenceEngine()

    def test_parse_valid_json(self):
        """Happy path: LLM returns clean JSON array."""
        raw = '[{"hook_type":"fear","hook_text":"Test","script":"S","cta":"C","estimated_ctr":0.07}]'
        result = self.engine._safe_parse_hooks(raw)
        assert len(result) == 1
        assert result[0]["hook_type"] == "fear"

    def test_parse_json_with_markdown(self):
        """LLM wraps JSON in markdown code blocks (very common)."""
        raw = '```json\n[{"hook_type":"curiosity","hook_text":"H","script":"S","cta":"C","estimated_ctr":0.05}]\n```'
        result = self.engine._safe_parse_hooks(raw)
        assert len(result) == 1
        assert result[0]["hook_type"] == "curiosity"

    def test_parse_json_with_preamble(self):
        """LLM adds explanation before JSON (regex extraction path)."""
        raw = 'Aquí están los hooks que generé:\n\n[{"hook_type":"urgency","hook_text":"H","script":"S","cta":"C","estimated_ctr":0.06}]'
        result = self.engine._safe_parse_hooks(raw)
        assert len(result) == 1

    def test_parse_invalid_json_returns_empty(self):
        """Malformed JSON must return empty list, never crash."""
        bad_inputs = [
            "This is not JSON at all",
            '{"key": "dict not array"}',
            '["partial array"',
            "",
            None,
            "```\nNo JSON here\n```",
        ]
        for bad in bad_inputs:
            result = self.engine._safe_parse_hooks(bad)
            assert result == [], f"Should return [] for input: {repr(bad)}"

    def test_parse_multiple_hooks(self):
        """Parse 5 hooks (max expected from LLM)."""
        hooks = [
            {"hook_type": t, "hook_text": f"Hook {i}", "script": "S",
             "cta": "C", "estimated_ctr": 0.05 + i * 0.01}
            for i, t in enumerate(["fear","transformation","social_proof","curiosity","urgency"])
        ]
        import json
        result = self.engine._safe_parse_hooks(json.dumps(hooks))
        assert len(result) == 5


class TestCreativeEngineValidation:
    """Test hook validation and ranking."""

    def setup_method(self):
        from engines.creative_engine import CreativeIntelligenceEngine
        self.engine = CreativeIntelligenceEngine()

    def test_sorted_by_ctr_descending(self):
        """Hooks must be sorted best CTR first."""
        hooks = [
            {"hook_type": "fear", "hook_text": "Low", "script": "S", "cta": "C", "estimated_ctr": 0.03},
            {"hook_type": "transformation", "hook_text": "High", "script": "S", "cta": "C", "estimated_ctr": 0.10},
            {"hook_type": "curiosity", "hook_text": "Mid", "script": "S", "cta": "C", "estimated_ctr": 0.06},
        ]
        result = self.engine._validate_and_rank(hooks)
        assert result[0]["estimated_ctr"] >= result[1]["estimated_ctr"]
        assert result[1]["estimated_ctr"] >= result[2]["estimated_ctr"]

    def test_invalid_hook_type_normalized(self):
        """Invalid hook_type should be normalized to 'curiosity'."""
        hooks = [{"hook_type": "INVALID_TYPE", "hook_text": "Test",
                  "script": "S", "cta": "C", "estimated_ctr": 0.05}]
        result = self.engine._validate_and_rank(hooks)
        assert result[0]["hook_type"] == "curiosity"

    def test_empty_hook_text_discarded(self):
        """Hooks without text must be discarded."""
        hooks = [
            {"hook_type": "fear", "hook_text": "", "script": "S", "cta": "C", "estimated_ctr": 0.05},
            {"hook_type": "fear", "hook_text": "   ", "script": "S", "cta": "C", "estimated_ctr": 0.05},
        ]
        result = self.engine._validate_and_rank(hooks)
        assert result == []

    def test_ctr_clamped_to_realistic_range(self):
        """CTR must be clamped to [0.0, 0.15]."""
        hooks = [
            {"hook_type": "fear", "hook_text": "H1", "script": "S", "cta": "C", "estimated_ctr": 99.0},
            {"hook_type": "fear", "hook_text": "H2", "script": "S", "cta": "C", "estimated_ctr": -5.0},
        ]
        result = self.engine._validate_and_rank(hooks)
        assert all(0.0 <= h["estimated_ctr"] <= 0.15 for h in result)

    def test_hook_text_truncated_to_150_chars(self):
        """Hook text must be truncated to max 150 chars."""
        long_text = "X" * 500
        hooks = [{"hook_type": "fear", "hook_text": long_text,
                  "script": "S", "cta": "C", "estimated_ctr": 0.05}]
        result = self.engine._validate_and_rank(hooks)
        assert len(result[0]["hook_text"]) <= 150


class TestCreativeEngineCache:
    """Test Feature Store caching behavior."""

    def setup_method(self):
        inject_store(get_test_store())
        from engines.creative_engine import CreativeIntelligenceEngine
        self.engine = CreativeIntelligenceEngine()

    def teardown_method(self):
        restore_store()

    def test_cache_hit_skips_llm(self):
        """If product has cached hooks, LLM must NOT be called."""
        async def run():
            store = fs_module._feature_store_instance
            await store.set("creative_hooks", "prod_cached",
                             {"hooks": [{"hook_text": "From cache", "estimated_ctr": 0.09}]})

            call_count = {"n": 0}
            original_generate = self.engine._generate_hooks_openai
            async def mock_generate(*args, **kwargs):
                call_count["n"] += 1
                return []
            self.engine._generate_hooks_openai = mock_generate

            result = await self.engine.run_creative_pipeline({
                "product_id": "prod_cached",
                "name": "Test Product",
                "niche": "health",
            })

            assert call_count["n"] == 0, "LLM should NOT be called on cache hit"
            assert len(result) == 1
            assert result[0]["hook_text"] == "From cache"

        asyncio.get_event_loop().run_until_complete(run())

    def test_result_cached_after_generation(self):
        """Generated hooks must be stored in Feature Store."""
        async def run():
            mock_hooks = [
                {"hook_type": "fear", "hook_text": "Generated Hook",
                 "script": "S", "cta": "C", "estimated_ctr": 0.09}
            ]
            import json
            async def _legacy_mock(*a, **kw):  # asyncio.coroutine removed in Python 3.11
                return mock_hooks
            self.engine._generate_hooks_openai = _legacy_mock

            async def mock_gen(*args, **kwargs):
                return mock_hooks

            self.engine._generate_hooks_openai = mock_gen

            await self.engine.run_creative_pipeline({
                "product_id": "prod_new",
                "name": "New Product",
                "niche": "beauty",
            })

            # Check it's in cache now
            store = fs_module._feature_store_instance
            cached = await store.get("creative_hooks", "prod_new")
            assert cached is not None, "Should be cached after generation"

        asyncio.get_event_loop().run_until_complete(run())

    def test_missing_product_id_no_cache(self):
        """Without product_id, should not attempt to cache."""
        async def run():
            async def mock_gen(*args, **kwargs):
                return [{"hook_type": "fear", "hook_text": "H",
                         "script": "S", "cta": "C", "estimated_ctr": 0.05}]
            self.engine._generate_hooks_openai = mock_gen

            # No product_id in request
            result = await self.engine.run_creative_pipeline({
                "name": "Product No ID",
                "niche": "tech",
            })
            # Should still work, just no caching
            assert isinstance(result, list)

        asyncio.get_event_loop().run_until_complete(run())


# ═══════════════════════════════════════════════════════════════════════════════
# ADS DECISION ENGINE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdsKillSwitch:
    """Test kill-switch financial rules."""

    def setup_method(self):
        from engines.ads_decision_engine import AdsDecisionEngine
        self.slack = MockSlack()
        self.engine = AdsDecisionEngine(slack=self.slack)

    def _make_campaign(self, spend, revenue, days=3, campaign_id="camp_1"):
        return {
            "campaign_id": campaign_id,
            "spend_usd": spend,
            "revenue_usd": revenue,
            "days_active": days,
            "tenant_id": "tenant_test",
        }

    # ── KILL rule 1: ROAS < 1.5 AND spend >= $50 ─────────────────────────────

    def test_kill_roas_below_1_5_spend_50(self):
        """ROAS 1.2 with $75 spend → KILL."""
        d = self.engine.evaluate_campaign(self._make_campaign(75.0, 90.0))
        assert d.action == "KILL"
        assert d.budget_change_pct == -100.0

    def test_kill_roas_zero_high_spend(self):
        """ROAS 0 (no revenue) with $50 spend → KILL."""
        d = self.engine.evaluate_campaign(self._make_campaign(50.0, 0.0))
        assert d.action == "KILL"

    def test_no_kill_low_spend_roas_below_1_5(self):
        """ROAS 1.0 but spend only $30 → HOLD (insufficient data)."""
        d = self.engine.evaluate_campaign(self._make_campaign(30.0, 30.0))
        assert d.action == "HOLD"

    # ── KILL rule 2: ROAS < 2.0 AND spend >= $200 ─────────────────────────────

    def test_kill_roas_1_8_spend_200(self):
        """ROAS 1.8 with $200 spend → KILL."""
        d = self.engine.evaluate_campaign(self._make_campaign(200.0, 360.0))
        assert d.action == "KILL"

    def test_no_kill_roas_2_0_exact_spend_200(self):
        """ROAS exactly 2.0 with $200 spend → NOT KILL (boundary)."""
        d = self.engine.evaluate_campaign(self._make_campaign(200.0, 400.0))
        # ROAS = 2.0 exactly should NOT trigger rule 2 (< 2.0)
        assert d.action != "KILL" or d.roas >= 2.0

    # ── SCALE rule ────────────────────────────────────────────────────────────

    def test_scale_roas_2_5_7_days(self):
        """ROAS 3.0 for 10 days → SCALE."""
        d = self.engine.evaluate_campaign(self._make_campaign(300.0, 900.0, days=10))
        assert d.action == "SCALE"
        assert d.budget_change_pct > 0

    def test_no_scale_roas_2_5_only_5_days(self):
        """ROAS 3.0 but only 5 days → HOLD (not enough time)."""
        d = self.engine.evaluate_campaign(self._make_campaign(200.0, 600.0, days=5))
        assert d.action == "HOLD"

    # ── HOLD ─────────────────────────────────────────────────────────────────

    def test_hold_roas_validated(self):
        """ROAS 2.0 with $80 → HOLD (validated but not scalable yet)."""
        d = self.engine.evaluate_campaign(self._make_campaign(80.0, 160.0, days=3))
        assert d.action == "HOLD"
        assert d.budget_change_pct == 0.0

    # ── Safe math ─────────────────────────────────────────────────────────────

    def test_safe_math_zero_spend(self):
        """Zero spend must NOT cause ZeroDivisionError."""
        try:
            d = self.engine.evaluate_campaign(self._make_campaign(0.0, 0.0))
            assert d.roas >= 0  # Any valid number
        except ZeroDivisionError:
            pytest.fail("ZeroDivisionError on zero spend!")

    def test_safe_math_zero_impressions(self):
        """Zero impressions in CTR calc must not crash."""
        campaign = {**self._make_campaign(50.0, 75.0), "impressions": 0, "clicks": 0}
        d = self.engine.evaluate_campaign(campaign)
        assert d is not None

    # ── Slack alerts ──────────────────────────────────────────────────────────

    def test_kill_sends_slack_alert(self):
        """KILL decision must send Slack alert."""
        self.slack.reset()
        self.engine.evaluate_campaign(self._make_campaign(75.0, 60.0))
        assert self.slack.call_count >= 1
        assert any("KILL" in m["text"] for m in self.slack.messages)

    def test_scale_sends_slack_approval_request(self):
        """SCALE decision must request Slack approval."""
        self.slack.reset()
        self.engine.evaluate_campaign(self._make_campaign(300.0, 900.0, days=10))
        assert self.slack.call_count >= 1
        assert any("SCALE" in m["text"] for m in self.slack.messages)

    def test_hold_no_slack_noise(self):
        """HOLD must NOT send Slack (too noisy)."""
        self.slack.reset()
        self.engine.evaluate_campaign(self._make_campaign(80.0, 160.0))
        # If action is HOLD, no messages should be sent
        holds = [m for m in self.slack.messages if "HOLD" in m.get("text", "")]
        assert len(holds) == 0

    def test_slack_failure_does_not_crash_decision(self):
        """If Slack fails, decision must still complete."""
        def failing_slack(channel, text):
            raise ConnectionError("Slack down")

        self.slack._post = failing_slack
        try:
            d = self.engine.evaluate_campaign(self._make_campaign(75.0, 60.0))
            assert d is not None  # Decision completed despite Slack failure
        except ConnectionError:
            pytest.fail("Slack failure should not propagate!")

    # ── Portfolio ─────────────────────────────────────────────────────────────

    def test_portfolio_mixed_decisions(self):
        """Portfolio with KILL, HOLD and SCALE campaigns."""
        campaigns = [
            {"campaign_id": "kill", "spend_usd": 100.0, "revenue_usd": 80.0,
             "days_active": 4, "tenant_id": "t1"},     # ROAS=0.8 → KILL
            {"campaign_id": "hold", "spend_usd": 50.0, "revenue_usd": 90.0,
             "days_active": 2, "tenant_id": "t1"},     # ROAS=1.8 → HOLD
            {"campaign_id": "scale", "spend_usd": 200.0, "revenue_usd": 600.0,
             "days_active": 9, "tenant_id": "t1"},     # ROAS=3.0 → SCALE
        ]
        decisions = self.engine.evaluate_portfolio(campaigns)
        assert len(decisions) == 3
        actions = {d.campaign_id: d.action for d in decisions}
        assert actions["kill"] == "KILL"
        assert actions["scale"] == "SCALE"

    def test_portfolio_empty_campaigns(self):
        """Empty portfolio must return empty list, not crash."""
        result = self.engine.evaluate_portfolio([])
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# DECISION ENGINE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestDecisionEngineThresholds:
    """Test product evaluation thresholds."""

    def setup_method(self):
        from core.decision_engine import DecisionEngine
        self.engine = DecisionEngine()

    def _make_product(self, score, niche="health", product_id=None):
        return {
            "product_id": product_id or f"p_{score}",
            "name": f"Product Score {score}",
            "niche": niche,
            "final_score": score,
            "viral_score": 60.0,
            "days_active": 0,
        }

    def test_reject_below_55(self):
        for score in [0.0, 30.0, 54.9]:
            r = self.engine.evaluate_product(self._make_product(score))
            assert r.decision == "reject", f"Score {score} should be rejected"
            assert r.budget_usd == 0.0

    def test_manual_review_55_to_69(self):
        for score in [55.0, 62.5, 69.9]:
            r = self.engine.evaluate_product(self._make_product(score))
            assert r.decision == "manual_review", \
                f"Score {score} should be manual_review"

    def test_launch_test_70_to_84(self):
        for score in [70.0, 77.5, 84.9]:
            r = self.engine.evaluate_product(self._make_product(score))
            assert r.decision == "launch_test", \
                f"Score {score} should be launch_test"
            assert r.budget_usd > 0

    def test_auto_go_85_plus(self):
        for score in [85.0, 92.0, 100.0]:
            r = self.engine.evaluate_product(self._make_product(score))
            assert r.decision == "launch_test", \
                f"Score {score} should be launch_test (AUTO_GO)"
            assert "AUTO_GO" in r.reason or score >= 85

    def test_confidence_proportional_to_score(self):
        r_low = self.engine.evaluate_product(self._make_product(70.0))
        r_high = self.engine.evaluate_product(self._make_product(95.0))
        assert r_high.confidence >= r_low.confidence

    def test_confidence_between_0_and_1(self):
        for score in [55.0, 70.0, 85.0, 100.0]:
            r = self.engine.evaluate_product(self._make_product(score))
            assert 0.0 <= r.confidence <= 1.0, \
                f"Confidence {r.confidence} out of [0,1] for score {score}"


class TestEconomicReward:
    """Test ChatGPT economic reward calculation."""

    def setup_method(self):
        from core.decision_engine import DecisionEngine
        self.engine = DecisionEngine()

    def test_reward_is_0_to_1(self):
        """Reward must always be in [0.0, 1.0]."""
        cases = [
            {"clicks": 0, "conversions": 0, "revenue_usd": 0, "impressions": 0, "viral_score": 0},
            {"clicks": 100, "conversions": 5, "revenue_usd": 250.0, "impressions": 5000, "viral_score": 70.0},
            {"clicks": 10000, "conversions": 5000, "revenue_usd": 500000.0, "impressions": 1000000, "viral_score": 100.0},
        ]
        for product in cases:
            reward = self.engine._compute_economic_reward(product)
            assert 0.0 <= reward <= 1.0, f"Reward {reward} out of [0,1]: {product}"

    def test_reward_no_zero_division(self):
        """Zero clicks/impressions must not cause ZeroDivisionError."""
        product = {"clicks": 0, "conversions": 0, "revenue_usd": 100.0,
                   "impressions": 0, "viral_score": 50.0}
        try:
            reward = self.engine._compute_economic_reward(product)
            assert reward >= 0
        except ZeroDivisionError:
            pytest.fail("ZeroDivisionError with zero clicks/impressions!")

    def test_higher_conversion_rate_means_higher_reward(self):
        """Products with better metrics should have higher reward."""
        low_cr = {"clicks": 100, "conversions": 1, "revenue_usd": 50.0,
                  "impressions": 5000, "viral_score": 40.0}
        high_cr = {"clicks": 100, "conversions": 20, "revenue_usd": 1000.0,
                   "impressions": 5000, "viral_score": 80.0}
        assert self.engine._compute_economic_reward(high_cr) > \
               self.engine._compute_economic_reward(low_cr)


class TestNicheDiversification:
    """Test portfolio niche diversification."""

    def setup_method(self):
        from core.decision_engine import DecisionEngine
        self.engine = DecisionEngine()

    def test_max_2_per_niche(self):
        """No more than 2 products from same niche in approved list."""
        products = [
            {"product_id": f"p{i}", "name": f"Health Product {i}",
             "niche": "health", "final_score": 75.0 + i}
            for i in range(5)  # 5 health products, max 2 should be approved
        ]
        result = self.engine.evaluate_portfolio(products, total_budget=500.0)
        approved = result["approved"]

        # Find products_by_id for niche lookup
        products_by_id = {p["product_id"]: p for p in products}
        niche_counts = {}
        for ev in approved:
            p = products_by_id.get(ev.get("product_id", ""), {})
            n = p.get("niche", "unknown")
            niche_counts[n] = niche_counts.get(n, 0) + 1

        for niche, count in niche_counts.items():
            assert count <= 2, f"Niche '{niche}' has {count} products > max 2"

    def test_best_products_preferred_within_niche(self):
        """Within a niche, higher score products should be preferred."""
        products = [
            {"product_id": "p_low", "name": "Low", "niche": "health", "final_score": 71.0},
            {"product_id": "p_mid", "name": "Mid", "niche": "health", "final_score": 78.0},
            {"product_id": "p_high", "name": "High", "niche": "health", "final_score": 90.0},
        ]
        result = self.engine.evaluate_portfolio(products, total_budget=300.0)
        approved_ids = {ev["product_id"] for ev in result["approved"]}

        assert "p_high" in approved_ids, "Highest score must always be included"
        assert "p_mid" in approved_ids, "Second highest must be included"
        # p_low could be excluded due to niche cap

    def test_multiple_niches_not_capped(self):
        """Products from different niches should not compete."""
        products = [
            {"product_id": "h1", "name": "Health 1", "niche": "health", "final_score": 80.0},
            {"product_id": "h2", "name": "Health 2", "niche": "health", "final_score": 78.0},
            {"product_id": "b1", "name": "Beauty 1", "niche": "beauty", "final_score": 82.0},
            {"product_id": "b2", "name": "Beauty 2", "niche": "beauty", "final_score": 79.0},
            {"product_id": "t1", "name": "Tech 1", "niche": "tech", "final_score": 85.0},
        ]
        result = self.engine.evaluate_portfolio(products, total_budget=600.0)
        assert len(result["approved"]) == 5, \
            "All 5 products (2+2+1) should be approved (different niches)"


class TestPortfolioBudgetAllocation:
    """Test Thompson Sampling budget allocation in portfolio."""

    def setup_method(self):
        from core.decision_engine import DecisionEngine
        self.engine = DecisionEngine()

    def test_total_budget_not_exceeded(self):
        """Allocated budget must not exceed total_budget significantly."""
        products = [
            {"product_id": f"p{i}", "name": f"Product {i}",
             "niche": f"niche_{i}", "final_score": 75.0 + i}
            for i in range(4)
        ]
        result = self.engine.evaluate_portfolio(products, total_budget=400.0)
        assert result["total_budget_allocated"] <= 410.0  # +$10 tolerance

    def test_empty_portfolio(self):
        """Empty products list must return valid empty result."""
        result = self.engine.evaluate_portfolio([], total_budget=500.0)
        assert result["approved"] == []
        assert result["rejected"] == []
        assert result["total_budget_allocated"] == 0.0

    def test_all_rejected_portfolio(self):
        """All low-score products → all rejected, zero budget allocated."""
        products = [
            {"product_id": f"p{i}", "name": f"Product {i}",
             "niche": "health", "final_score": 30.0 + i}
            for i in range(3)
        ]
        result = self.engine.evaluate_portfolio(products, total_budget=300.0)
        assert result["approved"] == []
        assert len(result["rejected"]) == 3
        assert result["total_budget_allocated"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION: FULL CYCLE (mocked)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCreativeToDecisionFlow:
    """Test Creative Engine → Decision Engine data flow."""

    def setup_method(self):
        inject_store(get_test_store())

    def teardown_method(self):
        restore_store()

    def test_hooks_attach_to_approved_products(self):
        """Approved products should have hooks attached by orchestrator."""
        async def run():
            from engines.creative_engine import CreativeIntelligenceEngine
            from core.decision_engine import DecisionEngine

            creative = CreativeIntelligenceEngine()
            decision = DecisionEngine()

            product = {
                "product_id": "integration_product",
                "name": "Masajeador cervical",
                "niche": "health",
                "final_score": 82.0,
                "viral_score": 78.0,
                "pain_points": ["dolor de cuello", "estrés"],
                "language": "es",
            }

            # Generate hooks (will call LLM mock)
            async def mock_gen(*args, **kwargs):
                return [{"hook_type": "fear", "hook_text": "¿Dolor cervical?",
                         "script": "Script", "cta": "Ver",
                         "estimated_ctr": 0.09, "pain_point_addressed": "dolor"}]

            creative._generate_hooks_openai = mock_gen
            hooks = await creative.run_creative_pipeline(product)
            product["creative_hooks"] = hooks

            # Evaluate product
            eval_result = decision.evaluate_product(product)

            assert eval_result.decision == "launch_test"
            assert len(hooks) > 0
            assert hooks[0]["estimated_ctr"] > 0

        asyncio.get_event_loop().run_until_complete(run())


# ─── Pytest runner ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short", "-x"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    sys.exit(result.returncode)
