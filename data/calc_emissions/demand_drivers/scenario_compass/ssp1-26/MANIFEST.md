# MANIFEST — Scenario Compass pull: ssp1-26 (GW3 ambition class)

**Pull date:** 2026-07-12
**Source:** IIASA Scenario Compass dashboard (SCI 2025), custom-panel download via download.scenariocompass.org
**Region:** Europe (R10), set in dashboard region selector. No region column in exports (schema as ssp1-19 generation; caveat (a) of the reconciliation memo applies — R10 by procedure, not machine-verifiable from files).
**Climate filter:** "Likely below 2°C" only. Machine-verified: all 280 pairs carry `meta_Climate Category|SCI 2025 [Tier I]` = GW3 (single class, pure). Peak warming (MAGICC v7.5.3 median) range: 1.71–1.87 °C. NOTE: this is an upgrade over the ssp1-19 pull, where class/region purity could not be machine-verified.
**Validation toggle:** "Validation against historical and current trends" was OFF at download (dashboard default). The toggle state of the ssp1-19 pull is unknown (GAP, retroactively unverifiable).

## Directory-name caveat — read before using
`ssp1-26` is shorthand for the ~2.6 W/m² / GW3 (peak likely below 2 °C) ambition class, chosen as the sibling label to `ssp1-19`. It does NOT mean SSP1 socioeconomics: only 25 of 280 scenario names carry any SSP tag (22 SSP2, 3 SSP5; 255 untagged). Consistent with the project finding that electrification in IAM ensembles is driven by climate-forcing targets, not SSP tags.

## Files (renamed from dashboard export names)
| File | Rows | (model, scenario) pairs |
|---|---|---|
| final_energy.csv | 4442 | 280 |
| final_energy_electricity.csv | 4347 | 275 |
| final_energy_res_com.csv | 4412 | 278 |
| final_energy_res_com_electricity.csv | 4412 | 278 |

Variable per file: Final Energy; Final Energy|Electricity; Final Energy|Residential and Commercial; Final Energy|Residential and Commercial|Electricity. Unit: EJ/yr throughout. 43 columns incl. metadata (climate assessment, vetting, feasibility/sustainability concerns, AR6 legacy mapping, ensemble weights).

## Missing pairs (verified 2026-07-12)
- `final_energy_electricity.csv` lacks 5 pairs, all MESSAGEix-GLOBIOM 1.2: COVID-Shift-{GreenPush, NoPolicyNoCOVID, Restore, SelfReliance, SmartUse}_1000 (analog of the `_550` gap in ssp1-19).
- Both res_com files lack 2 pairs, both REMIND-MAgPIE 1.7-3.0: COMMIT-2°C-2030, COMMIT-Bridge (new pattern, not present in ssp1-19).
- Intersections MUST be keyed on (model, scenario) pairs, never scenario name alone.

## Vetting split (dashboard-vs-export, same structure as ssp1-19)
Dashboard displayed 54 scenarios = the `meta_Vetting|SCI 2025` = "ok" subset. Export contains all 280 GW3 pairs (226 "failed"). Vetted-54 composition by model: WITCH 5.0 (14), POLES-JRC ENGAGE (10), IMAGE 3.2 (7), IMAGE 3.0 (4), REMIND-MAgPIE 3.2.0-4.8.0 (4), TIAM-ECN 1.1 (4), IMAGE 3.3 (3), GCAM 6.0 NGFS (2), MESSAGEix-GLOBIOM 2.0-M-R12-NGFS (2), REMIND-MAgPIE 3.3-4.8 (2), REMIND-MAgPIE 2.1-4.2 (1), GCAM 5.3 (1) — 12 frameworks. Unlike ssp1-19 (vetted-7, all IMAGE 3.2), the vetted subset here is multi-framework; the "single-model central estimate" weakness of curation Option A does not apply at this ambition level. Erik's A/B/C curation decision governs use.
COINCIDENCE FLAG: the vetted set again contains exactly 7 IMAGE 3.2 pairs — these are DIFFERENT scenarios than ssp1-19's vetted 7. Do not conflate.

## Overlap with ssp1-19 (verified 2026-07-12)
Intersection of (model, scenario) pairs with `../ssp1-19/final_energy.csv` (103 pairs): **0**. SCI climate categories are mutually exclusive bins, not cumulative; no scenario appears in both directories.

## Year grids (heterogeneous, per-pair — same discipline as ssp1-19 caveat (g))
final_energy.csv, 6 grid variants:
| Pairs | Points | Grid |
|---|---|---|
| 126 | 15 | 5-yearly 2010–2060, decadal 2070–2100 |
| 67 | 14 | as 15-pt, additionally missing one mid-century point |
| 59 | 19 | full 5-yearly 2010–2100 |
| 21 | 18 | full 5-yearly 2015–2100, no 2010 |
| 5 | 19 | 15-pt grid + annual 2021–2024 (MESSAGEix COVID-Shift) |
| 2 | 10 | decadal 2010–2100 (new variant, not seen in ssp1-19) |
NOTE (added 2026-07-12, after ssp3-70 verification): the two 19-point rows above are DISTINCT grids with equal point counts — full 5-yearly vs. 15-pt+annual-2021–2024. Grid identity must be established from year lists, never point counts alone; a point-count summary silently merged two 19-pt variants during the ssp3-70 pull before assertion-checking caught it. Counts in this table were re-verified against the committed files 2026-07-12 and are correct.
Year-indexed ensemble statistics draw on different pair subsets per year; verify per-year pair coverage before computing medians. No interpolation policy is adopted by default.

## Provenance of this MANIFEST
Counts, class purity, missing-pair lists, overlap check, and grid table machine-verified from the export files on 2026-07-12 (session: ssp1-26 pull verification). Dashboard screenshot on file showed 54 selected / 12 frameworks; per-framework counts matched the data except IMAGE 3.0 (screenshot 3 vs. data 4) — display discrepancy, export authoritative.
