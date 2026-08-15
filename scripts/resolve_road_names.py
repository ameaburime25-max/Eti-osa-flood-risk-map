"""
resolve_road_names.py
=======================
57.8% of Eti-Osa's 21,204 real OSM road segments (12,264 of them) have no
`name` tag at all -- and 90% of those are `residential` roads specifically
(not footpaths/service tracks, which might legitimately go unnamed). 65%
of every residential road in the district is missing a name in the source
data. Checked for a way to patch this from data already on hand -- `ref`
tags (route numbers) have 0% coverage on unnamed roads, and building
`addr:street` tags exist on only 0.4% of the 73,101 buildings -- neither
is usable. This is a genuine upstream OpenStreetMap completeness gap for
this region, not something the pipeline is getting wrong.

The real fix: reverse-geocode each distinct unnamed street's location and
ask what it's actually called. osmnx splits a single physical OSM way
into multiple graph edges at intersections, so the 12,264 unnamed
segments collapse to ~4,058 distinct physical streets (grouped here by
`osmid`) -- that's the real size of the lookup.

Uses LocationIQ (https://locationiq.com) rather than the public OSM
Nominatim endpoint directly. Same underlying Nominatim engine/data, but
LocationIQ is a company that explicitly permits this kind of one-time
bulk/programmatic reverse-geocoding under its own terms, unlike OSM's
shared public instance -- see https://operations.osmfoundation.org/policies/nominatim/
("systematic queries... reverse queries in a grid... downloading all POIs
in an area" are explicitly banned there and would risk getting the whole
project's network blocked from that free public service). Requires a
free LocationIQ API key (https://locationiq.com -- sign up, copy the
access token) set as the LOCATIONIQ_API_KEY environment variable.

This is a one-time enrichment, not part of the daily pipeline -- road
names don't change day to day. Progress is checkpointed to
data/road_name_lookup_cache.json after every request, so an interrupted
run (Ctrl-C, network drop) can just be re-run and it picks up where it
left off instead of re-paying for or re-querying already-resolved
streets.

Run with: LOCATIONIQ_API_KEY=your_key_here python scripts/resolve_road_names.py
Updates: data/etiosa_roads.geojson (the `name` column, in place)
Also saves: data/road_name_lookup_cache.json (the raw osmid -> name cache)
"""

import json
import os
import time
from pathlib import Path

import geopandas as gpd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
ROADS_PATH = DATA_DIR / "etiosa_roads.geojson"
CACHE_PATH = DATA_DIR / "road_name_lookup_cache.json"

METRIC_CRS = "EPSG:32631"

# Conservative pacing -- comfortably under LocationIQ's free-tier rate
# limit (2 req/s). Being slower than the ceiling costs us a few extra
# minutes across ~4,058 requests; being too close to it risks 429s.
SECONDS_BETWEEN_REQUESTS = 0.6
CHECKPOINT_EVERY = 20


def is_empty(value):
    if value is None:
        return True
    s = str(value).strip()
    return s in ("", "None", "nan", "[]")


def load_cache():
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=1)


def group_unnamed_roads(roads):
    """
    osmnx represents one physical OSM way as multiple graph edges when it
    crosses intersections, so grouping by osmid collapses the segment-
    level rows back down to the real, distinct physical streets that
    actually need a single reverse-geocode lookup each.
    """
    roads = roads.copy()
    roads["_unnamed"] = roads["name"].apply(is_empty)
    unnamed = roads[roads["_unnamed"]].copy()
    unnamed["_osmid_key"] = unnamed["osmid"].astype(str)

    print(f"{len(unnamed)} unnamed road segments out of {len(roads)} total "
          f"({100 * len(unnamed) / len(roads):.1f}%).")

    unnamed_m = unnamed.to_crs(METRIC_CRS)
    groups = []
    for osmid_key, group in unnamed_m.groupby("_osmid_key"):
        union_geom = group.geometry.union_all() if hasattr(group.geometry, "union_all") else group.unary_union
        centroid_m = union_geom.centroid
        groups.append({"osmid_key": osmid_key, "x": centroid_m.x, "y": centroid_m.y})

    groups_gdf = gpd.GeoDataFrame(
        groups,
        geometry=gpd.points_from_xy([g["x"] for g in groups], [g["y"] for g in groups]),
        crs=METRIC_CRS,
    ).to_crs("EPSG:4326")
    groups_gdf["lon"] = groups_gdf.geometry.x
    groups_gdf["lat"] = groups_gdf.geometry.y
    print(f"  Collapses to {len(groups_gdf)} distinct physical unnamed streets.")
    return groups_gdf[["osmid_key", "lat", "lon"]]


