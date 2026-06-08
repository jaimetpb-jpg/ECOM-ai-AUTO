# STATUS REAL — AI E-Commerce System V5.2

> **Propósito de este documento:** ser la **única fuente de verdad** sobre qué
> ha corrido alguna vez con dinero, capital, o producción.
>
> **Regla:** si una funcionalidad no está marcada ✅ aquí abajo, en cualquier
> otro documento (DevGuide, README, deck de inversor) debe describirse como
> "implementado, **no validado en producción**" — NO como "production ready".
>
> Última actualización: __FECHA__ por __NOMBRE__

---

## 1. ¿Qué ha corrido con dinero real?

Si tu respuesta a alguna de estas preguntas es "no", marca ❌ honestamente.
Es mucho mejor reconocerlo internamente que descubrirlo en un pitch.

| Funcionalidad | Estado | Evidencia (link a log/screenshot) | Fecha |
|---|---|---|---|
| Ciclo Oracle completo con LLM real (no mock) | ❌ / ✅ | | |
| Una campaña TikTok Ads de $1 real desde el sistema | ❌ / ✅ | | |
| Una campaña Meta Ads de $1 real desde el sistema | ❌ / ✅ | | |
| Una campaña Google Ads de $1 real desde el sistema | ❌ / ✅ | | |
| Un kill automático por ROAS<1.5 ejecutado en campaña real | ❌ / ✅ | | |
| Un mensaje WhatsApp de cart recovery enviado a número real | ❌ / ✅ | | |
| Un Human Gate aprobado por humano en Slack | ❌ / ✅ | | |
| Un video HeyGen generado y publicado a TikTok | ❌ / ✅ | | |
| Un producto pasado de validación → escala → primera venta real | ❌ / ✅ | | |
| Un ciclo completo de 6h ejecutado en producción 7 días seguidos | ❌ / ✅ | | |
| Budget Governor ha detenido alguna llamada real por exceder cap | ❌ / ✅ | | |
| Circuit Breaker se ha abierto en producción al menos una vez | ❌ / ✅ | | |

---

## 2. Métricas reales operadas

> Llenar solo con datos reales. Si la respuesta es "estimado", déjala vacía.

- **Costo LLM real promedio/día últimos 7 días:** $______
- **Productos ranqueados por Oracle con datos reales:** ______ (no mocks)
- **Campañas reales lanzadas total:** ______
- **ROAS promedio observado real (no simulado):** ______
- **Tasa de hit del Semantic Cache observada:** ____%
- **MER (Marketing Efficiency Ratio) últimos 7 días:** ______ ⚠️ Si vacío: NO ESTÁ CALCULADO EN CÓDIGO
- **TACOS últimos 7 días:** ______ ⚠️ Si vacío: NO ESTÁ CALCULADO EN CÓDIGO
- **Tiempo medio entre fail-fast trigger y campaña matada:** ______
- **Tenants SaaS reales activos:** ______ (no incluir tests internos)

---

## 3. Tests con LLM real (no mocks)

| Test | LLM real? | Costo aprox/run | Última corrida |
|---|---|---|---|
| test_pipeline_e2e.py | ❌ todos mocks | $0 | — |
| smoke_test_real.py (nuevo) | ✅ | ~$0.10 | __FECHA__ |
| test_engines_v51.py | ❌ todos mocks | $0 | — |
| test_circuit_breaker.py | ❌ mock | $0 | — |

**Meta razonable:** al menos 3 tests E2E deben costar dinero real cada vez que
corren en CI. Si todos cuestan $0, no estás testeando nada de tu sistema; solo
estás verificando que tu propia interfaz con tus propios mocks es consistente.

---

## 4. Gaps conocidos vs documentación oficial

> Lista honesta de lo que documentación dice que existe pero **no existe** o
> existe parcialmente.

- [ ] **MER < 1.4 kill-switch:** documentado, no implementado en código. Solo
      ROAS por campaña existe. `grep -r MER engines/ shared/ monitoring/` da 0.
- [ ] **TACOS > 30% emergency brake:** mismo caso. Documentado, no en código.
- [ ] **CrewAI 3 agentes en paralelo:** `requirements.txt` tiene `crewai` comentado.
      Las llamadas en `oracle/agents.py` usan `await router.route()` secuencial,
      no `Crew()` paralelo. La etiqueta "CrewAI" es aspiracional.
- [ ] **Webhooks Meta/TikTok:** `monitoring/metrics_collector.py:416-422`
      tiene `return campaign` (stubs). El sistema sigue siendo polling 6h.
- [ ] **Aislamiento multi-tenant SaaS:** `saas_spawn.py:152` admite "Using
      opportunities table as a proxy — in production add a tenants table".
      Sin tabla `tenants` con RLS, los planes SaaS no se pueden vender legalmente.
- [ ] **0% regex parseando JSON:** quedan ≥3 llamadas con `route()` + parsing.
      `oracle/agents.py:189, 207`, `engines/creative_engine.py:247`.
- [ ] **scaling/meta_ads.py con Circuit Breaker:** solo `try/except`.
- [ ] **Sonnet 4.6 ID exacto:** verificar que `claude-sonnet-4-6` y
      `claude-haiku-4-5-20251001` son IDs vigentes en API.

---

## 5. Decisiones de producto pendientes

- [ ] Validar umbral $50 con cálculo de potencia estadística. ¿Cuántos clics
      mínimos necesitamos por creative para que el Thompson Sampling no haga
      decisiones a partir de ruido?
- [ ] Definir tasa aceptable de falsos negativos (matar producto bueno) vs
      falsos positivos (escalar producto malo). Hoy es implícita.
- [ ] Decidir si SaaS Spawn se pospone a V6.0 o si invertimos 4-6 semanas
      en hacerlo aislamiento real (tabla tenants + RLS + per-tenant budget).
- [ ] Playbook de respuesta a ban de cuenta Meta/TikTok. NO existe documentado.
- [ ] Política de compliance: GDPR (datos UE), CCPA (California), refunds,
      disclosure de IA en ads (algunas jurisdicciones lo exigen 2026).

---

## 6. Reglas de actualización de este documento

1. Cada PR que toca producción debe actualizar este archivo.
2. Sprint review debe revisarlo antes que cualquier otro doc.
3. Antes de cualquier pitch externo (inversor, cliente, prensa), revisar este
   archivo y solo presentar lo que esté marcado ✅.
4. Si descubres que un ✅ no es cierto, bájalo a ❌ en el mismo commit en que
   lo descubres. No "lo arreglo y luego subo el check". Honestidad operativa.
