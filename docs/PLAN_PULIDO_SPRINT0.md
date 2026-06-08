# PLAN DE PULIDO — Sprint 0 (2 semanas antes del Sprint 1 oficial)

> Objetivo: cerrar la brecha entre "marketing del DevGuide" y "realidad del
> código" antes de que el equipo arranque los Sprints 1-10 oficiales.
>
> Duración: **10 días hábiles** (2 semanas)
> Personas: todo el equipo, ~6h/día efectivas

---

## Día 1-2 — Verdad operativa (JP + Jesus)

| Tarea | Responsable | Tiempo | Entregable |
|---|---|---|---|
| Llenar STATUS_REAL.md con la verdad cruda | JP + todo el equipo | 2h | STATUS_REAL.md commiteado |
| Quitar "Production Ready" del DevGuide PDF | JP | 1h | DevGuide v5.2.1 |
| Cambiar checks ✓ por "implementado, no validado" | JP | 2h | DevGuide v5.2.1 |
| Inventario de cuentas Meta/TikTok/Google | Ulises | 2h | RIESGO_PLATAFORMA.md sección 2 llena |
| Decidir: SaaS Spawn se pospone o se construye bien | JP + Jesus | 1h | Decisión documentada en STATUS_REAL.md |

**Criterio de éxito día 2:** un humano externo lee STATUS_REAL.md y entiende
exactamente en qué estado está el sistema. Sin sorpresas.

---

## Día 3-5 — Pulido técnico crítico (Ricardo + Javier)

| Tarea | Responsable | Tiempo | Entregable |
|---|---|---|---|
| Smoke test real (gasta $0.10) | Ricardo | 3h | scripts/smoke_test_real.py corre ✅ |
| Migrar oracle/agents.py:189 a route_structured() | Javier | 4h | PR + test sin mock |
| Migrar oracle/agents.py:207 a route_structured() | Javier | 3h | PR + test sin mock |
| Migrar creative_engine.py:247 a route_structured() | Javier | 3h | PR + test sin mock |
| Borrar `_parse_candidates()` y `_parse_and_merge_validation()` | Javier | 1h | -80 LOC |
| Limpiar `discovery_engine.py` (borrar llm_router unused) | Ricardo | 1h | PR |
| Añadir CircuitBreaker a scaling/meta_ads.py | Ricardo | 2h | PR |
| Implementar shared/financial_metrics.py (MER, TACOS) | Ricardo | 3h | módulo + 10 tests |
| Conectar financial_metrics al ads_decision_engine | Ricardo | 4h | PR con kill por MER |

**Criterio de éxito día 5:** smoke test pasa, ya no hay regex parseando JSON
de LLMs en ningún lado del código.

---

## Día 6-7 — Resiliencia operacional (Jhovany + Jose)

| Tarea | Responsable | Tiempo | Entregable |
|---|---|---|---|
| docker-compose probado en VPS Hetzner | Jhovany | 4h | URL pública con /health/deep verde |
| Backup automático de Supabase a S3/B2 | Jhovany | 3h | cron + restore test |
| Sandbox TikTok Ads + Meta Ads autenticando | Jose | 4h | tokens en .env funcionando |
| Crear 1 campaña sandbox $0.01 manualmente desde código | Jose | 3h | log de respuesta API en commit |
| Pixel + CAPI dual track configurado en 1 tienda dummy | Jose | 4h | eventos llegan a ambos |
| 2 BMs Meta como contingencia | Ulises | 2h | inventario actualizado |

**Criterio de éxito día 7:** sistema corre 24h sin caerse en Hetzner, y
existe contingencia documentada si una cuenta de ads cae.

---

## Día 8-9 — Tests con dinero real (Igor + Snayder + todos)

| Tarea | Responsable | Tiempo | Entregable |
|---|---|---|---|
| test_circuit_breaker_cascade (real, no stub) | Igor | 4h | PR mergeable |
| test_budget_governor_stops_runaway (real, no stub) | Igor | 4h | PR mergeable |
| test_oracle_to_score_with_real_llm.py (nuevo) | Snayder | 6h | E2E con LLM real |
| Probar primer ciclo Oracle completo con datos reales (sin ads) | Ricardo | 4h | log de 1 ciclo en STATUS_REAL.md |
| Verificar todos los tests E2E con LLM real | Igor | 4h | reporte de coverage |

**Criterio de éxito día 9:** al menos 5 tests del CI gastan dinero real cada
corrida. Coverage de tests sube de 18 a ≥25.

---

## Día 10 — Validación final y kickoff Sprint 1

| Tarea | Responsable | Tiempo |
|---|---|---|
| Reunión de equipo: presentar STATUS_REAL actualizado | JP | 1h |
| Reunión de equipo: revisión técnica del pulido | Jesus | 1h |
| Decidir umbrales $50 reales con cálculo de potencia | Ulises + Ricardo | 2h |
| Actualizar V52_SPRINT_BOARD.docx con realidades nuevas | JP | 2h |
| Kickoff Sprint 1 oficial con base sólida | Jesus | 2h |

---

## Métricas de éxito del Sprint 0

Al final de los 10 días debes poder decir, **honestamente**, todo esto:

- [x] STATUS_REAL.md existe y refleja la verdad
- [x] 0 llamadas con regex parseando JSON de LLMs
- [x] discovery_engine.py sin código muerto
- [x] CircuitBreaker en TODOS los wrappers de APIs externas
- [x] MER y TACOS calculados en código, no solo en marketing
- [x] Smoke test con LLM real pasando en CI
- [x] Al menos 5 tests gastan dinero real (~$0.50 USD/run)
- [x] docker-compose corriendo 24h en Hetzner sin caerse
- [x] Inventario de cuentas Meta/TikTok documentado
- [x] Playbook de ban Meta/TikTok escrito
- [x] DevGuide PDF actualizado: 0 "Production Ready" sin evidencia
- [x] Decisión documentada: SaaS Spawn → V6.0 o reconstrucción real

---

## Lo que NO está en este Sprint 0 (y por qué)

| Cosa | Por qué no ahora |
|---|---|
| Webhooks Meta/TikTok | Sprint 3-4 del board oficial, requiere infra ready |
| Brand Creator <2h | Sprint 7-8 oficial, no es bloqueante |
| SaaS Spawn real (con tenants table) | Decisión de producto, no técnica. Posponer. |
| Dashboard React | Snayder lo puede empezar en Sprint 1 oficial |
| Optimización pgvector | Premature, sin datos reales no sabes qué optimizar |
| Migración a webhooks | Tendrá su propio sprint, hoy polling 6h es ok para validar |

---

## Riesgo del Sprint 0

**Si en lugar de honestidad operativa, el equipo se siente atacado y
defensivo, todo este pulido falla.**

Sugerencia para JP: enmarcar el Sprint 0 como **"darnos cuenta de qué tan
sólido es nuestro sistema, para defenderlo bien"**, NO como "auditoría de
errores". El sistema técnico ES bueno. Lo que arreglamos es la narrativa, no
la calidad del trabajo del equipo.
