"""
demo_synthetic_pipeline.py
===========================
PIPELINE-MECHANICS DEMO ONLY -- elevation, buildings, and roads below are
SYNTHETIC (randomly generated), not real Lagos data. Real building/road
data requires OpenStreetMap's Overpass API and real elevation requires
SRTM/S3 -- both were unreachable from this sandbox's network, so this demo
exists to prove the rasterio/geopandas/matplotlib plotting pipeline itself
works correctly, using the REAL Eti-Osa LGA boundary (data/etiosa_boundary.geojson,
sourced from a Nigeria administrative-boundaries dataset on GitHub) as the
geographic frame.

Run scripts/build_basemap.py on a machine with normal internet access to
get the real version of this figure with real SRTM elevation + real OSM
buildings/roads.

Run with: python scripts/demo_synthetic_pipeline.py
Output: outputs/etiosa_DEMO_synthetic_terrain_buildings_roads.png
"""

from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.mask import mask
from rasterio.plot import show
from rasterio.transform import from_bounds
from scipy.ndimage import gaussian_filter
from shapely.geometry import LineString, Polygon, box

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
BOUNDARY_PATH = DATA_DIR / "etiosa_boundary.geojson"
DEM_PATH = DATA_DIR / "etiosa_dem_SYNTHETIC.tif"

RNG = np.random.default_rng(42)  # fixed seed so the demo is reproducible


def load_boundary():
    """
    geopandas.read_file() loads any vector file format (GeoJSON, Shapefile,
    GeoPackage, ...) into a GeoDataFrame. This is the REAL Eti-Osa LGA
    boundary polygon -- everything else in this demo is fake, but the
    outline itself is genuine so the shape/extent looks right.
    """
    print(f"Loading real Eti-Osa boundary from {BOUNDARY_PATH} ...")
    boundary_gdf = gpd.read_file(BOUNDARY_PATH)
    return boundary_gdf


def build_synthetic_dem(boundary_gdf):
    """
    Builds a fake-but-plausible elevation raster: Lagos's Eti-Osa LGA is
    coastal lowland, so real elevation there is mostly 0-15m. We fake that
    by smoothing random noise (gaussian_filter turns sharp random pixels
    into gentle rolling terrain) and scaling it into a believable range --
    NOT real measured elevation.
    """
    minx, miny, maxx, maxy = boundary_gdf.total_bounds
    width, height = 300, 300

    noise = RNG.normal(size=(height, width))
    smooth = gaussian_filter(noise, sigma=8)
    smooth = (smooth - smooth.min()) / (smooth.max() - smooth.min())  # 0-1
    fake_elevation = (smooth * 15).astype("float32")  # 0-15 m, plausible for coastal Lagos

    # from_bounds() builds the affine transform that maps pixel row/col to
    # real-world lon/lat, given the raster's geographic extent and shape --
    # this is what lets rasterio (and anything reading the GeoTIFF later)
    # know *where* each pixel sits on the map.
    transform = from_bounds(minx, miny, maxx, maxy, width, height)

    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": boundary_gdf.crs,
        "transform": transform,
        "nodata": -9999,
    }
    with rasterio.open(DEM_PATH, "w", **profile) as dst:
        dst.write(fake_elevation, 1)

    # Clip the fake raster to the real boundary polygon, same as the real
    # pipeline does with rasterio.mask.mask(), so shapes outside Eti-Osa
    # don't get plotted.
    with rasterio.open(DEM_PATH) as src:
        clipped_array, clipped_transform = mask(src, boundary_gdf.geometry, crop=True, nodata=-9999)
        clipped_profile = src.profile.copy()

    clipped_profile.update(
        height=clipped_array.shape[1], width=clipped_array.shape[2], transform=clipped_transform
    )
    with rasterio.open(DEM_PATH, "w", **clipped_profile) as dst:
        dst.write(clipped_array)

    print(f"  Synthetic DEM written to {DEM_PATH}")
    return DEM_PATH


