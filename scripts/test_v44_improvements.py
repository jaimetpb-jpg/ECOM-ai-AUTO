"""
scripts/test_v44_improvements.py

Tests de integración para las 3 mejoras de V4.4:
  1. NicheClusterer — clustering automático de nichos
  2. SurvivorshipScore — bonus/penalización por track record en ScoringEngine
  3. DB Indices — constantes y schema verificados

Ejecutar:
  python scripts/test_v44_improvements.py
  python -m pytest scripts/test_v44_improvements.py -v
"""

import sys
import os
import math
import json
import tempfile
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")


# ═══════════════════════════════════════════════════════════════════════════════
# MEJORA 1: NicheClusterer
# ═══════════════════════════════════════════════════════════════════════════════

def test_niche_clusterer_import():
    """El módulo debe importar correctamente."""
    from intelligence.niche_clusterer import NicheClusterer, extract_features
    assert NicheClusterer is not None
    assert extract_features is not None
    print("  ✅ NicheClusterer importable")


def test_extract_features_bounds():
    """extract_features siempre produce vector en [0,1]^4."""
    from intelligence.niche_clusterer import extract_features
    test_cases = [
        (0.0,    0.0,  0.0,   0.0),
        (999.0, 10.0, 100.0, 100.0),
        (49.99,  3.1,  65.0,  78.0),
        (19.99,  1.8,  30.0,  50.0),
    ]
    for price, roas, comp, dem in test_cases:
        vec = extract_features(price, roas, comp, dem)
        assert len(vec) == 4, f"Expected 4 dims, got {len(vec)}"
        for i, v in enumerate(vec):
            assert 0.0 <= v <= 1.0, f"Feature[{i}]={v} out of [0,1] for input ({price},{roas},{comp},{dem})"
    print("  ✅ extract_features: todos los vectores en [0,1]^4")


def test_niche_clusterer_fit_basic():
    """fit() con datos mínimos debe funcionar y marcar como fitted."""
    from intelligence.niche_clusterer import NicheClusterer

    clusterer = NicheClusterer(n_clusters=3)
    products = [
        {"price_usd": 49.99, "roas": 3.2, "competition_inv": 70, "demand": 80},
        {"price_usd": 19.99, "roas": 1.8, "competition_inv": 30, "demand": 60},
        {"price_usd": 79.99, "roas": 4.1, "competition_inv": 80, "demand": 85},
        {"price_usd": 14.99, "roas": 1.5, "competition_inv": 25, "demand": 45},
        {"price_usd": 34.99, "roas": 2.5, "competition_inv": 55, "demand": 70},
    ]
    clusterer.fit(products)

    assert clusterer._fitted is True
    assert len(clusterer._clusters) == 3
    assert clusterer._n_training_samples == 5
    print(f"  ✅ fit() OK | clusters={clusterer.get_all_cluster_ids()}")


def test_niche_clusterer_fit_raises_on_empty():
    """fit() con lista vacía debe lanzar ValueError."""
    from intelligence.niche_clusterer import NicheClusterer
    clusterer = NicheClusterer(n_clusters=3)
    try:
        clusterer.fit([])
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    print("  ✅ fit([]) raises ValueError correctly")


def test_niche_clusterer_assign_cluster():
    """assign_cluster() debe retornar un string válido de cluster_id."""
    from intelligence.niche_clusterer import NicheClusterer

    clusterer = NicheClusterer(n_clusters=3)
    products = [
        {"price_usd": 59.99, "roas": 3.5, "competition_inv": 75, "demand": 85},
        {"price_usd": 15.99, "roas": 1.6, "competition_inv": 25, "demand": 40},
        {"price_usd": 35.00, "roas": 2.3, "competition_inv": 50, "demand": 65},
        {"price_usd": 89.99, "roas": 4.0, "competition_inv": 82, "demand": 90},
        {"price_usd": 12.99, "roas": 1.3, "competition_inv": 20, "demand": 35},
        {"price_usd": 45.00, "roas": 2.8, "competition_inv": 60, "demand": 72},
    ]
    clusterer.fit(products)
    all_ids = set(clusterer.get_all_cluster_ids())

    # Test assignment for known product profiles
    test_cases = [
        (60.0, 3.5, 75.0, 85.0),   # premium low-comp → should be in a cluster
        (15.0, 1.6, 20.0, 40.0),   # budget high-comp
        (35.0, 2.3, 50.0, 65.0),   # mid moderate-comp
    ]
    for price, roas, comp, dem in test_cases:
        cid = clusterer.assign_cluster(price, roas, comp, dem)
        assert isinstance(cid, str), f"cluster_id must be str, got {type(cid)}"
        assert len(cid) > 0, "cluster_id must not be empty"
        assert cid in all_ids, f"cluster_id {cid!r} not in known clusters {all_ids}"
    print(f"  ✅ assign_cluster() returns valid cluster_ids from {all_ids}")


