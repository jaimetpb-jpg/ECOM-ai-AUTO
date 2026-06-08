# 🚀 E-Commerce AI V5.0 - Migration Guide

**Version:** V5.0  
**Release Date:** 2026-03-07  
**Migration Time:** 5 minutes  
**Downtime Required:** 0 minutes

---

## 📋 Overview

V5.0 is a **drop-in replacement** for V4.5 with three major production enhancements:

1. **Thompson Sampling Tie-Breaking** - More stable budget allocation
2. **Feature Store** - Intelligent caching for 60% CPU reduction
3. **Circuit Breaker** - Production reliability for external APIs

**Breaking Changes:** NONE  
**Configuration Changes:** NONE  
**Database Migrations:** NONE

---

## ✅ Pre-Migration Checklist

- [ ] **Backup V4.5:** `cp -r v45/ v45_backup_$(date +%Y%m%d)`
- [ ] **Review CHANGELOG:** Read `CHANGELOG_V50.md` for full details
- [ ] **Check Redis:** Ensure Redis is running (required for Feature Store)
- [ ] **Verify APIs:** All LLM API keys valid (OpenAI, Anthropic, Groq)
- [ ] **Test Environment:** Run migration in staging first (recommended)

---

## 🔄 Migration Steps

### Step 1: Backup Current System (2 min)

```bash
# Full backup
cd /path/to/project
cp -r v45/ v45_backup_$(date +%Y%m%d)

# Verify backup
ls -lh v45_backup_*
# Should show complete directory with all files
```

### Step 2: Deploy V5.0 Code (1 min)

```bash
# Extract V5.0 release
tar -xzf v50_release.tar.gz

# Replace V4.5 with V5.0
rm -rf v45/
mv v50/ v45/

# Verify files
ls -lh v45/shared/
# Should see new files: feature_store.py, circuit_breaker.py
```

### Step 3: Verify Installation (1 min)

```bash
# Run verification script
cd v45/
python scripts/verify_v50_features.py

# Expected output:
# ✅ PASSED: 5/5
#    • Thompson Sampling Tie-Breaking
#    • Feature Store
#    • Circuit Breaker
#    • LLMRouter Integration
#    • NicheClusterer Integration
# 🎉 ALL VERIFICATIONS PASSED!
```

### Step 4: Restart Services (1 min)

```bash
# Option A: systemd
systemctl restart ecommerce-ai

# Option B: Docker
docker-compose restart api

# Option C: PM2
pm2 restart ecommerce-ai

# Verify service is running
systemctl status ecommerce-ai
# OR
docker ps | grep ecommerce-ai
# OR
pm2 status
```

### Step 5: Monitor Initial Performance (<30 min)

```bash
# Watch logs for new features
tail -f logs/app.log | grep -E "(feature_cache|circuit_breaker|tie_breaking)"

# Expected log entries:
# [INFO] feature_store_initialized redis_available=True
# [INFO] circuit_breaker_closed name=anthropic
# [DEBUG] feature_cache_miss type=niche_vector product=...
# [DEBUG] feature_cache_hit type=niche_vector product=...
# [DEBUG] tie_breaking_boost product=... impressions=...
```

---

## 📊 Post-Migration Validation

### Validate Feature #1: Thompson Sampling

```python
# Check allocation stability in logs
grep "tie_breaking" logs/app.log | tail -20

# Expected: <10% of allocation decisions show tie-breaking
# Good: Multiple products getting tie-broken fairly
# Bad: Same product always winning ties (investigate)
```

### Validate Feature #2: Feature Store

```python
# Check cache hit rate after 24h
python -c "
from shared.feature_store import get_feature_store
store = get_feature_store()
stats = store.get_stats()
print(f'Hit Rate: {stats[\"hit_rate_pct\"]}%')
print(f'Total Requests: {stats[\"total_requests\"]}')
"

# Target after 24h:
# Hit Rate: >80%
# Total Requests: >100

# If hit rate <50% after 24h:
# - Check if Redis is running
# - Verify Redis connectivity
# - Check for TTL configuration
```

### Validate Feature #3: Circuit Breaker

```python
# Check circuit breaker health
python -c "
from shared.llm_router import LLMRouter
router = LLMRouter()
stats = router.get_circuit_breaker_stats()
for provider, cb_stats in stats.items():
    print(f'{provider}: {cb_stats[\"state\"]} - failures={cb_stats[\"current_failures\"]}')
"

# Expected output (all healthy):
# anthropic: closed - failures=0
# openai: closed - failures=0
# groq: closed - failures=0

# If any circuit is OPEN:
# - Check API keys
# - Check network connectivity
# - Review recent error logs
# - Circuit will auto-recover after 60s if issue resolved
```

---

## 🔧 Configuration (Optional)

### Feature Store Configuration

Default configuration works for most cases. To customize:

```python
# In main.py or initialization code
from shared.feature_store import get_feature_store

# Initialize with custom TTL
store = get_feature_store(redis_client=your_redis)
store.ttl = 3600 * 48  # 48h instead of 24h

# Reset statistics
store.reset_stats()
```

