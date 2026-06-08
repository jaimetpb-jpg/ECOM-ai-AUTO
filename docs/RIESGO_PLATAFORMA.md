# RIESGO_PLATAFORMA.md — Playbook de Ban Meta/TikTok

> **Por qué este documento existe:** ningún sistema autónomo de ads sobrevive
> sin un plan para cuando una cuenta cae. El ban no es "si" — es "cuándo".
> Este documento se actualiza cada quarter y se ensaya con simulacro.

---

## 1. Pre-mortem: ¿qué riesgos tenemos?

### Meta (Facebook/Instagram Ads)
| Causa típica de ban | Severidad |
|---|---|
| Volumen anómalo de creación de ads (típico en sistemas automáticos) | Alta |
| Creatives que violan política (claims médicos, "before/after") | Alta |
| Tarjeta de crédito declinada repetidas veces | Media |
| URL del landing en blacklist | Alta |
| Pixel disparando eventos sospechosos | Media |
| Política manual ML del lado de Meta (sin razón visible) | Alta |

### TikTok Ads
| Causa típica de ban | Severidad |
|---|---|
| Cuenta nueva sin warming gastando >$100/día | Alta |
| Mismo creative reciclado entre cuentas | Alta |
| Geolocalización inconsistente del proxy/IP | Media |
| Audiencia/edad mal segmentada (TikTok es estricto con <18) | Alta |
| Producto en categoría prohibida (incluso si parece ok) | Crítica |

---

## 2. Mitigación PREVENTIVA (hoy, antes de gastar más)

### Inventario de cuentas
Documentar EN UN SOLO LUGAR (Notion / Airtable):

```
META BUSINESS MANAGERS DISPONIBLES:
  BM #1: jp@empresa.com    | Verified | 3 cuentas | $500/día spend cap
  BM #2: ulises@empresa.com| Verified | 2 cuentas | $200/día spend cap
  BM #3: backup@gmail.com  | Pending  | 0 cuentas | (preparar)

TIKTOK ADS MANAGERS:
  Account #1: brand_main      | Warmed 6m | $300/día
  Account #2: brand_secondary | Warmed 2m | $100/día
  Account #3: testing_alt     | New       | $30/día
```

**Meta para reducir riesgo:** mínimo 2 BMs activos, 1 en warming, 1 dormido como
seguro. Nunca dependas de uno solo.

### Warming de cuentas nuevas
- Semana 1: gastar manualmente $5-10/día. NO usar el sistema automatizado.
- Semana 2-3: gastar $20-30/día. Sistema automatizado solo para optimización,
  no para creación masiva.
- Semana 4+: sistema completamente automatizado.

### Pixel y datos
- **Backup diario del Pixel ID y eventos:** export desde Events Manager.
- **Conversions API (CAPI):** configurar paralelamente al Pixel. Si el Pixel
  muere, CAPI sigue. CRÍTICO para resistencia.
- **Server-side tracking propio:** mantener tu propio event log en Supabase.
  Si Meta te banea, todavía sabes qué pasó.

### Creatives
- Antes de subir, pasar por checklist anti-rejection:
  - [ ] Sin claims médicos absolutos ("cura", "elimina")
  - [ ] Sin before/after explícito (categorías de salud/belleza)
  - [ ] Sin texto en imagen > 20%
  - [ ] Sin clickbait extremo ("doctores odian este truco")
  - [ ] Audio con licencia clara (TikTok especialmente)
  - [ ] CTA neutral ("aprende más" mejor que "compra ya")

---

## 3. Detección TEMPRANA (antes de que pase la cosa fea)

### Señales tempranas de ban inminente
| Señal | Acción inmediata |
|---|---|
| Reach cae 60%+ sin razón obvia en 24h | Pausar nuevos ads, abrir caso soporte |
| Ads rechazados >30% en una semana | Revisar política, frenar volumen |
| Charges declinados 2+ veces | Verificar tarjeta, NO retry automático |
| Recibes email de "policy review" | Detener TODO en esa cuenta, leer despacio |
| Account quality score baja a "average"/"low" | 3-4 días de freeze |

