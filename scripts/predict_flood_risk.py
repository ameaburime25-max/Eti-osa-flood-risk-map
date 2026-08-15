"""
predict_flood_risk.py
======================
The "tell the public" layer. Combines two things into one forward-looking
risk read per area, for tomorrow specifically:

  1. Susceptibility (already computed in compute_flood_risk.py) -- how
     structurally flood-prone an area is, based on elevation and distance
     to drainage. This doesn't change day to day.
  2. Tomorrow's actual rainfall forecast, pulled live from Open-Meteo (a
     free weather API, no signup/API key needed) -- this changes daily.

A susceptible area with no rain coming tomorrow isn't a real near-term
risk. A less-susceptible area getting hit with a storm might still flood
a little. Combining both gives an actual day-ahead answer: "is THIS area
at risk THIS week," not just "is this area generally vulnerable."

What this script does:
  1. Lays a 1km x 1km grid over Eti-Osa (public communication works at
     "this area," not "building #48213").
  2. Averages the per-building susceptibility scores (from
     compute_flood_risk.py) into each grid cell.
  3. Looks up the nearest real neighborhood name (Lekki Phase 1, Ikoyi,
     etc.) for each cell from OpenStreetMap, so results are readable.
  4. Fetches tomorrow's forecast rainfall for every cell from Open-Meteo.
  5. Combines susceptibility + forecast rain into one "risk tomorrow"
     score per cell, plots it, and prints a ranked list of the areas to
     watch.

Requires (from earlier scripts):
  data/etiosa_flood_risk.geojson  (compute_flood_risk.py)
  data/etiosa_boundary.geojson    (build_basemap.py)

Run with: python scripts/predict_flood_risk.py
Output: outputs/etiosa_flood_forecast_map.png
Also saves: data/etiosa_dynamic_risk_grid.geojson
"""

from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import osmnx as ox
import pandas as pd
import requests
from shapely.geometry import box

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"

RISK_BUILDINGS_PATH = DATA_DIR / "etiosa_flood_risk.geojson"
BOUNDARY_PATH = DATA_DIR / "etiosa_boundary.geojson"
PLACES_CACHE_PATH = DATA_DIR / "etiosa_places.geojson"
GRID_OUT_PATH = DATA_DIR / "etiosa_dynamic_risk_grid.geojson"

METRIC_CRS = "EPSG:32631"  # UTM zone 31N, meters, covers Lagos
CELL_SIZE_M = 1000  # 1km x 1km grid cells

# v1 formula: even with zero forecast rain, a susceptible area keeps a
# small baseline risk (standing water from prior days, tidal effects
# near the lagoon); risk climbs toward the full susceptibility score as
# forecast rain approaches/exceeds RAIN_SATURATION_MM.
RAIN_BASELINE = 0.3
RAIN_SATURATION_MM = 50  # daily rainfall (mm) at which rainfall_factor maxes out

# Calibrated against a real, documented event (the July 2024 Lekki/Ikoyi
# flood): at 25mm/day and below, susceptibility differentiates risk as
# before. Between 25-50mm/day, an additional "system-wide overwhelm" term
# kicks in, reflecting that Lagos State's own reporting on these floods
# blames overwhelmed/blocked drainage capacity everywhere, not just at
# the least-susceptible spots -- extreme rain floods areas that a purely
# relative susceptibility ranking would call "moderate."
EXTREME_RAIN_MM = 25
SEVERE_RAIN_MM = 50

# Drainage- and evaporation-aware rain carryover (replaces a flat
# yesterday+today sum -- see compute_dynamic_risk()). carryover_fraction
# is how much of yesterday's rain is even still sitting there as standing
# water rather than having already drained away, scaled by this cell's
# own mean_effective_drainage_score (0 = excellent/unblocked drainage,
# 1 = distant from any working drain -- see compute_flood_risk.py Section
# 10 of MODEL_CHANGELOG.md). Whatever's still sitting there is then
# reduced further by real ET0 (FAO-56 reference evapotranspiration,
# mm/day, from Open-Meteo) -- the sun genuinely dries up shallow standing
# water regardless of whether it was ever going to drain. Kept in sync
# with the same constants in validate_against_history.py and
# estimate_road_risk.py.
MIN_CARRYOVER = 0.15
MAX_CARRYOVER = 0.85


