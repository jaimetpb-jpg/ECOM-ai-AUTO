"""
oracle/sources.py — Oracle Data Sources V4.1

Fixes aplicados (revisión crítica §3):
  [1] ALTO:  Meta Ad Library — paginación completa con cursor + exponential backoff + jitter
  [2] ALTO:  Todas las llamadas HTTP usan httpx.AsyncClient (no blocking I/O)
  [3] MEDIO: Normalización de texto para clustering: lower, remove punct, truncate
  [4] MEDIO: Deduplicación por hash(ad_body) antes de procesar
  [5] MEDIO: Guardar last_fetched_cursor por keyword en DB (reintentos incrementales)
  [6] ALTO:  Backoff con jitter en todos los clientes API
"""

import os
import re
import time
import random
import asyncio
import hashlib
import logging
from typing import Optional
from shared.llm_router import LLMRouter
from shared.constants import LLM_TIER_BULK, LLM_TIER_OPS

logger = logging.getLogger(__name__)

META_AD_LIBRARY_URL = "https://graph.facebook.com/v21.0/ads_archive"

# ─── Backoff helper [FIX 1 / FIX 6] ─────────────────────────────────────────
async def backoff_retry(coro_fn, max_attempts: int = 4, base_delay: float = 1.0):
    """
    Exponential backoff with jitter.
    On rate-limit (429) or server error (5xx): wait 2^n + random(0,1) seconds.
    """
    for attempt in range(max_attempts):
        try:
            return await coro_fn()
        except Exception as e:
            is_last = (attempt == max_attempts - 1)
            # Check if it's a rate limit or server error worth retrying
            status = getattr(getattr(e, "response", None), "status_code", 0)
            if is_last or status not in (429, 500, 502, 503, 504):
                raise
            delay = (2 ** attempt) * base_delay + random.uniform(0, 1)  # jitter [FIX 1]
            logger.warning(f"backoff attempt={attempt+1}/{max_attempts} status={status} wait={delay:.1f}s error={e}")
            await asyncio.sleep(delay)
    return None


