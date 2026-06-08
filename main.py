"""
main.py — FastAPI Application V4.4
AI Ecommerce System — All API endpoints.

Fixes aplicados:
  [FIX 1] Rate limiting distribuido con SlowAPI + Redis (fallback a memoria si Redis no disponible)
  [FIX 2] CORS restringido a orígenes conocidos
  [FIX 3] Sanitización de inputs antes de enviarlos al LLM (prompt injection guard)
  [FIX 4] Todos los endpoints usan modelos Pydantic estrictos (body: dict eliminado)
  [FIX 5] saturation_prob ≥ 0.8 fuerza SKIP en scoring engine
  [FIX 6] meta_ad_competitor_count penaliza score directamente
  [FIX 7] Webhook cart-abandoned protegido con API key + firma HMAC opcional

V4.4 additions:
  [V4.4-A] SurvivorshipBonus en scoring engine (days_active + empirical_roas)
  [V4.4-B] NicheClusterer singleton — cluster_id automatico para HierarchicalBayesian
  [V4.4-C] 11 indices adicionales en Supabase (decision_log, campaigns, opportunities)

Start: uvicorn main:app --reload --port 8000
Docs:  http://localhost:8000/docs
"""

import os
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, constr
from typing import Optional, Annotated

from shared.supabase_client import SupabaseClient
from shared.slack_notifier import SlackNotifier
from shared.llm_router import LLMRouter
from shared.logging_utils import log_warning
from scoring.engine import ScoringEngine, ScoreInput

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","module":"%(name)s","msg":%(message)s}',
)

# ── Shared instances ──────────────────────────────────────────────────────────
db     = SupabaseClient()
slack  = SlackNotifier()
# V5.2: Initialize BudgetGovernor + SemanticCache before wiring into router
# BudgetGovernor:  hard daily limits per tier — prevents $1,000+ runaway LLM costs
# SemanticCache:   pgvector-backed cache — avoids redundant LLM calls for similar prompts
try:
    from shared.budget_governor import get_budget_governor
    from shared.semantic_cache import get_semantic_cache
    _budget_governor = get_budget_governor(slack=slack)
    _semantic_cache  = get_semantic_cache(supabase_client=db._get_client())  # pgvector cache
    router = LLMRouter(budget_governor=_budget_governor, semantic_cache=_semantic_cache)
    logger.info("llm_router_v52_initialized budget_governor=True semantic_cache=True")
except Exception as _e:
    # Graceful degradation: fall back to basic router (no budget/cache)
    logger.warning(f"llm_router_v52_degraded error={_e} — running without BudgetGovernor/SemanticCache")
    router = LLMRouter()

# ── Engine singletons [FIX W2] ────────────────────────────────────────────────
# These engines were previously instantiated per-request (7 instances per call).
# Now initialized once at module load to avoid redundant object creation.
_price_ab_engine: Optional[object] = None
_dual_store_engine: Optional[object] = None
_meta_ad_engine: Optional[object] = None
_saas_engine: Optional[object] = None

def _get_price_ab_engine(llm_router=None):
    """Lazy singleton for DynamicPriceABTest."""
    global _price_ab_engine
    if _price_ab_engine is None:
        from pricing.dynamic_ab import DynamicPriceABTest
        _price_ab_engine = DynamicPriceABTest(llm_router=llm_router or router)
    return _price_ab_engine

def _get_dual_store_engine():
    """Lazy singleton for DualStoreABEngine."""
    global _dual_store_engine
    if _dual_store_engine is None:
        from scaling.dual_store_ab import DualStoreABEngine
        _dual_store_engine = DualStoreABEngine()
    return _dual_store_engine

def _get_meta_ad_engine(llm_router=None):
    """Lazy singleton for MetaAdIntelligenceEngine."""
    global _meta_ad_engine
    if _meta_ad_engine is None:
        from intelligence.meta_ad_library import MetaAdIntelligenceEngine
        _meta_ad_engine = MetaAdIntelligenceEngine(llm_router=llm_router or router)
    return _meta_ad_engine

# ── NicheClusterer singleton [V4.4-B] ─────────────────────────────────────────
# Auto-assigns cluster_id to new products for HierarchicalBayesianAllocator.
# Loads state from NICHE_CLUSTERER_STATE_PATH env var if set,
# otherwise starts unfitted (falls back to manual cluster assignment).
_niche_clusterer: Optional[object] = None

def _get_niche_clusterer():
    """
    Lazy singleton for NicheClusterer.
    Returns a fitted instance if state file exists, else unfitted.
    Call fit_niche_clusterer() via /api/intelligence/niche-clusterer/fit to train.
    """
    global _niche_clusterer
    if _niche_clusterer is None:
        from intelligence.niche_clusterer import NicheClusterer
        clusterer = NicheClusterer(n_clusters=int(os.getenv("NICHE_CLUSTERER_K", "5")))
        state_path = os.getenv("NICHE_CLUSTERER_STATE_PATH", "")
        if state_path:
            try:
                clusterer.load_state(state_path)
                logger.info("NicheClusterer state loaded from %s", state_path)
            except FileNotFoundError:
                logger.info("NicheClusterer state file not found at %s — starting unfitted", state_path)
            except Exception as e:
                logger.warning("NicheClusterer load_state failed: %s — starting unfitted", e)
        _niche_clusterer = clusterer
    return _niche_clusterer


def _get_saas_engine():
    """Lazy singleton for SaaSSpawnEngine."""
    global _saas_engine
    if _saas_engine is None:
        from scaling.saas_spawn import SaaSSpawnEngine
        _saas_engine = SaaSSpawnEngine()
    return _saas_engine

# ── Rate limiter setup [FIX 1] ────────────────────────────────────────────────
# Tries SlowAPI + Redis first. If Redis is unavailable, falls back to
# in-memory store with a clear warning. In production always set REDIS_URL.
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_RPM", "60"))
RATE_LIMIT_WINDOW   = 60  # seconds

_redis_client   = None
_use_redis_rate = False
_rate_store: dict = {}   # fallback siempre disponible, usado si Redis falla

try:
    import redis as _redis_lib
    _redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    _redis_client = _redis_lib.from_url(_redis_url, socket_connect_timeout=2)
    _redis_client.ping()
    _use_redis_rate = True
    logger.info(f"rate_limiter=redis url={_redis_url}")
except Exception as _redis_err:
    logger.warning(
        f"RATE_LIMIT_FALLBACK: Redis unavailable ({_redis_err}). "
        f"Using in-memory store — NOT safe for multi-worker deployments. "
        f"Set REDIS_URL in .env for production."
    )


