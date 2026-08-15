"""
Quick connectivity/coverage check (throwaway, not part of the main
pipeline): does Open-Meteo's Marine API actually return real historical
tide data for a date back in October 2022, or does start_date/end_date
only work within a recent rolling window?

Testing against 24-26 October 2022 specifically -- Lagos State issued a
red alert on 25 Oct 2022 for Ikoyi/VI/Lekki due to high lagoon tide
backflow blocking stormwater discharge (a real, tide-driven event, not
rain-driven), which would be the first real test of this project's
tidal risk pathway if the data exists that far back.
"""
import requests

url = (
    "https://marine-api.open-meteo.com/v1/marine"
    "?latitude=6.4241&longitude=3.4219"
    "&hourly=sea_level_height_msl"
    "&start_date=2022-10-24&end_date=2022-10-26"
    "&timezone=Africa%2FLagos"
    "&models=era5_ocean"
)

resp = requests.get(url, timeout=30)
print(f"Status code: {resp.status_code}")
print(resp.text[:3000])