def reverse_geocode_one(lat, lon, api_key, verbose_errors=True):
    """
    Returns (success, name). success=False means the REQUEST failed (bad
    key, quota, network, timeout, etc.) -- these must NOT be cached, so a
    transient blip or a fixed key/quota gets retried automatically on the
    next run instead of being permanently remembered as "no name found."
    success=True, name=None means the request worked fine and LocationIQ
    genuinely has no road name for that point -- that's a real result
    worth caching.

    A network hiccup (timeout, connection reset) is not the same kind of
    failure as a bad API key -- it's expected to happen occasionally over
    ~4,000 requests and shouldn't crash the whole run. This retries once
    after a short pause before giving up on a given point.
    """
    url = "https://us1.locationiq.com/v1/reverse"
    params = {"key": api_key, "lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "format": "json", "addressdetails": 1}

    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.exceptions.RequestException as e:
            if verbose_errors:
                print(f"    Network error ({e.__class__.__name__}) -- {'retrying once' if attempt == 0 else 'giving up on this point for now'}...")
            if attempt == 0:
                time.sleep(3)
                continue
            return False, None

        if resp.status_code == 429:
            print("    Rate-limited (429) -- backing off 5s and retrying once...")
            time.sleep(5)
            continue
        if resp.status_code != 200:
            if verbose_errors:
                print(f"    HTTP {resp.status_code}: {resp.text[:300]}")
            return False, None

        data = resp.json()
        if "error" in data:
            if verbose_errors:
                print(f"    API error: {data['error']}")
            return False, None
        address = data.get("address", {})
        # "road" is the standard field for the street a point sits on.
        # Fall back to "pedestrian"/"footway" only if that's genuinely the
        # only named feature there -- still better than nothing for a real
        # residential street with no other data.
        name = address.get("road") or address.get("pedestrian") or address.get("footway")
        return True, name

    return False, None


def main():
    api_key = os.environ.get("LOCATIONIQ_API_KEY")
    if not api_key:
        raise SystemExit(
            "Set LOCATIONIQ_API_KEY first, e.g.:\n"
            "  LOCATIONIQ_API_KEY=your_key_here python scripts/resolve_road_names.py\n"
            "Get a free key at https://locationiq.com (sign up, copy the access token)."
        )

    if not ROADS_PATH.exists():
        raise SystemExit(f"{ROADS_PATH} not found. Run build_basemap.py first.")

    roads = gpd.read_file(ROADS_PATH)
    groups = group_unnamed_roads(roads)

    cache = load_cache()
    already_done = sum(1 for k in groups["osmid_key"] if k in cache)
    print(f"  {already_done} already resolved in a previous run; {len(groups) - already_done} left to look up.")

    n_processed = 0
    n_resolved = 0
    n_failed = 0
    for i, row in groups.iterrows():
        key = row["osmid_key"]
        if key in cache:
            continue

        success, name = reverse_geocode_one(row["lat"], row["lon"], api_key)
        n_processed += 1
        if not success:
            n_failed += 1
            # Deliberately NOT cached -- see reverse_geocode_one docstring.
            if n_failed >= 5 and n_failed == n_processed:
                raise SystemExit(
                    f"\nStopping after {n_failed} consecutive request failures -- "
                    "this looks systemic (bad/unconfirmed API key, quota, or network issue), "
                    "not occasional bad luck. Check the error messages printed above, fix it, "
                    "and re-run -- already-resolved streets are cached and won't be re-queried."
                )
        else:
            cache[key] = name
            if name:
                n_resolved += 1

        if n_processed % CHECKPOINT_EVERY == 0:
            save_cache(cache)
            print(f"  ...{n_processed} looked up this run ({n_resolved} resolved, {n_failed} failed), checkpointed.")

        time.sleep(SECONDS_BETWEEN_REQUESTS)

    save_cache(cache)
    print(f"\nDone this run: {n_processed} looked up, {n_resolved} resolved to a real name.")

    print("Applying resolved names back into the roads dataset...")
    roads = roads.copy()
    roads["_osmid_key"] = roads["osmid"].astype(str)
    roads["_unnamed"] = roads["name"].apply(is_empty)

    resolved_count = 0
    for idx in roads.index[roads["_unnamed"]]:
        key = roads.at[idx, "_osmid_key"]
        resolved_name = cache.get(key)
        if resolved_name:
            roads.at[idx, "name"] = resolved_name
            resolved_count += 1

    roads = roads.drop(columns=["_osmid_key", "_unnamed"])
    roads.to_file(ROADS_PATH, driver="GeoJSON")

    still_unnamed = roads["name"].apply(is_empty).sum()
    print(f"\n--- ROAD NAME RESOLUTION SUMMARY ---")
    print(f"Road segments updated with a resolved name: {resolved_count}")
    print(f"Road segments still unnamed: {still_unnamed} / {len(roads)} ({100 * still_unnamed / len(roads):.1f}%)")
    print(f"{ROADS_PATH} updated in place.")
    print("Re-run scripts/estimate_road_risk.py to pick up the new names in the app.")
    print("-------------------------------------")


if __name__ == "__main__":
    main()
