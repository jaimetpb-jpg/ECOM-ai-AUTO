"""
shared/llm_router.py — 4-Tier LLM Cost Optimizer V5.2

V5.2 Upgrades (all backwards-compatible):
  1. BudgetGovernor integration → hard daily limits, prevent runaway costs
  2. route_structured() → Structured Outputs (tool_use/json_schema) — ZERO regex
  3. route_cached() → SemanticLLMCache before LLM call
  4. get_full_stats() → unified health reporting

Tiers (unchanged):
  bulk      → Groq Llama 3.3 70B   (~$0 free)
  ops       → Claude Haiku 4.5     ($0.0008/1K)
  creative  → GPT-4o Mini          ($0.0015/1K)
  strategic → Claude Sonnet 4.6    ($0.015/1K)
"""

import os
import time
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel

from shared.logging_utils import log_info, log_warning, log_error
from shared.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

MODEL_MAP = {
    "bulk":      "llama-3.3-70b-versatile",
    "ops":       "claude-haiku-4-5-20251001",
    "creative":  "gpt-4o-mini",
    "strategic": "claude-sonnet-4-6",
}


class LLMUsageTracker:
    """Tracks cost across all LLM calls. Rolling window of last 1000 calls."""

    COSTS_PER_TOKEN = {
        "bulk":      0.0,
        "ops":       0.0008 / 1000,
        "creative":  0.0015 / 1000,
        "strategic": 0.015  / 1000,
    }
    MAX_CALLS = 1000

    def __init__(self):
        self.calls: list = []

    def record(self, tier: str, model: str, input_tokens: int, output_tokens: int) -> float:
        cost = (input_tokens + output_tokens) * self.COSTS_PER_TOKEN.get(tier, 0)
        self.calls.append({
            "tier": tier, "model": model,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost_usd": cost, "ts": time.time(),
        })
        if len(self.calls) > self.MAX_CALLS:
            self.calls = self.calls[-self.MAX_CALLS:]
        return cost

    def summary(self) -> dict:
        total = sum(c["cost_usd"] for c in self.calls)
        by_tier: dict = {}
        for c in self.calls:
            t = c["tier"]
            by_tier.setdefault(t, {"calls": 0, "cost_usd": 0.0})
            by_tier[t]["calls"] += 1
            by_tier[t]["cost_usd"] += c["cost_usd"]
        return {
            "total_calls": len(self.calls),
            "total_cost_usd": round(total, 4),
            "by_tier": {k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in by_tier.items()},
            "note": "Stats for last 1000 calls (rolling window)" if len(self.calls) == self.MAX_CALLS else None,
        }


