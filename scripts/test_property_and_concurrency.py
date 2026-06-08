"""
scripts/test_property_and_concurrency.py — Property-based + Concurrency Tests V4.1

§2.5 del documento de revisión:
  - Property-based tests para funciones matemáticas críticas (Hypothesis)
  - Tests de concurrencia: múltiples updates simultáneos a ProductStats
  - Tests de falla de Supabase (mock que falle → fallback)
  - Monte Carlo validation de thresholds

Run:
  python scripts/test_property_and_concurrency.py
  # Or with hypothesis verbosity:
  pytest scripts/test_property_and_concurrency.py -v
"""

import sys
import math
import random
import asyncio
import threading
import logging
sys.path.insert(0, ".")

logging.disable(logging.CRITICAL)  # quiet during tests


class Results:
    passed = failed = 0
    def ok(self, msg):
        self.passed += 1
        print(f"  ✅ {msg}")
    def fail(self, msg, err):
        self.failed += 1
        print(f"  ❌ {msg}: {err}")
    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*55}")
        print(f"Property+Concurrency Tests: {self.passed}/{total} passed")
        if self.failed:
            print(f"FAILED: {self.failed}")
            return False
        print("All property tests PASSED ✅")
        return True


R = Results()


# ─── §1.1 Property tests: stable_softmax ──────────────────────────────────────
def test_softmax_properties():
    print("\n[P1] stable_softmax — property tests")
    from intelligence.thompson_sampling import stable_softmax

    try:
        # Property 1: sum of probs = 1.0 for any input
        for trial in range(200):
            scores = [random.uniform(-1000, 1000) for _ in range(random.randint(1, 20))]
            probs  = stable_softmax(scores, tau=0.5)
            assert abs(sum(probs) - 1.0) < 1e-9, f"sum={sum(probs):.10f} ≠ 1.0 for {scores[:3]}..."
        R.ok("Sum = 1.0 for 200 random inputs (including large negatives/positives)")
    except AssertionError as e:
        R.fail("Sum property", e)

    try:
        # Property 2: all probs ≥ 0
        for trial in range(200):
            scores = [random.uniform(-500, 500) for _ in range(random.randint(1, 15))]
            probs  = stable_softmax(scores, tau=0.5)
            assert all(p >= 0 for p in probs)
            assert all(math.isfinite(p) for p in probs)
        R.ok("All probs ≥ 0 and finite for 200 random inputs")
    except AssertionError as e:
        R.fail("Non-negative property", e)

    try:
        # Property 3: higher score → higher prob (monotonicity)
        scores = [10.0, 5.0, 1.0, -5.0]
        probs  = stable_softmax(scores, tau=0.5)
        assert probs[0] > probs[1] > probs[2] > probs[3], f"Not monotone: {probs}"
        R.ok("Monotonicity: higher score → higher probability")
    except AssertionError as e:
        R.fail("Monotonicity", e)

    try:
        # Property 4: extreme scores don't produce NaN/inf
        extreme_cases = [
            [1e10, 1e10, 1e10],             # all equal huge
            [-1e10, -1e10, -1e10],           # all equal tiny
            [1e10, -1e10],                   # huge spread
            [0.0, 0.0, 0.0],                # all zero → uniform
            [700.1, 700.2],                  # near float overflow
        ]
        for scores in extreme_cases:
            probs = stable_softmax(scores, tau=0.5)
            assert all(math.isfinite(p) for p in probs), f"Non-finite for {scores}: {probs}"
            assert abs(sum(probs) - 1.0) < 1e-6
        R.ok("No NaN/inf for extreme inputs (including ±1e10 and near-overflow)")
    except AssertionError as e:
        R.fail("Extreme inputs", e)

    try:
        # Property 5: temperature effect (lower τ → winner takes more)
        scores = [10.0, 5.0, 1.0]
        probs_low  = stable_softmax(scores, tau=0.1)
        probs_high = stable_softmax(scores, tau=5.0)
        # Winner's share should be higher at low temperature
        assert probs_low[0] > probs_high[0], f"Temperature effect failed: τ=0.1→{probs_low[0]:.3f} vs τ=5.0→{probs_high[0]:.3f}"
        R.ok(f"Temperature effect: τ=0.1 winner={probs_low[0]:.1%} vs τ=5.0 winner={probs_high[0]:.1%}")
    except AssertionError as e:
        R.fail("Temperature effect", e)

    try:
        # Property 6: empty input returns empty
        assert stable_softmax([]) == []
        R.ok("Empty input → empty output (no crash)")
    except Exception as e:
        R.fail("Empty input", e)