def test_niche_clusterer_assign_without_fit_raises():
    """assign_cluster() sin fit() debe lanzar RuntimeError."""
    from intelligence.niche_clusterer import NicheClusterer
    clusterer = NicheClusterer()
    try:
        clusterer.assign_cluster(49.99, 3.0, 60.0, 75.0)
        assert False, "Should have raised RuntimeError"
    except RuntimeError:
        pass
    print("  ✅ assign_cluster() without fit() raises RuntimeError")


def test_niche_clusterer_same_products_same_cluster():
    """Dos productos idénticos deben ir al mismo cluster."""
    from intelligence.niche_clusterer import NicheClusterer

    products = [
        {"price_usd": p, "roas": r, "competition_inv": c, "demand": d}
        for p, r, c, d in [
            (50, 3.0, 70, 80), (50, 3.1, 71, 79), (50, 2.9, 69, 81),  # cluster A
            (15, 1.5, 20, 40), (16, 1.6, 22, 42), (14, 1.4, 18, 38),  # cluster B
            (80, 4.5, 90, 92), (82, 4.4, 88, 91), (79, 4.6, 91, 93),  # cluster C
        ]
    ]
    clusterer = NicheClusterer(n_clusters=3)
    clusterer.fit(products)

    c1 = clusterer.assign_cluster(50.0, 3.0, 70.0, 80.0)
    c2 = clusterer.assign_cluster(51.0, 3.0, 71.0, 79.0)
    c3 = clusterer.assign_cluster(49.0, 2.9, 69.0, 81.0)
    assert c1 == c2 == c3, f"Similar products got different clusters: {c1}, {c2}, {c3}"
    print(f"  ✅ Similar products → same cluster: {c1}")


def test_niche_clusterer_different_products_different_clusters():
    """Productos muy diferentes deben ir a clusters distintos."""
    from intelligence.niche_clusterer import NicheClusterer

    products = [
        {"price_usd": p, "roas": r, "competition_inv": c, "demand": d}
        for p, r, c, d in [
            (100, 5.0, 95, 95), (105, 4.9, 93, 94), (98, 5.1, 96, 96),  # luxury low-comp
            (10,  1.2, 10, 20), (11,  1.3, 12, 22), (9,   1.1,  9, 18),  # budget high-comp
        ]
    ]
    clusterer = NicheClusterer(n_clusters=2)
    clusterer.fit(products)

    luxury = clusterer.assign_cluster(100.0, 5.0, 95.0, 95.0)
    budget = clusterer.assign_cluster(10.0,  1.2, 10.0, 20.0)
    assert luxury != budget, f"Luxury and budget should be different clusters, got same: {luxury}"
    print(f"  ✅ Luxury ({luxury}) != Budget ({budget})")


def test_niche_clusterer_persist_and_reload():
    """save_state() + load_state() debe producir asignaciones idénticas."""
    from intelligence.niche_clusterer import NicheClusterer

    products = [
        {"price_usd": 49.99, "roas": 3.2, "competition_inv": 70, "demand": 80},
        {"price_usd": 19.99, "roas": 1.8, "competition_inv": 30, "demand": 60},
        {"price_usd": 79.99, "roas": 4.1, "competition_inv": 80, "demand": 85},
        {"price_usd": 14.99, "roas": 1.5, "competition_inv": 25, "demand": 45},
        {"price_usd": 34.99, "roas": 2.5, "competition_inv": 55, "demand": 70},
    ]
    original = NicheClusterer(n_clusters=3)
    original.fit(products)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        path = f.name

    original.save_state(path)

    reloaded = NicheClusterer(n_clusters=3)
    reloaded.load_state(path)

    test_inputs = [(50.0, 3.0, 70.0, 80.0), (15.0, 1.5, 25.0, 45.0), (80.0, 4.0, 80.0, 85.0)]
    for price, roas, comp, dem in test_inputs:
        c_orig   = original.assign_cluster(price, roas, comp, dem)
        c_reload = reloaded.assign_cluster(price, roas, comp, dem)
        assert c_orig == c_reload, (
            f"Mismatch after reload: orig={c_orig} reload={c_reload} for ({price},{roas},{comp},{dem})"
        )

    os.unlink(path)
    print(f"  ✅ save_state/load_state: assignments identical after reload")


