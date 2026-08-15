import streamlit as st
import folium
from streamlit_folium import st_folium
import geopandas as gpd
from shapely.geometry import Point
import ast

st.set_page_config(page_title="Eti-Osa Flood Risk", layout="wide")

risk_grid = gpd.read_file("data/etiosa_wall_flexure.geojson")
road_risk = gpd.read_file("data/etiosa_road_risk.geojson")


def _flatten(value):
    if isinstance(value, list):
        return value[0] if len(value) > 0 else None
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list) and len(parsed) > 0:
                return parsed[0]
        except (ValueError, SyntaxError):
            return text.strip("[]'\" ")
    return value


# --- Sidebar: every control lives here, so the main view is just the
# answer (risk summary + map), not a stack of settings you have to
# scroll past first -- same idea as a weather app tucking unit/location
# settings away and leading with today's conditions. ---
with st.sidebar:
    st.title("Eti-Osa Flood Risk")
    st.caption("Lekki, Ikoyi & Victoria Island, Lagos")

    day_label = st.radio("Show risk for", ["Today", "Tomorrow"], horizontal=True)
    suffix = "today" if day_label == "Today" else "tomorrow"
    if suffix == "tomorrow":
        st.caption("Forward forecast, not current conditions.")

    area_names = sorted(risk_grid["area_name"].unique())
    selected_name = st.selectbox("View an area", ["(none selected)"] + area_names)

# --- Main view ---
st.title("Eti-Osa Flood Risk")
st.caption(f"{day_label}'s flood risk ⋮ Eti-Osa LGA, Lagos, Nigeria")

flooded_roads = (
    road_risk[road_risk[f"flood_prone_{suffix}"]][
        ["road_name", "area_name", f"dynamic_risk_{suffix}", f"flood_cause_{suffix}", "geometry"]
    ]
    .rename(columns={f"dynamic_risk_{suffix}": "display_risk", f"flood_cause_{suffix}": "display_cause"})
    .copy()
)
flooded_roads["road_name"] = flooded_roads["road_name"].apply(_flatten).astype(str)
flooded_roads["display_cause"] = flooded_roads["display_cause"].apply(_flatten).astype(str)

tier_colors = {
    "Low": "#3ecf6e",
    "Medium": "#e8c547",
    "High": "#e8883d",
    "Very High": "#e8483d",
}
tier_bg = {
    "Low": "#132a1c",
    "Medium": "#2e2712",
    "High": "#2e1e12",
    "Very High": "#2e1414",
}


def style_function(feature):
    tier = feature["properties"][f"risk_tier_{suffix}"]
    return {
        "fillColor": tier_colors.get(tier, "gray"),
        "color": "white",
        "weight": 0.6,
        "fillOpacity": 0.65,
    }


map_center = [6.46, 3.53]
map_zoom = 12
dropdown_area = None
if selected_name != "(none selected)":
    matches = risk_grid[risk_grid["area_name"] == selected_name]
    dropdown_area = matches.loc[matches[f"dynamic_risk_score_{suffix}"].idxmax()]
    centroid = dropdown_area.geometry.centroid
    map_center = [centroid.y, centroid.x]
    map_zoom = 15

# Reserve the hero summary's position above the map now, fill it in
# after we know which area is selected (which can depend on a map
# click, resolved further down) -- Streamlit keeps elements in the
# order their placeholder was created, not the order they're written to.
hero = st.container()

