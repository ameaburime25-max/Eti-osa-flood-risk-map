"""
resolve_road_names_alt_tags.py
================================
A second, cheaper pass at the "Unnamed road" problem (see Section 12 of
MODEL_CHANGELOG.md and scripts/resolve_road_names.py). That first pass
used LocationIQ (a Nominatim-based reverse geocoder) and recovered 396 of
12,264 unnamed segments -- a real but modest gain, capped by the fact
that LocationIQ is fundamentally built on the same OSM map data our own
extract already came from.

This pass checks something genuinely different and easier: our original
Overpass/osmnx pull (build_basemap.py) only ever fetched a fixed set of
tags (highway, name, oneway, ref, bridge, maxspeed, junction, access,
tunnel, lanes) -- it never asked for `alt_name`, `old_name`, `loc_name`,
or `name:en`, which are all real, separate OSM tags that sometimes carry
a usable name even when the primary `name` tag on that specific way was
left empty. We simply never looked. This queries the Overpass API
directly (not Nominatim -- a different OSM service, built exactly for
this kind of bulk-by-known-ID structured tag lookup, no usage-policy
conflict) for the full tag set on every still-unnamed way, and fills in
`name` from whichever alternate tag has something, in priority order.

Purely additive: only ever fills in currently-EMPTY name cells, never
overwrites a name that's already there (from OSM itself or from the
LocationIQ pass).

Requires: data/etiosa_roads.geojson (build_basemap.py, ideally after
resolve_road_names.py has already run)
Run with: python scripts/resolve_road_names_alt_tags.py
Updates: data/etiosa_roads.geojson (the `name` column, in place)
"""

import time
from pathlib import Path

import geopandas as gpd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
ROADS_PATH = DATA_DIR / "etiosa_roads.geojson"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
BATCH_SIZE = 400  # conservative -- comfortably inside Overpass's normal query limits
SECONDS_BETWEEN_BATCHES = 2  # be a reasonable, non-hammering citizen of the free public instance

# Priority order: alt_name is the most likely to be a genuine commonly-
# used alternate name; name:en next (English-transliterated, useful if
# the primary name was only ever recorded in a different script/language);
# loc_name (informal local name) and old_name (superseded but still
# recognizable) last, since they're more likely to be stale or informal.
ALT_NAME_TAGS = ["alt_name", "name:en", "loc_name", "old_name"]


def is_empty(value):
    if value is None:
        return True
    s = str(value).strip()
    return s in ("", "None", "nan", "[]")


def flatten_way_ids(osmid_value):
    """osmid can be a single int or a list of ints (osmnx merges multiple
    OSM ways into one graph edge at times) -- normalize to a flat list."""
    if isinstance(osmid_value, (list, tuple)):
        return [int(v) for v in osmid_value]
    try:
        return [int(osmid_value)]
    except (TypeError, ValueError):
        return []


OVERPASS_HEADERS = {
    # A generic library User-Agent has been enough to trip up other free
    # OSM-ecosystem services in this project (Nominatim requires one
    # explicitly); sending a real one here too in case overpass-api.de's
    # front end is doing the same kind of filtering.
    "User-Agent": "EtiOsaFloodRiskProject/1.0 (student portfolio project; contact: iaburime1@sheffield.ac.uk)",
    "Accept": "application/json",
}


