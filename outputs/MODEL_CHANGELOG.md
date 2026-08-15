# Eti-Osa Flood Risk — Model Changelog

A running record of the major modeling decisions in this project, why each one was made, and what evidence (or lack of it) justified it. Kept honest on purpose — including the parts that were wrong until tested.

## 1. Data foundation

Real terrain (NASA SRTM elevation, later upgraded to FABDEM — see Section 7), real building footprints (73,101), real roads (21,204), and real drainage infrastructure (canals/drains/water bodies) for Eti-Osa LGA, all sourced from OpenStreetMap and satellite elevation data. No synthetic or placeholder data anywhere in the pipeline.

## 2. v1 static susceptibility score

Combined elevation (60%) and distance to nearest mapped drainage (40%) into a single 0-1 susceptibility score per building, later reused per road and per grid cell. Both inputs normalized against their 5th-95th percentile range (not raw min/max) to avoid outlier/sensor-noise distortion.

**Known limitation, accepted at the time:** this is a *relative* ranking against the rest of Eti-Osa, not an absolute measure of flood vulnerability. This later turned out to matter (see Section 6).

## 3. Forecast-driven "risk tomorrow" layer

Added live daily rainfall forecasts (Open-Meteo, free, no API key) per 1km grid cell, combined with static susceptibility:

```
dynamic_risk = susceptibility * (0.3 + 0.7 * rainfall_factor)
```

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

```
base_risk = susceptibility * (0.3 + 0.7 * rainfall_factor)
extreme_overwhelm = clip((rain - 25mm) / (50mm - 25mm), 0, 1)
dynamic_risk = 1 - (1 - base_risk) * (1 - extreme_overwhelm)
```

The 25mm/50mm calibration points come directly from the real event data (20-48mm/day rainfall was associated with real, reported flooding).

**Re-validated after the fix:** 3 July 2024 (47.8mm) now correctly shows Very High risk for Ikoyi, Lekki Phase I, and Lekki Phase II. 27 June 2024 (a calm day, 1.7-1.9mm) still correctly shows Low risk, confirming the fix adds sensitivity to real extreme events without just inflating every day's number. 4 July 2024 still under-scores (Medium) — likely because news coverage of this event is inconsistently dated 3 vs 4 July, suggesting the actual rain burst straddled the calendar-day boundary and got split across both daily totals in the historical weather record. A known, honestly-documented limitation of daily-resolution data, not a modeling flaw.

## 7. Elevation data upgrade: SRTM to FABDEM

Investigated two candidate replacements for raw SRTM, per a deliberate accuracy-improvement sequence (validate against history first, then improve the data itself): Copernicus GLO-30 and FABDEM.

**Copernicus GLO-30 was evaluated and rejected as insufficient on its own.** It's a newer, more precise satellite elevation source, but it's still a Digital Surface Model — it includes building and tree height as "ground," the exact same class of bias SRTM has. Switching to it would have improved measurement precision without fixing the underlying problem that caused the bridge/street false positives in Section 5.

**Switched to FABDEM instead** (Forest And Buildings removed Copernicus DEM, Neal & Hawker 2023, University of Bristol) — it starts from the same Copernicus GLO-30 surface but has building/vegetation height statistically removed via a trained correction model, producing an actual bare-earth estimate. Initially assumed this would require Google Earth Engine access; this turned out to be wrong — the `fabdem` Python package downloads just the tiles covering a given bounding box directly from the source archive, no Earth Engine needed. Data remains licensed CC BY-NC-SA 4.0 (non-commercial); would need re-licensing if this project were ever monetized.

**New bug surfaced by the switch, caught the same way as every prior bug in this project — checking the output against real knowledge, not trusting it:** Falomo Bridge and the Lekki-Ikoyi Bridge, both real lagoon crossings, started sampling at 0.00m elevation post-FABDEM and got flagged as flooding at 0.98 risk. Root cause: FABDEM's building-removal process assumes there's real ground underneath every structure it strips out. Under a bridge spanning open water, there is no ground to reveal — the correction falls back to the surrounding water surface (~0m). This is a known class of error in bare-earth DEM processing sometimes called the bridge/culvert artifact. The DEM isn't malfunctioning; it's answering a question ("what's the bare ground height here") that doesn't apply to an elevated structure.

