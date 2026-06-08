"""
shared/semantic_cache.py — Semantic LLM Cache V5.2

Cache semántico de respuestas LLM usando pgvector (YA instalado en el sistema).
Si dos prompts son >92% similares semánticamente, reutiliza la respuesta
en lugar de hacer una nueva llamada al LLM.

Por qué importa:
  En un ciclo Oracle típico con 5 nichos × 5 productos por nicho = 25 productos.
  Muchos están en el mismo nicho con pain points similares.
  Ejemplo:
    Prompt A: "Genera hooks para 'masajeador cervical', dolor de cuello"
    Prompt B: "Genera hooks para 'almohada cervical', dolor de cuello"
    → Coseno similarity: ~0.91 → por debajo del threshold → nueva llamada

    Prompt C: "Genera hooks para 'masajeador de cervical con calor', dolor cuello"
    → Coseno similarity vs A: ~0.97 → CACHE HIT → 0 tokens gastados

  En práctica: -40% a -60% llamadas LLM en ciclos con nichos concentrados.

Diseño:
  - Embeddings generados con text-embedding-3-small de OpenAI
    (mínimo costo: $0.00002/1K tokens ≈ $0.000002 por embedding)
  - Búsqueda vectorial en Supabase (pgvector ya instalado: pgvector==0.3.6)
  - Fallback completo a memoria si Supabase no disponible
  - TTL de 24h por defecto (configurable por tier)
  - Thread-safe con asyncio.Lock

Schema Supabase (ejecutar una sola vez):
    CREATE TABLE llm_semantic_cache (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tier        text NOT NULL,
        prompt_hash text NOT NULL,                  -- SHA256 del prompt limpiado
        embedding   vector(1536),                   -- text-embedding-3-small
        response    text NOT NULL,
        hit_count   integer DEFAULT 0,
        created_at  timestamptz DEFAULT now(),
        expires_at  timestamptz NOT NULL
    );
    CREATE INDEX ON llm_semantic_cache
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 50);
    CREATE INDEX ON llm_semantic_cache (tier, expires_at);
"""

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from shared.logging_utils import log_info, log_warning, log_error

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

SIMILARITY_THRESHOLD  = 0.92    # Cosine similarity required for cache hit
EMBEDDING_MODEL       = "text-embedding-3-small"
EMBEDDING_DIMENSIONS  = 1536
EMBEDDING_COST_PER_1K = 0.00002  # $0.02 / 1M tokens

# TTL per tier (seconds)
TIER_TTL = {
    "bulk":      3600 * 6,    # 6h — bulk outputs change fast
    "ops":       3600 * 12,   # 12h
    "creative":  3600 * 24,   # 24h — hooks stay valid a day
    "strategic": 3600 * 48,   # 48h — strategies more stable
}

# Maximum in-memory cache size before eviction (fallback mode)
MAX_MEMORY_ENTRIES = 500


# ─── SemanticLLMCache ─────────────────────────────────────────────────────────

