"""
get_drainage.py
================
Pulls existing drainage infrastructure for Eti-Osa LGA, Lagos, from
OpenStreetMap: canals, drains, ditches, streams (mapped as LINES you can
trace water flowing along) and open water bodies -- ponds, the lagoon
edge, retention basins (mapped as POLYGONS you can trace an area of).

This matters for flood risk because low elevation alone doesn't tell you
whether an area floods -- a low-lying spot right next to a working drain
can clear water fast, while a low-lying spot far from any drain will
pool. This script gets us the "where can water actually go" layer.

Requires data/etiosa_dem.tif to already exist (run build_basemap.py
first) so this can overlay drainage on the same elevation map.

Run with: python scripts/get_drainage.py
Output: outputs/etiosa_drainage_overlay.png
Also saves: data/etiosa_drainage_lines.geojson, data/etiosa_drainage_polygons.geojson
"""

from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import osmnx as ox
import rasterio
from rasterio.plot import show

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
DATA_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

DEM_PATH = DATA_DIR / "etiosa_dem.tif"
CACHED_BOUNDARY_PATH = DATA_DIR / "etiosa_boundary.geojson"
PLACE_NAME = "Eti-Osa, Lagos, Nigeria"

# UTM zone 31N -- the local flat (meters-based) coordinate system for this
# part of Lagos. We reproject into this just for measuring real lengths
# and areas; lat/lon degrees aren't a fixed distance, so you can't get a
# true "km" out of them directly.
METRIC_CRS = "EPSG:32631"


# ---------------------------------------------------------------------------
# Step 1: Get the Eti-Osa boundary (same approach as build_basemap.py)
# ---------------------------------------------------------------------------
def get_boundary():
    print(f"Looking up boundary for '{PLACE_NAME}' via OSM Nominatim...")
    try:
        return ox.geocode_to_gdf(PLACE_NAME)
    except Exception as e:
        print(f"  Nominatim lookup failed ({e}); falling back to cached boundary file.")
        if not CACHED_BOUNDARY_PATH.exists():
            raise RuntimeError(
                f"No cached boundary file found at {CACHED_BOUNDARY_PATH} either. "
                "Run build_basemap.py first, or connect to the internet."
            ) from e
        return gpd.read_file(CACHED_BOUNDARY_PATH)


# ---------------------------------------------------------------------------
# Step 2: Pull drainage-related features from OpenStreetMap
# ---------------------------------------------------------------------------
def get_drainage_features(boundary_gdf):
    """
    osmnx.features_from_polygon() again (same call build_basemap.py used
    for buildings), but with a different 'tags' dict so it matches
    drainage infrastructure instead:
      - waterway=* catches canals, drains, ditches, streams, rivers --
        anything OSM mappers tagged as a channel water moves through.
      - natural=water catches lakes, ponds, and open water bodies.
      - landuse=basin catches engineered retention/detention basins.

    Passing all three as one dict means "match any of these" -- one
    Overpass query instead of three separate downloads.

    IMPORTANT: this query returns the FULL geometry of anything that
    merely *touches* the search polygon, not just the part inside it.
    Lagos Lagoon and the Atlantic Ocean are both tagged natural=water as
    single enormous polygons, so without clipping (done in step 3 below)
    you'd end up "retrieving" the entire ocean outline just because a
    sliver of it touches Eti-Osa's coastline.
    """
    polygon = boundary_gdf.geometry.iloc[0]
    print("Downloading drainage infrastructure (canals, drains, water bodies) from OSM...")
    tags = {"waterway": True, "natural": "water", "landuse": "basin"}
    try:
        drainage = ox.features_from_polygon(polygon, tags=tags)
    except ox._errors.InsufficientResponseError:
        print("  No drainage features found for this area/tag set.")
        return gpd.GeoDataFrame(geometry=[], crs=boundary_gdf.crs)
    print(f"  Retrieved {len(drainage)} drainage-related features (before clipping).")
    return drainage


def split_by_geometry_type(drainage_gdf):
    """
    The query above returns a mix of geometry types in one table --
    LineStrings for canals/drains/streams, Polygons for ponds/basins.
    Split them so each group can be drawn with the right style (thin blue
    lines vs filled blue shapes) and measured the right way (length vs
    area).
    """
    if len(drainage_gdf) == 0:
        empty = gpd.GeoDataFrame(geometry=[], crs=drainage_gdf.crs)
        return empty, empty
    lines = drainage_gdf[drainage_gdf.geometry.type.isin(["LineString", "MultiLineString"])]
    polygons = drainage_gdf[drainage_gdf.geometry.type.isin(["Polygon", "MultiPolygon"])]
    return lines, polygons


