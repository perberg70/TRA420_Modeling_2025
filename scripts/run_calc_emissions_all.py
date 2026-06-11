"""Aggregate electricity-emission scenarios across all configured countries.

The helper discovers per-country configuration files referenced under
``calc_emissions.countries`` in ``config.yaml`` (defaulting to
``data/calc_emissions/countries/config_*.yaml``). For each country the standard
calc-emissions workflow is executed, producing per-scenario deltas. The script
then sums baseline and scenario totals across countries, writes aggregated
results under ``results/emissions/All_countries``, and optionally mirrors the
outputs to an additional directory when requested.

When imported, the ``run_all_countries`` function can be reused by higher-level
pipelines. Running the module as a script retains the previous CLI behaviour
with additional ``--results-output`` support.
"""

from __future__ import annotations

import argparse
import logging

# Ensure src/ is importable before importing the calculator
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from calc_emissions import (  # noqa: E402
    BASE_DEMAND_CASE,
    BASE_MIX_CASE,
    EmissionScenarioResult,
    compose_scenario_name,
    run_from_config,
)
from calc_emissions.writers import write_mix_directory, write_per_country_results  # noqa: E402
from config_paths import (  # noqa: E402
    apply_results_run_directory,
    get_config_path,
    get_config_root,
    get_results_run_directory,
)

LOGGER = logging.getLogger("calc_emissions.aggregate")


def _ensure_run_directory(path: Path, run_directory: str | None) -> Path:
    """Insert run_directory after 'results/' if missing."""
    if not run_directory:
        return path
    run_directory = run_directory.strip().strip("/")
    if not run_directory:
        return path
    parts = path.resolve().parts
    try:
        idx = parts.index("results")
    except ValueError:
        return path
    if idx + 1 < len(parts) and parts[idx + 1] == run_directory:
        return path
    rebuilt = Path(*parts[: idx + 1]) / run_directory / Path(*parts[idx + 1 :])
    return rebuilt


def _load_country_settings() -> tuple[
    Path,
    str,
    Path,
    Path,
    Path,
    list[str],
    list[str],
    dict[str, int] | None,
    str | None,
    str,
    str,
    str,
]:
    config_path = get_config_path(ROOT / "config.yaml")
    config = {}
    if config_path.exists():
        with config_path.open() as handle:
            config = yaml.safe_load(handle) or {}
    config_root = get_config_root(config, ROOT)
    run_directory = get_results_run_directory(config)
    module_cfg = config.get("calc_emissions", {})
    countries_cfg = module_cfg.get("countries", {})
    # default to the new data layout: data/configs for per-country YAMLs
    configured_dir = Path(countries_cfg.get("directory", "data/configs"))
    if configured_dir.is_absolute():
        directory = configured_dir
    else:
        directory = (config_root / configured_dir).resolve()
    # If the configured directory doesn't exist, try a few common alternate
    # locations we have used historically so the aggregator is resilient to
    # small reorganisations (data/calc_emissions/configs, data/configs, etc).
    if not directory.exists():
        candidates = [
            config_root / configured_dir,
            config_root / "data/configs",
            config_root / "data/calc_emissions/configs",
            config_root / "data/calc_emissions/countries",
            config_root / "country_data/configs",
        ]
        for cand in candidates:
            if cand.exists():
                directory = cand
                break
    pattern = countries_cfg.get("pattern", "config_{name}.yaml")
    aggregate_output_cfg = Path(
        countries_cfg.get("aggregate_output_directory", "results/emissions/All_countries")
    )
    if aggregate_output_cfg.is_absolute():
        aggregate_output = aggregate_output_cfg
    else:
        aggregate_output = (config_root / aggregate_output_cfg).resolve()
    aggregate_output = apply_results_run_directory(
        aggregate_output,
        run_directory,
        repo_root=ROOT,
    )
    aggregate_output = _ensure_run_directory(aggregate_output, run_directory)
    resources_root_cfg = Path(countries_cfg.get("resources_root", "results/emissions"))
    if resources_root_cfg.is_absolute():
        resources_root = resources_root_cfg
    else:
        resources_root = (config_root / resources_root_cfg).resolve()
    resources_root = apply_results_run_directory(
        resources_root,
        run_directory,
        repo_root=ROOT,
    )
    resources_root = _ensure_run_directory(resources_root, run_directory)
    mix_cases = countries_cfg.get("mix_scenarios", []) or []
    demand_cases = countries_cfg.get("demand_scenarios", []) or []
    baseline_case = (
        str(countries_cfg.get("baseline_demand_case", BASE_DEMAND_CASE)).strip() or BASE_DEMAND_CASE
    )
    baseline_mix_case = (
        str(
            countries_cfg.get(
                "baseline_mix_case", module_cfg.get("baseline_mix_case", BASE_MIX_CASE)
            )
        ).strip()
        or BASE_MIX_CASE
    )
    delta_mode = (
        str(
            countries_cfg.get(
                "delta_baseline_mode", module_cfg.get("delta_baseline_mode", "per_mix")
            )
        )
        .strip()
        .lower()
        or "per_mix"
    )
    global_horizon = config.get("time_horizon")
    return (
        directory,
        pattern,
        aggregate_output,
        resources_root,
        mix_cases,
        demand_cases,
        global_horizon,
        run_directory,
        baseline_case,
        baseline_mix_case,
        delta_mode,
    )