st.markdown(
    """
    <style>
    div[data-testid="stPopover"] button {
        margin-top: 14px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
map_title_col, map_menu_col = st.columns([6, 1], vertical_alignment="center")
with map_title_col:
    st.subheader("Interactive risk map")
with map_menu_col:
    with st.popover("⋮"):
        st.caption("Map layers")
        show_area = st.checkbox("Area risk", value=True)
        show_roads = st.checkbox("Flood-prone roads", value=True)
        show_drainage = st.checkbox("Drainage channels", value=False)
        show_coastline = st.checkbox("Lagoon / coastline", value=False)
st.caption("Click on map")

m = folium.Map(location=map_center, zoom_start=map_zoom, tiles="CartoDB dark_matter")

if show_area:
    folium.GeoJson(
        risk_grid.to_json(),
        style_function=style_function,
        tooltip=folium.GeoJsonTooltip(
            fields=["area_name", f"dynamic_risk_score_{suffix}", f"forecast_rain_mm_{suffix}", f"risk_tier_{suffix}"],
            aliases=["Area:", "Risk score:", "Forecast rain (mm):", "Risk tier:"],
        ),
    ).add_to(m)

if show_drainage:
    drainage_lines = gpd.read_file("data/etiosa_drainage_lines.geojson")

    def _dflatten(value):
        return value[0] if isinstance(value, list) else value

    if "name" in drainage_lines.columns:
        drainage_lines = drainage_lines.copy()
        drainage_lines["name"] = drainage_lines["name"].apply(_dflatten).astype(str)
        drainage_lines.loc[drainage_lines["name"].isin(["None", "nan", ""]), "name"] = "Unnamed drain"

    folium.GeoJson(
        drainage_lines.to_json(),
        style_function=lambda feature: {"color": "#00bfff", "weight": 1.8, "opacity": 0.8},
        tooltip=folium.GeoJsonTooltip(fields=["name"], aliases=["Drainage:"]) if "name" in drainage_lines.columns else None,
    ).add_to(m)

if show_coastline:
    drainage_polygons = gpd.read_file("data/etiosa_drainage_polygons.geojson")
    TIDAL_WATER_NAMES = ["Lagos Lagoon", "Five Cowries Creek", "Commodore Channel"]
    coastline = drainage_polygons[drainage_polygons["name"].isin(TIDAL_WATER_NAMES)]
    folium.GeoJson(
        coastline.to_json(),
        style_function=lambda feature: {"fillColor": "#1f6feb", "color": "#1f6feb", "weight": 1, "fillOpacity": 0.25},
        tooltip=folium.GeoJsonTooltip(fields=["name"], aliases=["Water body:"]),
    ).add_to(m)

if show_roads and len(flooded_roads) > 0:
    flooded_geojson = flooded_roads.to_json()
    road_tooltip = folium.GeoJsonTooltip(
        fields=["road_name", "area_name", "display_risk", "display_cause"],
        aliases=["Road:", "Area:", "Risk score:", "Likely cause:"],
    )
    folium.GeoJson(
        flooded_geojson,
        style_function=lambda feature: {"color": "#ff3333", "weight": 14, "opacity": 0.12},
    ).add_to(m)
    folium.GeoJson(
        flooded_geojson,
        style_function=lambda feature: {"color": "#ff3333", "weight": 4, "opacity": 0.75},
        tooltip=road_tooltip,
    ).add_to(m)

if dropdown_area is not None:
    centroid = dropdown_area.geometry.centroid
    folium.Marker(
        location=[centroid.y, centroid.x],
        popup=selected_name,
        icon=folium.Icon(color="blue", icon="info-sign"),
    ).add_to(m)

map_data = st_folium(m, width=1400, height=650, key=f"{selected_name}_{suffix}")

selected_area = dropdown_area

if selected_area is None and map_data and map_data.get("last_clicked"):
    click_point = Point(map_data["last_clicked"]["lng"], map_data["last_clicked"]["lat"])
    matches = risk_grid[risk_grid.contains(click_point)]
    if len(matches) > 0:
        selected_area = matches.iloc[0]

if selected_area is None:
    selected_area = risk_grid.loc[risk_grid[f"dynamic_risk_score_{suffix}"].idxmax()]
    area_label = "Highest risk area right now"
else:
    area_label = "Selected area"

tier = selected_area[f"risk_tier_{suffix}"]
score = selected_area[f"dynamic_risk_score_{suffix}"]
rain = selected_area[f"forecast_rain_mm_{suffix}"]

with hero:
    st.markdown(
        f"""
        <div style="background:{tier_bg.get(tier, '#1c1c1c')};border:1px solid {tier_colors.get(tier, '#444')}33;
                    border-radius:16px;padding:24px 28px;display:flex;align-items:center;justify-content:space-between;
                    flex-wrap:wrap;gap:20px;">
          <div>
            <div style="font-size:13px;opacity:0.65;text-transform:uppercase;letter-spacing:0.06em;">{area_label}</div>
            <div style="font-size:34px;font-weight:700;line-height:1.15;">{selected_area['area_name']}</div>
            <div style="font-size:18px;font-weight:600;color:{tier_colors.get(tier, '#ccc')};margin-top:4px;">{tier} risk</div>
          </div>
          <div style="display:flex;gap:32px;">
            <div>
              <div style="font-size:13px;opacity:0.65;">Risk score</div>
              <div style="font-size:24px;font-weight:600;">{score:.2f}</div>
            </div>
            <div>
              <div style="font-size:13px;opacity:0.65;">Forecast rain</div>
              <div style="font-size:24px;font-weight:600;">{rain:.1f} mm</div>
            </div>
            <div>
              <div style="font-size:13px;opacity:0.65;">Areas monitored</div>
              <div style="font-size:24px;font-weight:600;">{len(risk_grid)}</div>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.write("")

st.divider()

TYPOLOGY_LABELS = {
    "planned_estate": "Planned estate (225mm blockwork, BS 5628 mortar designation i-iii)",
    "informal_older_stock": "Informal / older housing stock (150mm blockwork, BS 5628 mortar designation iv)",
}

if suffix == "today":
    depth_col, fos_col, margin_col = "estimated_depth_m_today", "live_flexural_fos", "margin_to_failure_m"
else:
    depth_col, fos_col, margin_col = "estimated_depth_m_tomorrow", "live_flexural_fos_tomorrow", "margin_to_failure_m_tomorrow"

with st.expander("Show the structural engineering"):
    typology = selected_area["construction_typology"]
    depth_value = selected_area[depth_col]
    crack_depth = selected_area["critical_failure_depth_m"]
    margin = selected_area[margin_col]
    live_fos = selected_area[fos_col]

    st.write(
        f"**{selected_area['area_name']}** is modeled as **{TYPOLOGY_LABELS.get(typology, typology)}**. "
        f"Treating the ground-floor wall as a cantilever fixed at its base, {day_label.lower()}'s forecast-implied "
        f"floodwater depth of **{depth_value:.2f}m** is checked against the depth at which this wall's "
        f"bending strength (flexural strength fkx = {selected_area['wall_fkx_n_mm2']:.1f} N/mm2, "
        f"BS 5628 Part 1:1992) is exceeded and it is predicted to crack: **{crack_depth:.2f}m**."
    )

    tcol1, tcol2, tcol3 = st.columns(3)
    tcol1.metric(f"Estimated depth ({day_label.lower()})", f"{depth_value:.2f} m")
    tcol2.metric("Wall cracks at", f"{crack_depth:.2f} m")
    tcol3.metric("Safety margin", f"{margin:.2f} m")

    if live_fos < 1:
        st.error(f"Factor of Safety = {live_fos:.2f} -- structural failure predicted at {day_label.lower()}'s forecast depth.")
    else:
        st.success(f"Factor of Safety = {live_fos:.2f} -- wall holds at {day_label.lower()}'s forecast depth.")