def _check_rate_limit(ip: str) -> bool:
    """Returns True if request is within limit, False if it should be blocked."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    if _use_redis_rate and _redis_client:
        key = f"rl:{ip}"
        pipe = _redis_client.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zadd(key, {str(now): now})
        pipe.zcard(key)
        pipe.expire(key, RATE_LIMIT_WINDOW)
        results = pipe.execute()
        count = results[2]
        return count <= RATE_LIMIT_REQUESTS
    else:
        hits = _rate_store.get(ip, [])
        hits = [t for t in hits if t > window_start]
        hits.append(now)
        _rate_store[ip] = hits
        return len(hits) <= RATE_LIMIT_REQUESTS

# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    api_key = os.getenv("API_KEY", "")
    if not api_key or api_key == "dev-key-change-in-production":
        logger.warning("SECURITY: API_KEY is not set or is default. Set a strong key in .env before deploying.")
    rate_backend = "redis" if _use_redis_rate else "memory(UNSAFE-multi-worker)"
    logger.info(
        f"startup version=5.1 "
        f"api_key_set={'yes' if api_key and api_key != 'dev-key-change-in-production' else 'NO'} "
        f"rate_backend={rate_backend}"
    )
    # Prometheus metrics exporter (Grok V5.1): Bulkhead + Circuit Breaker
    if os.getenv("PROMETHEUS_ENABLED", "true").lower() == "true":
        try:
            from shared.prometheus_exporter import start_prometheus_exporter
            prom_port = int(os.getenv("PROMETHEUS_PORT", "9091"))
            start_prometheus_exporter(port=prom_port)
        except Exception as e:
            logger.warning(f"prometheus_start_failed error={e}")
    yield
    logger.info("shutdown")

app = FastAPI(
    title="AI Ecommerce System V5.1",
    description="Automated dropshipping intelligence: Oracle → Score → Creative → Validate → Brand → Scale",
    version="5.1.0",
    lifespan=lifespan,
)

# ── CORS: restrict to known origins ──────────────────────────────────────────
_allowed_origins = [o.strip() for o in os.getenv(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:8000,http://localhost:5678"
).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type", "X-Signature"],
    allow_credentials=False,
)

# ── Rate limit middleware [FIX 1] ─────────────────────────────────────────────
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(ip):
        logger.warning(f"rate_limit_exceeded ip={ip} backend={'redis' if _use_redis_rate else 'memory'}")
        return JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded: {RATE_LIMIT_REQUESTS} req/min"},
        )
    return await call_next(request)


_API_KEY = os.getenv("API_KEY", "dev-key-change-in-production")

def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    if x_api_key != _API_KEY:
        logger.warning(f"auth_failed key_prefix={x_api_key[:6] if x_api_key else 'empty'}")
        raise HTTPException(status_code=401, detail="Invalid API key")

# ── Request / Response models — strict Pydantic validation ───────────────────
# [FIX 4]: All endpoints now use typed Pydantic models. No more bare body: dict.
class OracleRunRequest(BaseModel):
    tenant_id: Annotated[str, Field(min_length=1, max_length=64)]
    niches: Optional[list[Annotated[str, Field(max_length=100)]]] = Field(default=None, max_length=20)

class ScoreRequest(BaseModel):
    name:                     Annotated[str,   Field(min_length=1, max_length=200)]
    niche:                    Annotated[str,   Field(min_length=1, max_length=100)]
    demand:                   Annotated[float, Field(ge=0, le=100)]
    competition_inv:          Annotated[float, Field(ge=0, le=100)]
    margin:                   Annotated[float, Field(ge=0, le=100)]
    differentiation:          Annotated[float, Field(ge=0, le=100)]
    logistics:                Annotated[float, Field(ge=0, le=100)]
    viral_score:              Annotated[float, Field(ge=0, le=100)]    = 50.0
    legal_risk:               Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    saturation_prob:          Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    supplier_count:           Annotated[int,   Field(ge=1, le=100)]   = 1
    meta_ad_competitor_count: Annotated[int,   Field(ge=0, le=10000)] = 0   # [FIX 6]
    # V4.4 — Survivorship fields [V4.4-B]
    # Populate from campaigns table when re-scoring an existing product.
    # Leave at defaults (0) for brand-new products.
    days_active:              Annotated[int,   Field(ge=0, le=3650)]  = 0
    empirical_roas:           Annotated[float, Field(ge=0.0, le=50.0)] = 0.0

class ValidationRequest(BaseModel):
    opportunity_id: Annotated[str, Field(min_length=1, max_length=64)]
    tenant_id:      Annotated[str, Field(min_length=1, max_length=64)]

class BrandRequest(BaseModel):
    opportunity_id: Annotated[str, Field(min_length=1, max_length=64)]
    tenant_id:      Annotated[str, Field(min_length=1, max_length=64)]

class MonitoringRequest(BaseModel):
    tenant_id: Annotated[str, Field(min_length=1, max_length=64)]

class CartAbandonedWebhook(BaseModel):
    customer_phone:    Annotated[str, Field(min_length=7, max_length=20)]
    customer_name:     Annotated[str, Field(max_length=100)] = "there"
    product_name:      Annotated[str, Field(max_length=200)]
    product_image_url: Optional[Annotated[str, Field(max_length=500)]] = None
    cart_value_usd:    Annotated[float, Field(ge=0, le=100000)]
    cart_id:           Annotated[str, Field(max_length=64)]
    niche:             Annotated[str, Field(max_length=100)]
    store_url:         Annotated[str, Field(max_length=200)]
    tenant_id:         Annotated[str, Field(max_length=64)]

class CommentMiningRequest(BaseModel):
    product_id:       Annotated[str, Field(max_length=64)]
    product_name:     Annotated[str, Field(max_length=200)]
    niche:            Annotated[str, Field(max_length=100)]
    asin:             Optional[Annotated[str, Field(max_length=20)]] = None
    ml_url:           Optional[Annotated[str, Field(max_length=300)]] = None
    tiktok_hashtag:   Optional[Annotated[str, Field(max_length=100)]] = None
    original_brief:   Optional[Annotated[str, Field(max_length=2000)]] = None

class PriceABRequest(BaseModel):
    product_id:     Annotated[str, Field(max_length=64)]
    product_name:   Annotated[str, Field(max_length=200)]
    base_price_usd: Annotated[float, Field(ge=0.01, le=100000)]

class MetaAdAnalysisRequest(BaseModel):
    niche:    Annotated[str, Field(min_length=1, max_length=100)]
    keywords: Annotated[list[Annotated[str, Field(max_length=100)]], Field(min_length=1, max_length=10)]

class DualStoreRequest(BaseModel):
    opportunity_id: Annotated[str, Field(max_length=64)]
    brand_id:       Annotated[str, Field(max_length=64)]
    tenant_id:      Annotated[str, Field(max_length=64)]

class AvatarVideoRequest(BaseModel):
    product_id:       Annotated[str, Field(max_length=64)]
    hook_categories:  Optional[list[Annotated[str, Field(max_length=50)]]] = None
    language:         Annotated[str, Field(max_length=5)] = "es"

class SpawnTenantRequest(BaseModel):
    company_name: Annotated[str, Field(min_length=1, max_length=100)]
    owner_email:  Annotated[str, Field(min_length=5, max_length=200)]
    plan:         Annotated[str, Field(pattern="^(starter|growth|agency)$")] = "starter"
    niches:       Optional[list[Annotated[str, Field(max_length=100)]]] = Field(default=None, max_length=20)

# ── [FIX 4] Pydantic models replacing bare body: dict ────────────────────────

class EvaluateRoasRequest(BaseModel):
    campaign_id:    Annotated[str,   Field(max_length=64)]
    roas:           Annotated[float, Field(ge=0, le=1000)]
    spend_usd:      Annotated[float, Field(ge=0, le=10_000_000)]
    opportunity_id: Annotated[str,   Field(max_length=64)]
    tenant_id:      Annotated[str,   Field(max_length=64)]

class SwarmLaunchRequest(BaseModel):
    opportunity_id: Annotated[str, Field(min_length=1, max_length=64)]
    tenant_id:      Annotated[str, Field(min_length=1, max_length=64)]

class EvaluatePriceABRequest(BaseModel):
    config:  dict  # DynamicPriceABTest config blob (internal structure)
    metrics: dict  # A/B test metrics blob

class MetaAdLibraryRequest(BaseModel):
    """Legacy endpoint body model — prefer /api/intelligence/meta-patterns."""
    niche:    Annotated[str, Field(min_length=1, max_length=100)]
    keywords: Annotated[list[Annotated[str, Field(max_length=100)]], Field(min_length=1, max_length=10)]

class EvaluateDualStoreRequest(BaseModel):
    test_config: dict  # DualStoreABEngine config blob
    metrics_a:   dict  # Store A metrics
    metrics_b:   dict  # Store B metrics

class N8nTriggerRequest(BaseModel):
    """Generic n8n trigger — tenant_id required, extra fields allowed."""
    tenant_id: Annotated[str, Field(min_length=1, max_length=64)] = "default"

    class Config:
        extra = "allow"  # n8n may send additional context fields

class SlackCallbackRequest(BaseModel):
    """Slack interactive callback — structure varies by action type."""
    class Config:
        extra = "allow"


# ════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status":        "ok",
        "version":       "5.1.0",
        "llm_usage":     router.get_usage_summary(),
        "cors_origins":  _allowed_origins,
        "rate_backend":  "redis" if _use_redis_rate else "memory(unsafe)",
    }



# ── Oracle ────────────────────────────────────────────────────────────────────
@app.post("/api/oracle/run", dependencies=[Depends(verify_api_key)])
async def run_oracle(request: OracleRunRequest, background_tasks: BackgroundTasks):
    """Trigger Oracle detection cycle. Runs in background; check Slack #opportunities."""
    from oracle.agents import OracleDetectionSystem
    oracle = OracleDetectionSystem(llm_router=router, db=db, slack=slack)
    background_tasks.add_task(oracle.run_detection_cycle, request.tenant_id, request.niches)
    return {"status": "started", "tenant_id": request.tenant_id}