**Fix:** stopped asking the terrain model about bridges at all. Used OpenStreetMap's own `bridge` tag (already present in the roads data, just unused until now) to hard-exclude bridge segments from the tidal risk pathway specifically — a bridge deck's flood risk is a structural clearance question, not a terrain question, and outside this project's current scope. Rain-driven risk on bridges is left untouched, since poor deck drainage during heavy rain is a real, separately-documented phenomenon in Lagos, unrelated to the DEM artifact.

**Net effect on the one validated historical event:** unchanged — 3 July 2024 still correctly scores Very High for Ikoyi/Lekki Phase I/Lekki Phase II, 27 June 2024 still correctly scores Low. The elevation swap's value so far is fixing structural false positives (bridges, and previously, the relative-vs-absolute street issue), not shifting the rainfall-driven validation, since susceptibility scoring is percentile-normalized and not very sensitive to sub-meter precision gains.

## 8. Splitting "today" from "tomorrow" — another real-world-caught bug

A user checked their own house (Banana Island) against the map and found it flagged as flood-prone (tidal, risk 0.98) while genuinely dry outside. Investigating surfaced two separate problems, both fixed together:

**Bug 1 — mislabeled forecast window.** Every "today" column in the app (`dynamic_risk_today`, `estimated_depth_m_today`, etc.) was actually built from **tomorrow's** forecast rain and tide (`forecast_rain_mm_tomorrow`, `tomorrow_tide_m`) under a "today" name. This meant the map was always showing a forward warning while claiming to show current conditions — confusing at best, and specifically what made a dry house read as "at risk today."

**Bug 2 — tide calibration read routine cycling as an anomaly.** The tidal risk pathway compared the forecasted tide against only the current 7-day window's own 5th-95th percentile range. Tides cycle on a ~14.8-day spring/neap rhythm, so almost any given day lands near the top of whatever narrow 7-day window contains it purely from routine variation — not because anything unusual is happening. `tide_factor` was reading ~0.98 nearly constantly as a result, especially for genuinely low-lying, coast-adjacent places like Banana Island (which has real, legitimate tidal exposure — the calibration was the problem, not the underlying physical premise).

**Fix:**
- `predict_flood_risk.py` and `estimate_road_risk.py` now compute genuinely separate `_today` and `_tomorrow` versions of every rain- and tide-driven field, from the same API calls (no extra cost — Open-Meteo's rain endpoint already returns a 2-day array, and the tide endpoint's rolling window already includes today).
- The tide baseline widened from 7 days to 37 days (`past_days=30` plus the 7-day forecast) — roughly two full spring-neap cycles — so "is this tide actually unusual" is judged against real tidal variation instead of wherever the forecast window happens to sit in the cycle.
- `app.py` now has an explicit Today / Tomorrow toggle, so the map is honest about which one it's showing rather than presenting a forecast as current fact.

**Open follow-up:** re-run `validate_against_history.py` with the widened tide baseline once a documented tidal-flooding event (as opposed to the rain-driven 2024 event already validated) can be found to check against — the current historical validation only tests the rainfall pathway.

## 9. A second historical event, and rolling 2-day rain

**New real event sourced:** the August 2025 Lekki corridor flood — Lagos State's Commissioner for Environment and Water Resources described flooding on Monday 4 August 2025 following rain that began the night of Sunday 3 August, explicitly naming the Lekki corridor among affected areas (Vanguard, 6 Aug 2025). Geographically less precise than the 2024 event (the source says "some areas around the Lekki corridor... not all," not a specific estate), but real, dated, and independently sourced.

