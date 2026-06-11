"""Generate configurable cross-module summaries (metrics + plots)."""

from __future__ import annotations

import contextlib
import logging
import math
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from calc_emissions.constants import BASE_DEMAND_CASE, BASE_MIX_CASE
from calc_emissions.scenario_io import (
    list_available_scenarios,
    load_scenario_absolute,
    load_scenario_delta,
    split_scenario_name,
)
from config_paths import apply_results_run_directory, get_results_run_directory
from economic_module.scc import _load_ssp_economic_data, damage_dice
from economic_module.socioeconomics import DiceSocioeconomics

LOGGER = logging.getLogger("results.summary")

AVAILABLE_METHODS: tuple[str, ...] = ("constant_discount", "ramsey_discount")
MM_PER_DAY_TO_YEAR = 365.0


@dataclass(slots=True)
class SummarySettings:
    years: list[int]
    output_directory: Path
    include_plots: bool = True
    plot_format: str = "png"
    baseline_mix_case: str = BASE_MIX_CASE
    year_periods: list[tuple[int, int]] = field(default_factory=list)
    aggregation_mode: str = "per_year"
    aggregation_horizon: tuple[int, int] | None = None
    plot_start: int = 2025
    plot_end: int = 2100
    climate_labels: list[str] = field(default_factory=list)
    run_method: str = "pulse"
    scc_output_directory: Path | None = None
    base_year: int = 2025
    climate_output_directory: Path | None = None
    gdp_series: dict[str, dict[int, float]] | None = None
    population_series: dict[str, dict[int, float]] | None = None
    socio_frames: dict[str, pd.DataFrame] | None = None
    per_country_emission_root: Path | None = None
    pattern_output_directory: Path | None = None
    pattern_countries: list[str] | None = None
    air_output_directory: Path | None = None
    air_pollution_pollutants: list[str] = field(default_factory=list)
    air_pollution_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    air_pollution_country_baseline: dict[str, float] = field(default_factory=dict)
    air_pollution_countries: list[str] = field(default_factory=list)
    air_pollution_vsl: float | None = None
    damage_function_cfg: Mapping[str, object] | None = None
    extreme_weather_costs_file: Path | None = None
    aggregate_emission_root: Path | None = None


@dataclass(slots=True)
class ScenarioMetrics:
    emission_delta_mt: dict[int, float] = field(default_factory=dict)
    temperature_delta_c: dict[int, float] = field(default_factory=dict)
    mortality_delta: dict[int, float] = field(default_factory=dict)
    mortality_percent: dict[int, float] = field(default_factory=dict)
    mortality_baseline: dict[int, float] = field(default_factory=dict)
    mortality_value_delta: dict[int, float] | None = None
    mortality_delta_timeseries: dict[int, float] | None = None
    mortality_value_timeseries: dict[int, float] | None = None
    mortality_sum: dict[str, float] = field(default_factory=dict)
    mortality_value_sum: dict[str, float] = field(default_factory=dict)
    scc_usd_per_tco2: dict[str, dict[int, float]] = field(default_factory=dict)
    damages_usd: dict[str, dict[int, float]] = field(default_factory=dict)
    damages_usd_timeseries: dict[str, dict[int, float]] | None = None
    scc_usd_per_tco2_timeseries: dict[str, dict[int, float]] | None = None
    damages_sum: dict[str, dict[str, float]] = field(default_factory=dict)
    scc_average: dict[str, float] = field(default_factory=dict)
    damage_total_usd: dict[str, float] = field(default_factory=dict)
    emission_absolute_timeseries: dict[int, float] | None = None
    emission_timeseries: dict[int, float] | None = None
    temperature_timeseries: dict[int, float] | None = None


def _ensure_years(summary_cfg: Mapping[str, object]) -> list[int]:
    years_cfg = summary_cfg.get("years")
    if not years_cfg:
        return [2030, 2050]
    years: list[int] = []
    for entry in years_cfg:
        try:
            years.append(int(entry))
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring invalid summary year '%s'", entry)
    return sorted(set(years))


def _resolve_methods(methods_cfg: Mapping[str, object]) -> list[str]:
    run = methods_cfg.get("run")
    if isinstance(run, str):
        if run.lower() == "all":
            return list(AVAILABLE_METHODS)
        run_list = [run]
    elif isinstance(run, Iterable):
        run_list = [str(item) for item in run]
    else:
        run_list = []
    selected = [method for method in run_list if method in AVAILABLE_METHODS]
    if selected:
        return selected
    return list(AVAILABLE_METHODS)


def _safe_name(name: str) -> str:
    return re.sub(r"[^0-9a-zA-Z_]+", "_", name.strip().lower()).strip("_") or "scenario"


def _slugify_label(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", value.strip()).strip("_")


def _parse_climate_labels(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Iterable):
        labels: list[str] = []
        for item in value:
            if item is None:
                continue
            label = str(item).strip()
            if label:
                labels.append(label)
        return labels
    return []


def _climate_labels_from_module(config: Mapping[str, object]) -> list[str]:
    climate_cfg = (
        config.get("climate_module", {}).get("climate_scenarios", {})
        if isinstance(config.get("climate_module"), Mapping)
        else {}
    )
    definitions = climate_cfg.get("definitions") or []
    label_map: dict[str, str] = {}
    for entry in definitions:
        if not isinstance(entry, Mapping):
            continue
        label = str(entry.get("label") or entry.get("id") or "").strip()
        if label:
            label_map[label] = label

    def _normalize(value: object) -> str | None:
        label = str(value).strip()
        if not label:
            return None
        if label_map:
            return label_map.get(label) or (label if label in label_map else None)
        return label

    run_cfg = climate_cfg.get("run")
    resolved: list[str] = []
    if isinstance(run_cfg, str):
        spec = run_cfg.strip().lower()
        if spec == "all":
            resolved = list(label_map.values()) if label_map else []
        else:
            candidate = _normalize(run_cfg)
            if candidate:
                resolved = [candidate]
    elif isinstance(run_cfg, Iterable):
        for entry in run_cfg:
            candidate = _normalize(entry)
            if candidate:
                resolved.append(candidate)
    if not resolved and label_map:
        resolved = list(label_map.values())
    return [label for label in resolved if label]


def _split_climate_suffix(name: str, climate_labels: Sequence[str]) -> tuple[str, str | None]:
    for label in sorted({cl.strip() for cl in climate_labels if cl}, key=len, reverse=True):
        suffix = f"_{label}"
        if name.endswith(suffix):
            return name[: -len(suffix)], label
    return name, None


def _scenario_climate_label(scenario: str, climate_labels: Sequence[str]) -> str | None:
    if not climate_labels:
        return None
    _, climate_label = _split_climate_suffix(scenario, climate_labels)
    return climate_label


def _infer_ssp_family_from_label(label: str | None) -> str:
    if not label:
        return "SSP2"
    match = re.match(r"ssp\s*([0-9])", label, re.IGNORECASE)
    if match:
        return f"SSP{match.group(1)}"
    return "SSP2"


def _collapse_by_climate_label(
    data: Mapping[str, dict[int, float]] | None, climate_labels: Sequence[str]
) -> dict[str, dict[int, float]]:
    if not data:
        return {}
    if not climate_labels:
        return dict(data)
    collapsed: dict[str, dict[int, float]] = {}
    for scenario, series in data.items():
        base, label = _split_climate_suffix(scenario, climate_labels)
        key = label or base or scenario
        if key not in collapsed:
            collapsed[key] = dict(series)
    return collapsed


def _resolve_directory(path: Path, label: str) -> Path:
    candidate = path.resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"{label} directory not found: {candidate}")
    if not candidate.is_dir():
        raise NotADirectoryError(f"{label} path is not a directory: {candidate}")
    return candidate


def _default_emission_root(
    root_cfg: Mapping[str, object],
    root: Path,
    run_directory: str | None,
) -> Path | None:
    countries_cfg = root_cfg.get("calc_emissions", {}).get("countries", {})
    path_str = countries_cfg.get("aggregate_output_directory") or countries_cfg.get(
        "aggregate_results_directory"
    )
    if not path_str:
        return None
    path = Path(path_str)
    if not path.is_absolute():
        path = (root / path).resolve()
    return apply_results_run_directory(path, run_directory, repo_root=root)


def _default_per_country_root(
    root_cfg: Mapping[str, object],
    root: Path,
    run_directory: str | None,
) -> Path | None:
    countries_cfg = root_cfg.get("calc_emissions", {}).get("countries", {})
    path_str = countries_cfg.get("resources_root")
    if not path_str:
        return None
    path = Path(path_str)
    if not path.is_absolute():
        path = (root / path).resolve()
    return apply_results_run_directory(path, run_directory, repo_root=root)


def _default_temperature_root(
    root_cfg: Mapping[str, object],
    root: Path,
    run_directory: str | None,
) -> Path | None:
    climate_cfg = root_cfg.get("climate_module", {})
    path_str = climate_cfg.get("resource_directory") or climate_cfg.get("output_directory")
    if not path_str:
        return None
    path = Path(path_str)
    if not path.is_absolute():
        path = (root / path).resolve()
    return apply_results_run_directory(path, run_directory, repo_root=root)


def _resolve_path(
    root: Path,
    value: object | None,
    run_directory: str | None,
) -> Path | None:
    if value in (None, "", False):
        return None
    path = Path(str(value))
    absolute = path if path.is_absolute() else (root / path).resolve()
    return apply_results_run_directory(absolute, run_directory, repo_root=root)


def _load_full_series(path_or_frame: Path | pd.DataFrame, value_column: str) -> dict[int, float]:
    if isinstance(path_or_frame, pd.DataFrame):
        frame = path_or_frame.copy()
    else:
        path = Path(path_or_frame)
        if not path.exists():
            LOGGER.debug("Data file missing for full series load: %s", path)
            return {}
        frame = pd.read_csv(path)
        if "year" not in frame.columns or value_column not in frame.columns:
            LOGGER.warning(
                "File %s missing required columns 'year'/'%s' for full series load",
                path,
                value_column,
            )
            return {}
    frame["year"] = frame["year"].astype(int)
    series = frame.set_index("year")[value_column].astype(float)
    return {int(year): float(series.get(int(year))) for year in series.index}


def _load_emission_frame_from_mix(
    root: Path,
    scenario: str,
    value_column: str,
    baseline_case: str,
) -> pd.DataFrame:
    if value_column == "delta":
        frame = load_scenario_delta(root, scenario, baseline_case=baseline_case)
    elif value_column == "absolute":
        frame = load_scenario_absolute(root, scenario)
    else:
        raise ValueError(
            "Unsupported emission_column '%s'. Use 'delta' or 'absolute'." % value_column
        )
    return frame