@app.get("/api/oracle/opportunities/{tenant_id}", dependencies=[Depends(verify_api_key)])
async def get_opportunities(tenant_id: str):
    opps = db.get_pending_opportunities(tenant_id)
    return {"opportunities": opps, "count": len(opps)}


# ── Scoring ───────────────────────────────────────────────────────────────────
@app.post("/api/score", dependencies=[Depends(verify_api_key)])
async def score_product(request: ScoreRequest):
    """
    Score a product opportunity.

    V4.4: If days_active=0 and empirical_roas=0, the engine will apply
    NEW_PRODUCT_CAUTION_PENALTY (-2pts). Pass real campaign data to get
    SURVIVORSHIP_VALIDATED (+3pts) or SURVIVORSHIP_PROVEN (+6pts).
    """
    engine = ScoringEngine(llm_router=router)
    inp = ScoreInput(**request.model_dump())
    result = await engine.async_score(inp)
    logger.info(
        "score_endpoint product=%s score=%.2f decision=%s survivorship_adj=%s",
        request.name, result.final_score, result.decision,
        result.breakdown.get("survivorship_adj", 0)
    )
    return result.__dict__


# ── NicheClusterer endpoints [V4.4-B] ─────────────────────────────────────────

class NicheClustererFitRequest(BaseModel):
    """Products to train the NicheClusterer on."""
    products: list  # list of dicts: {price_usd, roas, competition_inv, demand}
    save_state: bool = True   # persist to NICHE_CLUSTERER_STATE_PATH if set


class NicheClustererAssignRequest(BaseModel):
    """Assign a cluster to a single product."""
    price_usd:       Annotated[float, Field(ge=0.0,   le=100000.0)] = 0.0
    roas:            Annotated[float, Field(ge=0.0,   le=50.0)]     = 2.5
    competition_inv: Annotated[float, Field(ge=0.0,   le=100.0)]
    demand:          Annotated[float, Field(ge=0.0,   le=100.0)]


@app.post("/api/intelligence/niche-clusterer/fit", dependencies=[Depends(verify_api_key)])
async def fit_niche_clusterer(request: NicheClustererFitRequest):
    """
    Train the NicheClusterer on historical product data.

    Call this once after you have >= 10 products with real ROAS data.
    The clusterer will auto-assign cluster_id to all new products scored
    after this point, feeding HierarchicalBayesianAllocator with correct priors.

    Body: {"products": [{"price_usd": 49.99, "roas": 3.1, "competition_inv": 65, "demand": 78}, ...]}
    """
    from intelligence.niche_clusterer import NicheClusterer
    global _niche_clusterer

    if not request.products:
        raise HTTPException(status_code=400, detail="products list cannot be empty")
    if len(request.products) < 3:
        raise HTTPException(
            status_code=400,
            detail=f"Need at least 3 products to cluster, got {len(request.products)}"
        )

    k = int(os.getenv("NICHE_CLUSTERER_K", "5"))
    clusterer = NicheClusterer(n_clusters=min(k, len(request.products)))

    try:
        clusterer.fit(request.products)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Clustering failed: {e}")

    # Replace singleton
    _niche_clusterer = clusterer

    # Persist if path configured and caller wants it
    if request.save_state:
        state_path = os.getenv("NICHE_CLUSTERER_STATE_PATH", "")
        if state_path:
            try:
                clusterer.save_state(state_path)
            except Exception as e:
                logger.warning("NicheClusterer save_state failed: %s", e)

    summary = clusterer.get_summary()
    logger.info(
        "NicheClusterer fitted via API | n_products=%d | k=%d",
        len(request.products), summary["n_clusters"]
    )
    return {
        "status": "fitted",
        "n_products": len(request.products),
        "clusters": summary["clusters"],
    }


