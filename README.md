# LIBERATION_SCORE Dashboard

Pagina web con grafico interactivo del componente **regime/daily** del sistema
`BATMAN_LIBERATION_TRIPLE` (SPX, DTE 60, snapshot 10:30 ET).

URL publica: https://manumartinb.github.io/LIBERATION_SCORE/

## Que muestra

- Linea principal: **TENSION_3WAY_MIN** (percentil 252d, minimo de los 3 subcomponentes)
- Bandas coloreadas: FAVORABLE (>=80), NEUTRAL (20-80), ADVERSO (<=20)
- 3 subcomponentes toggleables (legend click): curv 15-30-45, slope 10-40, skew 25-50
- Selector de rango: 30D / 90D / 1A / 3A / All

## Pipeline

Actualizacion automatica diaria via `V0.[PERMA] MASTER_DAILY_PIPELINE.py`:

```
V18 (streaming) -> V8.0 (SKEW pipeline + Telegram) -> update_dashboard.py (este repo)
```

`update_dashboard.py` lee el CSV fuente del V8.0, regenera `data.json` y hace
`git push` a este repo. GitHub Pages sirve el HTML estatico.

## Fuente de datos

`SURFACE_SKEW_CONCAVITY_COMPONENTS_DAILY.csv` (columnas `TENSION_3WAY_MIN`,
`U_curv_15_30_45_pct_252`, `U_slope_10_40_pct_252`, `U_skew_25_50_pct_252`).

Documentacion del score: ver `SURFACE_TENSION_RELEASE_DISCOVERY_BATMAN.md`
en el repo principal de research.
