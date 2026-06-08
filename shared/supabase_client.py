"""
shared/supabase_client.py — Database Client + V4.4 Schema (13 tables + optimized indices)

Usage:
    db = SupabaseClient()
    db.run_migrations()              # run once on setup (safe to re-run)
    db.save_opportunity(data)
    db.get_active_campaigns(tenant_id)

V4.4: 11 additional indices added for:
  - decision_log (tenant+ts, entity, action)
  - campaigns (tenant+status, opportunity_id)
  - opportunities (tenant+score, niche)
  - metrics_history (ts DESC)
  - saturation_logs (campaign+ts)
  - allocation_runs (tenant+ts)

All imports of supabase are lazy so the module loads without the package installed.
"""

import os
import logging
from shared.logging_utils import log_info, log_warning, log_error
from typing import Optional
logger = logging.getLogger(__name__)


# ── V4.0 Schema — paste into Supabase SQL editor OR call db.run_migrations() ──
SCHEMA_V4_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    billing_plan TEXT DEFAULT 'starter',
    llm_quota JSONB DEFAULT '{"monthly_usd_limit": 100}',
    settings JSONB DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS opportunities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    name TEXT NOT NULL,
    niche TEXT,
    source TEXT,
    raw_data JSONB,
    score FLOAT,
    score_breakdown JSONB,
    viral_score FLOAT,
    saturation_score FLOAT,
    status TEXT DEFAULT 'detected',
    fail_fast_spend_usd FLOAT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    opportunity_id UUID REFERENCES opportunities(id),
    platform TEXT NOT NULL,
    external_campaign_id TEXT,
    budget_usd FLOAT,
    spend_usd FLOAT DEFAULT 0,
    revenue_usd FLOAT DEFAULT 0,
    roas FLOAT,
    impressions BIGINT DEFAULT 0,
    clicks BIGINT DEFAULT 0,
    conversions BIGINT DEFAULT 0,
    ctr FLOAT,
    cpa FLOAT,
    status TEXT DEFAULT 'active',
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    metrics_raw JSONB
);

CREATE TABLE IF NOT EXISTS hooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    hook_text TEXT NOT NULL,
    category TEXT NOT NULL,
    niche TEXT,
    avg_ctr FLOAT,
    test_count INT DEFAULT 0,
    winning_count INT DEFAULT 0,
    metadata JSONB DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS creatives (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    campaign_id UUID REFERENCES campaigns(id),
    hook_id UUID REFERENCES hooks(id),
    creative_type TEXT NOT NULL,
    generator TEXT NOT NULL,
    url TEXT,
    performance_ctr FLOAT,
    creative_meta JSONB DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS saturation_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    campaign_id UUID REFERENCES campaigns(id),
    ts TIMESTAMPTZ DEFAULT NOW(),
    delta_cpm FLOAT,
    new_competitors INT,
    delta_ctr FLOAT,
    saturation_score FLOAT,
    hazard_prob FLOAT,
    action_taken TEXT
);

CREATE TABLE IF NOT EXISTS allocation_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    ts TIMESTAMPTZ DEFAULT NOW(),
    total_budget_usd FLOAT,
    allocations JSONB,
    product_stats JSONB,
    rationale TEXT
);

CREATE TABLE IF NOT EXISTS metrics_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    campaign_id UUID REFERENCES campaigns(id),
    ts TIMESTAMPTZ DEFAULT NOW(),
    impressions BIGINT,
    clicks BIGINT,
    conversions BIGINT,
    spend_usd FLOAT,
    revenue_usd FLOAT,
    ctr FLOAT,
    cpa FLOAT,
    roas FLOAT,
    raw JSONB
);

