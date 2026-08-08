"""
validate_against_history.py
=============================
Backtest the flood risk model against a real, documented historical event
instead of just a plausibility argument.

The event: the "2024 Lekki flood" (documented on Wikipedia) -- heavy rain
starting the morning of 4 July 2024 that didn't stop for about 10 hours,
flooding Lekki, Ikoyi and Agungi. A related event around 25-27 June 2024
also flooded Agungi/Lekki after 48 hours of rain.

This pulls REAL recorded historical rainfall for those dates from
Open-Meteo's historical weather archive (not a forecast), combines it
with the existing static susceptibility scores already computed for each
grid cell, and checks whether the named areas that were actually reported
flooded would have been flagged High/Very High risk by this model.

Requires: data/etiosa_dynamic_risk_grid.geojson (predict_flood_risk.py)
Run with: python scripts/validate_against_history.py
"""

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

# Real documented flooding dates, from the Wikipedia "2024 Lekki flood"
# article and its cited news sources.
EVENT_DATES = ["2024-06-26", "2024-06-27", "2024-07-03", "2024-07-04"]

# Real named areas reported flooded in that event, matched to this
# project's area_name field.
REPORTED_FLOODED_AREAS = ["Lekki Phase I", "Lekki Phase II", "Ikoyi"]


def fetch_historical_rain(grid, dates, batch_size=50):
    print(f"Fetching REAL historical rainfall for {dates} from Open-Meteo archive...")
    centroids_metric = grid.to_crs(METRIC_CRS).geometry.centroid
    centroids = gpd.GeoSeries(centroids_metric, crs=METRIC_CRS).to_crs(grid.crs)
    lats = [pt.y for pt in centroids]
    lons = [pt.x for pt in centroids]

    daily_rain = {d: [np.nan] * len(lats) for d in dates}

    for i in range(0, len(lats), batch_size):
        lat_batch = lats[i : i + batch_size]
        lon_batch = lons[i : i + batch_size]
        lat_param = ",".join(f"{v:.5f}" for v in lat_batch)
        lon_param = ",".join(f"{v:.5f}" for v in lon_batch)
        url = (
            "https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat_param}&longitude={lon_param}"
            f"&start_date={dates[0]}&end_date={dates[-1]}"
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
                if d in daily_rain:
                    daily_rain[d][i + j] = p

    for d in dates:
        grid[f"rain_{d}"] = daily_rain[d]
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
    grid = gpd.read_file(GRID_PATH)
    grid = fetch_historical_rain(grid, EVENT_DATES)

    print("\n--- BACKTEST: 2024 Lekki/Ikoyi flood ---")
    for date in EVENT_DATES:
        col = f"rain_{date}"
        risk, tier = compute_historical_risk(grid, col)
        grid[f"risk_{date}"] = risk
        grid[f"tier_{date}"] = tier

        print(f"\nDate: {date}")
        subset = grid[grid["area_name"].isin(REPORTED_FLOODED_AREAS)]
        summary = subset.groupby("area_name").agg(
            real_rain_mm=(col, "mean"),
            model_risk=(f"risk_{date}", "mean"),
        )
        for area, row in summary.iterrows():
            model_tier = pd.cut(
                [row["model_risk"]], bins=[-0.01, 0.25, 0.5, 0.75, 1.01], labels=["Low", "Medium", "High", "Very High"]
            )[0]
            flagged = "CORRECTLY FLAGGED" if row["model_risk"] >= 0.5 else "MISSED"
            print(
                f"  {area:<20} real rain={row['real_rain_mm']:.1f}mm  "
                f"model risk={row['model_risk']:.2f} ({model_tier})  [{flagged}]"
            )

    print("\n-------------------------------------------")


if __name__ == "__main__":
    main()