def require_file(path, produced_by):
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run {produced_by} first.")


# ---------------------------------------------------------------------------
# Step 1: Build a 1km grid over Eti-Osa
# ---------------------------------------------------------------------------
def build_grid(boundary_gdf, cell_size_m=CELL_SIZE_M):
    """
    Lays a regular grid of square cells over the district in a
    meters-based CRS, then clips it to the real boundary (dropping cells
    that fall entirely outside, and trimming edge cells to the true
    shape) -- same clipping approach as the drainage script, for the same
    reason: don't let anything extend past the real district.
    """
    print(f"Building a {cell_size_m}m grid over Eti-Osa...")
    boundary_m = boundary_gdf.to_crs(METRIC_CRS)
    minx, miny, maxx, maxy = boundary_m.total_bounds

    xs = np.arange(minx, maxx, cell_size_m)
    ys = np.arange(miny, maxy, cell_size_m)
    cells = [box(x, y, x + cell_size_m, y + cell_size_m) for x in xs for y in ys]

    grid = gpd.GeoDataFrame(geometry=cells, crs=METRIC_CRS)
    grid = gpd.clip(grid, boundary_m[["geometry"]])
    # Drop slivers left over from clipping cells that barely touched the
    # boundary edge -- keep only cells that are at least 20% intact.
    grid = grid[grid.geometry.area > (cell_size_m * cell_size_m * 0.2)].copy()
    grid = grid.reset_index(drop=True)
    grid["cell_id"] = grid.index
    print(f"  Grid has {len(grid)} cells.")
    return grid.to_crs(boundary_gdf.crs)


# ---------------------------------------------------------------------------
# Step 2: Average building-level susceptibility into each grid cell
# ---------------------------------------------------------------------------
def aggregate_risk_to_grid(grid, risk_buildings):
    print("Averaging building-level susceptibility into grid cells...")
    grid_m = grid.to_crs(METRIC_CRS)
    buildings_m = risk_buildings.to_crs(METRIC_CRS).copy()
    buildings_m["geometry"] = buildings_m.geometry.centroid

    joined = gpd.sjoin(buildings_m, grid_m[["cell_id", "geometry"]], predicate="within")

    # mean_effective_drainage_score (compute_flood_risk.py -- drainage
    # proximity discounted by the nearest drain's blockage_risk) is
    # aggregated here too, purely additive, so it's available for a
    # drainage-aware rain carryover between days (see compute_dynamic_risk).
    # Falls back to the older raw drainage_score if a stale
    # etiosa_flood_risk.geojson without the blockage upgrade is in use.
    drainage_col = "effective_drainage_score" if "effective_drainage_score" in buildings_m.columns else "drainage_score"
    agg = (
        joined.groupby("cell_id")
        .agg(
            mean_susceptibility=("risk_score", "mean"),
            mean_effective_drainage_score=(drainage_col, "mean"),
            building_count=("risk_score", "size"),
        )
        .reset_index()
    )

    grid = grid.merge(agg, on="cell_id", how="left")
    before = len(grid)
    grid = grid[grid["building_count"].notna()].copy()
    print(f"  {before - len(grid)} empty cells (no buildings) dropped; {len(grid)} cells remain.")
    return grid


