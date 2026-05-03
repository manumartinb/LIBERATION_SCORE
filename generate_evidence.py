#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_evidence.py
====================
One-shot regenerator for the statistical evidence section of the
LIBERATION_SCORE dashboard.

Computes statistical evidence of TENSION_3WAY_MIN (regime score plotted on
the dashboard) vs Batman LT PnL across horizons d001 to d049 ("dlt50"):
  - Spearman correlation + bootstrap CI95 by horizon
  - Decile breakdown (D1..D10) with PF, win rate, monotonicity
  - Year stability 2019..2025
  - Regime split: FAVORABLE >=80, NEUTRAL, ADVERSO <=20

Produces 5 PNGs (matplotlib dark theme matching dashboard), copies 3 PNGs
from the TRIPLE infographic for context, builds the HTML tables that the
dashboard injects, and writes evidence/evidence.json.

Independent of update_dashboard.py and V0 master pipeline.
Manual regen with:
    python generate_evidence.py            # local only
    python generate_evidence.py --push     # local + git push to GitHub Pages

Auth: env var GH_DASHBOARD_TOKEN (User scope), Contents:write fine-grained
PAT scoped to manumartinb/LIBERATION_SCORE_BATMAN_LT.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

try:
    from scipy.stats import spearmanr
except Exception:
    spearmanr = None


# ============================== CONFIG ==============================

DASHBOARD_DIR = Path(r"C:\Users\Administrator\Desktop\LIBERATION_SCORE_DASHBOARD")
EVIDENCE_DIR = DASHBOARD_DIR / "evidence"

INPUT_CSV = Path(
    r"C:\Users\Administrator\Desktop\BULK OPTIONSTRAT\ESTRATEGIAS\Batman\SPX\LIVE"
    r"\[MAIN RANKEO LT]_combined_BATMAN_mediana_w_stats_w_vix_OWN_ALLDAYS.csv"
)
TRIPLE_DIR = Path(
    r"C:\Users\Administrator\Desktop\BULK OPTIONSTRAT\ESTRATEGIAS\Skew\RESEARCH_DATA\BATMAN"
)
TRIPLE_PNG_DIR = TRIPLE_DIR / "INFOGRAPHIC"

# Window-forward analysis: pre-computed CSV from
# `Skew/ANALISIS/13_SURFACE_REGIME/tension_window_forward_pnl.py`
WINDOW_FORWARD_CSV = Path(
    r"C:\Users\Administrator\Desktop\BULK OPTIONSTRAT\ESTRATEGIAS\Skew\ANALISIS"
    r"\13_SURFACE_REGIME\TENSION_window_forward_results.csv"
)

GH_REPO = "manumartinb/LIBERATION_SCORE_BATMAN_LT"
GH_USER_NAME = "manumartinb"
GH_USER_EMAIL = "manuelmartinbarranco@gmail.com"
TOKEN_ENV = "GH_DASHBOARD_TOKEN"
BRANCH = "main"
TZ = ZoneInfo("Europe/Madrid")

# Analysis params
SCORE_COL = "TENSION_3WAY_MIN"
DATE_COL = "trade_date"
WINDOWS = list(range(1, 50))                       # d001 .. d049
CHECKPOINTS = [1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 49]
BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 42
REGIME_FAV_MIN = 80.0
REGIME_ADV_MAX = 20.0
PNL_REF_HORIZON = 20                               # d020 = referencia headline

# Dark theme matching the dashboard
DARK_BG = "#0d1117"
DARK_PANEL = "#161b22"
DARK_TEXT = "#c9d1d9"
DARK_MUTED = "#8b949e"
DARK_BORDER = "#30363d"
DARK_GRID = "#21262d"
COLOR_TENSION = "#58a6ff"
COLOR_FAV = "#3fb950"
COLOR_NEU = "#d29922"
COLOR_ADV = "#f85149"
COLOR_ACCENT = "#a371f7"


# ============================== UTILS ==============================


def _setup_matplotlib_dark() -> None:
    plt.rcParams.update({
        "figure.facecolor": DARK_PANEL,
        "axes.facecolor": DARK_PANEL,
        "savefig.facecolor": DARK_PANEL,
        "savefig.edgecolor": DARK_PANEL,
        "text.color": DARK_TEXT,
        "axes.labelcolor": DARK_TEXT,
        "axes.titlecolor": DARK_TEXT,
        "xtick.color": DARK_TEXT,
        "ytick.color": DARK_TEXT,
        "axes.edgecolor": DARK_BORDER,
        "grid.color": DARK_GRID,
        "axes.grid": True,
        "grid.alpha": 0.45,
        "axes.unicode_minus": False,
        "font.size": 10,
        "font.family": "DejaVu Sans",
        "axes.spines.top": False,
        "axes.spines.right": False,
    })


def _safe_spearman(x: np.ndarray, y: np.ndarray) -> float:
    if x.size < 2 or y.size < 2:
        return float("nan")
    if spearmanr is not None:
        val = spearmanr(x, y, nan_policy="omit").correlation
        return float(val) if val is not None else float("nan")
    return float(pd.Series(x).corr(pd.Series(y), method="spearman"))


def _profit_factor(pnl: pd.Series) -> float:
    pnl = pd.to_numeric(pnl, errors="coerce").dropna()
    if pnl.empty:
        return float("nan")
    gw = float(pnl[pnl > 0].sum())
    gl = float((-pnl[pnl < 0]).sum())
    if gl <= 0:
        return float("nan")
    return gw / gl


def _winrate(pnl: pd.Series) -> float:
    p = pd.to_numeric(pnl, errors="coerce").dropna()
    if p.empty:
        return float("nan")
    return 100.0 * float((p > 0).mean())


def _fmt(v: float, prec: int = 2) -> str:
    if v is None or not np.isfinite(v):
        return "n/a"
    return f"{v:.{prec}f}"


def _fmt_int(v) -> str:
    if v is None:
        return "n/a"
    try:
        if not np.isfinite(v):
            return "n/a"
    except (TypeError, ValueError):
        pass
    return f"{int(v):,}"