def list_country_configs(
    directory: Path, pattern: str, countries_filter: Iterable[str] | None = None
) -> Dict[str, Path]:
    configs: Dict[str, Path] = {}
    glob_pattern = pattern.replace("{name}", "*")
    for path in sorted(directory.glob(glob_pattern)):
        if not path.is_file():
            continue
        if "Backup" in path.name:
            continue
        name = path.stem.replace("config_", "")
        if countries_filter and name not in countries_filter:
            continue
        configs[name] = path
    return configs


def _clone_result(result: EmissionScenarioResult) -> EmissionScenarioResult:
    return EmissionScenarioResult(
        name=result.name,
        demand_case=result.demand_case,
        mix_case=result.mix_case,
        years=list(result.years),
        demand_twh=result.demand_twh.copy(),
        generation_twh=result.generation_twh.copy(),
        technology_emissions_mt={k: df.copy() for k, df in result.technology_emissions_mt.items()},
        total_emissions_mt={k: series.copy() for k, series in result.total_emissions_mt.items()},
        delta_mtco2=result.delta_mtco2.copy(),
        electrification=(
            result.electrification.copy() if result.electrification is not None else None
        ),
    )


def _add_series(target: pd.Series | None, addition: pd.Series) -> pd.Series:
    if target is None:
        return addition.copy()
    idx = target.index.union(addition.index)
    target = target.reindex(idx, fill_value=0.0)
    addition = addition.reindex(idx, fill_value=0.0)
    return target + addition


def _add_frame(target: pd.DataFrame | None, addition: pd.DataFrame) -> pd.DataFrame:
    if target is None:
        return addition.copy()
    index = target.index.union(addition.index)
    columns = target.columns.union(addition.columns)
    target = target.reindex(index=index, columns=columns, fill_value=0.0)
    addition = addition.reindex(index=index, columns=columns, fill_value=0.0)
    return target + addition


def _accumulate_result(target: EmissionScenarioResult, addition: EmissionScenarioResult) -> None:
    target.demand_twh = _add_series(target.demand_twh, addition.demand_twh)
    target.generation_twh = _add_frame(target.generation_twh, addition.generation_twh)

    for pollutant, df in addition.technology_emissions_mt.items():
        target_df = target.technology_emissions_mt.get(pollutant)
        target.technology_emissions_mt[pollutant] = _add_frame(target_df, df)
    for pollutant, series in addition.total_emissions_mt.items():
        target_series = target.total_emissions_mt.get(pollutant)
        target.total_emissions_mt[pollutant] = _add_series(target_series, series)

    target.delta_mtco2 = _add_series(target.delta_mtco2, addition.delta_mtco2)


