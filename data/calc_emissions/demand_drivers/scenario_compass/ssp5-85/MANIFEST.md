# MANIFEST — Scenario Compass pull: ssp5-85 (GW8 ambition class)

**Pull date:** 2026-07-12
**Source:** IIASA Scenario Compass dashboard (SCI 2025), custom-panel download.
**Region:** Europe (R10), dashboard selector; no region column in this export generation (caveat (a) applies). NOTE: the archived `../../gw8_nopolicy/` files carry the only machine-readable `region` column ("Europe (R10)") across all pulls — retained partly for that evidentiary value.
**Climate filter:** "Above 4.0°C" only. Machine-verified: all 46 pairs GW8, single class, pure. Peak = end-of-century warming (MAGICC v7.5.3 median): 3.51–4.68 °C.
**Validation toggle:** OFF at download (default). **Net-zero CO2 year filter:** unset (consistent across all sibling pulls).

## Label caveats
(1) The dashboard label "Above 4.0°C" overstates the class floor: 3.51 °C minimum in practice (GW8 = highest/no-policy class, not strictly >4.0). (2) Canonical SSP5-8.5 (~4.4 °C end-century) sits WELL INSIDE the ensemble range — better canonical coverage than ssp3-70 (see that MANIFEST's warming-span caveat). (3) `ssp5-85` is ambition-class shorthand, not SSP5 socioeconomics: 7 SSP2-tagged, 6 SSP5-tagged, 33 untagged scenario names.

## Relationship to gw8_nopolicy/ (RESOLUTION of open item, verified 2026-07-12)
The abandoned 30-pair `gw8_nopolicy/` set is a STRICT SUBSET of this pull: all 30 (model, scenario) pairs reappear here; 16 pairs are new; 0 old-only. This directory supersedes gw8_nopolicy/ as the active GW8 source. gw8_nopolicy/ remains archived (not deleted) for: (a) its machine-readable region column — sole machine evidence for R10 across export generations; (b) its derived family-median and central-band files documenting the original low-electrification abandonment analysis. Schemas differ entirely (old: region/model/model_family/scenario/variable long format; new: 39-column metadata format) — scripts must not assume equivalence.

## Files (renamed from dashboard export names)
| File | Rows | Pairs |
|---|---|---|
| final_energy.csv | 752 | 46 |
| final_energy_electricity.csv | 638 | 40 |
| final_energy_res_com.csv | 722 | 44 |
| final_energy_res_com_electricity.csv | 708 | 43 |
Variables per convention; unit EJ/yr; 39 columns (ssp2-45/ssp3-70 schema, net-zero diagnostics absent).

## Missing pairs (verified 2026-07-12) — NOTE: first pull where the two res_com files differ from EACH OTHER
- final_energy_electricity.csv lacks 6, all MESSAGEix-GLOBIOM 1.2 COVID-Shift: GreenPush_max_GDP, NoPolicyNoCOVID, Restore, SelfReliance, SelfReliance_max_GDP, SmartUse.
- final_energy_res_com.csv lacks 2: (REMIND-MAgPIE 1.7-3.0, COMMIT-Baseline), (REMIND-MAgPIE 1.7-3.0, COMMIT-Current-Policies).
- final_energy_res_com_electricity.csv lacks 3: the 2 above + (AIM/Hub-Global 2.4, GEO7-Current Trends).
- Intersections MUST be keyed on (model, scenario) pairs, per file.

## Vetting split
Dashboard displayed 29 = vetted "ok"; export contains all 46 (17 failed). Vetted-29 by model: MESSAGEix-GLOBIOM 1.2 (6), REMIND 2.1 (6), REMIND-MAgPIE 1.7-3.0 (5), IMAGE 3.2 (3), IMAGE 3.0 (2), REMIND-Buildings 2.0 (2), MESSAGEix-GLOBIOM 1.1 (1), POLES-JRC ENGAGE (1), REMIND-MAgPIE 2.0-4.1 (1), REMIND-MAgPIE 2.1-4.2 (1), WITCH 5.0 (1) — 11 frameworks; sum 29 (exact dashboard match).

## Overlap with siblings (verified 2026-07-12)
ssp1-19 → 0; ssp1-26 → 0; ssp2-45 → 0; ssp3-70 → 0. Classes are mutually exclusive bins.

## Year grids (heterogeneous, per-pair — FOUR variants, incl. two distinct 19-pt grids; identity from year lists, never point counts)
| Pairs | Points | Grid | Models |
|---|---|---|---|
| 23 | 15 | 5-yearly 2010–2060, decadal 2070–2100 | MESSAGEix 1.1, REMIND family (incl. Buildings, Transport) |
| 11 | 19 | full 5-yearly 2010–2100 | AIM/CGE, GCAM 5.3/PR, IMAGE 3.2, WITCH 4.6/5.0 |
| 6 | 14 | as 15-pt, missing 2055 | AIM/Hub-Global 2.4, IMAGE 3.0, POLES-JRC |
| 6 | 19 | 15-pt grid + annual 2021–2024 | MESSAGEix-GLOBIOM 1.2 (COVID-Shift) |
Sum: 46 (all pairs). No 10-pt or 18-pt variants in this class (TIAM-ECN and GEM-E3 absent). Verify per-year pair coverage before ensemble statistics; no interpolation policy adopted by default.

## Provenance
All counts, purity, subset relation, overlaps, and grids machine-verified from export files 2026-07-12; figures assertion-checked (incl. grid-variant sum = 46) before packaging. Dashboard screenshot on file: 11 frameworks, sum 29 vetted (exact match).
