#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_dashboard.py
===================
Genera data.json del dashboard LIBERATION_SCORE y hace push a GitHub Pages.

Lee SURFACE_SKEW_CONCAVITY_COMPONENTS_DAILY.csv (output de V8.0 SKEW PIPELINE)
y publica en https://manumartinb.github.io/LIBERATION_SCORE/

Token leido de env var GH_DASHBOARD_TOKEN (User scope, set via setx).

Disenado para ser invocado al final de V0.[PERMA] MASTER_DAILY_PIPELINE.py
como Step 3, despues de V8.0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo

import pandas as pd

# ---------------- CONFIG ----------------
SOURCE_CSV = Path(
    r"C:\Users\Administrator\Desktop\BULK OPTIONSTRAT\ESTRATEGIAS\Skew\SURFACE_SKEW_CONCAVITY_COMPONENTS_DAILY.csv"
)
DASHBOARD_DIR = Path(r"C:\Users\Administrator\Desktop\LIBERATION_SCORE_DASHBOARD")
DATA_JSON = DASHBOARD_DIR / "data.json"

GH_REPO = "manumartinb/LIBERATION_SCORE"
GH_USER_NAME = "manumartinb"
GH_USER_EMAIL = "manuelmartinbarranco@gmail.com"
TOKEN_ENV = "GH_DASHBOARD_TOKEN"
BRANCH = "main"

TZ = ZoneInfo("Europe/Madrid")

TENSION_COL = "TENSION_3WAY_MIN"
SUB_COLS = (
    "U_curv_15_30_45_pct_252",
    "U_slope_10_40_pct_252",
    "U_skew_25_50_pct_252",
)

FAV_MIN = 80.0
ADV_MAX = 20.0


# ---------------- HELPERS ----------------
def regime_label(v: float) -> str:
    if pd.isna(v):
        return "INDETERMINADO"
    if v >= FAV_MIN:
        return "FAVORABLE"
    if v <= ADV_MAX:
        return "ADVERSO"
    return "NEUTRAL"


def _round_or_none(v) -> float | None:
    if pd.isna(v):
        return None
    return round(float(v), 2)


def build_data_payload() -> dict:
    if not SOURCE_CSV.exists():
        raise FileNotFoundError(f"Source CSV not found: {SOURCE_CSV}")

    cols_needed = {"trade_date", TENSION_COL, *SUB_COLS}
    df = pd.read_csv(
        SOURCE_CSV,
        usecols=lambda c: c in cols_needed,
        low_memory=False,
    )

    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df.dropna(subset=["trade_date", TENSION_COL]).copy()
    df["trade_date"] = df["trade_date"].dt.strftime("%Y-%m-%d")
    df = df.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)

    if df.empty:
        raise RuntimeError("No valid rows in source CSV after filtering")

    last_v = float(df[TENSION_COL].iloc[-1])
    last_date = str(df["trade_date"].iloc[-1])

    return {
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z"),
        "source": SOURCE_CSV.name,
        "n_days": int(len(df)),
        "thresholds": {"favorable_min": FAV_MIN, "adverso_max": ADV_MAX},
        "latest": {
            "date": last_date,
            "tension": round(last_v, 2),
            "regime": regime_label(last_v),
            "curv": _round_or_none(df["U_curv_15_30_45_pct_252"].iloc[-1]),
            "slope": _round_or_none(df["U_slope_10_40_pct_252"].iloc[-1]),
            "skew": _round_or_none(df["U_skew_25_50_pct_252"].iloc[-1]),
        },
        "dates": df["trade_date"].tolist(),
        "tension": [_round_or_none(v) for v in df[TENSION_COL]],
        "curv": [_round_or_none(v) for v in df["U_curv_15_30_45_pct_252"]],
        "slope": [_round_or_none(v) for v in df["U_slope_10_40_pct_252"]],
        "skew": [_round_or_none(v) for v in df["U_skew_25_50_pct_252"]],
    }


def _payload_data_changed(new_payload: dict) -> bool:
    """True if data (excluding generated_at) differs from current data.json."""
    if not DATA_JSON.exists():
        return True
    try:
        old = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    except Exception:
        return True
    keys_to_compare = ("dates", "tension", "curv", "slope", "skew", "latest", "n_days")
    for k in keys_to_compare:
        if old.get(k) != new_payload.get(k):
            return True
    return False


def write_data_json(payload: dict) -> None:
    DATA_JSON.write_text(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )


def _git(args: List[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(DASHBOARD_DIR), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def push_to_github() -> int:
    token = os.environ.get(TOKEN_ENV)
    if not token:
        print(f"[X] env var {TOKEN_ENV} not set; cannot push")
        return 1

    _git(["config", "user.name", GH_USER_NAME])
    _git(["config", "user.email", GH_USER_EMAIL])
    _git(["add", "-A"])

    status = _git(["status", "--porcelain"])
    if not status.stdout.strip():
        print("[INFO] no changes to commit, nothing to push")
        return 0

    today = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    commit = _git(["commit", "-m", f"daily update {today}"])
    if commit.returncode != 0:
        print(f"[X] commit failed: {commit.stderr.strip()}")
        return 1

    remote_url = f"https://x-access-token:{token}@github.com/{GH_REPO}.git"
    push = subprocess.run(
        ["git", "-C", str(DASHBOARD_DIR), "push", remote_url, BRANCH],
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        sanitized = push.stderr.replace(token, "***")
        print(f"[X] push failed: {sanitized.strip()}")
        return 1

    print(f"[OK] pushed to https://manumartinb.github.io/LIBERATION_SCORE/")
    return 0


# ---------------- MAIN ----------------
def main() -> int:
    try:
        if not DASHBOARD_DIR.exists():
            print(f"[X] dashboard dir not found: {DASHBOARD_DIR}")
            return 1

        payload = build_data_payload()
        changed = _payload_data_changed(payload)
        write_data_json(payload)

        latest = payload["latest"]
        print(
            f"[INFO] data.json {'updated' if changed else 'identical (timestamp refreshed)'} | "
            f"latest_date={latest['date']} tension={latest['tension']:.1f} regime={latest['regime']} | "
            f"n_days={payload['n_days']}"
        )

        if not changed:
            return 0

        return push_to_github()

    except Exception as exc:
        print(f"[X] update_dashboard failed: {exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