def _build_aggregated_results(
    per_country_results: List[dict[str, EmissionScenarioResult]],
    baseline_case: str,
    baseline_mix_case: str,
    delta_mode: str,
    per_country_map: Mapping[str, dict[str, EmissionScenarioResult]] | None = None,
) -> dict[str, EmissionScenarioResult]:
    aggregated: Dict[tuple[str, str], EmissionScenarioResult] = {}
    for result_map in per_country_results:
        for scenario in result_map.values():
            key = (scenario.mix_case, scenario.demand_case)
            clone = _clone_result(scenario)
            existing = aggregated.get(key)
            if existing is None:
                aggregated[key] = clone
            else:
                _accumulate_result(existing, clone)

    # Aggregated emissions cannot carry a single national electrification path;
    # keep the per-country series so downstream modules (e.g. indoor air
    # pollution) can use them.
    if per_country_map:
        for country, result_map in per_country_map.items():
            for scenario in result_map.values():
                if scenario.electrification is None:
                    continue
                key = (scenario.mix_case, scenario.demand_case)
                target = aggregated.get(key)
                if target is None:
                    continue
                if target.electrification_by_country is None:
                    target.electrification_by_country = {}
                target.electrification_by_country[country] = scenario.electrification.copy()
        for target in aggregated.values():
            target.electrification = None

    if not aggregated:
        raise ValueError("No scenarios aggregated – ensure country configs are configured.")

    baseline_global = None
    if delta_mode == "global":
        baseline_global = aggregated.get((baseline_mix_case, baseline_case))
        if baseline_global is None:
            raise ValueError(
                f"Missing baseline scenario '{baseline_mix_case}__{baseline_case}' for aggregation."
            )

    combined: Dict[str, EmissionScenarioResult] = {}
    for (mix_case, demand_case), scenario in aggregated.items():
        if delta_mode == "global":
            baseline_co2 = (
                baseline_global.total_emissions_mt.get("co2") if baseline_global else None
            )
            totals = scenario.total_emissions_mt.get("co2")
            if baseline_co2 is not None and totals is not None:
                idx = baseline_co2.index.union(totals.index)
                baseline_aligned = baseline_co2.reindex(idx, fill_value=0.0)
                totals_aligned = totals.reindex(idx, fill_value=0.0)
                scenario.total_emissions_mt["co2"] = totals_aligned
                scenario.delta_mtco2 = totals_aligned - baseline_aligned
            else:
                scenario.delta_mtco2 = scenario.delta_mtco2 * 0.0
        else:
            baseline = aggregated.get((mix_case, baseline_case))
            if baseline is not None:
                baseline_co2 = baseline.total_emissions_mt.get("co2")
                totals = scenario.total_emissions_mt.get("co2")
                if baseline_co2 is not None and totals is not None:
                    idx = baseline_co2.index.union(totals.index)
                    baseline_aligned = baseline_co2.reindex(idx, fill_value=0.0)
                    totals_aligned = totals.reindex(idx, fill_value=0.0)
                    scenario.total_emissions_mt["co2"] = totals_aligned
                    scenario.delta_mtco2 = totals_aligned - baseline_aligned
                else:
                    scenario.delta_mtco2 = scenario.delta_mtco2 * 0.0
            else:
                scenario.delta_mtco2 = scenario.delta_mtco2 * 0.0
        scenario.name = compose_scenario_name(demand_case, mix_case)
        combined[scenario.name] = scenario

    return combined


def _group_by_mix(
    results: dict[str, EmissionScenarioResult],
) -> dict[str, dict[str, EmissionScenarioResult]]:
    grouped: dict[str, dict[str, EmissionScenarioResult]] = {}
    for res in results.values():
        grouped.setdefault(res.mix_case, {})[res.demand_case] = res
    return grouped


def _write_outputs(
    aggregated: dict[str, EmissionScenarioResult],
    destinations: Iterable[Path],
    baseline_case: str,
    baseline_override: Mapping[str, pd.Series] | None = None,
) -> None:
    grouped = _group_by_mix(aggregated)
    resolved_destinations = [Path(path) for path in destinations]
    for mix_name, demand_map in grouped.items():
        for dest in resolved_destinations:
            dest.mkdir(parents=True, exist_ok=True)
            write_mix_directory(
                mix_name, demand_map, dest / mix_name, baseline_case, baseline_override
            )