def _collect_socio_series(
    root: Path,
    config: Mapping[str, object],
    scenario_labels: Iterable[str],
    climate_labels: Sequence[str],
    run_directory: str | None,
    plot_end: int,
) -> tuple[
    dict[str, dict[int, float]] | None,
    dict[str, dict[int, float]] | None,
    dict[str, pd.DataFrame] | None,
]:
    socio_cfg = config.get("socioeconomics", {}) or {}
    conv_cfg = socio_cfg.get("currency_conversion", {}) if isinstance(socio_cfg, Mapping) else {}

    def _currency_settings(key: str, default: float = 1.0) -> tuple[float, str]:
        try:
            value = float(conv_cfg.get(key, default))
        except Exception:
            value = float(default)
        if math.isclose(value, 1.0, rel_tol=1e-9, abs_tol=1e-9):
            return value, "USD_native"
        match = re.search(r"to_(\d{4})", key)
        if match:
            return value, f"USD_{match.group(1)}"
        digits = re.findall(r"(?:19|20|21)\d{2}", key)
        if digits:
            return value, f"USD_{digits[-1]}"
        return value, "USD_converted"

    dice_conversion, dice_currency_label = _currency_settings("usd_2017_to_2025", 1.0)
    economic_cfg = config.get("economic_module", {}) or {}
    socio_cfg = config.get("socioeconomics", {}) or {}
    scenario_list = [f"socio_{label}" for label in scenario_labels if label] or ["socio_default"]

    gdp_series_path = _resolve_path(root, economic_cfg.get("gdp_series"), run_directory)
    gdp_population_dir = economic_cfg.get("gdp_population_directory")
    if gdp_population_dir is not None:
        gdp_population_dir = _resolve_path(root, gdp_population_dir, run_directory)
    if gdp_population_dir is None:
        gdp_population_dir = (root / "data" / "GDP_and_Population_data").resolve()

    gdp_map: dict[str, dict[int, float]] = {}
    pop_map: dict[str, dict[int, float]] = {}
    socio_frames: dict[str, pd.DataFrame] = {}

    try:
        if gdp_series_path and gdp_series_path.exists():
            frame = pd.read_csv(gdp_series_path)
            if "year" not in frame.columns or "gdp_trillion_usd" not in frame.columns:
                raise ValueError(
                    f"GDP series file {gdp_series_path} missing required columns "
                    "'year'/'gdp_trillion_usd'"
                )
            frame["year"] = frame["year"].astype(int)
            gdp_base = {
                int(year): float(value)
                for year, value in frame.set_index("year")["gdp_trillion_usd"].astype(float).items()
            }
            pop_base: dict[int, float] | None = None
            if "population_million" in frame.columns:
                pop_base = {
                    int(year): float(value)
                    for year, value in frame.set_index("year")["population_million"]
                    .astype(float)
                    .items()
                }
            for scenario in scenario_list:
                gdp_map[scenario] = dict(gdp_base)
                if pop_base is not None:
                    pop_map[scenario] = dict(pop_base)
                socio_frame = frame[["year", "gdp_trillion_usd"]].copy()
                if pop_base is not None:
                    socio_frame["population_million"] = frame["population_million"].astype(float)
                socio_frame.attrs["currency_label"] = "USD_native"
                socio_frames[scenario] = socio_frame
            _extend_socio_maps(gdp_map, pop_map, plot_end)
            return gdp_map or None, pop_map or None, socio_frames or None

        socio_mode = str(socio_cfg.get("mode", "")).strip().lower()
        if socio_mode == "dice":
            dice_cfg = socio_cfg.get("dice", {}) or {}
            for scenario in scenario_list:
                climate_label = _scenario_climate_label(scenario, climate_labels)
                gdp_series, population_series, socio_frame = _project_dice_series(
                    root,
                    dice_cfg,
                    climate_label,
                    plot_end,
                    currency_conversion=dice_conversion,
                    currency_label=dice_currency_label,
                )
                if gdp_series:
                    gdp_map[scenario] = gdp_series
                if population_series:
                    pop_map[scenario] = population_series
                if socio_frame is not None:
                    socio_frames[scenario] = socio_frame
            _extend_socio_maps(gdp_map, pop_map, plot_end)
            return gdp_map or None, pop_map or None, socio_frames or None

        for scenario in scenario_list:
            climate_label = _scenario_climate_label(scenario, climate_labels)
            family = _infer_ssp_family_from_label(climate_label)
            gdp_series, population_series = _load_ssp_economic_data(family, gdp_population_dir)
            gdp_map[scenario] = {
                int(year): float(value) for year, value in gdp_series.sort_index().items()
            }
            if population_series is not None:
                pop_map[scenario] = {
                    int(year): float(value)
                    for year, value in population_series.sort_index().items()
                }
            frame = pd.DataFrame(
                {
                    "year": gdp_series.index.astype(int),
                    "gdp_trillion_usd": gdp_series.values.astype(float),
                }
            )
            if population_series is not None:
                frame["population_million"] = (
                    population_series.reindex(gdp_series.index).astype(float).values
                )
            frame.attrs["currency_label"] = "USD_native"
            socio_frames[scenario] = frame
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("Unable to load socioeconomics series: %s", exc)
        return None, None, None

    _extend_socio_maps(gdp_map, pop_map, plot_end)
    return gdp_map or None, pop_map or None, socio_frames or None


def _extend_socio_maps(
    gdp_map: dict[str, dict[int, float]],
    pop_map: dict[str, dict[int, float]],
    target_year: int,
) -> None:
    for series in gdp_map.values():
        _extend_series_to_year(series, target_year)
    for series in pop_map.values():
        _extend_series_to_year(series, target_year)


def _extend_series_to_year(series: dict[int, float], target_year: int) -> None:
    if not series:
        return
    years_sorted = sorted(series.keys())
    initial_last = years_sorted[-1]
    # Interpolate between known points
    for idx in range(len(years_sorted) - 1):
        y0 = years_sorted[idx]
        y1 = years_sorted[idx + 1]
        v0 = series[y0]
        v1 = series[y1]
        span = y1 - y0
        if span <= 1:
            continue
        slope = (v1 - v0) / span
        for year in range(y0 + 1, y1):
            if year not in series:
                series[year] = v0 + slope * (year - y0)
    years_sorted = sorted(series.keys())
    last_value = series[years_sorted[-1]]
    if target_year > initial_last:
        LOGGER.info(
            "Extending socioeconomics series from %s to %s by holding values constant.",
            initial_last,
            target_year,
        )
    for year in range(years_sorted[-1] + 1, max(target_year, years_sorted[-1]) + 1):
        if year in series:
            last_value = series[year]
        else:
            series[year] = last_value
    if years_sorted[-1] < target_year:
        LOGGER.info(
            "Extending socioeconomics series from %s to %s by holding values constant.",
            years_sorted[-1],
            target_year,
        )


def _project_dice_series(
    root: Path,
    dice_cfg: Mapping[str, object],
    climate_label: str | None,
    end_year: int,
    *,
    currency_conversion: float = 1.0,
    currency_label: str = "USD_native",
) -> tuple[dict[int, float] | None, dict[int, float] | None, pd.DataFrame]:
    cfg = dict(dice_cfg)
    scenario_setting = str(cfg.get("scenario", "SSP2")).strip()
    if scenario_setting.lower() == "as_climate_scenario":
        resolved = _infer_ssp_family_from_label(climate_label)
    else:
        resolved = scenario_setting.upper()
    cfg["scenario"] = resolved
    model = DiceSocioeconomics.from_config(cfg, base_path=root)
    frame = model.project(int(end_year), currency_conversion=currency_conversion)
    frame.attrs["currency_label"] = currency_label
    gdp_series = {
        int(year): float(value)
        for year, value in frame.set_index("year")["gdp_trillion_usd"].astype(float).items()
    }
    population_series = {
        int(year): float(value)
        for year, value in frame.set_index("year")["population_million"].astype(float).items()
    }
    return gdp_series, population_series, frame


def _read_series(
    source: Path | pd.DataFrame,
    value_column: str,
    years: Iterable[int],
    *,
    transform: callable | None = None,
) -> dict[int, float]:
    if isinstance(source, pd.DataFrame):
        frame = source.copy()
    else:
        path = Path(source)
        if not path.exists():
            LOGGER.debug("Data file missing: %s", path)
            return {year: math.nan for year in years}
        frame = pd.read_csv(path)
        if "year" not in frame.columns or value_column not in frame.columns:
            LOGGER.warning("File %s missing required columns 'year'/'%s'", path, value_column)
            return {year: math.nan for year in years}
    frame["year"] = frame["year"].astype(int)
    series = frame.set_index("year")[value_column].astype(float)
    if transform is not None:
        series = transform(series)
    return {int(year): float(series.get(int(year), np.nan)) for year in years}


def _read_temperature(path: Path, years: Iterable[int]) -> dict[int, float]:
    if not path.exists():
        LOGGER.debug("Temperature series missing: %s", path)
        return {year: math.nan for year in years}
    frame = pd.read_csv(path)
    if "year" not in frame.columns:
        LOGGER.warning("Temperature file %s missing 'year' column", path)
        return {year: math.nan for year in years}
    frame["year"] = frame["year"].astype(int)
    if "temperature_delta" in frame.columns:
        series = frame.set_index("year")["temperature_delta"].astype(float)
    elif {"temperature_baseline", "temperature_adjusted"}.issubset(frame.columns):
        base = frame.set_index("year")["temperature_baseline"].astype(float)
        adj = frame.set_index("year")["temperature_adjusted"].astype(float)
        series = adj - base
    else:
        LOGGER.warning("Temperature file %s missing delta information", path)
        return {year: math.nan for year in years}
    return {int(year): float(series.get(int(year), np.nan)) for year in years}


def _load_temperature_series(path: Path) -> dict[int, float] | None:
    if not path.exists():
        return None
    frame = pd.read_csv(path)
    if "year" not in frame.columns:
        return None
    frame["year"] = frame["year"].astype(int)
    if "temperature_delta" in frame.columns:
        series = frame.set_index("year")["temperature_delta"].astype(float)
    elif {"temperature_baseline", "temperature_adjusted"}.issubset(frame.columns):
        base = frame.set_index("year")["temperature_baseline"].astype(float)
        adj = frame.set_index("year")["temperature_adjusted"].astype(float)
        series = adj - base
    else:
        return None
    return {int(year): float(value) for year, value in series.items()}


def _read_mortality(
    path: Path, years: Iterable[int]
) -> tuple[dict[int, float], dict[int, float], dict[int, float], dict[int, float] | None]:
    if not path.exists():
        LOGGER.debug("Mortality summary missing: %s", path)
        nan_map = {year: math.nan for year in years}
        return nan_map, nan_map.copy(), nan_map.copy()
    frame = pd.read_csv(path)
    expected = {
        "year",
        "delta_deaths_per_year",
        "percent_change_mortality",
        "baseline_deaths_per_year",
    }
    value_col = frame.columns.intersection(["delta_value_usd"])
    if not expected.issubset(frame.columns):
        LOGGER.warning("Mortality file %s missing required columns", path)
        nan_map = {year: math.nan for year in years}
        return nan_map, nan_map.copy(), nan_map.copy(), None
    frame["year"] = frame["year"].astype(int)
    frame = frame.set_index("year")
    delta = frame["delta_deaths_per_year"].astype(float)
    percent = frame["percent_change_mortality"].astype(float)
    baseline = frame["baseline_deaths_per_year"].astype(float)
    value_series = frame[value_col[0]].astype(float) if len(value_col) else None
    return (
        {int(year): float(delta.get(int(year), np.nan)) for year in years},
        {int(year): float(percent.get(int(year), np.nan)) for year in years},
        {int(year): float(baseline.get(int(year), np.nan)) for year in years},
        (
            {int(year): float(value_series.get(int(year), np.nan)) for year in years}
            if value_series is not None
            else None
        ),
    )


def _load_mortality_timeseries(path: Path) -> tuple[dict[int, float], dict[int, float]]:
    if not path.exists():
        return {}, {}
    frame = pd.read_csv(path)
    if "year" not in frame.columns:
        return {}, {}
    frame["year"] = frame["year"].astype(int)
    delta = (
        frame.set_index("year")["delta_deaths_per_year"].astype(float)
        if "delta_deaths_per_year" in frame.columns
        else pd.Series(dtype=float)
    )
    value = (
        frame.set_index("year")["delta_value_usd"].astype(float)
        if "delta_value_usd" in frame.columns
        else pd.Series(dtype=float)
    )
    return (
        {int(y): float(delta.get(int(y))) for y in delta.index},
        {int(y): float(value.get(int(y))) for y in value.index},
    )


def _load_climate_scc_series(
    output_dir: Path,
    methods: Iterable[str],
    climate_labels: Sequence[str],
    years: Iterable[int],
) -> tuple[
    dict[tuple[str, str], dict[int, float]],
    dict[tuple[str, str], dict[int, float]],
    dict[tuple[str, str], float],
]:
    per_year_lookup: dict[tuple[str, str], dict[int, float]] = {}
    full_lookup: dict[tuple[str, str], dict[int, float]] = {}
    average_lookup: dict[tuple[str, str], float] = {}
    labels = [label for label in climate_labels if label] or ["default"]
    years_list = [int(year) for year in years]
    for label in labels:
        safe_label = _safe_name(label)
        for method in methods:
            candidates = [
                output_dir / f"pulse_scc_timeseries_{method}_{label}.csv",
                output_dir / f"pulse_scc_timeseries_{method}_{safe_label}.csv",
            ]
            path = next((p for p in candidates if p.exists()), None)
            if path is None or not path.exists():
                per_year_lookup[(label, method)] = {}
                full_lookup[(label, method)] = {}
                average_lookup[(label, method)] = math.nan
                continue
            try:
                frame = pd.read_csv(path)
            except Exception:
                per_year_lookup[(label, method)] = {}
                full_lookup[(label, method)] = {}
                average_lookup[(label, method)] = math.nan
                continue
            required_cols = {"year", "scc_usd_per_tco2"}
            if not required_cols.issubset(frame.columns):
                per_year_lookup[(label, method)] = {}
                full_lookup[(label, method)] = {}
                average_lookup[(label, method)] = math.nan
                continue
            frame["year"] = frame["year"].astype(int)
            scc_series = frame.set_index("year")["scc_usd_per_tco2"].astype(float)
            per_year_lookup[(label, method)] = {
                int(year): float(scc_series.get(int(year), math.nan)) for year in years_list
            }
            full_lookup[(label, method)] = {
                int(year): float(value) for year, value in scc_series.items()
            }
            if {"delta_emissions_tco2", "discounted_delta_usd"}.issubset(frame.columns):
                emissions = frame["delta_emissions_tco2"].astype(float).sum()
                discounted = frame["discounted_delta_usd"].astype(float).sum()
                if math.isfinite(emissions) and not math.isclose(emissions, 0.0):
                    average_lookup[(label, method)] = discounted / emissions
                else:
                    average_lookup[(label, method)] = math.nan
            else:
                average_lookup[(label, method)] = math.nan
    return per_year_lookup, full_lookup, average_lookup


def _sum_over_periods(
    frame: pd.DataFrame, value_col: str, periods: Iterable[tuple[int, int]]
) -> dict[str, float]:
    """Interpolate to annual steps and sum over configured periods."""
    if "year" not in frame.columns or value_col not in frame.columns:
        return {}
    series = frame.set_index(frame["year"].astype(int))[value_col].astype(float)
    if series.empty:
        return {}
    full_index = pd.Index(range(int(series.index.min()), int(series.index.max()) + 1))
    series = series.reindex(full_index).interpolate(method="index").ffill().bfill()
    sums: dict[str, float] = {}
    for start, end in periods:
        idx = pd.Index(range(start, end + 1))
        if idx.empty:
            continue
        window = series.reindex(idx).interpolate(method="index").ffill().bfill()
        sums[f"{start}_to_{end}"] = float(window.sum())
    return sums