class SemanticLLMCache:
    """
    Semantic similarity-based LLM response cache.

    Reduces LLM costs by reusing responses for semantically similar prompts.

    Usage:
        cache = SemanticLLMCache(supabase_client=db.supabase)

        # In LLMRouter.route():
        cached = await cache.get(tier, prompt)
        if cached:
            return cached   # Free! No LLM call needed.

        response = await llm_call(prompt)
        await cache.set(tier, prompt, response)
        return response

    With LLMRouter integration (route_with_cache):
        result = await router.route_with_cache(tier, prompt)
        # Automatically checks cache before calling LLM
    """

    def __init__(
        self,
        supabase_client=None,
        openai_client=None,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
    ):
        self._supabase = supabase_client
        self._openai = openai_client
        self._lock = asyncio.Lock()
        self.threshold = similarity_threshold

        # In-memory fallback cache: {key: (embedding, response, expires_ts)}
        self._memory_cache: Dict[str, Tuple[List[float], str, float]] = {}

        # Metrics
        self.hits = 0
        self.misses = 0
        self.embeddings_generated = 0
        self.total_embedding_cost_usd = 0.0
        self.estimated_llm_cost_saved_usd = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    async def get(
        self, tier: str, prompt: str
    ) -> Optional[str]:
        """
        Look up a cached response for semantically similar prompt.

        Returns cached response string if hit, None if miss.
        Never raises — on any error returns None (safe fallback).
        """
        try:
            async with self._lock:
                clean_prompt = self._clean_prompt(prompt)
                embedding = await self._embed(clean_prompt)
                if not embedding:
                    return None

                # Try Supabase pgvector first
                if self._supabase:
                    result = await self._vector_search_supabase(
                        embedding, tier
                    )
                    if result:
                        self.hits += 1
                        self._record_hit(result["id"])
                        log_info(
                            logger, "semantic_cache_hit",
                            tier=tier,
                            similarity=round(result["similarity"], 4),
                            source="pgvector",
                        )
                        return result["response"]

                # Try in-memory fallback
                result = self._memory_search(embedding, tier)
                if result:
                    self.hits += 1
                    log_info(
                        logger, "semantic_cache_hit",
                        tier=tier,
                        similarity=round(result["similarity"], 4),
                        source="memory",
                    )
                    return result["response"]

                self.misses += 1
                return None

        except Exception as e:
            log_warning(logger, "semantic_cache_get_error", error=str(e))
            self.misses += 1
            return None

    async def set(
        self, tier: str, prompt: str, response: str
    ) -> bool:
        """
        Store a response in the semantic cache.

        Returns True if stored successfully, False on error.
        Never raises.
        """
        try:
            async with self._lock:
                clean_prompt = self._clean_prompt(prompt)
                prompt_hash = self._hash(clean_prompt)
                embedding = await self._embed(clean_prompt)
                if not embedding:
                    return False

                ttl = TIER_TTL.get(tier, 3600 * 24)
                expires_ts = time.time() + ttl

                # Store in Supabase pgvector
                if self._supabase:
                    await self._store_supabase(
                        tier, prompt_hash, embedding, response, ttl
                    )

                # Always store in memory as backup
                self._store_memory(tier, prompt_hash, embedding, response, expires_ts)

                log_info(
                    logger, "semantic_cache_set",
                    tier=tier, prompt_len=len(clean_prompt), ttl_h=ttl // 3600
                )
                return True

        except Exception as e:
            log_warning(logger, "semantic_cache_set_error", error=str(e))
            return False

    async def get_or_generate(
        self,
        tier: str,
        prompt: str,
        generate_fn,
        **generate_kwargs,
    ) -> str:
        """
        Convenience method: get from cache or call generate_fn if miss.

        Args:
            tier:            LLM tier
            prompt:          Full prompt string
            generate_fn:     Async callable that calls the LLM
            **generate_kwargs: Extra args passed to generate_fn

        Returns:
            Response string (from cache or freshly generated)
        """
        cached = await self.get(tier, prompt)
        if cached is not None:
            return cached

        # Cache miss: call LLM
        response = await generate_fn(**generate_kwargs)

        # Cache the new response
        await self.set(tier, prompt, response)
        return response

    def get_stats(self) -> Dict[str, Any]:
        """Return cache performance metrics."""
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "total_lookups": total,
            "hit_rate_pct": round(self.hits / total * 100, 1) if total > 0 else 0.0,
            "embeddings_generated": self.embeddings_generated,
            "total_embedding_cost_usd": round(self.total_embedding_cost_usd, 6),
            "estimated_llm_cost_saved_usd": round(
                self.estimated_llm_cost_saved_usd, 4
            ),
            "memory_entries": len(self._memory_cache),
            "similarity_threshold": self.threshold,
        }

    # ── Embedding ─────────────────────────────────────────────────────────────

    async def _embed(self, text: str) -> Optional[List[float]]:
        """Generate text embedding using OpenAI text-embedding-3-small."""
        try:
            client = self._get_openai()
            resp = await client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=text[:8000],  # Hard limit for embedding API
            )
            embedding = resp.data[0].embedding
            self.embeddings_generated += 1

            # Track embedding cost
            tokens = resp.usage.total_tokens
            self.total_embedding_cost_usd += (
                tokens / 1000 * EMBEDDING_COST_PER_1K
            )
            return embedding

        except Exception as e:
            log_warning(logger, "embedding_failed", error=str(e))
            return None

    def _get_openai(self):
        """Lazy-load async OpenAI client."""
        if self._openai is None:
            try:
                import os
                from openai import AsyncOpenAI
                self._openai = AsyncOpenAI(
                    api_key=os.getenv("OPENAI_API_KEY")
                )
            except ImportError:
                raise RuntimeError("openai package required for semantic cache")
        return self._openai

    # ── Supabase pgvector storage ─────────────────────────────────────────────

    async def _vector_search_supabase(
        self, embedding: List[float], tier: str
    ) -> Optional[Dict]:
        """Search pgvector for similar cached responses."""
        try:
            # Use Supabase RPC for vector similarity search
            result = (
                self._supabase.rpc(
                    "search_llm_cache",
                    {
                        "query_embedding": embedding,
                        "match_tier": tier,
                        "similarity_threshold": self.threshold,
                        "match_count": 1,
                    },
                )
                .execute()
            )

            if result.data and len(result.data) > 0:
                row = result.data[0]
                return {
                    "id": row["id"],
                    "response": row["response"],
                    "similarity": float(row["similarity"]),
                }
            return None

        except Exception as e:
            log_warning(logger, "pgvector_search_error", error=str(e))
            return None

    async def _store_supabase(
        self,
        tier: str,
        prompt_hash: str,
        embedding: List[float],
        response: str,
        ttl_seconds: int,
    ) -> None:
        """Store response in Supabase llm_semantic_cache table."""
        try:
            expires_at = (
                datetime.utcnow() + timedelta(seconds=ttl_seconds)
            ).isoformat()

            self._supabase.table("llm_semantic_cache").insert({
                "tier": tier,
                "prompt_hash": prompt_hash,
                "embedding": embedding,
                "response": response,
                "expires_at": expires_at,
            }).execute()

        except Exception as e:
            log_warning(logger, "pgvector_store_error", error=str(e))

    def _record_hit(self, cache_id: str) -> None:
        """Increment hit_count for a cache entry."""
        try:
            if self._supabase:
                self._supabase.rpc(
                    "increment_cache_hit", {"cache_id": cache_id}
                ).execute()
        except Exception:
            pass  # Non-critical

    # ── In-memory fallback ────────────────────────────────────────────────────

    def _memory_search(
        self, embedding: List[float], tier: str
    ) -> Optional[Dict]:
        """
        Search in-memory cache using cosine similarity.
        O(N) but N is small (< MAX_MEMORY_ENTRIES).
        """
        now = time.time()
        best_sim = 0.0
        best_entry = None

        for key, (cached_emb, response, expires_ts) in self._memory_cache.items():
            if not key.startswith(f"{tier}:"):
                continue
            if expires_ts < now:
                continue

            sim = self._cosine_similarity(embedding, cached_emb)
            if sim > best_sim and sim >= self.threshold:
                best_sim = sim
                best_entry = {"response": response, "similarity": sim}

        return best_entry

    def _store_memory(
        self,
        tier: str,
        prompt_hash: str,
        embedding: List[float],
        response: str,
        expires_ts: float,
    ) -> None:
        """Store in in-memory cache with size limit."""
        # Evict expired entries first
        now = time.time()
        self._memory_cache = {
            k: v for k, v in self._memory_cache.items()
            if v[2] > now
        }

        # If still too full, evict oldest
        if len(self._memory_cache) >= MAX_MEMORY_ENTRIES:
            oldest_key = min(
                self._memory_cache.keys(),
                key=lambda k: self._memory_cache[k][2]
            )
            del self._memory_cache[oldest_key]

        key = f"{tier}:{prompt_hash}"
        self._memory_cache[key] = (embedding, response, expires_ts)

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """Fast cosine similarity without numpy dependency."""
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _clean_prompt(prompt: str) -> str:
        """Normalize prompt for consistent embedding."""
        # Remove extra whitespace, lowercase, strip
        import re
        cleaned = re.sub(r"\s+", " ", prompt.strip().lower())
        return cleaned[:4000]  # Cap at 4K chars for embedding

    @staticmethod
    def _hash(text: str) -> str:
        """SHA256 hash for deduplication."""
        return hashlib.sha256(text.encode()).hexdigest()


