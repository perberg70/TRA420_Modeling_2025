"""Compute health impacts from non-CO₂ emission changes.

This module links the electricity emission scenarios produced by
``calc_emissions`` to concentration-response functions for air pollutants.
High-level workflow:

1. Load baseline concentration statistics per country.
2. Retrieve baseline and scenario emissions from ``calc_emissions``.
3. Assume concentration changes scale linearly with emission ratios.
4. Apply a log-linear relative-risk model to estimate mortality response.

Outputs are written as CSV files per scenario/pollutant containing the
percentage change in mortality for each country and year.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
import yaml

from calc_emissions import (
    BASE_DEMAND_CASE,
    BASE_MIX_CASE,
    EmissionScenarioResult,
    run_from_config as run_emissions,
)
from config_paths import (
    apply_results_run_directory,
    get_config_path,
    get_config_root,
    get_results_run_directory,
)

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_POLLUTANT_FILES = {
    "pm25": "data/air_pollution/PM25_country_stats.csv",
    "nox": "data/air_pollution/NOx_country_stats.csv",
}

DEFAULT_RELATIVE_RISK = {"pm25": 1.08, "nox": 1.03}
DEFAULT_REFERENCE_DELTA = 10.0  # µg/m³ corresponding to the RR default.
DEFAULT_FALLBACK_MEASURES = ["median", "mean", "min", "max"]


@dataclass
class PollutantImpact:
    """Container for pollutant-specific health impacts."""

    pollutant: str
    concentration_metric: str
    beta: float
    concentrations: pd.Series
    emission_ratio: pd.Series
    impacts: pd.DataFrame
    country_weights: pd.Series
    weighted_percent_change: pd.Series
    baseline_deaths_per_year: float | None = None
    deaths_summary: pd.DataFrame | None = None
    baseline_deaths_by_country: pd.Series | None = None
    electricity_share: pd.Series | None = None


@dataclass(frozen=True)
class HealthCostAssumptions:
    """Unit-cost assumptions for monetising mortality and morbidity impacts.

    ``per_death`` values capture direct costs attached to each death (end-of-life
    treatment, lost earnings). ``per_case`` values are applied to non-fatal
    illness episodes via ``morbidity_cases_per_death``.
    """

    healthcare_cost_usd_per_death: float = 0.0
    income_loss_usd_per_death: float = 0.0
    morbidity_cases_per_death: float = 0.0
    healthcare_cost_usd_per_case: float = 0.0
    income_loss_usd_per_case: float = 0.0

    @property
    def healthcare_cost_per_death(self) -> float:
        return (
            self.healthcare_cost_usd_per_death
            + self.morbidity_cases_per_death * self.healthcare_cost_usd_per_case
        )

    @property
    def income_loss_per_death(self) -> float:
        return (
            self.income_loss_usd_per_death
            + self.morbidity_cases_per_death * self.income_loss_usd_per_case
        )


@dataclass
class AirPollutionResult:
    """Scenario-level health impact results."""

    scenario: str
    pollutant_results: dict[str, PollutantImpact]
    total_mortality_summary: pd.DataFrame | None = None
    indoor_impacts: pd.DataFrame | None = None
    indoor_summary: pd.DataFrame | None = None
    health_cost_summary: pd.DataFrame | None = None


def run_from_config(
    config_path: Path | str | None = None,
    emission_results: Mapping[str, EmissionScenarioResult] | None = None,
) -> dict[str, AirPollutionResult]:
    """Run air-pollution health calculations defined in ``config.yaml``.

    Parameters
    ----------
    config_path:
        Path to the configuration file containing ``air_pollution`` settings.
    emission_results:
        Optional results returned by :func:`calc_emissions.run_from_config`.
        When omitted the function will execute the emissions module first.

    Returns
    -------
    dict[str, AirPollutionResult]
        Mapping of scenario name to impact results.
    """

    config_path = Path(config_path) if config_path is not None else get_config_path()
    with config_path.open() as handle:
        full_config = yaml.safe_load(handle) or {}

    config_root = get_config_root(full_config, config_path.parent)

    calc_cfg = full_config.get("calc_emissions", {}) if isinstance(full_config, Mapping) else {}
    module_cfg_map: Mapping[str, object] = calc_cfg if isinstance(calc_cfg, Mapping) else {}
    countries_cfg: Mapping[str, object] = {}
    if module_cfg_map:
        raw_countries_cfg = module_cfg_map.get("countries")
        if isinstance(raw_countries_cfg, Mapping):
            countries_cfg = raw_countries_cfg

    baseline_demand_case = (
        str(
            countries_cfg.get(
                "baseline_demand_case",
                module_cfg_map.get("baseline_demand_case", BASE_DEMAND_CASE),
            )
        ).strip()
        or BASE_DEMAND_CASE
    )
    baseline_mix_case = (
        str(
            countries_cfg.get(
                "baseline_mix_case",
                module_cfg_map.get("baseline_mix_case", BASE_MIX_CASE),
            )
        ).strip()
        or BASE_MIX_CASE
    )
    delta_baseline_mode = (
        str(
            countries_cfg.get(
                "delta_baseline_mode",
                module_cfg_map.get("delta_baseline_mode", "per_mix"),
            )
        )
        .strip()
        .lower()
        or "per_mix"
    )

    module_cfg = full_config.get("air_pollution")
    if not module_cfg:
        raise ValueError("'air_pollution' section missing from config.yaml")

    run_directory = get_results_run_directory(full_config)
    output_dir = Path(module_cfg.get("output_directory", "results/air_pollution"))
    if not output_dir.is_absolute():
        output_dir = (config_root / output_dir).resolve()
    output_dir = apply_results_run_directory(
        output_dir,
        run_directory,
        repo_root=config_root,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    fallback_measures = _normalise_measures(
        module_cfg.get("concentration_fallback_order", DEFAULT_FALLBACK_MEASURES)
    )
    default_measure = module_cfg.get("concentration_measure", fallback_measures[0])
    countries_filter = module_cfg.get("countries")
    selection = _normalise_scenario_selection(module_cfg.get("scenarios", "all"))
    module_country_weights_cfg = module_cfg.get("country_weights")
    module_baseline_cfg = module_cfg.get("baseline_deaths")
    module_baseline_deaths = _parse_baseline_deaths(module_baseline_cfg)
    module_vsl_cfg = module_cfg.get("value_of_statistical_life_usd")
    module_vsl_value = float(module_vsl_cfg) if module_vsl_cfg is not None else None
    module_weights_cfg = (
        module_baseline_cfg.get("weights") if isinstance(module_baseline_cfg, Mapping) else None
    )
    electricity_share_cfg = module_cfg.get("electricity_share", 0.07)
    health_costs = _parse_health_costs(module_cfg.get("health_costs"))
    indoor_cfg = module_cfg.get("indoor")
    indoor_stats: pd.DataFrame | None = None
    if isinstance(indoor_cfg, Mapping) and indoor_cfg.get("stats_file"):
        indoor_stats = _load_indoor_stats(
            config_root,
            str(indoor_cfg["stats_file"]),
            countries_filter,
        )
    elif indoor_cfg not in (None, {}):
        if not isinstance(indoor_cfg, Mapping):
            raise TypeError("'air_pollution.indoor' must be a mapping.")
        raise ValueError("'air_pollution.indoor' requires a 'stats_file' entry.")

    if emission_results is None:
        emission_results = run_emissions(
            config_path=config_path,
            results_run_directory=run_directory,
        )
    baseline_by_mix = {
        res.mix_case: res
        for res in emission_results.values()
        if res.demand_case == baseline_demand_case
    }
    baseline_global = baseline_by_mix.get(baseline_mix_case)
    if not baseline_by_mix:
        raise ValueError(
            f"calc_emissions results must include '{baseline_demand_case}' demand cases."
        )
    if delta_baseline_mode == "global" and baseline_global is None:
        raise ValueError(
            f"calc_emissions results must include '{baseline_mix_case}__{baseline_demand_case}' "
            "when delta_baseline_mode is 'global'."
        )
    pollutant_cfg = module_cfg.get("pollutants", {})
    pollutants = set(pollutant_cfg) if pollutant_cfg else set(DEFAULT_POLLUTANT_FILES)

    results: dict[str, AirPollutionResult] = {}

    for scenario_name, scenario_result in emission_results.items():
        if scenario_result.demand_case == baseline_demand_case:
            continue
        if selection and scenario_name not in selection:
            continue
        if delta_baseline_mode == "global":
            baseline = baseline_global
        else:
            baseline = baseline_by_mix.get(scenario_result.mix_case)
        if baseline is None:
            continue

        pollutant_results: dict[str, PollutantImpact] = {}
        for pollutant in sorted(pollutants):
            scenario_series = scenario_result.total_emissions_mt.get(pollutant)
            baseline_series = baseline.total_emissions_mt.get(pollutant)
            if scenario_series is None or baseline_series is None:
                continue

            cfg = pollutant_cfg.get(pollutant, {})
            concentrations, baseline_deaths_by_country, electricity_share_series = (
                _load_concentrations(
                    config_root,
                    cfg.get("stats_file", DEFAULT_POLLUTANT_FILES.get(pollutant)),
                    cfg.get("concentration_measure", default_measure),
                    fallback_measures,
                    countries_filter,
                    electricity_share_cfg,
                )
            )

            if concentrations.empty:
                continue

            country_weights = _resolve_country_weights(
                concentrations.index,
                module_country_weights_cfg,
                cfg.get("country_weights"),
            )
            if baseline_deaths_by_country is not None and not baseline_deaths_by_country.empty:
                normalized = _weights_from_baseline(
                    baseline_deaths_by_country, concentrations.index
                )
                if normalized is not None:
                    country_weights = normalized

            beta = _derive_beta(cfg, pollutant)
            emission_ratio = _compute_emission_ratio(scenario_series, baseline_series)
            delta_fraction = _compute_delta_fraction(scenario_series, baseline_series)
            impacts = _build_impacts(
                concentrations, emission_ratio, delta_fraction, beta, electricity_share_series
            )

            if impacts.empty:
                continue

            weighted_percent_change = _weighted_percent_change(impacts, country_weights)

            baseline_deaths = _parse_baseline_deaths(cfg.get("baseline_deaths"))
            if baseline_deaths is None and baseline_deaths_by_country is not None:
                baseline_deaths = float(baseline_deaths_by_country.sum())
            deaths_summary = None
            if baseline_deaths is not None:
                deaths_summary = _build_death_summary(
                    weighted_percent_change,
                    baseline_deaths,
                    value_of_statistical_life=module_vsl_value,
                    health_costs=health_costs,
                )
                if deaths_summary.empty:
                    deaths_summary = None

            pollutant_results[pollutant] = PollutantImpact(
                pollutant=pollutant,
                concentration_metric=cfg.get("concentration_measure", default_measure),
                beta=beta,
                concentrations=concentrations,
                emission_ratio=emission_ratio,
                impacts=impacts,
                country_weights=country_weights,
                weighted_percent_change=weighted_percent_change,
                baseline_deaths_per_year=baseline_deaths,
                deaths_summary=deaths_summary,
                baseline_deaths_by_country=baseline_deaths_by_country,
                electricity_share=electricity_share_series,
            )

            _write_output(output_dir, scenario_name, pollutant, impacts)
            _write_concentration_summary(
                output_dir, scenario_name, pollutant, impacts, country_weights
            )
            if deaths_summary is not None:
                _write_mortality_summary(output_dir, scenario_name, pollutant, deaths_summary)

        indoor_impacts: pd.DataFrame | None = None
        indoor_summary: pd.DataFrame | None = None
        if indoor_stats is not None and not indoor_stats.empty:
            indoor_impacts, indoor_summary = _build_indoor_results(
                scenario_result,
                baseline,
                indoor_stats,
                value_of_statistical_life=module_vsl_value,
                health_costs=health_costs,
            )
            if indoor_impacts is not None:
                _write_indoor_health_impact(output_dir, scenario_name, indoor_impacts)
            if indoor_summary is not None:
                _write_indoor_mortality_summary(output_dir, scenario_name, indoor_summary)

        if pollutant_results or indoor_summary is not None:
            baseline_for_total = module_baseline_deaths
            if baseline_for_total is None:
                derived = sum(
                    impact.baseline_deaths_per_year or 0.0 for impact in pollutant_results.values()
                )
                if derived > 0.0:
                    baseline_for_total = float(derived)
                    module_baseline_deaths = baseline_for_total
            total_summary = None
            if pollutant_results and baseline_for_total is not None:
                total_summary = _build_total_mortality_summary(
                    pollutant_results,
                    baseline_for_total,
                    module_weights_cfg,
                    module_vsl_value,
                    health_costs=health_costs,
                )
                if total_summary is not None:
                    total_summary = _fold_indoor_into_total(total_summary, indoor_summary)
                    _write_total_mortality_summary(output_dir, scenario_name, total_summary)
            health_cost_summary = _build_health_cost_summary(
                total_summary,
                indoor_summary,
                value_of_statistical_life=module_vsl_value,
                health_costs=health_costs,
            )
            if health_cost_summary is not None:
                _write_health_cost_summary(output_dir, scenario_name, health_cost_summary)
            results[scenario_name] = AirPollutionResult(
                scenario=scenario_name,
                pollutant_results=pollutant_results,
                total_mortality_summary=total_summary,
                indoor_impacts=indoor_impacts,
                indoor_summary=indoor_summary,
                health_cost_summary=health_cost_summary,
            )

    return results


def _normalise_measures(measures: Iterable[str]) -> list[str]:
    ordered = []
    for measure in measures:
        if not isinstance(measure, str):
            continue
        key = measure.strip().lower()
        if key and key not in ordered:
            ordered.append(key)
    if not ordered:
        ordered = DEFAULT_FALLBACK_MEASURES.copy()
    return ordered


def _normalise_scenario_selection(selection: object) -> set[str]:
    if selection in (None, "all"):
        return set()
    if isinstance(selection, str):
        return {selection}
    if isinstance(selection, Iterable):
        return {str(item) for item in selection}
    return set()


def _resolve_electricity_share(cfg: object, countries: Iterable[str]) -> pd.Series:
    """Return per-country electricity shares bounded to [0, 1]."""

    countries = [str(c) for c in countries]
    default = cfg
    shares: dict[str, float] = {}

    if isinstance(cfg, Mapping):
        for key, value in cfg.items():
            if key is None:
                continue
            country = str(key)
            shares[country] = float(value)
        default = cfg.get("default", 0.07)

    if not isinstance(default, (int, float)):
        default = 0.07

    default = max(0.0, min(float(default), 1.0))
    series = pd.Series({c: max(0.0, min(float(shares.get(c, default)), 1.0)) for c in countries})
    return series.astype(float)


def _load_concentrations(
    config_dir: Path,
    stats_path: str | None,
    preferred_measure: str,
    fallback_measures: Iterable[str],
    countries_filter: Iterable[str] | None,
    electricity_share_cfg: object,
) -> tuple[pd.Series, pd.Series | None, pd.Series]:
    if not stats_path:
        return pd.Series(dtype=float)

    path = Path(stats_path)
    if not path.is_absolute():
        path = (config_dir / path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Air-pollution statistics file not found: '{path}'.")

    df = pd.read_csv(path)
    if "country" not in df.columns:
        raise ValueError(f"'country' column missing from air-pollution file: '{path}'.")

    measures = [preferred_measure.lower(), *[m.lower() for m in fallback_measures]]
    numeric_cols = ["mean", "median", "min", "max"]
    available = {col.lower(): col for col in df.columns if col.lower() in numeric_cols}

    chosen_col = None
    for candidate in measures:
        if candidate in available:
            chosen_col = available[candidate]
            break

    if chosen_col is None:
        raise ValueError(
            f"No usable concentration column found in '{path}'. Available: {sorted(available)}"
        )

    series = df.set_index("country")[chosen_col].astype(float)
    baseline_deaths = None
    if "baseline_deaths_per_year" in df.columns:
        baseline_series = df.set_index("country")["baseline_deaths_per_year"].astype(float)
        baseline_deaths = baseline_series
    if countries_filter:
        countries = [c for c in countries_filter if c in series.index]
        series = series.loc[countries]

    shares = _resolve_electricity_share(electricity_share_cfg, series.index)
    series = series.dropna()
    shares = shares.reindex(series.index).fillna(shares.mean() if not shares.empty else 0.0)
    if baseline_deaths is not None:
        baseline_deaths = baseline_deaths.reindex(series.index).dropna()
        shares = shares.reindex(series.index).fillna(shares.mean() if not shares.empty else 0.0)
    return series, baseline_deaths, shares


def _derive_beta(cfg: Mapping[str, object], pollutant: str) -> float:
    if "beta" in cfg:
        return float(cfg["beta"])
    rr = cfg.get("relative_risk", DEFAULT_RELATIVE_RISK.get(pollutant))
    reference = float(cfg.get("reference_delta", DEFAULT_REFERENCE_DELTA))
    if rr is None:
        raise ValueError(f"Pollutant '{pollutant}' requires either 'beta' or 'relative_risk'.")
    if reference == 0:
        raise ValueError("reference_delta must be non-zero.")
    return math.log(float(rr)) / reference


def _parse_baseline_deaths(baseline_cfg: Mapping[str, object] | None) -> float | None:
    if not baseline_cfg:
        return None
    if not isinstance(baseline_cfg, Mapping):
        raise TypeError("'baseline_deaths' must be a mapping with configuration values.")

    if "per_year" in baseline_cfg:
        return float(baseline_cfg["per_year"])

    total = baseline_cfg.get("total")
    if total is None:
        raise ValueError(
            "Provide either 'per_year' or 'total' inside 'baseline_deaths' configuration."
        )

    years = baseline_cfg.get("years")
    span = baseline_cfg.get("span")
    if years is not None and span is not None:
        raise ValueError("Use either 'years' (list) or 'span' (start/end) to define the period.")

    if years is not None:
        if isinstance(years, str) or not isinstance(years, Iterable):
            raise TypeError("'years' must be an iterable of years.")
        years_list = [int(year) for year in years]
        count = len(years_list)
    elif span is not None:
        if not isinstance(span, Mapping):
            raise TypeError("'span' must be a mapping with 'start' and 'end' years.")
        if "start" not in span or "end" not in span:
            raise ValueError("'span' must include 'start' and 'end' keys.")
        start = int(span["start"])
        end = int(span["end"])
        if start > end:
            raise ValueError("'span.start' must be <= 'span.end'.")
        count = end - start + 1
    else:
        raise ValueError("Specify either 'years' or 'span' when using 'baseline_deaths.total'.")

    if count <= 0:
        raise ValueError("The baseline period must cover at least one year.")

    return float(total) / float(count)


def _parse_weight_config(
    cfg: Mapping[str, object] | str | None, items: Iterable[str]
) -> pd.Series | None:
    items = [str(item) for item in items]
    if not items:
        return None

    if cfg is None:
        return None
    if isinstance(cfg, str):
        if cfg.lower() == "equal":
            return pd.Series(1.0, index=items, dtype=float)
        raise ValueError(f"Unsupported weight shorthand '{cfg}'. Use 'equal' or a mapping.")
    if not isinstance(cfg, Mapping):
        raise TypeError("Weights configuration must be a mapping or the string 'equal'.")

    weights: dict[str, float] = {}
    for key, value in cfg.items():
        if key is None:
            continue
        key_str = str(key)
        if key_str not in items:
            continue
        weights[key_str] = float(value)
    if not weights:
        return None
    series = pd.Series(weights, dtype=float)
    series = series.reindex(items, fill_value=0.0)
    return series


def _resolve_country_weights(
    countries: Iterable[str],
    module_cfg: Mapping[str, object] | str | None,
    pollutant_cfg: Mapping[str, object] | str | None,
) -> pd.Series:
    countries = [str(country) for country in countries]
    if not countries:
        return pd.Series(dtype=float)

    weights = _parse_weight_config(pollutant_cfg, countries)
    if weights is None:
        weights = _parse_weight_config(module_cfg, countries)
    if weights is None:
        weights = pd.Series(1.0, index=countries, dtype=float)

    weights = weights.astype(float).fillna(0.0)
    total = weights.sum()
    if total <= 0.0:
        weights = pd.Series(1.0, index=countries, dtype=float)
        total = weights.sum()
    return weights / total


def _resolve_weights(
    weights_cfg: Mapping[str, object] | str | None, items: Iterable[str]
) -> pd.Series:
    items = [str(item) for item in items]
    if not items:
        return pd.Series(dtype=float)

    weights = _parse_weight_config(weights_cfg, items)
    if weights is None:
        weights = pd.Series(1.0, index=items, dtype=float)

    weights = weights.astype(float).fillna(0.0)
    total = weights.sum()
    if total <= 0.0:
        weights = pd.Series(1.0, index=items, dtype=float)
        total = weights.sum()
    return weights / total


def _weights_from_baseline(
    baseline_series: pd.Series, countries: Iterable[str]
) -> pd.Series | None:
    filtered = baseline_series.reindex(list(countries)).dropna()
    if filtered.empty:
        return None
    total = filtered.sum()
    if not math.isfinite(total) or total <= 0.0:
        return None
    return (filtered / total).astype(float)


def _compute_emission_ratio(
    scenario_series: pd.Series,
    baseline_series: pd.Series,
) -> pd.Series:
    aligned = pd.concat([scenario_series, baseline_series], axis=1, keys=["scenario", "baseline"])
    aligned = aligned.sort_index()
    ratio = np.divide(
        aligned["scenario"],
        aligned["baseline"].replace(0.0, np.nan),
    )
    zero_mask = aligned["baseline"] == 0.0
    ratio = ratio.astype(float)
    ratio.loc[zero_mask & (aligned["scenario"] == 0.0)] = 1.0
    ratio.name = "emission_ratio"
    return ratio


def _compute_delta_fraction(
    scenario_series: pd.Series,
    baseline_series: pd.Series,
) -> pd.Series:
    """Return (scenario - baseline) / baseline with safeguards for zeros."""

    aligned = pd.concat([scenario_series, baseline_series], axis=1, keys=["scenario", "baseline"])
    aligned = aligned.sort_index()
    scen = aligned["scenario"]
    base = aligned["baseline"]
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = (scen - base) / base.replace(0.0, np.nan)
    # If both scenario and baseline are zero, treat the change as zero.
    zero_both = (scen == 0.0) & (base == 0.0)
    frac.loc[zero_both] = 0.0
    # If scenario is zero but baseline is positive, cap at -1 (a 100% reduction).
    zero_scen = (scen == 0.0) & (base > 0.0)
    frac.loc[zero_scen] = -1.0
    frac.name = "delta_fraction"
    return frac.astype(float)


def _build_impacts(
    concentrations: pd.Series,
    emission_ratio: pd.Series,
    delta_fraction: pd.Series,
    beta: float,
    electricity_share: pd.Series,
) -> pd.DataFrame:
    records = []
    for year, ratio in emission_ratio.items():
        delta_frac = delta_fraction.get(year, math.nan)
        if pd.isna(ratio) or pd.isna(delta_frac):
            for country, baseline_conc in concentrations.items():
                records.append(
                    {
                        "country": country,
                        "year": int(year),
                        "baseline_concentration": baseline_conc,
                        "emission_ratio": np.nan,
                        "delta_fraction": np.nan,
                        "new_concentration": np.nan,
                        "delta_concentration": np.nan,
                        "percent_change_mortality": np.nan,
                    }
                )
            continue

        multiplier = float(delta_frac)
        for country, baseline_conc in concentrations.items():
            share_value = electricity_share.get(country, math.nan)
            if pd.isna(share_value):
                share_value = (
                    float(electricity_share.mean()) if not electricity_share.empty else 0.0
                )
            share = max(0.0, min(float(share_value), 1.0))
            delta_conc = baseline_conc * share * multiplier
            new_conc = baseline_conc + delta_conc
            perc_change = math.exp(beta * delta_conc) - 1.0
            records.append(
                {
                    "country": country,
                    "year": int(year),
                    "baseline_concentration": baseline_conc,
                    "emission_ratio": float(ratio),
                    "delta_fraction": multiplier,
                    "new_concentration": new_conc,
                    "delta_concentration": delta_conc,
                    "percent_change_mortality": perc_change,
                }
            )

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame.from_records(records)
    df.sort_values(["country", "year"], inplace=True)
    return df


def _weighted_percent_change(impacts: pd.DataFrame, weights: pd.Series) -> pd.Series:
    if impacts.empty:
        return pd.Series(dtype=float)

    weights = weights.astype(float).fillna(0.0)
    grouped = impacts.groupby("year")
    results: list[tuple[int, float]] = []

    for year, group in grouped:
        group = group.dropna(subset=["percent_change_mortality"])
        if group.empty:
            continue
        values = group.set_index("country")["percent_change_mortality"].astype(float)
        available_weights = weights.reindex(values.index).fillna(0.0)
        if available_weights.sum() <= 0.0:
            available_weights = pd.Series(1.0, index=values.index, dtype=float)
        total = available_weights.sum()
        if total <= 0.0:
            continue
        normalized = available_weights / total
        percent_change = float(np.dot(values.loc[normalized.index], normalized))
        results.append((int(year), percent_change))

    if not results:
        return pd.Series(dtype=float)

    results.sort(key=lambda item: item[0])
    years, values = zip(*results, strict=False)
    return pd.Series(values, index=years, dtype=float)


def _build_death_summary(
    percent_change: pd.Series,
    baseline_deaths_per_year: float,
    *,
    value_of_statistical_life: float | None = None,
    health_costs: HealthCostAssumptions | None = None,
) -> pd.DataFrame:
    if percent_change.empty:
        return pd.DataFrame()

    df = percent_change.sort_index().astype(float).copy().to_frame(name="percent_change_mortality")
    df["baseline_deaths_per_year"] = baseline_deaths_per_year
    df["delta_deaths_per_year"] = df["baseline_deaths_per_year"] * df["percent_change_mortality"]
    df["new_deaths_per_year"] = df["baseline_deaths_per_year"] + df["delta_deaths_per_year"]
    if value_of_statistical_life is not None:
        vsl = float(value_of_statistical_life)
        df["value_of_statistical_life_usd"] = vsl
        df["baseline_value_usd"] = df["baseline_deaths_per_year"] * vsl
        df["delta_value_usd"] = df["delta_deaths_per_year"] * vsl
        df["new_value_usd"] = df["new_deaths_per_year"] * vsl
    _append_health_cost_columns(
        df,
        "delta_deaths_per_year",
        value_of_statistical_life=value_of_statistical_life,
        health_costs=health_costs,
    )
    df.reset_index(inplace=True)
    return df


def _append_health_cost_columns(
    df: pd.DataFrame,
    delta_deaths_column: str,
    *,
    value_of_statistical_life: float | None,
    health_costs: HealthCostAssumptions | None,
) -> None:
    """Add healthcare-cost, income-loss, and total-cost columns in place."""

    if health_costs is None:
        return
    deaths = df[delta_deaths_column].astype(float)
    df["delta_healthcare_cost_usd"] = deaths * health_costs.healthcare_cost_per_death
    df["delta_income_loss_usd"] = deaths * health_costs.income_loss_per_death
    total = df["delta_healthcare_cost_usd"] + df["delta_income_loss_usd"]
    if value_of_statistical_life is not None:
        total = total + deaths * float(value_of_statistical_life)
    df["delta_total_cost_usd"] = total


def _parse_health_costs(cfg: object) -> HealthCostAssumptions | None:
    """Parse the optional ``air_pollution.health_costs`` configuration block."""

    if not cfg:
        return None
    if not isinstance(cfg, Mapping):
        raise TypeError("'air_pollution.health_costs' must be a mapping.")

    known_fields = {
        "healthcare_cost_usd_per_death",
        "income_loss_usd_per_death",
        "morbidity_cases_per_death",
        "healthcare_cost_usd_per_case",
        "income_loss_usd_per_case",
    }
    unknown = set(map(str, cfg)) - known_fields
    if unknown:
        raise ValueError(
            f"Unknown 'air_pollution.health_costs' keys: {sorted(unknown)}. "
            f"Supported keys: {sorted(known_fields)}."
        )

    values: dict[str, float] = {}
    for field in known_fields:
        raw = cfg.get(field)
        if raw is None:
            continue
        value = float(raw)
        if value < 0.0:
            raise ValueError(f"'air_pollution.health_costs.{field}' must be non-negative.")
        values[field] = value
    return HealthCostAssumptions(**values)


def _build_total_mortality_summary(
    pollutant_results: Mapping[str, PollutantImpact],
    baseline_deaths_per_year: float | None,
    weights_cfg: Mapping[str, object] | str | None,
    value_of_statistical_life: float | None,
    *,
    health_costs: HealthCostAssumptions | None = None,
) -> pd.DataFrame | None:
    if baseline_deaths_per_year is None:
        return None

    series_map: dict[str, pd.Series] = {}
    baseline_map: dict[str, float] = {}
    for impact in pollutant_results.values():
        if impact.weighted_percent_change.empty:
            continue
        series_map[impact.pollutant] = impact.weighted_percent_change
        if impact.baseline_deaths_per_year is not None:
            baseline_map[impact.pollutant] = float(impact.baseline_deaths_per_year)

    if not series_map:
        return None

    combined = pd.DataFrame(series_map).sort_index()
    combined = combined.dropna(how="all")

    use_baseline_weights = not weights_cfg and any(value > 0.0 for value in baseline_map.values())
    rows: list[dict[str, float]] = []
    for year, row in combined.iterrows():
        available_pollutants = [col for col, value in row.items() if pd.notna(value)]
        if not available_pollutants:
            continue
        if use_baseline_weights:
            numerator = 0.0
            denom = 0.0
            for pollutant in available_pollutants:
                baseline = baseline_map.get(pollutant)
                if baseline is None or baseline <= 0.0:
                    continue
                numerator += baseline * float(row[pollutant])
                denom += baseline
            if denom <= 0.0:
                continue
            percent_change = numerator / denom
        else:
            weights = _resolve_weights(weights_cfg, available_pollutants)
            values = row[weights.index].to_numpy(dtype=float, copy=True)
            percent_change = float(np.dot(values, weights.to_numpy(dtype=float, copy=True)))
        delta = baseline_deaths_per_year * percent_change
        entry = {
            "year": int(year),
            "percent_change_mortality": percent_change,
            "baseline_deaths_per_year": baseline_deaths_per_year,
            "delta_deaths_per_year": delta,
            "new_deaths_per_year": baseline_deaths_per_year + delta,
        }
        if value_of_statistical_life is not None:
            vsl = float(value_of_statistical_life)
            entry["value_of_statistical_life_usd"] = vsl
            entry["baseline_value_usd"] = baseline_deaths_per_year * vsl
            entry["delta_value_usd"] = delta * vsl
            entry["new_value_usd"] = entry["new_deaths_per_year"] * vsl
        rows.append(entry)

    if not rows:
        return None

    summary = pd.DataFrame(rows).sort_values("year").reset_index(drop=True)
    _append_health_cost_columns(
        summary,
        "delta_deaths_per_year",
        value_of_statistical_life=value_of_statistical_life,
        health_costs=health_costs,
    )
    return summary


def _normalize_country_name(name: object) -> str:
    return re.sub(r"[\s_\-]+", " ", str(name).strip()).casefold()


def _match_country(target: str, candidates: Iterable[str]) -> str | None:
    """Match country labels that differ in separators or length (e.g. 'Bosnia')."""

    normalized = {_normalize_country_name(c): c for c in candidates}
    target_norm = _normalize_country_name(target)
    if target_norm in normalized:
        return normalized[target_norm]
    for candidate_norm, original in normalized.items():
        if candidate_norm.startswith(target_norm) or target_norm.startswith(candidate_norm):
            return original
    return None


def _load_indoor_stats(
    config_dir: Path,
    stats_path: str,
    countries_filter: Iterable[str] | None,
) -> pd.DataFrame:
    """Load per-country indoor (household) air pollution baselines."""

    path = Path(stats_path)
    if not path.is_absolute():
        path = (config_dir / path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Indoor air-pollution statistics file not found: '{path}'.")

    df = pd.read_csv(path)
    required = {"country", "baseline_deaths_per_year"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Indoor statistics file '{path}' is missing columns: {sorted(missing)}.")

    numeric_columns = [
        col for col in ("baseline_deaths_per_year", "base_electrification") if col in df.columns
    ]
    for column in numeric_columns:
        todo_mask = df[column].astype(str).str.strip().str.upper() == "TODO_SOURCE"
        if todo_mask.any():
            todo_countries = sorted(df.loc[todo_mask, "country"].astype(str))
            raise ValueError(
                f"Indoor statistics file '{path}' contains TODO_SOURCE values in "
                f"'{column}' for {todo_countries}; provide data before enabling "
                "the indoor module."
            )
        df[column] = df[column].astype(float)

    df = df.set_index("country")
    if countries_filter:
        matched = [
            country
            for country in df.index
            if any(_match_country(country, [f]) for f in countries_filter)
        ]
        df = df.loc[matched]
    return df


def _resolve_country_electrification(
    result: EmissionScenarioResult | None,
    country: str,
    years: Sequence[int],
) -> pd.Series | None:
    """Return the scenario electrification path for a country, if available."""

    if result is None:
        return None
    series: pd.Series | None = None
    by_country = getattr(result, "electrification_by_country", None)
    if by_country:
        key = _match_country(country, by_country.keys())
        if key is not None:
            series = by_country[key]
    if series is None:
        series = getattr(result, "electrification", None)
    if series is None:
        return None

    series = series.astype(float).sort_index()
    index = pd.Index(sorted({int(year) for year in years}))
    series = series.reindex(index.union(series.index))
    series = series.interpolate(method="index").ffill().bfill()
    return series.reindex(index)


def _build_indoor_results(
    scenario_result: EmissionScenarioResult,
    baseline_result: EmissionScenarioResult | None,
    indoor_stats: pd.DataFrame,
    *,
    value_of_statistical_life: float | None,
    health_costs: HealthCostAssumptions | None,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Return (per-country impacts, aggregated summary) for indoor pollution.

    Indoor (household) exposure is assumed proportional to the non-electrified
    share of households: deaths_t = deaths_0 * (1 - e_t) / (1 - e_0). Static
    demand cases without an electrification path keep constant indoor deaths.
    """

    years = [int(year) for year in scenario_result.years]
    if not years:
        return None, None

    records: list[dict[str, object]] = []
    any_dynamic = False
    for country, row in indoor_stats.iterrows():
        baseline_deaths = float(row["baseline_deaths_per_year"])
        scenario_elec = _resolve_country_electrification(scenario_result, str(country), years)
        baseline_elec = _resolve_country_electrification(baseline_result, str(country), years)
        base_share = row.get("base_electrification")
        base_share = float(base_share) if pd.notna(base_share) else None

        scenario_deaths = _indoor_death_series(baseline_deaths, scenario_elec, base_share, years)
        baseline_series = _indoor_death_series(baseline_deaths, baseline_elec, base_share, years)
        if scenario_elec is not None or baseline_elec is not None:
            any_dynamic = True

        for year in years:
            entry: dict[str, object] = {
                "country": country,
                "year": int(year),
                "electrification_share": (
                    float(scenario_elec.loc[year]) if scenario_elec is not None else np.nan
                ),
                "baseline_indoor_deaths_per_year": float(baseline_series.loc[year]),
                "indoor_deaths_per_year": float(scenario_deaths.loc[year]),
            }
            entry["delta_indoor_deaths_per_year"] = (
                entry["indoor_deaths_per_year"] - entry["baseline_indoor_deaths_per_year"]
            )
            records.append(entry)

    if not records or not any_dynamic:
        # Without any electrification path the indoor burden cannot respond to
        # the scenario; skip rather than reporting all-zero deltas.
        return None, None

    impacts = pd.DataFrame.from_records(records).sort_values(["country", "year"])

    summary = (
        impacts.groupby("year")[
            [
                "baseline_indoor_deaths_per_year",
                "indoor_deaths_per_year",
                "delta_indoor_deaths_per_year",
            ]
        ]
        .sum()
        .sort_index()
        .reset_index()
    )
    if value_of_statistical_life is not None:
        vsl = float(value_of_statistical_life)
        summary["value_of_statistical_life_usd"] = vsl
        summary["delta_value_usd"] = summary["delta_indoor_deaths_per_year"] * vsl
    _append_health_cost_columns(
        summary,
        "delta_indoor_deaths_per_year",
        value_of_statistical_life=value_of_statistical_life,
        health_costs=health_costs,
    )
    return impacts, summary