# ---------------------------------------------------------------------------
# Step 3: Attach a human-readable neighborhood name to each cell
# ---------------------------------------------------------------------------
def get_place_names(boundary_gdf):
    """
    osmnx.features_from_polygon() with tags={'place': [...]} pulls named
    neighborhood/suburb/quarter labels that OSM mappers have placed
    within Eti-Osa (e.g. Lekki Phase 1, Ikoyi) -- these are usually
    single points, not full boundary polygons, but a point is enough to
    find the nearest one to each grid cell.
    """
    if PLACES_CACHE_PATH.exists():
        print(f"Place names already cached at {PLACES_CACHE_PATH}, loading from disk.")
        return gpd.read_file(PLACES_CACHE_PATH)

    print("Downloading neighborhood/place names from OSM...")
    polygon = boundary_gdf.geometry.iloc[0]
    tags = {"place": ["suburb", "neighbourhood", "quarter", "town", "hamlet", "city_block"]}
    try:
        places = ox.features_from_polygon(polygon, tags=tags)
    except ox._errors.InsufficientResponseError:
        print("  No named places found; cells will be labeled by coordinates instead.")
        return gpd.GeoDataFrame(columns=["name", "geometry"], geometry="geometry", crs=boundary_gdf.crs)

    places = places.copy()
    places["geometry"] = places.geometry.centroid
    if "name" not in places.columns:
        places["name"] = None
    places = places[["name", "geometry"]]
    places["name"] = places["name"].astype(str)
    places.to_file(PLACES_CACHE_PATH, driver="GeoJSON")
    print(f"  Retrieved {len(places)} named places. Cached to {PLACES_CACHE_PATH}")
    return places


def label_grid_with_places(grid, places):
    if len(places) == 0:
        grid = grid.copy()
        centroids_metric = grid.to_crs(METRIC_CRS).geometry.centroid
        centroids_wgs84 = gpd.GeoSeries(centroids_metric, crs=METRIC_CRS).to_crs(grid.crs)
        grid["area_name"] = [f"Unnamed area ({pt.y:.4f}, {pt.x:.4f})" for pt in centroids_wgs84]
        return grid

    print("Matching each grid cell to its nearest named place...")
    grid_m = grid.to_crs(METRIC_CRS).copy()
    grid_m["geometry"] = grid_m.geometry.centroid
    places_m = places.to_crs(METRIC_CRS)

    joined = gpd.sjoin_nearest(grid_m, places_m[["name", "geometry"]], distance_col="_dist")
    joined = joined.drop_duplicates(subset="cell_id").sort_values("cell_id")

    grid = grid.copy()
    grid["area_name"] = joined["name"].to_numpy()
    grid.loc[grid["area_name"].isin(["None", "nan", ""]), "area_name"] = "Unnamed area"
    return grid


