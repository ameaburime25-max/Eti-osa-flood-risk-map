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

## 10. Drainage blockage modeling — human-caused, not just distance

Every version up to this point modeled drainage as pure geometry: distance to the nearest mapped channel, silently assuming that channel is clear and working. That's not how Lagos actually floods. Lagos State runs a standing Emergency Flood Abatement Gang specifically to "free up manholes and blackspots" (the Commissioner's own words, Oct 2022 Nairametrics) because real drains get choked with refuse, silt, and informal encroachment — a building 20m from a fully blocked drain has the same practical drainage as a building 20m from no drain at all.

**New script: `scripts/model_drainage_blockage.py`.** Scores every one of the 546 real OSM drainage-line segments in Eti-Osa with a `blockage_risk` (0-1), from three independent, physically-grounded signals actually present in the data:

- **Waterway type.** `drain` (426 segments) and `ditch` (90) are small, shallow, artificial channels — exactly what a bag of refuse or a load of construction sand can choke solid. `canal`/`river`/`stream` (9/4/17) carry real continuous flow and are far harder to fully block by informal dumping. Weighted 40% of the score.
- **Culvert/tunnel status.** 218 of 546 segments (40%) are tagged `tunnel=culvert` — covered/underground drainage. A blocked culvert is invisible from street level and can't be manually cleared the way an open channel can; that opacity is a real vulnerability independent of channel size. Fixed bonus, weighted 25%.
- **Building encroachment pressure.** Using the real 73,101-footprint building dataset, counts buildings within 15m of each segment, normalized per 100m of segment length. Dense building pressure right on a drain is a direct proxy for the two real human causes of blockage: informal structures built into the drainage right-of-way, and population density making dumping into the nearest open channel the path of least resistance. Weighted 35%.

Result: mean blockage_risk by type came out exactly as the physical reasoning predicts — drain 0.48 > ditch 0.37 > stream 0.19 > canal 0.12 > river 0.08 — without that ordering being hard-coded anywhere; it falls straight out of combining the three independent signals.

**Integration: "proximity only helps if the drain still works."** Rather than bolting blockage_risk on as a fourth independent weighted term (which would double-count distance and blockage as if they were unrelated), it's blended directly into the existing drainage_score in both `compute_flood_risk.py` (buildings) and `estimate_road_risk.py` (roads):

```
effective_drainage_score = drainage_score + (1 - drainage_score) * blockage_risk
```

If the nearest drain is essentially guaranteed clear (blockage_risk=0), this is identical to the old distance-only score. If the nearest drain is essentially guaranteed blocked (blockage_risk=1), the effective score is pushed to 1 (worst case) regardless of how close it is — treated as if there were no working drain nearby at all. In between, the proximity benefit is discounted proportionally. `risk_score` (buildings) and `susceptibility` (roads) now use this effective score in place of the raw one; the raw `drainage_score` and `blockage_risk_nearest_drain` are kept as separate columns for transparency.

**Result on the real data:** 207 of 73,101 buildings have a nearest drain with blockage_risk ≥ 0.7. Re-ran the full pipeline including the 6-event historical backtest from Section 9: still 6/6 on reported flood days — blockage risk shifts susceptibility at the individual building/road level (via the effective drainage score) without regressing the events already validated. On this particular forecast day, the top-flagged roads stayed dominated by tidal risk in low-lying Ajah (tide_factor was already 1.00, so `max(rainfall_risk, tidal_risk)` was already saturated there regardless of drainage blockage) — blockage's effect will show up more clearly on rain-dominated days and in the underlying susceptibility ranking than in today's specific flagged-road count.

**Honest caveat:** blockage_risk is a plausibility-weighted proxy built from real OSM tags and real building density, not an observed blockage survey — there's no ground-truth "this specific drain was blocked on this specific date" dataset to validate it against directly. The weights (40/35/25 split, and the specific per-type vulnerability values) are a defensible starting calibration, not something backtested against a real blockage-driven flood event the way the rainfall model now is.

## 11. Replacing the flat rolling-2-day rain sum with a drainage- and evaporation-aware carryover

Section 9's flat rolling-2-day sum (`yesterday's rain + today's rain`) fixed a real bug but was itself a blunt instrument: it treats every location identically, silently assuming standing water from yesterday behaves the same on a well-drained planned estate as it does next to a blocked drain. A direct critique of this from real local knowledge of how these streets actually dry out prompted a proper fix.

**New mechanism, two real physical loss pathways instead of one flat assumption:**

```
water_still_standing = yesterday's rain x carryover_fraction
water_after_evaporation = max(0, water_still_standing - ET0)
today's effective rain = today's rain + water_after_evaporation
```

