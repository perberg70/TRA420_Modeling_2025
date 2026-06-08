"""Dynamic electricity demand helpers for calc_emissions."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

TODO_SOURCE = "TODO_SOURCE"


def build_dynamic_demand_series(
    cfg: Mapping[str, object],
    years: Sequence[int],
    *,
    config_path: Path,
) -> pd.Series:
    """Return annual electricity demand in TWh for a configured dynamic model."""

    if not isinstance(cfg, Mapping):
        raise TypeError("dynamic_model must be a mapping.")

    base_cfg = _required_mapping(cfg, "base_demand")
    base_year = int(cfg.get("base_year", base_cfg.get("year")))
    requested_years = [int(year) for year in years]
    model_years = sorted(set(requested_years + [base_year]))

    base_demand_twh = load_base_electricity_demand_twh(base_cfg, config_path=config_path)
    income_elasticity = _float_value(
        cfg.get("income_elasticity"),
        "dynamic_model.income_elasticity",
    )
    price_elasticity = _float_value(
        cfg.get("price_elasticity"),
        "dynamic_model.price_elasticity",
    )
    validate_elasticities(income_elasticity, price_elasticity)

    income = _load_driver_series(
        _required_mapping(cfg, "income", aliases=("gdp", "income_path")),
        model_years,
        config_path=config_path,
        label="income",
    )
    price = _load_driver_series(
        _required_mapping(cfg, "price", aliases=("electricity_price", "price_path")),
        model_years,
        config_path=config_path,
        label="price",
    )
    electrification = _build_electrification_series(
        _required_mapping(cfg, "electrification"),
        model_years,
        base_year=base_year,
        income=income,
    )

    income_base = _positive_lookup(income, base_year, "income base")
    price_base = _positive_lookup(price, base_year, "price base")
    electrification_base = _positive_lookup(
        electrification,
        base_year,
        "electrification base",
    )

    demand = (
        float(base_demand_twh)
        * (electrification / electrification_base)
        * np.power(income / income_base, income_elasticity)
        * np.power(price / price_base, price_elasticity)
    )
    return demand.reindex(requested_years).rename("demand_twh")


def validate_elasticities(income_elasticity: float, price_elasticity: float) -> None:
    """Validate elasticity constraints used by the dynamic demand model."""

    if income_elasticity < 1.0:
        raise ValueError("dynamic_model.income_elasticity must be >= 1.0.")
    if not -0.8 <= price_elasticity <= -0.2:
        raise ValueError(
            "dynamic_model.price_elasticity must be between -0.8 and -0.2 inclusive."
        )


def load_base_electricity_demand_twh(
    cfg: Mapping[str, object],
    *,
    config_path: Path,
) -> float:
    """Read base electricity demand from the OECD workbook reference table."""

    source = _required_text(cfg, "source", "dynamic_model.base_demand.source")
    path = _resolve_path(source, config_path)
    country_code = _required_text(
        cfg,
        "country_code",
        "dynamic_model.base_demand.country_code",
    ).upper()
    demand_column = _required_text(
        cfg,
        "demand_column",
        "dynamic_model.base_demand.demand_column",
    )
    year = int(_float_value(cfg.get("year"), "dynamic_model.base_demand.year"))

    if path.suffix.lower() not in {".xlsx", ".xls"}:
        raise ValueError("dynamic_model.base_demand.source must point to an Excel workbook.")

    sheet = cfg.get("sheet")
    sheets = [str(sheet)] if sheet else pd.ExcelFile(path).sheet_names
    last_error: Exception | None = None
    for sheet_name in sheets:
        try:
            raw = pd.read_excel(path, sheet_name=sheet_name, header=None)
            return _extract_reference_demand_twh(
                raw,
                country_code=country_code,
                year=year,
                demand_column=demand_column,
                unit_column=cfg.get("unit_column"),
            )
        except Exception as exc:  # pragma: no cover - only used across workbook sheets
            last_error = exc
            continue
    message = f"Base electricity demand not found for {country_code} {year} in {path}."
    if last_error is not None:
        message = f"{message} Last error: {last_error}"
    raise ValueError(message)


def _extract_reference_demand_twh(
    raw: pd.DataFrame,
    *,
    country_code: str,
    year: int,
    demand_column: str,
    unit_column: object | None,
) -> float:
    reference_rows = raw.index[
        raw.iloc[:, 0].astype(str).str.strip().str.casefold() == "reference values"
    ].tolist()
    if not reference_rows:
        raise ValueError("Reference values table not found.")

    header_row = int(reference_rows[0]) + 1
    headers = ["" if pd.isna(value) else str(value).strip() for value in raw.iloc[header_row]]
    normalized_headers = [_normalize_header(value) for value in headers]

    year_idx = _find_header(normalized_headers, "year")
    demand_idx = _find_header(normalized_headers, demand_column)
    if unit_column is None:
        unit_idx = demand_idx + 1
    else:
        unit_idx = _find_header(normalized_headers, str(unit_column))
    if unit_idx >= raw.shape[1]:
        raise ValueError("Unit column for base electricity demand is outside the table.")

    data = raw.iloc[header_row + 1 :].copy()
    country_match = data.iloc[:, 0].astype(str).str.strip().str.upper() == country_code
    years = pd.to_numeric(data.iloc[:, year_idx], errors="coerce")
    matches = data[country_match & (years == year)]
    if matches.empty:
        raise ValueError(f"Reference values table has no row for {country_code} {year}.")

    value = _float_value(matches.iloc[0, demand_idx], "base electricity demand")
    unit = str(matches.iloc[0, unit_idx]).strip()
    return _convert_electricity_to_twh(value, unit)


def _load_driver_series(
    cfg: Mapping[str, object],
    years: Sequence[int],
    *,
    config_path: Path,
    label: str,
) -> pd.Series:
    values = cfg.get("values")
    if isinstance(values, Mapping):
        return _values_to_series(values, years, label)

    source = cfg.get("source")
    if source is None:
        raise ValueError(
            f"dynamic_model.{label} must define 'values' or 'source'. "
            "Use TODO_SOURCE until the data source is selected."
        )
    path = _resolve_path(
        _required_text(cfg, "source", f"dynamic_model.{label}.source"),
        config_path,
    )
    year_column = str(cfg.get("year_column", "year"))
    value_column = cfg.get("value_column") or cfg.get("column")
    if value_column is None:
        raise ValueError(f"dynamic_model.{label}.value_column must be set for source data.")

    if path.suffix.lower() in {".xlsx", ".xls"}:
        frame = pd.read_excel(path, sheet_name=cfg.get("sheet", 0))
    else:
        frame = pd.read_csv(path)

    country_column = cfg.get("country_column")
    country_code = cfg.get("country_code")
    if country_column and country_code:
        frame = frame[
            frame[str(country_column)].astype(str).str.strip().str.upper()
            == str(country_code).strip().upper()
        ]
    if frame.empty:
        raise ValueError(f"dynamic_model.{label} source has no matching rows.")
    if year_column not in frame.columns or str(value_column) not in frame.columns:
        raise ValueError(
            f"dynamic_model.{label} source must contain '{year_column}' and '{value_column}'."
        )

    mapping = dict(
        zip(
            frame[year_column].astype(int),
            frame[str(value_column)].astype(float),
            strict=False,
        )
    )
    return _values_to_series(mapping, years, label)


def _build_electrification_series(
    cfg: Mapping[str, object],
    years: Sequence[int],
    *,
    base_year: int,
    income: pd.Series,
) -> pd.Series:
    form = _required_text(cfg, "form", "dynamic_model.electrification.form").strip().lower()
    slope = _float_value(
        cfg.get("B", cfg.get("b", cfg.get("slope"))),
        "dynamic_model.electrification.B",
    )
    index = pd.Index([int(year) for year in years])

    if form == "linear_time":
        intercept = _float_value(
            cfg.get("A", cfg.get("a", cfg.get("intercept"))),
            "dynamic_model.electrification.A",
        )
        values = intercept + slope * (index.to_numpy(dtype=float) - float(base_year))
    elif form == "linear_income":
        income_aligned = income.reindex(index)
        if "base_share" in cfg or "e_base" in cfg:
            base_share = _float_value(
                cfg.get("base_share", cfg.get("e_base")),
                "dynamic_model.electrification.base_share",
            )
            income_base = _positive_lookup(income, base_year, "income base")
            values = base_share + slope * (income_aligned.to_numpy(dtype=float) - income_base)
        else:
            intercept = _float_value(
                cfg.get("A", cfg.get("a", cfg.get("intercept"))),
                "dynamic_model.electrification.A",
            )
            values = intercept + slope * income_aligned.to_numpy(dtype=float)
    else:
        raise ValueError("dynamic_model.electrification.form must be linear_time or linear_income.")

    series = pd.Series(values, index=index, dtype=float, name="electrification_share")
    bounds_mode = str(cfg.get("bounds", "validate")).strip().lower()
    if bool(cfg.get("clamp", False)) or bounds_mode == "clamp":
        return series.clip(lower=0.0, upper=1.0)
    if ((series < 0.0) | (series > 1.0)).any():
        bad_years = [int(year) for year in series.index[(series < 0.0) | (series > 1.0)]]
        raise ValueError(
            "dynamic_model.electrification produces values outside [0, 1] "
            f"for years: {bad_years}. Set bounds: clamp or revise assumptions."
        )
    return series


def _values_to_series(
    values: Mapping[object, object],
    years: Sequence[int],
    label: str,
) -> pd.Series:
    mapping: dict[int, float] = {}
    for key, value in values.items():
        _reject_todo(value, f"dynamic_model.{label}.values[{key}]")
        mapping[int(key)] = float(value)
    if not mapping:
        raise ValueError(f"No values provided for dynamic_model.{label}.")

    index = pd.Index(sorted(set(int(year) for year in years)))
    series = pd.Series(mapping, dtype=float).sort_index()
    series = series.reindex(index.union(series.index), method=None)
    series = series.interpolate(method="index").ffill().bfill()
    return series.reindex(index).rename(label)


def _resolve_path(source: str, config_path: Path) -> Path:
    _reject_todo(source, "source")
    path = Path(source)
    candidates = [path] if path.is_absolute() else []
    if not path.is_absolute():
        candidates.extend(
            [
                (config_path.parent / path).resolve(),
                (Path(__file__).resolve().parents[2] / path).resolve(),
            ]
        )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    checked = ", ".join(str(candidate) for candidate in candidates) or source
    raise FileNotFoundError(f"Dynamic demand source not found. Checked: {checked}")


def _required_mapping(
    cfg: Mapping[str, object],
    key: str,
    *,
    aliases: Sequence[str] = (),
) -> Mapping[str, object]:
    for candidate in (key, *aliases):
        value = cfg.get(candidate)
        if isinstance(value, Mapping):
            return value
        if value is not None:
            raise TypeError(f"dynamic_model.{candidate} must be a mapping.")
    alias_text = ", ".join((key, *aliases))
    raise ValueError(f"dynamic_model must define one of: {alias_text}.")


def _required_text(cfg: Mapping[str, object], key: str, label: str) -> str:
    value = cfg.get(key)
    if value is None:
        raise ValueError(f"{label} must be set.")
    _reject_todo(value, label)
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label} must not be empty.")
    return text


def _float_value(value: object, label: str) -> float:
    if value is None:
        raise ValueError(f"{label} must be set.")
    _reject_todo(value, label)
    result = float(value)
    if not np.isfinite(result):
        raise ValueError(f"{label} must be finite.")
    return result


def _positive_lookup(series: pd.Series, year: int, label: str) -> float:
    value = float(series.loc[int(year)])
    if value <= 0.0:
        raise ValueError(f"{label} must be greater than zero.")
    return value


def _find_header(headers: Sequence[str], target: str) -> int:
    normalized = _normalize_header(target)
    try:
        return list(headers).index(normalized)
    except ValueError as exc:
        raise ValueError(f"Reference values table missing column '{target}'.") from exc


def _normalize_header(value: object) -> str:
    return " ".join(str(value).strip().casefold().split())


def _convert_electricity_to_twh(value: float, unit: str) -> float:
    unit_norm = unit.strip().casefold().replace(" ", "")
    if unit_norm == "twh":
        return float(value)
    if unit_norm == "gwh":
        return float(value) / 1_000.0
    if unit_norm == "mwh":
        return float(value) / 1_000_000.0
    raise ValueError(f"Unsupported electricity demand unit '{unit}'. Expected MWh, GWh, or TWh.")


def _reject_todo(value: object, label: str) -> None:
    if isinstance(value, str) and value.strip().upper() == TODO_SOURCE:
        raise ValueError(f"{label} is TODO_SOURCE; provide a data source or scenario assumption.")
