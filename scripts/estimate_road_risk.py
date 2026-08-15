"""
estimate_road_risk.py
=======================
Street-level flood risk: instead of "Langbasa is at risk," this scores
every individual real road segment in Eti-Osa (21,204 of them) so the
map can show exactly which roads are predicted to flood today, not just
which neighborhood.

Each road gets its own terrain susceptibility score, using the same
elevation + drainage-distance method already proven on buildings
(compute_flood_risk.py) -- this is what actually differentiates one
road from another inside the same neighborhood. That's combined with
tomorrow's rain forecast, inherited from whichever 1km grid cell
(predict_flood_risk.py) the road sits in, since rain genuinely doesn't
vary street-to-street the way terrain does.

Requires:
  data/etiosa_roads.geojson (build_basemap.py)
  data/etiosa_dem.tif (build_basemap.py)
  data/etiosa_drainage_lines.geojson, etiosa_drainage_polygons.geojson (get_drainage.py)
  data/etiosa_dynamic_risk_grid.geojson (predict_flood_risk.py)

Run with: python scripts/estimate_road_risk.py
Saves: data/etiosa_road_risk.geojson
"""

from pathlib import Path

from datetime import date, timedelta

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

ROADS_PATH = DATA_DIR / "etiosa_roads.geojson"
DEM_PATH = DATA_DIR / "etiosa_dem.tif"
DRAINAGE_LINES_PATH = DATA_DIR / "etiosa_drainage_lines.geojson"
DRAINAGE_POLYGONS_PATH = DATA_DIR / "etiosa_drainage_polygons.geojson"
GRID_PATH = DATA_DIR / "etiosa_dynamic_risk_grid.geojson"
OUT_PATH = DATA_DIR / "etiosa_road_risk.geojson"

METRIC_CRS = "EPSG:32631"
RAIN_BASELINE = 0.3
RAIN_SATURATION_MM = 50

# Same extreme-rain-overwhelm term used in predict_flood_risk.py since
# the July 2024 historical validation fix -- this script had been left
# out of that fix until now (caught by re-reading the code while
# investigating a separate rolling-rain improvement), so road-level risk
# was structurally capped at susceptibility during genuinely extreme
# rain, same bug the area-level model had before.
EXTREME_RAIN_MM = 25
SEVERE_RAIN_MM = 50

# Real named tidal waterways in Eti-Osa (from data/etiosa_drainage_polygons.geojson) --
# these are the only water bodies actually connected to the ocean/lagoon tide, as
# opposed to small inland drainage ponds/canals.
TIDAL_WATER_NAMES = ["Lagos Lagoon", "Five Cowries Creek", "Commodore Channel"]

# Absolute elevation cutoff for tidal exposure. This is deliberately NOT the same
# elevation_score used for rain-driven susceptibility, because that's a RELATIVE
# ranking against every road in the district ("low compared to other Eti-Osa
# roads"), which has no physical meaning for tidal reach. Tide water only reaches
# roads that are absolutely close to sea level -- observed tide this week peaked
# at 0.55m, so 2.0m gives a conservative margin for SRTM's own vertical
# uncertainty and the datum difference between the DEM and the tide data.
TIDAL_ELEVATION_CUTOFF_M = 2.0

# Representative coastal reference point (Victoria Island shoreline) for pulling a
# single regional tide forecast -- tide doesn't vary meaningfully within Eti-Osa's
# short coastline at the Marine API's ~8km grid resolution, same logic as using one
# grid cell's rain forecast for a whole neighbourhood.
COASTAL_REF_LAT, COASTAL_REF_LON = 6.4241, 3.4219


def require_file(path, produced_by):
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run {produced_by} first.")


def sample_elevation(roads, dem_path):
    print("Sampling elevation at each road's midpoint...")
    original_crs = roads.crs
    midpoints_metric = roads.to_crs(METRIC_CRS).geometry.interpolate(0.5, normalized=True)
    midpoints = gpd.GeoSeries(midpoints_metric, crs=METRIC_CRS).to_crs(original_crs)
    coords = [(pt.x, pt.y) for pt in midpoints]
    with rasterio.open(dem_path) as src:
        elevations = [val[0] for val in src.sample(coords)]
    return np.array(elevations, dtype=float), midpoints