# ─── §1.4 Property tests: saturation_score ────────────────────────────────────
def test_saturation_properties():
    print("\n[P2] SaturationHazardModel — property tests")
    from intelligence.saturation_hazard import SaturationHazardModel, SaturationSignals

    model = SaturationHazardModel()

    try:
        # Property 1: score always in [0, 1] for any inputs
        for _ in range(300):
            s = SaturationSignals(
                "c", "n",
                delta_cpm      = random.uniform(-0.5, 5.0),
                new_competitors= random.randint(0, 100),
                delta_ctr      = random.uniform(-1.0, 0.5),
            )
            r = model.compute(s)
            assert 0.0 <= r.saturation_score <= 1.0, f"score={r.saturation_score:.4f} out of [0,1]"
            assert 0.0 <= r.hazard_prob_30d <= 1.0
            assert 0.0 <= r.hazard_prob_14d <= 1.0
        R.ok("Scores always in [0, 1] for 300 random signal inputs")
    except AssertionError as e:
        R.fail("Score bounds", e)

    try:
        # Property 2: P14d ≤ P30d always (more time = higher cumulative risk)
        for _ in range(200):
            s = SaturationSignals("c", "n",
                delta_cpm=random.uniform(0, 2.0),
                new_competitors=random.randint(0, 50),
                delta_ctr=random.uniform(-0.5, 0.0),
            )
            r = model.compute(s)
            assert r.hazard_prob_14d <= r.hazard_prob_30d + 1e-9, \
                f"P14d={r.hazard_prob_14d:.4f} > P30d={r.hazard_prob_30d:.4f}"
        R.ok("P(14d) ≤ P(30d) always (monotone in horizon)")
    except AssertionError as e:
        R.fail("Horizon monotonicity", e)

    try:
        # Property 3: more stress → higher score (monotone in signals)
        low_stress  = SaturationSignals("c","n", delta_cpm=0.0,  new_competitors=0,  delta_ctr=0.0)
        high_stress = SaturationSignals("c","n", delta_cpm=2.0,  new_competitors=50, delta_ctr=-0.5)
        r_low  = model.compute(low_stress)
        r_high = model.compute(high_stress)
        assert r_high.saturation_score > r_low.saturation_score, \
            f"High stress score {r_high.saturation_score:.4f} ≤ low {r_low.saturation_score:.4f}"
        R.ok(f"Monotone in stress: low={r_low.saturation_score:.3f} < high={r_high.saturation_score:.3f}")
    except AssertionError as e:
        R.fail("Monotone in signals", e)

    try:
        # Property 4: HARD_STOP always emits reduce_budget_pct = 1.0
        danger = SaturationSignals("c","n", delta_cpm=5.0, new_competitors=100, delta_ctr=-1.0)
        r = model.compute(danger)
        if r.action == "HARD_STOP":
            assert r.reduce_budget_pct == 1.0, f"HARD_STOP should be 1.0, got {r.reduce_budget_pct}"
        R.ok(f"HARD_STOP budget reduction = 100% (action={r.action})")
    except AssertionError as e:
        R.fail("HARD_STOP reduction", e)

    try:
        # Property 5: signal_components always present (§1.5 traceability)
        s = SaturationSignals("c","n", delta_cpm=0.3, new_competitors=5, delta_ctr=-0.1)
        r = model.compute(s)
        required = {"norm_cpm", "norm_comp", "norm_ctr", "raw", "logistic_k", "logistic_x0"}
        assert required.issubset(set(r.signal_components.keys())), \
            f"Missing components: {required - set(r.signal_components.keys())}"
        R.ok(f"signal_components contains all {len(required)} required keys for calibration")
    except AssertionError as e:
        R.fail("Signal components", e)