# ─── Supabase SQL helpers (run once to set up pgvector) ──────────────────────

SUPABASE_SETUP_SQL = """
-- Run this once in Supabase SQL editor to enable semantic cache

-- Enable pgvector (already installed via requirements.txt pgvector==0.3.6)
CREATE EXTENSION IF NOT EXISTS vector;

-- Create cache table
CREATE TABLE IF NOT EXISTS llm_semantic_cache (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tier        text NOT NULL,
    prompt_hash text NOT NULL,
    embedding   vector(1536),
    response    text NOT NULL,
    hit_count   integer DEFAULT 0,
    created_at  timestamptz DEFAULT now(),
    expires_at  timestamptz NOT NULL
);

-- Index for vector similarity search (cosine)
CREATE INDEX IF NOT EXISTS llm_cache_embedding_idx
    ON llm_semantic_cache
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 50);

-- Index for expiry cleanup
CREATE INDEX IF NOT EXISTS llm_cache_tier_expiry_idx
    ON llm_semantic_cache (tier, expires_at);

-- RPC function for similarity search (called from Python)
CREATE OR REPLACE FUNCTION search_llm_cache(
    query_embedding vector(1536),
    match_tier text,
    similarity_threshold float DEFAULT 0.92,
    match_count int DEFAULT 1
)
RETURNS TABLE (id uuid, response text, similarity float)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        lsc.id,
        lsc.response,
        1 - (lsc.embedding <=> query_embedding) AS similarity
    FROM llm_semantic_cache lsc
    WHERE lsc.tier = match_tier
      AND lsc.expires_at > now()
      AND 1 - (lsc.embedding <=> query_embedding) >= similarity_threshold
    ORDER BY lsc.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;

-- RPC function for hit count increment
CREATE OR REPLACE FUNCTION increment_cache_hit(cache_id uuid)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    UPDATE llm_semantic_cache
    SET hit_count = hit_count + 1
    WHERE id = cache_id;
END;
$$;

-- Cleanup old entries (run daily or add as pg_cron job)
-- DELETE FROM llm_semantic_cache WHERE expires_at < now();
"""


# ─── Singleton ────────────────────────────────────────────────────────────────

_semantic_cache_instance: Optional[SemanticLLMCache] = None


def get_semantic_cache(
    supabase_client=None,
    openai_client=None,
) -> SemanticLLMCache:
    """Get singleton SemanticLLMCache instance."""
    global _semantic_cache_instance
    if _semantic_cache_instance is None:
        _semantic_cache_instance = SemanticLLMCache(
            supabase_client=supabase_client,
            openai_client=openai_client,
        )
    return _semantic_cache_instance
