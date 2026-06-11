"""Calculate electricity-sector emissions from demand/mix scenarios.

Results include per-pollutant (CO₂, SO₂, NOₓ, PM₂.₅) emissions in Mt/year. The
climate module consumes the CO₂ deltas, while the additional pollutants support
air-quality analysis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
import yaml

from .constants import BASE_DEMAND_CASE, BASE_MIX_CASE, POLLUTANTS
from .demand_model import build_dynamic_demand_components

LOGGER = logging.getLogger("calc_emissions")
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    LOGGER.addHandler(handler)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


def compose_scenario_name(demand_case: str, mix_case: str) -> str:
    """Return the canonical scenario identifier."""
    return f"{mix_case}__{demand_case}"


@dataclass
class EmissionScenarioResult:
    """Result container for an emission scenario."""

    name: str
    demand_case: str
    mix_case: str
    years: list[int]
    demand_twh: pd.Series
    generation_twh: pd.DataFrame
    technology_emissions_mt: dict[str, pd.DataFrame]
    total_emissions_mt: dict[str, pd.Series]
    delta_mtco2: pd.Series
    # Electrification share series from the dynamic demand model (None for
    # static demand cases). Per-country mapping is populated by aggregators.
    electrification: pd.Series | None = None
    electrification_by_country: dict[str, pd.Series] | None = None


def run_from_config(
    config_path: Path | str = "config.yaml",
    *,
    default_years: Mapping[str, float | int] | None = None,
    results_run_directory: str | None = None,
    allowed_demand_cases: Sequence[str] | None = None,
    allowed_mix_cases: Sequence[str] | None = None,
    baseline_mix_case: str | None = None,
    delta_baseline_mode: str | None = None,
) -> dict[str, EmissionScenarioResult]:
    """Run emission calculations based on ``config.yaml`` and write delta CSVs."""
    from config_paths import get_config_path  # local import to avoid cycle

    config_path = Path(config_path) if config_path is not None else get_config_path()
    with config_path.open() as handle:
        config = yaml.safe_load(handle) or {}

    module_cfg = config.get("calc_emissions", {})
    if not module_cfg:
        raise ValueError("'calc_emissions' section missing from config.yaml")

    fallback_years = default_years
    if fallback_years is None:
        fallback_years = _discover_global_horizon(config_path)
    years = _generate_years(module_cfg.get("years"), fallback_years)

    ef_setting = module_cfg.get("emission_factors_file")
    if not ef_setting:
        raise ValueError(
            "'emission_factors_file' must be set in the country config and point to "
            "a file in data/calc_emissions/emission_factors"
        )
    ef_candidates: list[Path] = []
    ef_path = Path(ef_setting)
    if ef_path.is_absolute():
        ef_candidates.append(ef_path)
    else:
        ef_candidates.append((config_path.parent / ef_path).resolve())
    repo_root = Path(__file__).resolve().parents[2]
    ef_candidates.append(repo_root / "data" / "calc_emissions" / "emission_factors" / ef_path.name)
    existing = next((candidate for candidate in ef_candidates if candidate.exists()), None)
    if existing is None:
        raise FileNotFoundError(
            "Emission factors file not found. Checked absolute path, relative to the "
            f"country config, and data/calc_emissions/emission_factors/{ef_path.name}"
        )
    emission_factors = _load_emission_factors(existing)

    not bool(module_cfg.get("demand_scenarios"))
    demand_scenarios = module_cfg.get("demand_scenarios") or {}
    if not isinstance(demand_scenarios, Mapping) or not demand_scenarios:
        demand_scenarios = {}
        baseline_cfg = module_cfg.get("baseline", {})
        baseline_values = (
            baseline_cfg.get("demand_custom") if isinstance(baseline_cfg, Mapping) else None
        )
        baseline_case = (
            str(module_cfg.get("baseline_demand_case", BASE_DEMAND_CASE)).strip()
            or BASE_DEMAND_CASE
        )
        if isinstance(baseline_values, Mapping) and baseline_values:
            demand_scenarios[baseline_case] = {"values": dict(baseline_values)}
        for scenario in module_cfg.get("scenarios", []) or []:
            if not isinstance(scenario, Mapping):
                continue
            name = scenario.get("name")
            demand_custom = scenario.get("demand_custom")
            if not name or not isinstance(demand_custom, Mapping):
                continue
            demand_scenarios[str(name)] = {"values": dict(demand_custom)}
        if not demand_scenarios:
            raise ValueError("'demand_scenarios' must define at least one demand case.")
    demand_scenarios = dict(demand_scenarios)
    _maybe_add_mean_demand_case(demand_scenarios)
    if allowed_demand_cases:
        allowed = [str(case).strip() for case in allowed_demand_cases if str(case).strip()]
        missing = [case for case in allowed if case not in demand_scenarios]
        if missing:
            raise KeyError(f"Demand scenarios missing required cases: {missing}")
        demand_scenarios = {case: demand_scenarios[case] for case in allowed}
    baseline_case = (
        str(module_cfg.get("baseline_demand_case", BASE_DEMAND_CASE)).strip() or BASE_DEMAND_CASE
    )
    delta_mode = (
        str(delta_baseline_mode or module_cfg.get("delta_baseline_mode", "per_mix")).strip().lower()
        or "per_mix"
    )
    if delta_mode not in {"per_mix", "global"}:
        raise ValueError("calc_emissions.delta_baseline_mode must be 'per_mix' or 'global'.")
    if baseline_case not in demand_scenarios:
        raise ValueError(
            f"Demand scenarios must include '{baseline_case}'. "
            "Set calc_emissions.baseline_demand_case if a different label is required."
        )

    synthetic_mix = not bool(module_cfg.get("mix_scenarios"))
    scenario_mix_overrides: dict[str, Mapping[str, float]] = {}
    mix_scenarios = module_cfg.get("mix_scenarios") or {}
    if not isinstance(mix_scenarios, Mapping) or not mix_scenarios:
        mix_scenarios = {}
        baseline_cfg = module_cfg.get("baseline", {})
        baseline_mix = baseline_cfg.get("mix_custom") if isinstance(baseline_cfg, Mapping) else None
        baseline_shares = None
        if isinstance(baseline_mix, Mapping) and baseline_mix:
            baseline_shares = baseline_mix.get("shares", baseline_mix)
        if baseline_shares:
            mix_scenarios["baseline_mix"] = {"shares": baseline_shares}
        for scenario in module_cfg.get("scenarios", []) or []:
            if not isinstance(scenario, Mapping):
                continue
            name = scenario.get("name")
            mix_custom = scenario.get("mix_custom")
            if not name or not isinstance(mix_custom, Mapping):
                continue
            shares = mix_custom.get("shares", mix_custom)
            if shares:
                scenario_mix_overrides[str(name)] = shares
        if not mix_scenarios:
            raise ValueError("'mix_scenarios' must define at least one mix case.")
    if allowed_mix_cases:
        allowed = [str(case).strip() for case in allowed_mix_cases if str(case).strip()]
        missing = [case for case in allowed if case not in mix_scenarios]
        if missing:
            raise KeyError(f"Mix scenarios missing required cases: {missing}")
        mix_scenarios = {case: mix_scenarios[case] for case in allowed}
    raw_baseline_mix = (
        baseline_mix_case if baseline_mix_case is not None else module_cfg.get("baseline_mix_case")
    )
    baseline_mix = str(raw_baseline_mix).strip() if raw_baseline_mix is not None else ""
    if not baseline_mix:
        baseline_mix = BASE_MIX_CASE

    if baseline_mix not in mix_scenarios:
        # If an explicit baseline mix was provided, fail loudly.
        if raw_baseline_mix is not None:
            available = ", ".join(sorted(mix_scenarios)) or "<none>"
            raise ValueError(
                f"Baseline mix '{baseline_mix}' is not defined in mix_scenarios. "
                f"Available mixes: {available}. "
                f"Update calc_emissions.baseline_mix_case or mix_scenarios."
            )

        # Otherwise, fall back to common synthetic/default cases.
        if "baseline_mix" in mix_scenarios:
            baseline_mix = "baseline_mix"
        elif len(mix_scenarios) == 1:
            baseline_mix = next(iter(mix_scenarios))
        else:
            available = ", ".join(sorted(mix_scenarios)) or "<none>"
            raise ValueError(
                f"Baseline mix '{baseline_mix}' is not defined in mix_scenarios. "
                f"Available mixes: {available}. Set calc_emissions.baseline_mix_case explicitly."
            )

    results: dict[str, EmissionScenarioResult] = {}
    per_mix_results: dict[str, dict[str, EmissionScenarioResult]] = {}

    for mix_name in mix_scenarios:
        LOGGER.info("Calculating mix scenario '%s'", mix_name)
        mix_cfg = mix_scenarios.get(mix_name, {})
        if isinstance(mix_cfg, dict) and mix_cfg.get("type") == "timeseries" and "csv" in mix_cfg:
            mix_csv_path = _locate_mix_csv(config_path, mix_cfg["csv"])
            mix_ts = _load_mix_timeseries(mix_csv_path)
            mix_ts.columns = [c.strip().lower() for c in mix_ts.columns.astype(str)]
            mix_ts = mix_ts.reindex(years)
            # Align with requested years, interpolate between known points,
            # then hold the tail constant.
            mix_ts = mix_ts.interpolate(method="index")
            mix_ts = mix_ts.ffill().bfill()
            mix_ts = mix_ts.fillna(0.0)
            for t in emission_factors.index:
                if t not in mix_ts.columns:
                    mix_ts[t] = 0.0
            mix_ts = mix_ts[list(emission_factors.index)]
            mix_ts = mix_ts.div(mix_ts.sum(axis=1).replace(0, 1.0), axis=0)
            mix_shares = mix_ts
        else:
            mix_shares = _resolve_mix_shares(
                years,
                mix_scenarios,
                emission_factors.index,
                mix_name,
                None,
            )

        per_mix_results[mix_name] = {}
        for demand_name in demand_scenarios:
            LOGGER.info("  • demand case '%s'", demand_name)
            demand_series, electrification = _resolve_demand_series(
                years,
                demand_scenarios,
                demand_name,
                None,
                config_path=config_path,
            )
            mix_shares_current = mix_shares
            if synthetic_mix and demand_name in scenario_mix_overrides:
                mix_shares_current = _resolve_mix_shares(
                    years,
                    {mix_name: {"shares": scenario_mix_overrides[demand_name]}},
                    emission_factors.index,
                    mix_name,
                    None,
                )
            scenario_id = compose_scenario_name(demand_name, mix_name)
            scenario_result = calculate_emissions(
                name=scenario_id,
                demand_series=demand_series,
                mix_shares=mix_shares_current,
                emission_factors=emission_factors,
                demand_case=demand_name,
                mix_case=mix_name,
                electrification=electrification,
            )
            per_mix_results[mix_name][demand_name] = scenario_result
            results[scenario_id] = scenario_result

        if delta_mode == "per_mix":
            _assign_mix_deltas(per_mix_results[mix_name], baseline_case, years)
    if delta_mode == "global":
        _assign_global_deltas(results, baseline_mix, baseline_case, years)
    if synthetic_mix:
        # Provide alias keys using demand-case names for compatibility with consumers
        for res in list(results.values()):
            alias = res.demand_case
            if alias in results:
                continue
            results[alias] = EmissionScenarioResult(
                name=alias,
                demand_case=res.demand_case,
                mix_case=res.mix_case,
                years=list(res.years),
                demand_twh=res.demand_twh.copy(),
                generation_twh=res.generation_twh.copy(),
                technology_emissions_mt={
                    k: df.copy() for k, df in res.technology_emissions_mt.items()
                },
                total_emissions_mt={k: s.copy() for k, s in res.total_emissions_mt.items()},
                delta_mtco2=res.delta_mtco2.copy(),
                electrification=(
                    res.electrification.copy() if res.electrification is not None else None
                ),
            )
    return results


def calculate_emissions(
    name: str,
    demand_series: pd.Series,
    mix_shares: pd.DataFrame,
    emission_factors: pd.DataFrame,
    *,
    demand_case: str,
    mix_case: str,
    electrification: pd.Series | None = None,
) -> EmissionScenarioResult:
    """Calculate generation and emissions for a single scenario."""
    if not isinstance(demand_series.index, pd.Index):
        raise TypeError("demand_series must be a pandas Series with year index")

    # demand_series is in TWh and mix_shares are per-technology fractions
    # (summing to 1 per year). Keep generation in TWh so that multiplying by
    # emission factors (which are expressed per TWh) yields Mt directly.
    generation = mix_shares.multiply(demand_series, axis=0)

    technology_emissions: dict[str, pd.DataFrame] = {}
    total_emissions: dict[str, pd.Series] = {}

    for pollutant, _meta in POLLUTANTS.items():
        if pollutant not in emission_factors.columns:
            continue
        factors = emission_factors[pollutant]
        emissions = generation.mul(factors, axis=1)
        technology_emissions[pollutant] = emissions
        total_emissions[pollutant] = emissions.sum(axis=1)

    if "co2" not in total_emissions:
        raise ValueError("Emission factors must include CO₂ intensities.")

    co2_series = total_emissions["co2"]

    return EmissionScenarioResult(
        name=name,
        demand_case=demand_case,
        mix_case=mix_case,
        years=list(demand_series.index.astype(int)),
        demand_twh=demand_series,
        generation_twh=generation,
        technology_emissions_mt=technology_emissions,
        total_emissions_mt=total_emissions,
        delta_mtco2=pd.Series(
            np.zeros_like(co2_series.values), index=co2_series.index, dtype=float
        ),
        electrification=electrification,
    )


def _assign_mix_deltas(
    demand_map: Mapping[str, EmissionScenarioResult],
    baseline_case: str,
    years: Sequence[int],
) -> None:
    baseline_res = demand_map.get(baseline_case)
    if baseline_res is None:
        raise ValueError(f"Missing baseline demand '{baseline_case}' for mix.")
    baseline_co2 = baseline_res.total_emissions_mt.get("co2")
    if baseline_co2 is None:
        index = pd.Index([int(y) for y in years])
        baseline_co2 = pd.Series(np.zeros(len(index)), index=index, dtype=float)
        baseline_res.total_emissions_mt["co2"] = baseline_co2

    for scenario in demand_map.values():
        totals = scenario.total_emissions_mt.get("co2")
        if totals is None:
            totals = pd.Series(np.zeros(len(baseline_co2)), index=baseline_co2.index, dtype=float)
        else:
            totals = totals.reindex(baseline_co2.index, fill_value=0.0)
        scenario.total_emissions_mt["co2"] = totals
        scenario.delta_mtco2 = totals - baseline_co2


def _assign_global_deltas(
    results: Mapping[str, EmissionScenarioResult],
    baseline_mix: str,
    baseline_case: str,
    years: Sequence[int],
) -> None:
    baseline_key = compose_scenario_name(baseline_case, baseline_mix)
    baseline_res = results.get(baseline_key)
    if baseline_res is None:
        raise ValueError(f"Missing baseline scenario '{baseline_key}' required for global deltas.")
    baseline_co2 = baseline_res.total_emissions_mt.get("co2")
    if baseline_co2 is None:
        index = pd.Index([int(y) for y in years])
        baseline_co2 = pd.Series(np.zeros(len(index)), index=index, dtype=float)
        baseline_res.total_emissions_mt["co2"] = baseline_co2

    for scenario in results.values():
        totals = scenario.total_emissions_mt.get("co2")
        if totals is None:
            totals = pd.Series(np.zeros(len(baseline_co2)), index=baseline_co2.index, dtype=float)
        else:
            totals = totals.reindex(baseline_co2.index, fill_value=0.0)
        scenario.total_emissions_mt["co2"] = totals
        scenario.delta_mtco2 = totals - baseline_co2


def _maybe_add_mean_demand_case(demand_scenarios: dict[str, Mapping[str, object]]) -> None:
    lower = demand_scenarios.get("scen1_lower")
    upper = demand_scenarios.get("scen1_upper")
    mean_key = "scen1_mean"
    if mean_key in demand_scenarios or lower is None or upper is None:
        return
    lower_values = lower.get("values")
    upper_values = upper.get("values")
    if not isinstance(lower_values, Mapping) or not isinstance(upper_values, Mapping):
        return
    averaged = _average_demand_maps(lower_values, upper_values)
    if not averaged:
        return
    demand_scenarios[mean_key] = {
        "description": "Average of scen1_lower and scen1_upper demand cases.",
        "values": averaged,
    }


def _average_demand_maps(
    lower: Mapping[str, float | int],
    upper: Mapping[str, float | int],
) -> dict[int, float]:
    normalized_lower = _normalize_demand_map(lower)
    normalized_upper = _normalize_demand_map(upper)
    combined_years = sorted(set(normalized_lower) | set(normalized_upper))
    averaged: dict[int, float] = {}
    for year in combined_years:
        lower_val = normalized_lower.get(year)
        upper_val = normalized_upper.get(year)
        if lower_val is None and upper_val is None:
            continue
        if lower_val is None:
            averaged[year] = upper_val  # type: ignore[assignment]
        elif upper_val is None:
            averaged[year] = lower_val
        else:
            averaged[year] = (lower_val + upper_val) / 2.0
    return averaged


def _normalize_demand_map(values: Mapping[str, float | int]) -> dict[int, float]:
    normalized: dict[int, float] = {}
    for key, value in values.items():
        try:
            normalized[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return normalized


def _discover_global_horizon(config_path: Path) -> Mapping[str, float | int] | None:
    """Search parent directories for a root config containing ``time_horizon``."""

    for parent in [config_path.parent, *config_path.parents]:
        candidate = parent / "config.yaml"
        if not candidate.exists():
            continue
        try:
            with candidate.open() as handle:
                config = yaml.safe_load(handle) or {}
        except Exception:  # pragma: no cover - defensive against malformed configs
            continue
        horizon = config.get("time_horizon")
        if isinstance(horizon, Mapping):
            return horizon
    return None


def _generate_years(
    cfg: Mapping[str, float | int] | None,
    fallback: Mapping[str, float | int] | None = None,
) -> list[int]:
    values: dict[str, float | int] = {"start": 2025, "end": 2100, "step": 5}
    for source in (fallback, cfg):
        if not source:
            continue
        for key in ("start", "end", "step"):
            if key in source:
                values[key] = source[key]

    start = int(values["start"])
    end = int(values["end"])
    step = int(values["step"])
    if start >= end:
        raise ValueError("'start' year must be less than 'end' year")
    if step <= 0:
        raise ValueError("'step' must be positive")
    years = list(range(start, end + 1, step))
    return years


def _load_emission_factors(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Emission factors file not found: {path}")
    df = pd.read_csv(path, comment="#")
    if "technology" not in df.columns:
        raise ValueError("Emission factors CSV must contain a 'technology' column.")
    df["technology"] = df["technology"].str.strip().str.lower()
    factors: dict[str, pd.Series] = {}
    for pollutant, meta in POLLUTANTS.items():
        aliases = meta["aliases"]
        selected_series = None
        conversion = None
        for column, scale in aliases.items():
            if column in df.columns:
                selected_series = df.set_index("technology")[column].astype(float)
                conversion = float(scale)
                break
        if selected_series is None:
            continue
        factors[pollutant] = selected_series * conversion

    if "co2" not in factors:
        available = ", ".join(df.columns)
        raise ValueError(
            "Emission factors must provide a CO₂ intensity column. "
            f"Supported aliases include: {list(POLLUTANTS['co2']['aliases'])}. "
            f"Available columns: {available}"
        )

    factor_df = pd.DataFrame(factors)
    factor_df = factor_df.loc[df["technology"].tolist()]
    return factor_df


def _resolve_demand_series(
    years: Sequence[int],
    scenarios: Mapping[str, Mapping[str, Mapping]],
    scenario_name: str | None,
    custom: Mapping[int, float] | None,
    *,
    config_path: Path | None = None,
) -> tuple[pd.Series, pd.Series | None]:
    if scenario_name:
        scenario = scenarios.get(scenario_name)
        if scenario is None:
            raise ValueError(f"Demand scenario '{scenario_name}' not found in config.")
        dynamic_model = scenario.get("dynamic_model")
        if dynamic_model is not None:
            if config_path is None:
                raise ValueError("config_path is required for dynamic demand scenarios.")
            if not isinstance(dynamic_model, Mapping):
                raise TypeError(
                    f"Demand scenario '{scenario_name}' dynamic_model must be a mapping."
                )
            return build_dynamic_demand_components(
                dynamic_model,
                years,
                config_path=config_path,
            )
        values = scenario.get("values")
        if not values:
            raise ValueError(f"Demand scenario '{scenario_name}' must define 'values'.")
    elif custom:
        values = custom
    else:
        raise ValueError("Provide either 'demand_scenario' or 'demand_custom'.")

    series = _values_to_series(values, years, "demand")
    return series.rename("demand_twh"), None


def _resolve_mix_shares(
    years: Sequence[int],
    scenarios: Mapping[str, Mapping[str, Mapping]],
    technologies: Iterable[str],
    scenario_name: str | None,
    custom: Mapping[str, Mapping] | None,
) -> pd.DataFrame:
    tech_list = [tech.lower() for tech in technologies]
    if scenario_name:
        scenario = scenarios.get(scenario_name)
        if scenario is None:
            raise ValueError(f"Mix scenario '{scenario_name}' not found in config.")
        shares_cfg = scenario.get("shares")
        if shares_cfg is None:
            raise ValueError(f"Mix scenario '{scenario_name}' must define 'shares'.")
    elif custom:
        shares_cfg = custom.get("shares", {})
    else:
        raise ValueError("Provide either 'mix_scenario' or 'mix_custom'.")

    data = {}
    for tech in tech_list:
        entry = shares_cfg.get(tech)
        if entry is None:
            data[tech] = np.zeros(len(years))
            continue
        if isinstance(entry, Mapping):
            series = _values_to_series(entry, years, f"mix share for {tech}")
        else:
            series = pd.Series([float(entry)] * len(years), index=years)
        data[tech] = series.values

    mix_df = pd.DataFrame(data, index=years)
    totals = mix_df.sum(axis=1)
    if np.any(totals == 0):
        raise ValueError("Mix shares sum to zero for at least one year; cannot normalise.")
    mix_df = mix_df.divide(totals, axis=0)
    return mix_df


def _load_mix_timeseries(csv_path):
    """
    Load a mix timeseries CSV. Returns a pd.DataFrame indexed by year (int),
    columns are technology keys (strings), values are shares (floats, sum ~1).
    """
    # resolve to an absolute path using pathlib (avoid undefined helper `abspath`)
    csv_path = Path(csv_path).resolve()
    df = pd.read_csv(csv_path)

    # set year index (prefer explicit "year" column)
    df = df.set_index("year") if "year" in df.columns else df.set_index(df.columns[0])

    # ensure integer year index with clear error if conversion fails
    df.index = pd.to_numeric(df.index, errors="raise").astype(int)

    # normalize column names: strip, lowercase, replace common separators
    def normalize_col(s: str) -> str:
        s = str(s).strip().lower()
        s = s.replace("-", "_").replace(" ", "_")
        return s

    cols = [normalize_col(c) for c in df.columns]

    alias_map = {
        "other_res": "biomass",
        # add other aliases here, e.g. "pv": "solar", "wind_onshore": "wind"
    }

    mapped_cols = [alias_map.get(c, c) for c in cols]
    df.columns = mapped_cols

    # optional validation: ensure shares are numeric and rows sum to ~1
    df = df.apply(pd.to_numeric, errors="raise")
    row_sums = df.sum(axis=1)
    bad = (row_sums - 1.0).abs() > 1e-2
    if bad.any():
        LOGGER.warning(
            "Mix timeseries rows do not sum to 1.0 within tolerance (±0.01) for years: %s. "
            "Rows will be normalized.",
            ", ".join(map(str, row_sums.index[bad].astype(int))),
        )
    return df


def _locate_mix_csv(config_path: Path, csv_setting: str) -> str:
    """Locate a mix timeseries CSV file.

    Resolution order:
      1. If csv_setting is absolute and exists, return it.
      2. If csv_setting is relative, resolve relative to the country config directory.
      3. Fall back to repo_root/data/calc_emissions/electricity_mixes/<basename>.
    """
    p = Path(csv_setting)
    # 1: absolute
    if p.is_absolute() and p.exists():
        return str(p)
    # 2: relative to config
    candidate = (config_path.parent / p).resolve()
    if candidate.exists():
        return str(candidate)
    # 3: repo data directory
    repo_root = Path(__file__).resolve().parents[2]
    candidate = repo_root / "data" / "calc_emissions" / "electricity_mixes" / p.name
    if candidate.exists():
        return str(candidate)
    raise FileNotFoundError(
        "Mix timeseries CSV not found: tried "
        f"{csv_setting} and data/calc_emissions/electricity_mixes/{p.name}"
    )


def _resolve_generation_by_technology(
    demand_series, mix_cfg, available_years, emission_factors_index
):
    """
    demand_series: pd.Series indexed by year (TWh)
    mix_cfg: dict describing the mix (either has 'shares' dict for constant shares
             or has 'csv' path for timeseries)
    available_years: list/int index we want to compute for
    emission_factors_index: list of technology names expected
    Returns: pd.DataFrame generation_twh indexed by year, cols=technologies
    """
    # initialize empty DataFrame
    techs = list(emission_factors_index)
    gen_df = pd.DataFrame(0.0, index=available_years, columns=techs)

    if isinstance(mix_cfg, dict) and mix_cfg.get("type") == "timeseries" and "csv" in mix_cfg:
        mix_ts = _load_mix_timeseries(mix_cfg["csv"])
        # Align with requested years, interpolate between known points, and hold the tail constant
        mix_ts = mix_ts.reindex(available_years)
        mix_ts = mix_ts.interpolate(method="index")
        mix_ts = mix_ts.ffill().bfill()
        # Ensure columns for all technologies exist (missing -> 0)
        for t in techs:
            if t not in mix_ts.columns:
                mix_ts[t] = 0.0
        # Normalize per-year (if desired)
        mix_ts = mix_ts[techs]
        mix_ts = mix_ts.div(mix_ts.sum(axis=1).replace(0, 1.0), axis=0)
        # Multiply demand for each year by the share row
        for y in available_years:
            demand_val = demand_series.get(y, 0.0)
            gen_df.loc[y] = mix_ts.loc[y].values * demand_val
    else:
        # legacy constant shares handling (existing behavior)
        shares = mix_cfg["shares"] if isinstance(mix_cfg, dict) and "shares" in mix_cfg else mix_cfg
        shares = shares or {}
        # ensure share values for all technologies (missing -> 0)
        for t in techs:
            s = shares.get(t, 0.0)
            gen_df[t] = demand_series * float(s)
    return gen_df


def _values_to_series(
    values: Mapping[int | str, float],
    years: Sequence[int],
    label: str,
) -> pd.Series:
    mapping = {}
    for key, value in values.items():
        mapping[int(key)] = float(value)
    if not mapping:
        raise ValueError(f"No values provided for {label}.")
    series = pd.Series(mapping, dtype=float).sort_index()
    idx = pd.Index(sorted(set(years)))
    series = series.reindex(idx.union(series.index), method=None)
    series = series.interpolate(method="index").ffill().bfill()
    return series.reindex(idx)