def test_niche_clusterer_wires_to_hierarchical_bayesian():
    """cluster_id del NicheClusterer debe poder usarse en HierarchicalBayesianAllocator."""
    from intelligence.niche_clusterer import NicheClusterer
    from intelligence.hierarchical_bayesian import HierarchicalBayesianAllocator

    products = [
        {"price_usd": 49.99, "roas": 3.2, "competition_inv": 70, "demand": 80},
        {"price_usd": 19.99, "roas": 1.8, "competition_inv": 30, "demand": 60},
        {"price_usd": 79.99, "roas": 4.1, "competition_inv": 80, "demand": 85},
        {"price_usd": 14.99, "roas": 1.5, "competition_inv": 25, "demand": 45},
        {"price_usd": 34.99, "roas": 2.5, "competition_inv": 55, "demand": 70},
        {"price_usd": 55.00, "roas": 3.0, "competition_inv": 65, "demand": 75},
    ]

    clusterer = NicheClusterer(n_clusters=3)
    clusterer.fit(products)

    allocator = HierarchicalBayesianAllocator()

    # Register all discovered clusters with the Bayesian allocator
    for cluster_id in clusterer.get_all_cluster_ids():
        allocator.register_cluster(cluster_id)

    # Assign and register 4 campaigns
    campaign_clusters = {}
    test_products = [
        ("camp_001", 50.0, 3.2, 70.0, 80.0),
        ("camp_002", 20.0, 1.8, 30.0, 60.0),
        ("camp_003", 80.0, 4.0, 82.0, 88.0),
        ("camp_004", 35.0, 2.5, 55.0, 72.0),
    ]
    for camp_id, price, roas, comp, dem in test_products:
        cluster_id = clusterer.assign_cluster(price, roas, comp, dem)
        allocator.register_campaign(camp_id, cluster_id)
        campaign_clusters[camp_id] = cluster_id

    # Allocate budget
    all_camp_ids = [t[0] for t in test_products]
    first_cluster = campaign_clusters["camp_001"]
    allocation = allocator.allocate(all_camp_ids, first_cluster, total_budget=4000.0)

    assert len(allocation) == 4
    total = sum(allocation.values())
    assert total > 0, "Total allocation must be > 0"
    for cid, amount in allocation.items():
        assert amount >= 0, f"Negative allocation for {cid}: {amount}"

    print(
        f"  ✅ NicheClusterer -> HierarchicalBayesian wiring OK | "
        f"total=${total:.2f} | clusters={set(campaign_clusters.values())}"
    )


def test_niche_clusterer_k_larger_than_samples():
    """k > n_products debe ajustarse a n_products sin crash."""
    from intelligence.niche_clusterer import NicheClusterer

    # k=10 but only 3 products: should clamp k to 3
    clusterer = NicheClusterer(n_clusters=10)
    products = [
        {"price_usd": 49.99, "roas": 3.0, "competition_inv": 70, "demand": 80},
        {"price_usd": 19.99, "roas": 1.8, "competition_inv": 30, "demand": 60},
        {"price_usd": 79.99, "roas": 4.0, "competition_inv": 85, "demand": 90},
    ]
    clusterer.fit(products)
    assert clusterer._fitted
    assert len(clusterer._clusters) <= 10
    cid = clusterer.assign_cluster(50.0, 3.0, 70.0, 80.0)
    assert isinstance(cid, str)
    print(f"  ✅ k=10 with 3 products handled gracefully: k_actual={len(clusterer._clusters)}")


def test_niche_clusterer_summary_structure():
    """get_summary() debe retornar estructura válida."""
    from intelligence.niche_clusterer import NicheClusterer

    clusterer = NicheClusterer(n_clusters=2)
    products = [
        {"price_usd": 50, "roas": 3.0, "competition_inv": 70, "demand": 80},
        {"price_usd": 20, "roas": 1.8, "competition_inv": 30, "demand": 60},
        {"price_usd": 80, "roas": 4.0, "competition_inv": 85, "demand": 90},
    ]
    clusterer.fit(products)
    summary = clusterer.get_summary()

    assert "fitted" in summary
    assert "n_clusters" in summary
    assert "n_training_samples" in summary
    assert "clusters" in summary
    assert summary["fitted"] is True
    assert summary["n_training_samples"] == 3
    for c in summary["clusters"]:
        assert "cluster_id" in c
        assert "centroid" in c
        assert len(c["centroid"]) == 4
    print(f"  ✅ get_summary() structure valid | {summary['n_clusters']} clusters")


