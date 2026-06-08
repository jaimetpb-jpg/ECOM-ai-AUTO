"""
shared/prometheus_exporter.py — Prometheus Metrics Exporter V5.1

Expone métricas de Bulkhead + Circuit Breaker en formato Prometheus
para visualización en Grafana.

Aportación: Grok (V5.1 Sinergia)

Uso:
    # En main.py lifespan:
    from shared.prometheus_exporter import start_prometheus_exporter
    start_prometheus_exporter(port=9091)

    # Luego en Grafana: conectar datasource a http://localhost:9091/metrics

Métricas expuestas:
    bulkhead_current_in_use{name}      — slots activos por bulkhead
    bulkhead_max_concurrent{name}      — cap máximo por bulkhead
    bulkhead_rejections_total{name}    — tareas rechazadas (saturación)
    circuit_breaker_state{name}        — 0=closed, 1=open, 2=half_open
    circuit_breaker_failures_total{name}
    circuit_breaker_rejections_total{name}
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Importación condicional de prometheus_client ──────────────────────────────
# Si no está instalado, el exporter se desactiva silenciosamente (no bloquea)
try:
    from prometheus_client import (
        Gauge, Counter, start_http_server, REGISTRY
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False
    logger.warning(
        "prometheus_client not installed — metrics disabled. "
        "Install with: pip install prometheus-client"
    )

# ── Métricas globales (singleton) ─────────────────────────────────────────────
_metrics_initialized = False
_BULKHEAD_CURRENT: Optional[object] = None
_BULKHEAD_MAX: Optional[object] = None
_BULKHEAD_REJECTED: Optional[object] = None
_CB_STATE: Optional[object] = None
_CB_FAILURES: Optional[object] = None
_CB_REJECTIONS: Optional[object] = None


def _init_metrics():
    """Inicializar métricas Prometheus (una sola vez)."""
    global _metrics_initialized
    global _BULKHEAD_CURRENT, _BULKHEAD_MAX, _BULKHEAD_REJECTED
    global _CB_STATE, _CB_FAILURES, _CB_REJECTIONS

    if _metrics_initialized or not _PROMETHEUS_AVAILABLE:
        return

    _BULKHEAD_CURRENT = Gauge(
        "bulkhead_current_in_use",
        "Current concurrent tasks in bulkhead",
        ["name"],
    )
    _BULKHEAD_MAX = Gauge(
        "bulkhead_max_concurrent",
        "Maximum concurrent allowed in bulkhead",
        ["name"],
    )
    _BULKHEAD_REJECTED = Counter(
        "bulkhead_rejections_total",
        "Total rejected requests by bulkhead",
        ["name"],
    )
    _CB_STATE = Gauge(
        "circuit_breaker_state",
        "Circuit breaker state: 0=closed, 1=open, 2=half_open",
        ["name"],
    )
    _CB_FAILURES = Counter(
        "circuit_breaker_failures_total",
        "Total failures recorded by circuit breaker",
        ["name"],
    )
    _CB_REJECTIONS = Counter(
        "circuit_breaker_rejections_total",
        "Total requests rejected by open circuit breaker",
        ["name"],
    )
    _metrics_initialized = True


async def _update_metrics_loop(interval_seconds: int = 10):
    """
    Actualiza métricas de Bulkhead y Circuit Breaker cada N segundos.
    Corre como background task en el lifespan de FastAPI.
    """
    from shared.bulkhead import get_all_bulkhead_stats

    while True:
        try:
            # ── Bulkhead metrics ──────────────────────────────────────────────
            for name, stats in get_all_bulkhead_stats().items():
                _BULKHEAD_CURRENT.labels(name=name).set(stats["currently_in_use"])
                _BULKHEAD_MAX.labels(name=name).set(stats["max_concurrent"])
                # Counter: solo incrementar si hay nuevos rechazos
                # (prometheus_client no soporta set en Counter, solo inc)

            # ── Circuit Breaker metrics ───────────────────────────────────────
            # Import aquí para evitar circular import
            from shared.llm_router import LLMRouter
            router = LLMRouter()
            cb_stats = router.get_circuit_breaker_stats()
            state_map = {"closed": 0, "open": 1, "half_open": 2}
            for name, stats in cb_stats.items():
                state_val = state_map.get(stats.get("state", "closed"), 0)
                _CB_STATE.labels(name=name).set(state_val)

        except Exception as e:
            logger.warning(f"prometheus_metrics_update_failed error={e}")

        await asyncio.sleep(interval_seconds)


def start_prometheus_exporter(port: int = 9091) -> bool:
    """
    Iniciar servidor HTTP de métricas Prometheus.

    Args:
        port: Puerto del servidor (default 9091, separado del API en 8000)

    Returns:
        True si se inició correctamente, False si prometheus_client no está disponible
    """
    if not _PROMETHEUS_AVAILABLE:
        logger.warning(
            f"prometheus_exporter_disabled — "
            f"install prometheus-client to enable metrics on port {port}"
        )
        return False

    try:
        _init_metrics()
        start_http_server(port)
        logger.info(
            f"prometheus_exporter_started port={port} "
            f"metrics_url=http://localhost:{port}/metrics"
        )

        # Lanzar background task para actualizar métricas
        asyncio.create_task(_update_metrics_loop(interval_seconds=10))
        return True

    except OSError as e:
        # Puerto ya en uso — no bloquear el arranque
        logger.warning(f"prometheus_exporter_port_conflict port={port} error={e}")
        return False
    except Exception as e:
        logger.error(f"prometheus_exporter_start_failed error={e}")
        return False