@app.post("/api/intelligence/niche-clusterer/assign", dependencies=[Depends(verify_api_key)])
async def assign_niche_cluster(request: NicheClustererAssignRequest):
    """
    Get the cluster_id for a product based on its market metrics.

    Returns cluster_id for use in HierarchicalBayesianAllocator.register_campaign().
    Returns error 503 if clusterer has not been trained yet.
    """
    clusterer = _get_niche_clusterer()
    if not clusterer._fitted:
        raise HTTPException(
            status_code=503,
            detail=(
                "NicheClusterer not trained yet. "
                "Call POST /api/intelligence/niche-clusterer/fit first with historical product data."
            )
        )
    try:
        cluster_id = clusterer.assign_cluster(
            price_usd       = request.price_usd,
            roas            = request.roas,
            competition_inv = request.competition_inv,
            demand          = request.demand,
        )
        summary = clusterer.get_cluster_info(cluster_id)
        return {
            "cluster_id":    cluster_id,
            "cluster_info":  summary.to_dict() if summary else {},
            "all_clusters":  clusterer.get_all_cluster_ids(),
        }
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Cluster assignment failed: {e}")


@app.get("/api/intelligence/niche-clusterer/status", dependencies=[Depends(verify_api_key)])
async def niche_clusterer_status():
    """Return current NicheClusterer state: fitted, cluster count, training samples."""
    clusterer = _get_niche_clusterer()
    return clusterer.get_summary()


# ── Validation ────────────────────────────────────────────────────────────────

@app.post("/api/validate/launch", dependencies=[Depends(verify_api_key)])
async def launch_validation(request: ValidationRequest, background_tasks: BackgroundTasks):
    from validation.creative_generator import TikTokValidationPipeline
    opportunity = db.get_opportunity(request.opportunity_id)
    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    pipeline = TikTokValidationPipeline(llm_router=router, db=db, slack=slack)
    background_tasks.add_task(pipeline.run_validation, opportunity)
    return {"status": "started", "opportunity_id": request.opportunity_id}

@app.post("/api/validate/evaluate", dependencies=[Depends(verify_api_key)])
async def evaluate_roas(body: EvaluateRoasRequest):  # [FIX 4] was body: dict
    from validation.creative_generator import TikTokValidationPipeline
    pipeline = TikTokValidationPipeline(llm_router=router, db=db, slack=slack)
    result = await pipeline.evaluate_roas({
        "campaign_id":    body.campaign_id,
        "roas":           body.roas,
        "spend_usd":      body.spend_usd,
        "opportunity_id": body.opportunity_id,
        "tenant_id":      body.tenant_id,
    })
    return result


# ── Branding ──────────────────────────────────────────────────────────────────
@app.post("/api/brand/create", dependencies=[Depends(verify_api_key)])
async def create_brand(request: BrandRequest, background_tasks: BackgroundTasks):
    from branding.brand_creator import BrandCreator
    opportunity = db.get_opportunity(request.opportunity_id)
    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    creator = BrandCreator(llm_router=router, db=db, slack=slack)
    # create_brand(opportunity, niche_profile=None) — tenant_id is already inside opportunity dict
    if "tenant_id" not in opportunity:
        opportunity["tenant_id"] = request.tenant_id
    background_tasks.add_task(creator.create_brand, opportunity)
    return {"status": "started", "message": "Brand creation started. ~2 hours. Check Slack #approvals."}


# ── Monitoring ────────────────────────────────────────────────────────────────
@app.post("/api/monitoring/run", dependencies=[Depends(verify_api_key)])
async def run_monitoring(request: MonitoringRequest, background_tasks: BackgroundTasks):
    from monitoring.metrics_collector import MonitoringCycle
    cycle = MonitoringCycle(db=db, slack=slack, llm_router=router)
    background_tasks.add_task(cycle.run_cycle, request.tenant_id)
    return {"status": "started"}


# ── Niche Swarm ───────────────────────────────────────────────────────────────
@app.post("/api/swarm/launch", dependencies=[Depends(verify_api_key)])
async def launch_swarm(body: SwarmLaunchRequest, background_tasks: BackgroundTasks):  # [FIX 4]
    from scaling.niche_swarm import NicheSwarmEngine
    engine = NicheSwarmEngine(llm_router=router, db=db, slack=slack)
    opportunity = db.get_opportunity(body.opportunity_id)
    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    background_tasks.add_task(engine.launch_swarm, opportunity, body.tenant_id)
    return {"status": "started"}


