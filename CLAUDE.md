# AI Ecommerce System V4.0 — Claude Code Context

> Read this file first. It contains everything needed to work on this codebase.

---

## 🎯 What This System Does

Fully automated dropshipping/ecommerce system that:
1. **Detects** winning product opportunities (Oracle layer)
2. **Scores** them with a multi-variable formula (Scoring V4.0)
3. **Validates** with $50 TikTok tests (Validation layer)
4. **Brands** winners with 100% AI-generated identity (Branding layer)
5. **Scales** across TikTok → Meta → Google → Amazon/ML (Scaling layer)
6. **Monitors** continuously and optimizes budget via Thompson Sampling (Intelligence layer)

**Rule #1**: Never spend money before validating. Fail-Fast Budget cap = **$800 total** before first winner.

---

## 🗂️ Project Structure

```
ecommerce-ai-v4/
│
├── CLAUDE.md                    ← YOU ARE HERE
├── .cursorrules                 ← Cursor IDE rules (same content)
├── main.py                      ← FastAPI app — all API endpoints
├── requirements.txt             ← pip install -r requirements.txt
│
├── oracle/                      ← Opportunity Detection
│   ├── agents.py                ← CrewAI: NicheHunter + MarketValidator + Analyzer
│   └── sources.py               ← APIs: Helium10, TikTok, Google Trends, Apify, Meta Ad Library
│
├── scoring/                     ← Scoring Engine V4.0
│   └── engine.py                ← S = D×0.25 + C×0.18 + M×0.22 + O×0.12 + L×0.08 + V×0.15 − R×20 − Sr×10
│
├── validation/                  ← TikTok Validation Pipeline
│   └── creative_generator.py    ← $50 test · hooks · Flux.1 images · ROAS evaluation
│
├── branding/                    ← AI Brand Generation (<2h)
│   └── brand_creator.py         ← Sonnet (strategy) + GPT-Mini (copy) + Flux.1 (images)
│
├── intelligence/                ← NEW in V4.0: Math & Pattern Engines
│   ├── thompson_sampling.py     ← Bayesian budget allocation (ChatGPT Engine)
│   ├── saturation_hazard.py     ← Survival model: predict saturation 2-3 weeks early
│   ├── hook_engine.py           ← Hook Intelligence: learn which hook types win by niche
│   └── meta_ad_library.py       ← Meta Ad Library scraping for competitive intel
│
├── retention/                   ← NEW in V4.0: Post-Sale Retention
│   ├── whatsapp_recovery.py     ← WhatsApp cart abandonment recovery (18-28%)
│   └── comment_mining.py        ← 15-day loop: mine reviews → improve product
│
├── pricing/                     ← NEW in V4.0: Dynamic Pricing
│   └── dynamic_ab.py            ← 3-price-point A/B testing post-validation
│
├── monitoring/                  ← Continuous 6h monitoring cycle
│   └── metrics_collector.py     ← Decision rules: kill/scale/hold
│
├── scaling/                     ← Channel expansion
│   ├── niche_swarm.py           ← Niche Swarm: dominate niche completely before jumping
│   ├── meta_ads.py              ← Meta Ads automation
│   └── google_ads.py            ← Google Shopping automation
│
├── shared/                      ← Shared utilities
│   ├── llm_router.py            ← 4-tier LLM cost optimizer (65-70% savings)
│   ├── supabase_client.py       ← DB client + V4.0 schema (9 tables)
│   └── slack_notifier.py        ← Human gates + alerts
│
├── infra/
│   ├── docker-compose.yml       ← Full stack (n8n, Postgres, Redis, Metabase, FastAPI)
│   ├── hetzner_setup.sh         ← One-command VPS bootstrap
│   └── .env.example             ← All required env vars
│
├── n8n/
│   ├── oracle_workflow.json     ← Import directly into n8n UI
│   ├── monitoring_workflow.json ← 6h monitoring cycle
│   └── comment_mining_workflow.json ← 15-day product improvement loop
│
└── scripts/
    └── test_pipeline_v4.py      ← End-to-end test suite
```