def _fmt_pct(v: float, prec: int = 1) -> str:
    if v is None or not np.isfinite(v):
        return "n/a"
    return f"{v:.{prec}f}%"


# ============================== ANALYSIS ==============================


@dataclass
class Dataset:
    df: pd.DataFrame
    n_trades: int
    n_days: int
    date_min: str
    date_max: str


def load_dataset() -> Dataset:
    if not INPUT_CSV.exists():
        raise FileNotFoundError(f"Input CSV not found: {INPUT_CSV}")

    pnl_cols = [f"PnL_d{d:03d}_mediana" for d in WINDOWS]
    extra_cols = ["PnL_d050_mediana"]   # used by regime split (second horizon)
    needed = [DATE_COL, SCORE_COL] + pnl_cols + extra_cols
    df = pd.read_csv(INPUT_CSV, usecols=lambda c: c in set(needed), low_memory=False)
    df[DATE_COL] = pd.to_datetime(df[DATE_COL], errors="coerce")
    df[SCORE_COL] = pd.to_numeric(df[SCORE_COL], errors="coerce")
    for c in pnl_cols + [c for c in extra_cols if c in df.columns]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=[DATE_COL, SCORE_COL]).copy()
    df[DATE_COL] = df[DATE_COL].dt.normalize()
    df = df.sort_values(DATE_COL).reset_index(drop=True)

    return Dataset(
        df=df,
        n_trades=int(len(df)),
        n_days=int(df[DATE_COL].nunique()),
        date_min=str(df[DATE_COL].min().date()),
        date_max=str(df[DATE_COL].max().date()),
    )


