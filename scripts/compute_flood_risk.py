"""
compute_flood_risk.py
======================
The actual flood-risk model, v1. Combines two real signals for every
building in Eti-Osa into one risk score:

  1. Elevation -- lower ground floods first (basic physics).
  2. Distance to the nearest real drainage line (canal/drain/stream) --
     even a low spot drains fine if a working drain is right there;
     a low spot far from any drain has nowhere for water to go.

This is intentionally a simple, transparent v1 -- a starting model you
can defend and explain in one sentence, not a black box. The natural
upgrade later is replacing the distance-to-drain proxy with an actual
flow-accumulation/hydrology simulation (where water would really run
downhill and pool), using this same data.

Requires (from earlier scripts):
  data/etiosa_dem.tif              (build_basemap.py)
  data/etiosa_buildings.geojson    (build_basemap.py, now caches to disk)
  data/etiosa_boundary.geojson     (build_basemap.py)
  data/etiosa_drainage_lines.geojson (get_drainage.py)

Run with: python scripts/compute_flood_risk.py
Output: outputs/etiosa_flood_risk_map.png
Also saves: data/etiosa_flood_risk.geojson (every building + its score)
"""

from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.plot import show

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"

DEM_PATH = DATA_DIR / "etiosa_dem.tif"
BUILDINGS_PATH = DATA_DIR / "etiosa_buildings.geojson"
BOUNDARY_PATH = DATA_DIR / "etiosa_boundary.geojson"
DRAINAGE_LINES_PATH = DATA_DIR / "etiosa_drainage_lines.geojson"

# UTM zone 31N -- flat, meters-based coordinate system for this part of
# Lagos. Distances/areas are only meaningful once you're off lat/lon
# degrees and onto something measured in real ground units.
METRIC_CRS = "EPSG:32631"

# v1 weights: elevation matters more than drainage distance in this first
# pass, since elevation is the more reliable of the two open-data signals.
# Easy to retune once the model is validated against real flood reports.
ELEVATION_WEIGHT = 0.6
DRAINAGE_WEIGHT = 0.4

# Percentile clipping for BOTH signals, not just drainage distance --
# raw min()/max() on elevation is dangerous here because SRTM elevation
# data has known noise artifacts (false readings over water, tall
# rooftops misread as ground). One or two bad pixels reading -10m or 35m
# would otherwise stretch the entire 0-1 scale and drag every normal
# building toward "high risk" by comparison. Using the 5th/95th
# percentile instead means a handful of outliers get clipped to 0 or 1
# rather than distorting everyone else's score.
ELEVATION_LOW_PERCENTILE = 0.05
ELEVATION_HIGH_PERCENTILE = 0.95
DRAINAGE_DISTANCE_CAP_PERCENTILE = 0.95


def require_file(path, produced_by):
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run {produced_by} first.")


def load_inputs():
    require_file(DEM_PATH, "build_basemap.py")
    require_file(BUILDINGS_PATH, "build_basemap.py (with the buildings-caching update)")
    require_file(BOUNDARY_PATH, "build_basemap.py")
    require_file(DRAINAGE_LINES_PATH, "get_drainage.py")

    print("Loading cached buildings, boundary, and drainage lines...")
    buildings = gpd.read_file(BUILDINGS_PATH)
    boundary = gpd.read_file(BOUNDARY_PATH)
    drainage_lines = gpd.read_file(DRAINAGE_LINES_PATH)
    print(f"  {len(buildings)} buildings, {len(drainage_lines)} drainage line features.")
    return buildings, boundary, drainage_lines


