"""
shared/llm_batch_client.py — Anthropic Batch API Client V5.2

Reduce costos LLM un 50% usando la Batch API de Anthropic para
operaciones de scoring masivo (30-200 productos por ciclo Oracle).

Por qué importa:
  Ciclo actual: 50 productos × llamadas individuales a Haiku
  → cada llamada: ~500 tokens × $0.0008/1K = $0.0004
  → 50 llamadas = $0.02 por ciclo, ~$0.12/hora (24h × 6 ciclos)

  Con Batch API: mismas 50 llamadas con 50% descuento automático
  → $0.01 por ciclo → $0.06/hora → -50% en scoring masivo.

  A escala (200 productos, 6 ciclos/día, 30 días):
  → Sin batch:  200 × 6 × 30 × $0.0004 = $14.40/mes
  → Con batch:  200 × 6 × 30 × $0.0002 = $7.20/mes
  → Ahorro:     $7.20/mes por solo usar Batch API.

Cómo funciona:
  1. Agrupar N requests en un MessageBatch
  2. La API las procesa asíncronamente (generalmente < 60s para N < 200)
  3. Retornar resultados mapeados al mismo orden que el input
  4. Fallback automático a llamadas individuales si el batch falla

Uso en oracle/agents.py + scoring/engine.py:
    batch_client = AnthropicBatchClient()
    results = await batch_client.batch_score_products(
        products=candidate_list,
        system_prompt=SCORING_SYSTEM_PROMPT,
    )

Limitaciones conocidas:
  - No soporta streaming
  - Max 10,000 requests por batch (nunca llegaremos)
  - Tiempo de respuesta variable (5s–2min) — no usar para real-time
  - Solo Claude models (no GPT, no Groq)
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Callable

from shared.logging_utils import log_info, log_warning, log_error

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

BATCH_POLL_INTERVAL_S  = 5     # Poll every 5s
BATCH_MAX_WAIT_S       = 180   # Wait max 3 minutes before fallback
BATCH_MAX_SIZE         = 200   # Max items per batch (conservative, API max is 10k)
DEFAULT_BATCH_MODEL    = "claude-haiku-4-5-20251001"  # Cheapest, fastest for batch
DEFAULT_BATCH_TOKENS   = 400   # Sufficient for scoring/classification tasks


# ─── AnthropicBatchClient ─────────────────────────────────────────────────────

class AnthropicBatchClient:
    """
    Anthropic Message Batches API wrapper with automatic fallback.

    Provides 50% cost reduction for bulk LLM operations (scoring, classification,
    pre-filtering) that don't require real-time responses.

    Example:
        client = AnthropicBatchClient()

        # Batch score 50 products at once
        results = await client.batch_score_products(
            products=products_list,
            system_prompt="Score this product opportunity 0-100.",
        )
        # → List[dict] same order as input, each with 'product_id' and 'response'

    Fallback behavior:
        If batch API unavailable or times out → automatic sequential fallback
        with individual Haiku calls (same cost as before, no failure).
    """

    def __init__(self, model: str = DEFAULT_BATCH_MODEL):
        self.model = model
        self._client = None

        # Metrics
        self.batches_submitted = 0
        self.batches_completed = 0
        self.batches_failed = 0
        self.items_processed = 0
        self.fallback_calls = 0
        self.total_cost_saved_usd = 0.0

    def _get_client(self):
        """Lazy-load Anthropic client."""
        if self._client is None:
            try:
                import anthropic
                import os
                self._client = anthropic.AsyncAnthropic(
                    api_key=os.getenv("ANTHROPIC_API_KEY")
                )
            except ImportError:
                raise RuntimeError(
                    "anthropic package not installed. Run: pip install anthropic>=0.49.0"
                )
        return self._client

    # ── Main public methods ───────────────────────────────────────────────────

    async def batch_score_products(
        self,
        products: List[Dict[str, Any]],
        system_prompt: str,
        prompt_builder: Optional[Callable[[Dict], str]] = None,
        max_tokens: int = DEFAULT_BATCH_TOKENS,
    ) -> List[Dict[str, Any]]:
        """
        Score a list of products using Batch API for -50% cost.

        Args:
            products:        List of product dicts (must have 'product_id' or 'id')
            system_prompt:   System prompt for scoring context
            prompt_builder:  Optional fn(product) → str. Default: JSON stringify.
            max_tokens:      Max tokens per response (default 400)

        Returns:
            List of dicts: [{'product_id': ..., 'response': ..., 'success': bool}]
            Same order as input.
        """
        if not products:
            return []

        # Chunk into batches of BATCH_MAX_SIZE
        all_results = []
        for chunk_start in range(0, len(products), BATCH_MAX_SIZE):
            chunk = products[chunk_start: chunk_start + BATCH_MAX_SIZE]
            chunk_results = await self._process_batch(
                items=chunk,
                system_prompt=system_prompt,
                prompt_builder=prompt_builder or self._default_prompt_builder,
                max_tokens=max_tokens,
            )
            all_results.extend(chunk_results)

        self.items_processed += len(products)
        return all_results

    async def batch_generate(
        self,
        requests: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        max_tokens: int = DEFAULT_BATCH_TOKENS,
    ) -> List[Dict[str, Any]]:
        """
        Generic batch generation for any list of requests.

        Args:
            requests: List of {'id': str, 'prompt': str} dicts
            system_prompt: Optional shared system prompt
            max_tokens: Max tokens per response

        Returns:
            List of {'id': str, 'response': str, 'success': bool}
        """
        items = [
            {
                "product_id": r.get("id", f"req_{i}"),
                "_prompt_override": r.get("prompt", ""),
            }
            for i, r in enumerate(requests)
        ]

        def prompt_builder(item: dict) -> str:
            return item.get("_prompt_override", "")

        return await self._process_batch(
            items=items,
            system_prompt=system_prompt or "",
            prompt_builder=prompt_builder,
            max_tokens=max_tokens,
        )

    # ── Internal batch processing ─────────────────────────────────────────────

    async def _process_batch(
        self,
        items: List[Dict],
        system_prompt: str,
        prompt_builder: Callable,
        max_tokens: int,
    ) -> List[Dict[str, Any]]:
        """Submit a single batch and wait for results."""
        batch_id_map = {}   # anthropic_custom_id → product_id + position
        batch_requests = []

        for i, item in enumerate(items):
            product_id = str(
                item.get("product_id") or item.get("id") or f"item_{i}"
            )
            custom_id = f"req_{i}_{uuid.uuid4().hex[:8]}"
            batch_id_map[custom_id] = {"product_id": product_id, "index": i}

            messages = [{"role": "user", "content": prompt_builder(item)}]
            request_params: Dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system_prompt:
                request_params["system"] = system_prompt

            batch_requests.append({
                "custom_id": custom_id,
                "params": request_params,
            })

        log_info(
            logger, "batch_submitting",
            count=len(batch_requests), model=self.model
        )

        try:
            client = self._get_client()
            batch = await client.messages.batches.create(requests=batch_requests)
            self.batches_submitted += 1

            # Poll until complete
            results = await self._poll_batch(batch.id, batch_id_map, len(items))
            self.batches_completed += 1

            # Estimate cost saved
            # Individual: N × avg_tokens × cost_per_token
            # Batch:      N × avg_tokens × cost_per_token × 0.5
            # Saved:      N × avg_tokens × cost_per_token × 0.5
            approx_tokens = len(items) * max_tokens
            cost_rate = 0.0008 / 1000  # Haiku rate
            self.total_cost_saved_usd += approx_tokens * cost_rate * 0.5

            log_info(
                logger, "batch_complete",
                batch_id=batch.id, items=len(items),
                cost_saved_usd=round(self.total_cost_saved_usd, 4),
            )
            return results

        except Exception as e:
            self.batches_failed += 1
            log_warning(
                logger, "batch_failed_fallback",
                error=str(e), items=len(items),
                fallback="sequential_individual_calls"
            )
            return await self._sequential_fallback(
                items, system_prompt, prompt_builder, max_tokens
            )

    async def _poll_batch(
        self,
        batch_id: str,
        id_map: Dict[str, Dict],
        expected_count: int,
    ) -> List[Dict[str, Any]]:
        """
        Poll batch until complete or timeout.
        Returns results in original order.
        """
        start_ts = time.time()
        results_by_index: Dict[int, Dict] = {}

        while True:
            elapsed = time.time() - start_ts

            if elapsed > BATCH_MAX_WAIT_S:
                log_warning(
                    logger, "batch_timeout",
                    batch_id=batch_id, elapsed_s=round(elapsed, 1),
                    timeout_s=BATCH_MAX_WAIT_S,
                )
                raise TimeoutError(
                    f"Batch {batch_id} timed out after {BATCH_MAX_WAIT_S}s"
                )

            try:
                client = self._get_client()
                batch = await client.messages.batches.retrieve(batch_id)

                if batch.processing_status == "ended":
                    # Collect results
                    async for result in await client.messages.batches.results(batch_id):
                        meta = id_map.get(result.custom_id, {})
                        idx = meta.get("index", 0)
                        pid = meta.get("product_id", result.custom_id)

                        if result.result.type == "succeeded":
                            response_text = result.result.message.content[0].text
                            results_by_index[idx] = {
                                "product_id": pid,
                                "response": response_text,
                                "success": True,
                            }
                        else:
                            # Error result from API
                            results_by_index[idx] = {
                                "product_id": pid,
                                "response": "",
                                "success": False,
                                "error": str(result.result),
                            }

                    # Return in original order
                    return [
                        results_by_index.get(i, {
                            "product_id": f"missing_{i}",
                            "response": "",
                            "success": False,
                        })
                        for i in range(expected_count)
                    ]

                # Not done yet — wait and retry
                log_info(
                    logger, "batch_polling",
                    batch_id=batch_id,
                    status=batch.processing_status,
                    elapsed_s=round(elapsed, 1),
                )
                await asyncio.sleep(BATCH_POLL_INTERVAL_S)

            except TimeoutError:
                raise
            except Exception as e:
                log_warning(logger, "batch_poll_error", error=str(e))
                await asyncio.sleep(BATCH_POLL_INTERVAL_S)

    async def _sequential_fallback(
        self,
        items: List[Dict],
        system_prompt: str,
        prompt_builder: Callable,
        max_tokens: int,
    ) -> List[Dict[str, Any]]:
        """
        Fallback: process items one by one with individual API calls.
        Same cost as before batch, but guarantees no data loss.
        """
        log_info(logger, "batch_sequential_fallback", items=len(items))
        results = []

        for i, item in enumerate(items):
            product_id = str(
                item.get("product_id") or item.get("id") or f"item_{i}"
            )
            try:
                client = self._get_client()
                messages = [{"role": "user", "content": prompt_builder(item)}]
                kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "messages": messages,
                }
                if system_prompt:
                    kwargs["system"] = system_prompt

                resp = await client.messages.create(**kwargs)
                response_text = resp.content[0].text
                self.fallback_calls += 1

                results.append({
                    "product_id": product_id,
                    "response": response_text,
                    "success": True,
                })

            except Exception as e:
                log_warning(
                    logger, "batch_fallback_item_failed",
                    product_id=product_id, error=str(e)
                )
                results.append({
                    "product_id": product_id,
                    "response": "",
                    "success": False,
                    "error": str(e),
                })

        return results

    @staticmethod
    def _default_prompt_builder(item: dict) -> str:
        """Default: JSON-serialize the item as the prompt."""
        safe = {
            k: v for k, v in item.items()
            if not k.startswith("_") and isinstance(v, (str, int, float, bool))
        }
        return json.dumps(safe, ensure_ascii=False)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return performance statistics."""
        return {
            "batches_submitted":    self.batches_submitted,
            "batches_completed":    self.batches_completed,
            "batches_failed":       self.batches_failed,
            "items_processed":      self.items_processed,
            "fallback_calls":       self.fallback_calls,
            "total_cost_saved_usd": round(self.total_cost_saved_usd, 4),
            "success_rate_pct": (
                round(self.batches_completed / self.batches_submitted * 100, 1)
                if self.batches_submitted > 0 else 100.0
            ),
        }


# ─── Singleton ────────────────────────────────────────────────────────────────

_batch_client_instance: Optional[AnthropicBatchClient] = None


def get_batch_client() -> AnthropicBatchClient:
    """Get singleton AnthropicBatchClient."""
    global _batch_client_instance
    if _batch_client_instance is None:
        _batch_client_instance = AnthropicBatchClient()
    return _batch_client_instance
