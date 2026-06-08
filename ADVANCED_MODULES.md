# 🧠 Arquitecturas Avanzadas — ecommerce-ai v4.3

## Nuevos Módulos en `intelligence/`

Esta versión agrega los 3 módulos de arquitectura avanzada al sistema existente.
Son **código funcional real**, no conversaciones.

---

## 1. `hierarchical_bayesian.py` — Hierarchical Bayesian Allocator

**Qué hace:**  
Motor de asignación con 3 niveles de inferencia:
- **Nivel Global:** prior compartido entre todos los tenants/clusters
- **Nivel Cluster:** prior por nicho × geo × rango precio (ej. `skincare_us_70plus`)
- **Nivel Campaña:** posterior individual que hereda del cluster

**Por qué es mejor que Thompson Sampling plano:**  
- Campaña nueva hereda prior del cluster (no arranca ciega)
- Cluster nuevo hereda prior global (efecto red: más clientes = mejores priors para todos)
- Shrinkage jerárquico reduce overfitting

**Uso rápido:**
```python
from intelligence.hierarchical_bayesian import HierarchicalBayesianAllocator

allocator = HierarchicalBayesianAllocator(tau=0.5)
allocator.register_cluster("skincare_us_premium", alpha_ctr=2.0, beta_ctr=18.0)
allocator.register_campaign("camp_001", "skincare_us_premium")
allocator.register_campaign("camp_002", "skincare_us_premium")

# Actualizar con datos reales
allocator.update_campaign("camp_001", impressions=5000, clicks=120, revenue=8000, spend=2000)

# Asignar presupuesto
allocation = allocator.allocate(["camp_001", "camp_002"], "skincare_us_premium", budget=3000)
# → {'camp_001': 2100.0, 'camp_002': 900.0}  (winner gana más)
```

---

## 2. `portfolio_optimization.py` — Portfolio Optimizer

**Qué hace:**  
Aplica teoría de portafolio de Markowitz al presupuesto publicitario.

**Objetivo:** `max E[ROAS] - λ × Var[ROAS]`

**Capas de riesgo incluidas:**
- Stop-loss dinámico por campaña (drawdown > límite → $0)
- Portfolio kill-switch si ROAS promedio < umbral
- Concentration limit (ninguna campaña > max_weight)
- Volatility penalty configurable (λ = risk_aversion)

**Diferencia clave vs. bandit puro:**  
El optimizer considera correlaciones entre campañas.  
Si tienes 3 campañas muy correlacionadas (todas de verano), el portfolio optimizer
distribuye mejor el riesgo que Thompson Sampling independiente.

**Uso rápido:**
```python
from intelligence.portfolio_optimization import PortfolioOptimizer, CampaignMetrics

opt = PortfolioOptimizer(
    risk_aversion=2.0,        # λ: mayor = más conservador
    max_drawdown_pct=0.20,    # Stop-loss si drawdown > 20%
    max_concentration=0.50,   # Ninguna campaña > 50% del budget
)

campaigns = [
    CampaignMetrics("c1", roas_history=[3.0, 3.5, 2.8, 4.0], spend=2000),
    CampaignMetrics("c2", roas_history=[2.0, 4.0, 1.5, 5.0], spend=2000),  # alta volatilidad
    CampaignMetrics("c3", roas_history=[4.0, 4.5, 3.8, 4.2], spend=2000),
]

allocation = opt.optimize(campaigns, total_budget=6000)
analytics = opt.get_portfolio_analytics()
```

---

## 3. `monte_carlo.py` — Monte Carlo Simulator

**Qué hace:**  
Simula 1,000+ trayectorias bajo 7 regímenes de mercado para validar parámetros
**antes de exponerlos a dinero real**.

**Regímenes simulados:**
| Régimen | Descripción |
|---------|-------------|
| `bull_market` | ROAS estable alto (escala rápida) |
| `bear_market` | ROAS bajo + alta volatilidad |
| `trending_up` | Producto nuevo que escala |
| `trending_down` | Saturación de nicho |
| `regime_change` | iOS update, competidor agresivo |
| `fat_tails` | Black Friday + crashes |
| `seasonal` | Ciclos mensuales |

**Qué encuentra automáticamente:**
- `optimal_stoploss`: ROAS threshold que minimiza ruin_rate
- `optimal_min_budget`: mínimo por arm que maximiza ROAS en cambio de régimen  
- `recommended_tau`: temperatura que maximiza P95 ROAS en bull

**Uso rápido:**
```python
from intelligence.monte_carlo import MonteCarloSimulator

sim = MonteCarloSimulator(n_trajectories=1000, n_periods=90, n_campaigns=5)
results = sim.run_full_analysis()

print(results.summary())
results.export("validation/monte_carlo_v1.json")

# Output:
# ✅ Stop-loss óptimo:      ROAS < 1.20 por 48h
# ✅ Budget mínimo/arm:     $50.00
# ✅ Temperatura τ (softmax): 0.50
```

**Para CI rápido (200 trayectorias):**
```bash
python intelligence/monte_carlo.py 200 validation/mc_quick.json
```

---

## Tests

```bash
# Correr todos los tests de arquitecturas avanzadas (17 tests)
python scripts/test_advanced_architectures.py

# Con pytest
pytest scripts/test_advanced_architectures.py -v
```

**Resultado esperado:** `17/17 passed | 0 failed`

---

## Secuencia de implementación recomendada

Según el análisis del documento de propuestas:

```
FASE 1 (Ahora) — Motor Financiero Core
├── ✅ Thompson Sampling (v4.2 existente)
├── ✅ hierarchical_bayesian.py  ← NUEVO
├── ✅ portfolio_optimization.py ← NUEVO
└── ✅ monte_carlo.py            ← NUEVO (para validar parámetros)

FASE 2 (2-3 meses) — Creative Intelligence
├── Embedding → performance regression
├── Creative decay modelling
└── Opportunity gap detection (usa priors del Bayesian)

FASE 3 (6+ meses) — Autonomous Loop
├── Generación creativa dirigida por vector objetivo
└── Budget inicial con prior informado
```

**Regla de oro:** No actives Fase 2 hasta que Fase 1 demuestre:
1. Drawdown promedio ≤ reducción de 20% vs. control humano
2. Capital efficiency ≥ +15%  
3. Tiempo de identificar ganador ≤ -30%

El Monte Carlo te dice con qué parámetros puedes lograr eso antes de gastar un dólar real.