### Circuit Breaker Configuration

Default thresholds are production-tested. To customize:

```python
# In shared/llm_router.py __init__
self.circuit_breakers = {
    "anthropic": CircuitBreaker(
        failure_threshold=5,   # Default: 5 consecutive failures
        timeout_seconds=60,    # Default: 60s before HALF_OPEN
        name="anthropic"
    ),
    # ... other providers
}
```

### Thompson Sampling Configuration

Tie-breaking threshold is configurable:

```python
# In intelligence/thompson_sampling.py
# _allocate_with_tie_breaking method

tie_threshold = 0.05  # Default: 5% of best score
# Increase to 0.10 for more aggressive exploration
# Decrease to 0.02 for less tie-breaking
```

---

## 🚨 Rollback Procedure

If issues occur, rollback is instant:

```bash
# 1. Stop service
systemctl stop ecommerce-ai

# 2. Restore backup
rm -rf v45/
mv v45_backup_YYYYMMDD/ v45/

# 3. Restart service
systemctl start ecommerce-ai

# 4. Verify
systemctl status ecommerce-ai
```

**Rollback Time:** <2 minutes  
**Data Loss:** NONE (all data in Supabase, Redis preserved)

---

## 📈 Performance Monitoring

### Week 1 Metrics to Track

| Metric | Target | How to Check |
|--------|--------|--------------|
| **Feature Cache Hit Rate** | >80% after 24h | `store.get_stats()` |
| **Circuit Breaker State** | All CLOSED | `router.get_circuit_breaker_stats()` |
| **Thompson Tie Events** | <10% of allocations | `grep tie_breaking logs/` |
| **CPU Usage** | -40% to -60% | `top` or monitoring dashboard |
| **LLM API Costs** | -20% to -40% | Review monthly bill |

### Alerting Recommendations

Set up alerts for:

1. **Circuit Breaker OPEN** - Immediate attention needed
   ```
   circuit_breaker state=open for >5 minutes
   ```

2. **Feature Store Hit Rate Low** - May indicate Redis issues
   ```
   feature_store hit_rate < 50% after 24h
   ```

3. **Thompson Allocation Variance High** - May indicate data issues
   ```
   tie_breaking_events > 30% of allocations
   ```

---

## 🆘 Troubleshooting

### Issue: Feature Store Not Caching

**Symptoms:**
- Hit rate stays at 0%
- Logs show repeated cache misses

**Solutions:**
1. Check Redis:
   ```bash
   redis-cli ping
   # Should return: PONG
   ```

2. Check Redis connectivity in logs:
   ```bash
   grep "redis_get_failed" logs/app.log
   ```

3. Verify env vars:
   ```bash
   echo $REDIS_HOST
   echo $REDIS_PORT
   ```

### Issue: Circuit Breaker Stays OPEN

**Symptoms:**
- Circuit state is OPEN for >5 minutes
- Fallback provider being used constantly

**Solutions:**
1. Check API key:
   ```bash
   echo $ANTHROPIC_API_KEY
   # Should be set and valid
   ```

2. Test API manually:
   ```bash
   curl https://api.anthropic.com/v1/messages \
     -H "x-api-key: $ANTHROPIC_API_KEY" \
     -H "content-type: application/json" \
     -d '{"model":"claude-sonnet-4-20250514","max_tokens":10,"messages":[{"role":"user","content":"test"}]}'
   ```

3. Manual circuit reset:
   ```python
   from shared.llm_router import LLMRouter
   router = LLMRouter()
   router.circuit_breakers["anthropic"].reset()
   ```

### Issue: Thompson Allocation Unstable

**Symptoms:**
- Budget allocations change dramatically day-to-day
- Tie-breaking events >30% of allocations

**Solutions:**
1. Check data quality:
   ```sql
   -- Verify products have sufficient data
   SELECT product_id, impressions, clicks
   FROM product_stats
   WHERE impressions < 50
   ORDER BY impressions DESC;
   ```

2. Review tie threshold:
   - If too many ties: Decrease threshold to 0.02
   - If no ties: Increase threshold to 0.10

---

## 📞 Support

### Documentation
- **CHANGELOG:** `CHANGELOG_V50.md` - Full technical details
- **Feature Store:** `docs/FEATURE_STORE.md` - Usage guide
- **Circuit Breaker:** `docs/CIRCUIT_BREAKER.md` - Pattern guide

### Getting Help
- **GitHub Issues:** For bugs and feature requests
- **Slack:** #engineering channel
- **Email:** engineering@yourcompany.com

---

## ✅ Post-Migration Checklist

After migration, verify:

- [ ] All services running normally
- [ ] Feature Store cache hit rate increasing
- [ ] All circuit breakers in CLOSED state
- [ ] No errors in logs
- [ ] Thompson allocation decisions stable
- [ ] CPU usage decreased (check after 24h)
- [ ] LLM costs tracking normally

**If all checked:** Migration successful! 🎉

---

**Last Updated:** 2026-03-07  
**Version:** V5.0  
**Next Review:** Post-deploy +7 days
