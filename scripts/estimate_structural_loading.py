"""
estimate_structural_loading.py
===============================
The structural engineering angle: for the highest flood-risk areas
already identified (predict_flood_risk.py), estimate the actual
hydrostatic (standing-water) load floodwater puts on a building wall at
a few plausible depth scenarios, and flag when that crosses from "water
damage" into "genuine structural risk."

Hydrostatic pressure grows with depth (it's not the same problem at 0.3m
vs 1.5m): pressure at the base of a submerged wall is

    P = rho * g * h        (Pa, i.e. N per m^2)

and the total resultant force pushing on one meter of wall width (found
by integrating that triangular pressure distribution from the water
surface down to depth h) is

    F = 0.5 * rho * g * h^2      (N per m of wall width)

  rho = 1000 kg/m^3 (fresh water density)
  g   = 9.81 m/s^2

Depth scenarios used here aren't guessed -- they're grounded in two real
reference points:
  - 0.45m ("knee-deep") matches on-the-ground reporting of actual Lekki
    flooding (Lagos, 2026) -- not a made-up number.
  - 0.90m is a published structural threshold: single-wythe unreinforced
    masonry walls (the common Nigerian sandcrete block wall) are
    generally considered safe from structural damage up to about 90cm of
    standing water, PROVIDED the wall has a rigid top support (e.g. tied
    into a floor/roof) -- per FEMA / WBDG flood-resistant design
    guidance. Past that depth, risk shifts from "wet damage" to
    "structural distress."

This is a genuine physics calculation (the hydrostatic formulas are
exact), but it's still a simplified engineering estimate: real flood
conditions can add hydrodynamic (flowing water) force, debris impact,
and depend on the wall's actual support conditions and material
strength, none of which are modeled here. Treat this as a first-pass
screening tool, not a substitute for a site-specific structural
assessment.

Requires: data/etiosa_dynamic_risk_grid.geojson (predict_flood_risk.py)

Run with: python scripts/estimate_structural_loading.py
Output: outputs/etiosa_structural_loading.png
"""

from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"

RISK_GRID_PATH = DATA_DIR / "etiosa_dynamic_risk_grid.geojson"

RHO_WATER = 1000.0  # kg/m^3, fresh water
G = 9.81  # m/s^2

# (label, depth in meters, source note)
DEPTH_SCENARIOS = [
    ("Ankle (0.3m)", 0.3, "illustrative shallow scenario"),
    ("Knee-deep (0.45m)", 0.45, "matches reported 2026 Lekki flood depth"),
    ("Masonry safe-limit (0.9m)", 0.9, "FEMA/WBDG single-wythe masonry threshold"),
    ("Severe (1.5m)", 1.5, "illustrative severe scenario"),
]

MASONRY_SAFE_DEPTH_M = 0.9
TOP_N_AREAS = 5


def hydrostatic_pressure_kpa(depth_m):
    """P = rho * g * h, converted from Pa to kPa."""
    return RHO_WATER * G * depth_m / 1000.0


def hydrostatic_force_kn_per_m(depth_m):
    """F = 0.5 * rho * g * h^2, converted from N/m to kN/m."""
    return 0.5 * RHO_WATER * G * depth_m**2 / 1000.0


def require_file(path, produced_by):
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run {produced_by} first.")


def load_top_risk_areas(top_n=TOP_N_AREAS):
    require_file(RISK_GRID_PATH, "predict_flood_risk.py")
    print("Loading forecast risk grid and picking the highest-risk areas...")
    grid = gpd.read_file(RISK_GRID_PATH)
    top_areas = grid.nlargest(top_n, "dynamic_risk_score")
    return top_areas


def print_loading_table(top_areas):
    print("\n--- STRUCTURAL LOADING ESTIMATE (per meter width of wall) ---")
    print(
        "Scenario depths are grounded in real references: 0.45m = reported Lekki "
        "flood depth; 0.9m = published safe limit for single-wythe masonry walls."
    )
    for _, area in top_areas.iterrows():
        print(f"\n{area['area_name']}  (forecast risk tomorrow: {area['risk_tier']}, "
              f"{area['forecast_rain_mm_tomorrow']:.0f}mm rain forecast)")
        for label, depth, note in DEPTH_SCENARIOS:
            pressure = hydrostatic_pressure_kpa(depth)
            force = hydrostatic_force_kn_per_m(depth)
            flag = "exceeds typical masonry safe limit -- structural risk" if depth > MASONRY_SAFE_DEPTH_M else "within typical masonry safe limit"
            print(
                f"    {label:<28} pressure={pressure:5.2f} kPa  "
                f"force={force:6.2f} kN/m   [{flag}]"
            )
    print("\nNote: idealized standing-water (hydrostatic) loads only -- excludes flowing-water")
    print("(hydrodynamic) force, debris impact, and assumes the wall has a rigid top support.")
    print("Screening estimate only; not a substitute for a site-specific structural assessment.")
    print("---------------------------------------------------------------")


def plot_loading_curve():
    """
    A continuous force-vs-depth curve (not just the four scenario points)
    makes the physical relationship clear: force grows with the SQUARE of
    depth, not linearly -- doubling the water depth roughly quadruples the
    load on the wall, which is exactly why "just a bit deeper" flooding is
    disproportionately more dangerous structurally.
    """
    depths = np.linspace(0, 2.0, 200)
    forces = [hydrostatic_force_kn_per_m(d) for d in depths]
    pressures = [hydrostatic_pressure_kpa(d) for d in depths]

    fig, ax1 = plt.subplots(figsize=(9, 6))
    ax1.plot(depths, forces, color="firebrick", linewidth=2, label="Resultant force (kN/m)")
    ax1.set_xlabel("Floodwater depth against wall (m)")
    ax1.set_ylabel("Resultant hydrostatic force (kN per m of wall width)", color="firebrick")
    ax1.tick_params(axis="y", labelcolor="firebrick")

    ax2 = ax1.twinx()
    ax2.plot(depths, pressures, color="steelblue", linewidth=1.5, linestyle="--", label="Base pressure (kPa)")
    ax2.set_ylabel("Hydrostatic pressure at wall base (kPa)", color="steelblue")
    ax2.tick_params(axis="y", labelcolor="steelblue")

    ax1.axvline(MASONRY_SAFE_DEPTH_M, color="black", linestyle=":", linewidth=1.5)
    ax1.text(
        MASONRY_SAFE_DEPTH_M + 0.03, max(forces) * 0.9,
        "0.9m masonry\nsafe-limit\n(FEMA/WBDG)",
        fontsize=8, va="top",
    )

    for label, depth, _ in DEPTH_SCENARIOS:
        ax1.plot(depth, hydrostatic_force_kn_per_m(depth), "o", color="black", markersize=5)

    ax1.set_title("Hydrostatic Load on a Building Wall vs. Floodwater Depth")
    fig.tight_layout()

    out_path = OUTPUTS_DIR / "etiosa_structural_loading.png"
    fig.savefig(out_path, dpi=200)
    print(f"\nLoading curve saved to {out_path}")


def main():
    top_areas = load_top_risk_areas()
    print_loading_table(top_areas)
    plot_loading_curve()


if __name__ == "__main__":
    main()