class LLMRouter:
    """
    Route LLM calls to the cheapest appropriate model.

    V5.2: BudgetGovernor + Structured Outputs + SemanticCache.
    All V5.1 usage (router.route()) remains 100% unchanged.
    """

    def __init__(self, budget_governor=None, semantic_cache=None):
        self.tracker = LLMUsageTracker()
        self._anthropic = None
        self._openai    = None
        self._groq      = None
        self._budget_governor = budget_governor
        self._semantic_cache  = semantic_cache

        self.circuit_breakers = {
            "anthropic": CircuitBreaker(failure_threshold=5, timeout_seconds=60, name="anthropic"),
            "openai":    CircuitBreaker(failure_threshold=5, timeout_seconds=60, name="openai"),
            "groq":      CircuitBreaker(failure_threshold=3, timeout_seconds=30, name="groq"),
        }

    # ─── V5.1 route() — UNCHANGED ────────────────────────────────────────────

    async def route(
        self, tier: str, prompt: str,
        system: Optional[str] = None,
        max_tokens: int = 1500, temperature: float = 0.7,
    ) -> str:
        """Route to appropriate LLM tier. V5.2 adds budget guard."""
        await self._budget_check(tier, prompt, max_tokens)

        for attempt in range(3):
            try:
                if tier == "bulk":
                    try:
                        return await self.circuit_breakers["groq"].call(
                            self._call_groq, prompt, system, max_tokens, temperature)
                    except CircuitBreakerOpenError:
                        log_warning(logger, "groq_circuit_open", fallback="haiku")
                        return await self._call_haiku(prompt, system, max_tokens, temperature)

                elif tier == "ops":
                    try:
                        return await self.circuit_breakers["anthropic"].call(
                            self._call_haiku, prompt, system, max_tokens, temperature)
                    except CircuitBreakerOpenError:
                        log_warning(logger, "anthropic_circuit_open_haiku", fallback="gpt-mini")
                        return await self._call_gpt_mini(prompt, system, max_tokens, temperature)

                elif tier == "creative":
                    try:
                        return await self.circuit_breakers["openai"].call(
                            self._call_gpt_mini, prompt, system, max_tokens, temperature)
                    except CircuitBreakerOpenError:
                        log_warning(logger, "openai_circuit_open", fallback="haiku")
                        return await self._call_haiku(prompt, system, max_tokens, temperature)

                elif tier == "strategic":
                    try:
                        return await self.circuit_breakers["anthropic"].call(
                            self._call_sonnet, prompt, system, max_tokens, temperature)
                    except CircuitBreakerOpenError:
                        log_warning(logger, "anthropic_circuit_open_sonnet", fallback="gpt-4o")
                        return await self._call_gpt_4o_fallback(prompt, system, max_tokens, temperature)

                else:
                    raise ValueError(f"Unknown tier: '{tier}'. Use: bulk | ops | creative | strategic")

            except ValueError:
                raise
            except CircuitBreakerOpenError:
                raise
            except Exception as e:
                if attempt == 2:
                    raise
                log_warning(logger, "llm_retry", attempt=attempt + 1, error=str(e))
                await asyncio.sleep(2 ** attempt)

    # ─── V5.2 route_structured() ─────────────────────────────────────────────

    async def route_structured(
        self, tier: str, prompt: str, schema: Dict[str, Any],
        system: Optional[str] = None,
        pydantic_model: Optional[Type[BaseModel]] = None,
        max_tokens: int = 1500,
    ) -> Dict[str, Any]:
        """
        Guaranteed structured JSON output via Anthropic tool_use / OpenAI json_schema.
        Replaces all regex parsing in creative_engine, discovery_engine, oracle.

        Tier routing:
          ops / strategic  → Anthropic tool_use (claude-haiku / claude-sonnet)
          creative / bulk  → OpenAI json_schema (gpt-4o-mini)

        IMPORTANT: bulk tier here uses gpt-4o-mini (NOT Groq) because Groq does not
        support json_schema strict mode. Use route_structured with bulk only when
        guaranteed JSON is required; otherwise use route() to stay on Groq ($0).

        Args:
            schema:         JSON Schema dict — use YourModel.model_json_schema()
            pydantic_model: Optional Pydantic model for post-validation

        Returns:
            dict — pure Python dict, 100% valid, zero regex needed.
        """
        await self._budget_check(tier, prompt, max_tokens)

        for attempt in range(3):
            try:
                if tier in ("ops", "strategic"):
                    result = await self._structured_anthropic(tier, prompt, schema, system, max_tokens)
                else:
                    # creative or bulk → OpenAI json_schema
                    result = await self._structured_openai(tier, prompt, schema, system, max_tokens)

                if pydantic_model and result:
                    result = pydantic_model(**result).model_dump()

                return result

            except ValueError:
                raise
            except Exception as e:
                if attempt == 2:
                    log_error(logger, "structured_output_failed", tier=tier, error=str(e))
                    raise
                log_warning(logger, "structured_output_retry", attempt=attempt + 1, error=str(e))
                await asyncio.sleep(2 ** attempt)

    async def _structured_anthropic(
        self, tier: str, prompt: str, schema: Dict[str, Any],
        system: Optional[str], max_tokens: int,
    ) -> Dict[str, Any]:
        """Anthropic tool_use → guaranteed dict output."""
        anthropic = self._get_anthropic()
        model = MODEL_MAP[tier]
        tools = [{
            "name": "structured_response",
            "description": "Return data in the specified structured format.",
            "input_schema": schema,
        }]
        kwargs: Dict[str, Any] = {
            "model": model, "max_tokens": max_tokens,
            "tools": tools,
            "tool_choice": {"type": "tool", "name": "structured_response"},
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        resp = await anthropic.messages.create(**kwargs)
        for block in resp.content:
            if block.type == "tool_use" and block.name == "structured_response":
                self.tracker.record(tier, model, resp.usage.input_tokens, resp.usage.output_tokens)
                return block.input
        raise ValueError("Anthropic tool_use returned no structured block")

    async def _structured_openai(
        self, tier: str, prompt: str, schema: Dict[str, Any],
        system: Optional[str], max_tokens: int,
    ) -> Dict[str, Any]:
        """OpenAI json_schema → guaranteed JSON output."""
        openai = self._get_openai()
        model = "gpt-4o-mini"
        messages: List[Dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = await openai.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "structured_response", "strict": True, "schema": schema},
            },
        )
        message = resp.choices[0].message
        if message.refusal:
            raise ValueError(f"OpenAI refused structured output request: {message.refusal}")
        content = message.content
        if not content:
            raise ValueError("OpenAI returned empty content for structured output request")
        parsed = json.loads(content)
        self.tracker.record("creative", model, resp.usage.prompt_tokens, resp.usage.completion_tokens)
        return parsed

    # ─── V5.2 route_cached() ─────────────────────────────────────────────────

    async def route_cached(
        self, tier: str, prompt: str,
        system: Optional[str] = None,
        max_tokens: int = 1500, temperature: float = 0.7,
    ) -> str:
        """Route with semantic cache check first. Falls back to route() on miss."""
        if self._semantic_cache:
            cached = await self._semantic_cache.get(tier, prompt)
            if cached is not None:
                log_info(logger, "semantic_cache_hit_router", tier=tier)
                return cached

        response = await self.route(tier, prompt, system, max_tokens, temperature)

        if self._semantic_cache:
            await self._semantic_cache.set(tier, prompt, response)

        return response

    # ─── Budget check helper ─────────────────────────────────────────────────

    async def _budget_check(self, tier: str, prompt: str, max_tokens: int) -> None:
        """Check budget governor before any LLM call. Raises BudgetExceededError if blocked."""
        if not self._budget_governor:
            return
        est_tokens = len(prompt.split()) * 1.3 + max_tokens
        est_cost = self._budget_governor.estimate_cost(tier, int(est_tokens))
        allowed = await self._budget_governor.check_and_record(tier, est_cost)
        if not allowed:
            from shared.budget_governor import BudgetExceededError
            log_warning(logger, "llm_call_blocked_budget", tier=tier, est_cost=round(est_cost, 6))
            raise BudgetExceededError(
                tier=tier, spent=est_cost,
                limit=self._budget_governor._limits.get(tier, 0),
            )

    # ─── Provider calls (V5.1 unchanged) ─────────────────────────────────────

    def _get_anthropic(self):
        if self._anthropic is None:
            from anthropic import AsyncAnthropic
            self._anthropic = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        return self._anthropic

    def _get_openai(self):
        if self._openai is None:
            from openai import AsyncOpenAI
            self._openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        return self._openai

    def _get_groq(self):
        if self._groq is None:
            from groq import AsyncGroq
            self._groq = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
        return self._groq

    async def _call_groq(self, prompt, system, max_tokens, temperature) -> str:
        try:
            groq = self._get_groq()
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            resp = await groq.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages, max_tokens=max_tokens, temperature=temperature,
            )
            self.tracker.record("bulk", "groq/llama-3.3-70b",
                                resp.usage.prompt_tokens, resp.usage.completion_tokens)
            return resp.choices[0].message.content
        except Exception as e:
            logger.warning(f"Groq failed, falling back to Haiku: {e}")
            return await self._call_haiku(prompt, system, max_tokens, temperature)

    async def _call_haiku(self, prompt, system, max_tokens, temperature) -> str:
        anthropic = self._get_anthropic()
        kwargs: Dict[str, Any] = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        resp = await anthropic.messages.create(**kwargs)
        self.tracker.record("ops", "claude-haiku-4-5",
                            resp.usage.input_tokens, resp.usage.output_tokens)
        return resp.content[0].text

    async def _call_gpt_mini(self, prompt, system, max_tokens, temperature) -> str:
        openai = self._get_openai()
        messages: List[Dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = await openai.chat.completions.create(
            model="gpt-4o-mini", messages=messages,
            max_tokens=max_tokens, temperature=temperature,
        )
        self.tracker.record("creative", "gpt-4o-mini",
                            resp.usage.prompt_tokens, resp.usage.completion_tokens)
        return resp.choices[0].message.content

    async def _call_sonnet(self, prompt, system, max_tokens, temperature) -> str:
        anthropic = self._get_anthropic()
        kwargs: Dict[str, Any] = {
            "model": "claude-sonnet-4-6",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        resp = await anthropic.messages.create(**kwargs)
        self.tracker.record("strategic", "claude-sonnet-4-6",
                            resp.usage.input_tokens, resp.usage.output_tokens)
        return resp.content[0].text

    async def _call_gpt_4o_fallback(self, prompt, system, max_tokens, temperature) -> str:
        openai = self._get_openai()
        messages: List[Dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = await openai.chat.completions.create(
            model="gpt-4o", messages=messages,
            max_tokens=max_tokens, temperature=temperature,
        )
        self.tracker.record("strategic", "gpt-4o-fallback",
                            resp.usage.prompt_tokens, resp.usage.completion_tokens)
        return resp.choices[0].message.content

    # ─── Stats ────────────────────────────────────────────────────────────────

    def get_usage_summary(self) -> dict:
        return self.tracker.summary()

    def get_circuit_breaker_stats(self) -> dict:
        return {name: cb.get_stats() for name, cb in self.circuit_breakers.items()}

    def get_budget_report(self) -> Optional[dict]:
        if self._budget_governor:
            return self._budget_governor.get_daily_report().to_dict()
        return None

    def get_semantic_cache_stats(self) -> Optional[dict]:
        if self._semantic_cache:
            return self._semantic_cache.get_stats()
        return None

    def get_full_stats(self) -> dict:
        stats = {
            "usage_summary":    self.get_usage_summary(),
            "circuit_breakers": self.get_circuit_breaker_stats(),
        }
        if budget := self.get_budget_report():
            stats["budget"] = budget
        if cache := self.get_semantic_cache_stats():
            stats["semantic_cache"] = cache
        return stats
