"""
shared/prompt_store.py — Prompt Version Store V5.2

Gestiona prompts versionados en Supabase con A/B testing nativo.
Permite cambiar, rollback y experimentar con prompts sin redeploy.

Problema que resuelve:
  Actualmente los prompts están hardcodeados en:
    - engines/creative_engine.py (HOOK_SYSTEM_PROMPT, HOOK_USER_TEMPLATE)
    - engines/discovery_engine.py
    - oracle/agents.py

  Si un prompt nuevo rompe el sistema → redeploy completo.
  Si quieres probar 2 prompts en paralelo → imposible sin cambiar código.
  Si el LLM provider lanza un nuevo modelo optimizado → redeploy.

Solución:
  - Prompts almacenados en tabla Supabase `prompts`
  - Python siempre lee de Supabase con cache local 10 min (rápido)
  - Fallback automático a constante hardcodeada si Supabase no disponible
  - A/B testing nativo: split traffic 50/50 entre variantes
  - Rollback instantáneo desde Supabase dashboard (sin redeploy)

Schema Supabase:
    CREATE TABLE prompts (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name        text NOT NULL,          -- identifier, e.g. 'creative_hooks'
        version     int NOT NULL DEFAULT 1,
        content     text NOT NULL,
        description text DEFAULT '',
        is_active   bool DEFAULT true,
        win_rate    float DEFAULT 0.0,       -- para A/B tracking
        impressions int DEFAULT 0,
        created_at  timestamptz DEFAULT now(),
        UNIQUE(name, version)
    );
    CREATE INDEX ON prompts (name, is_active);

Usage:
    store = PromptStore(supabase_client=db.supabase)

    # Get active prompt (with fallback)
    hook_prompt = await store.get("creative_hooks")

    # A/B test two variants
    prompt = await store.ab_test(
        name="creative_hooks",
        variant_a_version=1,
        variant_b_version=2,
        experiment_id="exp_001"
    )

    # Record which variant won (updates win_rate)
    await store.record_win("creative_hooks", version=2)
"""

import asyncio
import logging
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from shared.logging_utils import log_info, log_warning, log_error

logger = logging.getLogger(__name__)


# ─── Cache TTL ────────────────────────────────────────────────────────────────

CACHE_TTL_SECONDS = 600   # 10-minute local cache to avoid DB hit on every call


# ─── Fallback prompts (hardcoded safety net) ─────────────────────────────────
# These are used when Supabase is unavailable.
# They mirror the current hardcoded prompts in the engines.

FALLBACK_PROMPTS: Dict[str, str] = {
    "creative_hooks_system": (
        "Eres un experto en copywriting para TikTok Ads y Meta Ads.\n"
        "Generas hooks virales basados en psicología del consumidor.\n"
        "Tipos de hooks efectivos:\n"
        "  fear         — miedo a perder algo, problema sin resolver\n"
        "  transformation — antes/después, cambio de vida\n"
        "  social_proof — 'miles de personas ya...', testimonios implícitos\n"
        "  curiosity    — 'nadie te dice esto sobre...', secreto revelado\n"
        "  urgency      — escasez real, tiempo limitado con razón creíble\n\n"
        "IMPORTANTE: Solo responde con JSON válido. Sin markdown, sin explicaciones."
    ),
    "creative_hooks_user": (
        "Producto: {name}\nNicho: {niche}\nPain points del cliente: {pain_points}\n"
        "Precio aproximado: {price_hint}\nIdioma de los ads: {language}\n\n"
        "Genera exactamente 5 hooks virales. Responde SOLO con este JSON (sin markdown):\n"
        "[\n"
        "  {{\n"
        "    \"hook_type\": \"fear|transformation|social_proof|curiosity|urgency\",\n"
        "    \"hook_text\": \"texto del gancho máximo 10 palabras\",\n"
        "    \"script\": \"script completo de 30 segundos\",\n"
        "    \"cta\": \"llamada a la acción máximo 10 palabras\",\n"
        "    \"estimated_ctr\": 0.0,\n"
        "    \"pain_point_addressed\": \"qué dolor específico ataca este hook\"\n"
        "  }}\n"
        "]\n"
        "Ordena de mayor a menor estimated_ctr (0.01 a 0.12 rango realista)."
    ),
    "discovery_prefilter": (
        "Eres un analista de productos para ecommerce DTC.\n"
        "Evalúa candidatos de productos y elige los más rentables.\n"
        "Criterios: margen ≥40%, diferenciable, logística simple, sin riesgo legal.\n"
        "Responde SOLO con JSON. Sin markdown."
    ),
    "oracle_scoring": (
        "Eres un experto en análisis de oportunidades de ecommerce.\n"
        "Puntúa oportunidades de negocio con criterios objetivos.\n"
        "Considera: demanda, competencia, margen, viralidad.\n"
        "Responde SOLO con JSON estructurado."
    ),
    "brand_strategy": (
        "Eres un experto en branding DTC y marketing digital.\n"
        "Crea estrategias de marca concisas, memorables y diferenciadas.\n"
        "Responde SOLO con JSON. Sin markdown, sin explicaciones extra."
    ),
}


