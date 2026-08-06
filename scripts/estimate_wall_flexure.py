"""
estimate_wall_flexure.py
=========================
The structural engineering core of the project: does a flooded wall
actually survive the water pressure on it, or does it crack?

Earlier work (estimate_structural_loading.py) computed the hydrostatic
force/pressure floodwater puts on a wall. That's a real number, but on
its own it doesn't say anything about a specific building -- force alone
isn't a pass/fail engineering answer.

This script finishes the job: it treats the wall as a cantilever fixed
at its base (the floor slab/foundation), bending under the triangular
water pressure profile, and checks the resulting bending stress against
the real characteristic flexural strength of masonry from BS 5628 Part
1:1992 Table 3 (concrete blockwork, failure plane perpendicular to the
bed joint -- i.e. the wall cracking across a horizontal mortar line,
exactly what happens when a wall bends under lateral pressure).

Two real construction typologies, assigned per real named area:
  - "planned_estate" (Ikoyi, Lekki Phase 1/2, Banana Island, Victoria
    Garden City, Victoria Island): regulated construction, thicker
    225mm blockwork, good mortar mix (BS 5628 designation i-iii,
    fkx = 0.9 N/mm2).
  - "informal_older_stock" (everywhere else in the grid -- Ajah,
    Badore, Ikate, Sangotedo, etc.): thinner 150mm blockwork, weaker
    mortar mix (designation iv, fkx = 0.6 N/mm2), consistent with
    documented lower-regulation development along the Lekki-Ajah
    corridor.

Requires: data/etiosa_dynamic_risk_grid.geojson (predict_flood_risk.py)
Run with: python scripts/estimate_wall_flexure.py
Saves: data/etiosa_wall_flexure.geojson
Also saves: outputs/etiosa_wall_flexure_chart.png
"""

from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd
from scipy.optimize import brentq

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"

GRID_PATH = DATA_DIR / "etiosa_dynamic_risk_grid.geojson"
OUT_PATH = DATA_DIR / "etiosa_wall_flexure.geojson"

RHO_WATER = 1000  # kg/m3
G = 9.81  # m/s2
WALL_HEIGHT_M = 3.0  # typical Nigerian residential storey height, floor to roof/ring beam tie (assumption)

PLANNED_ESTATE_KEYWORDS = [
    "ikoyi", "lekki phase i", "lekki phase ii", "banana island",
    "victoria garden city", "victoria island",
]

WALL_SPECS = {
    "planned_estate": {
        "thickness_mm": 225,
        "fkx_n_mm2": 0.9,
        "mortar_note": "BS 5628 designation (i)-(iii), good mix",
    },
    "informal_older_stock": {
        "thickness_mm": 150,
        "fkx_n_mm2": 0.6,
        "mortar_note": "BS 5628 designation (iv), weak mix",
    },
}

DEPTH_SCENARIOS_M = [0.3, 0.45, 0.9, 1.5]


def require_file(path, produced_by):
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run {produced_by} first.")


def classify_construction_typology(area_name):
    name = str(area_name).lower()
    if any(kw in name for kw in PLANNED_ESTATE_KEYWORDS):
        return "planned_estate"
    return "informal_older_stock"


def bending_moment_nm_per_m(depth_m):
    return RHO_WATER * G * depth_m**3 / 6


def bending_moment_propped_nm_per_m(depth_m, wall_height_m=WALL_HEIGHT_M):
    if depth_m <= 0:
        return 0.0
    depth_m = min(depth_m, wall_height_m)
    k = depth_m / wall_height_m
    return RHO_WATER * G * depth_m**3 * (1 / 6 - k * (5 - k) / 40)


def section_modulus_mm3_per_m(thickness_mm):
    return 1000 * thickness_mm**2 / 6


def flexural_fos(depth_m, wall_spec, wall_height_m=WALL_HEIGHT_M):
    if depth_m <= 0:
        return float("inf")
    moment_nmm = bending_moment_propped_nm_per_m(depth_m, wall_height_m) * 1000
    z = section_modulus_mm3_per_m(wall_spec["thickness_mm"])
    stress_n_mm2 = moment_nmm / z
    return wall_spec["fkx_n_mm2"] / stress_n_mm2


def critical_failure_depth_free_m(wall_spec):
    z = section_modulus_mm3_per_m(wall_spec["thickness_mm"])
    return ((wall_spec["fkx_n_mm2"] * z * 6) / (1000 * RHO_WATER * G)) ** (1 / 3)


def critical_failure_depth_m(wall_spec, wall_height_m=WALL_HEIGHT_M):
    z = section_modulus_mm3_per_m(wall_spec["thickness_mm"])
    target_moment_nm = wall_spec["fkx_n_mm2"] * z / 1000  # N.mm -> N.m

    def residual(depth_m):
        return bending_moment_propped_nm_per_m(depth_m, wall_height_m) - target_moment_nm

    if residual(wall_height_m) < 0:
        return None
    return brentq(residual, 1e-6, wall_height_m)


