"""
validate_against_history.py
=============================
Backtest the flood risk model against real, documented historical events
instead of just a plausibility argument.

Each entry in EVENTS is a real, dated, sourced flood report for Eti-Osa.
This pulls REAL recorded historical rainfall for each event's dates from
Open-Meteo's historical weather archive (not a forecast), combines it
with the existing static susceptibility scores already computed for each
grid cell, and checks whether the named areas that were actually reported
flooded would have been flagged High/Very High risk by this model.

Requires: data/etiosa_dynamic_risk_grid.geojson (predict_flood_risk.py)
Run with: python scripts/validate_against_history.py
"""

from datetime import date as _date, timedelta as _timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
GRID_PATH = DATA_DIR / "etiosa_dynamic_risk_grid.geojson"

METRIC_CRS = "EPSG:32631"
RAIN_BASELINE = 0.3
RAIN_SATURATION_MM = 50

# Real, dated, sourced rainfall-driven flood events affecting Eti-Osa.
# Only rain-driven events are here -- the tidal pathway has no equivalent
# historical dataset available (see MODEL_CHANGELOG.md Section 7/8), so
# it can't be backtested the same way yet.
EVENTS = [
    {
        "name": "2024 Lekki/Ikoyi flood",
        "dates": ["2024-06-26", "2024-06-27", "2024-07-03", "2024-07-04"],
        # The specific date Wikipedia's article title and lede attach the
        # event to. 26-27 June is a related but distinct earlier event
        # cited in passing; 3 July is the day before, included for
        # context on how the rain built up.
        "peak_dates": ["2024-07-04"],
        "flooded_areas": ["Lekki Phase I", "Lekki Phase II", "Ikoyi"],
        "source": "Wikipedia '2024 Lekki flood'; related news coverage of the 25-27 June and 4 July 2024 rain events.",
    },
    {
        "name": "August 2025 Lekki corridor flood",
        "dates": ["2025-08-03", "2025-08-04"],
        # 3 Aug is rain onset (Sunday night) -- flooding wasn't reported
        # until Monday 4 Aug, so only 4 Aug is a genuine test of "did the
        # model flag the day flooding actually happened."
        "peak_dates": ["2025-08-04"],
        # The source article is vaguer about exact estates than the 2024
        # event ("some areas around the Lekki corridor too -- not all"),
        # so this tests the broader Lekki-area names rather than one
        # precise estate. Weaker geographic precision than the 2024
        # event, but still a real, independently-dated, government-cited
        # rain event -- worth including with that caveat noted.
        "flooded_areas": ["Lekki Phase I", "Lekki Phase II", "Ajah"],
        "source": "Vanguard, 6 Aug 2025 -- Lagos State Commissioner for Environment and Water Resources describing flooding on Monday 4 Aug 2025 after rain that began the night of Sunday 3 Aug 2025, naming the Lekki corridor among affected areas.",
    },
]


def fetch_historical_rain(grid, dates, batch_size=50):
    """
    Fetches real historical daily rain covering `dates`, padded one extra
    day earlier, so a rolling 2-day rainfall sum (today + yesterday) can
    be computed for every requested date -- including the first one.

    This tests a hypothesis raised by the backtest results: daily-
    resolution data can split one continuous multi-day rain event across
    a calendar boundary, so the single day the news reports as "the
    flood day" doesn't always line up with the single day the data shows
    the most rain (see MODEL_CHANGELOG.md). A rolling 2-day sum should be
    less sensitive to exactly which side of midnight the rain landed on.
    """
    earliest = _date.fromisoformat(min(dates))
    latest = _date.fromisoformat(max(dates))
    padded_start = (earliest - _timedelta(days=1)).isoformat()
    padded_end = latest.isoformat()

    print(f"Fetching REAL historical rainfall for {padded_start} to {padded_end} from Open-Meteo archive...")
    centroids_metric = grid.to_crs(METRIC_CRS).geometry.centroid
    centroids = gpd.GeoSeries(centroids_metric, crs=METRIC_CRS).to_crs(grid.crs)
    lats = [pt.y for pt in centroids]
    lons = [pt.x for pt in centroids]

    daily_rain = {}

    for i in range(0, len(lats), batch_size):
        lat_batch = lats[i : i + batch_size]
        lon_batch = lons[i : i + batch_size]
        lat_param = ",".join(f"{v:.5f}" for v in lat_batch)
        lon_param = ",".join(f"{v:.5f}" for v in lon_batch)
        url = (
            "https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat_param}&longitude={lon_param}"
            f"&start_date={padded_start}&end_date={padded_end}"
            "&daily=precipitation_sum&timezone=Africa%2FLagos"
        )
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results = data if isinstance(data, list) else [data]
        for j, r in enumerate(results):
            times = r.get("daily", {}).get("time", [])
            precip = r.get("daily", {}).get("precipitation_sum", [])
            for d, p in zip(times, precip):
                daily_rain.setdefault(d, [np.nan] * len(lats))[i + j] = p

    for d, values in daily_rain.items():
        grid[f"rain_{d}"] = values

    for d in dates:
        prev = (_date.fromisoformat(d) - _timedelta(days=1)).isoformat()
        today_col = grid[f"rain_{d}"].fillna(0) if f"rain_{d}" in grid.columns else 0
        prev_col = grid[f"rain_{prev}"].fillna(0) if f"rain_{prev}" in grid.columns else 0
        grid[f"rain2d_{d}"] = today_col + prev_col

    return grid


