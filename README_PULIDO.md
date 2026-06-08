# 📦 Paquete de Pulido — V5.2

Este paquete contiene los **5 entregables concretos** para arrancar el
Sprint 0 (Pulido pre-Sprint 1). Pegar al root de tu repo.

## Contenido

```
pulido_v52/
├── README_PULIDO.md                       (este archivo)
├── STATUS_REAL.md                          ← #1 más importante. Llenar día 1.
├── PLAN_PULIDO_SPRINT0.md                  ← plan ejecutivo 10 días
├── RIESGO_PLATAFORMA.md                    ← playbook ban Meta/TikTok
├── scripts/
│   └── smoke_test_real.py                  ← primer test con $0.10 reales
├── shared/
│   └── financial_metrics.py                ← MER, TACOS que faltan en código
└── tests/unit/
    └── test_financial_metrics.py           ← 10 tests SIN mocks
```

## Cómo aplicar al repo

```bash
# Desde la raíz de tu repo v52_final/
cp -r pulido_v52/STATUS_REAL.md ./
cp -r pulido_v52/PLAN_PULIDO_SPRINT0.md ./docs/
cp -r pulido_v52/RIESGO_PLATAFORMA.md ./docs/
cp pulido_v52/scripts/smoke_test_real.py ./scripts/
cp pulido_v52/shared/financial_metrics.py ./shared/
cp pulido_v52/tests/unit/test_financial_metrics.py ./tests/unit/

# Verificar
make test  # debe incluir los 10 nuevos tests
python scripts/smoke_test_real.py --tier bulk  # gratis, solo Groq
python scripts/smoke_test_real.py             # ~$0.10 USD
```

## Orden de ejecución sugerido

**Día 1 (hoy):**
1. Llenar `STATUS_REAL.md` con honestidad cruda. NO embellecer.
2. Correr `python scripts/smoke_test_real.py --tier bulk` para confirmar Groq.
3. Leer `RIESGO_PLATAFORMA.md` con Ulises, llenar inventario sección 2.

**Día 2-5:**
4. Seguir `PLAN_PULIDO_SPRINT0.md` día por día.
5. Migrar las 3 llamadas con regex (ver código sugerido en feedback de Claude).
6. Conectar `financial_metrics.py` al ads_decision_engine.

**Día 6-10:**
7. Tests reales en CI.
8. Hetzner producción.
9. Kickoff Sprint 1 oficial.

## Lo que este paquete NO incluye (a propósito)

- **Migración completa de oracle/agents.py:** el patch va en feedback de Claude.
  Demasiado contextual para entregarlo "drop-in".
- **CircuitBreaker en meta_ads.py:** mismo motivo. Es un wrap de 5 líneas
  contextual al código actual.
- **Webhooks:** alcance del Sprint 3-4, no pre-Sprint.
- **Decisiones de producto:** SaaS pricing, umbrales $50, política HITL. Son
  decisiones de JP + Ulises, no de código.

## Filosofía de este pulido

> "Es mucho más barato perder un pitch por reconocer un gap, que perder un
> cliente o un inversor por ocultarlo."

Tu sistema técnico es bueno. La arquitectura LLM Router + Budget Governor +
CircuitBreaker + Thompson Sampling + Hierarchical Bayesian es real y
defendible. Lo que estamos puliendo es la disciplina narrativa, no la
calidad del trabajo del equipo.

Suerte con el Sprint 0. 🚀
