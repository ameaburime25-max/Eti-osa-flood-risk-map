"""
model_drainage_blockage.py
============================
Task #2 on the standing priority list: model human-caused drainage
BLOCKAGE, not just distance to the nearest drain. Distance-to-drain (the
signal compute_flood_risk.py and estimate_road_risk.py have used so far)
silently assumes every mapped drain is clear and working. In Lagos that's
often not true -- Lagos State's own Emergency Flood Abatement Gang exists
specifically to "free up manholes and blackspots" (Oct 2022 Nairametrics
quote, Commissioner Tunji Bello) because real drains get choked with
refuse, silt, and informal encroachment. A building 20m from a fully
blocked drain has the same practical drainage as a building 20m from no
drain at all.

This script scores every one of the 546 real OSM drainage-line segments
in Eti-Osa (data/etiosa_drainage_lines.geojson) with a blockage_risk
(0 = essentially unblockable, 1 = highly likely to be blocked), from
three real, physically-grounded signals in the data:

  1. Waterway type. A "drain" or "ditch" is small, shallow, artificial
     channel -- exactly what a bag of refuse or a load of construction
     sand can choke solid. A "canal"/"river"/"stream" carries real,
     continuous flow and is far harder to fully block by informal
     dumping. (waterway value counts in this dataset: drain=426,
     ditch=90, stream=17, canal=9, river=4.)
  2. Culvert / tunnel status. 218 of the 546 segments (40%) are tagged
     tunnel=culvert -- covered/underground drainage. A blocked culvert is
     invisible from street level; nobody can see it silting up the way
     you can see an open ditch, and it can't be manually cleared the way
     Lagos State's abatement gangs clear open channels. That opacity is
     itself a real vulnerability, independent of the channel's size.
  3. Building encroachment pressure. Using the real building-footprint
     data (73,101 footprints, data/etiosa_buildings.geojson), this counts
     how many buildings sit within 15m of each drainage segment. Dense
     building pressure right on top of a drain is a direct, physical
     proxy for the two real human causes of blockage this project is
     trying to capture: informal structures built over/into the drainage
     right-of-way, and a nearby population dense enough that dumping
     refuse into the nearest open channel is the path of least
     resistance. Sparse building pressure means neither of those
     mechanisms has much opportunity to act on that segment.

These three signals are genuinely independent of each other (a big canal
can still be heavily encroached; a covered culvert in a low-density area
still carries the "invisible" risk) and independent of the elevation/
distance signals already driving the model, so combining them is adding
real new information, not restating what compute_flood_risk.py already
knows.

blockage_risk is NOT saved as a new absolute risk category. It feeds
downstream (compute_flood_risk.py, estimate_road_risk.py) as a discount
on how much benefit proximity-to-a-drain provides -- see
compute_effective_drainage_score() in both those files: being near a
drain only helps if that drain still works.

Requires:
  data/etiosa_drainage_lines.geojson (get_drainage.py)
  data/etiosa_buildings.geojson (build_basemap.py)

Run with: python scripts/model_drainage_blockage.py
Saves: data/etiosa_drainage_blockage.geojson
       (every drainage line segment + its blockage_risk and components)
"""

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

DRAINAGE_LINES_PATH = DATA_DIR / "etiosa_drainage_lines.geojson"
BUILDINGS_PATH = DATA_DIR / "etiosa_buildings.geojson"
OUT_PATH = DATA_DIR / "etiosa_drainage_blockage.geojson"

METRIC_CRS = "EPSG:32631"

# How close a building has to be to a drainage segment to count as
# "encroachment pressure" on it. 15m is a deliberately generous margin --
# Lagos's official drainage right-of-way setback is much narrower in
# theory, but the real question here isn't "is this building illegally
# sited," it's "is there enough human activity immediately next to this
# channel that dumping/building pressure could plausibly choke it."
ENCROACHMENT_BUFFER_M = 15

# Base blockage vulnerability by real OSM waterway type. Small, shallow,
# artificial channels (drain/ditch) are the most choke-able; channels
# with real continuous flow (river/canal) are the least; stream sits
# between the two. "other/unspecified" gets a neutral middle value
# rather than 0, since an unlabeled channel shouldn't be assumed safe.
WATERWAY_VULNERABILITY = {
    "drain": 0.80,
    "ditch": 0.70,
    "stream": 0.35,
    "canal": 0.30,
    "river": 0.20,
}
DEFAULT_WATERWAY_VULNERABILITY = 0.50

# Fixed bonus added when a segment is tagged tunnel=culvert -- covered
# drainage that can't be visually monitored or manually cleared the way
# an open channel can.
CULVERT_BONUS = 0.25

# Weights for the three components. Waterway type carries the most
# weight since it's the most direct physical statement about how easy
# the channel is to choke; encroachment pressure is a close second since
# it's the actual human-behaviour signal the user asked this model to
# capture; the culvert bonus is smaller since it's a real but narrower
# effect (only relevant to the 40% of segments it applies to).
WATERWAY_WEIGHT = 0.40
ENCROACHMENT_WEIGHT = 0.35
CULVERT_WEIGHT = 0.25

ENCROACHMENT_LOW_PERCENTILE = 0.05
ENCROACHMENT_HIGH_PERCENTILE = 0.95


def require_file(path, produced_by):
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run {produced_by} first.")


