# LIBERATION_SCORE Dashboard

Pagina web con grafico interactivo del componente **regime/daily** del sistema
`BATMAN_LIBERATION_TRIPLE` (SPX, DTE 60, snapshot 10:30 ET).

URL publica: https://manumartinb.github.io/LIBERATION_SCORE_BATMAN_LT/

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

## Seccion de evidencia estadistica

Bajo la grafica diaria, la pagina muestra una seccion de evidencia con:

- Spearman r de TENSION vs PnL Batman LT por horizonte (d001-d049)
- Deciles D1-D10 + spread D10-D1 por horizonte
- Year stability 2019-2025
- Regime split (FAVORABLE / NEUTRAL / ADVERSO) en d020 y d050
- Contexto del sistema TRIPLE (BQI x TS_M3 x TENSION) con LOCO

La evidencia es **estatica** (no se regenera con V0 diario porque la data Batman LT
no cambia dia a dia). Para regenerar con datos historicos actualizados:

```
python "C:\Users\Administrator\Desktop\LIBERATION_SCORE_DASHBOARD\generate_evidence.py" --push
```

Lo que hace `generate_evidence.py`:

1. Lee `[MAIN RANKEO LT]_combined_BATMAN_mediana_w_stats_w_vix_OWN_ALLDAYS.csv`
2. Calcula sobre `TENSION_3WAY_MIN` aislado vs PnL_d001..d049: Spearman + bootstrap CI95, deciles, year stability, regime split.
3. Genera 5 PNGs propios (matplotlib dark theme matching dashboard).
4. Copia 3 PNGs ya existentes desde `Skew\RESEARCH_DATA\BATMAN\INFOGRAPHIC\` (deciles, LOCO, scoreboard).
5. Reusa CSVs `EVID_T0_master.csv` y `EVID_T5_loco.csv` para tablas TRIPLE.
6. Volca `evidence/evidence.json` con metricas + tablas HTML inline.
7. Si `--push`: hace `git pull --rebase`, commit y push usando `GH_DASHBOARD_TOKEN`.

**No correr entre 12:25 y 12:40 Madrid** &mdash; coincide con la ventana del push diario de V0 Step 3 y podria provocar conflictos de rebase.

Sin `--push`: solo genera locales (util para iterar diseno antes de publicar).
Tiempo de ejecucion: ~1-2 minutos (bootstrap n=2000, 11 checkpoints, optimizacion rank-once).