# ---------------------------------------------------------------------------
# Step 4: Fetch tomorrow's rainfall forecast for every cell
# ---------------------------------------------------------------------------
def fetch_rainfall_forecast(grid, batch_size=50):
    """
    Open-Meteo's forecast endpoint accepts comma-separated lists of
    latitudes/longitudes in one request and returns one forecast per
    coordinate -- so instead of one HTTP request per grid cell, this
    batches many cells into each call. 'daily=precipitation_sum' with
    past_days=1&forecast_days=2 returns a 3-value list per location:
    [yesterday's actual rain, today's rain, tomorrow's forecast rain] in
    millimeters.

    Yesterday's rain is fetched (a change from the previous 2-value
    today/tomorrow-only version) so a rolling 2-day accumulated rainfall
    total can be computed downstream -- validated against two real
    historical flood events (see MODEL_CHANGELOG.md) to catch flooding
    driven by rain accumulating over more than one calendar day, which a
    single midnight-to-midnight total can miss when the rain happens to
    straddle a day boundary.
    """
    print("Fetching yesterday's, today's, and tomorrow's rainfall (and ET0 evapotranspiration) from Open-Meteo...")
    # Centroid computed in meters, then reprojected back to WGS84 -- Open-
    # Meteo's API needs real lat/lon degrees, but the midpoint itself
    # should be found in a flat coordinate system, not directly in degrees.
    centroids_metric = grid.to_crs(METRIC_CRS).geometry.centroid
    centroids = gpd.GeoSeries(centroids_metric, crs=METRIC_CRS).to_crs(grid.crs)
    lats = [pt.y for pt in centroids]
    lons = [pt.x for pt in centroids]
    yesterday_vals = [np.nan] * len(lats)
    today_forecasts = [np.nan] * len(lats)
    tomorrow_forecasts = [np.nan] * len(lats)
    et0_yesterday_vals = [np.nan] * len(lats)
    et0_today_vals = [np.nan] * len(lats)
    et0_tomorrow_vals = [np.nan] * len(lats)

    for i in range(0, len(lats), batch_size):
        lat_batch = lats[i : i + batch_size]
        lon_batch = lons[i : i + batch_size]
        lat_param = ",".join(f"{v:.5f}" for v in lat_batch)
        lon_param = ",".join(f"{v:.5f}" for v in lon_batch)
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat_param}&longitude={lon_param}"
            # et0_fao_evapotranspiration: FAO-56 Penman-Monteith reference
            # evapotranspiration (mm/day) -- a real, standard meteorological
            # variable (temperature + wind + humidity + solar radiation),
            # not a guessed constant. Fetched in the same batched call as
            # rain, so no extra API cost.
            "&daily=precipitation_sum,et0_fao_evapotranspiration"
            "&past_days=1&forecast_days=2&timezone=Africa%2FLagos"
        )
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # With multiple coordinates, Open-Meteo returns a JSON array (one
        # response object per location); with just one, it returns a
        # single object -- handle both.
        results = data if isinstance(data, list) else [data]
        for j, r in enumerate(results):
            daily = r.get("daily", {})
            precip = daily.get("precipitation_sum", [])
            yesterday_mm = precip[0] if len(precip) > 0 else np.nan
            today_mm = precip[1] if len(precip) > 1 else yesterday_mm
            tomorrow_mm = precip[2] if len(precip) > 2 else today_mm
            yesterday_vals[i + j] = yesterday_mm
            today_forecasts[i + j] = today_mm
            tomorrow_forecasts[i + j] = tomorrow_mm

            et0 = daily.get("et0_fao_evapotranspiration", [])
            et0_yesterday = et0[0] if len(et0) > 0 else np.nan
            et0_today = et0[1] if len(et0) > 1 else et0_yesterday
            et0_tomorrow = et0[2] if len(et0) > 2 else et0_today
            et0_yesterday_vals[i + j] = et0_yesterday
            et0_today_vals[i + j] = et0_today
            et0_tomorrow_vals[i + j] = et0_tomorrow

    grid = grid.copy()
    n_missing = int(pd.isna(yesterday_vals).sum() + pd.isna(today_forecasts).sum() + pd.isna(tomorrow_forecasts).sum())
    if n_missing:
        print(f"  {n_missing} cell-day rainfall value(s) missing; treated as 0mm.")
    grid["rain_mm_yesterday"] = pd.Series(yesterday_vals).fillna(0).to_numpy()
    grid["forecast_rain_mm_today"] = pd.Series(today_forecasts).fillna(0).to_numpy()
    grid["forecast_rain_mm_tomorrow"] = pd.Series(tomorrow_forecasts).fillna(0).to_numpy()
    # ET0 missing values fall back to a modest 3mm/day (a typical humid-
    # tropical Lagos value) rather than 0, since treating missing ET0 as
    # "no evaporation happened" would bias the carryover formula toward
    # over-predicting standing water.
    grid["et0_mm_yesterday"] = pd.Series(et0_yesterday_vals).fillna(3.0).to_numpy()
    grid["et0_mm_today"] = pd.Series(et0_today_vals).fillna(3.0).to_numpy()
    grid["et0_mm_tomorrow"] = pd.Series(et0_tomorrow_vals).fillna(3.0).to_numpy()
    return grid