---

## 🧠 LLM Routing — CRITICAL, always follow this

| Tier | Model | Cost | Use for |
|------|-------|------|---------|
| BULK | Groq Llama 3.3 70B | ~$0 free | Pre-filters, scraping analysis, batch ops |
| OPS | Claude Haiku 4.5 | $0.0008/1K | WhatsApp bot, summaries, customer service |
| CREATIVE | GPT-4o Mini | $0.0015/1K | Hooks, ad copy, product descriptions |
| STRATEGIC | Claude Sonnet 4 | $0.015/1K | Scoring decisions, brand strategy, risk |

**NEVER** use Sonnet for tasks Haiku or Groq can do. Always call `llm_router.route(task_type, prompt)`.

```python
from shared.llm_router import LLMRouter
router = LLMRouter()
result = await router.route("bulk", "Classify this product...")       # → Groq
result = await router.route("creative", "Write 3 TikTok hooks...")   # → GPT-4o Mini
result = await router.route("strategic", "Analyze risk of...")       # → Sonnet
```

---

## 📊 Scoring Formula V4.0

```
S = (D×0.25 + C×0.18 + M×0.22 + O×0.12 + L×0.08 + V×0.15) − (R×20) − (Sr×10)
```

| Variable | Weight | Description |
|----------|--------|-------------|
| D | 0.25 | Demand: search volume + growth velocity |
| C | 0.18 | Competition⁻¹: lower competition = higher score |
| M | 0.22 | Margin: (price − COGS) / price |
| O | 0.12 | Differentiation opportunity |
| L | 0.08 | Logistics ease + Supplier Risk Score |
| **V** | **0.15** | **Viral Score TikTok (NEW V4.0)** |
| R | ×20 | Legal risk (HARD STOP if R ≥ 0.6) |
| **Sr** | **×10** | **Saturation hazard probability (NEW V4.0)** |

**Thresholds**: ≥85 AUTO_GO · 70-84 MANUAL_REVIEW · <70 SKIP

---

## 💰 ROAS Decision Rules — HARD RULES, never change without explicit instruction

```python
ROAS_KILL_1 = ("roas < 1.5", "spend >= 50")   # AUTO KILL
ROAS_KILL_2 = ("roas < 2.0", "spend >= 200")  # AUTO KILL
ROAS_VALIDATE = ("roas >= 1.5", "spend >= 40") # → Niche Swarm → Branding
ROAS_SCALE_META = ("roas >= 2.5", "days >= 7") # HUMAN GATE → Meta
ROAS_SCALE_GOOGLE = ("roas >= 2.5", "days >= 14") # HUMAN GATE → Google
ROAS_SCALE_AMZN = ("roas >= 3.0", "days >= 30")   # HUMAN GATE → Amazon/ML
LEGAL_HARD_STOP = ("risk_score >= 0.6")            # NEVER PROCEED
FAILFAST_CAP = 800  # Max total portfolio spend before first winner
```

---

## 🚪 Human Gates — NEVER bypass these

All gates implemented via Slack interactive messages with timeout.

| Trigger | Timeout | Channel |
|---------|---------|---------|
| Opportunity score ≥ 85 | 30 min | #opportunities |
| Branding approval | 10 min | #approvals |
| Meta Ads scale (ROAS ≥ 2.5 × 7d) | 60 min | #approvals |
| Google Ads scale (ROAS ≥ 2.5 × 14d) | 60 min | #approvals |
| Any spend decision > $100 | 30 min | #approvals |

---

## 🗄️ Database (Supabase)

Run schema: `shared/supabase_client.py` → constant `SCHEMA_V4_SQL`

Tables: `tenants`, `opportunities`, `campaigns`, `hooks`, `creatives`, `saturation_logs`, `allocation_runs`, `metrics_history`, `brands`, `products`, `niche_profiles`, `decision_log`, `buyer_graph`