def _read_scc_timeseries(
    output_dir: Path,
    scenario: str,
    methods: Iterable[str],
    years: Iterable[int],
    *,
    climate_labels: Sequence[str],
    climate_scc_lookup: Mapping[tuple[str, str], dict[int, float]],
    climate_scc_full: Mapping[tuple[str, str], dict[int, float]],
    currency_conversion: float = 1.0,
) -> tuple[
    dict[str, dict[int, float]],
    dict[str, dict[int, float]],
    dict[str, float],
    dict[str, dict[int, float]],
    dict[str, dict[int, float]],
]:
    scenario_key = _safe_name(scenario)
    base_name, climate_label = _split_climate_suffix(scenario, climate_labels)
    if not climate_label and climate_labels:
        climate_label = climate_labels[0]
    climate_key = climate_label or "default"
    try:
        mix_case, _ = split_scenario_name(base_name)
    except ValueError:
        mix_case = base_name
    damages: dict[str, dict[int, float]] = {}
    scc_values: dict[str, dict[int, float]] = {}
    totals: dict[str, float] = {}
    damages_full: dict[str, dict[int, float]] = {}
    scc_full: dict[str, dict[int, float]] = {}
    for method in methods:
        climate_series = climate_scc_lookup.get((climate_key, method), {})
        climate_full_series = climate_scc_full.get((climate_key, method), {})
        scc_values[method] = {
            int(year): float(climate_series.get(int(year), math.nan)) for year in years
        }
        scc_full[method] = {int(year): float(value) for year, value in climate_full_series.items()}

        damage_candidates = [
            output_dir / mix_case / f"damages_{method}_{scenario}.csv",
            output_dir / mix_case / f"damages_{method}_{scenario_key}.csv",
            output_dir / f"damages_{method}_{scenario}.csv",
        ]
        damage_path = next((p for p in damage_candidates if p.exists()), None)
        if damage_path is None:
            LOGGER.debug("Damage file missing for %s (%s)", scenario, method)
            damages[method] = {year: math.nan for year in years}
            totals[method] = math.nan
            damages_full[method] = {}
            continue
        try:
            frame = pd.read_csv(damage_path)
        except Exception:
            damages[method] = {year: math.nan for year in years}
            totals[method] = math.nan
            damages_full[method] = {}
            continue
        if "year" not in frame.columns or "damage_usd" not in frame.columns:
            damages[method] = {year: math.nan for year in years}
            totals[method] = math.nan
            damages_full[method] = {}
            continue
        frame["year"] = frame["year"].astype(int)
        damages_series = frame.set_index("year")["damage_usd"].astype(float) * currency_conversion
        damages[method] = {
            int(year): float(damages_series.get(int(year), np.nan)) for year in years
        }
        damages_full[method] = {int(year): float(value) for year, value in damages_series.items()}
        if "discounted_damage_usd" in frame.columns:
            totals[method] = float(
                frame["discounted_damage_usd"].astype(float).sum(skipna=True) * currency_conversion
            )
        elif "discount_factor" in frame.columns:
            totals[method] = float(
                (frame["damage_usd"].astype(float) * frame["discount_factor"].astype(float)).sum(
                    skipna=True
                )
                * currency_conversion
            )
        else:
            totals[method] = math.nan
    return damages, scc_values, totals, damages_full, scc_full


def _compute_damage_period_sums(
    output_dir: Path,
    scenario: str,
    methods: Iterable[str],
    periods: Iterable[tuple[int, int]],
    *,
    climate_labels: Sequence[str],
    currency_conversion: float = 1.0,
) -> dict[str, dict[str, float]]:
    if not periods:
        return {}
    sums: dict[str, dict[str, float]] = {}
    scenario_key = _safe_name(scenario)
    base_name, _ = _split_climate_suffix(scenario, climate_labels)
    try:
        mix_case, _ = split_scenario_name(base_name)
    except ValueError:
        mix_case = base_name
    for method in methods:
        method_sum: dict[str, float] = {}
        candidates = [
            output_dir / mix_case / f"damages_{method}_{scenario}.csv",
            output_dir / mix_case / f"damages_{method}_{scenario_key}.csv",
            output_dir / f"damages_{method}_{scenario}.csv",
        ]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            sums[method] = method_sum
            continue
        try:
            frame = pd.read_csv(path)
        except Exception:
            sums[method] = method_sum
            continue
        value_col = None
        for candidate in (
            "discounted_damage_usd",
            "discounted_delta_usd",
            "damage_usd",
            "delta_damage_usd",
        ):
            if candidate in frame.columns:
                value_col = candidate
                break
        if value_col is None:
            sums[method] = method_sum
            continue
        frame[value_col] = frame[value_col].astype(float) * currency_conversion
        method_sum = _sum_over_periods(frame, value_col, periods)
        sums[method] = method_sum
    return sums