# ---------------------------------------------------------------------------
# Step 5: Combine susceptibility + forecast into tomorrow's risk
# ---------------------------------------------------------------------------
def compute_carried_over_water(prev_rain_mm, prev_et0_mm, effective_drainage_score):
    """
    How much of a previous day's rain is still sitting as standing water
    today, instead of a flat "just add it all" assumption. Two real loss
    mechanisms, applied in sequence:
      1. Drainage: only the fraction of yesterday's rain this specific
         cell's own drainage COULDN'T clear stays behind as standing
         water -- carryover_fraction scales with mean_effective_drainage_score
         (0 = excellent drainage -> most of it actually left; 1 = blocked/
         distant drainage -> most of it is still there).
      2. Evaporation: whatever's still sitting there gets reduced further
         by that day's real ET0 (FAO-56 reference evapotranspiration, mm)
         -- the sun dries up shallow standing water regardless of whether
         it was ever going to drain. Can't evaporate more than what's
         actually there, hence the clip at 0.
    """
    carryover_fraction = MIN_CARRYOVER + (MAX_CARRYOVER - MIN_CARRYOVER) * effective_drainage_score
    water_after_drainage = prev_rain_mm * carryover_fraction
    water_after_evaporation = np.clip(water_after_drainage - prev_et0_mm, 0, None)
    return water_after_evaporation


def compute_dynamic_risk(grid):
    print("Combining susceptibility with drainage- and evaporation-aware rain carryover into today's and tomorrow's risk scores...")
    susceptibility = grid["mean_susceptibility"].to_numpy()
    grid = grid.copy()

    # Effective 2-day rain: today's actual rain plus whatever of
    # yesterday's rain is realistically still standing there once this
    # cell's own drainage condition and real evaporation are both
    # accounted for -- not a flat yesterday+today sum. A flat sum treats
    # a well-drained street and a blocked-drain street identically, which
    # doesn't match how standing water actually behaves. Column name kept
    # as "rolling_2day_rain_mm_*" for backward compatibility with
    # estimate_road_risk.py and app.py, which both read it -- see
    # MODEL_CHANGELOG.md for the fuller writeup and the historical
    # backtest comparing this against the older flat-sum version.
    effective_drainage_score = grid["mean_effective_drainage_score"].to_numpy()
    carried_from_yesterday = compute_carried_over_water(
        grid["rain_mm_yesterday"].to_numpy(), grid["et0_mm_yesterday"].to_numpy(), effective_drainage_score
    )
    carried_from_today = compute_carried_over_water(
        grid["forecast_rain_mm_today"].to_numpy(), grid["et0_mm_today"].to_numpy(), effective_drainage_score
    )
    rolling_rain = {
        "today": grid["forecast_rain_mm_today"].to_numpy() + carried_from_yesterday,
        "tomorrow": grid["forecast_rain_mm_tomorrow"].to_numpy() + carried_from_today,
    }

    for day in ["today", "tomorrow"]:
        rain = rolling_rain[day]
        rainfall_factor = np.clip(rain / RAIN_SATURATION_MM, 0, 1)
        base_risk = susceptibility * (RAIN_BASELINE + (1 - RAIN_BASELINE) * rainfall_factor)
        extreme_overwhelm = np.clip((rain - EXTREME_RAIN_MM) / (SEVERE_RAIN_MM - EXTREME_RAIN_MM), 0, 1)
        dynamic_risk = 1 - (1 - base_risk) * (1 - extreme_overwhelm)
        grid[f"rolling_2day_rain_mm_{day}"] = rain
        grid[f"dynamic_risk_score_{day}"] = dynamic_risk
        grid[f"risk_tier_{day}"] = pd.cut(
            dynamic_risk, bins=[-0.01, 0.25, 0.5, 0.75, 1.01], labels=["Low", "Medium", "High", "Very High"]
        )

    # Backward-compatible alias: the bare (unsuffixed) name now correctly
    # means TODAY. This used to silently hold TOMORROW's value under a
    # "today" label everywhere it was read downstream (estimate_wall_flexure.py's
    # "today" live check, in particular) -- that mislabeling was a real bug,
    # caught when a user's own house showed flood risk while dry outside.
    grid["dynamic_risk_score"] = grid["dynamic_risk_score_today"]
    grid["risk_tier"] = grid["risk_tier_today"]
    return grid


