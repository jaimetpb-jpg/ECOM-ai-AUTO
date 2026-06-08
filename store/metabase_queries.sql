-- ============================================================
-- metabase_queries.sql — ROAS Kill Dashboard Queries
-- Import these into Metabase as individual questions/cards.
-- Connect Metabase to your Supabase/Postgres DB.
-- ============================================================

-- ── CARD 1: Active Campaigns Needing Action ──────────────────────────────────
-- Shows all campaigns with ROAS below thresholds — sorted by urgency.
SELECT
  c.id,
  o.name                         AS product,
  c.platform,
  ROUND(c.roas::numeric, 2)      AS roas,
  ROUND(c.spend_usd::numeric, 2) AS spend_usd,
  ROUND(c.revenue_usd::numeric, 2) AS revenue_usd,
  c.conversions,
  c.status,
  c.started_at::date             AS started,
  CURRENT_DATE - c.started_at::date AS days_running,
  CASE
    WHEN c.roas < 1.5 AND c.spend_usd >= 50  THEN 'KILL NOW 🔴'
    WHEN c.roas < 2.0 AND c.spend_usd >= 200 THEN 'KILL NOW 🔴'
    WHEN c.roas >= 2.5 AND (CURRENT_DATE - c.started_at::date) >= 7 THEN 'SCALE META ✅'
    WHEN c.roas >= 1.5 AND c.spend_usd >= 40 THEN 'VALIDATED 🟢'
    WHEN c.spend_usd < 50 THEN 'TESTING 🟡'
    ELSE 'HOLD 🟡'
  END AS recommended_action
FROM campaigns c
JOIN opportunities o ON c.opportunity_id = o.id
WHERE c.status = 'active'
ORDER BY
  CASE WHEN c.roas < 1.5 AND c.spend_usd >= 50 THEN 0 ELSE 1 END,
  c.roas ASC;

-- ── CARD 2: Thompson Sampling — Last Allocation ───────────────────────────────
SELECT
  ar.ts                          AS allocation_time,
  ar.total_budget_usd,
  ar.allocations,
  ar.rationale
FROM allocation_runs ar
ORDER BY ar.ts DESC
LIMIT 10;

-- ── CARD 3: Saturation Alerts ─────────────────────────────────────────────────
SELECT
  sl.ts,
  c.platform,
  o.name                         AS product,
  o.niche,
  ROUND(sl.saturation_score::numeric, 3) AS saturation_score,
  ROUND(sl.hazard_prob::numeric, 3)      AS hazard_30d,
  sl.action_taken,
  sl.new_competitors,
  ROUND(sl.delta_cpm::numeric, 3)        AS delta_cpm,
  ROUND(sl.delta_ctr::numeric, 3)        AS delta_ctr
FROM saturation_logs sl
JOIN campaigns c ON sl.campaign_id = c.id
JOIN opportunities o ON c.opportunity_id = o.id
WHERE sl.ts >= NOW() - INTERVAL '7 days'
  AND sl.hazard_prob > 0.2
ORDER BY sl.hazard_prob DESC, sl.ts DESC;

-- ── CARD 4: Portfolio Fail-Fast Spend ────────────────────────────────────────
-- Shows total spend before first winner (fail-fast cap tracker)
SELECT
  t.name                         AS tenant,
  SUM(o.fail_fast_spend_usd)     AS total_validation_spend,
  800.0                          AS failfast_cap,
  ROUND(SUM(o.fail_fast_spend_usd) / 800.0 * 100, 1) AS pct_of_cap,
  COUNT(CASE WHEN o.status = 'validated' THEN 1 END) AS validated_products,
  COUNT(CASE WHEN o.status = 'testing' THEN 1 END)   AS products_testing,
  COUNT(CASE WHEN o.status = 'killed' THEN 1 END)    AS products_killed
FROM tenants t
LEFT JOIN opportunities o ON o.tenant_id = t.id
GROUP BY t.id, t.name
ORDER BY total_validation_spend DESC;

-- ── CARD 5: Hook Performance by Category ─────────────────────────────────────
SELECT
  h.category,
  h.niche,
  COUNT(*)                       AS hook_count,
  ROUND(AVG(h.avg_ctr)::numeric * 100, 2) AS avg_ctr_pct,
  SUM(h.winning_count)           AS total_wins,
  ROUND(SUM(h.winning_count)::numeric / NULLIF(SUM(h.test_count), 0) * 100, 1) AS win_rate_pct
FROM hooks h
WHERE h.test_count > 0
GROUP BY h.category, h.niche
ORDER BY avg_ctr_pct DESC;

-- ── CARD 6: Revenue by Niche ──────────────────────────────────────────────────
SELECT
  o.niche,
  COUNT(DISTINCT c.id)           AS active_campaigns,
  ROUND(SUM(c.spend_usd)::numeric, 2)   AS total_spend,
  ROUND(SUM(c.revenue_usd)::numeric, 2) AS total_revenue,
  ROUND(SUM(c.revenue_usd) / NULLIF(SUM(c.spend_usd), 0), 2) AS blended_roas,
  SUM(c.conversions)             AS total_conversions
FROM campaigns c
JOIN opportunities o ON c.opportunity_id = o.id
WHERE c.started_at >= NOW() - INTERVAL '30 days'
GROUP BY o.niche
ORDER BY total_revenue DESC;

-- ── CARD 7: Decision Audit Log ────────────────────────────────────────────────
SELECT
  dl.ts,
  dl.entity_type,
  dl.action,
  dl.trigger,
  dl.reason,
  t.name AS tenant
FROM decision_log dl
JOIN tenants t ON dl.tenant_id = t.id
ORDER BY dl.ts DESC
LIMIT 50;