def compute_drainage_distance(midpoints, drainage_lines, drainage_polygons):
    print("Computing distance from each road to the nearest drainage feature...")
    drainage = pd.concat(
        [drainage_lines[["geometry"]], drainage_polygons[["geometry"]]], ignore_index=True
    )
    drainage_gdf = gpd.GeoDataFrame(drainage, geometry="geometry", crs=drainage_lines.crs).to_crs(METRIC_CRS)
    points_gdf = gpd.GeoDataFrame(geometry=midpoints).to_crs(METRIC_CRS)
    joined = gpd.sjoin_nearest(points_gdf, drainage_gdf, distance_col="dist_to_drainage_m")
    joined = joined[~joined.index.duplicated(keep="first")]
    return joined["dist_to_drainage_m"].to_numpy()


def compute_susceptibility(elevation, drainage_dist):
    elev_low, elev_high = np.quantile(elevation, 0.05), np.quantile(elevation, 0.95)
    elevation_score = np.clip(1 - (elevation - elev_low) / (elev_high - elev_low), 0, 1)
    dist_cap = np.quantile(drainage_dist, 0.95)
    drainage_score = np.clip(drainage_dist / dist_cap, 0, 1)
    susceptibility = 0.6 * elevation_score + 0.4 * drainage_score
    return susceptibility, elevation_score


def identify_tidal_water(drainage_polygons):
    print("Isolating real tidal coastline (Lagos Lagoon and connected creeks)...")
    tidal = drainage_polygons[drainage_polygons["name"].isin(TIDAL_WATER_NAMES)]
    return tidal


def compute_coastal_exposure(midpoints, tidal_water):
    print("Computing distance from each road to the real coastline...")
    tidal_water_m = tidal_water.to_crs(METRIC_CRS)
    points_gdf = gpd.GeoDataFrame(geometry=midpoints).to_crs(METRIC_CRS)
    joined = gpd.sjoin_nearest(points_gdf, tidal_water_m[["geometry"]], distance_col="coastal_dist_m")
    joined = joined[~joined.index.duplicated(keep="first")]
    dist = joined["coastal_dist_m"].to_numpy()
    cap = np.quantile(dist, 0.95)
    exposure = np.clip(1 - dist / cap, 0, 1)
    return dist, exposure


def fetch_tide_forecast():
    """
    Returns {"today": (peak_height_m, tide_factor), "tomorrow": (peak_height_m, tide_factor)}.

    past_days=30 pulls roughly two full spring-neap tidal cycles (~14.8
    days each) of REAL recent tide data alongside the forecast, so "is
    today/tomorrow's tide unusual" is judged against genuine tidal
    variation -- not just wherever a single 7-day forecast window
    happens to sit in the cycle. The old 7-day-only baseline meant
    almost every day read as "near this week's peak" purely from routine
    semi-diurnal/spring-neap cycling, not a real signal of anomalous,
    flood-triggering tide behaviour. Caught when a real house (Banana
    Island) showed high tidal risk while genuinely dry outside.
    """
    print("Fetching live tide forecast from Open-Meteo Marine API...")
    url = (
        "https://marine-api.open-meteo.com/v1/marine"
        f"?latitude={COASTAL_REF_LAT}&longitude={COASTAL_REF_LON}"
        "&hourly=sea_level_height_msl&forecast_days=7&past_days=30&timezone=Africa%2FLagos"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    df = pd.DataFrame(
        {"time": pd.to_datetime(data["hourly"]["time"]), "height": data["hourly"]["sea_level_height_msl"]}
    )
    df["date"] = df["time"].dt.date
    low, high = df["height"].quantile(0.05), df["height"].quantile(0.95)

    result = {}
    for day, offset in [("today", 0), ("tomorrow", 1)]:
        target_date = date.today() + timedelta(days=offset)
        day_rows = df[df["date"] == target_date]
        day_max = day_rows["height"].max() if len(day_rows) > 0 else df["height"].max()
        tide_factor = float(np.clip((day_max - low) / (high - low), 0, 1)) if high > low else 0.0
        result[day] = (float(day_max), tide_factor)
        print(f"  {day.capitalize()}'s peak sea level: {day_max:.2f}m -> tide_factor={tide_factor:.2f}")

    print(f"  (37-day baseline range used for calibration: {low:.2f}m to {high:.2f}m)")
    return result


def clean_road_name(value):
    import ast

    if isinstance(value, list):
        value = value[0] if len(value) > 0 else None
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "Unnamed road"
    value = str(value).strip()
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, list) and len(parsed) > 0:
                value = str(parsed[0])
        except (ValueError, SyntaxError):
            value = value.strip("[]'\" ")
    return "Unnamed road" if value in ("None", "nan", "", "[]") else value


