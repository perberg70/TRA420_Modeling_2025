# Calc Emissions Module

The `calc_emissions` package converts electricity demand trajectories and
generation mix assumptions into pollutant emission time series expressed in
megatonnes (Mt) per year. Each country has its own configuration in
``data/calc_emissions/configs/`` by default (configurable via
`calc_emissions.countries.directory` in the repository `config.yaml`); utility
scripts pick the appropriate YAML and write per-scenario CSVs consumed by
downstream modules.

## Workflow Overview

1. **Demand schedule** – each country YAML prescribes electricity demand in
   terawatt-hours (TWh) for the simulation grid.
2. **Generation mix** – technology shares sum to one for every year. Shares can
   be provided as a single value (flat over time) or as a year-indexed map.
3. **Emission factors** – technology-level emission intensity. Columns such as
   ``*_kg_per_kwh``, ``*_mt_per_twh`` or ``*_kt_per_twh`` are accepted and
   harmonised to Mt/TWh automatically. An optional ``gwp100`` column captures
   the overall forcing in CO₂-equivalent units.
4. **Baseline scenario** – sets the reference demand and mix.
5. **Policy scenarios** – reuse named templates or provide custom demand/mix
   entries. Emission deltas are computed relative to the baseline.
6. **Outputs** – per-scenario CSV files (`co2.csv`, `so2.csv`, `nox.csv`,
   `pm25.csv`) under the configured `output_directory`. Each file contains
   `year` plus `absolute_*` / `delta_*` columns (Mt/year).

## Core Equations

For technology i in year t:

1. Generation (TWh):

   $$
   G_{i,t} = D_t \times s_{i,t}
   $$

   where $D_t$ is annual demand (TWh) and $s_{i,t}$ is the technology share.

2. Emissions (Mt):

   $$
   E_{i,t} = G_{i,t} \times f_i
   $$

   with $f_i$ expressed in Mt/TWh after harmonisation.

3. Aggregate totals by pollutant:  
   $$E^{tot}_{p,t} = Σ_i E_{i,t}$$

4. Scenario delta vs baseline:  
   $$ΔE_{p,t} = E^{scenario}_{p,t} - E^{baseline}_{p,t}$$

## Configuration Keys

### Per-country YAML (stored under ``data/calc_emissions/configs/`` by default)

Each file (e.g. ``config_Albania.yaml``) wraps a full ``calc_emissions`` block:

- ``emission_factors_file`` – country-specific CSV with a ``technology`` column
  and pollutant intensities. Paths can be absolute or relative to the country
  config; when only a filename is supplied the runner looks under
  ``data/calc_emissions/emission_factors/`` (which now mirrors the canonical
  `Emission_factors_all.xlsx` export).
- ``years`` – `{start, end, step}` covering the model horizon. These values should
  match the repository-level ``time_horizon`` entry to maintain consistency across modules.
- ``demand_scenarios`` / ``mix_scenarios`` – named templates that can be reused
  across the country's scenarios.
- ``baseline`` – references the demand/mix scenario used as the reference when
  calculating deltas.
- ``baseline_mix_case`` / ``baseline_demand_case`` / ``delta_baseline_mode`` – controls
  which baseline is used when computing `delta_*` columns:
  - ``per_mix``: each mix is differenced against its own ``<mix>__<baseline_demand_case>``.
  - ``global``: every scenario is differenced against the single combination
    ``<baseline_mix_case>__<baseline_demand_case>``.

  The library default is ``per_mix``; this repository’s `config.yaml` sets
  `calc_emissions.countries.delta_baseline_mode: global` so runs are comparable
  across mixes by default.
- ``demand_scenarios`` / ``mix_scenarios`` – mappings that define the demand
  cases and mix cases to combine. Every mix is paired with every demand case, and
  scenario identifiers follow `<mix>__<demand>` (for example `base_mix__scen1_lower`).
- ``output_directory`` / ``results_directory`` – legacy fields for per-scenario CSVs.
  The pipeline now writes directly to ``results/emissions/<mix>/<Country>/`` (per-country)
  and ``results/emissions/All_countries/<mix>/`` (aggregated sums).

### Dynamic demand model

Static demand cases remain valid through the existing `values` mapping. A demand
case can also opt into `dynamic_model`, which computes electricity demand while
reading base demand from the `Reference values` table in
`data/calc_emissions/Electricity_OECD.xlsx` instead of hard-coding it.

Use `docs/dynamic_demand_model_example.yaml` as the standalone YAML template.
It includes both supported electrification forms and keeps unresolved inputs as
`TODO_SOURCE` until a data source or explicit scenario assumption is selected.

The model uses:

$$
D_t = D_0 \times \frac{e_t}{e_0} \times
\left(\frac{Y_t}{Y_0}\right)^B \times
\left(\frac{P_t}{P_0}\right)^C
$$

where `D_0` is read from the workbook. The reference table is expected to provide
country code in column A, year in column B, total electricity demand in column C,
and unit in column D. Units are converted to TWh as follows: MWh / 1,000,000;
GWh / 1,000; TWh unchanged. For example, SRB's 2023 value of 30,021 GWh becomes
30.021 TWh.

Elasticity constraints are enforced when the model is loaded:

- income elasticity `B`: `income_elasticity >= 1.0`
- price elasticity `C`: `-0.8 <= price_elasticity <= -0.2`