EXTREME_RAIN_MM = 25
SEVERE_RAIN_MM = 50


def compute_historical_risk(grid, date_col):
    rain = grid[date_col].fillna(0).to_numpy()
    rainfall_factor = np.clip(rain / RAIN_SATURATION_MM, 0, 1)
    susceptibility = grid["mean_susceptibility"].to_numpy()
    base_risk = susceptibility * (RAIN_BASELINE + (1 - RAIN_BASELINE) * rainfall_factor)
    extreme_overwhelm = np.clip((rain - EXTREME_RAIN_MM) / (SEVERE_RAIN_MM - EXTREME_RAIN_MM), 0, 1)
    risk = 1 - (1 - base_risk) * (1 - extreme_overwhelm)
    tier = pd.cut(risk, bins=[-0.01, 0.25, 0.5, 0.75, 1.01], labels=["Low", "Medium", "High", "Very High"])
    return risk, tier


def main():
    base_grid = gpd.read_file(GRID_PATH)

    peak_correct_1d = 0
    peak_correct_2d = 0
    peak_total = 0

    for event in EVENTS:
        grid = fetch_historical_rain(base_grid.copy(), event["dates"])

        print(f"\n=== BACKTEST: {event['name']} ===")
        print(f"Source: {event['source']}")

        for date in event["dates"]:
            is_peak = date in event["peak_dates"]
            risk_1d, tier_1d = compute_historical_risk(grid, f"rain_{date}")
            risk_2d, tier_2d = compute_historical_risk(grid, f"rain2d_{date}")
            grid[f"risk_{date}"] = risk_1d
            grid[f"risk2d_{date}"] = risk_2d

            tag = " (REPORTED FLOOD DAY)" if is_peak else " (context/lead-in day)"
            print(f"\nDate: {date}{tag}")
            subset = grid[grid["area_name"].isin(event["flooded_areas"])]
            summary = subset.groupby("area_name").agg(
                real_rain_1d_mm=(f"rain_{date}", "mean"),
                real_rain_2d_mm=(f"rain2d_{date}", "mean"),
                model_risk_1d=(f"risk_{date}", "mean"),
                model_risk_2d=(f"risk2d_{date}", "mean"),
            )
            for area, row in summary.iterrows():
                flagged_1d = row["model_risk_1d"] >= 0.5
                flagged_2d = row["model_risk_2d"] >= 0.5
                if is_peak:
                    peak_total += 1
                    peak_correct_1d += int(flagged_1d)
                    peak_correct_2d += int(flagged_2d)
                status_1d = "FLAGGED" if flagged_1d else "missed"
                status_2d = "FLAGGED" if flagged_2d else "missed"
                print(
                    f"  {area:<20} 1-day rain={row['real_rain_1d_mm']:5.1f}mm -> risk={row['model_risk_1d']:.2f} [{status_1d}]"
                    f"   |   2-day rain={row['real_rain_2d_mm']:5.1f}mm -> risk={row['model_risk_2d']:.2f} [{status_2d}]"
                )

    print(f"\n=== ON REPORTED FLOOD DAYS ONLY: single-day = {peak_correct_1d}/{peak_total}, rolling 2-day = {peak_correct_2d}/{peak_total} ===")
    print("-------------------------------------------")


if __name__ == "__main__":
    main()