# The per-country writer implementation has been moved to
# `src/calc_emissions/writers.py` and is imported as
# `write_per_country_results` at module top-level. We no longer expose the
# underscored compatibility wrapper here.


def run_all_countries(
    countries: Iterable[str] | None = None,
    output: Path | None = None,
    results_output: Path | None = None,
    mirror_to_root: bool = True,
    scenarios: Iterable[str] | None = None,
) -> dict[str, EmissionScenarioResult]:
    (
        directory,
        pattern,
        default_output,
        per_country_root,
        configured_mix_cases,
        configured_demand_cases,
        global_horizon,
        run_directory,
        baseline_case,
        baseline_mix_case,
        delta_mode,
    ) = _load_country_settings()

    countries_filter = [c.replace(" ", "_") for c in countries] if countries else None
    configs = list_country_configs(directory, pattern, countries_filter)
    if not configs:
        raise FileNotFoundError("No country config files found for aggregation.")

    mix_filter = (
        [s for s in scenarios if s] if scenarios else [m for m in configured_mix_cases if m]
    )
    if baseline_mix_case and baseline_mix_case not in mix_filter:
        mix_filter.append(baseline_mix_case)
    demand_order = [d for d in configured_demand_cases if d]

    per_country_results: List[dict[str, EmissionScenarioResult]] = []
    per_country_map: dict[str, dict[str, EmissionScenarioResult]] = {}
    per_country_baselines: dict[str, dict[str, pd.Series]] = {}
    LOGGER.info("Running calc_emissions for %d countries", len(configs))
    for country, cfg_path in configs.items():
        LOGGER.info("  • %s", country)
        results = run_from_config(
            cfg_path,
            default_years=global_horizon,
            results_run_directory=run_directory,
            allowed_demand_cases=demand_order or None,
            allowed_mix_cases=mix_filter or None,
            baseline_mix_case=baseline_mix_case,
            delta_baseline_mode=delta_mode,
        )

        available_mixes = {res.mix_case for res in results.values()}
        if not mix_filter:
            mix_filter = sorted(available_mixes)
            LOGGER.info("No mix list configured; using mixes from %s: %s", country, mix_filter)
        requested_mixes = set(mix_filter)
        missing_mixes = requested_mixes - available_mixes
        if missing_mixes:
            raise KeyError(
                f"Country '{country}' missing required mix scenarios: {sorted(missing_mixes)}. "
                "Each country must define all mix scenarios listed in config.yaml "
                "under calc_emissions.countries.mix_scenarios."
            )

        available_demands = {res.demand_case for res in results.values()}
        if baseline_case not in available_demands:
            raise KeyError(
                f"Country '{country}' missing baseline demand '{baseline_case}'. "
                "Update the country config demand_scenarios or baseline_demand_case."
            )
        if demand_order:
            selected_demands = [d for d in demand_order if d in available_demands]
        else:
            selected_demands = []
        for demand in sorted(available_demands):
            if demand not in selected_demands:
                selected_demands.append(demand)
        if baseline_case not in selected_demands and baseline_case in available_demands:
            selected_demands.insert(0, baseline_case)

        filtered_results: dict[str, EmissionScenarioResult] = {}
        for name, res in results.items():
            if res.mix_case in requested_mixes and res.demand_case in selected_demands:
                filtered_results[name] = res

        if delta_mode == "global":
            baseline_key = compose_scenario_name(baseline_case, baseline_mix_case)
            baseline_res = filtered_results.get(baseline_key)
            if baseline_res is None:
                raise KeyError(
                    f"Country '{country}' missing global baseline scenario '{baseline_key}'. "
                    "Ensure baseline_mix_case and baseline_demand_case exist for every country."
                )
            per_country_baselines[country] = {
                pollutant: series.copy()
                for pollutant, series in baseline_res.total_emissions_mt.items()
                if series is not None
            }

        per_country_results.append(filtered_results)
        per_country_map[country] = filtered_results

        with cfg_path.open() as handle:
            cfg = yaml.safe_load(handle) or {}
        mod_cfg = cfg.get("calc_emissions", {})
        outdir = Path(mod_cfg.get("output_directory", "results/emissions"))
        if not outdir.is_absolute():
            outdir = (cfg_path.parent / outdir).resolve()
        if global_horizon and isinstance(global_horizon, dict):
            years_cfg = mod_cfg.get("years")
            if years_cfg:
                mismatch = any(
                    int(years_cfg.get(key, global_horizon[key])) != int(global_horizon[key])
                    for key in ("start", "end", "step")
                    if key in global_horizon
                )
                if mismatch:
                    LOGGER.warning(
                        "Country '%s' years %s differ from global time_horizon %s",
                        country,
                        years_cfg,
                        global_horizon,
                    )
    aggregated_results = _build_aggregated_results(
        per_country_results,
        baseline_case,
        baseline_mix_case,
        delta_mode,
        per_country_map=per_country_map,
    )

    aggregate_dest = output or default_output
    destinations = [aggregate_dest]
    if results_output is not None:
        destinations.append(results_output)

    baseline_override = None
    if delta_mode == "global":
        baseline_key = compose_scenario_name(baseline_case, baseline_mix_case)
        baseline_res = aggregated_results.get(baseline_key)
        if baseline_res is None:
            raise KeyError(
                f"Missing aggregated baseline scenario '{baseline_key}' for global deltas."
            )
        baseline_override = {
            pollutant: series.copy()
            for pollutant, series in baseline_res.total_emissions_mt.items()
            if series is not None
        }

    _write_outputs(aggregated_results, destinations, baseline_case, baseline_override)

    if per_country_map:
        write_per_country_results(
            per_country_map,
            per_country_root,
            baseline_case,
            per_country_baselines if delta_mode == "global" else None,
        )

    LOGGER.info("Aggregated deltas written to %s", aggregate_dest)
    if results_output is not None:
        LOGGER.info("Aggregated deltas also copied to %s", results_output)

    return aggregated_results