def build_summary(
    root: Path, config: Mapping[str, object]
) -> tuple[SummarySettings, list[str], dict[str, ScenarioMetrics]]:
    root = Path(root)
    try:
        economic_cfg = config["economic_module"]  # type: ignore[index]
    except KeyError as exc:
        raise ValueError(
            "economic_module configuration is required for summary generation."
        ) from exc
    if not isinstance(economic_cfg, Mapping):
        raise ValueError("economic_module configuration must be a mapping.")
    try:
        emission_to_tonnes = float(economic_cfg.get("emission_to_tonnes", 1e6))
    except (TypeError, ValueError):
        emission_to_tonnes = 1e6
    try:
        base_year_value = int(economic_cfg.get("base_year", 2025))
    except (TypeError, ValueError):
        base_year_value = 2025

    countries_cfg = config.get("calc_emissions", {}).get("countries", {}) or {}
    baseline_case = (
        str(countries_cfg.get("baseline_demand_case", BASE_DEMAND_CASE)).strip() or BASE_DEMAND_CASE
    )
    baseline_mix_case = (
        str(countries_cfg.get("baseline_mix_case", BASE_MIX_CASE)).strip() or BASE_MIX_CASE
    )

    summary_cfg = (
        config.get("results", {}).get("summary", {})
        if isinstance(config.get("results"), Mapping)
        else {}
    )
    run_directory = get_results_run_directory(config)

    years = _ensure_years(summary_cfg)
    if not years:
        raise ValueError("results.summary.years must contain at least one reporting year.")
    year_periods = _parse_year_periods(summary_cfg)

    output_directory = _resolve_path(root, summary_cfg.get("output_directory"), run_directory)
    if output_directory is None:
        default_summary_dir = (root / "results" / "summary").resolve()
        output_directory = apply_results_run_directory(
            default_summary_dir,
            run_directory,
            repo_root=root,
        )
    include_plots = bool(summary_cfg.get("include_plots", True))
    plot_format = str(summary_cfg.get("plot_format", "png"))
    time_horizon_cfg = config.get("time_horizon", {})
    if not isinstance(time_horizon_cfg, Mapping):
        time_horizon_cfg = {}
    horizon_start = time_horizon_cfg.get("start")
    horizon_end = time_horizon_cfg.get("end")
    plot_start = int(
        horizon_start if horizon_start is not None else summary_cfg.get("plot_start", 2025)
    )
    plot_end = int(horizon_end if horizon_end is not None else summary_cfg.get("plot_end", 2100))

    aggregation_mode = str(economic_cfg.get("aggregation", "per_year"))
    aggregation_horizon_cfg = economic_cfg.get("aggregation_horizon")
    aggregation_horizon: tuple[int, int] | None = None
    if isinstance(aggregation_horizon_cfg, Mapping):
        start = aggregation_horizon_cfg.get("start")
        end = aggregation_horizon_cfg.get("end")
        if start is not None and end is not None:
            aggregation_horizon = (int(start), int(end))

    # settings will be created after climate label resolution

    methods = _resolve_methods(economic_cfg.get("methods", {}))
    run_cfg = economic_cfg.get("run", {}) if isinstance(economic_cfg, Mapping) else {}
    run_method = str(run_cfg.get("method", "pulse")) if isinstance(run_cfg, Mapping) else "pulse"

    data_sources = economic_cfg.get("data_sources", {})
    if not isinstance(data_sources, Mapping):
        data_sources = {}
    emission_root = _resolve_path(root, data_sources.get("emission_root"), run_directory)
    if emission_root is None:
        emission_root = _default_emission_root(config, root, run_directory)
    if emission_root is None:
        raise ValueError("Unable to resolve emission data root directory.")
    try:
        emission_root = _resolve_directory(emission_root, "Emission data")
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ValueError(str(exc)) from exc

    temperature_root = _resolve_path(root, data_sources.get("temperature_root"), run_directory)
    if temperature_root is None:
        temperature_root = _default_temperature_root(config, root, run_directory)
    if temperature_root is None:
        raise ValueError("Unable to resolve temperature data directory.")
    try:
        temperature_root = _resolve_directory(temperature_root, "Temperature data")
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ValueError(str(exc)) from exc

    scc_output_dir = _resolve_path(root, economic_cfg.get("output_directory"), run_directory)
    if scc_output_dir is None:
        default_scc_dir = (root / "results" / "economic").resolve()
        scc_output_dir = apply_results_run_directory(
            default_scc_dir,
            run_directory,
            repo_root=root,
        )
    air_pollution_cfg = config.get("air_pollution", {})
    air_cfg = air_pollution_cfg if isinstance(air_pollution_cfg, Mapping) else {}
    air_output_dir = _resolve_path(root, air_cfg.get("output_directory"), run_directory)
    if air_output_dir is None:
        default_air_dir = (root / "results" / "air_pollution").resolve()
        air_output_dir = apply_results_run_directory(
            default_air_dir,
            run_directory,
            repo_root=root,
        )
    pollutants_list: list[str] = []
    air_stats: dict[str, dict[str, float]] = {}
    air_country_baseline: dict[str, float] = {}
    air_vsl: float | None = None
    if isinstance(air_cfg, Mapping):
        if "value_of_statistical_life_usd" in air_cfg:
            try:
                air_vsl = float(air_cfg.get("value_of_statistical_life_usd"))
            except (TypeError, ValueError):
                air_vsl = None
        pollutants_cfg = air_cfg.get("pollutants")
        if isinstance(pollutants_cfg, Mapping):
            for key, value in pollutants_cfg.items():
                pollutant_name = str(key)
                pollutants_list.append(pollutant_name)
                stats_file = value.get("stats_file") if isinstance(value, Mapping) else None
                if not stats_file:
                    continue
                stats_path = _resolve_path(root, stats_file, run_directory)
                try:
                    stats_df = pd.read_csv(stats_path)
                except Exception:
                    continue
                required = {"country", "baseline_deaths_per_year"}
                if not required.issubset(stats_df.columns):
                    continue
                mapping: dict[str, float] = {}
                for _, entry in stats_df.iterrows():
                    country = entry.get("country")
                    if not isinstance(country, str):
                        continue
                    slug = _slugify_label(country)
                    if not slug:
                        continue
                    try:
                        baseline_value = float(entry["baseline_deaths_per_year"])
                    except (TypeError, ValueError):
                        continue
                    mapping[slug] = baseline_value
                    air_country_baseline[slug] = (
                        air_country_baseline.get(slug, 0.0) + baseline_value
                    )
                if mapping:
                    air_stats[pollutant_name] = mapping
        elif isinstance(pollutants_cfg, Iterable):
            pollutants_list = [str(item) for item in pollutants_cfg if str(item)]

    emission_column = str(economic_cfg.get("emission_column", "delta"))

    reference_scenario = str(economic_cfg.get("reference_scenario", "")).strip()
    evaluation = economic_cfg.get("evaluation_scenarios", [])

    def _all_emission_scenarios() -> list[str]:
        countries_cfg = (
            config.get("calc_emissions", {}).get("countries", {})
            if isinstance(config, Mapping)
            else {}
        )
        demand_cases_cfg = countries_cfg.get("demand_scenarios", [])
        if isinstance(demand_cases_cfg, Mapping):
            demand_cases = list(demand_cases_cfg.keys())
        elif isinstance(demand_cases_cfg, Iterable) and not isinstance(
            demand_cases_cfg, (str, bytes)
        ):
            demand_cases = [str(case) for case in demand_cases_cfg]
        else:
            demand_cases = []
        baseline = (
            str(countries_cfg.get("baseline_demand_case", baseline_case)).strip() or baseline_case
        )
        try:
            return list_available_scenarios(
                emission_root,
                demand_cases or [baseline_case],
                include_baseline=False,
                baseline_case=baseline,
            )
        except Exception as exc:  # pragma: no cover - fallback path
            LOGGER.warning("Unable to enumerate emission scenarios; using empty list: %s", exc)
            return []

    include_all = False
    if isinstance(evaluation, str) and evaluation.strip().lower() == "all":
        include_all = True
    elif isinstance(evaluation, Iterable) and not isinstance(evaluation, (str, bytes)):
        eval_list = [item for item in evaluation if item is not None]
        include_all = len(eval_list) == 0
    else:
        include_all = not evaluation

    if include_all:
        evaluation_scenarios = _all_emission_scenarios()
    else:
        evaluation_scenarios = [str(label) for label in evaluation]
        evaluation_scenarios.extend(_all_emission_scenarios())
        evaluation_scenarios = sorted(set(evaluation_scenarios))

    if not evaluation_scenarios:
        raise ValueError(
            "economic_module.evaluation_scenarios must list at least one scenario or 'all'."
        )

    climate_inputs = _parse_climate_labels(data_sources.get("climate_scenarios"))
    if not climate_inputs:
        climate_inputs = _climate_labels_from_module(config)
    climate_labels: list[str | None]
    if climate_inputs:
        climate_labels = climate_inputs
    else:
        derived: list[str] = []
        if reference_scenario:
            prefix = f"{reference_scenario}_"
            for path in temperature_root.glob(f"{prefix}*.csv"):
                suffix = path.stem[len(prefix) :]
                if suffix:
                    derived.append(suffix)
        climate_labels = sorted(set(derived)) or [None]

    climate_labels_str = [cl for cl in climate_labels if cl is not None]
    effective_climate_labels = climate_labels_str or ["default"]

    (
        climate_scc_lookup,
        climate_scc_full,
        climate_scc_average,
    ) = _load_climate_scc_series(
        scc_output_dir,
        methods,
        effective_climate_labels,
        years,
    )

    metrics_map: dict[str, ScenarioMetrics] = {}

    for scenario in evaluation_scenarios:
        if reference_scenario and scenario == reference_scenario:
            continue
        emission_frame = _load_emission_frame_from_mix(
            emission_root, scenario, emission_column, baseline_case
        )
        emission_delta = _read_series(emission_frame, emission_column, years)
        emission_series = _load_full_series(emission_frame, emission_column)
        if not emission_series:
            emission_series_dict: dict[int, float] | None = None
        else:
            emission_series_dict = emission_series

        absolute_frame = load_scenario_absolute(emission_root, scenario)
        absolute_series = _load_full_series(absolute_frame, "absolute")
        if not absolute_series:
            absolute_series_dict: dict[int, float] | None = None
        else:
            absolute_series_dict = absolute_series

        mortality_path = (air_output_dir / scenario / "total_mortality_summary.csv").resolve()
        (
            mortality_delta,
            mortality_percent,
            mortality_baseline,
            mortality_value_delta,
        ) = _read_mortality(mortality_path, years)
        mortality_ts, mortality_value_ts = _load_mortality_timeseries(mortality_path)
        mortality_frame = pd.read_csv(mortality_path) if mortality_path.exists() else pd.DataFrame()
        mortality_sums = _sum_over_periods(mortality_frame, "delta_deaths_per_year", year_periods)
        mortality_value_sums = _sum_over_periods(mortality_frame, "delta_value_usd", year_periods)

        for climate_label in climate_labels:
            scenario_label = f"{scenario}_{climate_label}" if climate_label else scenario
            temperature_path = (temperature_root / f"{scenario_label}.csv").resolve()
            temperature_delta = _read_temperature(temperature_path, years)
            temperature_series = _load_temperature_series(temperature_path)

            socio_cfg = config.get("socioeconomics", {}) if isinstance(config, Mapping) else {}
            conv_cfg = (
                socio_cfg.get("currency_conversion", {}) if isinstance(socio_cfg, Mapping) else {}
            )
            conversion = float(conv_cfg.get("usd_2017_to_2025", 1.0))

            damages, scc_values, damage_totals, damages_full, scc_full = _read_scc_timeseries(
                scc_output_dir,
                scenario_label,
                methods,
                years,
                climate_labels=effective_climate_labels,
                climate_scc_lookup=climate_scc_lookup,
                climate_scc_full=climate_scc_full,
                currency_conversion=conversion,
            )
            damage_sums = _compute_damage_period_sums(
                scc_output_dir,
                scenario_label,
                methods,
                year_periods,
                climate_labels=effective_climate_labels,
                currency_conversion=conversion,
            )

            climate_key = climate_label or "default"
            if aggregation_mode == "average":
                scc_average = {
                    method: climate_scc_average.get((climate_key, method), math.nan)
                    for method in methods
                }
            else:
                scc_average = {}

            damages_adjusted = {method: dict(series) for method, series in damages.items()}
            ramsey_key = "ramsey_discount"
            if ramsey_key in methods:
                ramsey_scc = scc_values.get(ramsey_key, {})
                computed: dict[int, float] = {}
                for year in years:
                    emission_value = emission_delta.get(year, math.nan)
                    scc_value = ramsey_scc.get(year, math.nan)
                    if any(math.isnan(value) for value in (emission_value, scc_value)):
                        computed[year] = math.nan
                    else:
                        computed[year] = emission_value * emission_to_tonnes * scc_value
                damages_adjusted[ramsey_key] = computed

            scc_values_copy = {method: dict(series) for method, series in scc_values.items()}

            metrics_map[scenario_label] = ScenarioMetrics(
                emission_delta_mt=emission_delta,
                temperature_delta_c=temperature_delta,
                mortality_delta=mortality_delta,
                mortality_percent=mortality_percent,
                mortality_baseline=mortality_baseline,
                mortality_value_delta=mortality_value_delta,
                mortality_delta_timeseries=mortality_ts,
                mortality_value_timeseries=mortality_value_ts,
                mortality_sum=mortality_sums,
                mortality_value_sum=mortality_value_sums,
                scc_usd_per_tco2=scc_values_copy,
                scc_usd_per_tco2_timeseries=scc_full,
                damages_usd=damages_adjusted,
                damages_usd_timeseries=damages_full,
                damages_sum=damage_sums,
                scc_average=scc_average,
                damage_total_usd=damage_totals,
                emission_absolute_timeseries=absolute_series_dict,
                emission_timeseries=emission_series_dict,
                temperature_timeseries=temperature_series,
            )

    gdp_series_map, population_series_map, socio_frame_map = _collect_socio_series(
        root,
        config,
        climate_labels_str,
        climate_labels_str,
        run_directory,
        plot_end,
    )
    pattern_cfg = config.get("local_climate_impacts", {}) if isinstance(config, Mapping) else {}
    pattern_output_directory: Path | None = None
    pattern_countries: list[str] | None = None
    extreme_weather_file: Path | None = None
    if isinstance(pattern_cfg, Mapping):
        pattern_output = pattern_cfg.get("output_directory")
        if pattern_output:
            candidate = _resolve_path(root, pattern_output, run_directory)
            try:
                pattern_output_directory = _resolve_directory(candidate, "Pattern-scaling output")
            except (FileNotFoundError, NotADirectoryError):
                pattern_output_directory = None
        countries_cfg = pattern_cfg.get("countries")
        if isinstance(countries_cfg, Iterable) and not isinstance(countries_cfg, (str, bytes)):
            pattern_countries = [str(c) for c in countries_cfg if c]
        costs_setting = pattern_cfg.get("extreme_weather_costs_file")
        if costs_setting:
            candidate = Path(str(costs_setting))
            if not candidate.is_absolute():
                candidate = (root / candidate).resolve()
            extreme_weather_file = candidate
        else:
            default_costs = (
                root / "data" / "pattern_scaling" / "extreme_weather_costs.csv"
            ).resolve()
            if default_costs.exists():
                extreme_weather_file = default_costs

    air_country_list = sorted(air_country_baseline.keys())

    # Construct summary settings after climate label resolution
    settings = SummarySettings(
        years=years,
        output_directory=output_directory,
        include_plots=include_plots,
        plot_format=plot_format,
        baseline_mix_case=baseline_mix_case,
        aggregation_mode=aggregation_mode,
        aggregation_horizon=aggregation_horizon,
        plot_start=plot_start,
        plot_end=plot_end,
        climate_labels=climate_labels_str,
        run_method=run_method,
        scc_output_directory=scc_output_dir,
        base_year=base_year_value,
        climate_output_directory=temperature_root,
        gdp_series=gdp_series_map,
        population_series=population_series_map,
        socio_frames=socio_frame_map,
        pattern_output_directory=pattern_output_directory,
        pattern_countries=pattern_countries,
        year_periods=year_periods,
        air_output_directory=air_output_dir,
        air_pollution_pollutants=pollutants_list,
        air_pollution_stats=air_stats,
        air_pollution_country_baseline=air_country_baseline,
        air_pollution_countries=air_country_list,
        air_pollution_vsl=air_vsl,
        damage_function_cfg=(
            economic_cfg.get("damage_function", {})
            if isinstance(economic_cfg.get("damage_function"), Mapping)
            else economic_cfg.get("damage_function")
        ),
        extreme_weather_costs_file=extreme_weather_file,
        aggregate_emission_root=emission_root,
    )
    settings.per_country_emission_root = _default_per_country_root(config, root, run_directory)

    if not metrics_map:
        LOGGER.warning(
            "No metrics collected for summary; check input directories and configuration."
        )

    return settings, methods, metrics_map


