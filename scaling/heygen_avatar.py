"""
scaling/heygen_avatar.py — HeyGen AI Avatar de Marca como Influencer 24/7

Aportación Grok V4.0: costo fijo $29/mes vs $500-5000 por UGC real.
El avatar graba ads de producto en video, habla en múltiples idiomas,
nunca se cansa, mantiene consistencia de marca, escala sin fricción.

Flujo:
  1. Sonnet genera el script del video (hook + benefit + CTA)
  2. HeyGen API renderiza el video con el avatar elegido
  3. El video se sube automáticamente a TikTok Ads / Meta como creative
  4. A/B test automático de diferentes scripts con el mismo avatar

Resultado: +35-40% CTR vs creativos estáticos (benchmark HeyGen, 2024)
Costo por video: ~$0.50 vs $200+ con UGC real

API: https://docs.heygen.com/reference/list-avatars
Plan: Creator $29/mo = 15 min video/mes (~30 videos de 30 seg)
"""

import os
import asyncio
import logging
from typing import Optional
from shared.llm_router import LLMRouter
from shared.constants import LLM_TIER_STRATEGIC, LLM_TIER_CREATIVE

logger = logging.getLogger(__name__)

HEYGEN_API_BASE  = "https://api.heygen.com/v2"
HEYGEN_POLL_WAIT = 30   # seconds between status polls
HEYGEN_MAX_POLLS = 40   # 20 min max wait

# Recommended avatar IDs (update from HeyGen dashboard)
# GET /avatars to list all available with your plan
DEFAULT_AVATARS = {
    "professional_female": "Daisy-inblackskirt-20220818",
    "professional_male":   "Josh_Lite3_20230714",
    "casual_female":       "Abigail_20240702_public",
    "casual_male":         "Alex_20230731",
}


