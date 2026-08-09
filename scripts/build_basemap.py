"""
build_basemap.py
=================
Flood-risk prioritization tool for Eti-Osa LGA, Lagos, Nigeria (Lekki,
Ikoyi, Victoria Island) -- data foundation step.

What this script does, end to end:
  1. Look up the official Eti-Osa LGA boundary from OpenStreetMap.
  2. Download an SRTM elevation tile (or tiles) covering that boundary.
  3. Load the elevation raster with rasterio and plot it as a heatmap.
  4. Pull building footprints and the road network from OpenStreetMap
     with osmnx, load them with geopandas.
  5. Overlay buildings + roads on top of the elevation heatmap and save
     ONE combined image.
  6. Print sanity-check stats (bounding box, building count, elevation
     min/max/mean) so you can eyeball whether the data is legit.
  7. Cache buildings + roads to data/*.geojson so later scripts (the
     flood-risk model) don't have to re-query OpenStreetMap every time.

Run with:  python scripts/build_basemap.py
Output image: outputs/etiosa_terrain_buildings_roads.png
"""

import gzip
import shutil
from pathlib import Path
import fabdem

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import osmnx as ox
import rasterio
import requests
from rasterio.mask import mask
from rasterio.merge import merge
from rasterio.plot import show

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
DATA_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

PLACE_NAME = "Eti-Osa, Lagos, Nigeria"

DEM_MERGED_PATH = DATA_DIR / "etiosa_dem.tif"
CACHED_BOUNDARY_PATH = DATA_DIR / "etiosa_boundary.geojson"
BUILDINGS_CACHE_PATH = DATA_DIR / "etiosa_buildings.geojson"
ROADS_CACHE_PATH = DATA_DIR / "etiosa_roads.geojson"


# ---------------------------------------------------------------------------
# Step 1: Get the Eti-Osa LGA boundary from OpenStreetMap
# ---------------------------------------------------------------------------
def get_boundary():
    """
    osmnx.geocode_to_gdf() asks OpenStreetMap's Nominatim geocoder for a
    place by name and returns its administrative boundary as a GeoDataFrame
    (a table where one column, 'geometry', holds the actual polygon shape).
    We use this instead of typing bounding-box numbers by hand so the area
    matches the *real* Eti-Osa LGA boundary, not a rough guess.

    Falls back to a locally cached copy (data/etiosa_boundary.geojson,
    pulled from a Nigeria administrative-boundaries dataset) if Nominatim
    is unreachable -- e.g. behind a restrictive network/firewall.
    """
    print(f"Looking up boundary for '{PLACE_NAME}' via OSM Nominatim...")
    try:
        return ox.geocode_to_gdf(PLACE_NAME)
    except Exception as e:
        print(f"  Nominatim lookup failed ({e}); falling back to cached boundary file.")
        if not CACHED_BOUNDARY_PATH.exists():
            raise RuntimeError(
                "No internet access to Nominatim and no cached boundary file found at "
                f"{CACHED_BOUNDARY_PATH}. Run this on a machine with normal internet access."
            ) from e
        return gpd.read_file(CACHED_BOUNDARY_PATH)


# ---------------------------------------------------------------------------
# Step 2: Download SRTM elevation tile(s) covering the boundary
# ---------------------------------------------------------------------------
def srtm_tile_name(lat, lon):
    """
    SRTM elevation data is distributed as 1-degree-by-1-degree tiles, named
    by the lower-left (south-west) corner, e.g. 'N06E003' covers latitudes
    6-7 N and longitudes 3-4 E. This function converts a lat/lon into the
    right tile name so we know which file to download.
    """
    lat_floor = int(np.floor(lat))
    lon_floor = int(np.floor(lon))
    ns = "N" if lat_floor >= 0 else "S"
    ew = "E" if lon_floor >= 0 else "W"
    return f"{ns}{abs(lat_floor):02d}{ew}{abs(lon_floor):03d}"


