import streamlit as st
import folium
from streamlit_folium import st_folium
import geopandas as gpd
from shapely.geometry import Point

st.set_page_config(page_title="Eti-Osa Flood Risk", layout="wide")
st.title("Eti-Osa Flood Risk")
st.caption("Live, forecast-driven flood risk for Lekki, Ikoyi & Victoria Island, Lagos, Nigeria")

risk_grid = gpd.read_file("data/etiosa_wall_flexure.geojson")

col1, col2, col3, col4 = st.columns(4)

area_names = sorted(risk_grid["area_name"].unique())
selected_name = st.selectbox("Go to a specific area on the map", ["(none selected)"] + area_names)

tier_colors = {
    "Low": "green",
    "Medium": "yellow",
    "High": "orange",
    "Very High": "red",
}

def style_function(feature):
    tier = feature["properties"]["risk_tier"]
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
    dropdown_area = matches.loc[matches["dynamic_risk_score"].idxmax()]
    centroid = dropdown_area.geometry.centroid
    map_center = [centroid.y, centroid.x]
    map_zoom = 15

m = folium.Map(location=map_center, zoom_start=map_zoom, tiles="CartoDB dark_matter")
folium.GeoJson(
    risk_grid.to_json(),
    style_function=style_function,
    tooltip=folium.GeoJsonTooltip(
        fields=["area_name", "dynamic_risk_score", "forecast_rain_mm_tomorrow", "risk_tier"],
        aliases=["Area:", "Risk score:", "Forecast rain (mm):", "Risk tier:"],
    ),
).add_to(m)

if dropdown_area is not None:
    centroid = dropdown_area.geometry.centroid
    folium.Marker(
        location=[centroid.y, centroid.x],
        popup=selected_name,
        icon=folium.Icon(color="blue", icon="info-sign"),
    ).add_to(m)

st.subheader("Interactive risk map")
st.caption("Click an area, or use the dropdown above, to see its details")
map_data = st_folium(m, width=1400, height=650, key=selected_name)

selected_area = dropdown_area

if selected_area is None and map_data and map_data.get("last_clicked"):
    click_point = Point(map_data["last_clicked"]["lng"], map_data["last_clicked"]["lat"])
    matches = risk_grid[risk_grid.contains(click_point)]
    if len(matches) > 0:
        selected_area = matches.iloc[0]

if selected_area is None:
    selected_area = risk_grid.loc[risk_grid["dynamic_risk_score"].idxmax()]
    area_label = "Highest risk area"
else:
    area_label = "Selected area"

col1.metric("Areas monitored", len(risk_grid))
col2.metric(area_label, selected_area["area_name"], f"{selected_area['dynamic_risk_score']:.2f} score")
col3.metric("Forecast rain there", f"{selected_area['forecast_rain_mm_tomorrow']:.1f} mm")
col4.metric("Risk tier", selected_area["risk_tier"])

st.divider()

TYPOLOGY_LABELS = {
    "planned_estate": "Planned estate (225mm blockwork, BS 5628 mortar designation i-iii)",
    "informal_older_stock": "Informal / older housing stock (150mm blockwork, BS 5628 mortar designation iv)",
}

with st.expander("Show the structural engineering"):
    typology = selected_area["construction_typology"]
    depth_today = selected_area["estimated_depth_m_today"]
    crack_depth = selected_area["critical_failure_depth_m"]
    margin = selected_area["margin_to_failure_m"]
    live_fos = selected_area["live_flexural_fos"]

    st.write(
        f"**{selected_area['area_name']}** is modeled as **{TYPOLOGY_LABELS.get(typology, typology)}**. "
        f"Treating the ground-floor wall as a cantilever fixed at its base, today's forecast-implied "
        f"floodwater depth of **{depth_today:.2f}m** is checked against the depth at which this wall's "
        f"bending strength (flexural strength fkx = {selected_area['wall_fkx_n_mm2']:.1f} N/mm2, "
        f"BS 5628 Part 1:1992) is exceeded and it is predicted to crack: **{crack_depth:.2f}m**."
    )

    tcol1, tcol2, tcol3 = st.columns(3)
    tcol1.metric("Estimated depth today", f"{depth_today:.2f} m")
    tcol2.metric("Wall cracks at", f"{crack_depth:.2f} m")
    tcol3.metric("Safety margin", f"{margin:.2f} m")

    if live_fos < 1:
        st.error(f"Factor of Safety = {live_fos:.2f} -- structural failure predicted at today's forecast depth.")
    else:
        st.success(f"Factor of Safety = {live_fos:.2f} -- wall holds at today's forecast depth.")