Electrification supports two calculated forms. `linear_time` evaluates
`e_t = A + slope * (year - base_year)`. `linear_income` defaults to
`e_t = e_base + slope * (Y_t - Y_base)` when `base_share` is present; it also
supports `e_t = A + slope * Y_t` when `A` is supplied instead. The runtime still
accepts `B` as an electrification slope alias for the original linear equation,
but the standalone YAML template uses `slope` to avoid overloading income
elasticity `B`. Electrification shares are validated to stay within `[0, 1]`;
set `bounds: clamp` only when clamping is an explicit scenario assumption.

## Outputs

- Per-country runs now write outputs to `results/<run>/emissions/<mix>/<Country>/` when a run
  directory is configured (see `docs/scripts.md` for how `<run>` is resolved).
  Each CSV contains `year`, `absolute_<demand>`, and `delta_<demand>` columns for every configured
  demand case (e.g. `absolute_base_demand`, `absolute_scen1_lower`). Deltas are computed relative to
  the baseline defined by `delta_baseline_mode` (see above).
- Aggregated multi-country emissions are written to `results/<run>/emissions/All_countries/<mix>/`.
- Individual mix folders under `results/<run>/emissions/` contain subfolders for every country so
  downstream modules can discover `co2.csv` without further configuration.

### Repository-level metadata (``config.yaml``)

The top-level ``config.yaml`` now stores only the ``calc_emissions.countries``
metadata, which records the directory, filename pattern, aggregate output
location, optional notes file (purely informational), and the list of scenario
names to aggregate across countries. The CLI helpers read this block to discover
country configs.

## Usage Tips

- Supply dense year/value pairs when possible. Sparse entries are interpolated
  linearly via `_values_to_series`.
- Ensure technology names in mixes match the lowercase `technology` column in
  the emission factor file; missing entries default to zero share.
- Mix timeseries CSVs are interpolated between the provided years (and held
  constant beyond the last datapoint), so you can provide five-year snapshots
  and let the runner fill in annual values automatically.
- The module raises an error when mix shares sum to zero for any year, preventing divide
  by zero during normalisation.
- CO₂ deltas (`co2.csv`) drive the climate pipeline; other pollutants support
  air-quality analyses and can be ingested by downstream modules.
-- Run `python scripts/run_calc_emissions_all.py` to process all configured countries. The aggregator will:
  - re-run the per-country calculations (so each country's configured `output_directory` is refreshed),
  - write per-country CSVs to `results/emissions/<mix>/<Country>/co2.csv` (with `absolute_*`/`delta_*` columns),
  - write aggregated multi-country deltas to `results/emissions/All_countries/<mix>/co2.csv` (and to any additional directory passed via `--results-output`).

  The aggregator can therefore take over the role of the single-country runner (`scripts/run_calc_emissions.py`) when you want a single command to produce both per-country and aggregated outputs.

### Testing

- `tests/test_run_calc_emissions_all.py` verifies that the per-country CSV writer produces
  `year` plus `absolute_*` / `delta_*` columns with the expected values. Run tests with:

```bash
pytest -q tests/test_run_calc_emissions_all.py
```

## Per-country writer API

The per-country writer was moved into the `src` package so other modules can reuse it
directly without importing script helpers. Import the function like this:

```python
from calc_emissions.writers import write_per_country_results

# per_country_map is a mapping: Country -> { scenario_name: EmissionScenarioResult }
# where EmissionScenarioResult is the return type from the calculator.
write_per_country_results(per_country_map, Path("results/emissions"))
```

The function writes CSVs for every pollutant found in each scenario's
`total_emissions_mt` mapping and places files under `results/emissions/<mix>/<Country>/<pollutant>.csv`.
Each CSV has columns `year`, `absolute_<demand>`, and `delta_<demand>` for every demand case (Mt/year relative to the base demand case).

This API is useful when creating custom orchestration or tests that need to
produce the same per-country folder layout as the `scripts/run_calc_emissions_all.py`
aggregator.

## References

### Emission factors and technology intensities

- Emission factors for CO₂, NOₓ, SOₓ, PM₂.₅, and GWP100 were sourced from *ecoinvent* v3.10.1 (allocation cut-off by classification).  
  Wernet, G., Bauer, C., Steubing, B., Reinhard, J., Moreno-Ruiz, E., & Weidema, B. (2016). *The ecoinvent database version 3 (part I): overview and methodology.* The International Journal of Life Cycle Assessment, 21(9), 1218–1230. <http://link.springer.com/10.1007/s11367-016-1087-8> (accessed 21 Oct 2025).

### Electricity mix sources

- Republic of Serbia (2022). *Integrated National Energy and Climate Plan of the Republic of Serbia.*
- Ministry of Ecology, Sustainable Development and Development of the North (Montenegro) (2024). *Draft National Energy and Climate Plan of Montenegro (NECP Montenegro – All chapters, 13 Dec 2024).*
- Energy Community Secretariat (2023). *Recommendations on the Draft Integrated National Energy and Climate Plan of Bosnia and Herzegovina.*
- Energy Community Secretariat (2022). *Kosovo’s National Energy and Climate Plan (NECP).*
- Ministry of Infrastructure and Energy of Albania (2024). *National Energy and Climate Plan of the Republic of Albania.*
