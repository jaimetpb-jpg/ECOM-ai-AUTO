"""
intelligence/hierarchical_bayesian.py — Hierarchical Bayesian Model V1.0

Arquitectura de 3 niveles:
  NIVEL 1 — Global Prior:  distribución global sobre todos los tenants/clusters
  NIVEL 2 — Cluster Prior: distribución por cluster (nicho × geo × rango de precio)
  NIVEL 3 — Campaign Posterior: posterior individual por campaña

Math:
  Global:   μ_global ~ Normal(μ₀, σ₀²)
  Cluster:  μ_cluster | μ_global ~ Normal(μ_global, σ_cluster²)
  Campaign: θ_campaign | μ_cluster ~ Beta(α_cluster, β_cluster)
  Update:   Bayesian conjugate update en cada nivel

Ventajas sobre Thompson Sampling plano:
  - Campañas nuevas heredan prior del cluster (no ciegas)
  - Clusters nuevos heredan prior global (no ciegos)
  - Efecto red: más tenants → mejores priors para todos
  - Reducción de drawdown via regularización jerárquica

Uso:
  from intelligence.hierarchical_bayesian import HierarchicalBayesianAllocator
  allocator = HierarchicalBayesianAllocator()
  allocator.register_cluster("skincare_us_premium")
  allocator.update_campaign("c1", "skincare_us_premium", impressions=1000, clicks=45)
  allocation = allocator.allocate(["c1","c2","c3"], "skincare_us_premium", budget=5000)
"""

import math
import random
import logging
import json
from dataclasses import dataclass, field
from threading import Lock, RLock
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ─── Utilidades estadísticas ──────────────────────────────────────────────────

def beta_sample(alpha: float, beta: float, n: int = 1) -> List[float]:
    """Sample from Beta(alpha, beta) usando gamma ratio."""
    samples = []
    for _ in range(n):
        g1 = random.gammavariate(max(alpha, 1e-6), 1.0)
        g2 = random.gammavariate(max(beta, 1e-6), 1.0)
        total = g1 + g2
        samples.append(g1 / total if total > 1e-12 else 0.5)
    return samples


def normal_sample(mu: float, sigma: float) -> float:
    """Sample from Normal(mu, sigma)."""
    return random.gauss(mu, max(sigma, 1e-9))


def stable_softmax(scores: List[float], tau: float = 0.5) -> List[float]:
    """Softmax numéricamente estable con temperatura τ."""
    if not scores:
        return []
    n = len(scores)
    tau = max(1e-6, tau)
    scaled = [s / tau for s in scores]
    if all(s <= 0 for s in scaled):
        return [1.0 / n] * n
    max_s = max(scaled)
    exp_s = [math.exp(min(700.0, s - max_s)) for s in scaled]
    total = sum(exp_s) + 1e-12
    return [e / total for e in exp_s]


# ─── Estructuras de datos ─────────────────────────────────────────────────────

@dataclass
class GlobalPrior:
    """
    Prior global compartido entre todos los clusters.
    Representa el 'conocimiento del mercado' acumulado.
    """
    mu_ctr: float = 0.02          # CTR esperado global (2%)
    sigma_ctr: float = 0.015      # Incertidumbre global
    mu_cr: float = 0.03           # Conversion rate global (3%)
    sigma_cr: float = 0.02
    mu_roas: float = 2.5          # ROAS esperado global
    sigma_roas: float = 1.0
    n_observations: int = 0       # Total observaciones para credibilidad
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def credibility(self) -> float:
        """Qué tanto peso darle al prior global vs. datos locales."""
        # Aumenta con observaciones, max ~0.3 (30% peso global)
        return min(0.30, self.n_observations / (self.n_observations + 500))

    def to_beta_params(self) -> Tuple[float, float]:
        """Convierte mu/sigma a parámetros Beta para CTR prior."""
        mu = max(1e-4, min(0.9999, self.mu_ctr))
        # Method of moments: α = mu*(mu*(1-mu)/sigma² - 1), β = (1-mu)*...
        var = max(1e-8, self.sigma_ctr ** 2)
        nu = mu * (1 - mu) / var - 1
        nu = max(0.1, nu)
        return mu * nu, (1 - mu) * nu