def sample_elevation(buildings):
    """
    rasterio's .sample() takes a list of (x, y) points and returns the
    raster's pixel value at each one -- here, one elevation reading per
    building, taken at that building's centroid (geopandas' .centroid
    gives the geometric middle point of each footprint polygon).
    """
    print("Sampling elevation at each building's centroid...")
    with rasterio.open(DEM_PATH) as dem_src:
        # Compute centroids in a metric (meters-based) CRS first --
        # finding the geometric middle point directly in lat/lon degrees
        # is technically inaccurate, since degrees aren't equal-sized
        # units. Reproject to meters, take the centroid, then reproject
        # those points into the DEM's own CRS so they line up with its
        # coordinate grid for sampling.
        centroids_metric = buildings.to_crs(METRIC_CRS).geometry.centroid
        centroids_in_dem_crs = gpd.GeoSeries(centroids_metric, crs=METRIC_CRS).to_crs(dem_src.crs)
        coords = [(pt.x, pt.y) for pt in centroids_in_dem_crs]
        elevations = np.array([val[0] for val in dem_src.sample(coords)], dtype="float64")

        nodata = dem_src.nodata
        if nodata is not None:
            elevations[elevations == nodata] = np.nan

    n_missing = np.isnan(elevations).sum()
    if n_missing:
        # A few buildings right on the boundary edge can miss the raster
        # grid; fill those with the dataset's own mean rather than drop
        # buildings from the map entirely.
        print(f"  {n_missing} buildings had no elevation reading (edge effects); filling with mean.")
        elevations = np.where(np.isnan(elevations), np.nanmean(elevations), elevations)

    buildings = buildings.copy()
    buildings["elevation_m"] = elevations
    return buildings


def compute_drainage_distance(buildings, drainage_lines):
    """
    geopandas.sjoin_nearest() finds, for every building, the nearest
    feature in another GeoDataFrame and how far away it is -- here, the
    nearest drainage line. Both layers get reprojected to a meters-based
    CRS first so "distance" means real meters, not degrees of latitude.
    """
    print("Computing distance from each building to its nearest drainage line...")
    buildings_m = buildings.to_crs(METRIC_CRS).reset_index(drop=True)
    buildings_m["_bid"] = buildings_m.index
    drainage_m = drainage_lines.to_crs(METRIC_CRS)[["geometry"]]

    joined = gpd.sjoin_nearest(buildings_m, drainage_m, distance_col="dist_to_drain_m")
    # sjoin_nearest can return more than one row per building if two
    # drains are exactly tied for nearest; keep just the first match.
    joined = joined.drop_duplicates(subset="_bid").sort_values("_bid")

    buildings = buildings.copy()
    buildings["dist_to_drain_m"] = joined["dist_to_drain_m"].to_numpy()
    return buildings


def compute_risk_score(buildings):
    """
    Turns the two raw signals (elevation in meters, distance in meters)
    into one 0-1 risk score:
      - elevation_score: uses the 5th-95th percentile of elevation as the
        "full scale" (not the true min/max, which can be an SRTM noise
        artifact) -- 1.0 at or below the 5th percentile (lowest, riskiest
        ground), 0.0 at or above the 95th percentile (highest ground),
        scaled linearly between, clipped to [0,1] outside that range.
      - drainage_score: 0.0 right next to a drain, 1.0 at or beyond the
        95th-percentile distance (same outlier-capping idea, applied to
        distance instead of elevation).
      - risk_score: a weighted blend of the two (see ELEVATION_WEIGHT /
        DRAINAGE_WEIGHT at the top of this file).
    """
    print("Computing composite risk scores...")
    elev = buildings["elevation_m"].to_numpy()
    elev_low = np.quantile(elev, ELEVATION_LOW_PERCENTILE)
    elev_high = np.quantile(elev, ELEVATION_HIGH_PERCENTILE)
    elevation_score = 1 - (elev - elev_low) / (elev_high - elev_low)
    elevation_score = np.clip(elevation_score, 0, 1)

    dist = buildings["dist_to_drain_m"].to_numpy()
    dist_cap = np.quantile(dist, DRAINAGE_DISTANCE_CAP_PERCENTILE)
    drainage_score = np.clip(dist / dist_cap, 0, 1)

    risk_score = ELEVATION_WEIGHT * elevation_score + DRAINAGE_WEIGHT * drainage_score

    buildings = buildings.copy()
    buildings["elevation_score"] = elevation_score
    buildings["drainage_score"] = drainage_score
    buildings["risk_score"] = risk_score
    buildings["risk_tier"] = pd_cut_risk(risk_score)
    return buildings


def pd_cut_risk(risk_score):
    """Buckets the continuous 0-1 score into readable labels."""
    import pandas as pd

    return pd.cut(
        risk_score,
        bins=[-0.01, 0.25, 0.5, 0.75, 1.01],
        labels=["Low", "Medium", "High", "Very High"],
    )