def _indoor_death_series(
    baseline_deaths: float,
    electrification: pd.Series | None,
    base_share: float | None,
    years: Sequence[int],
) -> pd.Series:
    """Scale baseline indoor deaths by the non-electrified household share."""

    index = pd.Index([int(year) for year in years])
    if electrification is None:
        return pd.Series(baseline_deaths, index=index, dtype=float)

    reference = base_share
    if reference is None:
        reference = float(electrification.iloc[0])
    non_electric_base = 1.0 - reference
    if non_electric_base <= 0.0:
        # Fully electrified reference: no indoor solid-fuel burden remains.
        return pd.Series(0.0, index=index, dtype=float)
    ratio = ((1.0 - electrification) / non_electric_base).clip(lower=0.0)
    return (baseline_deaths * ratio).reindex(index).astype(float)


def _fold_indoor_into_total(
    total_summary: pd.DataFrame,
    indoor_summary: pd.DataFrame | None,
) -> pd.DataFrame:
    """Append indoor mortality columns to the combined ambient summary."""

    if indoor_summary is None or indoor_summary.empty:
        return total_summary
    indoor_delta = indoor_summary.set_index("year")["delta_indoor_deaths_per_year"]
    total_summary = total_summary.copy()
    total_summary["delta_indoor_deaths_per_year"] = (
        total_summary["year"].map(indoor_delta).fillna(0.0)
    )
    total_summary["delta_deaths_total_per_year"] = (
        total_summary["delta_deaths_per_year"] + total_summary["delta_indoor_deaths_per_year"]
    )
    return total_summary