CREATE TABLE IF NOT EXISTS brands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    opportunity_id UUID REFERENCES opportunities(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    name TEXT NOT NULL,
    strategy JSONB,
    visual_identity JSONB,
    store_url TEXT,
    status TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    brand_id UUID REFERENCES brands(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    name TEXT NOT NULL,
    sku TEXT,
    price_usd FLOAT,
    cogs_usd FLOAT,
    margin_pct FLOAT,
    description TEXT,
    images JSONB DEFAULT '[]',
    status TEXT DEFAULT 'draft'
);

CREATE TABLE IF NOT EXISTS niche_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    anchor_opportunity_id UUID REFERENCES opportunities(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    niche_name TEXT NOT NULL,
    anchor_product TEXT,
    complementary_products JSONB DEFAULT '[]',
    consolidated_brand_id UUID REFERENCES brands(id),
    swarm_status TEXT DEFAULT 'in_progress',
    combined_roas FLOAT
);

CREATE TABLE IF NOT EXISTS decision_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    ts TIMESTAMPTZ DEFAULT NOW(),
    entity_type TEXT,
    entity_id UUID,
    action TEXT,
    trigger TEXT,
    reason TEXT,
    data JSONB DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS buyer_graph (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    product_id UUID REFERENCES products(id),
    niche TEXT,
    buyer_vector vector(1536),
    co_purchase_data JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_opportunities_status ON opportunities(status);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_saturation_ts ON saturation_logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_campaign ON metrics_history(campaign_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_hooks_niche ON hooks(niche, category);

-- V4.4: Additional indices identified from production query patterns
-- decision_log is queried heavily by tenant+action+ts in monitoring cycles
CREATE INDEX IF NOT EXISTS idx_decision_log_tenant_ts
    ON decision_log(tenant_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_decision_log_entity
    ON decision_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_decision_log_action
    ON decision_log(action);

-- campaigns queried by tenant+status+platform in allocation runs
CREATE INDEX IF NOT EXISTS idx_campaigns_tenant_status
    ON campaigns(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_campaigns_opportunity
    ON campaigns(opportunity_id);

-- opportunities queried by tenant+score for ranking (Oracle cycle)
CREATE INDEX IF NOT EXISTS idx_opportunities_tenant_score
    ON opportunities(tenant_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_opportunities_niche
    ON opportunities(niche);

-- metrics_history queried by tenant in monitoring cycle (joins through campaign)
CREATE INDEX IF NOT EXISTS idx_metrics_ts
    ON metrics_history(ts DESC);

-- saturation_logs queried by campaign+niche for hazard model
CREATE INDEX IF NOT EXISTS idx_saturation_campaign_ts
    ON saturation_logs(campaign_id, ts DESC);

-- allocation_runs queried by tenant+ts in Thompson Sampling state reload
CREATE INDEX IF NOT EXISTS idx_allocation_tenant_ts
    ON allocation_runs(tenant_id, ts DESC);
"""


class SupabaseClient:
    """Supabase DB wrapper. All external imports are lazy."""

    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            url = os.getenv("SUPABASE_URL")
            key = os.getenv("SUPABASE_KEY")
            if not url or not key:
                logger.warning("SUPABASE_URL/KEY not set — DB operations will no-op")
                return None
            try:
                from supabase import create_client
                self._client = create_client(url, key)
            except ImportError:
                logger.warning("supabase package not installed — DB no-op mode")
                return None
            except Exception as e:
                logger.error(f"Supabase connection failed: {e}")
                return None
        return self._client

    def _exec(self, fn, default=None):
        """Safely execute a DB operation. Returns default on any failure."""
        client = self._get_client()
        if client is None:
            return default
        try:
            return fn(client)
        except Exception as e:
            logger.error(f"DB operation failed: {e}")
            return default

    def run_migrations(self):
        """Apply V4.4 schema. Safe to run multiple times (IF NOT EXISTS)."""
        self._get_client().rpc("execute_sql", {"sql": SCHEMA_V4_SQL}).execute()
        logger.info("Schema V4.4 applied")

    # ── Opportunities ──────────────────────────────────────────────────────────
    def save_opportunity(self, data: dict) -> dict:
        c = self._get_client()
        if c is None: return {}
        try:
            r = c.table("opportunities").insert(data).execute()
            return r.data[0] if r.data else {}
        except Exception as e:
            logger.error(f"save_opportunity failed: {e}")
            return {}

    def get_opportunity(self, opportunity_id: str) -> Optional[dict]:
        c = self._get_client()
        if c is None: return None
        try:
            r = c.table("opportunities").select("*").eq("id", opportunity_id).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error(f"get_opportunity failed: {e}")
            return None

    def update_opportunity_status(self, opportunity_id: str, status: str, extra: dict = None):
        data = {"status": status}
        if extra:
            data.update(extra)
        self._get_client().table("opportunities").update(data).eq("id", opportunity_id).execute()

    def get_pending_opportunities(self, tenant_id: str) -> list:
        c = self._get_client()
        if c is None: return []
        try:
            r = c.table("opportunities").select("*").eq("tenant_id", tenant_id).eq("status", "detected").order("score", desc=True).execute()
            return r.data or []
        except Exception as e:
            logger.error(f"get_pending_opportunities failed: {e}")
            return []

    def get_total_portfolio_spend(self, tenant_id: str) -> float:
        r = (self._get_client().table("opportunities")
             .select("fail_fast_spend_usd")
             .eq("tenant_id", tenant_id)
             .in_("status", ["testing", "detected", "approved"]).execute())
        return sum(row.get("fail_fast_spend_usd") or 0 for row in (r.data or []))

    def get_first_winner(self, tenant_id: str) -> Optional[dict]:
        r = (self._get_client().table("opportunities")
             .select("*").eq("tenant_id", tenant_id).eq("status", "validated")
             .order("created_at").limit(1).execute())
        return r.data[0] if r.data else None

    # ── Campaigns ──────────────────────────────────────────────────────────────
    def save_campaign(self, data: dict) -> dict:
        r = self._get_client().table("campaigns").insert(data).execute()
        return r.data[0] if r.data else {}

    def update_campaign_metrics(self, campaign_id: str, metrics: dict):
        self._get_client().table("campaigns").update(metrics).eq("id", campaign_id).execute()

    def get_active_campaigns(self, tenant_id: str) -> list:
        c = self._get_client()
        if c is None: return []
        try:
            r = c.table("campaigns").select("*").eq("tenant_id", tenant_id).eq("status", "active").execute()
            return r.data or []
        except Exception as e:
            logger.error(f"get_active_campaigns failed: {e}")
            return []

    def get_campaign_history(self, campaign_id: str, days: int = 30) -> list:
        from datetime import datetime, timedelta
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        r = (self._get_client().table("metrics_history")
             .select("*").eq("campaign_id", campaign_id).gte("ts", since)
             .order("ts").execute())
        return r.data or []

    # ── Hooks ──────────────────────────────────────────────────────────────────
    def save_hook(self, data: dict) -> dict:
        r = self._get_client().table("hooks").insert(data).execute()
        return r.data[0] if r.data else {}

    def get_best_hooks_for_niche(self, niche: str, limit: int = 5) -> list:
        r = (self._get_client().table("hooks")
             .select("*").eq("niche", niche).order("avg_ctr", desc=True).limit(limit).execute())
        return r.data or []

    def update_hook_performance(self, hook_id: str, ctr: float, won: bool):
        h = self._get_client().table("hooks").select("*").eq("id", hook_id).execute()
        if not h.data:
            return
        d = h.data[0]
        new_count   = d["test_count"] + 1
        new_avg_ctr = ((d.get("avg_ctr") or 0) * d["test_count"] + ctr) / new_count
        self._get_client().table("hooks").update({
            "test_count": new_count, "avg_ctr": new_avg_ctr,
            "winning_count": d["winning_count"] + (1 if won else 0),
        }).eq("id", hook_id).execute()

    # ── Saturation & Allocations ───────────────────────────────────────────────
    def log_saturation(self, data: dict):
        self._get_client().table("saturation_logs").insert(data).execute()

    def get_recent_saturation(self, campaign_id: str, limit: int = 5) -> list:
        r = (self._get_client().table("saturation_logs")
             .select("*").eq("campaign_id", campaign_id)
             .order("ts", desc=True).limit(limit).execute())
        return r.data or []

    def log_allocation(self, data: dict):
        self._get_client().table("allocation_runs").insert(data).execute()

    def save_metrics_snapshot(self, data: dict):
        self._get_client().table("metrics_history").insert(data).execute()

    # ── Brands & Niches ────────────────────────────────────────────────────────
    def save_brand(self, data: dict) -> dict:
        r = self._get_client().table("brands").insert(data).execute()
        return r.data[0] if r.data else {}

    def save_niche_profile(self, data: dict) -> dict:
        r = self._get_client().table("niche_profiles").insert(data).execute()
        return r.data[0] if r.data else {}

    # ── Decisions ─────────────────────────────────────────────────────────────
    def log_decision(self, data: dict):
        self._get_client().table("decision_log").insert(data).execute()
        logger.info(f"Decision logged: {data.get('action')} on {data.get('entity_type')}")

    # ── V4.4: Survivorship helpers ────────────────────────────────────────────

    def get_campaign_survivorship(self, opportunity_id: str) -> dict:
        """
        Returns days_active and average ROAS for a given opportunity.
        Used by the scoring engine to populate ScoreInput.days_active
        and ScoreInput.empirical_roas.

        Returns:
            {"days_active": int, "empirical_roas": float}
            Defaults to {"days_active": 0, "empirical_roas": 0.0} if no data.
        """
        c = self._get_client()
        if c is None:
            return {"days_active": 0, "empirical_roas": 0.0}
        try:
            r = (c.table("campaigns")
                 .select("started_at, revenue_usd, spend_usd")
                 .eq("opportunity_id", opportunity_id)
                 .eq("status", "active")
                 .execute())
            rows = r.data or []
            if not rows:
                return {"days_active": 0, "empirical_roas": 0.0}

            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            days_list = []
            total_revenue = 0.0
            total_spend   = 0.0
            for row in rows:
                started_raw = row.get("started_at")
                if started_raw:
                    try:
                        started = datetime.fromisoformat(
                            started_raw.replace("Z", "+00:00")
                        )
                        days = max(0, (now - started).days)
                        days_list.append(days)
                    except (ValueError, TypeError):
                        pass
                total_revenue += float(row.get("revenue_usd") or 0.0)
                total_spend   += float(row.get("spend_usd")   or 0.0)

            days_active    = max(days_list) if days_list else 0
            empirical_roas = total_revenue / total_spend if total_spend > 0 else 0.0
            return {
                "days_active":    days_active,
                "empirical_roas": round(empirical_roas, 4),
            }
        except Exception as e:
            logger.error("get_campaign_survivorship failed: %s", e)
            return {"days_active": 0, "empirical_roas": 0.0}

    def health_check(self) -> dict:
        """
        V5.2: Verify Supabase connectivity with a lightweight query.
        Used by /health/deep endpoint.
        Returns dict with status and latency.
        Raises exception if connection fails (caller handles it).
        """
        import time
        t0 = time.time()
        client = self._get_client()
        if client is None:
            raise ConnectionError("Supabase client not initialized — check SUPABASE_URL and SUPABASE_ANON_KEY env vars")
        # Lightweight check: query a small table
        result = client.table("tenants").select("id").limit(1).execute()
        latency_ms = round((time.time() - t0) * 1000)
        return {"status": "ok", "latency_ms": latency_ms}