def plot_risk_map(buildings, boundary, drainage_lines):
    fig, ax = plt.subplots(figsize=(12, 12))

    if DEM_PATH.exists():
        with rasterio.open(DEM_PATH) as dem_src:
            dem_array = dem_src.read(1)
            dem_masked = np.ma.masked_equal(dem_array, dem_src.nodata)
            show(dem_masked, transform=dem_src.transform, ax=ax, cmap="Greys", alpha=0.35)

    plot_crs = boundary.crs
    dem_xlim, dem_ylim = ax.get_xlim(), ax.get_ylim()

    # A single building (~15-20m across) is a fraction of a pixel at
    # full-district zoom (Eti-Osa spans ~30km), so plotted at true size
    # the risk colors would be invisible. .buffer() here grows each
    # building's shape outward by a fixed radius in meters -- purely for
    # visibility in this wide-view image, not saved to any file -- the
    # same standard cartographic trick behind why road lines on any map
    # app are drawn much thicker than the real road width.
    buildings_viz = buildings.to_crs(METRIC_CRS).copy()
    buildings_viz["geometry"] = buildings_viz.geometry.buffer(35)
    buildings_viz = buildings_viz.to_crs(plot_crs)

    # column='risk_score' colors each building by its own value in that
    # column; cmap picks the color ramp (green=low risk, red=high risk);
    # legend=True adds the colorbar so the map is self-explanatory.
    buildings_viz.plot(
        column="risk_score",
        cmap="RdYlGn_r",
        ax=ax,
        legend=True,
        legend_kwds={"label": "Flood risk score (0 = low, 1 = high)", "shrink": 0.6},
    )
    drainage_lines.to_crs(plot_crs).plot(ax=ax, color="blue", linewidth=1, alpha=0.6)
    boundary.boundary.plot(ax=ax, color="black", linewidth=1)

    if dem_xlim != (0.0, 1.0):  # only lock extent if the DEM actually plotted
        ax.set_xlim(dem_xlim)
        ax.set_ylim(dem_ylim)

    ax.set_title("Eti-Osa LGA: Building-Level Flood Risk (v1 model)")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")

    out_path = OUTPUTS_DIR / "etiosa_flood_risk_map.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    print(f"\nFlood risk map saved to {out_path}")


def print_sanity_checks(buildings):
    print("\n--- FLOOD RISK MODEL SANITY CHECK ---")
    print(f"Buildings scored: {len(buildings)}")
    print(
        f"Elevation range used: {buildings['elevation_m'].min():.2f}m to "
        f"{buildings['elevation_m'].max():.2f}m"
    )
    print(
        f"Distance to nearest drain: min={buildings['dist_to_drain_m'].min():.0f}m, "
        f"max={buildings['dist_to_drain_m'].max():.0f}m, "
        f"mean={buildings['dist_to_drain_m'].mean():.0f}m"
    )
    print("\nRisk tier breakdown:")
    print(buildings["risk_tier"].value_counts().sort_index())
    print("\nTop 10 highest-risk buildings (centroid coordinates):")
    top10 = buildings.nlargest(10, "risk_score")
    for _, row in top10.iterrows():
        c = row.geometry.centroid
        print(f"  risk={row['risk_score']:.2f}  lat={c.y:.5f}  lon={c.x:.5f}")
    print("--------------------------------------")


def main():
    buildings, boundary, drainage_lines = load_inputs()
    buildings = sample_elevation(buildings)
    buildings = compute_drainage_distance(buildings, drainage_lines)
    buildings = compute_risk_score(buildings)

    out_geojson = DATA_DIR / "etiosa_flood_risk.geojson"
    save_cols = [
        c
        for c in buildings.columns
        if c in ("geometry", "elevation_m", "dist_to_drain_m", "elevation_score", "drainage_score", "risk_score", "risk_tier")
    ]
    buildings_to_save = buildings[save_cols].copy()
    buildings_to_save["risk_tier"] = buildings_to_save["risk_tier"].astype(str)
    buildings_to_save.to_file(out_geojson, driver="GeoJSON")
    print(f"Scored buildings saved to {out_geojson}")

    plot_risk_map(buildings, boundary, drainage_lines)
    print_sanity_checks(buildings)


if __name__ == "__main__":
    main()