# ═══════════════════════════════════════════════════════════════════════════════
# MEJORA 2: SurvivorshipScore en ScoringEngine
# ═══════════════════════════════════════════════════════════════════════════════

def test_survivorship_math():
    """compute_survivorship_score debe seguir la fórmula log1p(days)*roas."""
    from scoring.engine import compute_survivorship_score

    # Known values
    assert abs(compute_survivorship_score(0,  2.0) - 0.0) < 1e-9
    assert abs(compute_survivorship_score(30, 0.0) - 0.0) < 1e-9
    assert abs(compute_survivorship_score(-1, 2.0) - 0.0) < 1e-9

    # 30 days, ROAS 2.5 → log1p(30)*2.5 ≈ 8.59 → should be >= 8.0
    s30 = compute_survivorship_score(30, 2.5)
    assert s30 >= 8.0, f"Expected >= 8.0, got {s30}"

    # 90 days, ROAS 3.5 → should be >= 15.0
    s90 = compute_survivorship_score(90, 3.5)
    assert s90 >= 15.0, f"Expected >= 15.0, got {s90}"

    print(f"  ✅ compute_survivorship_score | 3d={compute_survivorship_score(3,2.0):.2f}"
          f" | 30d={s30:.2f} | 90d={s90:.2f}")


def test_scoring_new_product_caution():
    """days_active=0 → NEW_PRODUCT_CAUTION_PENALTY flag y ajuste negativo en breakdown."""
    from scoring.engine import ScoringEngine, ScoreInput

    engine = ScoringEngine()
    inp = ScoreInput(
        name="New Product", niche="fitness",
        demand=80, competition_inv=70, margin=75,
        differentiation=70, logistics=80,
        viral_score=60, days_active=0, empirical_roas=0.0,
        supplier_count=2,
    )
    result = engine.score(inp)

    assert any("NEW_PRODUCT_CAUTION" in f for f in result.flags), (
        f"Expected NEW_PRODUCT_CAUTION flag, got: {result.flags}"
    )
    assert result.breakdown["survivorship_adj"] == -2.0, (
        f"Expected -2.0 adj, got {result.breakdown['survivorship_adj']}"
    )
    assert result.breakdown["survivorship_score_raw"] == 0.0
    print(f"  ✅ NEW_PRODUCT_CAUTION: adj={result.breakdown['survivorship_adj']} | score={result.final_score}")


def test_scoring_survivorship_validated():
    """30d + ROAS 2.5 → SURVIVORSHIP_VALIDATED (+3pts)."""
    from scoring.engine import ScoringEngine, ScoreInput

    engine = ScoringEngine()
    base = dict(
        name="Validated", niche="skincare",
        demand=80, competition_inv=70, margin=75,
        differentiation=70, logistics=80,
        viral_score=60, supplier_count=2,
    )

    new_result = engine.score(ScoreInput(**base, days_active=0,  empirical_roas=0.0))
    val_result = engine.score(ScoreInput(**base, days_active=30, empirical_roas=2.5))

    assert any("SURVIVORSHIP_VALIDATED" in f for f in val_result.flags), (
        f"Expected SURVIVORSHIP_VALIDATED, got: {val_result.flags}"
    )
    assert val_result.breakdown["survivorship_adj"] == 3.0
    # Validated product must score higher than new product
    assert val_result.final_score > new_result.final_score, (
        f"Validated ({val_result.final_score}) should beat new ({new_result.final_score})"
    )
    diff = val_result.final_score - new_result.final_score
    assert abs(diff - 5.0) < 0.01, (
        f"Difference should be 5.0 (+3 validated vs -2 new), got {diff}"
    )
    print(
        f"  ✅ SURVIVORSHIP_VALIDATED: +3pts | new={new_result.final_score}"
        f" validated={val_result.final_score} (delta={diff:.1f}pts)"
    )