def attach_forecast_rain(roads, midpoints, grid):
    """
    Pulls both the raw single-day rain (for display) and the rolling
    2-day accumulated rain (for the actual risk calculation) from each
    road's nearest grid cell -- see predict_flood_risk.py for why the
    rolling sum is what drives risk now.
    """
    print("Matching each road to its grid cell's rain forecast (today and tomorrow)...")
    points_gdf = gpd.GeoDataFrame(geometry=midpoints, crs=roads.crs).to_crs(METRIC_CRS)
    grid_m = grid.to_crs(METRIC_CRS)[
        [
            "area_name", "forecast_rain_mm_today", "forecast_rain_mm_tomorrow",
            "rolling_2day_rain_mm_today", "rolling_2day_rain_mm_tomorrow", "geometry",
        ]
    ]
    joined = gpd.sjoin_nearest(points_gdf, grid_m, distance_col="_dist")
    joined = joined[~joined.index.duplicated(keep="first")]
    return (
        joined["area_name"].to_numpy(),
        joined["forecast_rain_mm_today"].to_numpy(),
        joined["forecast_rain_mm_tomorrow"].to_numpy(),
        joined["rolling_2day_rain_mm_today"].to_numpy(),
        joined["rolling_2day_rain_mm_tomorrow"].to_numpy(),
    )