# ─── PromptStore ──────────────────────────────────────────────────────────────

class PromptStore:
    """
    Versioned prompt manager with A/B testing and instant rollback.

    Features:
    - Read prompts from Supabase with 10-min local cache (fast)
    - Fallback to hardcoded constants if DB unavailable
    - A/B testing: route % of traffic to new prompt versions
    - Win rate tracking for data-driven prompt optimization
    - Zero-downtime rollback: just flip `is_active` in Supabase dashboard

    Performance:
    - First call per prompt: 1 Supabase query (~20ms)
    - Subsequent calls within 10min: 0 network calls (local cache)
    - DB unavailable: instant fallback to FALLBACK_PROMPTS constant
    """

    def __init__(self, supabase_client=None):
        self._supabase = supabase_client
        self._lock = asyncio.Lock()

        # Local cache: {cache_key: (content, expires_ts)}
        self._cache: Dict[str, Tuple[str, float]] = {}

        # A/B experiment tracking: {experiment_id: {version: count}}
        self._ab_traffic: Dict[str, Dict[int, int]] = {}

        # Metrics
        self.cache_hits = 0
        self.db_reads = 0
        self.fallback_uses = 0
        self.ab_impressions = 0

    # ── Public API ────────────────────────────────────────────────────────────

    async def get(
        self,
        name: str,
        version: Optional[int] = None,
        fallback: Optional[str] = None,
    ) -> str:
        """
        Get prompt content by name.

        Args:
            name:     Prompt identifier (e.g., 'creative_hooks_system')
            version:  Specific version number. None = get active version.
            fallback: Override fallback if not in FALLBACK_PROMPTS dict.

        Returns:
            Prompt content string. Never raises.
        """
        async with self._lock:
            cache_key = f"{name}:v{version or 'active'}"

            # 1. Check local cache
            cached = self._get_cache(cache_key)
            if cached is not None:
                self.cache_hits += 1
                return cached

            # 2. Try Supabase
            if self._supabase:
                content = await self._fetch_from_db(name, version)
                if content:
                    self._set_cache(cache_key, content)
                    self.db_reads += 1
                    return content

            # 3. Fallback to hardcoded constant
            self.fallback_uses += 1
            result = (
                fallback
                or FALLBACK_PROMPTS.get(name)
                or f"[PROMPT NOT FOUND: {name}]"
            )
            if name not in FALLBACK_PROMPTS:
                log_warning(
                    logger, "prompt_not_found",
                    name=name, version=version, using_fallback=bool(fallback)
                )
            return result

    async def ab_test(
        self,
        name: str,
        variant_a_version: int,
        variant_b_version: int,
        traffic_pct_b: float = 0.5,
        experiment_id: Optional[str] = None,
    ) -> Tuple[str, int]:
        """
        Route traffic between two prompt versions for A/B testing.

        Args:
            name:              Prompt name
            variant_a_version: Control version
            variant_b_version: Test version
            traffic_pct_b:     Fraction of traffic to send to B (default 50%)
            experiment_id:     Optional tracking ID

        Returns:
            Tuple of (prompt_content, version_number_used)
        """
        self.ab_impressions += 1
        exp_id = experiment_id or f"{name}_ab"

        # Split traffic
        use_b = random.random() < traffic_pct_b
        selected_version = variant_b_version if use_b else variant_a_version

        # Track
        if exp_id not in self._ab_traffic:
            self._ab_traffic[exp_id] = {}
        self._ab_traffic[exp_id][selected_version] = (
            self._ab_traffic[exp_id].get(selected_version, 0) + 1
        )

        content = await self.get(name, version=selected_version)

        log_info(
            logger, "ab_test_impression",
            name=name, version=selected_version,
            experiment=exp_id,
            variant="B" if use_b else "A",
        )
        return content, selected_version

    async def record_win(
        self,
        name: str,
        version: int,
        metric_value: Optional[float] = None,
    ) -> None:
        """
        Record a conversion/win for a specific prompt version.
        Updates win_rate in Supabase for data-driven optimization.
        """
        if not self._supabase:
            return
        try:
            # Increment win count and update win_rate
            self._supabase.rpc(
                "increment_prompt_win",
                {"prompt_name": name, "prompt_version": version}
            ).execute()

            log_info(
                logger, "prompt_win_recorded",
                name=name, version=version, metric=metric_value
            )
        except Exception as e:
            log_warning(logger, "prompt_win_record_error", error=str(e))

    async def list_versions(self, name: str) -> List[Dict[str, Any]]:
        """List all versions of a prompt with their stats."""
        if not self._supabase:
            return [{"name": name, "source": "fallback", "content": FALLBACK_PROMPTS.get(name, "")}]
        try:
            result = (
                self._supabase.table("prompts")
                .select("id, name, version, description, is_active, win_rate, impressions, created_at")
                .eq("name", name)
                .order("version", desc=True)
                .execute()
            )
            return result.data or []
        except Exception as e:
            log_warning(logger, "prompt_list_error", error=str(e))
            return []

    async def upsert(
        self,
        name: str,
        content: str,
        version: Optional[int] = None,
        description: str = "",
        activate: bool = False,
    ) -> bool:
        """
        Create or update a prompt version in Supabase.

        Args:
            name:        Prompt name
            content:     Prompt text
            version:     Version number (auto-increments if None)
            description: What changed in this version
            activate:    Set as active version immediately

        Returns True on success.
        """
        if not self._supabase:
            log_warning(logger, "prompt_upsert_no_supabase", name=name)
            return False
        try:
            # Get next version if not specified
            if version is None:
                existing = await self.list_versions(name)
                version = (max(v["version"] for v in existing) + 1) if existing else 1

            row = {
                "name": name,
                "version": version,
                "content": content,
                "description": description,
                "is_active": activate,
            }

            self._supabase.table("prompts").upsert(row).execute()

            # Deactivate other versions if this one is being activated
            if activate:
                self._supabase.table("prompts").update(
                    {"is_active": False}
                ).eq("name", name).neq("version", version).execute()

            # Invalidate cache
            self._invalidate_cache(name)

            log_info(
                logger, "prompt_upserted",
                name=name, version=version, activated=activate
            )
            return True

        except Exception as e:
            log_error(logger, "prompt_upsert_error", name=name, error=str(e))
            return False

    def get_ab_stats(self) -> Dict[str, Any]:
        """Return A/B test traffic distribution."""
        return {
            "total_impressions": self.ab_impressions,
            "experiments": self._ab_traffic,
            "cache_hits": self.cache_hits,
            "db_reads": self.db_reads,
            "fallback_uses": self.fallback_uses,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _fetch_from_db(
        self, name: str, version: Optional[int]
    ) -> Optional[str]:
        """Fetch prompt from Supabase."""
        try:
            query = self._supabase.table("prompts").select("content").eq("name", name)

            if version is not None:
                query = query.eq("version", version)
            else:
                query = query.eq("is_active", True)

            result = query.limit(1).execute()

            if result.data:
                return result.data[0]["content"]
            return None

        except Exception as e:
            log_warning(logger, "prompt_db_fetch_error", name=name, error=str(e))
            return None

    def _get_cache(self, key: str) -> Optional[str]:
        entry = self._cache.get(key)
        if entry and entry[1] > time.time():
            return entry[0]
        if entry:
            del self._cache[key]
        return None

    def _set_cache(self, key: str, content: str) -> None:
        self._cache[key] = (content, time.time() + CACHE_TTL_SECONDS)

    def _invalidate_cache(self, name: str) -> None:
        """Remove all cache entries for a prompt name."""
        keys_to_del = [k for k in self._cache if k.startswith(f"{name}:")]
        for k in keys_to_del:
            del self._cache[k]


# ─── Supabase SQL setup ───────────────────────────────────────────────────────

SUPABASE_PROMPTS_SQL = """
-- Run once in Supabase SQL editor

CREATE TABLE IF NOT EXISTS prompts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    version     int NOT NULL DEFAULT 1,
    content     text NOT NULL,
    description text DEFAULT '',
    is_active   bool DEFAULT true,
    win_rate    float DEFAULT 0.0,
    impressions int DEFAULT 0,
    created_at  timestamptz DEFAULT now(),
    UNIQUE(name, version)
);

CREATE INDEX IF NOT EXISTS prompts_name_active_idx ON prompts (name, is_active);

-- RPC: increment wins for A/B tracking
CREATE OR REPLACE FUNCTION increment_prompt_win(
    prompt_name text,
    prompt_version int
)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    UPDATE prompts
    SET
        win_rate = (win_rate * impressions + 1) / NULLIF(impressions + 1, 0),
        impressions = impressions + 1
    WHERE name = prompt_name AND version = prompt_version;
END;
$$;
"""


# ─── Singleton ────────────────────────────────────────────────────────────────

_prompt_store_instance: Optional[PromptStore] = None


def get_prompt_store(supabase_client=None) -> PromptStore:
    """Get singleton PromptStore instance."""
    global _prompt_store_instance
    if _prompt_store_instance is None:
        _prompt_store_instance = PromptStore(supabase_client=supabase_client)
    return _prompt_store_instance