def test_scoring_survivorship_proven():
    """90d + ROAS 3.5 → SURVIVORSHIP_PROVEN (+6pts)."""
    from scoring.engine import ScoringEngine, ScoreInput

    engine = ScoringEngine()
    base = dict(
        name="Proven", niche="skincare",
        demand=80, competition_inv=70, margin=75,
        differentiation=70, logistics=80,
        viral_score=60, supplier_count=2,
    )

    new_result   = engine.score(ScoreInput(**base, days_active=0,  empirical_roas=0.0))
    proven_result = engine.score(ScoreInput(**base, days_active=90, empirical_roas=3.5))

    assert any("SURVIVORSHIP_PROVEN" in f for f in proven_result.flags), (
        f"Expected SURVIVORSHIP_PROVEN, got: {proven_result.flags}"
    )
    assert proven_result.breakdown["survivorship_adj"] == 6.0
    diff = proven_result.final_score - new_result.final_score
    assert abs(diff - 8.0) < 0.01, (
        f"Difference should be 8.0 (+6 proven vs -2 new), got {diff}"
    )
    print(
        f"  ✅ SURVIVORSHIP_PROVEN: +6pts | new={new_result.final_score}"
        f" proven={proven_result.final_score} (delta={diff:.1f}pts)"
    )


def test_scoring_survivorship_early():
    """14d + ROAS 2.5 → SURVIVORSHIP_EARLY (informational, no bonus/penalty)."""
    from scoring.engine import ScoringEngine, ScoreInput

    engine = ScoringEngine()
    inp = ScoreInput(
        name="Early", niche="fitness",
        demand=80, competition_inv=70, margin=75,
        differentiation=70, logistics=80,
        viral_score=60, supplier_count=2,
        days_active=14, empirical_roas=2.5,
    )
    result = engine.score(inp)
    assert any("SURVIVORSHIP_EARLY" in f for f in result.flags), (
        f"Expected SURVIVORSHIP_EARLY, got: {result.flags}"
    )
    assert result.breakdown["survivorship_adj"] == 0.0
    print(f"  ✅ SURVIVORSHIP_EARLY: adj=0 | score={result.final_score}")


def test_scoring_survivorship_does_not_override_hard_stop():
    """HARD_STOP must still fire even with proven survivorship."""
    from scoring.engine import ScoringEngine, ScoreInput

    engine = ScoringEngine()
    inp = ScoreInput(
        name="Illegal Proven", niche="pharma",
        demand=80, competition_inv=70, margin=75,
        differentiation=70, logistics=80,
        legal_risk=0.9,  # HARD STOP
        days_active=90, empirical_roas=4.0,  # would be +6pts
    )
    result = engine.score(inp)
    assert result.decision == "HARD_STOP", f"Expected HARD_STOP, got {result.decision}"
    assert result.final_score == 0.0
    print(f"  ✅ Survivorship does NOT override HARD_STOP: decision={result.decision}")


def test_scoring_survivorship_does_not_override_saturation_skip():
    """SATURATION_FORCED_SKIP must still fire even with proven survivorship."""
    from scoring.engine import ScoringEngine, ScoreInput

    engine = ScoringEngine()
    inp = ScoreInput(
        name="Saturated Proven", niche="fidget_spinner",
        demand=80, competition_inv=70, margin=75,
        differentiation=70, logistics=80,
        saturation_prob=0.90,  # FORCED SKIP
        days_active=90, empirical_roas=4.0,  # would be +6pts
    )
    result = engine.score(inp)
    assert result.decision == "SKIP", f"Expected SKIP, got {result.decision}"
    assert any("SATURATION_FORCED_SKIP" in f for f in result.flags)
    print(f"  ✅ Survivorship does NOT override SATURATION_FORCED_SKIP: decision={result.decision}")


def test_scoring_breakdown_has_survivorship_fields():
    """breakdown debe siempre incluir survivorship_adj y survivorship_score_raw."""
    from scoring.engine import ScoringEngine, ScoreInput
    import random

    engine = ScoringEngine()
    rng = random.Random(42)

    for i in range(100):
        inp = ScoreInput(
            name=f"P{i}", niche="n",
            demand=rng.uniform(0, 100),
            competition_inv=rng.uniform(0, 100),
            margin=rng.uniform(0, 100),
            differentiation=rng.uniform(0, 100),
            logistics=rng.uniform(0, 100),
            viral_score=rng.uniform(0, 100),
            legal_risk=rng.uniform(0, 0.59),
            saturation_prob=rng.uniform(0, 0.79),
            supplier_count=rng.choice([1, 2, 3]),
            days_active=rng.randint(0, 120),
            empirical_roas=rng.uniform(0, 5.0),
        )
        result = engine.score(inp)
        assert "survivorship_adj" in result.breakdown, f"Missing survivorship_adj at i={i}"
        assert "survivorship_score_raw" in result.breakdown, f"Missing survivorship_score_raw at i={i}"
        assert 0.0 <= result.final_score <= 100.0, f"Score out of range: {result.final_score}"
        assert result.decision in ("AUTO_GO", "MANUAL_REVIEW", "SKIP", "HARD_STOP")

    print(f"  ✅ 100 random inputs: breakdown always contains survivorship fields, score always 0-100")


