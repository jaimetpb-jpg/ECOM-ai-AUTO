"""
intelligence/niche_clusterer.py — Automatic Niche Clusterer V4.4

Assigns cluster_id to products automatically using K-Means on 4 features:
  [price_band, roas, competition_inv, demand]

This feeds directly into HierarchicalBayesianAllocator.register_campaign()
eliminating the need to assign cluster IDs manually.

Integration flow:
  1. NicheClusterer.fit(historical_data)         — train on past products
  2. NicheClusterer.assign_cluster(score_input)  — get cluster_id for new product
  3. HierarchicalBayesianAllocator.register_campaign(id, cluster_id)

Without this module, cluster_id was hardcoded per product. With it,
the system groups products automatically by market similarity and the
Bayesian allocator inherits the right priors from day 1.

Design:
  - No sklearn dependency in production path → pure Python K-Means
  - Optional sklearn fast-path when available (training only)
  - Thread-safe: fit() and assign_cluster() use RLock
  - Persists centroid state to JSON (hot-reload without refit)
  - Deterministic: fixed random_state=42 for reproducibility

Cluster naming convention:
  cluster_{price_tier}_{competition_tier}
  e.g.  cluster_premium_low_comp
        cluster_budget_high_comp
        cluster_mid_moderate_comp

Usage:
    from intelligence.niche_clusterer import NicheClusterer
    from scoring.engine import ScoreInput

    clusterer = NicheClusterer(n_clusters=5)

    # Train on historical opportunities from DB
    clusterer.fit([
        {"price_usd": 59.99, "roas": 3.2, "competition_inv": 70, "demand": 80},
        {"price_usd": 19.99, "roas": 1.8, "competition_inv": 30, "demand": 60},
        ...
    ])

    # Auto-assign cluster for new product at scoring time
    cluster_id = clusterer.assign_cluster_from_score(score_input)
    allocator.register_campaign(campaign_id, cluster_id)
"""

import math
import json
import asyncio
import random
import logging
from dataclasses import dataclass, field
from threading import RLock
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

# V5.0: Feature Store for caching
from shared.feature_store import get_feature_store

logger = logging.getLogger(__name__)


# ─── Feature engineering ──────────────────────────────────────────────────────

def _price_band(price_usd: float) -> float:
    """
    Normalize price into 0-100 band.
    < $15   → budget (0–30)
    $15–50  → mid    (30–60)
    $50–100 → premium (60–80)
    > $100  → luxury  (80–100)
    """
    if price_usd <= 0:
        return 0.0
    # log scale so $100 ≠ 10× $10 in cluster space
    return min(100.0, math.log1p(price_usd) / math.log1p(200) * 100.0)


def _normalize(value: float, min_val: float = 0.0, max_val: float = 100.0) -> float:
    """Clamp and normalize any value to [0, 1] for distance calculations."""
    span = max_val - min_val
    if span <= 0:
        return 0.0
    return max(0.0, min(1.0, (value - min_val) / span))


def extract_features(
    price_usd: float,
    roas: float,
    competition_inv: float,
    demand: float,
) -> List[float]:
    """
    Convert raw product metrics into a normalized 4-dimensional feature vector.

    All dimensions are mapped to [0, 1] for equal-weight Euclidean distance.

    Dimensions:
      [0] price_band      — log-normalized price tier
      [1] roas            — profitability signal (cap at 10x)
      [2] competition_inv — how open the market is (100 = no competition)
      [3] demand          — organic search + trend signal
    """
    return [
        _normalize(_price_band(price_usd), 0.0, 100.0),
        _normalize(min(roas, 10.0), 0.0, 10.0),
        _normalize(competition_inv, 0.0, 100.0),
        _normalize(demand, 0.0, 100.0),
    ]


# ─── Pure-Python K-Means ──────────────────────────────────────────────────────