def write_summary_csv(
    settings: SummarySettings,
    methods: Iterable[str],
    metrics_map: Mapping[str, ScenarioMetrics],
    *,
    output_path: Path | None = None,
) -> Path:
    summary_dir = output_path or settings.output_directory
    summary_dir.mkdir(parents=True, exist_ok=True)
    csv_path = summary_dir / "summary.csv"

    rows: list[dict[str, object]] = []
    per_country_root = settings.per_country_emission_root
    pattern_dir = settings.pattern_output_directory
    pattern_countries = [str(code).upper() for code in (settings.pattern_countries or [])]
    air_output_dir = settings.air_output_directory
    air_pollutants = settings.air_pollution_pollutants or []
    years_set = set(settings.years)
    cached_country_dirs: dict[str, list[Path]] = {}
    pattern_cache: dict[tuple[str, str], dict[str, pd.DataFrame]] = {}
    extreme_weather_costs = _load_extreme_weather_costs(settings.extreme_weather_costs_file)

    def _country_dirs(mix_case: str) -> list[Path]:
        if per_country_root is None:
            return []
        if mix_case in cached_country_dirs:
            return cached_country_dirs[mix_case]
        mix_dir = per_country_root / mix_case
        if not mix_dir.exists():
            cached_country_dirs[mix_case] = []
            return []
        dirs = sorted(child for child in mix_dir.iterdir() if child.is_dir())
        cached_country_dirs[mix_case] = dirs
        return dirs

    for scenario in sorted(metrics_map):
        metrics = metrics_map[scenario]
        base_label, climate_label = _split_climate_suffix(scenario, settings.climate_labels)
        try:
            mix_case, demand_case = split_scenario_name(base_label)
        except ValueError:
            mix_case, demand_case = base_label, BASE_DEMAND_CASE
        row: dict[str, object] = {
            "energy_mix": mix_case,
            "climate_scenario": climate_label or "",
            "demand_case": demand_case,
        }
        for year in settings.years:
            row[f"delta_co2_Mt_all_countries_{year}"] = metrics.emission_delta_mt.get(
                year, math.nan
            )
            row[f"delta_T_C_{year}"] = metrics.temperature_delta_c.get(year, math.nan)
            row[f"air_pollution_mortality_difference_all_countries_{year}"] = (
                metrics.mortality_delta.get(year, math.nan)
            )
            percent_value = metrics.mortality_percent.get(year, math.nan)
            if isinstance(percent_value, (int, float)) and math.isfinite(percent_value):
                percent_value = percent_value * 100.0
            row[f"air_pollution_mortality_percent_change_all_countries_{year}"] = percent_value
            if metrics.mortality_value_delta is not None:
                row[f"air_pollution_monetary_benefit_all_countries_usd_{year}"] = (
                    metrics.mortality_value_delta.get(year, math.nan)
                )
        for label, value in metrics.mortality_sum.items():
            row[f"air_pollution_mortality_difference_sum_all_countries_{label}"] = value
        for label, value in metrics.mortality_value_sum.items():
            row[f"air_pollution_monetary_benefit_sum_all_countries_usd_{label}"] = value
        if settings.aggregation_mode == "average":
            for method in methods:
                row[f"scc_average_{method}"] = metrics.scc_average.get(method, math.nan)
        else:
            for method in methods:
                for year in settings.years:
                    key = f"SCC_{method}_{year}_PPP_USD_2025_discounted_to_year_per_tco2"
                    value = metrics.scc_usd_per_tco2.get(method, {}).get(year, math.nan)
                    row[key] = value
        for method in methods:
            series = metrics.damages_usd.get(method, {})
            for year in settings.years:
                row[f"damages_PPP2020_usd_baseyear_{settings.base_year}_{method}_{year}"] = (
                    series.get(year, math.nan)
                )
            for label, value in metrics.damages_sum.get(method, {}).items():
                row[f"damages_PPP2020_usd_baseyear_{settings.base_year}_sum_{method}_{label}"] = (
                    value
                )

        if per_country_root is not None:
            for country_dir in _country_dirs(mix_case):
                country = country_dir.name
                for pollutant_file in sorted(country_dir.glob("*.csv")):
                    pollutant = pollutant_file.stem
                    try:
                        df = pd.read_csv(pollutant_file, comment="#")
                    except FileNotFoundError:
                        continue
                    delta_col = f"delta_{demand_case}"
                    if delta_col not in df.columns or "year" not in df.columns:
                        continue
                    for year in settings.years:
                        matches = df.loc[df["year"] == year, delta_col]
                        if matches.empty:
                            continue
                        row[f"delta_{pollutant}_{country}_{year}"] = float(matches.iloc[0])

        per_country_delta = defaultdict(lambda: defaultdict(float))
        if air_output_dir and air_pollutants:
            for pollutant in air_pollutants:
                conc_path = air_output_dir / scenario / f"{pollutant}_concentration_summary.csv"
                if not conc_path.exists():
                    continue
                try:
                    conc_df = pd.read_csv(conc_path)
                except Exception:
                    continue
                base_col = "baseline_concentration_micro_g_per_m3"
                new_col = "new_concentration_micro_g_per_m3"
                delta_col = "delta_concentration_micro_g_per_m3"
                if "year" not in conc_df.columns or delta_col not in conc_df.columns:
                    continue
                grouped = conc_df.groupby("year")
                for year in settings.years:
                    if year not in grouped.groups:
                        continue
                    subset = grouped.get_group(year)
                    weights = subset["weight"].astype(float) if "weight" in subset.columns else None
                    if weights is None or weights.sum() <= 0.0:
                        weights = pd.Series(1.0, index=subset.index, dtype=float)
                    weights = weights.fillna(0.0)
                    total = weights.sum()
                    if total <= 0.0:
                        continue
                    weights = weights / total
                    if base_col in subset.columns:
                        value = float((subset[base_col].astype(float) * weights).sum())
                        row[
                            f"baseline_concentration_{pollutant}_micro_g_per_m3_all_countries_{year}"
                        ] = value
                    if new_col in subset.columns:
                        new_value = float((subset[new_col].astype(float) * weights).sum())
                        row[
                            f"new_concentration_{pollutant}_micro_g_per_m3_all_countries_{year}"
                        ] = new_value
                    delta_value = float((subset[delta_col].astype(float) * weights).sum())
                    row[
                        f"air_pollution_concentration_delta_{pollutant}_microgram_per_m3_all_countries_{year}"
                    ] = delta_value
                    for _, entry in subset.iterrows():
                        country = entry.get("country")
                        if not isinstance(country, str):
                            continue
                        slug = _slugify_label(country)
                        if not slug:
                            continue
                        try:
                            per_country_delta = float(entry[delta_col])
                        except (TypeError, ValueError):
                            continue
                        row[
                            f"air_pollution_concentration_delta_{pollutant}_{slug}_microgram_per_m3_{year}"
                        ] = per_country_delta
            for pollutant in air_pollutants:
                stats_map = settings.air_pollution_stats.get(pollutant) or {}
                if not stats_map:
                    continue
                impact_path = air_output_dir / scenario / f"{pollutant}_health_impact.csv"
                if not impact_path.exists():
                    continue
                try:
                    impact_df = pd.read_csv(impact_path)
                except Exception:
                    continue
                required_cols = {"country", "year", "percent_change_mortality"}
                if not required_cols.issubset(impact_df.columns):
                    continue
                for _, entry in impact_df.iterrows():
                    country = entry.get("country")
                    if not isinstance(country, str):
                        continue
                    slug = _slugify_label(country)
                    if not slug:
                        continue
                    baseline = stats_map.get(slug)
                    if baseline is None:
                        continue
                    try:
                        year = int(entry["year"])
                        pct_value = float(entry["percent_change_mortality"])
                    except (TypeError, ValueError):
                        continue
                    if year not in years_set:
                        continue
                    per_country_delta[slug][year] += pct_value * baseline

        if per_country_delta:
            baseline_map = settings.air_pollution_country_baseline
            vsl = settings.air_pollution_vsl
            for slug, year_map in per_country_delta.items():
                baseline_total = baseline_map.get(slug, 0.0)
                for year, delta_value in year_map.items():
                    row[f"air_pollution_mortality_difference_{slug}_{year}"] = delta_value
                    percent = math.nan
                    if baseline_total:
                        percent = (delta_value / baseline_total) * 100.0
                    row[f"air_pollution_mortality_percent_change_{slug}_{year}"] = percent
                    if vsl:
                        row[f"air_pollution_monetary_benefit_{slug}_{year}"] = delta_value * vsl

        if pattern_dir is not None and climate_label:
            scenario_key = (base_label, climate_label)
            cache_entry = pattern_cache.setdefault(scenario_key, {})
            scenario_prefix = climate_label.lower()[:4] if climate_label else None
            aggregated_temp: dict[int, list[float]] = defaultdict(list)
            aggregated_precip: dict[int, list[float]] = defaultdict(list)
            aggregated_extreme: dict[int, list[float]] = defaultdict(list)
            for iso3 in pattern_countries:
                pattern_df = cache_entry.get(iso3)
                if pattern_df is None:
                    pattern_file = pattern_dir / iso3 / f"{base_label}_{climate_label}.csv"
                    if not pattern_file.exists():
                        pattern_file = pattern_dir / f"{iso3}_{base_label}_{climate_label}.csv"
                    if not pattern_file.exists():
                        continue
                    try:
                        pattern_df = pd.read_csv(pattern_file)
                    except Exception:
                        continue
                    cache_entry[iso3] = pattern_df
                if "year" not in pattern_df.columns:
                    continue
                for year in settings.years:
                    match = pattern_df.loc[pattern_df["year"] == year]
                    if match.empty:
                        continue
                    if "temperature_delta" in match.columns:
                        temp_val = float(match["temperature_delta"].iloc[0])
                        row[f"delta_T_{iso3}_{year}"] = temp_val
                        if math.isfinite(temp_val):
                            aggregated_temp[year].append(temp_val)
                    if "precipitation_delta_mm_per_day" in match.columns:
                        precip_day = float(match["precipitation_delta_mm_per_day"].iloc[0])
                        precip_year = precip_day * MM_PER_DAY_TO_YEAR
                        row[f"local_climate_precipitation_delta_mm_per_year_{iso3}_{year}"] = (
                            precip_year
                        )
                        if math.isfinite(precip_year):
                            aggregated_precip[year].append(precip_year)
                    damage_val = _lookup_extreme_weather(
                        extreme_weather_costs, iso3, scenario_prefix, year
                    )
                    if damage_val is not None:
                        row[f"extreme_weather_pct_gdp_{iso3}_{year}"] = damage_val
                        if math.isfinite(damage_val):
                            aggregated_extreme[year].append(damage_val)
            for year, values in aggregated_temp.items():
                if values:
                    row[f"delta_T_local_climate_C_all_countries_{year}"] = float(
                        sum(values) / len(values)
                    )
            for year, values in aggregated_precip.items():
                if values:
                    row[f"local_climate_precipitation_delta_mm_per_year_all_countries_{year}"] = (
                        float(sum(values) / len(values))
                    )
            for year, values in aggregated_extreme.items():
                if values:
                    row[f"extreme_weather_pct_gdp_all_countries_{year}"] = float(
                        sum(values) / len(values)
                    )

        rows.append(row)

    df = pd.DataFrame(rows)
    base_columns = ["energy_mix", "climate_scenario", "demand_case"]
    ordered: list[str] = []
    for year in settings.years:
        ordered.append(f"delta_co2_Mt_all_countries_{year}")
        ordered.append(f"delta_T_C_{year}")
        ordered.append(f"delta_T_local_climate_C_all_countries_{year}")
        ordered.append(f"local_climate_precipitation_delta_mm_per_year_all_countries_{year}")
        ordered.append(f"extreme_weather_pct_gdp_all_countries_{year}")
        for method in methods:
            ordered.append(f"SCC_{method}_{year}_PPP_USD_2025_discounted_to_year_per_tco2")
        for method in methods:
            ordered.append(f"damages_PPP2020_usd_baseyear_{settings.base_year}_{method}_{year}")
        ordered.append(f"air_pollution_mortality_difference_all_countries_{year}")
        ordered.append(f"air_pollution_mortality_percent_change_all_countries_{year}")
        ordered.append(f"air_pollution_monetary_benefit_all_countries_usd_{year}")
        for pollutant in settings.air_pollution_pollutants:
            ordered.append(
                f"air_pollution_concentration_delta_{pollutant}_microgram_per_m3_all_countries_{year}"
            )
        for country in settings.air_pollution_countries:
            ordered.append(f"air_pollution_mortality_difference_{country}_{year}")
            ordered.append(f"air_pollution_mortality_percent_change_{country}_{year}")
            ordered.append(f"air_pollution_monetary_benefit_{country}_{year}")
        for pollutant in settings.air_pollution_pollutants:
            for country in settings.air_pollution_countries:
                ordered.append(
                    f"air_pollution_concentration_delta_{pollutant}_{country}_microgram_per_m3_{year}"
                )
    for iso3 in pattern_countries:
        for year in settings.years:
            ordered.append(f"delta_T_{iso3}_{year}")
            ordered.append(f"local_climate_precipitation_delta_mm_per_year_{iso3}_{year}")
            ordered.append(f"extreme_weather_pct_gdp_{iso3}_{year}")
    if metrics_map:
        sample_metrics = next(iter(metrics_map.values()))
        for label in sample_metrics.mortality_sum:
            ordered.append(f"air_pollution_mortality_difference_sum_all_countries_{label}")
        for label in sample_metrics.mortality_value_sum:
            ordered.append(f"air_pollution_monetary_benefit_sum_all_countries_usd_{label}")
        for method in methods:
            for label in sample_metrics.damages_sum.get(method, {}):
                ordered.append(
                    f"damages_PPP2020_usd_baseyear_{settings.base_year}_sum_{method}_{label}"
                )
    other_columns = [col for col in df.columns if col not in base_columns + ordered]
    final_columns = (
        base_columns + [col for col in ordered if col in df.columns] + sorted(other_columns)
    )
    df = df[final_columns]
    df.to_csv(csv_path, index=False)
    return csv_path


def _pivot_metric(metrics_map: Mapping[str, ScenarioMetrics], attr: str, method: str | None = None):
    data = {}
    for scenario, metrics in metrics_map.items():
        metric_value = getattr(metrics, attr)
        if method is not None:
            metric_value = metric_value.get(method, {})
        data[scenario] = metric_value
    return data


def _load_extreme_weather_costs(
    path: Path | None,
) -> dict[tuple[str, str], dict[int, float]]:
    if path is None:
        return {}
    path = Path(path)
    if not path.exists():
        LOGGER.debug("Extreme-weather costs file not found: %s", path)
        return {}
    try:
        frame = pd.read_csv(path)
    except Exception:
        LOGGER.warning("Unable to read extreme-weather costs file at %s", path, exc_info=True)
        return {}
    normalized = {str(col).strip().lower(): col for col in frame.columns}
    country_col = normalized.get("country")
    scenario_col = normalized.get("scenario")
    if not country_col or not scenario_col:
        LOGGER.warning(
            "Extreme-weather costs file %s missing required 'Country'/'Scenario' columns",
            path,
        )
        return {}
    year_columns = [col for col in frame.columns if str(col).strip().lower().endswith("_pct_gdp")]
    if not year_columns:
        LOGGER.warning("Extreme-weather costs file %s missing '*_pct_gdp' year columns", path)
        return {}
    results: dict[tuple[str, str], dict[int, float]] = {}
    for _, entry in frame.iterrows():
        iso3 = str(entry.get(country_col, "")).strip().upper()
        scenario = str(entry.get(scenario_col, "")).strip().lower()
        if not iso3 or not scenario:
            continue
        key = (iso3, scenario)
        per_year = results.setdefault(key, {})
        for column in year_columns:
            try:
                year = int(str(column).split("_")[0])
                value = float(entry[column])
            except (ValueError, TypeError, KeyError):
                continue
            per_year[year] = value
    return results


def _lookup_extreme_weather(
    costs: Mapping[tuple[str, str], Mapping[int, float]],
    iso3: str,
    scenario_prefix: str | None,
    year: int,
) -> float | None:
    if not scenario_prefix:
        return None

    iso_key = iso3.upper()
    scenario_key = scenario_prefix.lower()

    def _value(prefix: str) -> float | None:
        per_year = costs.get((iso_key, prefix))
        if not per_year:
            return None
        value = per_year.get(year)
        return None if value is None else float(value)

    direct = _value(scenario_key)
    if direct is not None:
        return direct

    iso_scenarios = {scenario for country, scenario in costs if country == iso_key}
    if not iso_scenarios:
        return None

    target = _scenario_numeric_value(scenario_key)
    if target is None:
        return None

    fallback = min(
        iso_scenarios,
        key=lambda candidate: (
            abs((_scenario_numeric_value(candidate) or math.inf) - target),
            candidate,
        ),
        default=None,
    )
    if fallback is None:
        return None
    return _value(fallback)


def _scenario_numeric_value(label: str) -> float | None:
    for ch in label:
        if ch.isdigit():
            return float(ch)
    return None


