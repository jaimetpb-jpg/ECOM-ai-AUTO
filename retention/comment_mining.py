"""
retention/comment_mining.py — Comment Mining Loop (NEW V4.0)

Automated 15-day cycle: mine customer reviews → extract pain points → improve product.
Triggered by n8n cron for any product with >50 sales.

Outputs per cycle:
  1. Ranked pain points (by frequency + severity)
  2. Updated product brief for supplier
  3. 3 new hooks based on actual customer language
  4. Copy refresh suggestions
  5. Competitive gap analysis
"""

import logging
from typing import Optional
# from apify_client import ApifyClient  # lazy-loaded in methods
import os
from shared.llm_router import LLMRouter
from shared.constants import LLM_TIER_OPS, LLM_TIER_CREATIVE, LLM_TIER_STRATEGIC, COMMENT_MINING_MIN_SALES
from shared.logging_utils import log_info, log_warning

logger = logging.getLogger(__name__)


class CommentMiningLoop:
    """
    Runs the 15-day comment mining cycle for an active product.
    Fetches reviews from Amazon + MercadoLibre + TikTok comments via Apify.
    Extracts actionable insights using Claude Haiku.
    """

    def __init__(self, llm_router: Optional[LLMRouter] = None):
        self.router = llm_router or LLMRouter()
        self._apify = None

    def _get_apify(self):
        """Lazy-load ApifyClient — avoids NameError if apify_client not installed."""
        if self._apify is None:
            try:
                from apify_client import ApifyClient
                self._apify = ApifyClient(os.getenv("APIFY_TOKEN", ""))
            except ImportError:
                logger.warning("apify_client not installed — comment mining disabled. Install: pip install apify-client")
                return None
        return self._apify

    async def run_cycle(self, product: dict) -> dict:
        """
        Main cycle entry point. Called by n8n every 15 days.
        product: {id, name, niche, asin, ml_url, tiktok_hashtag, original_brief}
        """
        product_name = product.get("name", "Unknown")
        log_info(logger, "comment_mining_started", product=product_name)

        # 1. Scrape reviews from all available sources
        reviews = await self._scrape_reviews(product)
        if len(reviews) < 10:
            log_warning(logger, "insufficient_reviews", product=product_name, count=len(reviews))
            return {"status": "skipped", "reason": "insufficient_reviews"}

        # 2. Extract pain points (Haiku — bulk text analysis)
        pain_points = await self._extract_pain_points(reviews, product_name)

        # 3. Compare with original brief (Sonnet — strategic comparison)
        gap_analysis = await self._analyze_gaps(pain_points, product)

        # 4. Generate new hooks from pain points (GPT-4o Mini — creative)
        new_hooks = await self._generate_hooks_from_pain(pain_points, product)

        # 5. Generate copy refresh suggestions (GPT-4o Mini)
        copy_refresh = await self._generate_copy_refresh(pain_points, product)

        result = {
            "product_id": product.get("id"),
            "product_name": product_name,
            "review_count": len(reviews),
            "top_pain_points": pain_points,
            "gap_analysis": gap_analysis,
            "new_hooks": new_hooks,
            "copy_refresh": copy_refresh,
            "cycle_timestamp": __import__("datetime").datetime.utcnow().isoformat(),
        }

        log_info(logger, "comment_mining_complete", product=product_name,
                    pain_points_found=len(pain_points), new_hooks=len(new_hooks))
        return result

    async def _scrape_reviews(self, product: dict) -> list[str]:
        """Scrape reviews via Apify from Amazon and/or MercadoLibre."""
        apify = self._get_apify()
        if not apify:
            log_warning(logger, "apify_unavailable_skipping_reviews", product=product.get("name"))
            return []

        reviews = []

        # Amazon reviews (if ASIN available)
        asin = product.get("asin")
        if asin:
            try:
                run = apify.actor("junglee/amazon-reviews-scraper").call(
                    run_input={"asin": asin, "maxReviews": 100, "filterByStars": ["1_star", "2_star", "3_star"]}
                )
                items = apify.dataset(run["defaultDatasetId"]).iterate_items()
                for item in items:
                    if item.get("body"):
                        reviews.append(f"[Amazon ★{item.get('rating', '?')}] {item['body'][:300]}")
            except Exception as e:
                log_warning(logger, "amazon_scrape_failed", error=str(e))

        # MercadoLibre reviews (if URL available)
        ml_url = product.get("ml_url")
        if ml_url:
            try:
                run = apify.actor("web-scraper").call(
                    run_input={"startUrls": [{"url": ml_url + "/reviews"}], "maxPagesPerCrawl": 3}
                )
                items = apify.dataset(run["defaultDatasetId"]).iterate_items()
                for item in items:
                    text = item.get("text", "")
                    if len(text) > 30:
                        reviews.append(f"[ML] {text[:300]}")
            except Exception as e:
                log_warning(logger, "ml_scrape_failed", error=str(e))

        # TikTok comments via hashtag (if available)
        hashtag = product.get("tiktok_hashtag")
        if hashtag:
            try:
                run = apify.actor("clockworks/tiktok-scraper").call(
                    run_input={"hashtags": [hashtag], "maxItems": 50}
                )
                items = apify.dataset(run["defaultDatasetId"]).iterate_items()
                for item in items:
                    for comment in item.get("comments", [])[:5]:
                        if len(comment.get("text", "")) > 20:
                            reviews.append(f"[TikTok] {comment['text'][:200]}")
            except Exception as e:
                log_warning(logger, "tiktok_scrape_failed", error=str(e))

        log_info(logger, "reviews_scraped", product=product.get("name"), count=len(reviews))
        return reviews

    async def _extract_pain_points(self, reviews: list[str], product_name: str) -> list[dict]:
        """Extract and rank pain points using Claude Haiku."""
        sample = "\n".join(reviews[:50])  # Limit to avoid token overflow
        prompt = f"""Product: {product_name}

Customer reviews (negative/neutral):
{sample}

Extract the top 10 pain points customers mention. For each:
- Pain point (what they complain about)
- Frequency estimate (how many mention it)
- Severity (high/medium/low — does it affect purchasing?)
- Direct customer quote (exact words used)

Format each as:
PAIN: [issue] | FREQ: [N/50] | SEV: [level] | QUOTE: "..."

Be specific. Use customer language, not generic terms."""

        response = await self.router.route(LLM_TIER_OPS, prompt)
        return self._parse_pain_points(response)

    async def _analyze_gaps(self, pain_points: list[dict], product: dict) -> str:
        """Compare current pain points with original product brief. Use Sonnet."""
        original_brief = product.get("original_brief", "No brief provided")
        pain_summary = "\n".join(f"- {p['pain']} (severity: {p['severity']})" for p in pain_points[:10])

        prompt = f"""Original product brief:
{original_brief}

Current customer pain points (from {product.get('name')} reviews):
{pain_summary}

Analyze gaps:
1. Which problems are NOT addressed in the original brief?
2. Which NEW problems have emerged since launch?
3. What product improvements would solve the top 3 issues?
4. What should we tell the supplier to change?

Be specific and actionable. 300 words max."""

        return await self.router.route(LLM_TIER_STRATEGIC, prompt)

    async def _generate_hooks_from_pain(self, pain_points: list[dict], product: dict) -> list[str]:
        """Generate new hooks using actual customer language from pain points."""
        top_pains = [p["pain"] for p in pain_points[:5]]
        quotes = [p.get("quote", "") for p in pain_points[:3] if p.get("quote")]

        prompt = f"""Product: {product.get('name')} | Niche: {product.get('niche', 'lifestyle')}

Top customer complaints:
{chr(10).join(f"- {p}" for p in top_pains)}

Actual customer quotes:
{chr(10).join(f'- "{q}"' for q in quotes)}

Generate 3 TikTok hooks using the EXACT language customers use in their complaints.
Each hook should make someone who has this problem stop scrolling.
Max 2 sentences each. Conversational, no hashtags.
Format: HOOK 1: ... | HOOK 2: ... | HOOK 3: ..."""

        response = await self.router.route(LLM_TIER_CREATIVE, prompt)
        hooks = []
        for part in response.split("|"):
            part = part.strip()
            if part.startswith("HOOK"):
                text = part.split(":", 1)[-1].strip()
                if len(text) > 10:
                    hooks.append(text)
        return hooks

    async def _generate_copy_refresh(self, pain_points: list[dict], product: dict) -> dict:
        """Generate refreshed copy addressing the discovered pain points."""
        pains_str = "\n".join(f"- {p['pain']}" for p in pain_points[:5])
        prompt = f"""Product: {product.get('name')} | Niche: {product.get('niche')}

Customer pain points discovered:
{pains_str}

Generate refreshed copy that directly addresses these problems:
1. New product title (60 chars max, SEO-friendly, pain-point focused)
2. New bullet 1 (solves biggest pain point)
3. New bullet 2 (addresses second pain point)
4. New bullet 3 (social proof angle)
5. New meta description (160 chars)

Be specific. Use customer language."""

        response = await self.router.route(LLM_TIER_CREATIVE, prompt)
        return {"copy_refresh_suggestions": response}

    def _parse_pain_points(self, response: str) -> list[dict]:
        """Parse LLM pain point output."""
        pain_points = []
        for line in response.strip().split("\n"):
            if "PAIN:" in line:
                parts = {p.split(":", 1)[0].strip(): p.split(":", 1)[-1].strip()
                         for p in line.split("|") if ":" in p}
                pain_points.append({
                    "pain": parts.get("PAIN", ""),
                    "frequency": parts.get("FREQ", ""),
                    "severity": parts.get("SEV", "medium"),
                    "quote": parts.get("QUOTE", "").strip('"'),
                })
        return pain_points