def load_inputs():
    require_file(DRAINAGE_LINES_PATH, "get_drainage.py")
    require_file(BUILDINGS_PATH, "build_basemap.py")
    print("Loading drainage lines and building footprints...")
    drainage_lines = gpd.read_file(DRAINAGE_LINES_PATH)
    buildings = gpd.read_file(BUILDINGS_PATH)
    print(f"  {len(drainage_lines)} drainage line segments, {len(buildings)} buildings.")
    return drainage_lines, buildings


def score_waterway_type(drainage_lines):
    print("Scoring blockage vulnerability by waterway type...")
    waterway = drainage_lines["waterway"].astype(str).str.lower()
    vulnerability = waterway.map(WATERWAY_VULNERABILITY).fillna(DEFAULT_WATERWAY_VULNERABILITY)
    print(f"  {waterway.value_counts().to_dict()}")
    return vulnerability.to_numpy()


def score_culvert(drainage_lines):
    print("Flagging covered (culvert) segments...")
    tunnel = drainage_lines["tunnel"].astype(str).str.lower()
    is_culvert = tunnel == "culvert"
    print(f"  {int(is_culvert.sum())} / {len(drainage_lines)} segments are culverts.")
    return is_culvert.to_numpy().astype(float)


def score_building_encroachment(drainage_lines, buildings):
    """
    For every drainage segment, buffers it out by ENCROACHMENT_BUFFER_M
    (in real meters, via the metric CRS) and counts how many real
    building footprints fall inside that corridor -- normalized by the
    segment's own length, so a long segment isn't automatically scored
    as "more encroached" than a short one just because it has more
    chances to intersect a building.
    """
    print(f"Counting building encroachment within {ENCROACHMENT_BUFFER_M}m of each drainage segment...")
    lines_m = drainage_lines.to_crs(METRIC_CRS).reset_index(drop=True)
    lines_m["_seg_id"] = lines_m.index
    lines_m["_length_m"] = lines_m.geometry.length

    corridors = lines_m.copy()
    corridors["geometry"] = corridors.geometry.buffer(ENCROACHMENT_BUFFER_M)

    buildings_m = buildings.to_crs(METRIC_CRS).copy()
    buildings_m["geometry"] = buildings_m.geometry.centroid

    joined = gpd.sjoin(buildings_m[["geometry"]], corridors[["_seg_id", "geometry"]], predicate="within")
    counts = joined.groupby("_seg_id").size().reindex(lines_m["_seg_id"], fill_value=0)

    # Buildings per 100m of segment length -- a short segment with 3
    # buildings crowded against it is genuinely more encroached than a
    # long segment with 3 buildings spread along a kilometer of it.
    length_100m = np.maximum(lines_m["_length_m"].to_numpy() / 100, 0.1)
    density = counts.to_numpy() / length_100m

    low = np.quantile(density, ENCROACHMENT_LOW_PERCENTILE)
    high = np.quantile(density, ENCROACHMENT_HIGH_PERCENTILE)
    if high > low:
        encroachment_score = np.clip((density - low) / (high - low), 0, 1)
    else:
        encroachment_score = np.zeros_like(density)

    print(
        f"  Buildings per 100m of drain: min={density.min():.1f}, "
        f"median={np.median(density):.1f}, max={density.max():.1f}"
    )
    return encroachment_score, counts.to_numpy()


def compute_blockage_risk(drainage_lines, buildings):
    waterway_vulnerability = score_waterway_type(drainage_lines)
    culvert_bonus = score_culvert(drainage_lines)
    encroachment_score, building_count = score_building_encroachment(drainage_lines, buildings)

    blockage_risk = np.clip(
        WATERWAY_WEIGHT * waterway_vulnerability
        + ENCROACHMENT_WEIGHT * encroachment_score
        + CULVERT_WEIGHT * culvert_bonus,
        0,
        1,
    )

    scored = drainage_lines.copy()
    scored["waterway_vulnerability"] = waterway_vulnerability
    scored["is_culvert"] = culvert_bonus.astype(bool)
    scored["nearby_building_count"] = building_count
    scored["encroachment_score"] = encroachment_score
    scored["blockage_risk"] = blockage_risk
    return scored


def print_report(scored):
    print("\n--- DRAINAGE BLOCKAGE RISK: SUMMARY ---")
    print(f"Segments scored: {len(scored)}")
    print(f"Mean blockage_risk: {scored['blockage_risk'].mean():.2f}")
    print("\nMean blockage_risk by waterway type:")
    print(scored.groupby(scored["waterway"].astype(str).str.lower())["blockage_risk"].mean().sort_values(ascending=False))
    print("\nTop 15 highest blockage-risk segments:")
    top = scored.nlargest(15, "blockage_risk")
    for _, row in top.iterrows():
        name = row.get("name") or "(unnamed)"
        print(
            f"  {str(name):<30} waterway={str(row['waterway']):<8} "
            f"culvert={row['is_culvert']!s:<5} nearby_buildings={row['nearby_building_count']:>3} "
            f"-> blockage_risk={row['blockage_risk']:.2f}"
        )
    print("----------------------------------------")


def main():
    drainage_lines, buildings = load_inputs()
    scored = compute_blockage_risk(drainage_lines, buildings)

    save_cols = [
        c
        for c in [
            "name", "waterway", "tunnel", "waterway_vulnerability", "is_culvert",
            "nearby_building_count", "encroachment_score", "blockage_risk", "geometry",
        ]
        if c in scored.columns
    ]
    scored_to_save = scored[save_cols].copy()
    scored_to_save.to_file(OUT_PATH, driver="GeoJSON")
    print(f"\nDrainage blockage scores saved to {OUT_PATH}")

    print_report(scored)


if __name__ == "__main__":
    main()
