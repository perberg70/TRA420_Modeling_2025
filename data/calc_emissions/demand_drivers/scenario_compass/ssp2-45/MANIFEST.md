# MANIFEST — Scenario Compass pull: ssp2-45 (GW6 ambition class)

**Pull date:** 2026-07-12
**Source:** IIASA Scenario Compass dashboard (SCI 2025), custom-panel download.
**Region:** Europe (R10), dashboard selector; no region column in exports (caveat (a) applies — R10 by procedure).
**Climate filter:** "Below 3.0°C" only. Machine-verified: all 91 pairs GW6 (`meta_Climate Category|SCI 2025 [Tier I]`), single class, pure. Peak = end-of-century warming range (MAGICC v7.5.3 median): 2.51–2.97 °C, bracketing SSP2-4.5's ~2.7 °C.
**Validation toggle:** OFF at download (default). **Net-zero CO2 year filter:** unset (deliberate; curation happens downstream per supervisor decision, and cross-directory comparability requires identical filter policy).

## Directory-name caveat
`ssp2-45` is shorthand for the ~4.5 W/m² / GW6 (below 3.0 °C) ambition class, sibling label to `ssp1-19`/`ssp1-26`. NOT SSP2 socioeconomics: only 9 of 91 scenario names carry any SSP tag (5 SSP1, 4 SSP2; 82 untagged).

## Files (renamed from dashboard export names)
All four files: 1403 rows × 39 columns, identical 91 (model, scenario) pairs — no missing-pair asymmetry (unlike ssp1-19 and ssp1-26). Variables: Final Energy; |Electricity; |Residential and Commercial; |Residential and Commercial|Electricity. Unit: EJ/yr.

## SCHEMA DEVIATION vs ssp1-26 (39 vs 43 columns)
Four columns absent, all net-zero/typology diagnostics: `meta_Emissions Diagnostics|Year of Net Zero|CO2`, `|Year of Net Zero|Kyoto Gases`, `|Cumulative Net-Negative CO2 [2020-2100, Gt CO2]`, `meta_Scenario Typology|SCI 2025 [beta]`. Coherent for a below-3°C class (most pathways never reach net-zero). No data columns affected. Scripts must NOT assume schema equivalence across sibling directories.

## Vetting split
Dashboard displayed 40 = `meta_Vetting|SCI 2025` "ok"; export contains all 91 (51 failed). Vetted-40 by model: POLES-JRC ENGAGE (10), WITCH 5.0 (10), IMAGE 3.0 (4), IMAGE 3.2 (4), TIAM-ECN 1.1 (4), REMIND 2.1 (3), GCAM 6.0 NGFS (2), IMAGE 3.3 (2), MESSAGEix-GLOBIOM 2.0-M-R12-NGFS (1) — 9 frameworks (matches dashboard; per-framework counts sum to 40 exactly).

## Overlap with siblings (verified 2026-07-12)
Intersection of (model, scenario) pairs: ssp1-19 → 0; ssp1-26 → 0. Classes are mutually exclusive bins.

## Year grids (heterogeneous, per-pair)
| Pairs | Points | Grid |
|---|---|---|
| 34 | 15 | 5-yearly 2010–2060, decadal 2070–2100 |
| 32 | 14 | as 15-pt, one mid-century point missing |
| 19 | 19 | full 5-yearly 2010–2100 |
| 3 | 18 | full 5-yearly 2015–2100, no 2010 |
| 3 | 10 | decadal 2010–2100 |
No annual-2021–2024 variant (no MESSAGEix-GLOBIOM 1.2 COVID-Shift pairs in this class). Verify per-year pair coverage before ensemble statistics; no interpolation policy adopted by default.

## Provenance
All counts, purity, overlaps, and grids machine-verified from export files 2026-07-12; MANIFEST figures assertion-checked against files before packaging. Dashboard screenshot on file: 40 scenarios / 9 frameworks, per-framework sum = 40 (exact match, unlike ssp1-26's one-off display discrepancy).