def main():
    require_file(ROADS_PATH, "build_basemap.py")
    require_file(DEM_PATH, "build_basemap.py")
    require_file(DRAINAGE_LINES_PATH, "get_drainage.py")
    require_file(DRAINAGE_POLYGONS_PATH, "get_drainage.py")
    require_file(GRID_PATH, "predict_flood_risk.py")

    roads = gpd.read_file(ROADS_PATH)
    drainage_lines = gpd.read_file(DRAINAGE_LINES_PATH)
    drainage_polygons = gpd.read_file(DRAINAGE_POLYGONS_PATH)
    grid = gpd.read_file(GRID_PATH)

    elevation, midpoints = sample_elevation(roads, DEM_PATH)
    drainage_dist = compute_drainage_distance(midpoints, drainage_lines, drainage_polygons)
    susceptibility, elevation_score = compute_susceptibility(elevation, drainage_dist)
    (
        area_name, forecast_rain_today, forecast_rain_tomorrow,
        rolling_rain_today, rolling_rain_tomorrow,
    ) = attach_forecast_rain(roads, midpoints, grid)

    tidal_water = identify_tidal_water(drainage_polygons)
    coastal_dist, coastal_proximity = compute_coastal_exposure(midpoints, tidal_water)
    # Tidal flooding only threatens LOW-LYING roads near the coast -- proximity alone
    # isn't enough (a bridge deck spanning the lagoon is "at distance zero" but far too
    # high up to be touched by a 0.5m tide). Gating proximity by elevation_score means
    # only low ground near the water gets flagged, which matches the real "tidal
    # locking" mechanism (drains near the coast can't discharge, so low streets pool).
    tidal_elevation_factor = np.clip(1 - elevation / TIDAL_ELEVATION_CUTOFF_M, 0, 1)
    coastal_exposure = coastal_proximity * tidal_elevation_factor
    tide = fetch_tide_forecast()  # {"today": (peak_m, factor), "tomorrow": (peak_m, factor)}

    # FABDEM samples bridges spanning open water at ~0m (the bare-earth
    # correction has no real ground to reveal under an elevated deck, so
    # it falls back to the surrounding water surface). That falsely
    # triggers the tidal elevation cutoff. A bridge deck's flood risk is
    # a structural clearance question, not a terrain one -- so use OSM's
    # own bridge tag to exclude bridges from the tidal pathway directly,
    # rather than trying to out-guess the DEM.
    is_bridge = roads["bridge"].notna() & (roads["bridge"].astype(str).str.lower() != "no")
    n_bridges = int(is_bridge.sum())
    print(f"Excluding {n_bridges} bridge segment(s) from tidal risk (elevated structure, not ground)...")

    roads = roads.copy()
    roads["is_bridge"] = is_bridge
    roads["road_name"] = roads["name"].apply(clean_road_name)
    roads["elevation_m"] = elevation
    roads["dist_to_drainage_m"] = drainage_dist
    roads["coastal_dist_m"] = coastal_dist
    roads["susceptibility"] = susceptibility
    roads["area_name"] = area_name

    # Compute both TODAY's and TOMORROW's risk with the same formula used
    # at the area level -- previously only one pathway existed and it was
    # actually built from tomorrow's forecast under a "today" label (the
    # bug a user's own dry house exposed). Now both are genuine and the
    # app can let people choose which to look at. Rainfall risk uses the
    # rolling 2-day accumulated rain plus the extreme-overwhelm term
    # (both validated in predict_flood_risk.py / MODEL_CHANGELOG.md).
    display_rain_by_day = {"today": forecast_rain_today, "tomorrow": forecast_rain_tomorrow}
    rolling_rain_by_day = {"today": rolling_rain_today, "tomorrow": rolling_rain_tomorrow}
    for day in ["today", "tomorrow"]:
        tide_m, tide_factor = tide[day]
        tidal_risk = coastal_exposure * tide_factor
        tidal_risk = np.where(is_bridge.to_numpy(), 0.0, tidal_risk)

        rain = rolling_rain_by_day[day]
        rainfall_factor = np.clip(rain / RAIN_SATURATION_MM, 0, 1)
        base_risk = susceptibility * (RAIN_BASELINE + (1 - RAIN_BASELINE) * rainfall_factor)
        extreme_overwhelm = np.clip((rain - EXTREME_RAIN_MM) / (SEVERE_RAIN_MM - EXTREME_RAIN_MM), 0, 1)
        rainfall_risk = 1 - (1 - base_risk) * (1 - extreme_overwhelm)

        combined_risk = np.maximum(rainfall_risk, tidal_risk)
        flood_cause = np.where(tidal_risk > rainfall_risk, "tidal", "rainfall")

        roads[f"forecast_rain_mm_{day}"] = display_rain_by_day[day]
        roads[f"rolling_2day_rain_mm_{day}"] = rain
        roads[f"{day}_tide_m"] = tide_m
        roads[f"rainfall_risk_{day}"] = rainfall_risk
        roads[f"tidal_risk_{day}"] = tidal_risk
        roads[f"dynamic_risk_{day}"] = combined_risk
        roads[f"flood_cause_{day}"] = flood_cause
        roads[f"flood_prone_{day}"] = combined_risk >= 0.5

    roads_out = roads[
        [
            "road_name", "highway", "area_name", "elevation_m", "dist_to_drainage_m",
            "coastal_dist_m", "susceptibility", "is_bridge",
            "forecast_rain_mm_today", "forecast_rain_mm_tomorrow",
            "rolling_2day_rain_mm_today", "rolling_2day_rain_mm_tomorrow",
            "today_tide_m", "tomorrow_tide_m",
            "rainfall_risk_today", "rainfall_risk_tomorrow",
            "tidal_risk_today", "tidal_risk_tomorrow",
            "dynamic_risk_today", "dynamic_risk_tomorrow",
            "flood_cause_today", "flood_cause_tomorrow",
            "flood_prone_today", "flood_prone_tomorrow",
            "geometry",
        ]
    ]
    roads_out.to_file(OUT_PATH, driver="GeoJSON")
    print(f"\nRoad risk data saved to {OUT_PATH}")

    for day in ["today", "tomorrow"]:
        n_flagged = roads_out[f"flood_prone_{day}"].sum()
        print(f"\n--- ROADS FLAGGED AS FLOODING ({day.upper()}): {n_flagged} / {len(roads_out)} ---")
        top = roads_out[roads_out[f"flood_prone_{day}"]].sort_values(f"dynamic_risk_{day}", ascending=False).head(10)
        for _, row in top.iterrows():
            print(
                f"  {row['road_name']:<30} ({row['area_name']:<20}) "
                f"risk={row[f'dynamic_risk_{day}']:.2f} [{row[f'flood_cause_{day}']}]  elev={row['elevation_m']:.2f}m  "
                f"coastal_dist={row['coastal_dist_m']:.0f}m"
            )
    print("---------------------------------------------")


if __name__ == "__main__":
    main()