def download_srtm_tile(tile_name):
    """
    Downloads one SRTM tile in 'Skadi' (.hgt) format from the public,
    no-API-key AWS Open Data 'elevation-tiles-prod' bucket. This is the
    same raw SRTM elevation data OpenTopography serves, just mirrored
    somewhere we can fetch without registering for an API key.

    Each .hgt file is a raw grid of elevation values rasterio can read
    directly (GDAL has a built-in SRTMHGT driver).
    """
    lat_prefix = tile_name[:3]  # e.g. 'N06'
    gz_path = DATA_DIR / f"{tile_name}.hgt.gz"
    hgt_path = DATA_DIR / f"{tile_name}.hgt"

    if hgt_path.exists():
        print(f"  {tile_name}.hgt already downloaded, skipping.")
        return hgt_path

    url = f"https://s3.amazonaws.com/elevation-tiles-prod/skadi/{lat_prefix}/{tile_name}.hgt.gz"
    print(f"  Downloading {url} ...")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    gz_path.write_bytes(resp.content)

    # .hgt.gz -> .hgt (SRTM tiles are gzip-compressed on the server)
    with gzip.open(gz_path, "rb") as f_in, open(hgt_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    gz_path.unlink()  # cleanup the .gz, keep only the raw .hgt

    return hgt_path


def build_dem(boundary_gdf):
    """
    Downloads FABDEM (Forest And Buildings removed Copernicus GLO-30 DEM)
    for the Eti-Osa bounding box, then clips it to just the Eti-Osa
    boundary polygon.

    FABDEM starts from the same Copernicus GLO-30 surface model, but has
    building and tree height statistically removed via a trained
    correction model (Hawker et al. 2022). This is the actual fix for
    the "elevation reflects building/tree height, not bare ground" bias
    that forced the hand-coded absolute elevation cutoff in the tidal
    exposure logic earlier in this project -- bare-earth elevation
    should make road/tidal risk scoring more accurate without that
    workaround.

    Source: Neal & Hawker (2023), University of Bristol
    (https://doi.org/10.5523/bris.s5hqmjcdj8yo2ibzi9b4ew3sn), via the
    `fabdem` package, which downloads only the tiles covering the
    requested bounding box rather than the full ~300GB global archive.

    Data license: CC BY-NC-SA 4.0 -- non-commercial use only. If this
    project is ever monetized, this elevation layer would need a
    commercial license (contact fabdem@fathom.global).

    Requires: pip install fabdem
    """
    if DEM_MERGED_PATH.exists():
        print(f"DEM already exists at {DEM_MERGED_PATH}, skipping download.")
        return DEM_MERGED_PATH

    minx, miny, maxx, maxy = boundary_gdf.total_bounds
    tmp_path = DATA_DIR / "_fabdem_mosaic_tmp.tif"

    print(f"Downloading FABDEM for bounds ({minx:.4f}, {miny:.4f}, {maxx:.4f}, {maxy:.4f}) ...")
    fabdem.download(
        (minx, miny, maxx, maxy),
        output_path=str(tmp_path),
        cache=DATA_DIR / "fabdem_cache",
        show_progress=True,
    )

    # Clip to the actual Eti-Osa boundary polygon, same as the old SRTM
    # path did -- fabdem.download() only gives us a rectangular bbox.
    print("  Clipping DEM to Eti-Osa boundary polygon...")
    with rasterio.open(tmp_path) as src:
        mosaic_crs = src.crs
        boundary_geom = boundary_gdf.to_crs(mosaic_crs).geometry
        clipped_array, clipped_transform = mask(src, boundary_geom, crop=True)
        clipped_profile = src.profile.copy()

    clipped_profile.update({
        "height": clipped_array.shape[1],
        "width": clipped_array.shape[2],
        "transform": clipped_transform,
    })
    with rasterio.open(DEM_MERGED_PATH, "w", **clipped_profile) as dst:
        dst.write(clipped_array)

    tmp_path.unlink()
    print(f"  DEM saved to {DEM_MERGED_PATH}")
    return DEM_MERGED_PATH

# ---------------------------------------------------------------------------
# Step 3: Pull buildings and roads from OpenStreetMap with osmnx
# ---------------------------------------------------------------------------
def _make_geojson_safe(gdf):
    """
    OSM tag columns (e.g. 'name', 'maxspeed', 'osmid') sometimes contain
    lists instead of plain values, when a feature has multiple OSM tags
    merged together. GeoJSON can't serialize a list inside a table cell,
    so this converts every non-geometry column to plain text before saving
    -- keeps the geometry precise, just flattens the descriptive columns.
    """
    safe = gdf.copy()
    for col in safe.columns:
        if col != "geometry":
            safe[col] = safe[col].astype(str)
    return safe


def get_buildings(boundary_gdf):
    """
    osmnx.features_from_polygon() queries OpenStreetMap (via the Overpass
    API) for every map feature matching a tag -- here, tags={'building':
    True} means "give me anything tagged as a building" -- that falls
    inside the given polygon. It comes back as a geopandas GeoDataFrame,
    one row per building, with a 'geometry' column holding each building's
    footprint polygon.
    """
    if BUILDINGS_CACHE_PATH.exists():
        print(f"Buildings already cached at {BUILDINGS_CACHE_PATH}, loading from disk.")
        return gpd.read_file(BUILDINGS_CACHE_PATH)

    print("Downloading building footprints from OSM (this can take a bit)...")
    polygon = boundary_gdf.geometry.iloc[0]
    buildings = ox.features_from_polygon(polygon, tags={"building": True})
    # Keep only polygon-ish geometries (drop stray points/lines OSM sometimes
    # tags as 'building' by mistake, e.g. a building entrance node).
    buildings = buildings[buildings.geometry.type.isin(["Polygon", "MultiPolygon"])]
    print(f"  Retrieved {len(buildings)} building footprints.")

    _make_geojson_safe(buildings).to_file(BUILDINGS_CACHE_PATH, driver="GeoJSON")
    print(f"  Cached to {BUILDINGS_CACHE_PATH}")
    return buildings


def get_roads(boundary_gdf):
    """
    osmnx.graph_from_polygon() downloads the road network inside a polygon
    as a NetworkX graph (nodes = intersections, edges = road segments) --
    this graph structure is what makes osmnx useful later for routing /
    connectivity analysis, not just drawing lines.

    ox.graph_to_gdfs(..., nodes=False) converts just the edges (road
    segments) into a geopandas GeoDataFrame we can plot like any other
    vector layer.
    """
    if ROADS_CACHE_PATH.exists():
        print(f"Roads already cached at {ROADS_CACHE_PATH}, loading from disk.")
        return gpd.read_file(ROADS_CACHE_PATH)

    print("Downloading road network from OSM...")
    polygon = boundary_gdf.geometry.iloc[0]
    graph = ox.graph_from_polygon(polygon, network_type="drive")
    roads_gdf = ox.graph_to_gdfs(graph, nodes=False)
    print(f"  Retrieved {len(roads_gdf)} road segments.")

    _make_geojson_safe(roads_gdf).to_file(ROADS_CACHE_PATH, driver="GeoJSON")
    print(f"  Cached to {ROADS_CACHE_PATH}")
    return roads_gdf


# ---------------------------------------------------------------------------
# Step 4: Plot everything together
# ---------------------------------------------------------------------------
def plot_combined(dem_path, buildings, roads, boundary_gdf):
    with rasterio.open(dem_path) as dem_src:
        dem_array = dem_src.read(1)  # band 1 = the single elevation band
        dem_nodata = dem_src.nodata
        dem_crs = dem_src.crs

        # Mask out nodata pixels (areas outside the clipped LGA boundary)
        # so they don't skew the color scale or the min/max/mean stats.
        dem_masked = np.ma.masked_equal(dem_array, dem_nodata)

        fig, ax = plt.subplots(figsize=(12, 12))

        # rasterio.plot.show() draws the raster with correct geographic
        # extent (so pixel coordinates line up with the buildings/roads
        # we'll draw on top in the same lat/lon space).
        show(
            dem_masked,
            transform=dem_src.transform,
            ax=ax,
            cmap="terrain",
            title="Eti-Osa LGA: Elevation, Buildings & Roads",
        )
        dem_xlim, dem_ylim = ax.get_xlim(), ax.get_ylim()

        # geopandas GeoDataFrames need to be in the same CRS as the raster
        # before overlaying, otherwise shapes land in the wrong place.
        buildings_plot = buildings.to_crs(dem_crs)
        roads_plot = roads.to_crs(dem_crs)

        # GeoDataFrame.plot() draws every geometry in the table onto the
        # given matplotlib axes -- buildings as filled polygons, roads as
        # thin lines.
        buildings_plot.plot(ax=ax, color="black", alpha=0.6, linewidth=0)
        roads_plot.plot(ax=ax, color="red", linewidth=0.5, alpha=0.7)

        boundary_gdf.to_crs(dem_crs).boundary.plot(ax=ax, color="blue", linewidth=1.5)

        ax.set_xlim(dem_xlim)
        ax.set_ylim(dem_ylim)
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")

        out_path = OUTPUTS_DIR / "etiosa_terrain_buildings_roads.png"
        fig.savefig(out_path, dpi=200, bbox_inches="tight")
        print(f"\nCombined map saved to {out_path}")

    return dem_masked


# ---------------------------------------------------------------------------
# Step 5: Sanity-check stats
# ---------------------------------------------------------------------------
def print_sanity_checks(boundary_gdf, buildings, dem_masked):
    minx, miny, maxx, maxy = boundary_gdf.total_bounds
    print("\n--- SANITY CHECK ---")
    print(f"Bounding box: minx={minx:.4f}, miny={miny:.4f}, maxx={maxx:.4f}, maxy={maxy:.4f}")
    print(f"Number of buildings pulled: {len(buildings)}")
    print(
        f"Elevation (m): min={dem_masked.min():.2f}, "
        f"max={dem_masked.max():.2f}, mean={dem_masked.mean():.2f}"
    )
    print("--------------------")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    boundary_gdf = get_boundary()
    dem_path = build_dem(boundary_gdf)
    buildings = get_buildings(boundary_gdf)
    roads = get_roads(boundary_gdf)
    dem_masked = plot_combined(dem_path, buildings, roads, boundary_gdf)
    print_sanity_checks(boundary_gdf, buildings, dem_masked)


if __name__ == "__main__":
    main()