Key operations:
```python
from shared.supabase_client import SupabaseClient
db = SupabaseClient()
await db.save_opportunity(opportunity_data)
await db.get_active_campaigns(tenant_id)
await db.log_allocation(allocation_data)
```

---

## 🚀 Quick Start

```bash
# 1. Install dependencies
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Configure environment
cp infra/.env.example .env
# Edit .env with all API keys

# 3. Start infrastructure
docker-compose -f infra/docker-compose.yml up -d

# 4. Run DB migrations
python -c "from shared.supabase_client import SupabaseClient; SupabaseClient().run_migrations()"

# 5. Test everything
python scripts/test_pipeline_v4.py

# 6. Start API
uvicorn main:app --reload --port 8000
```

---

## 🔑 Environment Variables Required

```
ANTHROPIC_API_KEY        # Claude Sonnet + Haiku
OPENAI_API_KEY           # GPT-4o Mini
GROQ_API_KEY             # Llama 3.3 70B (free tier)
REPLICATE_API_TOKEN      # Flux.1 image generation
SUPABASE_URL             # Database
SUPABASE_KEY             # Database
TIKTOK_APP_ID            # TikTok Ads API
TIKTOK_SECRET            # TikTok Ads API
TIKTOK_ACCESS_TOKEN      # TikTok Ads API
TIKTOK_ADVERTISER_ID     # TikTok Ads API
META_ACCESS_TOKEN        # Meta Ads API
META_AD_ACCOUNT_ID       # Meta Ads API
HELIUM10_API_KEY         # Product demand data
APIFY_TOKEN              # Amazon/ML scraping
TWILIO_ACCOUNT_SID       # WhatsApp messages
TWILIO_AUTH_TOKEN        # WhatsApp messages
TWILIO_WHATSAPP_NUMBER   # WhatsApp number
SLACK_BOT_TOKEN          # Human gates + alerts
SLACK_SIGNING_SECRET     # Slack verification
N8N_WEBHOOK_URL          # n8n orchestration base URL
```

---

## 🆕 V4.0 vs V3.0 — What Changed

| Module | Change |
|--------|--------|
| `scoring/engine.py` | Added Viral Score (V×0.15) + Saturation penalty (Sr×10) |
| `intelligence/` | NEW: Thompson Sampling, Saturation Hazard, Hook Engine, Meta Ad Library |
| `retention/` | NEW: WhatsApp Cart Recovery + Comment Mining 15d loop |
| `pricing/` | NEW: Dynamic Price A/B Testing (3 price points) |
| `scaling/niche_swarm.py` | Enhanced: full Niche Swarm strategy before channel jump |
| `shared/supabase_client.py` | 5 new tables: hooks, creatives, saturation_logs, allocation_runs, metrics_history |
| Fail-Fast Budget | Hard cap $800 portfolio before first winner |
| Organic Pre-test | Post $0 organic before every $50 TikTok test |

---

## 🧪 Common Tasks

**Add a new Oracle data source:**
1. Add function to `oracle/sources.py`
2. Create CrewAI tool in `oracle/agents.py`
3. Use `router.route("bulk", ...)` for classification

**Change a ROAS threshold:**
1. Update `shared/constants.py` (ROAS_RULES dict)
2. Update `monitoring/metrics_collector.py` (apply_decision_rules)
3. Update `n8n/monitoring_workflow.json` (matching node)

**Add a new human gate:**
1. Call `slack_notifier.request_approval(message, timeout_min, channel)`
2. Returns `True/False` after timeout
3. Log result to `decision_log` table

**Run a manual Oracle cycle:**
```bash
curl -X POST http://localhost:8000/api/oracle/run \
  -H "Authorization: Bearer $API_KEY"
```

**Test Thompson Sampling allocation:**
```python
from intelligence.thompson_sampling import ThompsonSamplingAllocator
allocator = ThompsonSamplingAllocator()
allocation = allocator.allocate(products, total_budget=500.0)
```