def _euclidean(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _centroid(points: List[List[float]]) -> List[float]:
    if not points:
        return [0.0] * 4
    n = len(points)
    return [sum(p[i] for p in points) / n for i in range(len(points[0]))]


def _kmeans_fit(
    data: List[List[float]],
    k: int,
    max_iter: int = 100,
    tol: float = 1e-6,
    random_state: int = 42,
) -> List[List[float]]:
    """
    Pure-Python K-Means. Returns list of k centroids.

    Uses K-Means++ initialization for better convergence:
      - First centroid: random
      - Each subsequent: probability ∝ D²(x, nearest centroid)
    """
    rng = random.Random(random_state)
    n = len(data)

    if n < k:
        # Fewer points than clusters: duplicate data points as centroids
        centroids = [list(data[i % n]) for i in range(k)]
        return centroids

    # K-Means++ initialization
    first_idx = rng.randint(0, n - 1)
    centroids = [list(data[first_idx])]

    for _ in range(1, k):
        # Squared distances from each point to its nearest centroid
        dists = []
        for point in data:
            d = min(_euclidean(point, c) ** 2 for c in centroids)
            dists.append(d)
        total = sum(dists)
        if total <= 0:
            # All points coincide — pick random
            centroids.append(list(data[rng.randint(0, n - 1)]))
            continue
        # Sample proportionally to D²
        threshold = rng.random() * total
        cumulative = 0.0
        chosen = data[0]
        for point, d in zip(data, dists):
            cumulative += d
            if cumulative >= threshold:
                chosen = point
                break
        centroids.append(list(chosen))

    # Lloyd iterations
    for iteration in range(max_iter):
        # Assignment step
        assignments = []
        for point in data:
            distances = [_euclidean(point, c) for c in centroids]
            assignments.append(distances.index(min(distances)))

        # Update step
        new_centroids = []
        max_shift = 0.0
        for k_idx in range(k):
            cluster_points = [data[i] for i, a in enumerate(assignments) if a == k_idx]
            if not cluster_points:
                # Empty cluster: reinitialize to random point
                new_c = list(data[rng.randint(0, n - 1)])
            else:
                new_c = _centroid(cluster_points)
            shift = _euclidean(centroids[k_idx], new_c) if centroids else 0.0
            max_shift = max(max_shift, shift)
            new_centroids.append(new_c)

        centroids = new_centroids

        if max_shift < tol:
            logger.debug("K-Means converged at iteration %d", iteration + 1)
            break

    return centroids


# ─── Cluster naming ───────────────────────────────────────────────────────────

def _describe_cluster(centroid: List[float], cluster_idx: int) -> str:
    """
    Generate a human-readable cluster name from its centroid coordinates.

    centroid = [price_norm, roas_norm, comp_inv_norm, demand_norm]
    """
    price_n, roas_n, comp_n, demand_n = centroid

    # Price tier
    if price_n >= 0.65:
        price_label = "luxury"
    elif price_n >= 0.40:
        price_label = "premium"
    elif price_n >= 0.20:
        price_label = "mid"
    else:
        price_label = "budget"

    # Competition tier
    if comp_n >= 0.65:
        comp_label = "low_comp"
    elif comp_n >= 0.35:
        comp_label = "moderate_comp"
    else:
        comp_label = "high_comp"

    # Profitability qualifier
    if roas_n >= 0.60:
        prof_label = "_high_roas"
    elif roas_n <= 0.20:
        prof_label = "_low_roas"
    else:
        prof_label = ""

    return f"cluster_{price_label}_{comp_label}{prof_label}_{cluster_idx}"


# ─── Main class ───────────────────────────────────────────────────────────────

@dataclass
class ClusterInfo:
    """Metadata for a trained cluster."""
    cluster_idx:    int
    cluster_id:     str           # human-readable name
    centroid:       List[float]   # 4D normalized centroid
    n_members:      int = 0       # products assigned during training
    avg_roas:       float = 0.0
    avg_demand:     float = 0.0
    avg_comp_inv:   float = 0.0
    created_at:     str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return {
            "cluster_idx":  self.cluster_idx,
            "cluster_id":   self.cluster_id,
            "centroid":     [round(c, 6) for c in self.centroid],
            "n_members":    self.n_members,
            "avg_roas":     round(self.avg_roas, 4),
            "avg_demand":   round(self.avg_demand, 4),
            "avg_comp_inv": round(self.avg_comp_inv, 4),
            "created_at":   self.created_at,
        }


class NicheClusterer:
    """
    Automatic product-niche clustering for HierarchicalBayesianAllocator.

    Workflow:
      1. fit(products)           — train K-Means on historical product data
      2. assign_cluster(...)     — classify a new product into a cluster
      3. save_state(path)        — persist centroids to JSON
      4. load_state(path)        — reload without retraining

    Thread-safe: all public methods use RLock.

    Example:
        clusterer = NicheClusterer(n_clusters=5)
        clusterer.fit([
            {"price_usd": 49.99, "roas": 3.1, "competition_inv": 65, "demand": 78},
            {"price_usd": 19.99, "roas": 1.9, "competition_inv": 40, "demand": 55},
            ...
        ])
        cluster_id = clusterer.assign_cluster(
            price_usd=39.99, roas=2.5, competition_inv=60, demand=70
        )
        # → "cluster_mid_moderate_comp_2"
    """

    def __init__(
        self,
        n_clusters: int = 5,
        max_iter:   int = 100,
        tol:        float = 1e-6,
        random_state: int = 42,
    ):
        if n_clusters < 1:
            raise ValueError("n_clusters must be >= 1")
        self.n_clusters   = n_clusters
        self.max_iter     = max_iter
        self.tol          = tol
        self.random_state = random_state

        self._clusters: Dict[int, ClusterInfo] = {}
        self._fitted:   bool = False
        self._lock:     RLock = RLock()
        self._n_training_samples: int = 0

        logger.info(
            "NicheClusterer initialized | n_clusters=%d | random_state=%d",
            n_clusters, random_state
        )

    # ── Training ──────────────────────────────────────────────────────────────

    def fit(self, products: List[dict]) -> "NicheClusterer":
        """
        Train K-Means on a list of product dictionaries.

        Each dict must have:
          price_usd (float)       — product price in USD  [required]
          competition_inv (float) — 100 = no competition  [required]
          demand (float)          — demand score 0-100    [required]
          roas (float)            — observed ROAS         [optional, default 2.5]

        Returns self for chaining.

        Raises:
          ValueError if products list is empty.
        """
        if not products:
            raise ValueError("fit() requires at least 1 product")

        with self._lock:
            # Build feature matrix
            feature_matrix: List[List[float]] = []
            for p in products:
                vec = extract_features(
                    price_usd       = float(p.get("price_usd", 0.0)),
                    roas            = float(p.get("roas", 2.5)),
                    competition_inv = float(p.get("competition_inv", 50.0)),
                    demand          = float(p.get("demand", 50.0)),
                )
                feature_matrix.append(vec)

            k = min(self.n_clusters, len(feature_matrix))

            # Try sklearn fast-path first; fall back to pure-Python
            centroids = self._fit_with_sklearn_or_fallback(feature_matrix, k)

            # Assign training points to clusters for metadata
            assignments = []
            for vec in feature_matrix:
                dists = [_euclidean(vec, c) for c in centroids]
                assignments.append(dists.index(min(dists)))

            # Build ClusterInfo with aggregate stats from training data
            cluster_to_products: Dict[int, List[dict]] = {i: [] for i in range(k)}
            for i, product in enumerate(products):
                cluster_to_products[assignments[i]].append(product)

            self._clusters = {}
            for idx in range(k):
                members = cluster_to_products[idx]
                if members:
                    avg_roas     = sum(float(p.get("roas", 2.5)) for p in members) / len(members)
                    avg_demand   = sum(float(p.get("demand", 50.0)) for p in members) / len(members)
                    avg_comp_inv = sum(float(p.get("competition_inv", 50.0)) for p in members) / len(members)
                else:
                    avg_roas = avg_demand = avg_comp_inv = 0.0

                cluster_id = _describe_cluster(centroids[idx], idx)
                self._clusters[idx] = ClusterInfo(
                    cluster_idx  = idx,
                    cluster_id   = cluster_id,
                    centroid     = centroids[idx],
                    n_members    = len(members),
                    avg_roas     = avg_roas,
                    avg_demand   = avg_demand,
                    avg_comp_inv = avg_comp_inv,
                )
                logger.info(
                    "Cluster %d | id=%s | members=%d | avg_roas=%.2f | avg_demand=%.1f | avg_comp=%.1f",
                    idx, cluster_id, len(members), avg_roas, avg_demand, avg_comp_inv
                )

            self._fitted = True
            self._n_training_samples = len(products)
            
            # Explicitly clear feature_matrix to free memory (important for large datasets)
            del feature_matrix
            del assignments
            del cluster_to_products
            
            logger.info(
                "NicheClusterer fitted | n_products=%d | k=%d",
                len(products), k
            )
        return self

    def _fit_with_sklearn_or_fallback(
        self, feature_matrix: List[List[float]], k: int
    ) -> List[List[float]]:
        """
        Try sklearn KMeans (faster for large datasets), fall back to pure Python.
        Returns list of k centroids as List[List[float]].
        """
        try:
            from sklearn.cluster import KMeans as _SKLearnKMeans
            import numpy as np
            km = _SKLearnKMeans(
                n_clusters=k,
                max_iter=self.max_iter,
                tol=self.tol,
                random_state=self.random_state,
                n_init=10,
            )
            km.fit(np.array(feature_matrix))
            centroids = km.cluster_centers_.tolist()
            logger.debug("K-Means fitted with sklearn | k=%d", k)
            return centroids
        except ImportError:
            logger.debug("sklearn not available, using pure-Python K-Means")
        except Exception as e:
            logger.warning("sklearn KMeans failed (%s), falling back to pure Python", e)

        return _kmeans_fit(
            feature_matrix, k,
            max_iter=self.max_iter,
            tol=self.tol,
            random_state=self.random_state,
        )

    # ── Assignment ────────────────────────────────────────────────────────────

    def assign_cluster(
        self,
        price_usd:       float,
        roas:            float,
        competition_inv: float,
        demand:          float,
        product_id:      Optional[str] = None,  # V5.0: For caching
    ) -> str:
        """
        Classify a product into a cluster.
        
        V5.0 Enhancement: Uses Feature Store to cache computed features.
        If product_id is provided, features are cached for reuse.

        Returns the cluster_id string for use in HierarchicalBayesianAllocator.

        Raises:
          RuntimeError if fit() has not been called yet.
        """
        if not self._fitted:
            raise RuntimeError(
                "NicheClusterer.fit() must be called before assign_cluster(). "
                "Pass at least one historical product or load_state() from disk."
            )

        with self._lock:
            # V5.0: Use Feature Store for caching if product_id provided
            if product_id:
                feature_store = get_feature_store()
                # Try to get cached vector
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                
                cached = loop.run_until_complete(
                    feature_store.get("niche_vector", product_id)
                )
                
                if cached:
                    vec = cached.get("features")
                    logger.debug(f"niche_vector_cache_hit product={product_id}")
                else:
                    vec = extract_features(price_usd, roas, competition_inv, demand)
                    # Cache for future use
                    loop.run_until_complete(
                        feature_store.set("niche_vector", product_id, {"features": vec})
                    )
                    logger.debug(f"niche_vector_cached product={product_id}")
            else:
                # No product_id - compute without caching
                vec = extract_features(price_usd, roas, competition_inv, demand)
            
            best_idx = 0
            best_dist = float("inf")

            for idx, info in self._clusters.items():
                d = _euclidean(vec, info.centroid)
                if d < best_dist:
                    best_dist = d
                    best_idx = idx

            cluster_id = self._clusters[best_idx].cluster_id
            logger.debug(
                "assign_cluster | price=%.2f roas=%.2f comp=%.1f demand=%.1f "
                "→ cluster=%s (dist=%.4f)",
                price_usd, roas, competition_inv, demand, cluster_id, best_dist
            )
            return cluster_id

    def assign_cluster_from_score(self, score_input: object) -> str:
        """
        Convenience wrapper — accepts a ScoreInput dataclass directly.

        price_usd will be 0.0 if not set on the ScoreInput (uses price_band=budget tier).
        roas defaults to 2.5 if product hasn't been tested yet.

        Args:
            score_input: ScoreInput instance from scoring.engine

        Returns:
            cluster_id string
        """
        price_usd = getattr(score_input, "price_usd", None) or 0.0
        roas      = getattr(score_input, "empirical_roas", None) or 2.5
        return self.assign_cluster(
            price_usd       = float(price_usd),
            roas            = float(roas),
            competition_inv = float(getattr(score_input, "competition_inv", 50.0)),
            demand          = float(getattr(score_input, "demand", 50.0)),
        )

    # ── Introspection ─────────────────────────────────────────────────────────

    def get_cluster_info(self, cluster_id: str) -> Optional[ClusterInfo]:
        """Return ClusterInfo by cluster_id string, or None if not found."""
        with self._lock:
            for info in self._clusters.values():
                if info.cluster_id == cluster_id:
                    return info
        return None

    def get_all_cluster_ids(self) -> List[str]:
        """Return all cluster_id strings for registering with HierarchicalBayesian."""
        with self._lock:
            return [info.cluster_id for info in self._clusters.values()]

    def get_summary(self) -> dict:
        """Return JSON-serializable summary for logging / Metabase."""
        with self._lock:
            return {
                "fitted":             self._fitted,
                "n_clusters":         len(self._clusters),
                "n_training_samples": self._n_training_samples,
                "clusters":           [info.to_dict() for info in self._clusters.values()],
            }

    # ── Persistence ───────────────────────────────────────────────────────────

    def save_state(self, path: str) -> None:
        """
        Persist centroid state to JSON file.
        Call after fit() to avoid retraining on every restart.
        """
        with self._lock:
            state = {
                "n_clusters":             self.n_clusters,
                "n_training_samples":     self._n_training_samples,
                "fitted":                 self._fitted,
                "saved_at":               datetime.now(timezone.utc).isoformat(),
                "clusters": {
                    str(idx): info.to_dict()
                    for idx, info in self._clusters.items()
                },
            }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        logger.info("NicheClusterer state saved to %s", path)

    def load_state(self, path: str) -> "NicheClusterer":
        """
        Load centroid state from JSON — no need to call fit() again.

        Returns self for chaining.
        Raises: FileNotFoundError, json.JSONDecodeError on bad file.
        """
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)

        with self._lock:
            self._n_training_samples = state.get("n_training_samples", 0)
            self._fitted = state.get("fitted", False)
            self._clusters = {}
            for idx_str, info_dict in state.get("clusters", {}).items():
                idx = int(idx_str)
                self._clusters[idx] = ClusterInfo(
                    cluster_idx  = info_dict["cluster_idx"],
                    cluster_id   = info_dict["cluster_id"],
                    centroid     = info_dict["centroid"],
                    n_members    = info_dict.get("n_members", 0),
                    avg_roas     = info_dict.get("avg_roas", 0.0),
                    avg_demand   = info_dict.get("avg_demand", 0.0),
                    avg_comp_inv = info_dict.get("avg_comp_inv", 0.0),
                    created_at   = info_dict.get("created_at", ""),
                )
        logger.info(
            "NicheClusterer state loaded from %s | clusters=%d",
            path, len(self._clusters)
        )
        return self