def _plot_grouped_bars(
    data: Mapping[str, Mapping[int, float]],
    years: list[int],
    *,
    title: str,
    ylabel: str,
    output_path: Path,
    file_suffix: str,
    plot_format: str,
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover - optional dependency
        LOGGER.warning("matplotlib not available; skipping plots for %s", file_suffix)
        return

    scenarios = list(data.keys())
    x = np.arange(len(scenarios))
    if len(years) == 0:
        return
    width = 0.8 / max(len(years), 1)

    fig, ax = plt.subplots(figsize=(max(6, len(scenarios) * 1.5), 4.5))
    for idx, year in enumerate(years):
        values = [data[scenario].get(year, np.nan) for scenario in scenarios]
        offset = (idx - (len(years) - 1) / 2) * width
        ax.bar(x + offset, values, width, label=str(year))
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios, rotation=30, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend()
    ax.grid(axis="y", linestyle="--", alpha=0.3)
    output_path.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(output_path / f"{file_suffix}.{plot_format}", format=plot_format)
    plt.close(fig)


def _plot_emission_timeseries(
    data: Mapping[str, Mapping[int, float]],
    *,
    output_path: Path,
    file_name: str,
    plot_format: str,
    window: tuple[int, int],
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover - optional dependency
        LOGGER.warning("matplotlib not available; skipping emission timeseries plot")
        return

    valid_series = {scenario: series for scenario, series in data.items() if series}
    if not valid_series:
        LOGGER.info("No emission timeseries data available; skipping plot.")
        return

    fig, ax = plt.subplots(figsize=(7, 4.5))
    start_year, end_year = window
    for scenario, series in valid_series.items():
        years_sorted = sorted(year for year in series if start_year <= year <= end_year)
        if not years_sorted:
            continue
        values = [series[year] for year in years_sorted]
        ax.plot(years_sorted, values, marker="o", label=scenario)

    ax.set_xlabel("Year")
    ax.set_ylabel("Emission delta (Mt CO₂)")
    ax.set_title("Emission Delta Over Time")
    ax.grid(True, linestyle="--", alpha=0.3)
    ax.legend()

    output_path.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(output_path / f"{file_name}.{plot_format}", format=plot_format)
    plt.close(fig)


def _plot_absolute_emissions_combined(
    mix_data: Mapping[str, Mapping[str, Mapping[str, Mapping[int, float]]]],
    settings: SummarySettings,
    output_dir: Path,
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        LOGGER.warning("matplotlib not available; skipping absolute emission plots")
        return

    if not mix_data:
        return

    fig, ax = plt.subplots(figsize=(7, 4.5))
    baseline_map = _load_mix_baseline_emissions(settings.aggregate_emission_root)
    plotted = False
    for idx, (mix, climates) in enumerate(sorted(mix_data.items(), key=lambda item: str(item[0]))):
        if not climates:
            continue
        demand_map = climates.get("all") or next(iter(climates.values()), None)
        if not demand_map:
            continue
        lower = demand_map.get("scen1_lower")
        upper = demand_map.get("scen1_upper")
        center = demand_map.get("scen1_mean") or demand_map.get(BASE_DEMAND_CASE)
        if center is None and lower and upper:
            center = {
                year: (lower_val + upper[year]) / 2.0
                for year, lower_val in lower.items()
                if year in upper
            }
        if center is None or lower is None or upper is None:
            continue
        years = sorted(
            y
            for y in set(center) & set(lower) & set(upper)
            if settings.plot_start <= y <= settings.plot_end
        )
        if not years:
            continue
        center_vals = [center[y] for y in years]
        lower_vals = [lower[y] for y in years]
        upper_vals = [upper[y] for y in years]
        color = f"C{idx % 10}"
        ax.plot(years, center_vals, label=mix, color=color, linewidth=2)
        ax.fill_between(years, lower_vals, upper_vals, color=color, alpha=0.15)
        baseline_series = baseline_map.get(mix) or demand_map.get(BASE_DEMAND_CASE)
        if baseline_series:
            baseline_years = sorted(
                y for y in baseline_series if settings.plot_start <= y <= settings.plot_end
            )
            if baseline_years:
                baseline_vals = [baseline_series[y] for y in baseline_years]
                ax.plot(
                    baseline_years,
                    baseline_vals,
                    color=color,
                    linestyle="--",
                    linewidth=1.5,
                    label=f"{mix} base demand",
                )
        plotted = True

    if not plotted:
        plt.close(fig)
        return

    ax.set_title("Absolute Emissions — All Countries")
    ax.set_xlabel("Year")
    ax.set_ylabel("Mt CO₂/year")
    ax.grid(True, linestyle="--", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(
        output_dir / f"absolute_emissions_all_mix.{settings.plot_format}",
        format=settings.plot_format,
    )
    plt.close(fig)


def _plot_emission_difference_vs_base(
    mix_data: Mapping[str, Mapping[str, Mapping[str, Mapping[int, float]]]],
    settings: SummarySettings,
    output_dir: Path,
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        LOGGER.warning("matplotlib not available; skipping emission-difference plot")
        return

    if not mix_data:
        return

    baseline_map = _load_mix_baseline_emissions(settings.aggregate_emission_root)
    if not baseline_map:
        LOGGER.info("No baseline emissions found; skipping emission-difference plot.")
        return

    fig, ax = plt.subplots(figsize=(7, 4.5))
    plotted = False
    for idx, (mix, climates) in enumerate(sorted(mix_data.items(), key=lambda item: str(item[0]))):
        if not climates:
            continue
        demand_map = climates.get("all") or next(iter(climates.values()), None)
        if not demand_map:
            continue
        baseline_series = baseline_map.get(mix)
        lower = demand_map.get("scen1_lower")
        upper = demand_map.get("scen1_upper")
        center = demand_map.get("scen1_mean") or demand_map.get(BASE_DEMAND_CASE)
        if center is None and lower and upper:
            center = {
                year: (lower_val + upper[year]) / 2.0
                for year, lower_val in lower.items()
                if year in upper
            }
        if center is None or lower is None or upper is None or baseline_series is None:
            continue
        years = sorted(
            y
            for y in set(center) & set(lower) & set(upper) & set(baseline_series)
            if settings.plot_start <= y <= settings.plot_end
        )
        if not years:
            continue
        center_vals = [center[y] - baseline_series[y] for y in years]
        lower_vals = [lower[y] - baseline_series[y] for y in years]
        upper_vals = [upper[y] - baseline_series[y] for y in years]
        color = f"C{idx % 10}"
        ax.plot(years, center_vals, label=mix, color=color, linewidth=2)
        ax.fill_between(years, lower_vals, upper_vals, color=color, alpha=0.15)
        plotted = True

    if not plotted:
        plt.close(fig)
        return

    ax.axhline(0, color="k", linestyle="--", linewidth=1, alpha=0.7)
    ax.set_title("Emission Difference vs Base Demand — All Countries")
    ax.set_xlabel("Year")
    ax.set_ylabel("Δ Mt CO₂/year (vs base demand)")
    ax.grid(True, linestyle="--", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(
        output_dir / f"emission_difference_vs_base_all_mix.{settings.plot_format}",
        format=settings.plot_format,
    )
    plt.close(fig)


def _plot_emission_difference_vs_global_baseline(
    mix_data: Mapping[str, Mapping[str, Mapping[str, Mapping[int, float]]]],
    settings: SummarySettings,
    output_dir: Path,
    *,
    baseline_mix: str = BASE_MIX_CASE,
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        LOGGER.warning("matplotlib not available; skipping global-baseline emission plot")
        return

    if not mix_data:
        return

    baseline_map = _load_mix_baseline_emissions(settings.aggregate_emission_root)
    global_baseline = baseline_map.get(baseline_mix)
    if not global_baseline:
        LOGGER.info(
            "Global baseline emissions not found for mix '%s'; skipping plot.",
            baseline_mix,
        )
        return

    fig, ax = plt.subplots(figsize=(7, 4.5))
    plotted = False
    for idx, (mix, climates) in enumerate(sorted(mix_data.items(), key=lambda item: str(item[0]))):
        if not climates:
            continue
        demand_map = climates.get("all") or next(iter(climates.values()), None)
        if not demand_map:
            continue
        lower = demand_map.get("scen1_lower")
        upper = demand_map.get("scen1_upper")
        center = demand_map.get("scen1_mean") or demand_map.get(BASE_DEMAND_CASE)
        if center is None and lower and upper:
            center = {
                year: (lower_val + upper[year]) / 2.0
                for year, lower_val in lower.items()
                if year in upper
            }
        if center is None or lower is None or upper is None:
            continue
        years = sorted(
            y
            for y in set(center) & set(lower) & set(upper) & set(global_baseline)
            if settings.plot_start <= y <= settings.plot_end
        )
        if not years:
            continue
        center_vals = [center[y] - global_baseline[y] for y in years]
        lower_vals = [lower[y] - global_baseline[y] for y in years]
        upper_vals = [upper[y] - global_baseline[y] for y in years]
        color = f"C{idx % 10}"
        ax.plot(years, center_vals, label=mix, color=color, linewidth=2)
        ax.fill_between(years, lower_vals, upper_vals, color=color, alpha=0.15)
        plotted = True

    if not plotted:
        plt.close(fig)
        return

    ax.axhline(0, color="k", linestyle="--", linewidth=1, alpha=0.7)
    ax.set_title("Emission Difference vs Global Baseline — All Countries")
    ax.set_xlabel("Year")
    ax.set_ylabel(f"Δ Mt CO₂/year (vs {baseline_mix} {BASE_DEMAND_CASE})")
    ax.grid(True, linestyle="--", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(
        output_dir / f"emission_difference_vs_global_baseline_all_mix.{settings.plot_format}",
        format=settings.plot_format,
    )
    plt.close(fig)


def _plot_metric_all_mix(
    mix_data: Mapping[str, Mapping[str, Mapping[str, Mapping[int, float]]]],
    settings: SummarySettings,
    output_dir: Path,
    *,
    title: str,
    ylabel: str,
    file_stem: str,
) -> None:
    """Plot a single timeseries chart overlaying all mixes (with uncertainty envelopes)."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        LOGGER.warning("matplotlib not available; skipping %s plot", file_stem)
        return

    if not mix_data:
        return

    preferred_order = ["base_mix", "WEM", "WAM"]

    def _mix_sort_key(mix: str) -> tuple[int, str]:
        try:
            return (preferred_order.index(mix), mix)
        except ValueError:
            return (len(preferred_order), mix)

    fig, ax = plt.subplots(figsize=(7, 4.5))
    plotted = False

    for idx, mix in enumerate(sorted(mix_data, key=_mix_sort_key)):
        climates = mix_data.get(mix, {})
        if not climates:
            continue
        demand_map = climates.get("all") or next(iter(climates.values()), None)
        if not demand_map:
            continue
        lower = demand_map.get("scen1_lower")
        upper = demand_map.get("scen1_upper")
        center = demand_map.get("scen1_mean") or demand_map.get(BASE_DEMAND_CASE)
        if center is None and lower and upper:
            center = {
                year: (lower_val + upper[year]) / 2.0
                for year, lower_val in lower.items()
                if year in upper
            }
        if center is None or lower is None or upper is None:
            continue
        years = sorted(
            y
            for y in set(center) & set(lower) & set(upper)
            if settings.plot_start <= y <= settings.plot_end
        )
        if not years:
            continue
        center_vals = [center[y] for y in years]
        lower_vals = [lower[y] for y in years]
        upper_vals = [upper[y] for y in years]
        color = f"C{idx % 10}"
        ax.plot(years, center_vals, label=mix, color=color, linewidth=2)
        ax.fill_between(years, lower_vals, upper_vals, color=color, alpha=0.15)
        plotted = True

    if not plotted:
        plt.close(fig)
        return

    ax.set_title(title)
    ax.set_xlabel("Year")
    ax.set_ylabel(ylabel)
    ax.grid(True, linestyle="--", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_dir / f"{file_stem}.{settings.plot_format}", format=settings.plot_format)
    plt.close(fig)


def _plot_absolute_emissions_bars(
    mix_data: Mapping[str, Mapping[str, Mapping[str, Mapping[int, float]]]],
    settings: SummarySettings,
    output_dir: Path,
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        LOGGER.warning("matplotlib not available; skipping absolute emission bar plots")
        return

    if not mix_data:
        return

    mixes = []
    mix_series: dict[str, tuple[dict[int, float], dict[int, float], dict[int, float]]] = {}
    for mix, climates in mix_data.items():
        demand_map = climates.get("all") or next(iter(climates.values()), None)
        if not demand_map:
            continue
        lower = demand_map.get("scen1_lower")
        upper = demand_map.get("scen1_upper")
        center = demand_map.get("scen1_mean") or demand_map.get(BASE_DEMAND_CASE)
        if center is None and lower and upper:
            center = {
                year: (lower_val + upper[year]) / 2.0
                for year, lower_val in lower.items()
                if year in upper
            }
        if center is None or lower is None or upper is None:
            continue
        mixes.append(mix)
        mix_series[mix] = (center, lower, upper)

    if not mixes:
        return

    candidate_years = [
        y for y in range(2025, 2051, 5) if settings.plot_start <= y <= settings.plot_end
    ]
    years = [y for y in candidate_years if any(y in center for center, _, _ in mix_series.values())]
    if not years:
        return

    x = np.arange(len(years))
    width = 0.8 / max(len(mixes), 1)
    fig, ax = plt.subplots(figsize=(max(6, len(years) * 1.6), 4.5))

    for idx, mix in enumerate(sorted(mixes)):
        center, lower, upper = mix_series[mix]
        vals = [center.get(y, np.nan) for y in years]
        err_lower = [max(0.0, center.get(y, np.nan) - lower.get(y, np.nan)) for y in years]
        err_upper = [max(0.0, upper.get(y, np.nan) - center.get(y, np.nan)) for y in years]
        offsets = x + (idx - (len(mixes) - 1) / 2) * width
        ax.bar(
            offsets,
            vals,
            width,
            label=mix,
            yerr=[err_lower, err_upper],
            capsize=4,
        )

    ax.set_xticks(x)
    ax.set_xticklabels([str(y) for y in years])
    ax.set_xlabel("Year")
    ax.set_ylabel("Mt CO₂/year")
    ax.set_title("Absolute Emissions — Bars with Uncertainty")
    ax.grid(axis="y", linestyle="--", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(
        output_dir / f"absolute_emissions_bar.{settings.plot_format}",
        format=settings.plot_format,
    )
    plt.close(fig)


def _load_country_absolute_emissions(
    per_country_root: Path | None,
) -> dict[str, dict[str, dict[str, dict[int, float]]]]:
    """Load absolute emission timeseries per country and mix."""
    if per_country_root is None:
        return {}
    root = Path(per_country_root)
    if not root.exists():
        return {}
    try:
        mix_dirs = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        return {}
    mix_names = {path.name for path in mix_dirs}

    country_map: dict[str, dict[str, dict[str, dict[int, float]]]] = {}
    for mix_dir in mix_dirs:
        mix_name = mix_dir.name
        try:
            country_dirs = [child for child in mix_dir.iterdir() if child.is_dir()]
        except OSError:
            continue
        for country_dir in country_dirs:
            if country_dir.name in mix_names:
                continue
            co2_path = country_dir / "co2.csv"
            if not co2_path.exists():
                continue
            try:
                frame = pd.read_csv(co2_path, comment="#")
            except Exception:
                continue
            if "year" not in frame.columns:
                continue
            try:
                frame["year"] = frame["year"].astype(int)
            except Exception:
                continue
            for column in frame.columns:
                if not column.startswith("absolute_"):
                    continue
                demand_case = column.removeprefix("absolute_")
                try:
                    values = frame[column].astype(float)
                except Exception:
                    continue
                series = {
                    int(year): float(value)
                    for year, value in zip(frame["year"], values, strict=False)
                    if math.isfinite(value)
                }
                if not series:
                    continue
                demand_map = country_map.setdefault(country_dir.name, {}).setdefault(mix_name, {})
                demand_map[demand_case] = series
    return country_map


def _plot_absolute_emissions_country_tiles(
    settings: SummarySettings,
    output_dir: Path,
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        LOGGER.warning("matplotlib not available; skipping country emission tiles")
        return

    country_data = _load_country_absolute_emissions(settings.per_country_emission_root)
    if not country_data:
        return

    countries = sorted(country_data)
    n_countries = len(countries)
    ncols = min(3, n_countries) or 1
    nrows = int(math.ceil(n_countries / ncols))
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(ncols * 4.2, nrows * 3.2),
        sharex=True,
        sharey=False,
        layout="constrained",
    )
    axes_flat = np.atleast_1d(axes).ravel()
    legend_handles: dict[str, object] = {}
    baseline_handles: dict[str, object] = {}

    for ax, country in zip(axes_flat, countries, strict=False):
        mix_map = country_data.get(country, {})
        plotted = False
        for idx, (mix, demand_map) in enumerate(sorted(mix_map.items(), key=lambda item: item[0])):
            lower = demand_map.get("scen1_lower")
            upper = demand_map.get("scen1_upper")
            center = demand_map.get("scen1_mean") or demand_map.get(BASE_DEMAND_CASE)
            if center is None and lower and upper:
                center = {
                    year: (lower_val + upper.get(year, lower_val)) / 2.0
                    for year, lower_val in lower.items()
                    if year in upper
                }
            if center is None or lower is None or upper is None:
                continue
            years = sorted(
                y
                for y in set(center) & set(lower) & set(upper)
                if settings.plot_start <= y <= settings.plot_end
            )
            if not years:
                continue
            color = f"C{idx % 10}"
            central_vals = [center[y] for y in years]
            lower_vals = [lower[y] for y in years]
            upper_vals = [upper[y] for y in years]
            line = ax.plot(years, central_vals, label=mix, color=color, linewidth=1.6)[0]
            ax.fill_between(years, lower_vals, upper_vals, color=color, alpha=0.14)
            baseline = demand_map.get(BASE_DEMAND_CASE)
            if baseline:
                baseline_years = [
                    y for y in sorted(baseline) if settings.plot_start <= y <= settings.plot_end
                ]
                if baseline_years:
                    baseline_vals = [baseline[y] for y in baseline_years]
                    baseline_line = ax.plot(
                        baseline_years,
                        baseline_vals,
                        color=color,
                        linestyle="--",
                        linewidth=1.1,
                    )[0]
                    baseline_label = f"{mix} base demand"
                    baseline_handles.setdefault(baseline_label, baseline_line)
            legend_handles.setdefault(mix, line)
            plotted = True

        if not plotted:
            ax.axis("off")
            continue
        title = country.replace("_", " ")
        ax.set_title(title, fontsize=10)
        ax.grid(True, linestyle="--", alpha=0.25)

    for ax in axes_flat[len(countries) :]:
        ax.axis("off")

    fig.supylabel("Mt CO₂/year")
    fig.suptitle("Absolute Emissions by Country and Energy Mix", fontsize=12)
    preferred_order = ["base_mix", "WEM", "WAM"]
    baseline_order = [f"{label} base demand" for label in preferred_order]

    def _ordered_handles(
        handle_map: dict[str, object], order: list[str]
    ) -> list[tuple[str, object]]:
        entries: list[tuple[str, object]] = []
        for label in order:
            if label in handle_map:
                entries.append((label, handle_map[label]))
        for label, handle in handle_map.items():
            if label not in order:
                entries.append((label, handle))
        return entries

    main_entries = _ordered_handles(legend_handles, preferred_order)
    baseline_entries = _ordered_handles(baseline_handles, baseline_order)
    all_entries = main_entries + baseline_entries

    if all_entries:
        labels, handles = zip(*all_entries, strict=False)
        fig.legend(
            handles,
            labels,
            loc="lower center",
            ncol=min(len(preferred_order), 3),
            bbox_to_anchor=(0.5, 0),
        )
    fig.tight_layout(rect=(0, 0.1, 1, 0.98))
    fig.savefig(
        output_dir / f"absolute_emissions_country_tiles.{settings.plot_format}",
        format=settings.plot_format,
    )
    plt.close(fig)


def _plot_temperature_timeseries(
    data: Mapping[str, Mapping[int, float] | None],
    *,
    output_path: Path,
    file_name: str,
    plot_format: str,
    window: tuple[int, int],
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover - optional dependency
        LOGGER.warning("matplotlib not available; skipping temperature timeseries plot")
        return

    valid_series = {
        scenario: series
        for scenario, series in data.items()
        if series is not None and len(series) > 0
    }
    if not valid_series:
        LOGGER.debug("No temperature timeseries data available; skipping plot.")
        return
    # Compute mean across scenarios and plot a single line
    all_years = sorted({year for series in valid_series.values() for year in series})
    mean_series: dict[int, float] = {}
    for year in all_years:
        vals = [series.get(year) for series in valid_series.values() if year in series]
        if vals:
            mean_series[year] = float(np.mean(vals))

    fig, ax = plt.subplots(figsize=(7, 4.5))
    start_year, end_year = window
    years_sorted = [y for y in sorted(mean_series) if start_year <= y <= end_year]
    if years_sorted:
        values = [mean_series[y] for y in years_sorted]
        ax.plot(years_sorted, values, marker="o", label="mean")

    ax.set_xlabel("Year")
    ax.set_ylabel("Temperature delta (°C)")
    ax.set_title("Temperature Delta Over Time")
    ax.grid(True, linestyle="--", alpha=0.3)
    ax.legend()

    output_path.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(output_path / f"{file_name}.{plot_format}", format=plot_format)
    plt.close(fig)


def _load_mix_baseline_emissions(emission_root: Path | None) -> dict[str, dict[int, float]]:
    if emission_root is None:
        return {}
    emission_root = Path(emission_root)
    if not emission_root.exists():
        return {}
    baselines: dict[str, dict[int, float]] = {}
    try:
        mix_dirs = [path for path in emission_root.iterdir() if path.is_dir()]
    except OSError:
        return {}
    column = f"absolute_{BASE_DEMAND_CASE}"
    for mix_dir in mix_dirs:
        co2_path = mix_dir / "co2.csv"
        if not co2_path.exists():
            continue
        try:
            frame = pd.read_csv(co2_path, comment="#")
        except Exception:
            continue
        if "year" not in frame.columns or column not in frame.columns:
            continue
        try:
            years = frame["year"].astype(int)
            values = frame[column].astype(float)
        except Exception:
            continue
        data = {
            int(year): float(value)
            for year, value in zip(years, values, strict=False)
            if math.isfinite(value)
        }
        if data:
            baselines[mix_dir.name] = data
    return baselines


def _plot_temperature_envelopes(
    metrics_map: Mapping[str, ScenarioMetrics], settings: SummarySettings, output_dir: Path
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        LOGGER.warning("matplotlib not available; skipping temperature envelopes")
        return

    grouped: dict[tuple[str, str | None], dict[str, Mapping[int, float]]] = {}
    for scenario, metrics in metrics_map.items():
        base, climate = _split_climate_suffix(scenario, settings.climate_labels)
        try:
            mix, demand = split_scenario_name(base)
        except ValueError:
            continue
        series = metrics.temperature_timeseries
        if not series:
            continue
        grouped.setdefault((mix, climate), {})[demand] = series

    # One plot per mix, multiple SSPs in same figure
    mixes = {}
    for (mix, climate), demand_map in grouped.items():
        mixes.setdefault(mix, {})[climate or "all"] = demand_map

    for mix, climates in mixes.items():
        fig, ax = plt.subplots(figsize=(7, 4.5))
        has_data = False
        for idx, (climate, demand_map) in enumerate(sorted(climates.items(), key=lambda x: x[0])):
            mean_series = demand_map.get("scen1_mean")
            lower_series = demand_map.get("scen1_lower")
            upper_series = demand_map.get("scen1_upper")
            if mean_series is None or lower_series is None or upper_series is None:
                continue
            years = sorted(set(mean_series) & set(lower_series) & set(upper_series))
            if not years:
                continue
            x = [int(y) for y in years if settings.plot_start <= y <= settings.plot_end]
            if not x:
                continue
            mean_vals = [mean_series[y] for y in x]
            lower_vals = [lower_series[y] for y in x]
            upper_vals = [upper_series[y] for y in x]
            color = f"C{idx % 10}"
            ax.plot(x, mean_vals, label=climate or "all SSPs", color=color)
            ax.fill_between(x, lower_vals, upper_vals, color=color, alpha=0.2)
            has_data = True
        if not has_data:
            plt.close(fig)
            continue
        ax.set_xlabel("Year")
        ax.set_ylabel("Temperature delta (°C)")
        ax.set_title(f"Temperature Delta — {mix}")
        ax.grid(True, linestyle="--", alpha=0.3)
        ax.legend()
        mix_dir = output_dir / mix
        mix_dir.mkdir(parents=True, exist_ok=True)
        fname = f"temperature_delta_envelopes.{settings.plot_format}"
        fig.tight_layout()
        fig.savefig(mix_dir / fname, format=settings.plot_format)
        plt.close(fig)


def _plot_scc_timeseries(
    settings: SummarySettings,
    methods: Iterable[str],
    metrics_map: Mapping[str, ScenarioMetrics],
) -> None:
    if settings.scc_output_directory is None:
        LOGGER.info("SCC output directory not available; skipping SCC timeseries plot.")
        return
    LOGGER.debug("SCC output directory: %s", settings.scc_output_directory)
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover - optional dependency
        LOGGER.warning("matplotlib not available; skipping SCC timeseries plot")
        return

    output_dir = settings.output_directory / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Single plot: SCC per SSP over full horizon
    label_candidates = list(settings.climate_labels or [])
    if not label_candidates and settings.scc_output_directory is not None:
        auto_labels: set[str] = set()
        for path in settings.scc_output_directory.glob("pulse_scc_timeseries_*.csv"):
            stem = path.stem  # pulse_scc_timeseries_<method>_<label>
            parts = stem.split("_")
            if len(parts) < 4:
                continue
            auto_labels.add(parts[-1])
        if auto_labels:
            label_candidates = sorted(auto_labels)
    if not label_candidates:
        label_candidates = ["default"]
    methods = list(methods)
    for method in methods:
        curves: list[tuple[list[int], list[float], str]] = []
        for climate in label_candidates:
            label = climate or "default"
            safe_label = _safe_name(label)
            candidates = [
                settings.scc_output_directory / f"pulse_scc_timeseries_{method}_{label}.csv",
                settings.scc_output_directory / f"pulse_scc_timeseries_{method}_{safe_label}.csv",
            ]
            path = next((p for p in candidates if p.exists()), None)
            if path is None:
                continue
            LOGGER.debug("Reading SCC timeseries from %s", path)
            try:
                df = pd.read_csv(path)
            except Exception:
                continue
            if "year" not in df.columns or "scc_usd_per_tco2" not in df.columns:
                continue
            df["year"] = df["year"].astype(int)
            df = df.sort_values("year")
            window = df[(df["year"] >= settings.plot_start) & (df["year"] <= settings.plot_end)]
            if window.empty:
                continue
            years_sorted = window["year"].tolist()
            values = window["scc_usd_per_tco2"].astype(float).tolist()
            curves.append((years_sorted, values, label))

        if not curves:
            continue

        fig, ax = plt.subplots(figsize=(7, 4.5))
        for years, values, label in curves:
            ax.plot(years, values, marker="o", linewidth=1.6, label=label)
        ax.set_xlabel("t (Year)")
        ax.set_ylabel("SCC (PPP USD-2025 per tCO₂, discounted to t)")
        ax.set_title("SCC(t)")
        ax.grid(True, linestyle="--", alpha=0.3)
        ax.legend()
        fig.tight_layout()
        filename = "scc_timeseries" if len(methods) == 1 else f"scc_timeseries_{method}"
        fig.savefig(
            output_dir / f"{filename}.{settings.plot_format}",
            format=settings.plot_format,
        )
        plt.close(fig)


def write_plots(
    settings: SummarySettings,
    methods: Iterable[str],
    metrics_map: Mapping[str, ScenarioMetrics],
) -> None:
    if not settings.include_plots:
        LOGGER.info("Plot generation disabled via configuration.")
        return

    output_dir = settings.output_directory / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Temperature envelopes per mix/SSP
    _plot_temperature_timeseries(
        {},
        output_path=output_dir,
        file_name="temperature_delta_timeseries",  # legacy placeholder; envelopes handled below
        plot_format=settings.plot_format,
        window=(settings.plot_start, settings.plot_end),
    )
    _plot_temperature_envelopes(metrics_map, settings, output_dir)
    _plot_socioeconomic_timeseries(settings)
    _include_background_plots(settings, output_dir)

    # SCC timeseries per method/SSP (single PNG per method)
    _plot_scc_timeseries(settings, methods, metrics_map)
    _plot_damage_function(settings)

    def _collect_by_mix(attr: str, method: str | None = None):
        data: dict[str, dict[str, dict[str, dict[int, float]]]] = {}
        for scenario, metrics in metrics_map.items():
            base, climate = _split_climate_suffix(scenario, settings.climate_labels)
            try:
                mix, demand = split_scenario_name(base)
            except ValueError:
                continue
            series = getattr(metrics, attr)
            if attr == "emission_delta_mt" and metrics.emission_timeseries:
                series = metrics.emission_timeseries
            if attr == "mortality_delta" and metrics.mortality_delta_timeseries:
                series = metrics.mortality_delta_timeseries
            if attr == "mortality_value_delta" and metrics.mortality_value_timeseries:
                series = metrics.mortality_value_timeseries
            if method and isinstance(series, Mapping):
                if attr == "damages_usd" and metrics.damages_usd_timeseries:
                    series = metrics.damages_usd_timeseries.get(method, {})
                else:
                    series = series.get(method, {})
            if not isinstance(series, Mapping):
                continue
            mapping = data.setdefault(mix, {}).setdefault(climate or "all", {})
            mapping[demand] = {int(k): float(v) for k, v in series.items()}
        return data

    mix_emission = _collect_by_mix("emission_delta_mt")
    mix_absolute_emission = _collect_by_mix("emission_absolute_timeseries")
    mix_mortality = _collect_by_mix("mortality_delta")
    mix_mortality_value = _collect_by_mix("mortality_value_delta")
    mix_damages = _collect_by_mix("damages_usd", method="ramsey_discount")

    _plot_metric_all_mix(
        mix_mortality,
        settings,
        output_dir,
        title="Mortality Delta — All Mixes",
        ylabel="deaths/year",
        file_stem="mortality_delta_all_mix",
    )
    _plot_metric_all_mix(
        mix_mortality_value,
        settings,
        output_dir,
        title="Mortality Value Delta — All Mixes",
        ylabel="USD/year",
        file_stem="mortality_value_all_mix",
    )

    _plot_lines_for_mix(
        mix_emission,
        "emission_delta",
        "Mt CO₂",
        settings,
        output_dir,
        per_climate=False,
    )
    _plot_absolute_emissions_combined(
        mix_absolute_emission,
        settings,
        output_dir,
    )
    _plot_emission_difference_vs_base(
        mix_absolute_emission,
        settings,
        output_dir,
    )
    _plot_emission_difference_vs_global_baseline(
        mix_absolute_emission,
        settings,
        output_dir,
        baseline_mix=settings.baseline_mix_case,
    )
    _plot_absolute_emissions_bars(
        mix_absolute_emission,
        settings,
        output_dir,
    )
    _plot_absolute_emissions_country_tiles(
        settings,
        output_dir,
    )
    _plot_lines_for_mix(
        mix_mortality,
        "mortality_delta",
        "deaths/year",
        settings,
        output_dir,
        per_climate=False,
    )
    _plot_lines_for_mix(
        mix_mortality_value,
        "mortality_value",
        "USD/year",
        settings,
        output_dir,
        per_climate=False,
    )
    _plot_lines_for_mix(
        mix_damages,
        "damages",
        "USD",
        settings,
        output_dir,
    )


def write_socioeconomic_tables(settings: SummarySettings) -> list[Path]:
    """Write detailed socioeconomic projections per climate/scenario."""
    outputs: list[Path] = []
    output_dir = settings.scc_output_directory or settings.output_directory
    if settings.socio_frames is None:
        return outputs

    for label, frame in settings.socio_frames.items():
        if frame is None or frame.empty:
            continue
        safe_label = _safe_name(label)
        path = output_dir / f"socioeconomics_{safe_label}.csv"
        currency_label = frame.attrs.get("currency_label", f"USD_{settings.base_year}")
        socio_frame = frame.copy()
        socio_frame.attrs["currency_label"] = currency_label
        rename_map = {
            "gdp_trillion_usd": f"gdp_trillion_usd_{currency_label}",
            "gdp_per_capita_usd": f"gdp_per_capita_usd_{currency_label}",
            "consumption_trillion_usd": f"consumption_trillion_usd_{currency_label}",
            "consumption_per_capita_usd": f"consumption_per_capita_usd_{currency_label}",
        }
        socio_frame = socio_frame.rename(
            columns={k: v for k, v in rename_map.items() if k in socio_frame.columns}
        )
        try:
            socio_frame.to_csv(path, index=False)
        except Exception:
            continue
        outputs.append(path)

    for old_name in ("socioeconomics_gdp.csv", "socioeconomics_population.csv"):
        with contextlib.suppress(Exception):
            (output_dir / old_name).unlink(missing_ok=True)
    return outputs


def _include_background_plots(settings: SummarySettings, plots_dir: Path) -> None:
    climate_dir = settings.climate_output_directory
    if climate_dir is None:
        return
    for stem in ("background_climate_full", "background_climate_horizon"):
        source = climate_dir / f"{stem}.png"
        if not source.exists():
            continue
        destination = plots_dir / source.name
        try:
            shutil.copyfile(source, destination)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            LOGGER.warning("Unable to copy %s into summary plots: %s", source, exc)


def _plot_socioeconomic_timeseries(settings: SummarySettings) -> None:
    if not settings.gdp_series:
        return
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover - optional dependency
        LOGGER.warning("matplotlib not available; skipping socioeconomics plot")
        return

    plots_dir = settings.output_directory / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    years = list(range(settings.plot_start, settings.plot_end + 1))
    if not years:
        return

    fig, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    gdp_series = _collapse_by_climate_label(settings.gdp_series, settings.climate_labels)
    pop_series = (
        _collapse_by_climate_label(settings.population_series, settings.climate_labels)
        if settings.population_series
        else None
    )

    scenarios = sorted(gdp_series.keys())
    for scenario in scenarios:
        series = gdp_series[scenario]
        values = [series.get(year, math.nan) for year in years]
        axes[0].plot(years, values, label=scenario)
    axes[0].set_ylabel("GDP (trillion USD)")
    axes[0].set_title("Socioeconomic trajectories")
    axes[0].grid(True, alpha=0.3)
    axes[0].legend(loc="best", fontsize=8)

    if pop_series:
        for scenario in sorted(pop_series.keys()):
            series = pop_series.get(scenario)
            if not series:
                continue
            values = [series.get(year, math.nan) for year in years]
            axes[1].plot(years, values, label=scenario)
        axes[1].set_ylabel("Population (million)")
        axes[1].legend(loc="best", fontsize=8)
    else:
        axes[1].set_visible(False)
    axes[-1].set_xlabel("Year")
    fig.tight_layout()
    fig.savefig(
        plots_dir / f"socioeconomics.{settings.plot_format}",
        format=settings.plot_format,
    )
    plt.close(fig)


def _plot_damage_function(settings: SummarySettings) -> None:
    cfg = settings.damage_function_cfg
    if not isinstance(cfg, Mapping) or not cfg:
        return
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        LOGGER.warning("matplotlib not available; skipping damage-function plot")
        return

    plots_dir = settings.output_directory / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    cfg_copy = dict(cfg)
    max_temp = float(cfg_copy.pop("plot_max_temperature", 8.0) or 8.0)
    cfg_copy.pop("mode", None)
    max_temp = max(1.0, max_temp)
    temps = np.linspace(0.0, max_temp, 400)
    try:
        fractions = damage_dice(temps, **cfg_copy)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("Unable to plot damage function: %s", exc)
        return
    fractions = np.clip(fractions, 0.0, 1.0)

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(temps, fractions * 100.0, linewidth=2.0, color="#C0504D")
    ax.set_xlabel("T (°C)")
    ax.set_ylabel("Damage (% of GDP)")
    ax.set_title("Damage Function")
    ax.grid(True, alpha=0.3, linestyle="--")
    fig.tight_layout()
    fig.savefig(plots_dir / f"damage_function.{settings.plot_format}", format=settings.plot_format)
    plt.close(fig)


def _parse_year_periods(summary_cfg: Mapping[str, object]) -> list[tuple[int, int]]:
    periods_cfg = summary_cfg.get("year_period", []) if isinstance(summary_cfg, Mapping) else []
    periods: list[tuple[int, int]] = []
    if isinstance(periods_cfg, Mapping):
        periods_cfg = [periods_cfg]
    if isinstance(periods_cfg, Iterable) and not isinstance(periods_cfg, (str, bytes)):
        for entry in periods_cfg:
            if not isinstance(entry, Mapping):
                continue
            try:
                start = int(entry.get("start"))
                end = int(entry.get("end"))
            except Exception:
                continue
            if start >= end:
                continue
            periods.append((start, end))
    return periods


def _plot_lines_for_mix(
    mix_data: Mapping[str, Mapping[str, Mapping[str, Mapping[int, float]]]],
    metric_name: str,
    ylabel: str,
    settings: SummarySettings,
    output_dir: Path,
    *,
    per_climate: bool = True,
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        LOGGER.warning("matplotlib not available; skipping %s plots", metric_name)
        return

    if not mix_data:
        return

    for mix, climates in mix_data.items():
        if not climates:
            continue
        if per_climate:
            data_entries = sorted(climates.items(), key=lambda item: str(item[0]))
        else:
            representative = climates.get("all") or next(iter(climates.values()), None)
            data_entries = [("aggregate", representative)] if representative else []
        if not data_entries:
            continue

        fig, ax = plt.subplots(figsize=(7, 4.5))
        plotted = False
        for idx, (label_key, demand_map) in enumerate(data_entries):
            if not demand_map:
                continue
            center = demand_map.get("scen1_mean") or demand_map.get(BASE_DEMAND_CASE)
            lower = demand_map.get("scen1_lower")
            upper = demand_map.get("scen1_upper")
            if center is None or lower is None or upper is None:
                continue
            yrs = sorted(
                y
                for y in set(center) & set(lower) & set(upper)
                if settings.plot_start <= y <= settings.plot_end
            )
            if not yrs:
                continue
            central_vals = [center[y] for y in yrs]
            lower_vals = [lower[y] for y in yrs]
            upper_vals = [upper[y] for y in yrs]
            color = f"C{idx % 10}"
            label = label_key if per_climate else "mean"
            ax.plot(yrs, central_vals, label=label, color=color, linewidth=2)
            ax.fill_between(yrs, lower_vals, upper_vals, color=color, alpha=0.15)
            plotted = True

        if not plotted:
            plt.close(fig)
            continue

        ax.set_title(f"{metric_name.replace('_', ' ').title()} — {mix}")
        ax.set_xlabel("Year")
        ax.set_ylabel(ylabel)
        ax.grid(True, linestyle="--", alpha=0.3)
        if per_climate:
            ax.legend()
        subdir = output_dir / mix
        subdir.mkdir(parents=True, exist_ok=True)
        fname = f"{metric_name}.{settings.plot_format}"
        fig.tight_layout()
        fig.savefig(subdir / fname, format=settings.plot_format)
        plt.close(fig)