@dataclass
class ClusterPrior:
    """
    Prior específico por cluster.
    Cluster = nicho × geo × rango_precio.
    Ejemplo: 'skincare_us_70plus'
    """
    cluster_id: str
    # Prior heredado del global, refinado con datos del cluster
    alpha_ctr: float = 1.0       # Parámetro Beta para CTR
    beta_ctr: float = 9.0        # Default: espera CTR ~10%
    mu_roas: float = 2.5
    sigma_roas: float = 1.0
    n_campaigns: int = 0         # Campañas activas en este cluster
    n_observations: int = 0      # Total observaciones del cluster
    total_clicks: int = 0
    total_impressions: int = 0
    lock: Lock = field(default_factory=Lock, repr=False, compare=False)
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def expected_ctr(self) -> float:
        return self.alpha_ctr / (self.alpha_ctr + self.beta_ctr)

    def update_from_global(self, global_prior: GlobalPrior, weight: float = 0.1):
        """
        Actualiza el prior del cluster incorporando información global.
        weight: qué fracción del global incorporar (shrinkage).
        """
        with self.lock:
            g_alpha, g_beta = global_prior.to_beta_params()
            self.alpha_ctr = (1 - weight) * self.alpha_ctr + weight * g_alpha
            self.beta_ctr = (1 - weight) * self.beta_ctr + weight * g_beta
            self.mu_roas = (1 - weight) * self.mu_roas + weight * global_prior.mu_roas

    def update_from_observations(self, clicks: int, impressions: int, roas: Optional[float] = None):
        """Actualiza el prior del cluster con nuevas observaciones."""
        with self.lock:
            self.total_clicks += clicks
            self.total_impressions += impressions
            self.n_observations += impressions
            # Conjugate update Beta: alpha += clicks, beta += (impressions - clicks)
            self.alpha_ctr += clicks
            self.beta_ctr += max(0, impressions - clicks)
            if roas is not None:
                # Online update de mu_roas (Welford)
                n = self.n_observations
                delta = roas - self.mu_roas
                self.mu_roas += delta / max(1, n)
            self.last_updated = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "alpha_ctr": self.alpha_ctr,
            "beta_ctr": self.beta_ctr,
            "expected_ctr": self.expected_ctr(),
            "mu_roas": self.mu_roas,
            "n_campaigns": self.n_campaigns,
            "n_observations": self.n_observations,
        }


@dataclass
class CampaignState:
    """
    Estado posterior individual de una campaña.
    Hereda prior del cluster, se actualiza con datos propios.
    """
    campaign_id: str
    cluster_id: str
    # Posterior Beta para CTR
    alpha: float = 1.0
    beta: float = 9.0
    # Métricas financieras
    impressions: int = 0
    clicks: int = 0
    conversions: int = 0
    revenue: float = 0.0
    spend: float = 0.0
    # Configuración de riesgo
    min_budget: float = 10.0
    max_budget: float = 5000.0
    prior_alpha: float = 1.0     # Guardamos prior original para reset
    prior_beta: float = 9.0
    lock: Lock = field(default_factory=Lock, repr=False, compare=False)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def update(self, impressions: int, clicks: int,
               conversions: int = 0, revenue: float = 0.0, spend: float = 0.0):
        """Thread-safe update del posterior."""
        with self.lock:
            self.impressions += impressions
            self.clicks += clicks
            self.conversions += conversions
            self.revenue += revenue
            self.spend += spend
            # Conjugate Bayesian update
            self.alpha = self.prior_alpha + self.clicks
            self.beta = self.prior_beta + max(0, self.impressions - self.clicks)
            self.last_updated = datetime.now(timezone.utc).isoformat()

    def sample_ctr(self, n_samples: int = 50) -> float:
        """Muestrea CTR del posterior Beta."""
        samples = beta_sample(self.alpha, self.beta, n_samples)
        return sum(samples) / len(samples)

    def roas(self) -> float:
        """ROAS actual. Retorna 0 si no hay spend."""
        return self.revenue / max(self.spend, 1e-6)

    def credibility(self) -> float:
        """Confianza en los datos propios vs. prior del cluster."""
        return min(0.95, self.impressions / (self.impressions + 1000))

    def to_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "cluster_id": self.cluster_id,
            "alpha": self.alpha,
            "beta": self.beta,
            "expected_ctr": self.alpha / (self.alpha + self.beta),
            "impressions": self.impressions,
            "clicks": self.clicks,
            "roas": self.roas(),
            "credibility": self.credibility(),
        }