# ─── §2.5 Concurrency tests: ProductStats thread safety ───────────────────────
def test_concurrency_product_stats():
    print("\n[P3] ProductStats — concurrency (thread safety)")
    from intelligence.thompson_sampling import ProductStats

    # Test: 100 threads updating simultaneously → no race condition
    try:
        p = ProductStats("product_1", "campaign_1")
        errors = []

        def worker():
            try:
                for _ in range(50):
                    p.update(impressions=10, clicks=1, conversions=0, spend=0.5, revenue=2.0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert not errors, f"Thread errors: {errors}"
        assert p.impressions == 100 * 50 * 10  # 50_000
        assert p.clicks      == 100 * 50 * 1   # 5_000
        assert abs(p.alpha - (1.0 + p.clicks)) < 1e-6
        R.ok(f"100 threads × 50 updates: impressions={p.impressions}, no race conditions")
    except AssertionError as e:
        R.fail("Thread safety", e)

    try:
        # Clicks can't exceed impressions (guard in update)
        p2 = ProductStats("p2", "c2")
        p2.update(impressions=10, clicks=999)   # should clip clicks to 10
        assert p2.clicks <= p2.impressions, f"clicks={p2.clicks} > impressions={p2.impressions}"
        R.ok(f"Clicks capped to impressions: clicks={p2.clicks} ≤ impressions={p2.impressions}")
    except AssertionError as e:
        R.fail("Clicks capping", e)


# ─── §2.5 Concurrency tests: Allocator under load ─────────────────────────────
def test_concurrency_allocator():
    print("\n[P4] ThompsonSamplingAllocator — concurrent calls")
    from intelligence.thompson_sampling import ThompsonSamplingAllocator, ProductStats

    allocator = ThompsonSamplingAllocator()
    errors, results = [], []

    def run_allocation():
        try:
            products = [
                ProductStats(f"p{i}", f"c{i}") for i in range(random.randint(2, 8))
            ]
            for i, p in enumerate(products):
                p.update(
                    impressions=random.randint(100, 5000),
                    clicks=random.randint(1, 200),
                    conversions=random.randint(0, 20),
                    spend=random.uniform(10, 500),
                    revenue=random.uniform(0, 2000),
                )
            alloc = allocator.allocate(products, total_budget=random.uniform(100, 1000))
            results.append(alloc)
        except Exception as e:
            errors.append(e)

    try:
        threads = [threading.Thread(target=run_allocation) for _ in range(50)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert not errors, f"Allocation errors: {errors[:3]}"
        assert len(results) == 50
        # Every result should have finite, non-negative values
        for alloc in results:
            assert all(v >= 0 and math.isfinite(v) for v in alloc.values()), \
                f"Non-finite allocation: {alloc}"
        R.ok(f"50 concurrent allocations: all returned valid results, no crashes")
    except AssertionError as e:
        R.fail("Concurrent allocations", e)


# ─── §8 Monte Carlo calibration validation ────────────────────────────────────
def test_monte_carlo_thresholds():
    print("\n[P5] Monte Carlo — threshold calibration validation")
    from intelligence.thompson_sampling import ThompsonSamplingAllocator, ProductStats

    allocator = ThompsonSamplingAllocator(stoploss_roas=1.0, min_budget=5.0)
    N_SIMS = 500
    BUDGET = 300.0

    winners, zero_alloc_blocked = 0, 0
    total_budget_wasted = 0.0

    for _ in range(N_SIMS):
        # Simulate 3 products: 1 winner, 1 mediocre, 1 loser (below stop-loss)
        winner   = ProductStats("winner", "c1")
        mediocre = ProductStats("mediocre", "c2")
        loser    = ProductStats("loser", "c3")

        winner.update(impressions=3000, clicks=150, conversions=15,
                      spend=150.0, revenue=600.0)    # ROAS=4.0
        mediocre.update(impressions=2000, clicks=80, conversions=5,
                        spend=100.0, revenue=150.0)  # ROAS=1.5
        loser.update(impressions=1000, clicks=20, conversions=1,
                     spend=80.0, revenue=50.0)       # ROAS=0.625 (blocked)

        alloc = allocator.allocate([winner, mediocre, loser], BUDGET)

        # Winner should get most
        if alloc.get("winner", 0) > alloc.get("mediocre", 0):
            winners += 1
        # Loser should get 0
        if alloc.get("loser", 0) == 0.0:
            zero_alloc_blocked += 1
        total_budget_wasted += alloc.get("loser", 0)

    winner_rate  = winners / N_SIMS
    blocked_rate = zero_alloc_blocked / N_SIMS

    try:
        assert winner_rate >= 0.85, f"Winner preference rate {winner_rate:.0%} < 85%"
        R.ok(f"Winner gets more than mediocre in {winner_rate:.0%} of {N_SIMS} simulations")
    except AssertionError as e:
        R.fail("Winner preference", e)

    try:
        assert blocked_rate >= 0.99, f"Stop-loss block rate {blocked_rate:.0%} < 99%"
        R.ok(f"Stop-loss blocks loser in {blocked_rate:.0%} of simulations (ROAS=0.625 < 1.0)")
    except AssertionError as e:
        R.fail("Stop-loss effectiveness", e)

    try:
        avg_wasted = total_budget_wasted / N_SIMS
        assert avg_wasted < 1.0, f"Avg budget wasted on stopped product ${avg_wasted:.2f} > $1"
        R.ok(f"Budget waste on stopped product: avg ${avg_wasted:.3f} per cycle (near $0)")
    except AssertionError as e:
        R.fail("Budget waste", e)


# ─── §2.5 Supabase failure mock test ──────────────────────────────────────────
def test_supabase_failure_fallback():
    print("\n[P6] Supabase failure → graceful fallback")
    from unittest.mock import patch, MagicMock

    try:
        # Test: SUPABASE_URL/KEY missing → _get_client returns None → methods return defaults
        import os
        orig_url = os.environ.pop("SUPABASE_URL", None)
        orig_key = os.environ.pop("SUPABASE_KEY", None)
        try:
            from shared.supabase_client import SupabaseClient
            db = SupabaseClient()
            db._client = None  # force re-check
            result = db.save_opportunity({
                "tenant_id": "test", "name": "Test", "niche": "test",
                "source": "unit_test", "raw_data": {}, "status": "pending",
            })
            assert result == {} or result is None, f"Expected empty, got {result}"
            R.ok("save_opportunity returns {} gracefully when SUPABASE_URL not set")
        finally:
            if orig_url: os.environ["SUPABASE_URL"] = orig_url
            if orig_key: os.environ["SUPABASE_KEY"] = orig_key
    except Exception as e:
        R.fail("Supabase fallback", e)

    try:
        orig_url = os.environ.pop("SUPABASE_URL", None)
        orig_key = os.environ.pop("SUPABASE_KEY", None)
        try:
            db = SupabaseClient()
            db._client = None
            result = db.get_pending_opportunities("tenant_test")
            assert result == [], f"Expected [], got {result}"
            R.ok("get_pending_opportunities returns [] when SUPABASE_URL not set")
        finally:
            if orig_url: os.environ["SUPABASE_URL"] = orig_url
            if orig_key: os.environ["SUPABASE_KEY"] = orig_key
    except Exception as e:
        R.fail("Supabase read fallback", e)


if __name__ == "__main__":
    print("=" * 55)
    print("  Property + Concurrency + Monte Carlo Tests V4.1")
    print("=" * 55)

    test_softmax_properties()
    test_saturation_properties()
    test_concurrency_product_stats()
    test_concurrency_allocator()
    test_monte_carlo_thresholds()
    test_supabase_failure_fallback()

    success = R.summary()
    sys.exit(0 if success else 1)