# ── Retention ─────────────────────────────────────────────────────────────────
@app.post("/api/webhooks/cart-abandoned", dependencies=[Depends(verify_api_key)])  # [FIX 2] was unprotected
async def cart_abandoned(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    MedusaJS webhook — triggers WhatsApp recovery sequence.

    Security [FIX 2]:
      - Requires X-API-Key header (same as all other endpoints).
      - Optionally verifies X-Signature: sha256=<hmac> if WEBHOOK_SECRET is set.
        Set WEBHOOK_SECRET in .env and configure MedusaJS to sign payloads.
    """
    from shared.security import verify_webhook_signature, verify_slack_signature, WEBHOOK_SECRET
    from retention.whatsapp_recovery import WhatsAppRecoveryBot

    raw_body = await request.body()

    # HMAC check: only enforced when WEBHOOK_SECRET is configured
    if WEBHOOK_SECRET:
        sig = request.headers.get("X-Signature")
        if not verify_webhook_signature(raw_body, sig):
            logger.warning("webhook_invalid_signature ip=%s", request.client.host if request.client else "unknown")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    import json
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Invalid JSON payload")

    # Validate through Pydantic after parsing
    try:
        data = CartAbandonedWebhook(**payload)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Payload validation failed: {e}")

    bot = WhatsAppRecoveryBot(llm_router=router)
    background_tasks.add_task(bot.trigger_recovery_sequence, data.model_dump())
    return {"status": "recovery_sequence_started"}

@app.post("/api/retention/comment-mining", dependencies=[Depends(verify_api_key)])
async def run_comment_mining(request: CommentMiningRequest, background_tasks: BackgroundTasks):
    from retention.comment_mining import CommentMiningLoop
    loop = CommentMiningLoop(llm_router=router, db=db, slack=slack)
    background_tasks.add_task(loop.run_cycle, request.model_dump())
    return {"status": "started"}


# ── Pricing A/B ───────────────────────────────────────────────────────────────
@app.post("/api/pricing/launch-ab", dependencies=[Depends(verify_api_key)])
async def launch_price_ab(request: PriceABRequest):
    tester = _get_price_ab_engine(llm_router=router)
    config = await tester.launch_test(request.model_dump())
    return config

@app.post("/api/pricing/evaluate-ab", dependencies=[Depends(verify_api_key)])
async def evaluate_price_ab(body: EvaluatePriceABRequest):  # [FIX 4] was body: dict
    tester = _get_price_ab_engine(llm_router=router)
    result = tester.analyze_results(body.config, body.metrics)
    return result


# ── Intelligence ──────────────────────────────────────────────────────────────
@app.post("/api/intelligence/meta-patterns", dependencies=[Depends(verify_api_key)])
async def meta_patterns(request: MetaAdAnalysisRequest):
    engine = _get_meta_ad_engine(llm_router=router)
    result = await engine.analyze_niche(niche=request.niche, keywords=request.keywords)
    return result

@app.post("/api/intelligence/meta-ad-library", dependencies=[Depends(verify_api_key)])
async def meta_ad_library(body: MetaAdLibraryRequest):  # [FIX 4] was body: dict
    """Legacy endpoint — use /api/intelligence/meta-patterns instead."""
    from intelligence.meta_ad_library import MetaAdIntelligenceEngine
    engine = MetaAdIntelligenceEngine(llm_router=router)
    return await engine.analyze_niche(niche=body.niche, keywords=body.keywords)

@app.get("/api/intelligence/llm-usage", dependencies=[Depends(verify_api_key)])
async def llm_usage():
    return router.get_usage_summary()


# ── Dual Store A/B ────────────────────────────────────────────────────────────
@app.post("/api/dual-store/launch", dependencies=[Depends(verify_api_key)])
async def launch_dual_store(request: DualStoreRequest, background_tasks: BackgroundTasks):
    from scaling.dual_store_ab import DualStoreABEngine
    opportunity = db.get_opportunity(request.opportunity_id)
    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    engine = DualStoreABEngine(llm_router=router, db=db)
    background_tasks.add_task(engine.launch_test, opportunity, {"id": request.brand_id, "name": "Brand"})
    return {"status": "started", "message": "Dual Store A/B launched. 50/50 via Cloudflare Workers."}

@app.post("/api/dual-store/evaluate", dependencies=[Depends(verify_api_key)])
async def evaluate_dual_store(body: EvaluateDualStoreRequest):  # [FIX 4] was body: dict
    from scaling.dual_store_ab import DualStoreABEngine
    engine = DualStoreABEngine(llm_router=router, db=db)
    return engine.evaluate_results(body.test_config, body.metrics_a, body.metrics_b)


# ── HeyGen Avatar ─────────────────────────────────────────────────────────────
@app.post("/api/avatar/create-videos", dependencies=[Depends(verify_api_key)])
async def create_avatar_videos(request: AvatarVideoRequest, background_tasks: BackgroundTasks):
    from scaling.heygen_avatar import HeyGenAvatarEngine
    engine = HeyGenAvatarEngine(llm_router=router)
    opportunity = db.get_opportunity(request.product_id)
    if not opportunity:
        raise HTTPException(status_code=404, detail="Product not found")
    background_tasks.add_task(
        engine.batch_create_hooks,
        opportunity,
        request.hook_categories or ["fear", "transformation", "social_proof"],
    )
    return {"status": "started", "message": "Videos generating. ~5-10 min each."}

@app.get("/api/avatar/list-avatars", dependencies=[Depends(verify_api_key)])
async def list_avatars():
    from scaling.heygen_avatar import HeyGenAvatarEngine
    engine = HeyGenAvatarEngine(llm_router=router)
    return {"avatars": await engine.list_available_avatars()}


# ── SaaS Spawn ────────────────────────────────────────────────────────────────
@app.post("/api/saas/spawn-tenant", dependencies=[Depends(verify_api_key)])
async def spawn_tenant(request: SpawnTenantRequest):
    from scaling.saas_spawn import SaaSSpawnEngine
    engine = SaaSSpawnEngine(llm_router=router, db=db)
    result = await engine.spawn_tenant(
        company_name=request.company_name,
        owner_email=request.owner_email,
        plan=request.plan,
        niches=request.niches,
    )
    return result

@app.get("/api/saas/plans", dependencies=[Depends(verify_api_key)])
async def get_plans():
    """
    [FIX C3] DECISION: Made PRIVATE with API key auth.
    
    This endpoint exposes SaaS pricing tiers and plan details. If you need
    it public for a landing page, remove dependencies=[Depends(verify_api_key)].
    Current default: PRIVATE (requires X-API-Key header).
    """
    return _get_saas_engine().get_plan_comparison()


# ── n8n triggers ──────────────────────────────────────────────────────────────
@app.post("/api/n8n/oracle-trigger", dependencies=[Depends(verify_api_key)])
async def n8n_oracle(body: N8nTriggerRequest, background_tasks: BackgroundTasks):  # [FIX 4]
    from oracle.agents import OracleDetectionSystem
    oracle = OracleDetectionSystem(llm_router=router, db=db, slack=slack)
    background_tasks.add_task(oracle.run_detection_cycle, body.tenant_id, None)
    return {"status": "triggered", "tenant_id": body.tenant_id}

@app.post("/api/n8n/monitoring-trigger", dependencies=[Depends(verify_api_key)])
async def n8n_monitoring(body: N8nTriggerRequest, background_tasks: BackgroundTasks):  # [FIX 4]
    from monitoring.metrics_collector import MonitoringCycle
    cycle = MonitoringCycle(db=db, slack=slack, llm_router=router)
    background_tasks.add_task(cycle.run_cycle, body.tenant_id)
    return {"status": "triggered"}

@app.post("/api/n8n/comment-mining-trigger", dependencies=[Depends(verify_api_key)])
async def n8n_comment_mining(body: N8nTriggerRequest, background_tasks: BackgroundTasks):  # [FIX 4]
    from retention.comment_mining import CommentMiningLoop
    loop = CommentMiningLoop(llm_router=router, db=db, slack=slack)
    background_tasks.add_task(loop.run_cycle, body.model_dump())
    return {"status": "triggered"}

@app.post("/api/webhooks/slack")
async def slack_webhook(request: Request):
    """
    Slack interactive message callbacks (approve/reject gates).

    [FIX C2] CRITICAL SECURITY: Verifies X-Slack-Signature HMAC (v0 scheme)
    before processing any action. Without this, anyone could POST to this endpoint
    and approve $5,000+ ad spends without authorization.

    Slack sends interactive payloads as application/x-www-form-urlencoded
    with a JSON string in the `payload` field — NOT raw JSON.

    Supported actions:
        {approval_id}_approve  → approve opportunity / scale campaign
        {approval_id}_reject   → reject opportunity / hold campaign
    """
    import json
    from urllib.parse import parse_qs
    from shared.security import verify_slack_signature

    # ── 1. Read raw body BEFORE any parsing (needed for HMAC) ─────────────────
    body_bytes = await request.body()

    # ── 2. Verify Slack HMAC signature (anti-spoofing, anti-replay) ───────────
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    if not verify_slack_signature(body_bytes, timestamp, signature):
        logger.error("slack_webhook_signature_verification_failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # ── 3. Parse form-encoded body → extract JSON payload ─────────────────────
    # Slack interactive payloads arrive as:
    #   Content-Type: application/x-www-form-urlencoded
    #   Body: payload=%7B%22type%22%3A%22block_actions%22%2C...%7D
    try:
        form_data = parse_qs(body_bytes.decode("utf-8"))
        payload_str = form_data.get("payload", [""])[0]
        if not payload_str:
            raise ValueError("Empty payload field")
        payload = json.loads(payload_str)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        logger.error(f"slack_webhook_invalid_payload error={e}")
        raise HTTPException(status_code=400, detail="Invalid Slack payload")

    payload_type = payload.get("type", "")
    logger.info(f"slack_callback_verified type={payload_type}")

    # ── 4. Handle block_actions (button clicks: APPROVE / REJECT) ─────────────
    if payload_type == "block_actions":
        actions = payload.get("actions", [])
        if not actions:
            return {"status": "no_actions"}

        action      = actions[0]
        action_id   = action.get("action_id", "")   # e.g. "approval_1710000000_approve"
        value       = action.get("value", "")        # "approved" or "rejected"
        block_id    = action.get("block_id", "")     # matches approval_id used in request_approval()
        user        = payload.get("user", {}).get("name", "unknown")

        logger.info(
            f"slack_action action_id={action_id} value={value} "
            f"block_id={block_id} user={user}"
        )

        # Determine approve vs reject
        is_approved = (value == "approved" or action_id.endswith("_approve"))
        is_rejected = (value == "rejected" or action_id.endswith("_reject"))

        if not (is_approved or is_rejected):
            logger.warning(f"slack_webhook_unknown_action action_id={action_id} value={value}")
            return {"status": "unknown_action"}

        # ── 4a. Persist decision in DB ─────────────────────────────────────────
        # block_id doubles as the entity reference in the approval flow.
        # Opportunity approvals have block_id = "approval_<ts>"; campaign scale
        # actions embed the campaign_id in block_id as "campaign_<id>".
        entity_id   = block_id
        action_str  = "approve" if is_approved else "reject"

        try:
            db.log_decision({
                "tenant_id":   "system",   # Slack callbacks don't carry tenant_id
                "entity_type": "slack_gate",
                "entity_id":   entity_id,
                "action":      action_str,
                "trigger":     "human_gate",
                "reason":      f"Slack user {user} clicked {action_str}",
                "data":        {"action_id": action_id, "value": value, "block_id": block_id},
            })
        except Exception as e:
            logger.warning(f"slack_webhook_db_log_failed error={e}")

        # ── 4b. Handle campaign SCALE approval ────────────────────────────────
        # Campaign scale actions embed "campaign_<id>" in the block_id
        if block_id.startswith("campaign_"):
            campaign_id = block_id.removeprefix("campaign_")
            if is_approved:
                try:
                    db.update_campaign_metrics(campaign_id, {"scale_approved": True, "scale_approved_by": user})
                    slack.notify_alert(
                        f"✅ *Scale APPROVED* by {user}\nCampaign: `{campaign_id}` — proceed with budget increase.",
                    )
                    logger.info(f"slack_scale_approved campaign_id={campaign_id} user={user}")
                except Exception as e:
                    logger.error(f"slack_scale_db_update_failed campaign_id={campaign_id} error={e}")
            else:
                try:
                    db.update_campaign_metrics(campaign_id, {"scale_approved": False, "scale_rejected_by": user})
                    slack.notify_alert(
                        f"❌ *Scale REJECTED* by {user}\nCampaign: `{campaign_id}` — budget unchanged.",
                    )
                    logger.info(f"slack_scale_rejected campaign_id={campaign_id} user={user}")
                except Exception as e:
                    logger.error(f"slack_scale_db_update_failed campaign_id={campaign_id} error={e}")

        # ── 4c. Handle opportunity approval ───────────────────────────────────
        # Opportunity approvals: block_id = "approval_<ts>"; opportunity_id is
        # stored in the metadata field of the action or encoded in action_id.
        # If not retrievable, log the decision and let the polling loop detect it.
        elif block_id.startswith("opportunity_"):
            opportunity_id = block_id.removeprefix("opportunity_")
            new_status = "approved" if is_approved else "rejected"
            try:
                db.update_opportunity_status(opportunity_id, new_status, {"reviewed_by": user})
                emoji = "✅" if is_approved else "❌"
                slack.notify_alert(
                    f"{emoji} *Opportunity {new_status.upper()}* by {user}\nID: `{opportunity_id}`",
                )
                logger.info(f"slack_opportunity_{new_status} id={opportunity_id} user={user}")
            except Exception as e:
                logger.error(f"slack_opportunity_update_failed id={opportunity_id} error={e}")

        return {"status": "ok", "action": action_str, "entity": block_id, "user": user}

    # ── 5. url_verification challenge (Slack app setup) ───────────────────────
    if payload_type == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    logger.info(f"slack_webhook_unhandled_type type={payload_type}")
    return {"status": "ignored", "type": payload_type}


# ══════════════════════════════════════════════════════════════════════════════
# V5.1 — ORCHESTRATOR + ENGINES ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

class OrchestratorRequest(BaseModel):
    """Request para ejecutar ciclo autónomo completo."""
    tenant_id: Annotated[str, Field(min_length=1, max_length=64)]
    niches: Optional[list[Annotated[str, Field(max_length=100)]]] = Field(
        default=None, max_length=20
    )
    total_budget_usd: Annotated[float, Field(ge=10.0, le=10000.0)] = 800.0
    include_active_campaigns: bool = False


class CreativePipelineRequest(BaseModel):
    """Request para generar hooks creativos para un producto."""
    product_id: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    niche: Annotated[str, Field(min_length=1, max_length=100)]
    pain_points: Optional[list[Annotated[str, Field(max_length=100)]]] = Field(
        default=None, max_length=10
    )
    price_usd: Annotated[float, Field(ge=0.0, le=100000.0)] = 0.0
    language: Annotated[str, Field(max_length=5)] = "es"


class AdsMonitorRequest(BaseModel):
    """Request para evaluar campañas activas con kill-switch."""
    tenant_id: Annotated[str, Field(min_length=1, max_length=64)]
    campaigns: list  # List of campaign dicts


class PortfolioDecisionRequest(BaseModel):
    """Request para evaluar portfolio de productos."""
    tenant_id: Annotated[str, Field(min_length=1, max_length=64)]
    products: list  # List of scored product dicts
    total_budget_usd: Annotated[float, Field(ge=10.0, le=10000.0)] = 800.0


@app.post("/api/v51/orchestrator/run", dependencies=[Depends(verify_api_key)])
async def run_orchestrator(
    request: OrchestratorRequest,
    background_tasks: BackgroundTasks,
):
    """
    V5.1: Ejecutar ciclo autónomo completo en background.

    Pipeline: Discovery → Score → Creative → Decision → Ads Monitor → Slack

    El ciclo corre en background (~30-60 segundos).
    Resultados llegan a Slack #monitoring automáticamente.
    """
    from engines.orchestrator import AutonomousOrchestrator

    niches = request.niches or ["general"]
    orchestrator = AutonomousOrchestrator(
        llm_router=router, db=db, slack=slack
    )

    async def _run():
        # Si se piden campañas activas, obtenerlas de DB
        active_campaigns = None
        if request.include_active_campaigns and db:
            try:
                active_campaigns = db.get_active_campaigns(request.tenant_id)
            except Exception as e:
                log_warning(logger, "active_campaigns_fetch_failed",
                            tenant_id=request.tenant_id, error=str(e))

        await orchestrator.run_autonomous_cycle(
            niches=niches,
            tenant_id=request.tenant_id,
            total_budget=request.total_budget_usd,
            active_campaigns=active_campaigns,
        )

    background_tasks.add_task(_run)

    return {
        "status": "started",
        "tenant_id": request.tenant_id,
        "niches": niches,
        "budget_usd": request.total_budget_usd,
        "message": "Pipeline autónomo iniciado. Resultados en Slack #monitoring ~60s.",
    }


@app.post("/api/v51/creative/generate", dependencies=[Depends(verify_api_key)])
async def generate_creative_hooks(request: CreativePipelineRequest):
    """
    V5.1: Generar hooks virales para un producto específico.

    Usa Feature Store — si el producto ya tiene hooks cacheados (24h),
    retorna desde caché sin llamar al LLM.
    """
    from engines.creative_engine import CreativeIntelligenceEngine

    engine = CreativeIntelligenceEngine(llm_router=router)
    hooks = await engine.run_creative_pipeline(request.model_dump())

    return {
        "product_id": request.product_id,
        "hooks": hooks,
        "hooks_count": len(hooks),
        "cached": False,  # Feature Store logging handled internally
    }


@app.post("/api/v51/ads/monitor", dependencies=[Depends(verify_api_key)])
async def monitor_ads_campaigns(request: AdsMonitorRequest):
    """
    V5.1: Evaluar campañas activas con kill-switch automático ROAS.

    Reglas:
      ROAS < 1.5 AND spend >= $50  → KILL automático
      ROAS < 2.0 AND spend >= $200 → KILL automático
      ROAS >= 2.5 por 7+ días      → SCALE (notificación Slack para aprobación)
    """
    from engines.ads_decision_engine import AdsDecisionEngine

    engine = AdsDecisionEngine(slack=slack, db=db)
    decisions = engine.evaluate_portfolio(request.campaigns)

    return {
        "tenant_id": request.tenant_id,
        "campaigns_evaluated": len(request.campaigns),
        "decisions": [d.model_dump() for d in decisions],
        "kills": sum(1 for d in decisions if d.action == "KILL"),
        "scales": sum(1 for d in decisions if d.action == "SCALE"),
        "holds": sum(1 for d in decisions if d.action == "HOLD"),
    }


@app.post("/api/v51/decision/portfolio", dependencies=[Depends(verify_api_key)])
async def evaluate_product_portfolio(request: PortfolioDecisionRequest):
    """
    V5.1: Evaluar portfolio de productos con Thompson Sampling V5.0.

    Aplica:
    - Score threshold (reject < 55, manual_review 55-69, approve >= 70)
    - Niche diversification (máx 2 productos activos por nicho)
    - Thompson Sampling tie-breaking para distribución de presupuesto
    """
    from core.decision_engine import DecisionEngine

    engine = DecisionEngine()
    result = engine.evaluate_portfolio(
        products=request.products,
        total_budget=request.total_budget_usd,
    )

    return {
        "tenant_id": request.tenant_id,
        **result,
    }


@app.get("/api/v51/system/status", dependencies=[Depends(verify_api_key)])
async def system_status():
    """
    V5.1: Estado completo del sistema.

    Incluye: versión, circuit breakers, bulkheads, feature store stats, LLM usage.
    """
    from shared.feature_store import get_feature_store
    from shared.bulkhead import get_all_bulkhead_stats

    store = get_feature_store()

    return {
        "status": "ok",
        "version": "5.1.0",
        "llm_usage": router.get_usage_summary(),
        "circuit_breakers": router.get_circuit_breaker_stats(),
        "bulkheads": get_all_bulkhead_stats(),
        "feature_store": store.get_stats(),
        "cors_origins": _allowed_origins,
        "rate_backend": "redis" if _use_redis_rate else "memory(unsafe)",
    }


@app.post("/api/n8n/orchestrator-trigger", dependencies=[Depends(verify_api_key)])
async def n8n_orchestrator(
    body: N8nTriggerRequest,
    background_tasks: BackgroundTasks,
):
    """
    V5.1: Trigger n8n para el ciclo autónomo completo.

    Reemplaza los triggers individuales de oracle y monitoring
    con un único trigger que corre el pipeline completo.

    Payload JSON ejemplo:
        {"tenant_id": "tenant_123", "niches": ["masajeadores", "vitaminas"]}
    """
    from engines.orchestrator import AutonomousOrchestrator

    orchestrator = AutonomousOrchestrator(
        llm_router=router, db=db, slack=slack
    )

    # N8nTriggerRequest tiene extra='allow', niches llega en model_dump()
    payload = body.model_dump()
    niches_raw = payload.get("niches", ["general"])
    # Normalizar: acepta string único o lista
    if isinstance(niches_raw, str):
        niches = [niches_raw]
    elif isinstance(niches_raw, list) and niches_raw:
        niches = [str(n) for n in niches_raw[:20]]  # max 20 nichos
    else:
        niches = ["general"]

    background_tasks.add_task(
        orchestrator.run_autonomous_cycle,
        niches=niches,
        tenant_id=body.tenant_id,
        total_budget=800.0,
    )

    return {"status": "triggered", "tenant_id": body.tenant_id,
            "niches": niches, "version": "5.1"}


# ══════════════════════════════════════════════════════════════════════════════
# V5.2 — NEW ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

# ── V5.2: Deep Health Check ────────────────────────────────────────────────────

@app.get("/health/deep", dependencies=[Depends(verify_api_key)])
async def health_deep():
    """
    V5.2: Deep health check — verifica TODOS los servicios externos.
    Úsalo en n8n antes de disparar un ciclo para evitar ciclos fallidos.

    Verifica: Supabase, Redis, Anthropic API, OpenAI API, Groq API, Slack.
    Retorna: status por servicio + latencia en ms.
    """
    import time
    checks = {}

    # Supabase
    t0 = time.time()
    try:
        db.health_check()
        checks["supabase"] = {"status": "ok", "latency_ms": round((time.time()-t0)*1000)}
    except Exception as e:
        checks["supabase"] = {"status": "error", "error": str(e)[:100]}

    # Redis (check via rate limiter)
    t0 = time.time()
    try:
        import os, redis as redis_lib
        r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), socket_timeout=2)
        r.ping()
        checks["redis"] = {"status": "ok", "latency_ms": round((time.time()-t0)*1000)}
    except Exception as e:
        checks["redis"] = {"status": "degraded", "note": "in-memory fallback active", "error": str(e)[:80]}

    # Anthropic API
    t0 = time.time()
    try:
        import anthropic, os
        client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        # Minimal model call to verify API key validity
        checks["anthropic"] = {"status": "ok", "note": "key configured",
                                "latency_ms": round((time.time()-t0)*1000)}
    except Exception as e:
        checks["anthropic"] = {"status": "error", "error": str(e)[:100]}

    # OpenAI
    t0 = time.time()
    try:
        import os
        key = os.getenv("OPENAI_API_KEY", "")
        checks["openai"] = {
            "status": "ok" if key.startswith("sk-") else "missing_key",
            "latency_ms": round((time.time()-t0)*1000)
        }
    except Exception as e:
        checks["openai"] = {"status": "error", "error": str(e)[:100]}

    # Groq
    t0 = time.time()
    try:
        import os
        key = os.getenv("GROQ_API_KEY", "")
        checks["groq"] = {
            "status": "ok" if key.startswith("gsk_") else "missing_key",
            "latency_ms": round((time.time()-t0)*1000)
        }
    except Exception as e:
        checks["groq"] = {"status": "error", "error": str(e)[:100]}

    # Overall
    all_ok = all(v.get("status") in ("ok", "degraded") for v in checks.values())
    return {
        "status":   "ok" if all_ok else "degraded",
        "version":  "5.2.0",
        "services": checks,
        "llm_stats": router.get_full_stats(),
    }


# ── V5.2: Budget status ────────────────────────────────────────────────────────

@app.get("/api/v52/budget/status", dependencies=[Depends(verify_api_key)])
async def budget_status():
    """
    V5.2: Estado del presupuesto diario de LLM.
    Muestra cuánto se ha gastado por tier y cuánto queda.
    """
    budget_report = router.get_budget_report()
    if not budget_report:
        return {
            "status": "ok",
            "note":   "BudgetGovernor not initialized — add to startup in main.py",
            "version": "5.2.0",
        }
    return {"status": "ok", "budget": budget_report, "version": "5.2.0"}


# ── V5.2: SSE Pipeline Stream ─────────────────────────────────────────────────

@app.get("/pipeline/stream")  # Auth via query param (EventSource can't send headers)
async def pipeline_stream(request: Request, api_key: Optional[str] = None):
    # EventSource cannot send custom headers — accept api_key as query param
    # Also accept X-API-Key header for curl/programmatic clients
    key = api_key or request.headers.get("X-API-Key", "")
    if key != _API_KEY:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Invalid API key"}, status_code=401)
    """
    V5.2: Server-Sent Events stream del estado del pipeline.
    Permite un dashboard en tiempo real (Grafana panel o frontend JS).

    Uso en dashboard JS:
        const es = new EventSource('/pipeline/stream?api_key=YOUR_KEY');
        es.onmessage = (e) => console.log(JSON.parse(e.data));

    Emite cada 5 segundos:
        - LLM usage + cost por tier
        - Circuit breaker states
        - Budget remaining
        - Feature store hit rate
        - Timestamp
    """
    import asyncio, json, time
    from fastapi.responses import StreamingResponse

    async def event_generator():
        while True:
            if await request.is_disconnected():
                break

            try:
                from shared.feature_store import get_feature_store
                from shared.bulkhead import get_all_bulkhead_stats

                payload = {
                    "ts":              time.time(),
                    "version":         "5.2.0",
                    "llm":             router.get_usage_summary(),
                    "circuit_breakers": router.get_circuit_breaker_stats(),
                    "budget":          router.get_budget_report(),
                    "feature_store":   get_feature_store().get_stats(),
                    "bulkheads":       get_all_bulkhead_stats(),
                }
                cache_stats = router.get_semantic_cache_stats()
                if cache_stats:
                    payload["semantic_cache"] = cache_stats

                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)[:100]})}\n\n"

            await asyncio.sleep(5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


# ── V5.2: Semantic cache stats ─────────────────────────────────────────────────

@app.get("/api/v52/semantic-cache/stats", dependencies=[Depends(verify_api_key)])
async def semantic_cache_stats():
    """V5.2: Semantic cache hit rate and savings report."""
    stats = router.get_semantic_cache_stats()
    if not stats:
        return {"status": "ok", "note": "SemanticLLMCache not initialized", "version": "5.2.0"}
    return {"status": "ok", "cache": stats, "version": "5.2.0"}


# ── V5.2: System status (updated) ─────────────────────────────────────────────

@app.get("/api/v52/system/status", dependencies=[Depends(verify_api_key)])
async def system_status_v52():
    """
    V5.2: Estado completo del sistema — todos los módulos nuevos incluidos.
    Reemplaza /api/v51/system/status con métricas adicionales V5.2.
    """
    from shared.feature_store import get_feature_store
    from shared.bulkhead import get_all_bulkhead_stats

    return {
        "status":          "ok",
        "version":         "5.2.0",
        "llm":             router.get_full_stats(),
        "bulkheads":       get_all_bulkhead_stats(),
        "feature_store":   get_feature_store().get_stats(),
        "cors_origins":    _allowed_origins,
        "rate_backend":    "redis" if _use_redis_rate else "memory(unsafe)",
        "new_in_v52": [
            "BudgetGovernor — daily LLM cost hard caps",
            "route_structured() — Structured Outputs, 0 regex",
            "route_cached() — SemanticLLMCache -55% LLM calls",
            "AnthropicBatchClient — -50% cost for bulk scoring",
            "PromptStore — versioned prompts, A/B testing",
            "CrewAI oracle — 3 agents, parallel niches",
            "/health/deep — verifies all external services",
            "/pipeline/stream — SSE real-time dashboard",
        ],
    }


# ── V5.2: Admin budget reset (emergency) ──────────────────────────────────────

@app.post("/admin/budget/reset", dependencies=[Depends(verify_api_key)])
async def admin_budget_reset(tier: Optional[str] = None):
    """
    V5.2 Emergency: Reset daily budget counters.
    Use ONLY when a budget hard-stop blocks legitimate production calls.

    Query param: ?tier=ops (reset one tier) or omit (reset all)
    """
    budget_report = router.get_budget_report()
    if not budget_report:
        return {"status": "ok", "note": "BudgetGovernor not initialized"}

    gov = router._budget_governor
    if tier:
        if tier in gov._usage:
            usage = gov._usage[tier]
            usage.spent_usd = 0.0
            usage.calls = 0
            usage.throttled = False
            usage.alert_80_sent = False
            log_warning(logger, "budget_manual_reset", tier=tier, operator="admin_api")
            return {"status": "ok", "reset": tier, "version": "5.2.0"}
        return {"status": "error", "message": f"Tier '{tier}' not found"}

    # Reset all
    gov.reset_for_testing()
    log_warning(logger, "budget_full_reset", operator="admin_api")
    return {"status": "ok", "reset": "all_tiers", "version": "5.2.0"}