def test_scoring_proven_beats_new_when_equal_fundamentals():
    """Con mismos fundamentales, un producto probado debe superar al nuevo."""
    from scoring.engine import ScoringEngine, ScoreInput

    engine = ScoringEngine()
    base = dict(
        name="X", niche="test",
        demand=75, competition_inv=65, margin=70,
        differentiation=68, logistics=72,
        viral_score=55, supplier_count=2,
        legal_risk=0.0, saturation_prob=0.1,
        meta_ad_competitor_count=10,
    )

    scores = []
    for days, roas in [(0, 0.0), (3, 1.5), (14, 2.0), (30, 2.5), (60, 3.0), (90, 3.5)]:
        r = engine.score(ScoreInput(**base, days_active=days, empirical_roas=roas))
        scores.append((days, roas, r.final_score, r.breakdown["survivorship_adj"]))

    # days=0 must have lowest score (penalty), days=90+ROAS3.5 must have highest
    assert scores[0][2] < scores[-1][2], (
        f"New product ({scores[0][2]}) must score less than proven ({scores[-1][2]})"
    )
    print("  ✅ Survivorship score progression:")
    for days, roas, score, adj in scores:
        print(f"     days={days:3d} roas={roas:.1f} → score={score:.1f} (surv_adj={adj:+.0f})")


# ═══════════════════════════════════════════════════════════════════════════════
# MEJORA 3: DB Indices
# ═══════════════════════════════════════════════════════════════════════════════

def test_db_schema_has_new_indices():
    """El SCHEMA_V4_SQL debe contener todos los índices V4.4."""
    from shared.supabase_client import SCHEMA_V4_SQL

    required_indices = [
        # V4.4 new indices
        "idx_decision_log_tenant_ts",
        "idx_decision_log_entity",
        "idx_decision_log_action",
        "idx_campaigns_tenant_status",
        "idx_campaigns_opportunity",
        "idx_opportunities_tenant_score",
        "idx_opportunities_niche",
        "idx_metrics_ts",
        "idx_saturation_campaign_ts",
        "idx_allocation_tenant_ts",
        # Original indices still present
        "idx_opportunities_status",
        "idx_campaigns_status",
        "idx_saturation_ts",
        "idx_metrics_campaign",
        "idx_hooks_niche",
    ]

    missing = []
    for idx in required_indices:
        if idx not in SCHEMA_V4_SQL:
            missing.append(idx)

    assert not missing, f"Missing indices in SCHEMA_V4_SQL: {missing}"
    print(f"  ✅ SCHEMA_V4_SQL contains all {len(required_indices)} indices (5 original + 10 new)")


def test_db_schema_indices_are_idempotent():
    """Todos los indices deben usar IF NOT EXISTS para seguridad en re-run."""
    from shared.supabase_client import SCHEMA_V4_SQL

    required_indices = [
        "idx_decision_log_tenant_ts", "idx_decision_log_entity",
        "idx_decision_log_action", "idx_campaigns_tenant_status",
        "idx_campaigns_opportunity", "idx_opportunities_tenant_score",
        "idx_opportunities_niche", "idx_metrics_ts",
        "idx_saturation_campaign_ts", "idx_allocation_tenant_ts",
    ]
    for idx in required_indices:
        pattern = f"IF NOT EXISTS {idx}"
        assert pattern in SCHEMA_V4_SQL, (
            f"Index {idx} missing IF NOT EXISTS — not safe for re-run"
        )
    print(f"  ✅ All new indices use IF NOT EXISTS (safe to re-run migrations)")