def _attach_deciles(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        out["decile"] = np.nan
        return out
    try:
        dec = pd.qcut(out["score"], 10, labels=False, duplicates="drop")
        out["decile"] = dec.astype("float") + 1.0
    except Exception:
        out["decile"] = np.nan
    return out


def _decile_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "decile" not in df.columns:
        return pd.DataFrame(columns=["decile", "N", "mean", "median", "PF", "winrate"])
    rows = []
    for d in sorted(df["decile"].dropna().unique()):
        sub = df[df["decile"] == d]
        if sub.empty:
            continue
        pnl = sub["pnl"]
        rows.append({
            "decile": int(d),
            "N": int(pnl.notna().sum()),
            "mean": float(pnl.mean()),
            "median": float(pnl.median()),
            "PF": _profit_factor(pnl),
            "winrate": _winrate(pnl),
        })
    return pd.DataFrame(rows)


def _adjacent_non_decreasing_ratio(means: pd.Series) -> float:
    vals = means.sort_index().dropna().to_numpy(dtype=float)
    if vals.size < 2:
        return float("nan")
    return float(np.sum(np.diff(vals) >= 0) / (vals.size - 1))


def _bootstrap_ci(score: np.ndarray, pnl: np.ndarray, dec: np.ndarray,
                  n_boot: int, seed: int) -> Dict[str, float]:
    """Bootstrap CI95 for Spearman r and delta D10-D1.

    Optimization: Spearman is computed via Pearson correlation on PRE-ranked
    arrays (rank-once-then-bootstrap). Standard bootstrap variant of Spearman;
    much faster than calling scipy.stats.spearmanr per iteration which would
    re-rank the resampled vectors each time.
    """
    n = score.size
    if n < 30:
        return {"sp_lo": float("nan"), "sp_hi": float("nan"),
                "delta_lo": float("nan"), "delta_hi": float("nan")}

    score_rank = pd.Series(score).rank().to_numpy(dtype=float)
    pnl_rank = pd.Series(pnl).rank().to_numpy(dtype=float)

    rng = np.random.default_rng(seed)
    sp_vals = np.full(n_boot, np.nan, dtype=float)
    delta_vals = np.full(n_boot, np.nan, dtype=float)
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        sr = score_rank[idx]
        pr = pnl_rank[idx]
        # Pearson on pre-ranked vectors == bootstrap Spearman estimate
        sx = sr.std(); sy = pr.std()
        if sx > 0 and sy > 0:
            sp_vals[b] = float(np.mean((sr - sr.mean()) * (pr - pr.mean())) / (sx * sy))
        p = pnl[idx]; d = dec[idx]
        d1 = p[d == 1]; d10 = p[d == 10]
        if d1.size > 0 and d10.size > 0:
            delta_vals[b] = float(np.mean(d10) - np.mean(d1))
    return {
        "sp_lo": float(np.nanpercentile(sp_vals, 2.5)),
        "sp_hi": float(np.nanpercentile(sp_vals, 97.5)),
        "delta_lo": float(np.nanpercentile(delta_vals, 2.5)),
        "delta_hi": float(np.nanpercentile(delta_vals, 97.5)),
    }


def compute_horizon_metrics(ds: Dataset) -> pd.DataFrame:
    rows = []
    for d in WINDOWS:
        pnl_col = f"PnL_d{d:03d}_mediana"
        sub = ds.df[[DATE_COL, SCORE_COL, pnl_col]].copy()
        sub = sub.rename(columns={SCORE_COL: "score", pnl_col: "pnl"})
        sub = sub.dropna(subset=["score", "pnl"])
        if len(sub) < 100:
            continue
        sub_dec = _attach_deciles(sub)
        dec_tbl = _decile_table(sub_dec)
        means = dec_tbl.set_index("decile")["mean"] if not dec_tbl.empty else pd.Series(dtype=float)
        adj = _adjacent_non_decreasing_ratio(means)

        d1 = dec_tbl[dec_tbl["decile"] == 1]
        d10 = dec_tbl[dec_tbl["decile"] == 10]
        if not d1.empty and not d10.empty:
            d1_row = d1.iloc[0]; d10_row = d10.iloc[0]
            delta_mean = float(d10_row["mean"] - d1_row["mean"])
            pf_ratio = (float(d10_row["PF"] / d1_row["PF"])
                        if (np.isfinite(d10_row["PF"]) and np.isfinite(d1_row["PF"]) and d1_row["PF"] > 0)
                        else float("nan"))
        else:
            delta_mean = float("nan")
            pf_ratio = float("nan")

        sp = _safe_spearman(sub["score"].to_numpy(dtype=float),
                            sub["pnl"].to_numpy(dtype=float))

        do_boot = (d in CHECKPOINTS)
        if do_boot:
            ci = _bootstrap_ci(
                sub_dec["score"].to_numpy(dtype=float),
                sub_dec["pnl"].to_numpy(dtype=float),
                sub_dec["decile"].to_numpy(dtype=float),
                BOOTSTRAP_N,
                BOOTSTRAP_SEED + d,
            )
        else:
            ci = {"sp_lo": float("nan"), "sp_hi": float("nan"),
                  "delta_lo": float("nan"), "delta_hi": float("nan")}

        rows.append({
            "horizon_d": d,
            "N": int(len(sub)),
            "spearman": sp,
            "spearman_ci_lo": ci["sp_lo"],
            "spearman_ci_hi": ci["sp_hi"],
            "delta_mean_d10_d1": delta_mean,
            "delta_ci_lo": ci["delta_lo"],
            "delta_ci_hi": ci["delta_hi"],
            "pf_ratio_d10_d1": pf_ratio,
            "adjacent_non_decreasing": adj,
            "is_checkpoint": int(do_boot),
        })
    return pd.DataFrame(rows)


def compute_decile_table_ref(ds: Dataset) -> pd.DataFrame:
    pnl_col = f"PnL_d{PNL_REF_HORIZON:03d}_mediana"
    sub = ds.df[[DATE_COL, SCORE_COL, pnl_col]].copy()
    sub = sub.rename(columns={SCORE_COL: "score", pnl_col: "pnl"}).dropna(subset=["score", "pnl"])
    sub = _attach_deciles(sub)
    return _decile_table(sub)


def compute_year_stability(ds: Dataset) -> pd.DataFrame:
    pnl_col = f"PnL_d{PNL_REF_HORIZON:03d}_mediana"
    sub = ds.df[[DATE_COL, SCORE_COL, pnl_col]].copy()
    sub = sub.rename(columns={SCORE_COL: "score", pnl_col: "pnl"}).dropna(subset=["score", "pnl"])
    sub["year"] = sub[DATE_COL].dt.year
    rows = []
    for y, g in sub.groupby("year", sort=True):
        if len(g) < 50:
            continue
        sp = _safe_spearman(g["score"].to_numpy(dtype=float), g["pnl"].to_numpy(dtype=float))
        gd = _attach_deciles(g)
        dt = _decile_table(gd)
        d1 = dt[dt["decile"] == 1]; d10 = dt[dt["decile"] == 10]
        delta = (float(d10["mean"].iloc[0] - d1["mean"].iloc[0])
                 if (not d1.empty and not d10.empty) else float("nan"))
        rows.append({
            "year": int(y),
            "N": int(len(g)),
            "spearman": sp,
            "delta_mean_d10_d1": delta,
            "spearman_pos": int(np.isfinite(sp) and sp > 0),
            "delta_pos": int(np.isfinite(delta) and delta > 0),
        })
    return pd.DataFrame(rows)


def compute_regimes(ds: Dataset) -> pd.DataFrame:
    sub_d020 = ds.df[[SCORE_COL, f"PnL_d{PNL_REF_HORIZON:03d}_mediana", "PnL_d050_mediana"]].copy()
    sub_d020 = sub_d020.dropna(subset=[SCORE_COL])

    def _bucket(v):
        if v >= REGIME_FAV_MIN:
            return "FAVORABLE"
        if v <= REGIME_ADV_MAX:
            return "ADVERSO"
        return "NEUTRAL"

    sub_d020["regime"] = sub_d020[SCORE_COL].apply(_bucket)

    rows = []
    for label in ["FAVORABLE", "NEUTRAL", "ADVERSO"]:
        g = sub_d020[sub_d020["regime"] == label]
        n = int(len(g))
        if n == 0:
            continue
        for hcol, hkey in [(f"PnL_d{PNL_REF_HORIZON:03d}_mediana", "d020"),
                           ("PnL_d050_mediana", "d050")]:
            p = pd.to_numeric(g[hcol], errors="coerce").dropna()
            if p.empty:
                continue
            mean = float(p.mean())
            if len(p) >= 30:
                rng = np.random.default_rng(BOOTSTRAP_SEED)
                arr = p.to_numpy(dtype=float)
                boot = np.array([float(np.mean(arr[rng.integers(0, len(arr), size=len(arr))]))
                                 for _ in range(800)])
                ci_lo = float(np.percentile(boot, 2.5))
                ci_hi = float(np.percentile(boot, 97.5))
            else:
                ci_lo = float("nan"); ci_hi = float("nan")
            rows.append({
                "regime": label,
                "horizon": hkey,
                "N": n,
                "mean": mean,
                "ci_lo": ci_lo,
                "ci_hi": ci_hi,
                "PF": _profit_factor(p),
                "winrate": _winrate(p),
            })
    return pd.DataFrame(rows)


# ============================== PLOTS ==============================


def plot_spearman_curve(horizons: pd.DataFrame, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.6))
    h = horizons.sort_values("horizon_d")
    ax.plot(h["horizon_d"], h["spearman"], "-", color=COLOR_TENSION, linewidth=2.0,
            label="Spearman r")
    ck = h[h["is_checkpoint"] == 1]
    ax.errorbar(
        ck["horizon_d"], ck["spearman"],
        yerr=[ck["spearman"] - ck["spearman_ci_lo"], ck["spearman_ci_hi"] - ck["spearman"]],
        fmt="o", color=COLOR_TENSION, ecolor=COLOR_MUTED_BAR, elinewidth=1.4,
        capsize=4, markersize=6, markeredgecolor="white", markeredgewidth=0.6,
        label="Checkpoint + CI95",
    )
    ax.axhline(0, color=DARK_MUTED, linewidth=0.8, linestyle="--", alpha=0.7)
    ax.set_xlabel("Horizonte (dias)")
    ax.set_ylabel("Spearman r (TENSION vs PnL)")
    ax.set_title("Predictividad de TENSION_3WAY_MIN por horizonte (d001-d049)")
    ax.set_xticks([1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 49])
    ax.legend(loc="lower right", framealpha=0.9, facecolor=DARK_PANEL, edgecolor=DARK_BORDER)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_decile_bars(decs: pd.DataFrame, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.4))
    if decs.empty:
        ax.text(0.5, 0.5, "no data", ha="center", va="center", color=DARK_MUTED)
    else:
        d = decs.sort_values("decile")
        cmap = plt.cm.RdYlGn
        norm = (d["mean"] - d["mean"].min()) / max((d["mean"].max() - d["mean"].min()), 1e-9)
        colors = [cmap(0.15 + 0.7 * v) for v in norm]
        bars = ax.bar(d["decile"].astype(int), d["mean"], color=colors,
                      edgecolor=DARK_BORDER, linewidth=0.8)
        for b, m in zip(bars, d["mean"]):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(),
                    f"{m:+.1f}", ha="center", va="bottom" if m >= 0 else "top",
                    color=DARK_TEXT, fontsize=9)
    ax.axhline(0, color=DARK_MUTED, linewidth=0.8)
    ax.set_xlabel("Decil del score (1=baja TENSION, 10=alta TENSION)")
    ax.set_ylabel(f"PnL medio d{PNL_REF_HORIZON:03d} (puntos)")
    ax.set_title(f"PnL d{PNL_REF_HORIZON:03d} por decil de TENSION_3WAY_MIN")
    ax.set_xticks(list(range(1, 11)))
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_year_stability(years: pd.DataFrame, out_path: Path) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.0))
    if years.empty:
        for ax in (ax1, ax2):
            ax.text(0.5, 0.5, "no data", ha="center", va="center", color=DARK_MUTED)
    else:
        y = years.sort_values("year")
        # Spearman per year
        colors = [COLOR_FAV if v > 0 else COLOR_ADV for v in y["spearman"]]
        ax1.bar(y["year"].astype(str), y["spearman"], color=colors,
                edgecolor=DARK_BORDER, linewidth=0.8)
        ax1.axhline(0, color=DARK_MUTED, linewidth=0.8)
        ax1.set_title(f"Spearman r por anio (d{PNL_REF_HORIZON:03d})")
        ax1.set_ylabel("Spearman r")
        for x, v in zip(y["year"].astype(str), y["spearman"]):
            ax1.text(x, v, f"{v:+.2f}", ha="center",
                     va="bottom" if v >= 0 else "top",
                     color=DARK_TEXT, fontsize=8)
        # Delta D10-D1
        colors2 = [COLOR_FAV if v > 0 else COLOR_ADV for v in y["delta_mean_d10_d1"]]
        ax2.bar(y["year"].astype(str), y["delta_mean_d10_d1"], color=colors2,
                edgecolor=DARK_BORDER, linewidth=0.8)
        ax2.axhline(0, color=DARK_MUTED, linewidth=0.8)
        ax2.set_title(f"Delta D10 - D1 por anio (d{PNL_REF_HORIZON:03d}, pts)")
        ax2.set_ylabel("Delta PnL medio (pts)")
        for x, v in zip(y["year"].astype(str), y["delta_mean_d10_d1"]):
            ax2.text(x, v, f"{v:+.1f}", ha="center",
                     va="bottom" if v >= 0 else "top",
                     color=DARK_TEXT, fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_regime_pnl(regimes: pd.DataFrame, out_path: Path) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.0))
    color_map = {"FAVORABLE": COLOR_FAV, "NEUTRAL": COLOR_NEU, "ADVERSO": COLOR_ADV}
    order = ["ADVERSO", "NEUTRAL", "FAVORABLE"]

    for ax, hkey, title in [(ax1, "d020", "PnL d020 por regimen"),
                            (ax2, "d050", "PnL d050 por regimen")]:
        sub = regimes[regimes["horizon"] == hkey].set_index("regime").reindex(order).reset_index()
        if sub.empty or sub["mean"].isna().all():
            ax.text(0.5, 0.5, "no data", ha="center", va="center", color=DARK_MUTED)
            continue
        means = sub["mean"].fillna(0).values
        lo = (sub["mean"] - sub["ci_lo"]).fillna(0).values
        hi = (sub["ci_hi"] - sub["mean"]).fillna(0).values
        colors = [color_map.get(r, DARK_MUTED) for r in sub["regime"]]
        bars = ax.bar(sub["regime"], means, color=colors, yerr=[lo, hi],
                      capsize=8, edgecolor=DARK_BORDER, linewidth=0.8,
                      ecolor=DARK_TEXT)
        for b, m, n in zip(bars, means, sub["N"].fillna(0).astype(int)):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(),
                    f"{m:+.1f}\nN={n:,}", ha="center",
                    va="bottom" if m >= 0 else "top",
                    color=DARK_TEXT, fontsize=9)
        ax.axhline(0, color=DARK_MUTED, linewidth=0.8)
        ax.set_title(title)
        ax.set_ylabel("PnL medio (pts)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_delta_curve(horizons: pd.DataFrame, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.0))
    h = horizons.sort_values("horizon_d")
    ax.plot(h["horizon_d"], h["delta_mean_d10_d1"], "-",
            color=COLOR_ACCENT, linewidth=2.0, label="Delta D10-D1")
    ck = h[h["is_checkpoint"] == 1]
    ax.errorbar(
        ck["horizon_d"], ck["delta_mean_d10_d1"],
        yerr=[ck["delta_mean_d10_d1"] - ck["delta_ci_lo"],
              ck["delta_ci_hi"] - ck["delta_mean_d10_d1"]],
        fmt="o", color=COLOR_ACCENT, ecolor=COLOR_MUTED_BAR, elinewidth=1.4,
        capsize=4, markersize=6, markeredgecolor="white", markeredgewidth=0.6,
        label="Checkpoint + CI95",
    )
    ax.axhline(0, color=DARK_MUTED, linewidth=0.8, linestyle="--", alpha=0.7)
    ax.set_xlabel("Horizonte (dias)")
    ax.set_ylabel("Delta PnL medio D10-D1 (pts)")
    ax.set_title("Spread D10 - D1 por horizonte (gap entre top y bottom decil)")
    ax.set_xticks([1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 49])
    ax.legend(loc="lower right", framealpha=0.9, facecolor=DARK_PANEL, edgecolor=DARK_BORDER)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


COLOR_MUTED_BAR = DARK_MUTED  # sentinel forward-decl for plots above


def plot_window_forward(csv_path: Path, out_path: Path) -> bool:
    """Render the window-forward chart from pre-computed CSV.

    Layout: 3 rows (SPX filter) x 2 cols (forward 20d, 50d). Each panel:
    bars at observation days t=0,10,20,30,40 with green=HIGH (P80+) and
    red=LOW (P20-) TENSION at t. Returns True on success.
    """
    if not csv_path.exists():
        print(f"[WARN] window-forward CSV not found: {csv_path}")
        return False
    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        print(f"[WARN] failed to read window-forward CSV: {exc}")
        return False

    filters_order = ["sin filtro", "|SPX|<=3%", "|SPX|<=2%"]
    forwards = [20, 50]
    obs_days = sorted([int(t) for t in df["t"].unique()])
    n_obs = len(obs_days)

    fig, axes = plt.subplots(3, 2, figsize=(13, 11), sharey="col")
    for i, flt in enumerate(filters_order):
        for j, fwd in enumerate(forwards):
            ax = axes[i][j]
            sub = df[(df["spx_filter"] == flt) & (df["x"] == fwd)].sort_values("t")
            if sub.empty:
                ax.text(0.5, 0.5, "no data", ha="center", va="center", color=DARK_MUTED)
                continue
            x = np.arange(n_obs)
            w = 0.36
            ax.bar(x - w / 2, sub["high_mean"].values, w, color=COLOR_FAV,
                   edgecolor=DARK_BORDER, linewidth=0.7,
                   label="HIGH (TENSION P80+)" if (i == 0 and j == 0) else None)
            ax.bar(x + w / 2, sub["low_mean"].values, w, color=COLOR_ADV,
                   edgecolor=DARK_BORDER, linewidth=0.7,
                   label="LOW (TENSION P20-)" if (i == 0 and j == 0) else None)
            ax.axhline(0, color=DARK_MUTED, linewidth=0.7)
            ax.set_xticks(x)
            ax.set_xticklabels([f"t={t}" for t in obs_days], fontsize=9)
            ax.set_title(f"forward +{fwd}d  |  filtro: {flt}", fontsize=10)
            if j == 0:
                ax.set_ylabel(f"Delta PnL en proximos {fwd}d (pts)", fontsize=9)
            if i == 2:
                ax.set_xlabel("Observation day t (cuando miramos TENSION)", fontsize=9)
            # value labels
            for k, (h, lo, nh, nl) in enumerate(zip(
                sub["high_mean"].values, sub["low_mean"].values,
                sub["N_high"].values, sub["N_low"].values
            )):
                ax.text(k - w / 2, h, f"{h:+.1f}",
                        ha="center", va="bottom" if h >= 0 else "top",
                        color=DARK_TEXT, fontsize=7.5)
                ax.text(k + w / 2, lo, f"{lo:+.1f}",
                        ha="center", va="bottom" if lo >= 0 else "top",
                        color=DARK_TEXT, fontsize=7.5)
            if i == 0 and j == 0:
                ax.legend(loc="upper right", fontsize=8, framealpha=0.9,
                          facecolor=DARK_PANEL, edgecolor=DARK_BORDER)

    fig.suptitle(
        "TENSION en ventana alta (P80+) vs baja (P20-): cambio de PnL en proximos x dias",
        fontsize=12, fontweight="bold", color=DARK_TEXT,
    )
    fig.text(0.5, 0.945,
             "Particion por regimen de TENSION en el dia de observacion t. Verde: HIGH (P80+).  Rojo: LOW (P20-).",
             ha="center", color=DARK_MUTED, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return True


def build_table_window_forward_html(csv_path: Path) -> str:
    """Pretty summary table of HIGH vs LOW spreads at key (t, x, filter) cells."""
    if not csv_path.exists():
        return ("<p style='color:#f85149'>"
                "TENSION_window_forward_results.csv no encontrado.</p>")
    df = pd.read_csv(csv_path)
    rows = []
    for t in [0, 20, 40]:
        for fwd in [20, 50]:
            for flt in ["sin filtro", "|SPX|<=3%", "|SPX|<=2%"]:
                sub = df[(df["t"] == t) & (df["x"] == fwd) & (df["spx_filter"] == flt)]
                if sub.empty:
                    continue
                r = sub.iloc[0]
                spread = float(r["spread"])
                spread_color = "#3fb950" if spread > 0 else "#f85149"
                rows.append([
                    f't={int(t)}',
                    f'+{int(fwd)}d',
                    str(flt),
                    f'<span style="color:#3fb950">{float(r["high_mean"]):+.1f}</span>',
                    _fmt_int(r["N_high"]),
                    _fmt_pct(float(r["high_WR"]), 1),
                    f'<span style="color:#f85149">{float(r["low_mean"]):+.1f}</span>',
                    _fmt_int(r["N_low"]),
                    _fmt_pct(float(r["low_WR"]), 1),
                    f'<b style="color:{spread_color}">{spread:+.1f}</b>',
                ])
    return _table_html(
        rows,
        header=["t", "Fwd", "Filtro SPX",
                "HIGH mean", "N HIGH", "WR HIGH",
                "LOW mean", "N LOW", "WR LOW",
                "Spread"],
    )


# ============================== HTML TABLES ==============================


def _table_html(rows: List[List[str]], header: List[str], align: Optional[List[str]] = None) -> str:
    if align is None:
        align = ["right"] * len(header)
        if header:
            align[0] = "left"
    head_cells = "".join(
        f'<th style="text-align:{a}">{c}</th>' for c, a in zip(header, align)
    )
    body_rows = []
    for r in rows:
        body_rows.append(
            "<tr>" + "".join(
                f'<td style="text-align:{a}">{c}</td>' for c, a in zip(r, align)
            ) + "</tr>"
        )
    return (
        '<div style="overflow-x:auto"><table class="ev-table">'
        f"<thead><tr>{head_cells}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>"
    )


def build_table_horizons_html(horizons: pd.DataFrame) -> str:
    ck = horizons[horizons["is_checkpoint"] == 1].sort_values("horizon_d")
    rows = []
    for _, r in ck.iterrows():
        sp_str = f'{_fmt(r["spearman"], 3)} [{_fmt(r["spearman_ci_lo"], 3)}, {_fmt(r["spearman_ci_hi"], 3)}]'
        delta_str = f'{_fmt(r["delta_mean_d10_d1"], 1)} [{_fmt(r["delta_ci_lo"], 1)}, {_fmt(r["delta_ci_hi"], 1)}]'
        rows.append([
            f'd{int(r["horizon_d"]):03d}',
            _fmt_int(r["N"]),
            sp_str,
            delta_str,
            _fmt(r["pf_ratio_d10_d1"], 2),
            _fmt(r["adjacent_non_decreasing"], 2),
        ])
    return _table_html(
        rows,
        header=["Horizonte", "N", "Spearman r [CI95]", "Delta D10-D1 (pts) [CI95]", "PF D10/D1", "Monotonia adj"],
    )


def build_table_deciles_html(decs: pd.DataFrame) -> str:
    rows = []
    for _, r in decs.sort_values("decile").iterrows():
        rows.append([
            f'D{int(r["decile"])}',
            _fmt_int(r["N"]),
            _fmt(r["mean"], 2),
            _fmt(r["median"], 2),
            _fmt(r["PF"], 2),
            _fmt_pct(r["winrate"], 1),
        ])
    return _table_html(
        rows,
        header=[f"Decil", "N", f"Mean d{PNL_REF_HORIZON:03d}", "Median", "PF", "Win Rate"],
    )


def build_table_years_html(years: pd.DataFrame) -> str:
    rows = []
    for _, r in years.sort_values("year").iterrows():
        sp_color = "#3fb950" if r["spearman_pos"] else "#f85149"
        delta_color = "#3fb950" if r["delta_pos"] else "#f85149"
        rows.append([
            str(int(r["year"])),
            _fmt_int(r["N"]),
            f'<span style="color:{sp_color}">{_fmt(r["spearman"], 3)}</span>',
            f'<span style="color:{delta_color}">{_fmt(r["delta_mean_d10_d1"], 1)}</span>',
        ])
    summary_rows = [
        [
            "<b>Total +</b>",
            "",
            f'<b>{int(years["spearman_pos"].sum())}/{len(years)}</b>',
            f'<b>{int(years["delta_pos"].sum())}/{len(years)}</b>',
        ]
    ]
    return _table_html(
        rows + summary_rows,
        header=["Anio", "N", f"Spearman d{PNL_REF_HORIZON:03d}", "Delta D10-D1 (pts)"],
    )


def build_table_regimes_html(regimes: pd.DataFrame) -> str:
    rows = []
    order = ["FAVORABLE", "NEUTRAL", "ADVERSO"]
    horizon_order = ["d020", "d050"]
    for reg in order:
        for hkey in horizon_order:
            sub = regimes[(regimes["regime"] == reg) & (regimes["horizon"] == hkey)]
            if sub.empty:
                continue
            r = sub.iloc[0]
            color = {"FAVORABLE": "#3fb950", "NEUTRAL": "#d29922",
                     "ADVERSO": "#f85149"}.get(reg, "#c9d1d9")
            mean_str = f'<span style="color:{color}"><b>{_fmt(r["mean"], 2)}</b></span>'
            ci_str = f'[{_fmt(r["ci_lo"], 1)}, {_fmt(r["ci_hi"], 1)}]'
            rows.append([
                f'<span style="color:{color}">{reg}</span>',
                hkey,
                _fmt_int(r["N"]),
                mean_str,
                ci_str,
                _fmt(r["PF"], 2),
                _fmt_pct(r["winrate"], 1),
            ])
    return _table_html(
        rows,
        header=["Regimen", "Horizonte", "N", "Mean PnL", "CI95", "PF", "Win Rate"],
    )


def build_table_triple_master_html() -> Tuple[str, Dict[str, Dict[str, float]]]:
    """Read EVID_T0_master.csv and build summary table + dict for evidence.json."""
    path = TRIPLE_DIR / "EVID_T0_master.csv"
    summary: Dict[str, Dict[str, float]] = {}
    if not path.exists():
        return ("<p style='color:#f85149'>EVID_T0_master.csv no encontrado.</p>", summary)
    t = pd.read_csv(path)

    keep = {
        "UNIVERSO": "universo",
        "BQI_V4 top P80": "bqi_p80",
        "TS_M3 bot P20": "ts_p20",
        "TENSION_3WAY_MIN top P80": "tension_p80",
        "TRIPLE_GOOD_P50": "triple_p50",
        "TRIPLE_GOOD_P80": "triple_p80",
        "TRIPLE_BAD_P80": "triple_bad_p80",
    }
    rows = []
    for _, r in t.iterrows():
        lbl = str(r["label"]).strip()
        if lbl not in keep:
            continue
        d = {
            "label": lbl,
            "N": int(r["N"]),
            "mean_d020": float(r["mean_d020"]),
            "WR_pct": float(r["WR_pct"]),
            "PF": float(r["PF"]),
            "CVaR5_pct": float(r["CVaR5_pct"]),
            "mean_d050": float(r["mean_d050"]),
        }
        summary[keep[lbl]] = d

        is_triple_good = "TRIPLE_GOOD" in lbl
        is_bad = "BAD" in lbl
        if is_triple_good:
            color = "#3fb950"
        elif is_bad:
            color = "#f85149"
        else:
            color = "#c9d1d9"
        rows.append([
            f'<span style="color:{color}">{lbl}</span>',
            _fmt_int(d["N"]),
            f'<b>{_fmt(d["mean_d020"], 2)}</b>',
            _fmt_pct(d["WR_pct"], 1),
            _fmt(d["PF"], 2),
            _fmt(d["CVaR5_pct"], 1),
            _fmt(d["mean_d050"], 2),
        ])
    html = _table_html(
        rows,
        header=["Subconjunto", "N", "Mean d020", "Win Rate", "PF", "CVaR5%", "Mean d050"],
    )
    return html, summary


def build_table_triple_loco_html() -> Tuple[str, List[Dict[str, float]]]:
    path = TRIPLE_DIR / "EVID_T5_loco.csv"
    if not path.exists():
        return ("<p style='color:#f85149'>EVID_T5_loco.csv no encontrado.</p>", [])
    t = pd.read_csv(path)
    rows = []
    summary = []
    for _, r in t.iterrows():
        delta_mean = float(r["delta_vs_full_mean"])
        delta_pf = float(r["delta_vs_full_PF"])
        is_critical = ("TEN" in str(r["variant"])) and ("Drop" in str(r["variant"]))
        color = "#f85149" if is_critical else ("#c9d1d9" if not is_critical else "#3fb950")
        # Drop TEN row deserves emphasis:
        if "Drop TEN" in str(r["variant"]):
            color = "#f85149"
        rows.append([
            f'<span style="color:{color}">{r["variant"]}</span>',
            _fmt_int(r["N"]),
            _fmt(r["mean_d020"], 2),
            _fmt_pct(r["WR_pct"], 1),
            _fmt(r["PF"], 2),
            f'{_fmt(delta_mean, 2)}',
            f'{_fmt(delta_pf, 2)}',
        ])
        summary.append({
            "variant": str(r["variant"]),
            "N": int(r["N"]),
            "mean_d020": float(r["mean_d020"]),
            "WR_pct": float(r["WR_pct"]),
            "PF": float(r["PF"]),
            "delta_vs_full_mean": delta_mean,
            "delta_vs_full_PF": delta_pf,
        })
    html = _table_html(
        rows,
        header=["Variante", "N", "Mean d020", "Win Rate", "PF", "Delta mean", "Delta PF"],
    )
    return html, summary


# ============================== ORCHESTRATION ==============================


def copy_triple_pngs(out_dir: Path) -> Dict[str, str]:
    mapping = {
        "01_decile_monotonicity.png": "triple_decile_monotonicity.png",
        "03_loco_comparison.png": "triple_loco_comparison.png",
        "08_headline_scoreboard.png": "triple_headline_scoreboard.png",
    }
    out_paths: Dict[str, str] = {}
    for src_name, dst_name in mapping.items():
        src = TRIPLE_PNG_DIR / src_name
        if src.exists():
            shutil.copyfile(src, out_dir / dst_name)
            out_paths[dst_name] = f"evidence/{dst_name}"
        else:
            print(f"[WARN] TRIPLE PNG not found: {src}")
    return out_paths


def build_evidence_json(
    ds: Dataset,
    horizons: pd.DataFrame,
    decs: pd.DataFrame,
    years: pd.DataFrame,
    regimes: pd.DataFrame,
    triple_master: Dict[str, Dict[str, float]],
    triple_loco: List[Dict[str, float]],
    tables: Dict[str, str],
    triple_pngs: Dict[str, str],
) -> dict:
    sp_by_h = {f'd{int(r.horizon_d):03d}': float(r.spearman) for r in horizons.itertuples()}
    delta_by_h = {f'd{int(r.horizon_d):03d}': float(r.delta_mean_d10_d1) for r in horizons.itertuples()}

    h_d020 = horizons[horizons["horizon_d"] == PNL_REF_HORIZON]
    headline = {}
    if not h_d020.empty:
        r = h_d020.iloc[0]
        headline = {
            "horizon": f"d{PNL_REF_HORIZON:03d}",
            "spearman": float(r["spearman"]),
            "spearman_ci": [float(r["spearman_ci_lo"]), float(r["spearman_ci_hi"])],
            "delta_d10_d1": float(r["delta_mean_d10_d1"]),
            "delta_ci": [float(r["delta_ci_lo"]), float(r["delta_ci_hi"])],
            "pf_ratio_d10_d1": float(r["pf_ratio_d10_d1"]),
            "adjacent_non_decreasing": float(r["adjacent_non_decreasing"]),
        }

    images = {
        "spearman_curve": "evidence/tension_spearman_curve.png",
        "decile_bars": "evidence/tension_decile_bars.png",
        "year_stability": "evidence/tension_year_stability.png",
        "regime_pnl": "evidence/tension_regime_pnl.png",
        "delta_curve": "evidence/tension_delta_curve.png",
        "window_forward": "evidence/tension_window_forward.png",
    }
    images.update(triple_pngs)

    return {
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z"),
        "input": {
            "file": INPUT_CSV.name,
            "n_trades": ds.n_trades,
            "n_days": ds.n_days,
            "date_min": ds.date_min,
            "date_max": ds.date_max,
        },
        "params": {
            "score_col": SCORE_COL,
            "horizons": [int(d) for d in WINDOWS],
            "checkpoints": CHECKPOINTS,
            "bootstrap_n": BOOTSTRAP_N,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "regime_favorable_min": REGIME_FAV_MIN,
            "regime_adverso_max": REGIME_ADV_MAX,
            "pnl_reference_horizon": PNL_REF_HORIZON,
        },
        "tension": {
            "headline": headline,
            "spearman_by_horizon": sp_by_h,
            "delta_by_horizon": delta_by_h,
            "deciles_d020": decs.to_dict(orient="records"),
            "year_stability": years.to_dict(orient="records"),
            "regimes": regimes.to_dict(orient="records"),
        },
        "triple": {
            "master": triple_master,
            "loco": triple_loco,
        },
        "tables_html": tables,
        "images": images,
    }


def main(push: bool) -> int:
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        _setup_matplotlib_dark()

        print(f"[INFO] reading {INPUT_CSV.name}")
        ds = load_dataset()
        print(f"[INFO] dataset: {ds.n_trades:,} trades / {ds.n_days:,} days "
              f"({ds.date_min} -> {ds.date_max})")

        print("[INFO] computing horizon metrics (d001..d049, bootstrap on 11 checkpoints)")
        horizons = compute_horizon_metrics(ds)
        print(f"[INFO] horizons computed: {len(horizons)} rows")

        print("[INFO] computing decile table at d020")
        decs = compute_decile_table_ref(ds)

        print("[INFO] computing year stability")
        years = compute_year_stability(ds)
        print(f"[INFO] year stability: {len(years)} years")

        print("[INFO] computing regime split")
        regimes = compute_regimes(ds)

        print("[INFO] generating PNG plots (TENSION isolated)")
        plot_spearman_curve(horizons, EVIDENCE_DIR / "tension_spearman_curve.png")
        plot_decile_bars(decs, EVIDENCE_DIR / "tension_decile_bars.png")
        plot_year_stability(years, EVIDENCE_DIR / "tension_year_stability.png")
        plot_regime_pnl(regimes, EVIDENCE_DIR / "tension_regime_pnl.png")
        plot_delta_curve(horizons, EVIDENCE_DIR / "tension_delta_curve.png")

        print("[INFO] rendering window-forward chart (HIGH vs LOW TENSION at obs day t)")
        wf_ok = plot_window_forward(WINDOW_FORWARD_CSV,
                                    EVIDENCE_DIR / "tension_window_forward.png")
        if wf_ok:
            print("[INFO] window-forward chart OK")
        else:
            print("[WARN] window-forward chart skipped (CSV missing or unreadable)")

        print("[INFO] copying TRIPLE infographic PNGs")
        triple_pngs = copy_triple_pngs(EVIDENCE_DIR)

        print("[INFO] building HTML tables")
        tables = {
            "spearman": build_table_horizons_html(horizons),
            "deciles": build_table_deciles_html(decs),
            "years": build_table_years_html(years),
            "regimes": build_table_regimes_html(regimes),
            "window_forward": build_table_window_forward_html(WINDOW_FORWARD_CSV),
        }
        triple_master_html, triple_master_dict = build_table_triple_master_html()
        triple_loco_html, triple_loco_list = build_table_triple_loco_html()
        tables["triple_master"] = triple_master_html
        tables["triple_loco"] = triple_loco_html

        print("[INFO] writing evidence/evidence.json")
        ev = build_evidence_json(
            ds, horizons, decs, years, regimes,
            triple_master_dict, triple_loco_list, tables, triple_pngs,
        )
        out_json = EVIDENCE_DIR / "evidence.json"
        out_json.write_text(json.dumps(ev, ensure_ascii=False, separators=(",", ":")),
                            encoding="utf-8")

        readme = EVIDENCE_DIR / "README.txt"
        readme.write_text(
            f"Evidence regenerated: {ev['generated_at']}\n"
            f"Input: {INPUT_CSV.name}\n"
            f"N trades: {ds.n_trades:,}  N days: {ds.n_days:,}\n"
            f"Date range: {ds.date_min} to {ds.date_max}\n"
            f"Score: {SCORE_COL}\n"
            f"Bootstrap: n={BOOTSTRAP_N}, seed={BOOTSTRAP_SEED}\n"
            f"Reference horizon: d{PNL_REF_HORIZON:03d}\n"
            f"Headline Spearman r: {ev['tension']['headline'].get('spearman', float('nan')):.3f}\n",
            encoding="utf-8",
        )

        h = ev["tension"]["headline"]
        print(f"[OK] headline d{PNL_REF_HORIZON:03d}: spearman={h.get('spearman', float('nan')):.3f} "
              f"CI95=[{h.get('spearman_ci', [float('nan'), float('nan')])[0]:.3f}, "
              f"{h.get('spearman_ci', [float('nan'), float('nan')])[1]:.3f}]  "
              f"delta_D10_D1={h.get('delta_d10_d1', float('nan')):.2f} pts")

        if push:
            return git_push()
        else:
            print("[INFO] run with --push to publish to GitHub Pages")
            return 0

    except Exception as exc:
        print(f"[X] generate_evidence failed: {exc}")
        traceback.print_exc()
        return 1


# ============================== GIT PUSH ==============================


def _git(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(DASHBOARD_DIR), *args],
        capture_output=True, text=True, check=False,
    )


def git_push() -> int:
    token = os.environ.get(TOKEN_ENV)
    if not token:
        print(f"[X] env var {TOKEN_ENV} not set; cannot push")
        return 1

    _git(["config", "user.name", GH_USER_NAME])
    _git(["config", "user.email", GH_USER_EMAIL])

    # Pull --rebase to avoid colliding with V0 daily push
    remote_url = f"https://x-access-token:{token}@github.com/{GH_REPO}.git"
    pull = subprocess.run(
        ["git", "-C", str(DASHBOARD_DIR), "pull", "--rebase", remote_url, BRANCH],
        capture_output=True, text=True,
    )
    if pull.returncode != 0:
        sanitized = pull.stderr.replace(token, "***")
        # Pull failure is sometimes benign (no upstream changes). Continue if not destructive.
        if "CONFLICT" in sanitized or "merge" in sanitized.lower():
            print(f"[X] pull --rebase had conflicts: {sanitized.strip()}")
            return 1
        print(f"[WARN] pull --rebase output: {sanitized.strip()}")

    _git(["add", "evidence/", "generate_evidence.py", "index.html", "README.md"])
    status = _git(["status", "--porcelain"])
    if not status.stdout.strip():
        print("[INFO] no changes to commit")
        return 0

    today = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    commit = _git(["commit", "-m", f"evidence regen {today}"])
    if commit.returncode != 0:
        print(f"[X] commit failed: {commit.stderr.strip()}")
        return 1

    push = subprocess.run(
        ["git", "-C", str(DASHBOARD_DIR), "push", remote_url, BRANCH],
        capture_output=True, text=True,
    )
    if push.returncode != 0:
        sanitized = push.stderr.replace(token, "***")
        print(f"[X] push failed: {sanitized.strip()}")
        return 1

    print(f"[OK] pushed to https://manumartinb.github.io/LIBERATION_SCORE_BATMAN_LT/")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Regenerate LIBERATION_SCORE dashboard evidence.")
    parser.add_argument("--push", action="store_true",
                        help="After regen, commit and push to GitHub Pages.")
    args = parser.parse_args()
    sys.exit(main(push=args.push))