def build_synthetic_buildings(boundary_gdf, n=600):
    """
    Scatters n small square polygons ('buildings') at random points inside
    the boundary. Uses GeoDataFrame.sjoin-style containment checking via
    shapely's .contains() to make sure every fake building actually lands
    inside the Eti-Osa polygon, not in the surrounding lagoon/ocean.
    """
    polygon = boundary_gdf.geometry.iloc[0]
    minx, miny, maxx, maxy = boundary_gdf.total_bounds

    buildings = []
    attempts = 0
    while len(buildings) < n and attempts < n * 20:
        attempts += 1
        x = RNG.uniform(minx, maxx)
        y = RNG.uniform(miny, maxy)
        if polygon.contains(gpd.points_from_xy([x], [y])[0]):
            # ~15-30m fake footprint, roughly converted to degrees
            half_side = RNG.uniform(0.00007, 0.00015)
            buildings.append(box(x - half_side, y - half_side, x + half_side, y + half_side))

    buildings_gdf = gpd.GeoDataFrame(geometry=buildings, crs=boundary_gdf.crs)
    print(f"  Generated {len(buildings_gdf)} synthetic building footprints.")
    return buildings_gdf


def build_synthetic_roads(boundary_gdf, n=40):
    """
    Draws n random line segments inside the boundary's bounding box and
    clips them to the polygon, standing in for a real OSM road network
    (which would normally come from osmnx.graph_from_polygon()).
    """
    polygon = boundary_gdf.geometry.iloc[0]
    minx, miny, maxx, maxy = boundary_gdf.total_bounds

    lines = []
    for _ in range(n):
        x1, y1 = RNG.uniform(minx, maxx), RNG.uniform(miny, maxy)
        x2, y2 = RNG.uniform(minx, maxx), RNG.uniform(miny, maxy)
        line = LineString([(x1, y1), (x2, y2)])
        clipped = line.intersection(polygon)
        if not clipped.is_empty:
            lines.append(clipped)

    roads_gdf = gpd.GeoDataFrame(geometry=lines, crs=boundary_gdf.crs)
    print(f"  Generated {len(roads_gdf)} synthetic road segments.")
    return roads_gdf


def plot_combined(dem_path, buildings, roads, boundary_gdf):
    with rasterio.open(dem_path) as dem_src:
        dem_array = dem_src.read(1)
        dem_nodata = dem_src.nodata
        dem_masked = np.ma.masked_equal(dem_array, dem_nodata)

        fig, ax = plt.subplots(figsize=(12, 12))
        show(
            dem_masked,
            transform=dem_src.transform,
            ax=ax,
            cmap="terrain",
            title="Eti-Osa LGA -- SYNTHETIC DEMO (pipeline mechanics only, not real data)",
        )

        buildings.plot(ax=ax, color="black", alpha=0.6, linewidth=0)
        roads.plot(ax=ax, color="red", linewidth=0.7, alpha=0.7)
        boundary_gdf.boundary.plot(ax=ax, color="blue", linewidth=1.5)

        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")

        out_path = OUTPUTS_DIR / "etiosa_DEMO_synthetic_terrain_buildings_roads.png"
        fig.savefig(out_path, dpi=200, bbox_inches="tight")
        print(f"\nDemo combined map saved to {out_path}")

    return dem_masked


def print_sanity_checks(boundary_gdf, buildings, dem_masked):
    minx, miny, maxx, maxy = boundary_gdf.total_bounds
    print("\n--- SANITY CHECK (SYNTHETIC DEMO DATA) ---")
    print(f"Bounding box (real, from actual Eti-Osa boundary): minx={minx:.4f}, miny={miny:.4f}, maxx={maxx:.4f}, maxy={maxy:.4f}")
    print(f"Number of synthetic buildings generated: {len(buildings)}")
    print(
        f"Synthetic elevation (m): min={dem_masked.min():.2f}, "
        f"max={dem_masked.max():.2f}, mean={dem_masked.mean():.2f}"
    )
    print("(Elevation values are fabricated -- run build_basemap.py locally for real SRTM data)")
    print("--------------------------------------------")


def main():
    boundary_gdf = load_boundary()
    dem_path = build_synthetic_dem(boundary_gdf)
    buildings = build_synthetic_buildings(boundary_gdf)
    roads = build_synthetic_roads(boundary_gdf)
    dem_masked = plot_combined(dem_path, buildings, roads, boundary_gdf)
    print_sanity_checks(boundary_gdf, buildings, dem_masked)


if __name__ == "__main__":
    main()