def query_overpass_batch(way_ids, attempt=0):
    id_list = ",".join(str(w) for w in way_ids)
    query = f"[out:json][timeout:180];way(id:{id_list});out tags;"
    resp = requests.post(OVERPASS_URL, data={"data": query}, headers=OVERPASS_HEADERS, timeout=200)

    if resp.status_code != 200:
        print(f"    HTTP {resp.status_code} for a batch of {len(way_ids)} way IDs.")
        print(f"    Response body: {resp.text[:500]}")
        if resp.status_code == 429 and attempt < 2:
            print("    Rate-limited -- backing off 30s and retrying this batch...")
            time.sleep(30)
            return query_overpass_batch(way_ids, attempt=attempt + 1)
        if len(way_ids) > 50 and attempt < 3:
            # If it's not a plain rate limit, a smaller batch rules out
            # a payload-size/complexity issue before giving up entirely.
            print(f"    Retrying as two smaller batches of {len(way_ids) // 2}...")
            mid = len(way_ids) // 2
            result = query_overpass_batch(way_ids[:mid], attempt=attempt + 1)
            result.update(query_overpass_batch(way_ids[mid:], attempt=attempt + 1))
            return result
        print(f"    Giving up on this batch of {len(way_ids)} way IDs after diagnostics above.")
        return {}

    data = resp.json()
    result = {}
    for el in data.get("elements", []):
        way_id = el.get("id")
        tags = el.get("tags", {})
        result[way_id] = tags
    return result


def main():
    if not ROADS_PATH.exists():
        raise SystemExit(f"{ROADS_PATH} not found. Run build_basemap.py first.")

    roads = gpd.read_file(ROADS_PATH)
    roads["_unnamed"] = roads["name"].apply(is_empty)
    n_unnamed = roads["_unnamed"].sum()
    print(f"{n_unnamed} / {len(roads)} road segments currently unnamed ({100 * n_unnamed / len(roads):.1f}%).")

    unnamed = roads[roads["_unnamed"]]
    all_way_ids = sorted({wid for osmid in unnamed["osmid"] for wid in flatten_way_ids(osmid)})
    print(f"Querying Overpass for {len(all_way_ids)} distinct way IDs, in batches of {BATCH_SIZE}...")

    way_tags = {}
    n_batches = (len(all_way_ids) - 1) // BATCH_SIZE + 1
    for i in range(0, len(all_way_ids), BATCH_SIZE):
        batch = all_way_ids[i : i + BATCH_SIZE]
        try:
            batch_result = query_overpass_batch(batch)
        except requests.exceptions.RequestException as e:
            # A network hiccup on one batch shouldn't lose everything
            # already fetched -- skip this batch (its way IDs just stay
            # unresolved this run) and keep going.
            print(f"    Network error on this batch ({e.__class__.__name__}) -- skipping it for now.")
            batch_result = {}
        way_tags.update(batch_result)
        print(f"  ...batch {i // BATCH_SIZE + 1} / {n_batches} done "
              f"({len(way_tags)} way tag sets fetched so far).")
        time.sleep(SECONDS_BETWEEN_BATCHES)

    print("Applying any alt-name tags found to still-unnamed segments...")
    resolved_count = 0
    tag_source_counts = {tag: 0 for tag in ALT_NAME_TAGS}
    for idx in roads.index[roads["_unnamed"]]:
        way_ids = flatten_way_ids(roads.at[idx, "osmid"])
        for way_id in way_ids:
            tags = way_tags.get(way_id, {})
            found = None
            for tag_key in ALT_NAME_TAGS:
                if tags.get(tag_key):
                    found = tags[tag_key]
                    tag_source_counts[tag_key] += 1
                    break
            if found:
                roads.at[idx, "name"] = found
                resolved_count += 1
                break

    roads = roads.drop(columns=["_unnamed"])
    roads.to_file(ROADS_PATH, driver="GeoJSON")

    still_unnamed = roads["name"].apply(is_empty).sum()
    print("\n--- ALT-TAG RESOLUTION SUMMARY ---")
    print(f"Road segments newly resolved from alternate tags: {resolved_count}")
    for tag_key, count in tag_source_counts.items():
        print(f"  from {tag_key}: {count}")
    print(f"Road segments still unnamed: {still_unnamed} / {len(roads)} ({100 * still_unnamed / len(roads):.1f}%)")
    print(f"{ROADS_PATH} updated in place.")
    print("Re-run scripts/estimate_road_risk.py to pick up the new names in the app.")
    print("-----------------------------------")


if __name__ == "__main__":
    main()