def clip_to_boundary(gdf, boundary_gdf):
    """
    geopandas.clip() cuts every geometry down to only the part that falls
    inside the boundary polygon -- so the Lagos Lagoon / Atlantic Ocean,
    which OSM returned as huge shapes extending far outside Eti-Osa, get
    trimmed down to just the sliver of coastline actually inside the
    district, instead of counting (and drawing) the whole ocean.
    """
    if len(gdf) == 0:
        return gdf
    clipped = gpd.clip(gdf, boundary_gdf[["geometry"]])
    return clipped


# ---------------------------------------------------------------------------
# Step 3: Plot drainage over the elevation map
# ---------------------------------------------------------------------------
def plot_drainage(boundary_gdf, lines, polygons):
    if not DEM_PATH.exists():
        raise FileNotFoundError(
            f"{DEM_PATH} not found. Run build_basemap.py first to download the elevation data."
        )

    with rasterio.open(DEM_PATH) as dem_src:
        dem_array = dem_src.read(1)
        dem_masked = np.ma.masked_equal(dem_array, dem_src.nodata)

        fig, ax = plt.subplots(figsize=(12, 12))
        show(
            dem_masked,
            transform=dem_src.transform,
            ax=ax,
            cmap="terrain",
            title="Eti-Osa LGA: Drainage Infrastructure over Elevation",
        )

        # Lock the view to the DEM's own extent *before* plotting anything
        # else. Even after clipping, this stops any stray geometry from
        # silently expanding the view again (matplotlib auto-zooms to fit
        # everything plotted, by default) -- we want the map to always
        # frame Eti-Osa, not whatever the widest layer happens to be.
        dem_xlim, dem_ylim = ax.get_xlim(), ax.get_ylim()

        dem_crs = dem_src.crs
        if len(polygons) > 0:
            polygons.to_crs(dem_crs).plot(ax=ax, color="deepskyblue", alpha=0.7, edgecolor="blue")
        if len(lines) > 0:
            lines.to_crs(dem_crs).plot(ax=ax, color="blue", linewidth=1.3)
        boundary_gdf.to_crs(dem_crs).boundary.plot(ax=ax, color="black", linewidth=1)

        ax.set_xlim(dem_xlim)
        ax.set_ylim(dem_ylim)
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")

        out_path = OUTPUTS_DIR / "etiosa_drainage_overlay.png"
        fig.savefig(out_path, dpi=200, bbox_inches="tight")
        print(f"\nDrainage overlay map saved to {out_path}")


# ---------------------------------------------------------------------------
# Step 4: Sanity-check stats
# ---------------------------------------------------------------------------
def print_sanity_checks(boundary_gdf, lines, polygons):
    minx, miny, maxx, maxy = boundary_gdf.total_bounds

    # .to_crs(METRIC_CRS) reprojects into meters so .length and .area give
    # real-world units instead of degrees (which aren't a fixed distance).
    total_length_km = lines.to_crs(METRIC_CRS).length.sum() / 1000 if len(lines) else 0.0
    total_water_area_km2 = (
        polygons.to_crs(METRIC_CRS).area.sum() / 1_000_000 if len(polygons) else 0.0
    )
    lga_area_km2 = boundary_gdf.to_crs(METRIC_CRS).area.sum() / 1_000_000

    print("\n--- DRAINAGE SANITY CHECK (clipped to Eti-Osa boundary) ---")
    print(f"Bounding box: minx={minx:.4f}, miny={miny:.4f}, maxx={maxx:.4f}, maxy={maxy:.4f}")
    print(f"Eti-Osa LGA area (for reference): {lga_area_km2:.2f} km²")
    print(f"Drainage line features (canals/drains/streams): {len(lines)}")
    print(f"Total drainage line length: {total_length_km:.1f} km")
    print(f"Water body / basin polygons: {len(polygons)}")
    print(f"Total water body area: {total_water_area_km2:.2f} km² (should be <= LGA area)")
    print("-----------------------------")


def main():
    boundary_gdf = get_boundary()
    drainage = get_drainage_features(boundary_gdf)
    lines, polygons = split_by_geometry_type(drainage)

    lines = clip_to_boundary(lines, boundary_gdf)
    polygons = clip_to_boundary(polygons, boundary_gdf)
    print(f"  After clipping to boundary: {len(lines)} line features, {len(polygons)} polygon features.")

    if len(lines) > 0:
        lines.to_file(DATA_DIR / "etiosa_drainage_lines.geojson", driver="GeoJSON")
    if len(polygons) > 0:
        polygons.to_file(DATA_DIR / "etiosa_drainage_polygons.geojson", driver="GeoJSON")

    plot_drainage(boundary_gdf, lines, polygons)
    print_sanity_checks(boundary_gdf, lines, polygons)


if __name__ == "__main__":
    main()