# ---------------------------------------------------------------------------
# Step 6: Plot + report
# ---------------------------------------------------------------------------
def plot_forecast_map(grid, boundary_gdf, forecast_date):
    fig, ax = plt.subplots(figsize=(12, 12))

    grid.plot(
        column="dynamic_risk_score_tomorrow",
        cmap="RdYlGn_r",
        ax=ax,
        edgecolor="white",
        linewidth=0.3,
        legend=True,
        legend_kwds={"label": "Flood risk tomorrow (0 = low, 1 = high)", "shrink": 0.6},
        vmin=0,
        vmax=1,
    )
    boundary_gdf.boundary.plot(ax=ax, color="black", linewidth=1)

    # Label the top 5 highest-risk cells -- this is the "tell the public"
    # part: names people recognize, not coordinates. Offset each label
    # with a small pointer arrow (alternating up/down) rather than
    # stamping text directly on the cell, so labels for adjacent
    # high-risk cells don't overlap each other.
    top5 = grid.nlargest(5, "dynamic_risk_score_tomorrow")
    for i, (_, row) in enumerate(top5.iterrows()):
        c = row.geometry.centroid
        y_offset = 25 if i % 2 == 0 else -25
        ax.annotate(
            row["area_name"],
            xy=(c.x, c.y),
            xytext=(0, y_offset),
            textcoords="offset points",
            fontsize=8,
            fontweight="bold",
            ha="center",
            color="black",
            arrowprops=dict(arrowstyle="-", color="black", lw=0.8),
            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="black", alpha=0.9),
        )

    ax.set_title(f"Eti-Osa LGA: Flood Risk Forecast for {forecast_date.isoformat()}")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")

    out_path = OUTPUTS_DIR / "etiosa_flood_forecast_map.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    print(f"\nForecast map saved to {out_path}")


def print_report(grid, forecast_date):
    print(f"\n--- FLOOD RISK FORECAST FOR {forecast_date.isoformat()} ---")
    print(f"Grid cells (populated areas only): {len(grid)}")
    print(
        f"Forecast rainfall across Eti-Osa: min={grid['forecast_rain_mm_tomorrow'].min():.1f}mm, "
        f"max={grid['forecast_rain_mm_tomorrow'].max():.1f}mm, "
        f"mean={grid['forecast_rain_mm_tomorrow'].mean():.1f}mm"
    )
    print("\nRisk tier breakdown:")
    print(grid["risk_tier_tomorrow"].value_counts().sort_index())

    print(f"\nTop 10 areas to watch for {forecast_date.isoformat()}:")
    top10 = grid.nlargest(10, "dynamic_risk_score_tomorrow")
    for _, row in top10.iterrows():
        print(
            f"  {row['area_name']:<30} risk={row['dynamic_risk_score_tomorrow']:.2f} "
            f"({row['risk_tier_tomorrow']})  forecast_rain={row['forecast_rain_mm_tomorrow']:.1f}mm  "
            f"susceptibility={row['mean_susceptibility']:.2f}"
        )
    print("-----------------------------------------------")


def main():
    require_file(RISK_BUILDINGS_PATH, "compute_flood_risk.py")
    require_file(BOUNDARY_PATH, "build_basemap.py")

    risk_buildings = gpd.read_file(RISK_BUILDINGS_PATH)
    boundary = gpd.read_file(BOUNDARY_PATH)

    grid = build_grid(boundary)
    grid = aggregate_risk_to_grid(grid, risk_buildings)

    places = get_place_names(boundary)
    grid = label_grid_with_places(grid, places)

    grid = fetch_rainfall_forecast(grid)
    grid = compute_dynamic_risk(grid)

    grid_to_save = grid.drop(columns=["cell_id"])
    grid_to_save["risk_tier"] = grid_to_save["risk_tier"].astype(str)
    grid_to_save["risk_tier_today"] = grid_to_save["risk_tier_today"].astype(str)
    grid_to_save["risk_tier_tomorrow"] = grid_to_save["risk_tier_tomorrow"].astype(str)
    grid_to_save.to_file(GRID_OUT_PATH, driver="GeoJSON")
    print(f"Dynamic risk grid saved to {GRID_OUT_PATH}")

    forecast_date = date.today() + timedelta(days=1)
    plot_forecast_map(grid, boundary, forecast_date)
    print_report(grid, forecast_date)


if __name__ == "__main__":
    main()