def compute_wall_flexure(grid):
    print("Classifying construction typology and computing wall flexural capacity...")
    grid = grid.copy()
    grid["construction_typology"] = grid["area_name"].apply(classify_construction_typology)

    thickness, fkx, critical_depth, critical_depth_free = [], [], [], []
    fos_columns = {f"fos_at_{d}m".replace(".", "_"): [] for d in DEPTH_SCENARIOS_M}

    for typology in grid["construction_typology"]:
        spec = WALL_SPECS[typology]
        thickness.append(spec["thickness_mm"])
        fkx.append(spec["fkx_n_mm2"])
        critical_depth.append(critical_failure_depth_m(spec))
        critical_depth_free.append(critical_failure_depth_free_m(spec))
        for d in DEPTH_SCENARIOS_M:
            col = f"fos_at_{d}m".replace(".", "_")
            fos_columns[col].append(flexural_fos(d, spec))

    grid["wall_thickness_mm"] = thickness
    grid["wall_fkx_n_mm2"] = fkx
    grid["critical_failure_depth_m"] = critical_depth
    grid["critical_failure_depth_free_m"] = critical_depth_free
    for col, values in fos_columns.items():
        grid[col] = values

    # Live check: estimate today's likely flood depth from the model's own
    # forecast-driven risk score, capped at the same 1.5m upper bound used
    # elsewhere in this project (grounded in real reported Lagos flooding
    # events) -- then see how close that puts each real area to its own
    # cracking point, today.
    grid["estimated_depth_m_today"] = grid["dynamic_risk_score"] * 1.5
    grid["live_flexural_fos"] = [
        flexural_fos(depth, WALL_SPECS[typology])
        for typology, depth in zip(grid["construction_typology"], grid["estimated_depth_m_today"])
    ]
    grid["margin_to_failure_m"] = grid["critical_failure_depth_m"] - grid["estimated_depth_m_today"]

    return grid


def print_report(grid):
    print("\n--- WALL FLEXURAL CAPACITY BY AREA ---")
    summary = (
        grid[["area_name", "construction_typology", "wall_thickness_mm", "critical_failure_depth_m", "critical_failure_depth_free_m"]]
        .drop_duplicates(subset="area_name")
        .sort_values("critical_failure_depth_m")
    )
    for _, row in summary.iterrows():
        print(
            f"  {row['area_name']:<25} {row['construction_typology']:<22} "
            f"{row['wall_thickness_mm']:.0f}mm wall  "
            f"cracks at {row['critical_failure_depth_m']:.2f}m (propped by roof) "
            f"vs {row['critical_failure_depth_free_m']:.2f}m (free cantilever, no roof)"
        )
    print("---------------------------------------")


def print_live_report(grid):
    print("\n--- TODAY'S LIVE WALL STATUS (forecast-driven) ---")
    ranked = grid.sort_values("live_flexural_fos")
    for _, row in ranked.head(10).iterrows():
        status = "PREDICTED FAILURE" if row["live_flexural_fos"] < 1 else "holds"
        print(
            f"  {row['area_name']:<25} est. depth today={row['estimated_depth_m_today']:.2f}m  "
            f"cracks at {row['critical_failure_depth_m']:.2f}m  "
            f"FoS={row['live_flexural_fos']:.2f}  ({status})"
        )
    n_failing = (grid["live_flexural_fos"] < 1).sum()
    print(f"\nAreas predicted to fail today: {n_failing} / {len(grid)}")
    print("---------------------------------------")


def plot_critical_depths(grid, out_path):
    summary = (
        grid[["area_name", "construction_typology", "critical_failure_depth_m"]]
        .drop_duplicates(subset="area_name")
        .sort_values("critical_failure_depth_m")
    )
    colors = summary["construction_typology"].map(
        {"planned_estate": "#2ca02c", "informal_older_stock": "#d62728"}
    )
    fig, ax = plt.subplots(figsize=(10, 8))
    ax.barh(summary["area_name"], summary["critical_failure_depth_m"], color=colors)
    ax.set_xlabel("Floodwater depth at which the wall is predicted to crack (m)")
    ax.set_title("Wall flexural failure depth by area (BS 5628 masonry check)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=200)
    print(f"\nChart saved to {out_path}")


def main():
    require_file(GRID_PATH, "predict_flood_risk.py")
    grid = gpd.read_file(GRID_PATH)

    grid = compute_wall_flexure(grid)

    grid_to_save = grid.copy()
    grid_to_save.to_file(OUT_PATH, driver="GeoJSON")
    print(f"\nWall flexure data saved to {OUT_PATH}")

    print_report(grid)
    print_live_report(grid)
    plot_critical_depths(grid, OUTPUTS_DIR / "etiosa_wall_flexure_chart.png")


if __name__ == "__main__":
    main()