def _parse_args() -> argparse.Namespace:
    (
        _directory,
        _pattern,
        default_output,
        _resources_root,
        configured_mix_cases,
        _configured_demand_cases,
        _global_horizon,
        _run_directory,
        _baseline_case,
        _baseline_mix_case,
        _delta_mode,
    ) = _load_country_settings()
    parser = argparse.ArgumentParser(
        description="Run emissions for all countries and aggregate deltas."
    )
    parser.add_argument(
        "--countries",
        nargs="*",
        help=(
            "Optional list of country names to include (matching config filenames without prefix). "
            "Examples: Albania Bosnia-Herzegovina North_Macedonia Serbia Kosovo Montenegro"
        ),
    )
    parser.add_argument(
        "--output",
        default=str(
            default_output.relative_to(ROOT)
            if default_output.is_relative_to(ROOT)
            else default_output
        ),
        help=(
            "Aggregate output directory "
            "(defaults to calc_emissions.countries.aggregate_output_directory)"
        ),
    )
    parser.add_argument(
        "--results-output",
        default=None,
        help=("Optional directory to copy aggregated results in addition to --output."),
    )
    parser.add_argument(
        "--scenarios",
        nargs="*",
        help=(
            "Mix scenario names to aggregate (must exist in every country config). "
            f"Defaults to config list: {configured_mix_cases or 'all'}"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    countries = args.countries if args.countries else None
    output = Path(args.output)
    results_output = Path(args.results_output) if args.results_output else None
    scenario_filter = args.scenarios if args.scenarios else None

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")

    aggregated = run_all_countries(
        countries=countries,
        output=output,
        results_output=results_output,
        scenarios=scenario_filter,
    )
    baseline_case = BASE_DEMAND_CASE
    example = next(
        (name for name in sorted(aggregated) if aggregated[name].demand_case != baseline_case),
        None,
    )
    if example:
        co2_delta = aggregated[example].delta_mtco2
        years_to_show = [y for y in [2030, 2050, 2100] if y in co2_delta.index]
        if years_to_show:
            LOGGER.info(
                "Example scenario '%s' CO₂ deltas:\n%s",
                example,
                co2_delta.loc[years_to_show].to_frame(name="delta_mt").to_string(),
            )


if __name__ == "__main__":
    main()