### Monitoreo automático sugerido
```python
# Añadir a monitoring/metrics_collector.py
async def detect_account_health(meta_account_id: str) -> dict:
    # GET /act_{id}/account_status
    # Alertar Slack si:
    #   - disable_reason != None
    #   - account_status != 1 (active)
    #   - amount_spent / spend_cap > 0.85
    ...
```

---

## 4. Respuesta a ban (cuando ya pasó)

### Hora 0 — Triaje (15 min)
1. **NO entrar en pánico, NO crear cuenta nueva inmediatamente.** Eso te marca.
2. Documentar QUÉ se vio (screenshot del ban + email).
3. **Pausar TODO en otras cuentas relacionadas** (mismo BM, misma tarjeta).
   Meta correlaciona y el contagio es real.
4. Notificar a Slack #alerts con severidad CRITICAL.

### Hora 0-4 — Diagnóstico
1. ¿Ban es de cuenta personal, BM, o ad account? Cada uno es diferente.
2. ¿Hay opción de apelación visible? (a veces no la hay para violaciones graves).
3. Si hay apelación: enviarla en máximo 24h con tono profesional y datos
   verificables. NO intentar varias veces — una sola apelación bien escrita.

### Día 1-7 — Continuidad operativa
1. **Activar BM dormido** (el #3 del inventario).
2. Migrar pixel events vía CAPI a la nueva propiedad.
3. **Resetear warming:** la nueva cuenta NO empieza en $300/día. Empieza en $30.
4. Notificar a stakeholders (Slack, email a JP/Ulises) con timeline esperado.

### Semana 2+ — Lecciones aprendidas
1. Postmortem documentado en `INCIDENTS/YYYY-MM-DD-meta-ban.md`.
2. Análisis: ¿qué creative/copy/landing disparó el ban?
3. Actualizar checklist anti-rejection si aplica.
4. Decidir si la cuenta vieja es recuperable o se da por muerta.

---

## 5. Reglas DURAS del sistema

Estas reglas deben vivir en `shared/constants.py` y el código debe respetarlas:

```python
# scaling/meta_ads.py — NUEVO
MAX_NEW_CAMPAIGNS_PER_HOUR  = 5   # No crear más de 5 campañas/hora por cuenta
MAX_NEW_ADS_PER_HOUR        = 20  # No crear más de 20 ads/hora por cuenta
MIN_TIME_BETWEEN_BUDGETS_UP = 6   # Horas entre subidas de budget (Meta no le gusta)
MAX_BUDGET_INCREASE_PCT     = 30  # Nunca subir budget >30% de golpe

# Si el sistema intenta saltarse esto, debe BLOQUEARSE y alertar Slack
```

Estas reglas valen su peso en oro. Meta ML detecta "ad campaign creation
velocity" como señal #1 de bot/spam. Respetar ritmo humano es supervivencia.

---

## 6. Recursos y cuentas de emergencia

> Mantener esta sección actualizada y backupeada.

- **Soporte directo Meta:** https://www.facebook.com/business/help/contact
  (requiere Meta Business Partner status para soporte humano rápido)
- **TikTok Ads support:** chat in-app, solo cuentas con spend > $1k/mes
- **Backup credit cards:** mínimo 2 tarjetas diferentes por BM
- **Backup phone numbers:** SIMs reales, NO virtual numbers (Meta detecta)
- **Backup KYC docs:** scanned, ready to upload en apelaciones

---

## 7. Simulacro trimestral

Una vez por trimestre, ejecutar **drill** controlado:
1. Pausar tu BM principal a propósito por 2 horas.
2. Activar el BM dormido.
3. Migrar las 3 campañas top.
4. Medir: ¿cuánto tiempo desde "pausa" hasta "primera impresión nueva"?
5. Documentar fricciones. La meta es <4 horas para migración total.

Si en el drill descubres que tu BM dormido no tiene business verification, o
no tiene método de pago, o el equipo no sabe los logins — esa es exactamente
la información que necesitabas ANTES del incidente real.
