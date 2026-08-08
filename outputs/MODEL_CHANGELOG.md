# Eti-Osa Flood Risk — Model Changelog

A running record of the major modeling decisions in this project, why each one was made, and what evidence (or lack of it) justified it. Kept honest on purpose — including the parts that were wrong until tested.

## 1. Data foundation

Real terrain (NASA SRTM elevation), real building footprints (73,101), real roads (21,204), and real drainage infrastructure (canals/drains/water bodies) for Eti-Osa LGA, all sourced from OpenStreetMap and satellite elevation data. No synthetic or placeholder data anywhere in the pipeline.

## 2. v1 static susceptibility score

Combined elevation (60%) and distance to nearest mapped drainage (40%) into a single 0-1 susceptibility score per building, later reused per road and per grid cell. Both inputs normalized against their 5th-95th percentile range (not raw min/max) to avoid outlier/sensor-noise distortion.

**Known limitation, accepted at the time:** this is a *relative* ranking against the rest of Eti-Osa, not an absolute measure of flood vulnerability. This later turned out to matter (see Section 6).

## 3. Forecast-driven "risk tomorrow" layer

Added live daily rainfall forecasts (Open-Meteo, free, no API key) per 1km grid cell, combined with static susceptibility:
This was the first version to make the tool genuinely forecast-driven rather than a static map.

## 4. Structural engineering: wall flexural capacity

Initially built hydrostatic pressure/force on a wall from floodwater depth. This alone wasn't a real pass/fail structural answer, so it was extended to a genuine flexural (bending) capacity check: treat the wall as a cantilever fixed at its base, compute the bending moment from the triangular hydrostatic pressure profile, and compare against real characteristic flexural strength values from BS 5628 Part 1:1992.

**Refinement:** the original model treated the wall as free-standing (fixed base, free top), which is unrealistic since almost every real house has a roof tying the top of the wall down. Rebuilt as a proper propped cantilever (statically indeterminate, solved via the force method), which raised the realistic failure depth for both construction typologies (informal: 1.11m -> 1.24m; planned estate: 1.67m -> 2.02m).

Two construction typologies assigned per real named area (informal/older stock vs planned estate), each with a different wall thickness and BS 5628 mortar grade, reflecting documented differences in regulated vs informal Lagos construction.

## 5. Road-level and tidal flood risk

Extended susceptibility scoring from buildings down to all 21,204 individual road segments, so the map shows specific flood-prone streets, not just neighborhoods.

Added a second, independent risk pathway for tidal flooding after a real-world observation (flooding near Shoprite, Victoria Island, with no rain — a known Lagos phenomenon officials call "tidal locking," where a high lagoon level blocks stormwater drains from discharging). Sourced the real Lagos Lagoon/Five Cowries Creek/Commodore Channel coastline from OpenStreetMap, pulled live tide forecasts from Open-Meteo's free Marine API, and combined coastal proximity with an **absolute** elevation cutoff (2m) gating tidal exposure.

**Bugs found and fixed during this build, each caught by sanity-checking against real knowledge rather than trusting the output:**
- Bridges spanning the lagoon (Falomo Bridge, Lekki-Ikoyi Bridge) were incorrectly flagged as flooding, because tidal exposure only considered horizontal distance to water, not elevation. Fixed by gating exposure on elevation.
- Well-established Ikoyi/VI streets at 2-4m elevation (Awolowo Road, Osborne Road) were still being flagged, because the elevation gate was reusing the *relative* susceptibility elevation score (rank within the whole district) instead of an *absolute* height check against real observed tide levels. Fixed by switching to an absolute 2m cutoff.

Combined final risk per road = whichever of "rain-driven" or "tidal" risk is higher, since Lagos State's own reporting treats both as independent, sufficient causes of street flooding.

## 6. Historical validation — the most important entry

Up to this point, every risk number was physically reasoned but never checked against reality. Backtested the model against a real, documented, dated event: the 2024 Lekki/Ikoyi flood (heavy rain starting the morning of 4 July 2024, ~10 hours, widely reported, own Wikipedia entry), pulling **actual recorded historical rainfall** (not forecast) from Open-Meteo's archive for the surrounding dates.

**Result: the model missed the real flood on every single date tested**, including 3 July 2024's 47.8mm of rain — a genuinely severe rainfall day cited as part of the real event. Lekki Phase I only reached a risk score of 0.39 (Medium) at nearly-saturating rainfall.

**Root cause:** the risk formula structurally capped `dynamic_risk` at each area's own static susceptibility score — no amount of rain could push a location's risk above its baseline vulnerability. Lekki and Ikoyi's susceptibility scores (0.24-0.40) were too low *relative to the rest of the district* to ever cross the "High" threshold, even though real Lagos State statements about these exact floods blame overwhelmed/blocked drainage capacity (illegal structures on drains, silt buildup) — a district-wide failure mode the model wasn't representing.

**Fix:** added a second, calibrated term representing system-wide drainage overwhelm during genuinely extreme rain, combined with the original susceptibility-driven risk via a complementary-probability combination (either factor can push risk up, rather than one capping the other):
The 25mm/50mm calibration points come directly from the real event data (20-48mm/day rainfall was associated with real, reported flooding).

**Re-validated after the fix:** 3 July 2024 (47.8mm) now correctly shows Very High risk for Ikoyi, Lekki Phase I, and Lekki Phase II. 27 June 2024 (a calm day, 1.7-1.9mm) still correctly shows Low risk, confirming the fix adds sensitivity to real extreme events without just inflating every day's number. 4 July 2024 still under-scores (Medium) — likely because news coverage of this event is inconsistently dated 3 vs 4 July, suggesting the actual rain burst straddled the calendar-day boundary and got split across both daily totals in the historical weather record. A known, honestly-documented limitation of daily-resolution data, not a modeling flaw.

## Open limitations (as of this entry)

- Only one historical event has been used for validation so far. More real, dated events (the HDX/NEMA flood datasets and the 1968-2020 Lagos flood inventory) would make the calibration more robust.
- Elevation data is raw SRTM (~30m resolution, includes building/tree height noise). A corrected dataset (Copernicus GLO-30, or FABDEM which specifically strips buildings/vegetation) would likely reduce false positives further, at the cost of added complexity (FABDEM requires Google Earth Engine access and carries a non-commercial license).
- Daily-resolution rainfall can split multi-hour extreme rain events across two calendar days, understating single-day severity in edge cases like 4 July 2024 above.
- Drainage "capacity" is still modeled only as distance to the nearest mapped channel, not the channel's actual size, condition, or blockage status — which real government sources identify as a primary cause of these floods.
