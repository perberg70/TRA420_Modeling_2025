import json
from pathlib import Path

import pandas as pd
import pytest

from calc_emissions.demand_model import (
    build_dynamic_demand_series,
    load_base_electricity_demand_twh,
    validate_elasticities,
)
from calc_emissions.calculator import run_from_config


def _write_reference_workbook(path: Path, *, value: float = 30021.0, unit: str = "GWh") -> None:
    rows = [
        ["Reference values", None, None, None],
        [None, "year", "Total electricity demand", "unit"],
        ["SRB", 2023, value, unit],
    ]
    pd.DataFrame(rows).to_excel(path, index=False, header=False)


def test_load_base_electricity_demand_converts_gwh_to_twh(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook)

    demand = load_base_electricity_demand_twh(
        {
            "source": str(workbook),
            "country_code": "SRB",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        config_path=tmp_path / "config.yaml",
    )

    assert demand == pytest.approx(30.021)


def test_load_base_electricity_demand_converts_mwh_to_twh(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook, value=5_887_708, unit="MWh")

    demand = load_base_electricity_demand_twh(
        {
            "source": str(workbook),
            "country_code": "SRB",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        config_path=tmp_path / "config.yaml",
    )

    assert demand == pytest.approx(5.887708)


def test_dynamic_demand_uses_excel_base_and_elasticity_formula(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook)
    cfg = {
        "base_demand": {
            "source": str(workbook),
            "country_code": "SRB",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        "income": {
            "values": {
                2023: 100.0,
                2025: 110.0,
                2030: 120.0,
            }
        },
        "price": {
            "values": {
                2023: 100.0,
                2025: 90.0,
                2030: 110.0,
            }
        },
        "electrification": {
            "form": "linear_time",
            "A": 0.5,
            "B": 0.01,
        },
        "income_elasticity": 1.1,
        "price_elasticity": -0.4,
    }

    demand = build_dynamic_demand_series(
        cfg,
        [2025, 2030],
        config_path=tmp_path / "config.yaml",
    )

    expected_2025 = 30.021 * (0.52 / 0.5) * (110.0 / 100.0) ** 1.1 * (
        90.0 / 100.0
    ) ** -0.4
    expected_2030 = 30.021 * (0.57 / 0.5) * (120.0 / 100.0) ** 1.1 * (
        110.0 / 100.0
    ) ** -0.4
    assert demand.loc[2025] == pytest.approx(expected_2025)
    assert demand.loc[2030] == pytest.approx(expected_2030)


def test_dynamic_demand_supports_linear_income_electrification(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook)
    cfg = {
        "base_demand": {
            "source": str(workbook),
            "country_code": "SRB",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        "income": {"values": {2023: 100.0, 2025: 110.0}},
        "price": {"values": {2023: 100.0, 2025: 100.0}},
        "electrification": {
            "form": "linear_income",
            "base_share": 0.4,
            "B": 0.002,
        },
        "income_elasticity": 1.0,
        "price_elasticity": -0.2,
    }

    demand = build_dynamic_demand_series(
        cfg,
        [2025],
        config_path=tmp_path / "config.yaml",
    )

    assert demand.loc[2025] == pytest.approx(30.021 * (0.42 / 0.4) * 1.1)


@pytest.mark.parametrize(
    ("income_elasticity", "price_elasticity"),
    [
        (0.99, -0.4),
        (1.0, -0.81),
        (1.0, -0.19),
    ],
)
def test_dynamic_demand_validates_elasticity_bounds(
    income_elasticity: float,
    price_elasticity: float,
):
    with pytest.raises(ValueError):
        validate_elasticities(income_elasticity, price_elasticity)


def test_dynamic_demand_rejects_todo_source(tmp_path: Path):
    cfg = {
        "base_demand": {
            "source": "TODO_SOURCE",
            "country_code": "SRB",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        "income": {"values": {2023: 100.0, 2025: 101.0}},
        "price": {"values": {2023: 100.0, 2025: 100.0}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.0},
        "income_elasticity": 1.0,
        "price_elasticity": -0.2,
    }

    with pytest.raises(ValueError, match="TODO_SOURCE"):
        build_dynamic_demand_series(cfg, [2025], config_path=tmp_path / "config.yaml")


def test_run_from_config_accepts_dynamic_model_demand_case(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook)
    factors_path = tmp_path / "factors.csv"
    pd.DataFrame(
        {
            "technology": ["coal"],
            "co2_mt_per_twh": [1.0],
        }
    ).to_csv(factors_path, index=False)
    config = {
        "calc_emissions": {
            "emission_factors_file": str(factors_path),
            "demand_scenarios": {
                "base_demand": {
                    "dynamic_model": {
                        "base_demand": {
                            "source": str(workbook),
                            "country_code": "SRB",
                            "demand_column": "Total electricity demand",
                            "year": 2023,
                        },
                        "income": {"values": {2023: 100.0, 2025: 100.0}},
                        "price": {"values": {2023: 100.0, 2025: 100.0}},
                        "electrification": {
                            "form": "linear_time",
                            "A": 0.5,
                            "B": 0.0,
                        },
                        "income_elasticity": 1.0,
                        "price_elasticity": -0.2,
                    }
                }
            },
            "mix_scenarios": {"base_mix": {"shares": {"coal": 1.0}}},
        }
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps(config))

    results = run_from_config(
        config_path,
        default_years={"start": 2025, "end": 2026, "step": 1},
    )

    result = results["base_mix__base_demand"]
    assert result.demand_twh.loc[2025] == pytest.approx(30.021)
    assert result.total_emissions_mt["co2"].loc[2025] == pytest.approx(30.021)