# ─── Allocator principal ──────────────────────────────────────────────────────

class HierarchicalBayesianAllocator:
    """
    Motor de asignación de presupuesto Bayesiano Jerárquico.

    3 niveles de inferencia:
      Global → Cluster → Campaign

    El prior de cada campaña nueva hereda del cluster.
    El prior de cada cluster hereda del global.
    Todos se actualizan en tiempo real con datos observados.

    Ejemplo de uso:
        allocator = HierarchicalBayesianAllocator()

        # Registrar un cluster (nicho)
        allocator.register_cluster("skincare_us_premium", alpha_ctr=2.0, beta_ctr=18.0)

        # Registrar campañas en ese cluster
        allocator.register_campaign("camp_001", "skincare_us_premium")
        allocator.register_campaign("camp_002", "skincare_us_premium")

        # Actualizar con datos observados
        allocator.update_campaign("camp_001", impressions=5000, clicks=120, roas=3.2)

        # Asignar presupuesto
        allocation = allocator.allocate(
            campaign_ids=["camp_001", "camp_002"],
            cluster_id="skincare_us_premium",
            total_budget=2000.0
        )
    """

    def __init__(
        self,
        n_samples: int = 100,
        tau: float = 0.5,
        global_update_interval: int = 50,
    ):
        self.n_samples = n_samples
        self.tau = tau
        self.global_update_interval = global_update_interval

        self.global_prior = GlobalPrior()
        self.clusters: Dict[str, ClusterPrior] = {}
        self.campaigns: Dict[str, CampaignState] = {}
        self._global_lock = RLock()
        self._update_count = 0

        logger.info("HierarchicalBayesianAllocator initialized | tau=%.2f | n_samples=%d",
                    tau, n_samples)

    # ── Registro ──────────────────────────────────────────────────────────────

    def register_cluster(
        self,
        cluster_id: str,
        alpha_ctr: Optional[float] = None,
        beta_ctr: Optional[float] = None,
        mu_roas: Optional[float] = None,
    ) -> ClusterPrior:
        """
        Registra un nuevo cluster heredando del prior global.
        Si ya existe, lo retorna sin modificar.
        """
        if cluster_id in self.clusters:
            return self.clusters[cluster_id]

        # Prior inicial: hereda del global
        g_alpha, g_beta = self.global_prior.to_beta_params()
        cluster = ClusterPrior(
            cluster_id=cluster_id,
            alpha_ctr=alpha_ctr if alpha_ctr is not None else g_alpha,
            beta_ctr=beta_ctr if beta_ctr is not None else g_beta,
            mu_roas=mu_roas if mu_roas is not None else self.global_prior.mu_roas,
        )
        self.clusters[cluster_id] = cluster
        logger.info("Cluster registered | cluster=%s | expected_ctr=%.4f | mu_roas=%.2f",
                    cluster_id, cluster.expected_ctr(), cluster.mu_roas)
        return cluster

    def register_campaign(
        self,
        campaign_id: str,
        cluster_id: str,
        min_budget: float = 10.0,
        max_budget: float = 5000.0,
    ) -> CampaignState:
        """
        Registra una campaña nueva. Su prior heredará del cluster.
        Si el cluster no existe, se crea con defaults globales.
        """
        if campaign_id in self.campaigns:
            return self.campaigns[campaign_id]

        # Asegurar que el cluster existe
        if cluster_id not in self.clusters:
            self.register_cluster(cluster_id)

        cluster = self.clusters[cluster_id]
        campaign = CampaignState(
            campaign_id=campaign_id,
            cluster_id=cluster_id,
            alpha=cluster.alpha_ctr,
            beta=cluster.beta_ctr,
            prior_alpha=cluster.alpha_ctr,
            prior_beta=cluster.beta_ctr,
            min_budget=min_budget,
            max_budget=max_budget,
        )
        self.campaigns[campaign_id] = campaign
        with self.clusters[cluster_id].lock:
            self.clusters[cluster_id].n_campaigns += 1

        logger.info("Campaign registered | campaign=%s | cluster=%s | prior_ctr=%.4f",
                    campaign_id, cluster_id,
                    campaign.alpha / (campaign.alpha + campaign.beta))
        return campaign

    # ── Updates ───────────────────────────────────────────────────────────────

    def update_campaign(
        self,
        campaign_id: str,
        impressions: int,
        clicks: int,
        conversions: int = 0,
        revenue: float = 0.0,
        spend: float = 0.0,
    ):
        """
        Actualiza el posterior de una campaña y propaga hacia arriba:
        Campaign posterior → Cluster prior → Global prior.

        Esto es el 'efecto red': cada update mejora priors para todos.
        """
        if campaign_id not in self.campaigns:
            logger.warning("update_campaign: campaign %s not registered", campaign_id)
            return

        campaign = self.campaigns[campaign_id]
        campaign.update(impressions, clicks, conversions, revenue, spend)

        # Propagar al cluster
        cluster_id = campaign.cluster_id
        if cluster_id in self.clusters:
            roas = revenue / max(spend, 1e-6) if spend > 0 else None
            self.clusters[cluster_id].update_from_observations(clicks, impressions, roas)

        # Actualizar global periódicamente
        self._update_count += 1
        if self._update_count % self.global_update_interval == 0:
            self._update_global_prior()

        logger.debug(
            "Campaign updated | campaign=%s | impressions=%d | clicks=%d | "
            "posterior_ctr=%.4f | roas=%.2f",
            campaign_id, impressions, clicks,
            campaign.alpha / (campaign.alpha + campaign.beta),
            revenue / max(spend, 1e-6) if spend > 0 else 0,
        )

    def _update_global_prior(self):
        """
        Recalcula el prior global como media ponderada de todos los clusters.
        Implementa Empirical Bayes: los parámetros del prior se estiman de los datos.
        """
        with self._global_lock:
            clusters = list(self.clusters.values())
            if not clusters:
                return

            # Media ponderada por número de observaciones
            total_obs = sum(c.n_observations for c in clusters) + 1
            weights = [c.n_observations / total_obs for c in clusters]

            weighted_ctr = sum(w * c.expected_ctr() for w, c in zip(weights, clusters))
            weighted_roas = sum(w * c.mu_roas for w, c in zip(weights, clusters))

            self.global_prior.mu_ctr = weighted_ctr
            self.global_prior.mu_roas = weighted_roas
            self.global_prior.n_observations = int(total_obs)
            self.global_prior.last_updated = datetime.now(timezone.utc).isoformat()

            # Propagate global updates back to clusters (shrinkage)
            credibility = self.global_prior.credibility()
            if credibility > 0.05:
                for cluster in clusters:
                    cluster.update_from_global(self.global_prior, weight=credibility * 0.1)

            logger.debug(
                "Global prior updated | mu_ctr=%.4f | mu_roas=%.2f | "
                "n_obs=%d | credibility=%.3f",
                self.global_prior.mu_ctr, self.global_prior.mu_roas,
                self.global_prior.n_observations, credibility
            )

    # ── Asignación de presupuesto ─────────────────────────────────────────────

    def allocate(
        self,
        campaign_ids: List[str],
        cluster_id: str,
        total_budget: float,
        min_budget_per_campaign: float = 10.0,
    ) -> Dict[str, float]:
        """
        Asigna presupuesto entre campañas usando Thompson Sampling
        sobre posteriors Bayesianos jerárquicos.

        Returns:
            Dict[campaign_id → budget_asignado]
        """
        if not campaign_ids:
            logger.warning("allocate called with empty campaign list")
            return {}

        if total_budget <= 0:
            logger.warning("allocate called with budget=%.2f", total_budget)
            return {cid: 0.0 for cid in campaign_ids}

        # Asegurar que todas las campañas están registradas
        for cid in campaign_ids:
            if cid not in self.campaigns:
                self.register_campaign(cid, cluster_id)

        # Calcular scores via Thompson Sampling jerárquico
        scores = []
        for cid in campaign_ids:
            score = self._compute_hierarchical_score(cid, cluster_id)
            scores.append(score)
            logger.debug("Score | campaign=%s | score=%.6f", cid, score)

        # Stable softmax → probabilidades
        probs = stable_softmax(scores, tau=self.tau)

        # Asignar presupuesto con mínimos garantizados
        n = len(campaign_ids)
        min_total = min_budget_per_campaign * n
        if total_budget < min_total:
            # No hay suficiente para todos los mínimos
            allocation = {cid: total_budget / n for cid in campaign_ids}
            logger.warning("Budget %.2f < min total %.2f, distributing equally", total_budget, min_total)
            return allocation

        # Reservar mínimos, distribuir resto proporcionalmente
        remaining = total_budget - min_total
        allocation = {}
        for cid, prob in zip(campaign_ids, probs):
            allocation[cid] = min_budget_per_campaign + prob * remaining

        # Aplicar caps máximos
        for cid in campaign_ids:
            campaign = self.campaigns.get(cid)
            if campaign and allocation[cid] > campaign.max_budget:
                excess = allocation[cid] - campaign.max_budget
                allocation[cid] = campaign.max_budget
                # Redistribuir el exceso proporcionalmente entre el resto
                other_ids = [c for c in campaign_ids if c != cid]
                for other in other_ids:
                    allocation[other] += excess / max(len(other_ids), 1)

        total_allocated = sum(allocation.values())
        logger.info(
            "Budget allocated | cluster=%s | total=%.2f | campaigns=%d | "
            "max_share=%.1f%% | min_share=%.1f%%",
            cluster_id, total_allocated, n,
            max(probs) * 100, min(probs) * 100
        )
        return allocation

    def _compute_hierarchical_score(self, campaign_id: str, cluster_id: str) -> float:
        """
        Calcula el score de una campaña combinando:
        1. Thompson sample del posterior propio
        2. Prior del cluster (mayor peso si pocos datos propios)
        3. Ajuste por ROAS histórico
        """
        campaign = self.campaigns.get(campaign_id)
        if campaign is None:
            return 0.0

        # Credibilidad de los datos propios vs. prior del cluster
        own_cred = campaign.credibility()
        cluster_cred = 1.0 - own_cred

        # Sample del posterior propio
        own_ctr_sample = campaign.sample_ctr(self.n_samples)

        # Sample del prior del cluster
        cluster = self.clusters.get(cluster_id)
        if cluster:
            cluster_samples = beta_sample(cluster.alpha_ctr, cluster.beta_ctr, self.n_samples)
            cluster_ctr_sample = sum(cluster_samples) / len(cluster_samples)
            cluster_roas = cluster.mu_roas
        else:
            cluster_ctr_sample = self.global_prior.mu_ctr
            cluster_roas = self.global_prior.mu_roas

        # Score híbrido: combina datos propios con prior jerárquico
        blended_ctr = own_cred * own_ctr_sample + cluster_cred * cluster_ctr_sample

        # Bonus por ROAS histórico (si tenemos datos suficientes)
        roas_adjustment = 1.0
        if campaign.impressions > 500 and campaign.spend > 0:
            own_roas = campaign.roas()
            blended_roas = own_cred * own_roas + cluster_cred * cluster_roas
            roas_adjustment = max(0.1, blended_roas / max(cluster_roas, 1e-6))

        score = blended_ctr * roas_adjustment
        return max(0.0, score)

    # ── Diagnóstico y serialización ───────────────────────────────────────────

    def get_state_summary(self) -> dict:
        """Retorna resumen del estado del allocator para logging/Metabase."""
        return {
            "global_prior": {
                "mu_ctr": self.global_prior.mu_ctr,
                "mu_roas": self.global_prior.mu_roas,
                "n_observations": self.global_prior.n_observations,
                "credibility": self.global_prior.credibility(),
            },
            "clusters": {cid: c.to_dict() for cid, c in self.clusters.items()},
            "campaigns": {cid: c.to_dict() for cid, c in self.campaigns.items()},
            "total_clusters": len(self.clusters),
            "total_campaigns": len(self.campaigns),
        }

    def save_state(self, path: str):
        """Persiste el estado a JSON (para Supabase o disco)."""
        state = self.get_state_summary()
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
        logger.info("State saved to %s", path)

    def load_global_prior(self, mu_ctr: float, sigma_ctr: float,
                          mu_roas: float, n_obs: int = 0):
        """Carga prior global desde datos históricos externos."""
        self.global_prior.mu_ctr = mu_ctr
        self.global_prior.sigma_ctr = sigma_ctr
        self.global_prior.mu_roas = mu_roas
        self.global_prior.n_observations = n_obs
        logger.info("Global prior loaded | mu_ctr=%.4f | mu_roas=%.2f | n_obs=%d",
                    mu_ctr, mu_roas, n_obs)