def test_survivorship_constants_importable():
    """Las constantes de survivorship deben importarse correctamente."""
    from shared.constants import (
        SURVIVORSHIP_VALIDATED_THRESHOLD,
        SURVIVORSHIP_PROVEN_THRESHOLD,
        SURVIVORSHIP_VALIDATED_BONUS,
        SURVIVORSHIP_PROVEN_BONUS,
        NEW_PRODUCT_CAUTION_PENALTY,
    )
    assert SURVIVORSHIP_VALIDATED_THRESHOLD == 8.0
    assert SURVIVORSHIP_PROVEN_THRESHOLD    == 15.0
    assert SURVIVORSHIP_VALIDATED_BONUS     == 3.0
    assert SURVIVORSHIP_PROVEN_BONUS        == 6.0
    assert NEW_PRODUCT_CAUTION_PENALTY      == 2.0
    # Business logic check: proven bonus > validated bonus > caution penalty
    assert SURVIVORSHIP_PROVEN_BONUS > SURVIVORSHIP_VALIDATED_BONUS > NEW_PRODUCT_CAUTION_PENALTY
    print(
        f"  ✅ Survivorship constants: "
        f"validated={SURVIVORSHIP_VALIDATED_THRESHOLD} (+{SURVIVORSHIP_VALIDATED_BONUS}pts) | "
        f"proven={SURVIVORSHIP_PROVEN_THRESHOLD} (+{SURVIVORSHIP_PROVEN_BONUS}pts) | "
        f"new_penalty=-{NEW_PRODUCT_CAUTION_PENALTY}pts"
    )


def test_score_request_has_survivorship_fields():
    """ScoreRequest en main.py debe tener days_active y empirical_roas."""
    # We test by importing main's model directly (without starting FastAPI)
    import ast
    with open(os.path.join(os.path.dirname(__file__), "..", "main.py")) as f:
        src = f.read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ScoreRequest":
            fields = [n.target.id for n in ast.walk(node)
                      if isinstance(n, ast.AnnAssign) and hasattr(n.target, "id")]
            assert "days_active" in fields, f"days_active missing from ScoreRequest: {fields}"
            assert "empirical_roas" in fields, f"empirical_roas missing from ScoreRequest: {fields}"
            print(f"  ✅ ScoreRequest has days_active and empirical_roas | all fields: {len(fields)}")
            return
    assert False, "ScoreRequest class not found in main.py"


# ═══════════════════════════════════════════════════════════════════════════════
# Integration test: all 3 improvements working together
# ═══════════════════════════════════════════════════════════════════════════════