def _build_health_cost_summary(
    total_summary: pd.DataFrame | None,
    indoor_summary: pd.DataFrame | None,
    *,
    value_of_statistical_life: float | None,
    health_costs: HealthCostAssumptions | None,
) -> pd.DataFrame | None:
    """Combine ambient and indoor mortality deltas into a monetised summary."""

    if total_summary is None and indoor_summary is None:
        return None
    if value_of_statistical_life is None and health_costs is None:
        return None

    ambient = (
        total_summary.set_index("year")["delta_deaths_per_year"]
        if total_summary is not None
        else pd.Series(dtype=float)
    )
    indoor = (
        indoor_summary.set_index("year")["delta_indoor_deaths_per_year"]
        if indoor_summary is not None
        else pd.Series(dtype=float)
    )
    years = sorted(set(ambient.index) | set(indoor.index))
    if not years:
        return None

    df = pd.DataFrame({"year": [int(year) for year in years]})
    df["ambient_delta_deaths_per_year"] = df["year"].map(ambient).fillna(0.0)
    df["indoor_delta_deaths_per_year"] = df["year"].map(indoor).fillna(0.0)
    df["total_delta_deaths_per_year"] = (
        df["ambient_delta_deaths_per_year"] + df["indoor_delta_deaths_per_year"]
    )
    if value_of_statistical_life is not None:
        df["delta_value_usd"] = df["total_delta_deaths_per_year"] * float(value_of_statistical_life)
    _append_health_cost_columns(
        df,
        "total_delta_deaths_per_year",
        value_of_statistical_life=value_of_statistical_life,
        health_costs=health_costs,
    )
    return df


