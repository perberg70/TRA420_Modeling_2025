# MANIFEST — Scenario Compass pull: SSP1-1.9 constraint set (GW0+GW1)

**Pull date:** 2026-07-11
**Committed:** 2026-07-12
**Source:** IIASA Scenario Compass (SCI 2025, AR6-based), custom-panel export via download.scenariocompass.org
**Dashboard filters:** Climate categories GW0 ("below 1.5°C without overshoot") + GW1 ("below 1.5°C with limited overshoot"); variables as listed per file below.
**Region:** Europe R10 — confirmed by dashboard region-selector screenshot only. **No region column exists in these exports**; region is not machine-verifiable from file contents. (Note: the superseded GW8-era `ssp_constraint_*.csv` files carried a region column; these exports do not. Schema differs between pulls.)

## Files

| File | Variable | Model–scenario pairs | Rows | Year grid |
|---|---|---|---|---|
| `final_energy.csv` | Final Energy | 103 | 1673 | 2010–2100, 23 pts (incl. 2021–2024 annuals) |
| `final_energy_electricity.csv` | Final Energy\|Electricity | **98** | 1578 | 2010–2100, **19 pts** (no 2021–2024 annuals) |
| `final_energy_res_com.csv` | Final Energy\|Residential and Commercial | 103 | 1673 | 2010–2100, 23 pts |
| `final_energy_res_com_electricity.csv` | Final Energy\|Residential and Commercial\|Electricity | 103 | 1673 | 2010–2100, 23 pts |

SHA-256 (first 12 hex): final_energy `5576a3936e50` · final_energy_electricity `c8516bcb2f56` · final_energy_res_com `bbdf09deeb32` · final_energy_res_com_electricity `670b27d1ad01`

## Composition (verified by direct file inspection, 2026-07-12)

- **Model names:** 18 distinct. **Model families:** 10 by first-token grouping (AIM, COFFEE, GCAM, GEM-E3, IMAGE, MESSAGEix-GLOBIOM, POLES-JRC, REMIND, REMIND-MAgPIE, WITCH); **9 if REMIND-MAgPIE is folded into REMIND**. Any "9 families" statement in project documents uses the folded convention — state it when citing.
- **Coverage gap:** 5 pairs (all MESSAGEix-GLOBIOM 1.2, COVID-Shift-*_550 scenarios) lack `Final Energy|Electricity`. Economy-wide electrification ratios computed on the pair intersection therefore cover 98 pairs, not 103. Intersections must be keyed on (model, scenario), never scenario name alone.
- **Vetting (`meta_Vetting|SCI 2025`):** 7 pairs `ok`, 96 `failed`. All 7 vetted pairs are IMAGE 3.2; scenario names carry SPA1-19 (SSP1) or SPA2-19 (SSP2) tags at 1.9 W/m² forcing:
  - SSP2021-SSP1-SPA1-19-Default-LowBiomass
  - SSP2021-SSP1-SPA1-19-Lifestyle
  - SSP2021-SSP1-SPA1-19-Renewables-LowBiomass
  - SSP2021-SSP2-SPA1-19-Default-LowBiomass
  - SSP2021-SSP2-SPA2-19-Default
  - SSP2021-SSP2-SPA2-19-Lifestyle
  - SSP2021-SSP2-SPA2-19-Lifestyle-Renewables
- **Vetting criteria:** not documented in export or accessed Scenario Compass documentation. Open gap.
- **Schema:** 43 columns, kept as exported (raw provenance preserved). Analysis scripts select columns via YAML config; no columns stripped at commit.

## Known caveats carried forward

1. Region R10 is procedural (screenshot), not machine-verified.
2. R10 is pan-European; WB6 use requires downscaling (population/GDP share) — never direct application of R10 medians.
3. SCI-2025 vetting criteria undocumented; vetted-7 vs. all-103 curation decision open (supervisor input pending).
4. ResCom sector definition vs. demand-module ResCom category: cross-check not yet done.
5. Kosovo: R10 aggregate is not country-disaggregated; Kosovo handling must be explicit in any WB6-level result derived from this set.

## Sibling pulls (future)

SSP2–5 exports go in sibling directories (`../ssp2-45/`, etc.) with identical file naming and a MANIFEST.md following this template. Do not overwrite files in place across pulls.