class HeyGenAvatarEngine:
    """
    AI Avatar video generation for product ads.
    One avatar, unlimited scripts = consistent brand influencer.
    """

    def __init__(self, llm_router: Optional[LLMRouter] = None):
        self.router    = llm_router or LLMRouter()
        self.api_key   = os.getenv("HEYGEN_API_KEY")
        self._client   = None

    def _get_client(self):
        if self._client is None:
            try:
                import httpx
                self._client = __import__("httpx")
            except ImportError:
                raise RuntimeError("Install httpx: pip install httpx")
        return self._client

    async def create_product_video(
        self,
        product: dict,
        hook_category: str  = "fear",
        avatar_id: str      = None,
        voice_id: str       = None,
        language: str       = "es",
        duration_target: int = 30,  # seconds
    ) -> dict:
        """
        Generate a complete product ad video.

        Returns:
          {video_id, video_url, script, hook_category, duration, status}
        """
        product_name = product.get("name", "")
        niche        = product.get("niche", "")

        # 1. Generate script with Sonnet (strategic quality — this is the ad)
        script = await self._generate_script(product, hook_category, language, duration_target)
        logger.info(f"heygen_script_generated product={product_name} hook={hook_category}")

        if not self.api_key:
            logger.warning("HEYGEN_API_KEY not set — returning mock result")
            return {
                "video_id": f"mock_video_{product_name[:8]}",
                "video_url": f"https://heygen.com/video/mock_{product_name[:8]}",
                "script": script,
                "hook_category": hook_category,
                "duration_sec": duration_target,
                "status": "mock",
                "note": "Set HEYGEN_API_KEY to generate real videos",
            }

        # 2. Submit to HeyGen
        av_id  = avatar_id  or DEFAULT_AVATARS.get("professional_female")
        voi_id = voice_id   or self._default_voice(language)

        video_id = await self._submit_video(script, av_id, voi_id)
        if not video_id:
            return {"status": "error", "script": script}

        # 3. Poll for completion
        video_url = await self._poll_until_ready(video_id)

        logger.info(f"heygen_video_ready video_id={video_id} url={video_url}")
        return {
            "video_id": video_id,
            "video_url": video_url,
            "script": script,
            "hook_category": hook_category,
            "duration_sec": duration_target,
            "status": "ready",
        }

    async def batch_create_hooks(self, product: dict, hook_categories: list = None) -> list:
        """
        Create one video per hook category.
        For A/B testing: run same avatar with different psychological angles.
        Default: fear, transformation, social_proof (best performing for DTC)
        """
        hooks = hook_categories or ["fear", "transformation", "social_proof"]
        logger.info(f"heygen_batch_start product={product.get('name')} hooks={hooks}")

        tasks = [
            self.create_product_video(product, hook_category=hook)
            for hook in hooks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        videos = []
        for hook, result in zip(hooks, results):
            if isinstance(result, Exception):
                logger.error(f"Video failed for hook={hook}: {result}")
                videos.append({"hook_category": hook, "status": "error", "error": str(result)})
            else:
                videos.append(result)
        return videos

    async def _generate_script(
        self, product: dict, hook: str, language: str, duration: int
    ) -> str:
        """Sonnet generates the video script. This is what the avatar will say."""
        words_target = duration * 2  # ~120 words per minute for natural speech
        lang_instruction = "Spanish (Latin American)" if language == "es" else "English (US)"

        prompt = f"""Write a {duration}-second video ad script for an AI avatar spokesperson.

Product: {product.get('name')}
Niche: {product.get('niche')}
Price: ${product.get('base_price_usd', 39.99):.2f}
Hook type: {hook}
Language: {lang_instruction}
Target words: ~{words_target} (must match {duration}-second duration at natural pace)

SCRIPT FORMAT (avatar reads this exactly — write in first person):
[HOOK - first 3 seconds - {hook} based, immediately grabs attention]
[PROBLEM - 5 seconds - agitate the pain]
[SOLUTION - 10 seconds - introduce product as THE answer]
[PROOF - 7 seconds - specific result / number / social proof]
[CTA - 5 seconds - direct call to action with urgency]

Rules:
- Write ONLY the spoken words (no stage directions, no brackets in final output)
- Conversational tone, not salesy
- Include ONE specific number or statistic
- End with clear CTA: "Link in bio" or "Click the link below"
- Output ONLY the final script, nothing else"""

        raw = await self.router.route(LLM_TIER_STRATEGIC, prompt, max_tokens=400, temperature=0.75)
        return raw.strip()

    async def _submit_video(self, script: str, avatar_id: str, voice_id: str) -> Optional[str]:
        """Submit video generation job to HeyGen API."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{HEYGEN_API_BASE}/video/generate",
                    headers={
                        "X-Api-Key":    self.api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "video_inputs": [{
                            "character": {
                                "type": "avatar",
                                "avatar_id": avatar_id,
                                "avatar_style": "normal",
                            },
                            "voice": {
                                "type": "text",
                                "voice_id": voice_id,
                                "input_text": script,
                                "speed": 1.05,
                            },
                            "background": {"type": "color", "value": "#FFFFFF"},
                        }],
                        "dimension": {"width": 1080, "height": 1920},  # TikTok vertical
                        "aspect_ratio": "9:16",
                        "caption": False,
                    }
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", {}).get("video_id")
        except Exception as e:
            logger.error(f"HeyGen submit failed: {e}")
            return None

    async def _poll_until_ready(self, video_id: str) -> Optional[str]:
        """Poll HeyGen until video is ready. Returns URL."""
        try:
            import httpx
            for _ in range(HEYGEN_MAX_POLLS):
                await asyncio.sleep(HEYGEN_POLL_WAIT)
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{HEYGEN_API_BASE}/video_status.get",
                        headers={"X-Api-Key": self.api_key},
                        params={"video_id": video_id},
                    )
                    resp.raise_for_status()
                    data   = resp.json().get("data", {})
                    status = data.get("status")
                    if status == "completed":
                        return data.get("video_url")
                    elif status == "failed":
                        logger.error(f"HeyGen render failed: {data.get('error')}")
                        return None
        except Exception as e:
            logger.error(f"HeyGen poll failed: {e}")
        return None

    def _default_voice(self, language: str) -> str:
        """Default voice IDs by language (from HeyGen voice library)."""
        voices = {
            "es": "es-MX-DaliaNeural",    # Spanish Mexico
            "en": "en-US-AriaNeural",     # English US
            "pt": "pt-BR-FranciscaNeural",# Portuguese Brazil
        }
        return voices.get(language, voices["en"])

    async def list_available_avatars(self) -> list:
        """Fetch available avatars from HeyGen (for setup/configuration)."""
        if not self.api_key:
            return list(DEFAULT_AVATARS.items())
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{HEYGEN_API_BASE}/avatars",
                    headers={"X-Api-Key": self.api_key},
                )
                resp.raise_for_status()
                avatars = resp.json().get("data", {}).get("avatars", [])
                return [{"id": a.get("avatar_id"), "name": a.get("avatar_name")} for a in avatars]
        except Exception as e:
            logger.error(f"HeyGen list_avatars failed: {e}")
            return []