def _write_output(output_dir: Path, scenario: str, pollutant: str, impacts: pd.DataFrame) -> None:
    scenario_dir = output_dir / scenario
    scenario_dir.mkdir(parents=True, exist_ok=True)
    path = scenario_dir / f"{pollutant}_health_impact.csv"
    impacts.to_csv(path, index=False)


def _write_concentration_summary(
    output_dir: Path, scenario: str, pollutant: str, impacts: pd.DataFrame, weights: pd.Series
) -> None:
    if impacts.empty:
        return
    scenario_dir = output_dir / scenario
    scenario_dir.mkdir(parents=True, exist_ok=True)
    path = scenario_dir / f"{pollutant}_concentration_summary.csv"

    weights = weights.astype(float).fillna(0.0)
    if weights.sum() <= 0.0:
        weights = pd.Series(1.0, index=weights.index)
    weights = weights / weights.sum()

    rows: list[dict[str, float | str]] = []
    for year, group in impacts.groupby("year"):
        for _, row in group.iterrows():
            country = row["country"]
            rows.append(
                {
                    "year": int(year),
                    "country": country,
                    "weight": float(weights[country]) if country in weights else 0.0,
                    "baseline_concentration_micro_g_per_m3": float(row["baseline_concentration"]),
                    "new_concentration_micro_g_per_m3": float(row["new_concentration"]),
                    "delta_concentration_micro_g_per_m3": float(row["delta_concentration"]),
                }
            )
    if rows:
        pd.DataFrame(rows).sort_values(["year", "country"]).to_csv(path, index=False)