def test_full_v44_integration():
    """
    Integration test: NicheClusterer -> HierarchicalBayesian + SurvivorshipScore.
    Simulates the complete lifecycle:
      1. Fit clusterer on historical data
      2. Score new product with survivorship data
      3. Assign cluster automatically
      4. Register with Bayesian allocator using auto-cluster
      5. Allocate budget
    """
    from intelligence.niche_clusterer import NicheClusterer
    from intelligence.hierarchical_bayesian import HierarchicalBayesianAllocator
    from scoring.engine import ScoringEngine, ScoreInput
    import random

    rng = random.Random(42)

    # Step 1: Historical product data (simulates what comes from Supabase)
    historical = [
        {"price_usd": p, "roas": r, "competition_inv": c, "demand": d}
        for p, r, c, d in [
            (59.99, 3.5, 75, 85), (54.99, 3.2, 72, 82), (64.99, 3.7, 78, 88),
            (19.99, 1.7, 25, 42), (22.99, 1.9, 28, 45), (17.99, 1.6, 22, 40),
            (79.99, 4.2, 88, 92), (84.99, 4.5, 90, 95), (74.99, 4.0, 86, 90),
        ]
    ]
    clusterer = NicheClusterer(n_clusters=3)
    clusterer.fit(historical)

    # Step 2: Score a product with real survivorship data
    engine = ScoringEngine()
    product_input = ScoreInput(
        name="ProSkin Serum V2", niche="skincare",
        demand=82, competition_inv=72, margin=78,
        differentiation=74, logistics=80,
        viral_score=65, supplier_count=2,
        legal_risk=0.0, saturation_prob=0.15,
        days_active=45, empirical_roas=3.1,
        price_usd=59.99,
    )
    score_result = engine.score(product_input)
    assert score_result.decision in ("AUTO_GO", "MANUAL_REVIEW"), (
        f"Good product should be GO or REVIEW, got {score_result.decision}"
    )
    assert score_result.breakdown["survivorship_adj"] == 3.0  # 45d * 3.1 = 11.7 → VALIDATED

    # Step 3: Auto-assign cluster
    cluster_id = clusterer.assign_cluster_from_score(product_input)
    assert cluster_id in clusterer.get_all_cluster_ids()

    # Step 4 + 5: Register and allocate
    allocator = HierarchicalBayesianAllocator()
    for cid in clusterer.get_all_cluster_ids():
        allocator.register_cluster(cid)

    campaigns = [f"camp_{i:03d}" for i in range(5)]
    for camp in campaigns:
        # Each campaign goes through clusterer -> auto-assigned cluster
        p = rng.uniform(40, 80)
        r = rng.uniform(2.5, 4.0)
        alloc_cluster = clusterer.assign_cluster(p, r, rng.uniform(60, 85), rng.uniform(75, 90))
        allocator.register_campaign(camp, alloc_cluster)
        allocator.update_campaign(camp, impressions=1000, clicks=30, revenue=800, spend=300)

    allocation = allocator.allocate(campaigns, cluster_id, total_budget=5000.0)
    assert len(allocation) == 5
    total = sum(allocation.values())
    assert total > 0

    print(
        f"  ✅ Full V4.4 integration OK | "
        f"score={score_result.final_score:.1f} ({score_result.decision}) | "
        f"cluster={cluster_id} | "
        f"surv_adj={score_result.breakdown['survivorship_adj']:+.0f}pts | "
        f"budget_total=${total:.2f}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test runner
# ═══════════════════════════════════════════════════════════════════════════════

def run_all_tests():
    tests = [
        # ── Mejora 1: NicheClusterer ──────────────────────────────────────
        ("NicheClusterer - import",                        test_niche_clusterer_import),
        ("NicheClusterer - extract_features bounds",       test_extract_features_bounds),
        ("NicheClusterer - fit basic",                     test_niche_clusterer_fit_basic),
        ("NicheClusterer - fit raises on empty",           test_niche_clusterer_fit_raises_on_empty),
        ("NicheClusterer - assign_cluster returns valid",  test_niche_clusterer_assign_cluster),
        ("NicheClusterer - assign without fit raises",     test_niche_clusterer_assign_without_fit_raises),
        ("NicheClusterer - similar → same cluster",        test_niche_clusterer_same_products_same_cluster),
        ("NicheClusterer - different → different cluster", test_niche_clusterer_different_products_different_clusters),
        ("NicheClusterer - persist and reload",            test_niche_clusterer_persist_and_reload),
        ("NicheClusterer - wires to HierarchicalBayesian", test_niche_clusterer_wires_to_hierarchical_bayesian),
        ("NicheClusterer - k > n_samples handled",         test_niche_clusterer_k_larger_than_samples),
        ("NicheClusterer - get_summary structure",         test_niche_clusterer_summary_structure),
        # ── Mejora 2: Survivorship Score ──────────────────────────────────
        ("SurvivorshipScore - math formula",               test_survivorship_math),
        ("SurvivorshipScore - new product caution",        test_scoring_new_product_caution),
        ("SurvivorshipScore - validated (+3pts)",          test_scoring_survivorship_validated),
        ("SurvivorshipScore - proven (+6pts)",             test_scoring_survivorship_proven),
        ("SurvivorshipScore - early (neutral)",            test_scoring_survivorship_early),
        ("SurvivorshipScore - no override HARD_STOP",      test_scoring_survivorship_does_not_override_hard_stop),
        ("SurvivorshipScore - no override FORCED_SKIP",    test_scoring_survivorship_does_not_override_saturation_skip),
        ("SurvivorshipScore - breakdown always present",   test_scoring_breakdown_has_survivorship_fields),
        ("SurvivorshipScore - proven > new progression",   test_scoring_proven_beats_new_when_equal_fundamentals),
        # ── Mejora 3: DB Indices ──────────────────────────────────────────
        ("DB Indices - SCHEMA has all new indices",        test_db_schema_has_new_indices),
        ("DB Indices - all use IF NOT EXISTS",             test_db_schema_indices_are_idempotent),
        ("DB Indices - survivorship constants",            test_survivorship_constants_importable),
        ("DB Indices - ScoreRequest has surv fields",      test_score_request_has_survivorship_fields),
        # ── Integration ───────────────────────────────────────────────────
        ("Integration - Full V4.4 pipeline",               test_full_v44_integration),
    ]

    print("\n" + "=" * 70)
    print("  TESTS V4.4: NicheClusterer + Survivorship + DB Indices")
    print("=" * 70)

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            print(f"\n▶ {name}")
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 70)
    print(f"  RESULTS: {passed}/{len(tests)} passed | {failed} failed")
    print("=" * 70)
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
