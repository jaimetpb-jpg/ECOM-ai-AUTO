"""
shared/bulkhead.py — Bulkhead Pattern V5.1
Aísla recursos para que un motor lento no paralice a los demás.

Complementa perfectamente al Circuit Breaker:
  Circuit Breaker → protege contra fallos externos (APIs caídas)
  Bulkhead        → protege contra fallos internos (un engine lento consume todo)

Implementación: asyncio.Semaphore — zero overhead, 100% async-native.

Aportación: Grok (V5.1 Sinergia)

Configuración recomendada (Silicon Valley tuning):
  llm_anthropic   → max_concurrent=15  (rate limit Anthropic)
  llm_openai      → max_concurrent=20  (rate limit OpenAI)
  llm_groq        → max_concurrent=30  (Groq es más permisivo)
  engine_creative → max_concurrent=8   (evitar explosión de costos LLM)
  engine_ads      → max_concurrent=12  (campañas en paralelo)
  orchestrator    → max_concurrent=12  (productos procesados en paralelo)
"""

import asyncio
import logging
from typing import Dict, Any, Optional
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class Bulkhead:
    """
    Aislamiento de recursos por componente usando asyncio.Semaphore.

    Previene que una tarea lenta (e.g. Creative Engine con LLM timeout de 45s)
    bloquee el procesamiento de otras tareas críticas (e.g. Ads Kill-Switch).

    Example:
        bh = Bulkhead("engine_creative", max_concurrent=8)
        async with bh.acquire(timeout_seconds=45):
            hooks = await generate_hooks(product)
    """

    def __init__(self, name: str, max_concurrent: int = 10):
        self.name = name
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

        # Métricas (Prometheus/Grafana ready)
        self.total_acquired = 0
        self.total_rejected = 0
        self.total_wait_time = 0.0
        self.currently_in_use = 0

    @asynccontextmanager
    async def acquire(self, timeout_seconds: float = 30.0):
        """
        Context manager para adquirir slot del bulkhead.

        Args:
            timeout_seconds: Máximo tiempo de espera antes de rechazar.

        Raises:
            asyncio.TimeoutError: Si no hay slots disponibles en timeout_seconds.
        """
        loop = asyncio.get_event_loop()
        start = loop.time()

        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=timeout_seconds
            )
            self.total_acquired += 1
            self.currently_in_use += 1

            wait_time = loop.time() - start
            self.total_wait_time += wait_time

            if wait_time > 5.0:
                logger.warning(
                    f"bulkhead_slow_acquire name={self.name} "
                    f"wait={wait_time:.2f}s max={self.max_concurrent}"
                )
            yield

        except asyncio.TimeoutError:
            self.total_rejected += 1
            logger.error(
                f"bulkhead_rejected name={self.name} "
                f"queue_full=True max_concurrent={self.max_concurrent}"
            )
            raise asyncio.TimeoutError(
                f"Bulkhead '{self.name}' saturado ({self.max_concurrent} concurrentes)"
            )
        finally:
            # Solo liberar si se adquirió el semáforo
            if self.currently_in_use > 0:
                self._semaphore.release()
                self.currently_in_use = max(0, self.currently_in_use - 1)

    def get_stats(self) -> Dict[str, Any]:
        """Estadísticas para monitoreo (Prometheus/Grafana/Metabase)."""
        return {
            "name": self.name,
            "max_concurrent": self.max_concurrent,
            "currently_in_use": self.currently_in_use,
            "total_acquired": self.total_acquired,
            "total_rejected": self.total_rejected,
            "total_wait_time_seconds": round(self.total_wait_time, 2),
            "utilization_pct": round(
                (self.currently_in_use / max(self.max_concurrent, 1)) * 100, 2
            ),
        }


# ── Registro global (singleton style, igual que FeatureStore) ─────────────────
_bulkheads: Dict[str, Bulkhead] = {}


def get_bulkhead(name: str, max_concurrent: int = 10) -> Bulkhead:
    """
    Obtener o crear un bulkhead singleton por nombre.

    Example:
        bh = get_bulkhead("engine_creative", max_concurrent=8)
        async with bh.acquire():
            ...
    """
    global _bulkheads
    if name not in _bulkheads:
        _bulkheads[name] = Bulkhead(name, max_concurrent)
        logger.info(
            f"bulkhead_initialized name={name} max_concurrent={max_concurrent}"
        )
    return _bulkheads[name]


def get_all_bulkhead_stats() -> Dict[str, Dict[str, Any]]:
    """Obtener estadísticas de todos los bulkheads para /system/status."""
    return {name: bh.get_stats() for name, bh in _bulkheads.items()}