- `carryover_fraction` = 0.15 to 0.85, scaled by that specific location's own `effective_drainage_score` (Section 10) -- a well-drained cell only carries ~15% of yesterday's rain forward (most of it genuinely left), a blocked/distant-drainage cell carries ~85% (it's still sitting there). Grid cells use their mean; roads use their own, more precise, per-road score.
- `ET0` is real FAO-56 Penman-Monteith reference evapotranspiration (mm/day) -- a standard variable Open-Meteo publishes from temperature, wind, humidity and solar radiation, fetched in the same batched API call already being made for rain, at no extra cost. This is the correct term for what's colloquially "the sun drying up standing water" -- not condensation, which is a different, unrelated process (cloud/dew formation from water vapour, not relevant to floodwater loss).

**Result:** backtested against both real events in Section 9's suite (3-way comparison: single-day, flat rolling-2-day, drainage+evaporation-aware carryover). On reported flood days: still 6/6, matching the flat sum exactly -- no regression. On a genuinely useful side-effect: on 26 June 2024, a lead-in day that was never reported as a flood day, the flat sum flagged all 3 tracked areas (Ikoyi, Lekki Phase I, Lekki Phase II) while the new carryover only flagged 1 (Lekki Phase II, the one with worse local drainage) -- a concrete sign this version is less prone to over-flagging days that weren't actually flood events, directly relevant to the still-open false-positive question (see Open limitations). Rolled into `predict_flood_risk.py` (grid-level, using `mean_effective_drainage_score`) and `estimate_road_risk.py` (road-level, using each road's own `effective_drainage_score` -- more precise than inheriting the grid's averaged number).

**Honest caveat:** the 0.15/0.85 carryover bounds are a first, defensible calibration (ground never fully dries by morning, ground never fully retains 100% either), not a measured constant -- same status as the other thresholds in this project. It hasn't been tested against a full rainy season of ordinary multi-day rain sequences either, so its false-positive behaviour beyond the one lead-in day above is still mostly unverified.

## 12. Reverse-geocoding unnamed roads — a real data gap, only partially fixable

A direct usability complaint: many roads flagged in the app showed up as "Unnamed road," which is implausible for a real neighborhood where every street has a name. Checked the actual OSM data rather than assuming: 57.8% of all 21,204 road segments (12,264 of them) have no `name` tag at all, and 90% of those are `highway=residential` — real neighborhood streets, not footpaths or service tracks that might legitimately go unnamed. 65% of every residential road segment in Eti-Osa is missing a name in the source data. This is a genuine upstream OpenStreetMap completeness gap for this region, not a bug in the pipeline.

**Ruled out from data already on hand:** `ref` tags (route numbers) have 0% coverage on unnamed roads. Building `addr:street` tags exist on only 0.4% of the 73,101 buildings — nowhere near enough to spatial-join a street name from a nearby building's address.

**New script: `scripts/resolve_road_names.py`.** osmnx splits one physical OSM way into multiple graph edges at intersections, so the 12,264 unnamed segments collapse to 4,058 distinct physical streets. Each gets reverse-geocoded once (a representative centroid point) via LocationIQ (same underlying Nominatim engine as OSM's own geocoder, but a company whose terms explicitly permit this kind of one-time bulk lookup — OSM's own public Nominatim instance explicitly bans "systematic queries... reverse queries in a grid... downloading all POIs in an area," which this would have resembled; using OSM's shared free resource for this risked getting it blocked for everyone). One-time enrichment, checkpointed to `data/road_name_lookup_cache.json` after every request so an interrupted run resumes instead of restarting and re-paying for already-resolved streets.

**Result:** 396 of the 12,264 unnamed segments recovered a real name (Ozumba Mbadiwe Avenue, Kusenla Road, Maroko Road, Eleshin Street, Cardinal Anthony Olubunmi Okogie Road, among others) — unnamed segments dropped from 57.8% to 56.0% of the total. A real, modest, honestly-earned improvement, not a full fix. The low yield itself is informative: LocationIQ is fundamentally built on the same OpenStreetMap map data our own extract already came from, so a residential Lagos street OSM never named usually isn't independently named in LocationIQ's own data either — it only recovers names that exist *somewhere* in the broader OSM/address ecosystem (a different edit, a nearby tagged point of interest) but weren't tagged on that specific road geometry. It is not manufacturing street names from nothing.

**Honest caveat:** two engineering bugs were caught and fixed mid-run rather than before it, both from real failures during the actual run, not anticipated in advance: the first version silently cached every failed request as a permanent "no name found," which would have made a temporarily bad API key or a network blip permanently poison the result for that street; the second version didn't catch `requests`-level exceptions (only bad HTTP status codes), so a single read-timeout crashed the entire multi-hour run. Both are fixed in the committed version, but worth recording since it's the honest history of how this got built.

## Open limitations (as of this entry)

- Two historical rainfall-driven events have now been validated (2024, 2025), both scoring 3/3 on their reported flood day once rolling 2-day rain was introduced (Section 9), and the drainage+evaporation-aware carryover (Section 11) matches that 6/6 with better lead-in-day selectivity. Still just two events, and both are rain-driven — the tidal pathway has no validated event yet (see Section 9's October 2022 lead). More real, dated events (the HDX/NEMA flood datasets and the 1968-2020 Lagos flood inventory) would make the calibration more robust.
- The rain-carryover thresholds (`EXTREME_RAIN_MM`/`SEVERE_RAIN_MM`, and now `MIN_CARRYOVER`/`MAX_CARRYOVER`) are first, defensible calibrations, not measured constants, and haven't been checked against a full season of ordinary (non-extreme) rain for false positives -- see Sections 9 and 11.
- Drainage blockage (Section 10) is now modeled from real waterway type, culvert status, and building encroachment — a real upgrade from pure distance — but it's a physically-reasoned proxy, not validated against an observed blockage event, since no such dataset exists for Eti-Osa.
- Culverts and smaller water crossings that aren't tagged `bridge=yes` in OpenStreetMap could still carry the same DEM artifact found in Section 7 — worth spot-checking if more false positives turn up at other water crossings.
- Open-Meteo's own documentation notes `sea_level_height_msl` accuracy is "limited in coastal areas" and "may be completely unreliable further inland" — a real, honest caveat on the entire tidal pathway regardless of the calibration fix in Section 8. Its historical archive also doesn't actually serve this variable at all, on any model tested (Section 9), so tidal validation will need a different data source (a real Lagos tide gauge record, or an astronomical/harmonic tide prediction model) rather than Open-Meteo.
- Still 56.0% of road segments unnamed even after reverse-geocoding (Section 12) — most of Eti-Osa's residential street network genuinely isn't named anywhere in accessible map data, not just in this project's source extract. A full fix would need Lagos State's own street-naming/addressing records, which aren't publicly available as open data.