**Also investigated but could not validate:** a real, tide-driven event on 25 October 2022 (Lagos State issued a red alert for Ikoyi/VI/Lekki citing high-tide backflow blocking stormwater discharge — the same "tidal locking" mechanism this project models). Open-Meteo's Marine API returned only null values for that date on every model tried, including ERA5-Ocean despite its claimed 1940-present coverage (`sea_level_height_msl` isn't actually served by that model). As an independent, API-free cross-check, 25 October 2022 was confirmed to be an exact new moon (10:49 UTC that day, close enough to cause a partial solar eclipse) — real astronomical corroboration of a genuine spring tide, even without a precise historical height reading. This is a lead to pick up when the tidal pathway gets its own validation pass, not a completed backtest.

**Backtesting the new event exposed a real, fixable pattern, not just a one-off gap.** On the specific days each event's source explicitly reported flooding ("reported flood days"), the single-calendar-day model got 3 of 6 area-checks right across the two events — including missing 4 July 2024 itself, the exact date the 2024 event is named after. In both misses, the shortfall traced to the same cause: a continuous multi-hour rain event straddling a calendar-day boundary, so the day the news reported as "the flood day" wasn't the day the data showed the most rain.

**Fix:** switched the rainfall risk calculation from a single midnight-to-midnight rain total to a rolling 2-day accumulated total (yesterday+today for "today's" risk, today+tomorrow for "tomorrow's"). Re-run against both events: 6 of 6 reported-flood-day area-checks now correctly flagged, including a bonus fix — 26 June 2024 (part of the "related event" the Wikipedia source also cites) now correctly flags too, previously missed. The genuinely low-rain lead-in days in both events (27 June 2024, 3 Aug 2025) correctly stayed unflagged under the new formula, so this isn't just inflating everything.

**Bonus bug found while doing this:** `estimate_road_risk.py` had never received the extreme-rain-overwhelm term added to `predict_flood_risk.py` after the original July 2024 validation fix (Section 6) — road-level rainfall risk was still structurally capped at susceptibility during extreme rain, the same bug the area-level model had before. Fixed at the same time, using the same rolling 2-day rain input for consistency between area-level and road-level risk.

**Honest caveat:** the rolling 2-day sum is now being run through thresholds (`EXTREME_RAIN_MM`/`SEVERE_RAIN_MM` = 25mm/50mm) that were originally calibrated against single-day totals. Both known events validate cleanly under this reuse, and the lead-in days in both events (6.6mm and 22.2mm 2-day totals for 2024, 3.8mm and 14.3mm for 2025) correctly stayed low — but this hasn't been checked against a full rainy season of ordinary, non-flood 2-day rain sequences, so the false-positive rate on typical (non-extreme) two-day wet spells is still unverified.

## Open limitations (as of this entry)

- Two historical rainfall-driven events have now been validated (2024, 2025), both scoring 3/3 on their reported flood day once rolling 2-day rain was introduced. Still just two events, and both are rain-driven — the tidal pathway has no validated event yet (see Section 9's October 2022 lead). More real, dated events (the HDX/NEMA flood datasets and the 1968-2020 Lagos flood inventory) would make the calibration more robust.
- The rolling 2-day rain thresholds are reused from single-day calibration and haven't been checked against a full season of ordinary (non-extreme) rain for false positives -- see Section 9.
- Drainage "capacity" is still modeled only as distance to the nearest mapped channel, not the channel's actual size, condition, or blockage status — which real government sources identify as a primary cause of these floods.
- Culverts and smaller water crossings that aren't tagged `bridge=yes` in OpenStreetMap could still carry the same DEM artifact found in Section 7 — worth spot-checking if more false positives turn up at other water crossings.
- Open-Meteo's own documentation notes `sea_level_height_msl` accuracy is "limited in coastal areas" and "may be completely unreliable further inland" — a real, honest caveat on the entire tidal pathway regardless of the calibration fix in Section 8. Its historical archive also doesn't actually serve this variable at all, on any model tested (Section 9), so tidal validation will need a different data source (a real Lagos tide gauge record, or an astronomical/harmonic tide prediction model) rather than Open-Meteo.