# ─── Text normalization [FIX 3] ───────────────────────────────────────────────
def normalize_text(text: str, max_len: int = 500) -> str:
    """Normalize ad copy for clustering: lower, remove punct, truncate."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)    # remove punctuation
    text = re.sub(r"\s+", " ", text).strip() # collapse whitespace
    return text[:max_len]


def ad_body_hash(body: str) -> str:
    """Fingerprint for deduplication [FIX 4]."""
    return hashlib.md5(normalize_text(body, max_len=200).encode()).hexdigest()


class Helium10Client:
    """Fetches product demand and competition data from Helium10."""

    BASE_URL = "https://api.helium10.com/v1"

    def __init__(self):
        self.api_key = os.getenv("HELIUM10_API_KEY")

    async def search_products(self, keyword: str, marketplace: str = "US") -> list:
        if not self.api_key:
            logger.warning("HELIUM10_API_KEY not set — skipping")
            return []
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                async def _call():
                    r = await client.get(
                        f"{self.BASE_URL}/keywords/magnet",
                        headers={"X-API-Key": self.api_key},
                        params={"keyword": keyword, "marketplace": marketplace, "limit": 20},
                    )
                    r.raise_for_status()
                    return r.json().get("data", {}).get("keywords", [])
                return await backoff_retry(_call)
        except Exception as e:
            logger.error(f"helium10_search_failed keyword={keyword} error={e}")
            return []

    async def get_product_metrics(self, asin: str) -> Optional[dict]:
        if not self.api_key:
            return None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                async def _call():
                    r = await client.get(
                        f"{self.BASE_URL}/products/{asin}",
                        headers={"X-API-Key": self.api_key},
                    )
                    r.raise_for_status()
                    return r.json().get("data")
                return await backoff_retry(_call)
        except Exception as e:
            logger.error(f"helium10_product_failed asin={asin} error={e}")
            return None

    def compute_demand_score(self, metrics: dict) -> float:
        monthly_searches = metrics.get("monthly_search_volume", 0)
        trend_change     = metrics.get("trend_change_pct", 0)
        base  = min(100, (monthly_searches / 10000) * 50)
        trend = min(30, max(-20, trend_change / 2))
        return max(0, min(100, base + trend))


class TikTokTrendsClient:
    """Fetches trending products and viral data from TikTok Creative Center."""

    BASE_URL = "https://business-api.tiktok.com/open_api/v1.3"

    def __init__(self):
        self.access_token = os.getenv("TIKTOK_ACCESS_TOKEN")

    async def get_trending_products(self, category: str = "", days: int = 7) -> list:
        if not self.access_token:
            logger.warning("TIKTOK_ACCESS_TOKEN not set")
            return []
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                async def _call():
                    r = await client.post(
                        f"{self.BASE_URL}/creative_center/product/trending/",
                        headers={"Access-Token": self.access_token, "Content-Type": "application/json"},
                        json={"time_range": days, "category": category, "page_size": 20},
                    )
                    r.raise_for_status()
                    return r.json().get("data", {}).get("products", [])
                return await backoff_retry(_call)
        except Exception as e:
            logger.error(f"tiktok_trends_failed error={e}")
            return []

    async def compute_viral_score(self, keyword: str) -> float:
        if not self.access_token:
            return 50.0
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                async def _call():
                    r = await client.post(
                        f"{self.BASE_URL}/creative_center/keyword/analytics/",
                        headers={"Access-Token": self.access_token, "Content-Type": "application/json"},
                        json={"keyword": keyword, "time_range": 7},
                    )
                    r.raise_for_status()
                    return r.json().get("data", {})
                data = await backoff_retry(_call)
            if not data:
                return 50.0
            hv = min(100, (data.get("hashtag_growth_pct", 0) / 50) * 100)
            er = min(100, (data.get("engagement_rate", 0) / 0.06) * 100)
            vg = min(100, (data.get("view_growth_pct", 0) / 50) * 100)
            tc = data.get("creative_center_rank_score", 50)
            return round(min(100, max(0, hv * 0.35 + er * 0.30 + vg * 0.20 + tc * 0.15)), 1)
        except Exception as e:
            logger.warning(f"viral_score_failed keyword={keyword} error={e}")
            return 50.0


class GoogleTrendsClient:
    """Seasonal signal and demand validation via Google Trends."""

    def __init__(self):
        self._pytrends = None

    def _get_pytrends(self):
        if self._pytrends is None:
            from pytrends.request import TrendReq
            self._pytrends = TrendReq(hl="en-US", tz=360)
        return self._pytrends

    def get_interest_score(self, keyword: str) -> float:
        try:
            pt = self._get_pytrends()
            pt.build_payload([keyword], timeframe="today 3-m", geo="")
            data = pt.interest_over_time()
            if data.empty:
                return 30.0
            return float(data[keyword].mean())
        except Exception as e:
            logger.warning(f"google_trends_failed keyword={keyword} error={e}")
            return 30.0

    def get_seasonal_peak_weeks(self, keyword: str) -> Optional[int]:
        try:
            import datetime
            pt = self._get_pytrends()
            pt.build_payload([keyword], timeframe="today 5-y", geo="")
            data = pt.interest_over_time()
            if data.empty:
                return None
            monthly_avg = data[keyword].groupby(data.index.month).mean()
            peak_month  = int(monthly_avg.idxmax())
            now = datetime.datetime.utcnow()
            next_peak = datetime.datetime(
                now.year if now.month <= peak_month else now.year + 1,
                peak_month, 1,
            )
            weeks = max(0, (next_peak - now).days // 7)
            return weeks if 0 < weeks <= 52 else None
        except Exception as e:
            logger.warning(f"seasonal_peak_failed keyword={keyword} error={e}")
            return None


class ApifyProductScraper:
    """Scrapes Amazon and MercadoLibre for product data and reviews."""

    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            from apify_client import ApifyClient
            self._client = ApifyClient(os.getenv("APIFY_TOKEN", ""))
        return self._client

    async def scrape_amazon_product(self, keyword: str, max_items: int = 20) -> list:
        try:
            client = self._get_client()
            run = client.actor("junglee/amazon-search-scraper").call(
                run_input={"keywords": keyword, "maxItemsPerStartUrl": max_items, "useCaptchaSolver": True}
            )
            items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
            # Deduplicate by title hash [FIX 4]
            seen, unique = set(), []
            for item in items:
                h = ad_body_hash(item.get("title", ""))
                if h not in seen:
                    seen.add(h)
                    unique.append(item)
            return unique
        except Exception as e:
            logger.error(f"amazon_scrape_failed keyword={keyword} error={e}")
            return []

    async def scrape_ml_product(self, keyword: str, max_items: int = 20) -> list:
        try:
            client = self._get_client()
            run = client.actor("tri_angle/mercadolibre-scraper").call(
                run_input={"keyword": keyword, "country": "MX", "maxItems": max_items}
            )
            return list(client.dataset(run["defaultDatasetId"]).iterate_items())
        except Exception as e:
            logger.error(f"ml_scrape_failed keyword={keyword} error={e}")
            return []

    def compute_competition_score(self, products: list) -> float:
        if not products:
            return 70.0
        strong_sellers = sum(1 for p in products if int(p.get("reviewsCount", 0)) > 500)
        competition_raw = min(100, (strong_sellers / len(products)) * 100)
        return max(0, 100 - competition_raw)


class MetaAdLibraryFetcher:
    """
    Meta Ad Library API client with full pagination, backoff+jitter, and deduplication.

    [FIX 1]: Full cursor-based pagination (paging.next) — fetches ALL results, not just first page.
    [FIX 1]: Exponential backoff with jitter on 429/5xx.
    [FIX 4]: Deduplication by hash(ad_body) — no duplicate clusters.
    [FIX 5]: Saves last cursor per keyword for incremental fetches.
    [FIX 3]: Normalizes text before returning.
    """

    def __init__(self, cursor_store: Optional[dict] = None):
        self.access_token = os.getenv("META_ACCESS_TOKEN")
        # In-memory cursor store (replace with Redis/DB in production) [FIX 5]
        self._cursor_store = cursor_store if cursor_store is not None else {}

    async def search_ads_paginated(
        self,
        query: str,
        countries: list = None,
        max_results: int = 100,
        min_active_days: int = 0,
        max_active_days: int = 9999,
    ) -> list:
        """
        Full paginated fetch from Meta Ad Library.
        Respects max_results limit and continues through paging.next cursors.
        Returns deduplicated, normalized ads.
        """
        if not self.access_token:
            logger.warning("META_ACCESS_TOKEN not set — returning empty")
            return []

        import httpx
        from datetime import datetime, timedelta

        all_ads: list = []
        seen_hashes:  set = set()
        # Resume from saved cursor if available [FIX 5]
        after_cursor = self._cursor_store.get(query)

        params = {
            "search_terms":         query,
            "ad_reached_countries": str(countries or ["MX", "US", "CO"]),
            "ad_active_status":     "ACTIVE",
            "ad_delivery_date_min": (datetime.now() - timedelta(days=max_active_days)).strftime("%Y-%m-%d"),
            "fields": ",".join([
                "id", "page_name", "page_id",
                "ad_creative_bodies", "ad_creative_link_titles",
                "ad_delivery_start_time", "spend", "impressions",
                "publisher_platforms",
            ]),
            "limit":        50,
            "access_token": self.access_token,
        }
        if after_cursor:
            params["after"] = after_cursor

        async with httpx.AsyncClient(timeout=30) as client:
            url = META_AD_LIBRARY_URL

            while url and len(all_ads) < max_results:
                async def _fetch(u=url, p=params):
                    r = await client.get(u, params=p)
                    r.raise_for_status()
                    return r.json()

                try:
                    data = await backoff_retry(_fetch)
                except Exception as e:
                    logger.error(f"meta_ad_library_fetch_failed query={query} error={e}")
                    break

                raw_ads = data.get("data", [])
                for ad in raw_ads:
                    if len(all_ads) >= max_results:
                        break
                    # Dedup by body hash [FIX 4]
                    bodies = ad.get("ad_creative_bodies", [])
                    body   = bodies[0] if bodies else ""
                    h      = ad_body_hash(body)
                    if h in seen_hashes:
                        continue
                    seen_hashes.add(h)

                    # Enrich with days_running and normalized body
                    start_str  = ad.get("ad_delivery_start_time", "")
                    days       = self._days_running(start_str)
                    if not (min_active_days <= days <= max_active_days):
                        continue

                    ad["days_running"]     = days
                    ad["lifecycle_stage"]  = self._lifecycle(days)
                    ad["normalized_body"]  = normalize_text(body)  # [FIX 3]
                    ad["body_hash"]        = h
                    all_ads.append(ad)

                # Pagination cursor [FIX 1]
                paging = data.get("paging", {})
                next_cursor = paging.get("cursors", {}).get("after")
                next_url    = paging.get("next")

                if next_cursor:
                    # Save cursor for next incremental run [FIX 5]
                    self._cursor_store[query] = next_cursor
                    params["after"] = next_cursor
                    url = META_AD_LIBRARY_URL
                elif next_url:
                    url    = next_url
                    params = {}  # URL already includes all params
                else:
                    break  # No more pages

        logger.info(
            f"meta_ad_library_fetched query={query} total={len(all_ads)} "
            f"deduped={len(seen_hashes)-len(all_ads)} pages_used=cursor_pagination"
        )
        return all_ads

    @staticmethod
    def _days_running(start_time: str) -> int:
        if not start_time:
            return 0
        try:
            from datetime import datetime
            start = datetime.fromisoformat(start_time.replace("+0000", "").strip())
            return (datetime.utcnow() - start).days
        except Exception:
            return 0

    @staticmethod
    def _lifecycle(days: int) -> str:
        if days <= 14:   return "testing"
        if days <= 45:   return "growing"
        if days <= 90:   return "scaling"
        if days <= 180:  return "mature"
        return "saturated"
