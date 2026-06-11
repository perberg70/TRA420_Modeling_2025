"""Compute the social cost of carbon using precomputed temperature and emission scenarios.

Usage
-----
```bash
python scripts/run_scc.py --method constant_discount \
    --temperature baseline=results/climate/baseline_t.csv \
    --temperature policy=results/climate/policy_t.csv \
    --emission baseline=data/baseline_e.csv --emission policy=data/policy_e.csv

python scripts/run_scc.py --method ramsey_discount --rho 0.01 --eta 1.5 --aggregation per_year
python scripts/run_scc.py --method all
```

Inputs
------
- Temperature CSVs must expose ``year`` and the column named via
  ``--temperature-column`` (default ``temperature_adjusted``).
- Emission CSVs supply annual CO₂ deltas with ``year`` plus
  ``--emission-column`` (default ``delta``); values are converted to tonnes
  using ``--emission-unit-multiplier``.
- Temperature CSVs exported by the climate module now include
  ``climate_scenario`` to signal which SSP family should drive GDP/population
  selection.
- Scenario labels supplied for temperatures and emissions must match exactly.
- Aggregation can be ``average`` (policy-wide USD/tCO₂) or ``per_year`` (time-varying SCC series).
- Damage function parameters (DICE ``delta1`` / ``delta2`` and optional threshold, saturation,
  and catastrophe extensions) can be set in ``config.yaml`` or overridden via the
  ``--damage-*`` CLI options.

The script loads GDP, temperature, and emission difference series, feeds them into
:mod:`economic_module`, and writes per-method detail tables plus SCC time paths to
``results/economic``. Methods can be configured via ``config.yaml`` under the
``economic_module`` section or overridden through CLI options.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import math
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Iterable, Mapping, MutableMapping, Sequence, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import _path_setup as path_setup  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from calc_emissions import BASE_DEMAND_CASE  # noqa: E402
from calc_emissions.scenario_io import (  # noqa: E402
    load_scenario_absolute,
    load_scenario_delta,
    split_scenario_name,
)
from climate_module import ScenarioSpec, run_scenarios  # noqa: E402
from climate_module.calibration import load_fair_calibration  # noqa: E402
from config_paths import (  # noqa: E402
    apply_results_run_directory,
    get_config_path,
    get_results_run_directory,
    join_run_directory,
    sanitize_run_directory,
    set_config_root,
)
from economic_module import (  # noqa: E402
    EconomicInputs,
    SCCAggregation,
    SCCResult,
    compute_scc,
)
from economic_module.socioeconomics import DiceSocioeconomics  # noqa: E402

ROOT = path_setup.ROOT
AVAILABLE_DISCOUNT_METHODS = ["constant_discount", "ramsey_discount"]
AVAILABLE_AGGREGATIONS: tuple[SCCAggregation, ...] = ("average", "per_year")

RUN_DIRECTORY: str | None = None


def _safe_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in name.lower())
    return cleaned.strip("_") or "scenario"


def _write_scc_outputs(result: SCCResult, method: str, climate_key: str, output_dir: Path) -> None:
    """Export pulse SCC timeseries for a given climate pathway/method."""
    safe_climate = _safe_name(climate_key)
    safe_method = _safe_name(method)
    per_year = result.per_year.copy()
    required = [
        "year",
        "discount_factor",
        "discounted_damage_attributed_usd",
        "scc_usd_per_tco2",
    ]
    missing = [col for col in required if col not in per_year.columns]
    if missing:
        raise KeyError(f"Per-year table missing required columns: {missing}")
    frame = pd.DataFrame(
        {
            "year": per_year["year"].astype(int),
            "discount_factor": per_year["discount_factor"].astype(float),
            "pv_damage_per_pulse_usd": per_year["discounted_damage_attributed_usd"].astype(float),
            "scc_usd_per_tco2": per_year["scc_usd_per_tco2"].astype(float),
        }
    )
    if "pulse_size_tco2" in per_year.columns:
        frame["pulse_size_tco2"] = per_year["pulse_size_tco2"].astype(float)
    else:
        frame["pulse_size_tco2"] = np.nan
    if "delta_emissions_tco2" in per_year.columns:
        frame["delta_emissions_tco2"] = per_year["delta_emissions_tco2"].astype(float)
    else:
        frame["delta_emissions_tco2"] = np.nan
    if "discounted_delta_usd" in per_year.columns:
        frame["discounted_delta_usd"] = per_year["discounted_delta_usd"].astype(float)
    else:
        frame["discounted_delta_usd"] = np.nan
    pulse_path = output_dir / f"pulse_scc_timeseries_{safe_method}_{safe_climate}.csv"
    frame.to_csv(pulse_path, index=False)

    if result.pulse_details is not None and not result.pulse_details.empty:
        detail = result.pulse_details.copy()
        keep_columns = [
            "pulse_year",
            "year",
            "delta_temperature_c",
            "pulse_mass_tco2",
            "baseline_temperature_c",
            "gdp_trillion_usd",
            "delta_damage_fraction",
            "delta_damage_usd",
            "discount_factor",
            "pv_damage_usd",
        ]
        missing_cols = [col for col in keep_columns if col not in detail.columns]
        if missing_cols:
            raise KeyError(f"Pulse detail table missing required columns: {missing_cols}")
        detail = detail[keep_columns].copy()
        detail["pulse_year"] = detail["pulse_year"].astype(int)
        detail["year"] = detail["year"].astype(int)
        detail["delta_temperature_c"] = detail["delta_temperature_c"].astype(float)
        detail["pulse_mass_tco2"] = detail["pulse_mass_tco2"].astype(float)
        detail["baseline_temperature_c"] = detail["baseline_temperature_c"].astype(float)
        detail["gdp_trillion_usd"] = detail["gdp_trillion_usd"].astype(float)
        detail["delta_damage_fraction"] = detail["delta_damage_fraction"].astype(float)
        detail["delta_damage_usd"] = detail["delta_damage_usd"].astype(float)
        detail["discount_factor"] = detail["discount_factor"].astype(float)
        detail["pv_damage_usd"] = detail["pv_damage_usd"].astype(float)
        detail_path = output_dir / f"detailed_pulse_response_{safe_climate}.csv"
        detail.to_csv(detail_path, index=False)


def _build_damage_table(
    scenario_label: str,
    scc_result: SCCResult,
    emission_to_tonnes: float,
    emission_frame: pd.DataFrame | None = None,
    emission_column: str = "delta",
) -> pd.DataFrame:
    per_year = scc_result.per_year.copy()
    years = per_year["year"].astype(int)
    if emission_frame is not None:
        frame = emission_frame.copy()
        frame["year"] = frame["year"].astype(int)
        series = frame.set_index("year")[emission_column].astype(float)
        aligned = series.reindex(years).fillna(0.0)
        delta_tonnes = aligned.to_numpy(dtype=float) * float(
            emission_to_tonnes if emission_to_tonnes not in (0, None) else 1.0
        )
    else:
        delta_tonnes = per_year["delta_emissions_tco2"].astype(float)
    scc_series = per_year["scc_usd_per_tco2"].astype(float)
    discount_series = per_year.get("discount_factor")
    if discount_series is not None:
        discount = discount_series.astype(float)
    else:
        discount = pd.Series(np.nan, index=per_year.index)

    divisor = emission_to_tonnes if emission_to_tonnes not in (0, None) else 1.0
    damage_df = pd.DataFrame(
        {
            "year": years,
            "delta_emissions_mtco2": delta_tonnes / divisor,
            "delta_emissions_tco2": delta_tonnes,
            "scc_usd_per_tco2": scc_series,
            "discount_factor": discount,
        }
    )
    damage_df["damage_usd"] = damage_df["delta_emissions_tco2"] * damage_df["scc_usd_per_tco2"]
    damage_df["discounted_damage_usd"] = damage_df["damage_usd"] * damage_df[
        "discount_factor"
    ].fillna(0.0)

    details = scc_result.details
    if "consumption_per_capita_usd" in details.columns:
        consumption_lookup = dict(
            zip(details["year"], details["consumption_per_capita_usd"], strict=False)
        )
        damage_df["consumption_per_capita_usd"] = damage_df["year"].map(consumption_lookup)
    if "consumption_growth" in details.columns:
        growth_lookup = dict(zip(details["year"], details["consumption_growth"], strict=False))
        damage_df["consumption_growth"] = damage_df["year"].map(growth_lookup)
    return damage_df


def _resolve_path(path_like: str | Path, *, apply_run_directory: bool = True) -> Path:
    path = Path(path_like)
    absolute = path if path.is_absolute() else (ROOT / path)
    absolute = absolute.resolve()
    if apply_run_directory:
        absolute = apply_results_run_directory(
            absolute,
            RUN_DIRECTORY,
            repo_root=ROOT,
        )
    return absolute


def _load_emission_frame_from_mix(
    root: Path,
    scenario: str,
    column: str,
    baseline_case: str,
) -> pd.DataFrame:
    if column == "delta":
        return load_scenario_delta(root, scenario, baseline_case=baseline_case)
    if column == "absolute":
        return load_scenario_absolute(root, scenario)
    raise ValueError(
        f"Unsupported emission column '{column}'. Use 'delta' or 'absolute' for mix-based data."
    )


def _zero_emission_frame(source: Path | pd.DataFrame, emission_column: str) -> pd.DataFrame:
    if isinstance(source, (str, Path)):
        frame = pd.read_csv(source, usecols=["year"])
        years = frame["year"].astype(int)
    else:
        years = pd.Series(source["year"]).astype(int)
    result = pd.DataFrame({"year": years})
    result[emission_column] = 0.0
    return result


def _align_reference_to_climate(
    reference: str,
    evaluation: list[str],
    available: set[str],
    climate_order: list[str],
) -> tuple[str, list[str]]:
    if reference not in available:
        for candidate in climate_order:
            if candidate in available:
                reference = candidate
                break
        else:
            if not available:
                raise ValueError("No climate scenarios available to use as SCC reference.")
            reference = sorted(available)[0]
    filtered_eval = [
        scenario for scenario in evaluation if scenario in available and scenario != reference
    ]
    if not filtered_eval:
        filtered_eval = [
            scenario
            for scenario in climate_order
            if scenario in available and scenario != reference
        ]
    return reference, filtered_eval


def _max_required_year(time_cfg: Mapping[str, object], economic_cfg: Mapping[str, object]) -> int:
    start = int(time_cfg.get("start", economic_cfg.get("base_year", 2025)))
    end_candidates: list[int] = [int(time_cfg.get("end", start))]
    damage_duration = economic_cfg.get("damage_duration_years")
    if isinstance(damage_duration, (int, float)):
        end_candidates.append(start + int(damage_duration) - 1)
    agg_cfg = economic_cfg.get("aggregation_horizon", {}) or {}
    agg_end = agg_cfg.get("end")
    if agg_end is not None:
        end_candidates.append(int(agg_end))
    return max(end_candidates)


def _currency_label_from_key(key: str) -> str | None:
    match = re.search(r"to_(\d{4})", key)
    if match:
        return f"USD_{match.group(1)}"
    digits = re.findall(r"(?:19|20|21)\d{2}", key)
    if digits:
        return f"USD_{digits[-1]}"
    return None


def _get_currency_conversion(
    root_cfg: Mapping[str, object], key: str, default: float = 1.0
) -> tuple[float, str]:
    socio_cfg = root_cfg.get("socioeconomics", {}) if isinstance(root_cfg, Mapping) else {}
    conv_cfg = socio_cfg.get("currency_conversion", {}) if isinstance(socio_cfg, Mapping) else {}
    try:
        value = float(conv_cfg.get(key, default))
    except Exception:
        value = float(default)
    if math.isclose(value, 1.0, rel_tol=1e-9, abs_tol=1e-9):
        return value, "USD_native"
    label = _currency_label_from_key(key) or "USD_converted"
    return value, label


def _build_socioeconomic_projection(
    root_cfg: Mapping[str, object],
    *,
    end_year: int,
    climate_label: str | None,
    currency_conversion: float = 1.0,
    currency_label: str = "USD_native",
) -> pd.DataFrame | None:
    socio_cfg = root_cfg.get("socioeconomics", {}) or {}
    mode = str(socio_cfg.get("mode", "")).strip().lower()
    if mode != "dice":
        return None
    dice_cfg = dict(socio_cfg.get("dice", {}) or {})
    scenario_setting = str(dice_cfg.get("scenario", "SSP2")).strip()
    if scenario_setting.lower() == "as_climate_scenario":
        if not climate_label:
            raise ValueError(
                "socioeconomics.dice.scenario='as_climate_scenario' "
                "requires a climate scenario context."
            )
        match = re.match(r"ssp\s*([0-9])", climate_label, re.IGNORECASE)
        if not match:
            raise ValueError(
                "Unable to infer SSP family from climate scenario '" + str(climate_label) + "'."
            )
        resolved = f"SSP{match.group(1)}"
    else:
        resolved = scenario_setting.upper()

    dice_cfg["scenario"] = resolved
    model = DiceSocioeconomics.from_config(dice_cfg, base_path=ROOT)
    frame = model.project(end_year)
    if currency_conversion not in (1.0, 1):
        factor = float(currency_conversion)
        for column in ("gdp_trillion_usd", "consumption_trillion_usd"):
            if column in frame.columns:
                frame[column] = frame[column].astype(float) * factor
        for column in ("gdp_per_capita_usd", "consumption_per_capita_usd"):
            if column in frame.columns:
                frame[column] = frame[column].astype(float) * factor
    frame.attrs["currency_label"] = currency_label
    return frame


def _load_config() -> dict:
    path = get_config_path(ROOT / "config.yaml")
    if not path.exists():
        return {}
    with path.open() as handle:
        config = yaml.safe_load(handle) or {}
    set_config_root(config, path.parent)
    return config


def _ensure_directory(path: Path, label: str) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"{label} directory not found: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"{label} path is not a directory: {path}")
    return path


def _default_emission_root(root_cfg: dict) -> Path | None:
    countries_cfg = root_cfg.get("calc_emissions", {}).get("countries", {})
    path_str = countries_cfg.get("aggregate_output_directory") or countries_cfg.get(
        "aggregate_results_directory"
    )
    if not path_str:
        return None
    return _resolve_path(path_str)


def _default_temperature_root(root_cfg: dict) -> Path | None:
    climate_cfg = root_cfg.get("climate_module", {})
    path_str = climate_cfg.get("resource_directory") or climate_cfg.get("output_directory")
    if not path_str:
        return None
    return _resolve_path(path_str)


CLIMATE_OVERRIDE_KEYS = {
    "ocean_heat_capacity",
    "ocean_heat_transfer",
    "deep_ocean_efficacy",
    "forcing_4co2",
    "equilibrium_climate_sensitivity",
}


def _select_climate_ids(
    root_cfg: Mapping[str, object],
    requested: list[str] | None = None,
    *,
    fallback: list[str] | None = None,
) -> tuple[list[str], dict[str, Mapping[str, object]]]:
    climate_cfg = root_cfg.get("climate_module", {}) or {}
    scenarios_cfg = climate_cfg.get("climate_scenarios", {}) or {}
    definitions = scenarios_cfg.get("definitions") or []
    definition_map: dict[str, Mapping[str, object]] = {}
    for entry in definitions:
        ident = entry.get("id")
        if ident is None:
            continue
        key = str(ident).strip()
        if key:
            definition_map[key] = entry
    if not definition_map:
        candidates = requested or fallback or []
        normalized = [str(item).strip() for item in candidates if str(item).strip()]
        return normalized, {}

    if requested:
        base_list = [str(item).strip() for item in requested if str(item).strip()]
    else:
        run_cfg = scenarios_cfg.get("run")
        if isinstance(run_cfg, str):
            base_value = run_cfg.strip()
            if not base_value or base_value.lower() == "all":
                base_list = list(definition_map.keys())
            else:
                base_list = [base_value]
        elif isinstance(run_cfg, Iterable):
            base_list = [str(item).strip() for item in run_cfg if str(item).strip()]
        else:
            base_list = list(definition_map.keys())
    selected = [cid for cid in base_list if cid in definition_map]
    if not selected:
        selected = list(definition_map.keys())
    return selected, definition_map


def _generate_ssp_temperature_frames(
    root_cfg: Mapping[str, object],
    climate_ids: list[str],
    *,
    temperature_column: str,
) -> dict[str, pd.DataFrame]:
    if not climate_ids:
        return {}
    climate_cfg = root_cfg.get("climate_module", {}) or {}
    scenarios_cfg = climate_cfg.get("climate_scenarios", {}) or {}
    definitions = scenarios_cfg.get("definitions") or []
    definition_map = {
        str(entry.get("id")).strip(): entry for entry in definitions if entry.get("id")
    }

    time_horizon = root_cfg.get("time_horizon", {}) or {}
    econ_cfg = root_cfg.get("economic_module", {}) or {}
    parameters = climate_cfg.get("parameters", {}) or {}

    default_start = float(parameters.get("start_year", 1750.0))
    horizon_start = default_start
    horizon_end = float(time_horizon.get("end", parameters.get("end_year", horizon_start)))
    damage_duration = econ_cfg.get("damage_duration_years")
    if isinstance(damage_duration, (int, float)) and damage_duration > 0:
        horizon_end = max(horizon_end, horizon_start + float(damage_duration) - 1)

    base_start = float(parameters.get("start_year", default_start))
    base_end = float(parameters.get("end_year", max(horizon_end, horizon_start)))
    base_timestep = float(parameters.get("timestep", 1.0))
    base_setup = parameters.get("climate_setup", "ar6")
    base_overrides = {key: parameters[key] for key in CLIMATE_OVERRIDE_KEYS if key in parameters}

    calibration_cfg = climate_cfg.get("fair", {}).get("calibration", {}) or {}
    calibration = load_fair_calibration(calibration_cfg, repo_root=ROOT)

    warming_ref_start = float(parameters.get("warming_reference_start_year", 1850.0))
    warming_ref_end = float(parameters.get("warming_reference_end_year", 1900.0))

    def _apply_reference_and_warming(result) -> None:
        """Apply 1850-1900 reference offset and optional warming-baseline scaling."""
        import re as _re

        import numpy as _np

        # Reference offset relative to 1850–1900 (or configured window)
        mask = (result.years >= warming_ref_start) & (result.years <= warming_ref_end)
        if not _np.any(mask):
            return
        offset = float(_np.mean(result.baseline[mask]))
        result.baseline = result.baseline - offset
        result.adjusted = result.adjusted - offset

        if calibration is None or calibration.warming_baseline is None:
            return
        target_info = calibration.warming_baseline
        target_value = target_info.get("value")
        if target_value is None:
            return
        column = calibration_cfg.get("warming_baseline_column")
        if not column:
            return
        years = _re.findall(r"(?:19|20)\d{2}", str(column))
        if len(years) < 2:
            return
        start_year = int(years[0])
        end_year = int(years[-1])
        mask_target = (result.years >= start_year) & (result.years <= end_year)
        if not _np.any(mask_target):
            return
        model_target = float(_np.mean(result.baseline[mask_target]))
        if not _np.isfinite(model_target) or abs(model_target) < 1e-6:
            return
        scale = float(target_value) / model_target
        result.baseline = result.baseline * scale
        result.adjusted = result.adjusted * scale

    specs: list[ScenarioSpec] = []
    label_lookup: dict[str, str] = {}
    for climate_id in climate_ids:
        definition = definition_map.get(climate_id, {})
        label = str(definition.get("label") or climate_id).strip() or climate_id
        start_year = float(definition.get("start_year", base_start))
        end_year = float(definition.get("end_year", base_end))
        timestep = float(definition.get("timestep", base_timestep))
        climate_setup = definition.get("climate_setup", base_setup)
        overrides = dict(base_overrides)
        for key in CLIMATE_OVERRIDE_KEYS:
            if key in definition:
                overrides[key] = definition[key]
        compute_kwargs: dict[str, object] = {"climate_setup": climate_setup}
        if overrides:
            compute_kwargs["climate_overrides"] = overrides
        if calibration is not None:
            compute_kwargs["fair_calibration"] = calibration
        specs.append(
            ScenarioSpec(
                label=label,
                scenario=climate_id,
                start_year=start_year,
                end_year=end_year,
                timestep=timestep,
                compute_kwargs=compute_kwargs,
            )
        )
        label_lookup[label] = climate_id

    if not specs:
        return {}
    results = run_scenarios(specs)
    frames: dict[str, pd.DataFrame] = {}
    for spec in specs:
        result = results[spec.label]
        _apply_reference_and_warming(result)
        frame = pd.DataFrame(
            {
                "year": result.years.astype(int),
                temperature_column: result.adjusted.astype(float),
                "climate_scenario": spec.scenario,
            }
        )
        frames[spec.scenario] = frame
    return frames


def _fallback_summary_dir(root_cfg: Mapping[str, object]) -> Path:
    results_cfg = root_cfg.get("results", {}) if isinstance(root_cfg, Mapping) else {}
    summary_cfg = results_cfg.get("summary", {}) if isinstance(results_cfg, Mapping) else {}
    summary_path_cfg = summary_cfg.get("output_directory", "results/summary")
    summary_path = Path(summary_path_cfg)
    if not summary_path.is_absolute():
        summary_path = ROOT / summary_path
    run_dir = get_results_run_directory(root_cfg)
    summary_path = apply_results_run_directory(summary_path, run_dir, repo_root=ROOT)
    summary_path.mkdir(parents=True, exist_ok=True)
    return summary_path


def _write_scc_fallback_plots(
    root_cfg: Mapping[str, object],
    scc_cache: Mapping[tuple[str | None, str], SCCResult],
    socio_cache: Mapping[str, pd.DataFrame | None],
    *,
    socio_output_dir: Path | None = None,
) -> None:
    if not scc_cache:
        return
    base_year = int((root_cfg.get("economic_module", {}) or {}).get("base_year", 2025))
    summary_dir = _fallback_summary_dir(root_cfg)
    plot_dir = summary_dir / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)

    import matplotlib

    matplotlib.use("Agg")  # type: ignore[attr-defined]
    import matplotlib.pyplot as plt

    # Group SCC results by discount method so we can plot SSP pathways together.
    grouped: dict[str, list[tuple[str, SCCResult]]] = {}
    for (climate_label, method), result in scc_cache.items():
        label = climate_label or result.scenario or "default"
        grouped.setdefault(method, []).append((label, result))

    for method, entries in grouped.items():
        if not entries:
            continue
        fig, ax = plt.subplots()
        combined: pd.DataFrame | None = None
        for label, result in sorted(entries, key=lambda item: item[0]):
            per_year = result.per_year
            years = per_year["year"].astype(int)
            values = per_year["scc_usd_per_tco2"].astype(float)
            ax.plot(years, values, label=label)
            column_name = f"scc_usd_per_tco2_{_safe_name(label)}"
            series = per_year[["year", "scc_usd_per_tco2"]].rename(
                columns={"scc_usd_per_tco2": column_name}
            )
            combined = (
                series if combined is None else combined.merge(series, on="year", how="outer")
            )
        ax.set_title(f"SCC ({method}) – SSP comparison")
        ax.set_xlabel("Year")
        ax.set_ylabel("SCC (USD/tCO₂, base year)")
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend()
        fig.tight_layout()
        fig.savefig(plot_dir / f"scc_timeseries_{method}.png", dpi=200)
        plt.close(fig)
        if combined is not None:
            combined = combined.sort_values("year").reset_index(drop=True)
            combined.to_csv(summary_dir / f"scc_timeseries_{method}.csv", index=False)

    socio_entries = [
        (label or "default", frame)
        for label, frame in socio_cache.items()
        if frame is not None and not frame.empty
    ]
    if socio_entries:
        socio_entries.sort(key=lambda item: item[0])
        target_dir = socio_output_dir or summary_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for label, frame in socio_entries:
            safe_label = _safe_name(label)
            currency_label = frame.attrs.get("currency_label", f"USD_{base_year}")
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
            full_path = target_dir / f"socioeconomics_{safe_label}.csv"
            with contextlib.suppress(Exception):
                socio_frame.to_csv(full_path, index=False)

    gdp_fig = gdp_ax = None
    pop_fig = pop_ax = None

    for label, frame in socio_entries:
        if "gdp_trillion_usd" in frame.columns:
            if gdp_ax is None:
                gdp_fig, gdp_ax = plt.subplots()
            years = frame["year"].astype(int)
            values = frame["gdp_trillion_usd"].astype(float)
            gdp_ax.plot(years, values, label=label)
        if "population_million" in frame.columns:
            if pop_ax is None:
                pop_fig, pop_ax = plt.subplots()
            years = frame["year"].astype(int)
            values = frame["population_million"].astype(float)
            pop_ax.plot(years, values, label=label)

    if gdp_ax is not None and gdp_fig is not None:
        gdp_ax.set_title("Socioeconomics – GDP trajectories")
        gdp_ax.set_xlabel("Year")
        gdp_ax.set_ylabel("GDP (trillion USD)")
        gdp_ax.grid(True, linestyle="--", alpha=0.4)
        gdp_ax.legend()
        gdp_fig.tight_layout()
        gdp_fig.savefig(plot_dir / "socioeconomics_gdp.png", dpi=200)
        plt.close(gdp_fig)
        old_gdp = summary_dir / "socioeconomics_gdp.csv"
        old_gdp.unlink(missing_ok=True)

    if pop_ax is not None and pop_fig is not None:
        pop_ax.set_title("Socioeconomics – Population trajectories")
        pop_ax.set_xlabel("Year")
        pop_ax.set_ylabel("Population (million)")
        pop_ax.grid(True, linestyle="--", alpha=0.4)
        pop_ax.legend()
        pop_fig.tight_layout()
        pop_fig.savefig(plot_dir / "socioeconomics_population.png", dpi=200)
        plt.close(pop_fig)
        old_pop = summary_dir / "socioeconomics_population.csv"
        old_pop.unlink(missing_ok=True)

    print(f"[SCC] Fallback SCC plots written to {plot_dir.relative_to(ROOT)}")


def _run_summary_outputs(
    root_cfg: Mapping[str, object],
    scc_cache: Mapping[tuple[str | None, str], SCCResult],
    socio_cache: Mapping[str, pd.DataFrame | None],
    has_emissions: bool,
    *,
    socio_output_dir: Path | None = None,
) -> None:
    if not scc_cache:
        return
    if has_emissions:
        print("[SCC] Generating summary outputs...")
        argv_backup = sys.argv.copy()
        try:
            sys.argv = ["generate_summary.py"]
            from scripts import generate_summary

            generate_summary.main()
            return
        except Exception as exc:  # pragma: no cover - best-effort summary
            print(f"[SCC] Summary generation skipped: {exc}")
        finally:
            sys.argv = argv_backup
    else:
        print("[SCC] Emission data missing; writing SCC fallback plots only.")
    _write_scc_fallback_plots(root_cfg, scc_cache, socio_cache, socio_output_dir=socio_output_dir)


def _parse_climate_labels(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        labels = [item.strip() for item in value.split(",") if item.strip()]
        return labels
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


def _climate_labels_from_module(root_cfg: Mapping[str, object]) -> list[str]:
    climate_cfg = root_cfg.get("climate_module", {}).get("climate_scenarios", {}) or {}
    definitions = climate_cfg.get("definitions") or []
    label_map: dict[str, str] = {}
    for entry in definitions:
        if not isinstance(entry, Mapping):
            continue
        label = str(entry.get("label") or entry.get("id") or "").strip()
        if label:
            label_map[label] = label

    def _normalize(value: object) -> str | None:
        if value is None:
            return None
        label = str(value).strip()
        if not label:
            return None
        if label_map:
            return label_map.get(label) or (label if label in label_map else None)
        return label

    run_cfg = climate_cfg.get("run")
    resolved: list[str] = []
    if isinstance(run_cfg, str):
        if run_cfg.strip().lower() == "all":
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


def _restrict_inputs_window(
    inputs: EconomicInputs, start_year: int, end_year: int
) -> EconomicInputs:
    years = inputs.years.astype(int)
    current_start = int(years.min())
    current_end = int(years.max())

    if start_year < current_start:
        raise ValueError(
            f"SCC inputs start at {current_start} but requested window begins at {start_year}."
        )

    gdp = inputs.gdp_trillion_usd
    population = inputs.population_million
    consumption = inputs.consumption_trillion_usd
    temp_series = dict(inputs.temperature_scenarios_c)
    emission_series = dict(inputs.emission_scenarios_tco2)

    if end_year > current_end:
        extra_years = np.arange(current_end + 1, end_year + 1, dtype=int)
        if extra_years.size:
            years = np.concatenate([years, extra_years])
            gdp_extension = np.full(extra_years.shape, gdp[-1], dtype=float)
            gdp = np.concatenate([gdp, gdp_extension])

            if population is not None:
                pop_extension = np.full(extra_years.shape, population[-1], dtype=float)
                population = np.concatenate([population, pop_extension])
            if consumption is not None:
                cons_extension = np.full(extra_years.shape, consumption[-1], dtype=float)
                consumption = np.concatenate([consumption, cons_extension])

            for name, series in temp_series.items():
                extension = np.full(extra_years.shape, series[-1], dtype=float)
                temp_series[name] = np.concatenate([series, extension])

            for name, series in emission_series.items():
                extension = np.full(extra_years.shape, series[-1], dtype=float)
                emission_series[name] = np.concatenate([series, extension])

    mask = (years >= start_year) & (years <= end_year)
    if not mask.any():
        raise ValueError(
            f"No data available between {start_year} and {end_year} for SCC calculation."
        )

    selected_years = years[mask]
    gdp_selected = gdp[mask]
    population_selected = population[mask] if population is not None else None
    consumption_selected = consumption[mask] if consumption is not None else None
    temperatures = {label: series[mask] for label, series in temp_series.items()}
    emissions = {label: series[mask] for label, series in emission_series.items()}

    climate_scenarios = None
    if inputs.climate_scenarios:
        climate_scenarios = dict(inputs.climate_scenarios)

    return EconomicInputs(
        years=selected_years,
        gdp_trillion_usd=gdp_selected,
        temperature_scenarios_c=temperatures,
        emission_scenarios_tco2=emissions,
        population_million=population_selected,
        consumption_trillion_usd=consumption_selected,
        climate_scenarios=climate_scenarios,
        ssp_family=inputs.ssp_family,
    )


def _default_discount_methods(cfg: dict) -> list[str]:
    methods_cfg = cfg.get("methods", {})
    run = methods_cfg.get("run")
    if isinstance(run, str):
        if run.lower() == "all":
            return AVAILABLE_DISCOUNT_METHODS
        return [run]
    if isinstance(run, Iterable):
        selected = [str(item) for item in run]
        valid = [m for m in selected if m in AVAILABLE_DISCOUNT_METHODS]
        return valid or AVAILABLE_DISCOUNT_METHODS
    return AVAILABLE_DISCOUNT_METHODS


def _build_parser(cfg: dict, root_cfg: dict) -> argparse.ArgumentParser:
    gdp_default = cfg.get("gdp_series")
    gdp_pop_dir_default = cfg.get("gdp_population_directory", "data/GDP_and_Population_data")
    base_year_default = int(cfg.get("base_year", 2025))
    aggregation_default = str(cfg.get("aggregation", "average")).lower()
    if aggregation_default not in AVAILABLE_AGGREGATIONS:
        aggregation_default = "average"
    damage_cfg = cfg.get("damage_function", {})
    damage_mode_default = str(damage_cfg.get("mode", "dice")).strip().lower()
    if damage_mode_default not in {"dice", "custom"}:
        damage_mode_default = "dice"
    damage_delta1_default = float(damage_cfg.get("delta1", 0.0))
    damage_delta2_default = float(damage_cfg.get("delta2", 0.002))
    damage_use_threshold_default = bool(damage_cfg.get("use_threshold", False))
    damage_threshold_temp_default = float(damage_cfg.get("threshold_temperature", 3.0))
    damage_threshold_scale_default = float(damage_cfg.get("threshold_scale", 0.2))
    damage_threshold_power_default = float(damage_cfg.get("threshold_power", 2.0))
    damage_use_saturation_default = bool(damage_cfg.get("use_saturation", False))
    damage_max_fraction_default = float(damage_cfg.get("max_fraction", 0.99))
    damage_saturation_mode_default = str(damage_cfg.get("saturation_mode", "rational")).lower()
    damage_use_catastrophic_default = bool(damage_cfg.get("use_catastrophic", False))
    damage_catastrophic_temp_default = float(damage_cfg.get("catastrophic_temperature", 5.0))
    damage_disaster_fraction_default = float(damage_cfg.get("disaster_fraction", 0.75))
    damage_disaster_gamma_default = float(damage_cfg.get("disaster_gamma", 1.0))
    damage_disaster_mode_default = str(damage_cfg.get("disaster_mode", "prob")).lower()
    aggregation_horizon_cfg = cfg.get("aggregation_horizon", {}) or {}
    aggregation_start_default = aggregation_horizon_cfg.get("start")
    aggregation_end_default = aggregation_horizon_cfg.get("end")

    data_sources_cfg = cfg.get("data_sources", {}) or {}
    write_damages_default = bool(cfg.get("write_damages", True))
    damage_custom_terms_default = damage_cfg.get("custom_terms")
    emission_root_default = data_sources_cfg.get("emission_root")
    temperature_root_default = data_sources_cfg.get("temperature_root")

    climate_defaults_cfg = data_sources_cfg.get("climate_scenarios")
    if climate_defaults_cfg:
        climate_label_default = ",".join(
            str(item).strip() for item in climate_defaults_cfg if str(item).strip()
        )
    else:
        climate_single_default = data_sources_cfg.get("climate_scenario")
        if climate_single_default is not None:
            climate_label_default = str(climate_single_default).strip()
        else:
            climate_label_default = ""

    parser = argparse.ArgumentParser(
        description=(
            "Calculate SCC for temperature/emission scenarios relative to a reference pathway."
        )
    )
    parser.add_argument(
        "--temperature",
        action="append",
        help=(
            "Temperature series specification as 'label=path/to.csv'. Provide at least two entries."
        ),
    )
    parser.add_argument(
        "--emission",
        action="append",
        help=(
            "Emission delta series specification as 'label=path/to.csv'. Provide "
            "matching labels to temperature."
        ),
    )
    parser.add_argument(
        "--reference-scenario",
        default=cfg.get("reference_scenario"),
        help=(
            "Scenario label treated as the reference/baseline "
            "(default from config or first listed)."
        ),
    )
    parser.add_argument(
        "--scenario",
        action="append",
        help=(
            "Scenario label to evaluate (repeat flag for multiple). Defaults to "
            "config evaluation list or all non-reference scenarios."
        ),
    )
    parser.add_argument(
        "--gdp-csv",
        default=gdp_default,
        help=(
            "Optional override CSV with 'year', 'gdp_trillion_usd', and "
            "optionally 'population_million'."
        ),
    )
    parser.add_argument(
        "--gdp-population-directory",
        default=gdp_pop_dir_default,
        help=(
            "Directory with SSP GDP/Population data. Defaults to the IIASA Scenario "
            "Explorer extracts (`IIASA/GDP.csv`, `IIASA/Population.csv`) under "
            "data/GDP_and_Population_data."
        ),
    )
    parser.add_argument(
        "--base-year",
        type=int,
        default=base_year_default,
        help="Year used as present value reference.",
    )
    parser.add_argument(
        "--aggregation",
        choices=AVAILABLE_AGGREGATIONS,
        default=aggregation_default,
        help="Return per-year SCC values or the aggregated average (default from config).",
    )
    parser.add_argument(
        "--discount-methods",
        default=None,
        help=(
            "Comma-separated discounting methods to run "
            "(constant_discount, ramsey_discount, or 'all'; default from config)."
        ),
    )
    parser.add_argument(
        "--emission-root",
        help=(
            "Root directory containing <scenario>/co2.csv emission deltas "
            "(defaults to economic_module.data_sources.emission_root)."
        ),
        default=emission_root_default,
    )
    parser.add_argument(
        "--temperature-root",
        help=(
            "Directory containing climate CSVs named <scenario>_<climate>.csv "
            "(defaults to economic_module.data_sources.temperature_root)."
        ),
        default=temperature_root_default,
    )
    parser.add_argument(
        "--climate-scenario",
        help=(
            "Climate scenario label(s) to pair with emission deltas (comma-separated). "
            "Defaults to economic_module.data_sources.climate_scenarios or the first "
            "climate definition."
        ),
        default=climate_label_default,
    )
    parser.add_argument(
        "--aggregation-start",
        type=int,
        default=aggregation_start_default,
        help=(
            "First year included when aggregation=='average'. "
            "Overrides economic_module.aggregation_horizon.start."
        ),
    )
    parser.add_argument(
        "--aggregation-end",
        type=int,
        default=aggregation_end_default,
        help=(
            "Final year (inclusive) when aggregation=='average'. "
            "Overrides economic_module.aggregation_horizon.end."
        ),
    )
    parser.add_argument(
        "--discount-rate",
        type=float,
        default=float(
            cfg.get("methods", {}).get("constant_discount", {}).get("discount_rate", 0.03)
        ),
        help="Constant annual discount rate (decimal).",
    )
    parser.add_argument(
        "--rho",
        type=float,
        default=float(cfg.get("methods", {}).get("ramsey_discount", {}).get("rho", 0.005)),
        help="Ramsey: pure rate of time preference (decimal).",
    )
    parser.add_argument(
        "--eta",
        type=float,
        default=float(cfg.get("methods", {}).get("ramsey_discount", {}).get("eta", 1.0)),
        help="Ramsey: elasticity of marginal utility of consumption.",
    )
    parser.add_argument(
        "--add-tco2",
        type=float,
        default=cfg.get("add_tco2"),
        help=(
            "Optional override for total emission delta (t CO₂). If omitted, "
            "derived from emission series."
        ),
    )
    parser.add_argument(
        "--temperature-column",
        default=cfg.get("temperature_column", "temperature_adjusted"),
        help="Column name for temperatures inside the CSV files (default 'temperature_adjusted').",
    )
    parser.add_argument(
        "--emission-column",
        default=cfg.get("emission_column", "delta"),
        help="Column name for emission deltas inside the CSV files (default 'delta').",
    )
    parser.add_argument(
        "--emission-unit-multiplier",
        type=float,
        default=float(cfg.get("emission_to_tonnes", 1e6)),
        help="Factor converting emission column units to t CO₂ (default converts Mt to tonnes).",
    )
    parser.add_argument(
        "--damage-delta1",
        type=float,
        default=damage_delta1_default,
        help="Linear term (delta1) for the DICE quadratic damage function.",
    )
    parser.add_argument(
        "--damage-delta2",
        type=float,
        default=damage_delta2_default,
        help="Quadratic term (delta2) for the DICE damage function.",
    )
    parser.add_argument(
        "--damage-threshold-temperature",
        type=float,
        default=damage_threshold_temp_default,
        help="Temperature (°C) above which threshold amplification begins.",
    )
    parser.add_argument(
        "--damage-threshold-scale",
        type=float,
        default=damage_threshold_scale_default,
        help="Scale factor applied when threshold amplification is enabled.",
    )
    parser.add_argument(
        "--damage-threshold-power",
        type=float,
        default=damage_threshold_power_default,
        help="Power used in the threshold amplification curve.",
    )
    parser.add_argument(
        "--damage-max-fraction",
        type=float,
        default=damage_max_fraction_default,
        help="Upper bound on GDP loss fraction (used for saturation and final cap).",
    )
    parser.add_argument(
        "--damage-saturation-mode",
        choices=("rational", "clamp"),
        default=damage_saturation_mode_default,
        help="Saturation style when enabled: 'rational' curve or hard clamp.",
    )
    parser.add_argument(
        "--damage-disaster-mode",
        choices=("prob", "step"),
        default=damage_disaster_mode_default,
        help="Catastrophic add-on style: probabilistic ('prob') or step change.",
    )
    parser.set_defaults(
        damage_use_threshold=damage_use_threshold_default,
        damage_use_saturation=damage_use_saturation_default,
        damage_use_catastrophic=damage_use_catastrophic_default,
        write_damages=write_damages_default,
        damage_custom_terms=damage_custom_terms_default,
    )
    parser.add_argument(
        "--damage-mode",
        choices=("dice", "custom"),
        default=damage_mode_default,
        help="Select 'dice' for the quadratic baseline or 'custom' for coefficient/exponent lists.",
    )
    parser.add_argument(
        "--damage-use-threshold",
        dest="damage_use_threshold",
        action="store_true",
        help="Enable temperature threshold amplification in the damage function.",
    )
    parser.add_argument(
        "--damage-no-threshold",
        dest="damage_use_threshold",
        action="store_false",
        help="Disable temperature threshold amplification.",
    )
    parser.add_argument(
        "--damage-use-saturation",
        dest="damage_use_saturation",
        action="store_true",
        help="Enable saturation of damages to stay below the configured max fraction.",
    )
    parser.add_argument(
        "--damage-no-saturation",
        dest="damage_use_saturation",
        action="store_false",
        help="Disable saturation; damages still capped at the max fraction.",
    )
    parser.add_argument(
        "--damage-use-catastrophic",
        dest="damage_use_catastrophic",
        action="store_true",
        help="Enable catastrophic damages above the catastrophic temperature.",
    )
    parser.add_argument(
        "--damage-no-catastrophic",
        dest="damage_use_catastrophic",
        action="store_false",
        help="Disable catastrophic damage add-ons.",
    )
    parser.add_argument(
        "--damage-catastrophic-temperature",
        type=float,
        default=damage_catastrophic_temp_default,
        help="Temperature (°C) at which catastrophic damages begin.",
    )
    parser.add_argument(
        "--damage-disaster-fraction",
        type=float,
        default=damage_disaster_fraction_default,
        help="Fractional GDP loss added during catastrophic events.",
    )
    parser.add_argument(
        "--damage-disaster-gamma",
        type=float,
        default=damage_disaster_gamma_default,
        help="Controls the steepness of the probabilistic catastrophic response.",
    )
    parser.add_argument(
        "--skip-damages",
        dest="write_damages",
        action="store_false",
        help="Skip emission-scenario damage CSV exports (SCC only).",
    )
    parser.add_argument(
        "--write-damages",
        dest="write_damages",
        action="store_true",
        help="Enable emission-scenario damage CSV exports.",
    )
    parser.add_argument(
        "--output-directory",
        default=cfg.get("output_directory", "results/economic"),
        help="Directory for per-method detail tables.",
    )
    parser.add_argument(
        "--list-methods",
        action="store_true",
        help="Display available SCC calculation methods and exit.",
    )
    return parser


def _collect_sources(
    arg_entries: list[str] | None,
    cfg_map: Mapping[str, str] | None,
    *,
    minimum: int,
    label: str,
) -> dict[str, Path]:
    entries: dict[str, str] = {}
    if isinstance(cfg_map, Mapping):
        for key, value in cfg_map.items():
            entries[str(key)] = str(value)

    for spec in arg_entries or []:
        if "=" not in spec:
            raise ValueError(f"{label} arguments must use the form name=path.")
        name, path = spec.split("=", 1)
        entries[name.strip()] = path.strip()

    if len(entries) < minimum:
        raise ValueError(f"Provide at least {minimum} {label} series via config or CLI.")

    return {name: _resolve_path(path) for name, path in entries.items()}


def _select_reference(args: argparse.Namespace, cfg: dict, available: Iterable[str]) -> str:
    available = list(available)
    if not available:
        raise ValueError("No temperature scenarios available.")
    candidate = args.reference_scenario or cfg.get("reference_scenario")
    if candidate and candidate in available:
        return candidate
    return available[0]


def _select_targets(
    args: argparse.Namespace, cfg: dict, available: Iterable[str], reference: str
) -> list[str]:
    available_set = set(available)
    available_set.discard(reference)
    if not available_set:
        raise ValueError("Reference scenario is the only series provided.")

    if args.scenario:
        targets = [label for label in args.scenario if label in available_set]
        if targets:
            return targets
    cfg_targets = cfg.get("evaluation_scenarios")
    if isinstance(cfg_targets, Iterable):
        targets = [str(label) for label in cfg_targets if str(label) in available_set]
        if targets:
            return targets
    return sorted(available_set)


def _run_single_configuration(
    root_cfg: Mapping[str, object],
    argv: Sequence[str] | None = None,
) -> None:
    global RUN_DIRECTORY
    RUN_DIRECTORY = get_results_run_directory(root_cfg)
    config = root_cfg.get("economic_module", {})
    run_cfg = config.get("run", {}) or {}
    pulse_cfg = run_cfg.get("pulse", {}) or {}
    # Scenario-based SCC calculations have been removed; always use SSP-based pulses.
    ssp_only_mode = True
    countries_cfg = root_cfg.get("calc_emissions", {}).get("countries", {}) or {}
    baseline_case = (
        str(countries_cfg.get("baseline_demand_case", BASE_DEMAND_CASE)).strip() or BASE_DEMAND_CASE
    )
    climate_cfg_root = root_cfg.get("climate_module", {}) or {}
    calibration_cfg = climate_cfg_root.get("fair", {}).get("calibration", {}) or {}
    fair_calibration = load_fair_calibration(calibration_cfg, repo_root=ROOT)
    climate_parameters = climate_cfg_root.get("parameters", {}) or {}

    parser = _build_parser(config, root_cfg)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.list_methods:
        msg_lines = [
            "This tool always uses the FaIR pulse workflow.",
            "",
            "Available discounting methods:",
            "  constant_discount - fixed annual discount rate (e.g., 3%).",
            "  ramsey_discount   - Ramsey rule with rho (time preference) and eta (risk aversion).",
        ]
        print("\n".join(msg_lines))
        return

    discount_methods = _default_discount_methods(config)
    if args.discount_methods:
        spec = args.discount_methods.strip()
        if spec.lower() == "all":
            discount_methods = AVAILABLE_DISCOUNT_METHODS
        else:
            requested = [item.strip() for item in spec.split(",") if item.strip()]
            valid = [m for m in requested if m in AVAILABLE_DISCOUNT_METHODS]
            if valid:
                discount_methods = valid
    if not discount_methods:
        parser.error("No valid discounting methods selected.")

    time_horizon_cfg = root_cfg.get("time_horizon", {})
    horizon_start = int(time_horizon_cfg.get("start", args.base_year))
    if args.base_year != horizon_start:
        parser.error(
            "economic_module.base_year must match time_horizon.start "
            f"(currently {args.base_year} vs {horizon_start})."
        )

    required_year = _max_required_year(time_horizon_cfg, config)
    gdp_path = _resolve_path(args.gdp_csv) if args.gdp_csv else None
    socio_cfg = root_cfg.get("socioeconomics", {}) or {}
    socio_mode = str(socio_cfg.get("mode", "")).strip().lower()
    use_socioeconomics = gdp_path is None and socio_mode == "dice"
    gdp_population_directory = None
    if gdp_path is None and not use_socioeconomics and args.gdp_population_directory:
        gdp_population_directory = _resolve_path(args.gdp_population_directory)
    socio_cache: dict[str, pd.DataFrame | None] = {}
    currency_conv_2017, currency_label_2017 = _get_currency_conversion(
        root_cfg, "usd_2017_to_2025", 1.0
    )
    currency_conv_2023, currency_label_2023 = _get_currency_conversion(
        root_cfg, "usd_2023_to_2025", 1.0
    )

    try:
        manual_temperature = _collect_sources(
            args.temperature, config.get("temperature_series"), minimum=0, label="temperature"
        )
        manual_emission = _collect_sources(
            args.emission, config.get("emission_series"), minimum=0, label="emission"
        )
    except ValueError as exc:
        parser.error(str(exc))

    if manual_temperature or manual_emission:
        parser.error(
            "Manual --temperature/--emission overrides are no longer supported; "
            "configure the economic_module.data_sources section instead."
        )
    else:
        data_sources_cfg = config.get("data_sources", {}) or {}
        emission_root_input = args.emission_root or data_sources_cfg.get("emission_root")
        emission_root_path: Path | None = None
        if emission_root_input:
            emission_candidate = _resolve_path(emission_root_input)
            if emission_candidate.exists():
                emission_root_path = emission_candidate
        if emission_root_path is None:
            default_emission_root = _default_emission_root(root_cfg)
            if default_emission_root and default_emission_root.exists():
                emission_root_path = default_emission_root
        if emission_root_path is not None:
            emission_root_path = _ensure_directory(emission_root_path, "emission delta root")
        else:
            print("[SCC] No emission delta outputs detected; proceeding without emission damages.")

        reference_default = str(
            args.reference_scenario or config.get("reference_scenario") or f"{BASE_DEMAND_CASE}"
        )
        evaluation_cfg = config.get("evaluation_scenarios") or []
        if isinstance(evaluation_cfg, str):
            evaluation_cfg = [evaluation_cfg]
        evaluation_cfg = [entry for entry in evaluation_cfg if str(entry).strip()]
        scenario_candidates = [
            reference_default,
            *[s for s in evaluation_cfg if s != reference_default],
        ]

        fallback_climate_cfg = _parse_climate_labels(data_sources_cfg.get("climate_scenarios"))
        if not fallback_climate_cfg:
            single_climate = data_sources_cfg.get("climate_scenario")
            if single_climate:
                fallback_climate_cfg = [str(single_climate).strip()]
        requested_climates = _parse_climate_labels(args.climate_scenario)
        climate_inputs, climate_definition_map = _select_climate_ids(
            root_cfg, requested_climates, fallback=fallback_climate_cfg
        )
        if not climate_inputs:
            parser.error("Unable to determine climate scenario labels.")

        temperature_root_input = args.temperature_root or data_sources_cfg.get("temperature_root")
        temperature_root_path: Path | None = None
        if temperature_root_input:
            temperature_candidate = _resolve_path(temperature_root_input)
            if temperature_candidate.exists():
                temperature_root_path = temperature_candidate
        if temperature_root_path is None:
            default_temperature_root = _default_temperature_root(root_cfg)
            if default_temperature_root and default_temperature_root.exists():
                temperature_root_path = default_temperature_root
        if temperature_root_path is not None:
            temperature_root_path = _ensure_directory(temperature_root_path, "temperature root")

        scenario_temperature: dict[tuple[str, str], Path | pd.DataFrame] = {}
        scenario_climate: dict[str, str] = {}
        emission_cache: dict[str, pd.DataFrame] = {}
        available_scenarios: set[str] = set()
        damage_entries: list[dict[str, str]] = []
        climate_only_mode = False
        climate_jobs: list[dict[str, str]] = []

        def _add_temperature_entry(scenario_name: str) -> bool:
            if temperature_root_path is None:
                return False
            added = False
            for climate_label in climate_inputs:
                temp_file = temperature_root_path / f"{scenario_name}_{climate_label}.csv"
                if not temp_file.exists():
                    continue
                scenario_temperature[(scenario_name, climate_label)] = temp_file
                scenario_climate.setdefault(scenario_name, climate_label)
                available_scenarios.add(scenario_name)
                added = True
            return added

        if emission_root_path is not None:
            for mix_dir in sorted(p for p in emission_root_path.iterdir() if p.is_dir()):
                co2_path = mix_dir / "co2.csv"
                if not co2_path.exists():
                    continue
                try:
                    df = pd.read_csv(co2_path, comment="#")
                except Exception:
                    continue
                demand_cases: list[str] = []
                for col in df.columns:
                    if col.startswith("delta_") or col.startswith("absolute_"):
                        demand_cases.append(col.split("_", 1)[1])
                demand_cases = sorted(set(demand_cases))
                if not demand_cases:
                    continue
                for case in demand_cases:
                    scenario_name = f"{mix_dir.name}__{case}"
                    if scenario_name not in emission_cache:
                        try:
                            emission_cache[scenario_name] = _load_emission_frame_from_mix(
                                emission_root_path,
                                scenario_name,
                                args.emission_column,
                                baseline_case,
                            )
                        except (FileNotFoundError, KeyError, ValueError):
                            continue
                    if not _add_temperature_entry(scenario_name):
                        continue
                    _, demand_case = split_scenario_name(scenario_name)
                    if demand_case != baseline_case:
                        for climate_label in climate_inputs:
                            if (scenario_name, climate_label) in scenario_temperature:
                                damage_entries.append(
                                    {
                                        "scenario": scenario_name,
                                        "mix": mix_dir.name,
                                        "climate": climate_label,
                                    }
                                )

        if emission_root_path is None and not climate_only_mode:
            for scenario_name in scenario_candidates:
                if not _add_temperature_entry(scenario_name):
                    continue
                if scenario_name not in emission_cache:
                    example_entry = None
                    for label in climate_inputs:
                        tuple_key = (scenario_name, label)
                        if tuple_key in scenario_temperature:
                            example_entry = scenario_temperature[tuple_key]
                            break
                    if example_entry is None:
                        continue
                    emission_cache[scenario_name] = _zero_emission_frame(
                        example_entry, args.emission_column
                    )
        if (
            ssp_only_mode
            or not scenario_temperature
            or reference_default not in available_scenarios
        ):
            climate_only_mode = True
            frames = _generate_ssp_temperature_frames(
                root_cfg,
                climate_inputs,
                temperature_column=args.temperature_column,
            )
            if not frames:
                parser.error(
                    "Unable to construct SSP temperature scenarios based onclimate_module settings."
                )
            scenario_temperature.clear()
            scenario_climate.clear()
            available_scenarios.clear()
            for climate_label, frame in frames.items():
                label = str(frame["climate_scenario"].iloc[0])
                climate_label = label or climate_label
                ref_name = f"{climate_label}__baseline"
                driver_name = f"{climate_label}__pulse"
                for scenario_name in (ref_name, driver_name):
                    scenario_temperature[(scenario_name, climate_label)] = frame
                    scenario_climate[scenario_name] = climate_label
                    emission_cache[scenario_name] = _zero_emission_frame(
                        frame, args.emission_column
                    )
                    available_scenarios.add(scenario_name)
                climate_jobs.append(
                    {
                        "climate": climate_label,
                        "reference": ref_name,
                        "driver": driver_name,
                    }
                )
            if climate_jobs:
                reference_default = climate_jobs[0]["reference"]
                evaluation_cfg = [job["driver"] for job in climate_jobs]
                climate_inputs = [job["climate"] for job in climate_jobs]
            else:
                reference_default, evaluation_cfg = _align_reference_to_climate(
                    reference_default,
                    evaluation_cfg,
                    available_scenarios,
                    climate_inputs,
                )

        scenario_candidates = [
            reference_default,
            *[s for s in evaluation_cfg if s != reference_default],
        ]

        if reference_default not in available_scenarios:
            location = (
                temperature_root_path if temperature_root_path is not None else "generated SSP runs"
            )
            parser.error(
                f"Reference scenario '{reference_default}' missing temperature data ({location})."
            )
        if not scenario_temperature:
            parser.error("No temperature scenario files available for SCC calculation.")
    output_dir = _resolve_path(args.output_directory)
    output_dir.mkdir(parents=True, exist_ok=True)

    aggregation = cast(SCCAggregation, args.aggregation)
    agg_start = args.aggregation_start
    agg_end = args.aggregation_end

    run_cfg = config.get("run", {}) or {}
    pulse_cfg = pulse_cfg or run_cfg.get("pulse", {}) or {}
    damage_kwargs = {
        "delta1": args.damage_delta1,
        "delta2": args.damage_delta2,
        "custom_terms": None,
        "use_threshold": args.damage_use_threshold,
        "threshold_temperature": args.damage_threshold_temperature,
        "threshold_scale": args.damage_threshold_scale,
        "threshold_power": args.damage_threshold_power,
        "use_saturation": args.damage_use_saturation,
        "max_fraction": args.damage_max_fraction,
        "saturation_mode": args.damage_saturation_mode,
        "use_catastrophic": args.damage_use_catastrophic,
        "catastrophic_temperature": args.damage_catastrophic_temperature,
        "disaster_fraction": args.damage_disaster_fraction,
        "disaster_gamma": args.damage_disaster_gamma,
        "disaster_mode": args.damage_disaster_mode,
    }
    pulse_size_default = 1.0
    if pulse_cfg:
        pulse_size_default = float(pulse_cfg.get("pulse_size_tco2", 1.0e6))
        damage_kwargs["_pulse_size_tco2__"] = pulse_size_default
    if args.damage_mode == "custom":
        custom_terms = args.damage_custom_terms
        if not custom_terms:
            parser.error(
                "damage_function.mode='custom' requires damage_function.custom_terms "
                "with coefficient/exponent entries."
            )
        if not isinstance(custom_terms, Sequence):
            parser.error("damage_function.custom_terms must be a list of mappings.")
        damage_kwargs["custom_terms"] = custom_terms
    else:
        damage_kwargs.pop("custom_terms", None)
    scc_cache: dict[tuple[str, str], SCCResult] = {}

    add_tco2_override = args.add_tco2
    if add_tco2_override is None and emission_root_path is None:
        add_tco2_override = pulse_size_default

    if reference_default not in available_scenarios:
        parser.error(
            f"Reference scenario '{reference_default}' not found in available temperature files."
        )

    driver_candidates = [
        scenario
        for scenario in evaluation_cfg
        if scenario in available_scenarios and scenario != reference_default
    ]
    driver_scenario = driver_candidates[0] if driver_candidates else None
    if driver_scenario is None:
        fallback = [s for s in sorted(available_scenarios) if s != reference_default]
        driver_scenario = fallback[0] if fallback else reference_default

    def _temperature_lookup(scenario_name: str, climate_label: str) -> Path | pd.DataFrame | None:
        entry = scenario_temperature.get((scenario_name, climate_label))
        if entry is not None:
            return entry
        default = scenario_climate.get(scenario_name)
        if default is not None:
            return scenario_temperature.get((scenario_name, default))
        return None

    def _run_job(climate_label: str, reference_scenario: str, driver_scenario: str) -> bool:
        temp_reference = _temperature_lookup(reference_scenario, climate_label)
        temp_driver = _temperature_lookup(driver_scenario, climate_label)
        if temp_reference is None or temp_driver is None:
            print(
                f"[SCC] Skipping climate={climate_label} "
                f"(missing temperature data for {reference_scenario} or {driver_scenario})."
            )
            return False
        emission_reference = emission_cache.get(reference_scenario)
        emission_driver = emission_cache.get(driver_scenario)
        if emission_reference is None or emission_driver is None:
            parser.error("Emission data missing for reference or driver scenario.")

        reference_label = f"{reference_scenario}_{climate_label}"
        driver_label = f"{driver_scenario}_{climate_label}"

        temp_map = {
            reference_label: temp_reference,
            driver_label: temp_driver,
        }
        emission_map = {
            reference_label: emission_reference,
            driver_label: emission_driver,
        }

        gdp_frame_override: pd.DataFrame | None = None
        if use_socioeconomics:
            cache_key = climate_label or "__default__"
            if cache_key not in socio_cache:
                try:
                    socio_cache[cache_key] = _build_socioeconomic_projection(
                        root_cfg,
                        end_year=required_year,
                        climate_label=climate_label,
                        currency_conversion=currency_conv_2017,
                        currency_label=currency_label_2017,
                    )
                except (FileNotFoundError, ValueError) as exc:
                    parser.error(str(exc))
            gdp_frame_override = socio_cache.get(cache_key)

        gdp_currency_conversion = 1.0
        if gdp_frame_override is None:
            if gdp_population_directory is not None:
                gdp_currency_conversion = currency_conv_2023
            elif gdp_path:
                gdp_currency_conversion = currency_conv_2017

        inputs = EconomicInputs.from_csv(
            temp_map,
            emission_map,
            gdp_path,
            temperature_column=args.temperature_column,
            emission_column=args.emission_column,
            emission_to_tonnes=args.emission_unit_multiplier,
            gdp_frame=gdp_frame_override,
            gdp_population_directory=gdp_population_directory,
            gdp_currency_conversion=gdp_currency_conversion,
        )

        working_inputs = inputs
        damage_duration_years = config.get("damage_duration_years")
        if damage_duration_years is not None:
            try:
                damage_duration = int(damage_duration_years)
            except (TypeError, ValueError):
                parser.error("damage_duration_years must be an integer number of years.")
            if damage_duration <= 0:
                parser.error("damage_duration_years must be positive.")
            start_year = int(time_horizon_cfg.get("start", int(working_inputs.years.min())))
            if "start" in time_horizon_cfg and "end" in time_horizon_cfg:
                horizon_span = int(time_horizon_cfg["end"]) - int(time_horizon_cfg["start"])
                if damage_duration <= horizon_span:
                    parser.error("damage_duration_years must exceed the time_horizon span.")
            target_end = start_year + damage_duration - 1
            try:
                working_inputs = _restrict_inputs_window(working_inputs, start_year, target_end)
            except ValueError as exc:
                parser.error(str(exc))

        pulse_max_year = int(working_inputs.years.max())
        horizon_end_cfg = time_horizon_cfg.get("end")
        if horizon_end_cfg is not None:
            pulse_max_year = min(pulse_max_year, int(horizon_end_cfg))

        if aggregation == "average":
            if agg_start is None or agg_end is None:
                parser.error(
                    "When aggregation=='average', provide aggregation_horizon start and end."
                )
            try:
                agg_start_int = int(agg_start)
                agg_end_int = int(agg_end)
            except (TypeError, ValueError):
                parser.error("Aggregation horizon years must be integers.")
            if agg_start_int > agg_end_int:
                parser.error("aggregation_horizon.start must be <= aggregation_horizon.end.")
            if agg_start_int < int(working_inputs.years.min()) or agg_end_int > int(
                working_inputs.years.max()
            ):
                parser.error(
                    "Aggregation horizon "
                    f"{agg_start_int}-{agg_end_int} falls outside available data "
                    f"({working_inputs.years[0]}-{working_inputs.years[-1]})."
                )
            if not (agg_start_int <= args.base_year <= agg_end_int):
                parser.error(
                    f"Base year {args.base_year} must lie within aggregation "
                    f"horizon {agg_start_int}-{agg_end_int}."
                )
            try:
                working_inputs = _restrict_inputs_window(working_inputs, agg_start_int, agg_end_int)
            except ValueError as exc:
                parser.error(str(exc))

            if args.base_year not in set(working_inputs.years.tolist()):
                parser.error(
                    f"Base year {args.base_year} is outside the SCC evaluation window "
                    f"({working_inputs.years[0]}-{working_inputs.years[-1]})."
                )

            if agg_end is not None:
                pulse_max_year = min(pulse_max_year, int(agg_end))

        for discount_method in discount_methods:
            run_kwargs = {
                "scenario": driver_label,
                "reference": reference_label,
                "base_year": args.base_year,
                "aggregation": aggregation,
                "add_tco2": add_tco2_override,
                "damage_kwargs": dict(damage_kwargs),
            }
            if discount_method == "constant_discount":
                run_kwargs["discount_rate"] = args.discount_rate
            elif discount_method == "ramsey_discount":
                run_kwargs["rho"] = args.rho
                run_kwargs["eta"] = args.eta

            result = compute_scc(
                working_inputs,
                "pulse",
                discount_method=discount_method,
                pulse_max_year=pulse_max_year,
                fair_calibration=fair_calibration,
                climate_start_year=int(climate_parameters.get("start_year", 1750)),
                **run_kwargs,
            )

            cache_key = climate_label or driver_scenario
            scc_cache[(cache_key, discount_method)] = result
            _write_scc_outputs(result, discount_method, cache_key, output_dir)

            print(
                f"[SCC pulse:{discount_method}] SSP={cache_key} driver={driver_scenario} "
                f"vs {reference_scenario} (base year {result.base_year})"
            )
        return True

    if climate_jobs:
        ran_any = False
        for job in climate_jobs:
            ran_any = _run_job(job["climate"], job["reference"], job["driver"]) or ran_any
        if not ran_any:
            parser.error("No SCC results generated; check climate scenario availability.")
    else:
        ran_any = False
        for climate_label in climate_inputs:
            ran_any = _run_job(climate_label, reference_default, driver_scenario) or ran_any
        if not ran_any:
            parser.error("No SCC results generated; check climate scenario availability.")

    _run_summary_outputs(
        root_cfg,
        scc_cache,
        socio_cache,
        has_emissions=emission_root_path is not None,
        socio_output_dir=output_dir,
    )

    if not scc_cache:
        parser.error("No SCC results generated; check climate scenario availability.")

    if args.write_damages and emission_root_path is not None:
        for entry in damage_entries:
            climate_label = entry["climate"]
            scenario_name = entry["scenario"]
            mix_case = entry["mix"]
            scenario_label = f"{scenario_name}_{climate_label}"
            mix_dir = output_dir / mix_case
            mix_dir.mkdir(parents=True, exist_ok=True)
            for discount_method in discount_methods:
                scc_result = scc_cache.get((climate_label, discount_method))
                if scc_result is None:
                    continue
                emission_frame = emission_cache.get(scenario_name)
                damage_df = _build_damage_table(
                    scenario_label,
                    scc_result,
                    args.emission_unit_multiplier,
                    emission_frame=emission_frame,
                    emission_column=args.emission_column,
                )
                damage_df["climate_scenario"] = climate_label
                damage_df["method"] = discount_method
                damage_path = mix_dir / f"damages_{discount_method}_{scenario_label}.csv"
                damage_df.to_csv(damage_path, index=False)
    elif damage_entries:
        print("[SCC] Skipping damage exports (--skip-damages).")


def _deep_merge(target: MutableMapping[str, object], overrides: Mapping[str, object]) -> None:
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), MutableMapping):
            _deep_merge(target[key], value)  # type: ignore[index]
        else:
            target[key] = deepcopy(value)


def _resolve_suite_directory(
    base_config: Mapping[str, object],
    run_cfg: Mapping[str, object],
    suite_name: str,
) -> str | None:
    base_run_dir = get_results_run_directory(base_config, include_run_subdir=False)
    suite_base = sanitize_run_directory(run_cfg.get("output_subdir"))
    return join_run_directory(base_run_dir, suite_base, suite_name)


def _run_scenario_suite(
    base_config: Mapping[str, object],
    config_path: Path,
    argv: Sequence[str],
) -> None:
    run_cfg = base_config.get("run", {}) or {}
    scenario_file = run_cfg.get("scenario_file")
    if not scenario_file:
        raise ValueError("run.scenario_file must be provided when run.mode == 'scenarios'.")
    scenario_path = Path(scenario_file)
    if not scenario_path.is_absolute():
        scenario_path = (config_path.parent / scenario_path).resolve()
    if not scenario_path.exists():
        raise FileNotFoundError(f"Scenario file not found: {scenario_path}")

    with scenario_path.open() as handle:
        scenario_overrides = yaml.safe_load(handle) or {}
    if not isinstance(scenario_overrides, Mapping):
        raise ValueError("Scenario file must define a mapping of scenario names to overrides.")

    suite_name = sanitize_run_directory(scenario_path.stem) or "scenarios"
    suite_run_dir = _resolve_suite_directory(base_config, run_cfg, suite_name)
    base_root = config_path.parent

    for scenario_name, overrides in scenario_overrides.items():
        overrides = overrides or {}
        if not isinstance(overrides, Mapping):
            raise ValueError(f"Scenario '{scenario_name}' overrides must be a mapping.")
        config_copy = deepcopy(base_config)
        _deep_merge(config_copy, overrides)
        config_copy.setdefault("run", {})["mode"] = "normal"
        scenario_run_cfg = (
            overrides.get("run", {}) if isinstance(overrides.get("run"), Mapping) else {}
        )
        scenario_output = scenario_run_cfg.get("output_subdir") or scenario_name
        final_run_dir = join_run_directory(suite_run_dir, scenario_output)
        config_copy.setdefault("run", {})["output_subdir"] = scenario_output
        results_cfg = config_copy.setdefault("results", {})
        if isinstance(results_cfg, MutableMapping):
            if final_run_dir:
                results_cfg["run_directory"] = final_run_dir
            else:
                results_cfg.pop("run_directory", None)
        set_config_root(config_copy, base_root)
        if final_run_dir:
            print(f"[SCC] Scenario '{scenario_name}' → results/{final_run_dir}")
        else:
            print(f"[SCC] Scenario '{scenario_name}' → default results directory")
        _run_single_configuration(config_copy, argv)


def main(argv: Sequence[str] | None = None) -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
    econ_logger = logging.getLogger("economic_module")
    econ_logger.setLevel(logging.INFO)
    pulse_logger = logging.getLogger("economic_module.scc")
    pulse_logger.setLevel(logging.INFO)
    # Rely on root/economic_module handlers; avoid duplicate handlers on this logger.
    pulse_logger.handlers.clear()
    pulse_logger.propagate = True
    cli_args = list(argv) if argv is not None else sys.argv[1:]
    config_path = get_config_path(ROOT / "config.yaml")
    root_cfg = _load_config()
    set_config_root(root_cfg, config_path.parent)

    run_cfg = root_cfg.get("run", {}) or {}
    run_mode = str(run_cfg.get("mode", "normal")).strip().lower()

    if "--list-methods" in cli_args:
        _run_single_configuration(root_cfg, cli_args)
        return

    if run_mode == "scenarios":
        _run_scenario_suite(root_cfg, config_path, cli_args)
    else:
        _run_single_configuration(root_cfg, cli_args)


if __name__ == "__main__":
    main()
