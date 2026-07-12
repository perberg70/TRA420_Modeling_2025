# MANIFEST — Scenario Compass pull: ssp3-70 (GW7 ambition class)

**Pull date:** 2026-07-12
**Source:** IIASA Scenario Compass dashboard (SCI 2025), custom-panel download.
**Region:** Europe (R10), dashboard selector; no region column in exports (caveat (a) applies — R10 by procedure).
**Climate filter:** "Below 4.0°C" only. Machine-verified: all 52 pairs GW7, single class, pure. Peak = end-of-century warming range (MAGICC v7.5.3 median): 3.01–3.47 °C.
**Validation toggle:** OFF at download (default). **Net-zero CO2 year filter:** unset (deliberate, consistent across all sibling pulls).

## WARMING-SPAN CAVEAT — read before presenting as "SSP3-7.0"
Canonical SSP3-7.0 is assessed at ~3.6 °C end-century — WARMER than every member of this class (max 3.47 °C). "Below 4.0°C" was the closest available dashboard bin, but the ensemble systematically under-spans the canonical scenario's warming. Any UI narrative labeling this directory "SSP3-7.0" or "SSP3-7.0-like" must carry this discrepancy. Additionally, the directory contains ZERO SSP3-tagged scenario names (12 SSP2, 1 SSP5, 39 untagged) — the label is ambition-class shorthand only, doubly removed from SSP3 socioeconomics.

## Files (renamed from dashboard export names)
| File | Rows | Pairs |
|---|---|---|
| final_energy.csv | 811 | 52 |
| final_energy_electricity.csv | 754 | 49 |
| final_energy_res_com.csv | 796 | 51 |
| final_energy_res_com_electricity.csv | 796 | 51 |
Variables per convention; unit EJ/yr; 39 columns (ssp2-45 schema: net-zero/typology diagnostics absent — see ssp2-45 MANIFEST; scripts must not assume schema equivalence across sibling directories).

## Missing pairs (verified 2026-07-12)
- final_energy_electricity.csv lacks 3, all MESSAGEix-GLOBIOM 1.2: COVID-Shift-GreenPush, COVID-Shift-GreenPush_min_GDP, COVID-Shift-SelfReliance_min_GDP.
- Both res_com files lack 1: (REMIND-MAgPIE 1.7-3.0, COMMIT-NDCplus).
- Intersections MUST be keyed on (model, scenario) pairs.

## Vetting split
Dashboard displayed 27 = vetted "ok"; export contains all 52 (25 failed). Vetted-27 by model: TIAM-ECN 1.1 (5), WITCH 5.0 (4), POLES-JRC ENGAGE (3), REMIND-MAgPIE 2.1-4.2 (3), IMAGE 3.0 (2), IMAGE 3.2 (2), REMIND-MAgPIE 2.1-4.3 (2), REMIND-MAgPIE 3.2-4.6 (2), GCAM 5.3 (1), IMAGE 3.3 (1), MESSAGEix-GLOBIOM 1.1 (1), REMIND-MAgPIE 3.3-4.8 (1) — 12 frameworks; per-framework counts sum to 27 (exact dashboard match).

## Overlap with siblings (verified 2026-07-12)
ssp1-19 → 0; ssp1-26 → 0; ssp2-45 → 0. Classes are mutually exclusive bins.

## Year grids (heterogeneous, per-pair — SIX variants, incl. two distinct 19-pt grids)
| Pairs | Points | Grid | Models |
|---|---|---|---|
| 21 | 15 | 5-yearly 2010–2060, decadal 2070–2100 | MESSAGEix 1.1, REMIND(-MAgPIE) family |
| 11 | 19 | full 5-yearly 2010–2100 | GCAM 5.2/5.3/PR, IMAGE 3.2, WITCH 5.0 |
| 9 | 14 | as 15-pt, missing 2055 | COFFEE 1.1, IMAGE 3.0/3.3, POLES-JRC |
| 5 | 10 | decadal 2010–2100 | TIAM-ECN 1.1 |
| 3 | 18 | full 5-yearly 2015–2100, no 2010 | GEM-E3 V2021 |
| 3 | 19 | 15-pt grid + annual 2021–2024 | MESSAGEix-GLOBIOM 1.2 (COVID-Shift) |
Sum: 52 (all pairs). The two 19-pt variants have the SAME point count but DIFFERENT compositions — grid identity must be established from year lists, never point counts alone (a point-count summary lost 11 pairs during this pull's verification before assertion-checking caught it). Verify per-year pair coverage before ensemble statistics; no interpolation policy adopted by default. NOTE: 52-pair ensemble is the smallest sibling so far; thin-year collapse effects will be proportionally stronger.

## Provenance
All counts, purity, overlaps, grids machine-verified from export files 2026-07-12; figures assertion-checked before packaging. Dashboard screenshot on file: 12 frameworks, sum 27 vetted (exact match).
