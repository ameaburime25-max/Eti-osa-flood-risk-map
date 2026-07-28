import streamlit as st
import folium
from streamlit_folium import st_folium
import geopandas as gpd

st.title("Eti-Osa Flood Risk")
st.write("This is going to become a live flood risk map for Eti-Osa, Lagos.")

risk_grid = gpd.read_file("data/etiosa_dynamic_risk_grid.geojson")

tier_colors = {
    "Low": "green",
    "Medium": "gold",
    "High": "orange",
    "Very High": "red",
}

def style_function(feature):
    tier = feature["properties"]["risk_tier"]
    return {
        "fillColor": tier_colors.get(tier, "gray"),
        "color": "black",
        "weight": 0.5,
        "fillOpacity": 0.6,
    }

m = folium.Map(location=[6.46, 3.53], zoom_start=12)
folium.GeoJson(
    risk_grid.to_json(),
    style_function=style_function,
    tooltip=folium.GeoJsonTooltip(
        fields=["area_name", "dynamic_risk_score", "forecast_rain_mm_tomorrow", "risk_tier"],
        aliases=["Area:", "Risk score:", "Forecast rain (mm):", "Risk tier:"],
    ),
).add_to(m)
st_folium(m, width=700, height=500)