def _write_mortality_summary(
    output_dir: Path, scenario: str, pollutant: str, summary: pd.DataFrame
) -> None:
    scenario_dir = output_dir / scenario
    scenario_dir.mkdir(parents=True, exist_ok=True)
    path = scenario_dir / f"{pollutant}_mortality_summary.csv"
    summary.to_csv(path, index=False)


def _write_total_mortality_summary(output_dir: Path, scenario: str, summary: pd.DataFrame) -> None:
    scenario_dir = output_dir / scenario
    scenario_dir.mkdir(parents=True, exist_ok=True)
    path = scenario_dir / "total_mortality_summary.csv"
    summary.to_csv(path, index=False)


def _write_indoor_health_impact(output_dir: Path, scenario: str, impacts: pd.DataFrame) -> None:
    scenario_dir = output_dir / scenario
    scenario_dir.mkdir(parents=True, exist_ok=True)
    path = scenario_dir / "indoor_health_impact.csv"
    impacts.to_csv(path, index=False)


def _write_indoor_mortality_summary(output_dir: Path, scenario: str, summary: pd.DataFrame) -> None:
    scenario_dir = output_dir / scenario
    scenario_dir.mkdir(parents=True, exist_ok=True)
    path = scenario_dir / "indoor_mortality_summary.csv"
    summary.to_csv(path, index=False)


def _write_health_cost_summary(output_dir: Path, scenario: str, summary: pd.DataFrame) -> None:
    scenario_dir = output_dir / scenario
    scenario_dir.mkdir(parents=True, exist_ok=True)
    path = scenario_dir / "health_cost_summary.csv"
    summary.to_csv(path, index=False)
