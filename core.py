"""Deterministic core: load shipments, weekly cost per tonne-km, the two baselines, the anomaly test.

No AI in this file. Every number in the output CSV is computed here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

NUMERIC_COLS = ("quantity_tonnes", "distance_km", "freight_cost_inr")
REQUIRED_COLS = ("shipment_id", "origin", "destination", "route_type", *NUMERIC_COLS, "shipment_date")
# One format must parse the WHOLE column; we never guess per row (01-02-2025 must not flip meaning).
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y")


def parse_dates(values: pd.Series) -> pd.Series:
    text = values.astype(str).str.strip()
    for fmt in DATE_FORMATS:
        parsed = pd.to_datetime(text, format=fmt, errors="coerce")
        if parsed.notna().all():
            return parsed.dt.normalize()
    raise ValueError(f"Unrecognised date format. First values: {text.head(3).tolist()}")


def monday_of(dates: pd.Series) -> pd.Series:
    """Weeks run Monday to Sunday; a week is labelled by its Monday."""
    return dates - pd.to_timedelta(dates.dt.weekday, unit="D")


def load_shipments(path) -> tuple[pd.DataFrame, list[str]]:
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing columns {missing}")

    issues: list[str] = []
    for col in ("origin", "destination", "route_type"):
        df[col] = df[col].astype(str).str.strip()
    for col in NUMERIC_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # A blank date is dropped like any other bad row; the non-blank dates must still share one format.
    no_date = df["shipment_date"].isna() | (df["shipment_date"].astype(str).str.strip() == "")
    if no_date.any():
        issues.append(f"dropped {int(no_date.sum())} rows with no shipment_date: "
                      f"{df.loc[no_date, 'shipment_id'].tolist()[:10]}")
    df = df.loc[~no_date].copy()
    df["shipment_date"] = parse_dates(df["shipment_date"])

    bad = (df[list(NUMERIC_COLS)].isna().any(axis=1) | (df["quantity_tonnes"] <= 0) | (df["distance_km"] <= 0)
           | (df["freight_cost_inr"] <= 0))
    if bad.any():
        issues.append(f"dropped {int(bad.sum())} rows with missing/non-positive quantity, distance or cost: "
                      f"{df.loc[bad, 'shipment_id'].tolist()[:10]}")
    df = df.loc[~bad].copy()

    df["route"] = df["origin"] + "-" + df["destination"]
    df["tonne_km"] = df["quantity_tonnes"] * df["distance_km"]
    df["week_of"] = monday_of(df["shipment_date"])

    types_per_route = df.groupby("route")["route_type"].nunique()
    for route in types_per_route[types_per_route > 1].index:
        issues.append(f"{route} has several route_type values; using the most common one")
    return df, issues


def weekly_route_costs(df: pd.DataFrame) -> pd.DataFrame:
    """Cost per tonne-km for a route-week = total freight cost / total tonne-km of that week's shipments."""
    route_type = df.groupby("route")["route_type"].agg(lambda s: s.mode().iloc[0])
    weekly = (df.groupby(["route", "week_of"])
                .agg(shipments=("shipment_id", "count"),
                     total_cost=("freight_cost_inr", "sum"),
                     tonne_km=("tonne_km", "sum"))
                .reset_index())
    weekly["route_type"] = weekly["route"].map(route_type)
    weekly["cost_per_tonne_km"] = weekly["total_cost"] / weekly["tonne_km"]
    return weekly.sort_values(["route", "week_of"]).reset_index(drop=True)


def add_baselines(weekly: pd.DataFrame, history_weeks: int = 8) -> pd.DataFrame:
    """The two graded comparisons, exactly as defined in the brief.

    vs_own_history : average of the route's weekly cost/t-km over the `history_weeks` calendar weeks
                     strictly BEFORE this week (no look-ahead). With fewer prior weeks available, use
                     what exists; with none, the comparison is left blank (never padded/extrapolated).
    vs_similar     : average weekly cost/t-km, same week, over all OTHER routes with the same route_type.
    """
    w = weekly.copy()
    series = {r: g.set_index("week_of")["cost_per_tonne_km"] for r, g in w.groupby("route")}

    own_avg, own_n = [], []
    for route, week in zip(w["route"], w["week_of"]):
        s = series[route]
        prior = s[(s.index < week) & (s.index >= week - pd.Timedelta(weeks=history_weeks))]
        own_n.append(len(prior))
        own_avg.append(prior.mean() if len(prior) else np.nan)
    w["own_avg"] = own_avg
    w["own_weeks_used"] = own_n
    w["vs_own_pct"] = (w["cost_per_tonne_km"] / w["own_avg"] - 1) * 100

    by_week = w.pivot(index="week_of", columns="route", values="cost_per_tonne_km")
    type_of = w.drop_duplicates("route").set_index("route")["route_type"]
    peer_avg, peer_n = [], []
    for route, rtype, week in zip(w["route"], w["route_type"], w["week_of"]):
        peers = [p for p in type_of.index if type_of[p] == rtype and p != route]
        values = by_week.loc[week, peers].dropna() if peers else pd.Series(dtype=float)
        peer_n.append(len(values))
        peer_avg.append(values.mean() if len(values) else np.nan)
    w["peer_avg"] = peer_avg
    w["peer_routes_used"] = peer_n
    w["vs_similar_pct"] = (w["cost_per_tonne_km"] / w["peer_avg"] - 1) * 100
    return w


def mark_anomalies(w: pd.DataFrame, own_threshold_pct: float, peer_threshold_pct: float) -> pd.DataFrame:
    """Rising (above own trailing average) AND out of the ordinary (vs own history OR vs similar routes)."""
    w = w.copy()
    w["rising"] = w["vs_own_pct"] > 0
    w["own_unusual"] = w["vs_own_pct"] >= own_threshold_pct
    w["peer_unusual"] = w["vs_similar_pct"] >= peer_threshold_pct
    w["anomaly"] = w["rising"] & (w["own_unusual"] | w["peer_unusual"])
    return w
