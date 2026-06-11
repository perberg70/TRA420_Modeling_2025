"""Load and apply FaIR calibration datasets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd
from fair import FAIR

HISTORICAL_SPECIE_MAP: Mapping[str, str] = {
    "CO2 FFI": "CO2 FFI",
    "CO2 AFOLU": "CO2 AFOLU",
    "CH4": "CH4",
    "N2O": "N2O",
}


@dataclass(slots=True)
class FairCalibration:
    """Container with calibration parameters and driver series."""

    parameters: pd.Series
    co2_row: pd.Series
    ch4_row: pd.Series
    historical_emissions: pd.DataFrame
    historical_years: np.ndarray
    solar_forcing: pd.DataFrame
    volcanic_forcing: pd.DataFrame
    landuse_scale: float | None = None
    lapsi_scale: float | None = None
    warming_baseline: dict[str, float] | None = None


def _resolve(base: Path, candidate: str | Path) -> Path:
    path = Path(candidate)
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _select_ensemble_row(
    df: pd.DataFrame, member_id: int | None, member_index: int | None
) -> pd.Series:
    table = df.copy()
    if "Unnamed: 0" in table.columns:
        table = table.rename(columns={"Unnamed: 0": "ensemble_id"})
    if member_id is not None:
        matches = table[table.get("ensemble_id") == member_id]
        if matches.empty:
            raise ValueError(f"Ensemble member id {member_id} not found in calibration table.")
        return matches.iloc[0]
    if member_index is not None:
        if not 0 <= member_index < len(table):
            raise ValueError(
                f"ensemble_member_index {member_index} outside available range 0-{len(table) - 1}."
            )
        return table.iloc[member_index]
    return table.iloc[0]


def _select_row_by_label(df: pd.DataFrame, label: str) -> pd.Series:
    if df.empty:
        raise ValueError("Calibration table is empty.")
    label_col = df.columns[0]
    table = df.set_index(label_col)
    table.index = table.index.map(lambda x: str(x).strip())
    key = str(label).strip()
    if key not in table.index:
        raise ValueError(f"Label '{label}' not found in calibration table {label_col}.")
    return table.loc[key]


def load_fair_calibration(
    cfg: Mapping[str, object],
    *,
    repo_root: Path,
) -> FairCalibration | None:
    """Load calibration parameters and drivers when enabled."""

    if not cfg or not bool(cfg.get("enabled", False)):
        return None

    base_candidate = cfg.get("base_path", "data/FaIR_calibration_data/v1.5.0")
    base_path = _resolve(repo_root, base_candidate)
    if not base_path.exists():
        raise FileNotFoundError(f"Calibration base path not found: {base_path}")

    ensemble_path = _resolve(
        base_path, cfg.get("ensemble_file", "calibrated_constrained_parameters.csv")
    )
    species_path = _resolve(base_path, cfg.get("species_file", "species_configs_properties.csv"))
    ch4_path = _resolve(base_path, cfg.get("ch4_lifetime_file", "CH4_lifetime.csv"))
    hist_emissions_path = _resolve(
        base_path, cfg.get("historical_emissions_file", "historical_emissions_1750-2023_cmip7.csv")
    )
    solar_path = _resolve(base_path, cfg.get("solar_forcing_file", "solar_forcing_timebounds.csv"))
    volcanic_path = _resolve(
        base_path, cfg.get("volcanic_forcing_file", "volcanic_forcing_timebounds.csv")
    )

    ensemble_df = pd.read_csv(ensemble_path)
    parameters = _select_ensemble_row(
        ensemble_df,
        member_id=cfg.get("ensemble_member_id"),
        member_index=cfg.get("ensemble_member_index"),
    )

    species_df = pd.read_csv(species_path)
    co2_name = str(cfg.get("co2_species_name", "CO2"))
    co2_matches = species_df[species_df["name"] == co2_name]
    if co2_matches.empty:
        raise ValueError(f"Species '{co2_name}' not present in {species_path}.")
    co2_row = co2_matches.iloc[0]

    ch4_df = pd.read_csv(ch4_path)
    ch4_label = str(cfg.get("ch4_lifetime_label", "historical_best"))
    ch4_row = _select_row_by_label(ch4_df, ch4_label)

    historical_df = pd.read_csv(hist_emissions_path)
    # Keep the world historical scenario entries only.
    mask = (historical_df.get("region") == "World") & (
        historical_df.get("scenario") == "historical"
    )
    if mask.notnull().any():
        filtered = historical_df[mask.fillna(False)]
        if not filtered.empty:
            historical_df = filtered
    year_columns = [col for col in historical_df.columns if str(col).isdigit()]
    if not year_columns:
        raise ValueError("Historical emissions table lacks year columns.")
    historical_years = np.array(sorted(int(col) for col in year_columns), dtype=int)

    solar_df = pd.read_csv(solar_path)
    volcanic_df = pd.read_csv(volcanic_path)

    landuse_scale = None
    lapsi_scale = None
    if cfg.get("landuse_scale_file"):
        landuse_row = _select_row_by_label(
            pd.read_csv(_resolve(base_path, cfg["landuse_scale_file"])),
            str(cfg.get("landuse_scale_label", "historical_best")),
        )
        landuse_scale = float(landuse_row.iloc[0])
    if cfg.get("lapsi_scale_file"):
        lapsi_row = _select_row_by_label(
            pd.read_csv(_resolve(base_path, cfg["lapsi_scale_file"])),
            str(cfg.get("lapsi_scale_label", "historical_best")),
        )
        lapsi_scale = float(lapsi_row.iloc[0])

    warming_baseline = None
    if cfg.get("warming_baselines_file"):
        warming_df = pd.read_csv(_resolve(base_path, cfg["warming_baselines_file"]))
        label = cfg.get("warming_baseline_label")
        if label is not None:
            row = _select_row_by_label(warming_df, str(label))
            column = cfg.get("warming_baseline_column")
            if column and column in row:
                warming_baseline = {"label": str(label), "value": float(row[column])}

    return FairCalibration(
        parameters=parameters,
        co2_row=co2_row,
        ch4_row=ch4_row,
        historical_emissions=historical_df,
        historical_years=historical_years,
        solar_forcing=solar_df,
        volcanic_forcing=volcanic_df,
        landuse_scale=landuse_scale,
        lapsi_scale=lapsi_scale,
        warming_baseline=warming_baseline,
    )


def _set_species_parameter(model: FAIR, field: str, specie: str, values: Sequence[float]) -> None:
    arr = model.species_configs[field]
    if "specie" not in arr.dims:
        arr.loc[:] = np.full(len(model.configs), float(values[0] if values else 0.0))
        return
    if "gasbox" in arr.dims:
        for idx, value in enumerate(values):
            arr.loc[{"specie": specie, "gasbox": idx}] = float(value)
    else:
        arr.loc[{"specie": specie}] = np.full(len(model.configs), float(values[0]))


def _apply_co2_properties(model: FAIR, calibration: FairCalibration) -> None:
    specie = calibration.co2_row["name"]
    n_boxes = model.species_configs["partition_fraction"].sizes.get("gasbox", 0)
    partitions = [calibration.co2_row.get(f"partition_fraction{i}", 0.0) for i in range(n_boxes)]
    lifetimes = [calibration.co2_row.get(f"unperturbed_lifetime{i}", 0.0) for i in range(n_boxes)]
    _set_species_parameter(model, "partition_fraction", specie, partitions)
    _set_species_parameter(model, "unperturbed_lifetime", specie, lifetimes)
    for column in ("iirf_0", "iirf_airborne", "iirf_uptake", "iirf_temperature", "g0", "g1"):
        if column in calibration.co2_row:
            values = np.full(len(model.configs), float(calibration.co2_row[column]))
            model.species_configs[column].loc[{"specie": specie}] = values


def _apply_ch4_lifetime(model: FAIR, calibration: FairCalibration) -> None:
    specie = "CH4"
    base_value = float(
        calibration.ch4_row.get("base", calibration.co2_row.get("unperturbed_lifetime0", 0.0))
    )
    lifetime = [base_value] * model.species_configs["unperturbed_lifetime"].sizes.get("gasbox", 0)
    _set_species_parameter(model, "unperturbed_lifetime", specie, lifetime)
    if "CH4" in calibration.ch4_row:
        chem = model.species_configs["ch4_lifetime_chemical_sensitivity"]
        target = np.full(len(model.configs), float(calibration.ch4_row["CH4"]))
        if "specie" in chem.dims:
            chem.loc[{"specie": specie}] = target
        else:
            chem.loc[:] = target
    if "temp" in calibration.ch4_row:
        temp_field = model.species_configs["lifetime_temperature_sensitivity"]
        temp_values = np.full(len(model.configs), float(calibration.ch4_row["temp"]))
        if "specie" in temp_field.dims:
            temp_field.loc[{"specie": specie}] = temp_values
        else:
            temp_field.loc[:] = temp_values


def _apply_climate_parameters(model: FAIR, parameters: pd.Series) -> None:
    def _collect(prefix: str) -> list[float]:
        values: list[float] = []
        idx = 0
        while f"{prefix}[{idx}]" in parameters.index:
            values.append(float(parameters[f"{prefix}[{idx}]"]))
            idx += 1
        return values

    capacities = _collect("ocean_heat_capacity")
    transfers = _collect("ocean_heat_transfer")
    for cfg in model.configs:
        if capacities:
            model.climate_configs["ocean_heat_capacity"].loc[cfg, :] = np.asarray(
                capacities, dtype=float
            )
        if transfers:
            model.climate_configs["ocean_heat_transfer"].loc[cfg, :] = np.asarray(
                transfers, dtype=float
            )
        if "deep_ocean_efficacy" in parameters:
            model.climate_configs["deep_ocean_efficacy"].loc[cfg] = float(
                parameters["deep_ocean_efficacy"]
            )
        if "forcing_4co2" in parameters:
            model.climate_configs["forcing_4co2"].loc[cfg] = float(parameters["forcing_4co2"])

    for specie in model.species:
        scale_key = f"forcing_scale[{specie}]"
        if scale_key in parameters:
            model.species_configs["forcing_scale"].loc[{"specie": specie}] = np.full(
                len(model.configs), float(parameters[scale_key])
            )
        base_key = f"baseline_concentration[{specie}]"
        if base_key in parameters:
            model.species_configs["baseline_concentration"].loc[{"specie": specie}] = np.full(
                len(model.configs), float(parameters[base_key])
            )


def _override_historical_emissions(
    model: FAIR, calibration: FairCalibration, scenario: str
) -> None:
    timepoints = np.asarray(model.timepoints, dtype=float)
    for variable, specie in HISTORICAL_SPECIE_MAP.items():
        rows = calibration.historical_emissions[
            calibration.historical_emissions["variable"] == variable
        ]
        if rows.empty:
            continue
        row = rows.iloc[0]
        values = row[[str(year) for year in calibration.historical_years]].astype(float).to_numpy()
        lookup = dict(zip(calibration.historical_years, values, strict=False))
        for cfg in model.configs:
            selection = {"specie": specie, "scenario": scenario, "config": cfg}
            baseline = model.emissions.loc[selection].values.copy()
            for idx, point in enumerate(timepoints):
                year = int(np.floor(point))
                if year in lookup:
                    baseline[idx] = lookup[year]
            model.emissions.loc[selection] = baseline


def _interpolate_forcing(timebounds: np.ndarray, forcing_df: pd.DataFrame) -> np.ndarray:
    years = forcing_df[forcing_df.columns[0]].to_numpy(dtype=float)
    values = forcing_df[forcing_df.columns[1]].to_numpy(dtype=float)
    return np.interp(timebounds, years, values, left=values[0], right=values[-1])


def _override_forcing(model: FAIR, calibration: FairCalibration, scenario: str) -> None:
    timebounds = np.asarray(model.timebounds, dtype=float)
    solar = _interpolate_forcing(timebounds, calibration.solar_forcing)
    volcanic = _interpolate_forcing(timebounds, calibration.volcanic_forcing)
    for cfg in model.configs:
        model.forcing.loc[{"scenario": scenario, "config": cfg, "specie": "Solar"}] = solar
        model.forcing.loc[{"scenario": scenario, "config": cfg, "specie": "Volcanic"}] = volcanic


def _apply_forcing_scaling(model: FAIR, calibration: FairCalibration, scenario: str) -> None:
    """Scale land-use and LAPSI forcing series when calibration provides factors."""

    def _scale(specie: str, factor: float | None) -> None:
        if factor is None:
            return
        try:
            selection = {"scenario": scenario, "specie": specie}
            model.forcing.loc[selection] = model.forcing.loc[selection] * float(factor)
        except KeyError:  # specie absent in this configuration
            return

    _scale("Land use", calibration.landuse_scale)
    _scale("Light absorbing particles on snow and ice", calibration.lapsi_scale)


def apply_fair_calibration(model: FAIR, calibration: FairCalibration, scenario: str) -> None:
    """Apply calibration parameters and drivers to a FaIR model."""

    _apply_climate_parameters(model, calibration.parameters)
    _apply_co2_properties(model, calibration)
    _apply_ch4_lifetime(model, calibration)
    _override_historical_emissions(model, calibration, scenario)
    _override_forcing(model, calibration, scenario)
    _apply_forcing_scaling(model, calibration, scenario)
