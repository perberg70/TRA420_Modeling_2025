import json
from pathlib import Path

import pandas as pd
import pytest

from calc_emissions.calculator import run_from_config
from calc_emissions.demand_model import (
    build_dynamic_demand_components,
    build_dynamic_demand_series,
    load_base_electricity_demand_twh,
    load_workbook_reform_price_index,
    load_workbook_shiftable_share,
    validate_elasticities,
)


def _write_reference_workbook(path: Path, *, value: float = 30021.0, unit: str = "GWh") -> None:
    rows = [
        ["Reference values", None, None, None],
        [None, "year", "Total electricity demand", "unit"],
        ["TST", 2023, value, unit],
    ]
    pd.DataFrame(rows).to_excel(path, index=False, header=False)


def _write_reform_workbook(path: Path) -> None:
    """Workbook mimicking the Electricity_OECD.xlsx layout with reform tables."""

    rows = [
        ["Reference values", None, None, None, None, None, None, None],
        [
            None,
            "year",
            "Total electricity demand",
            "unit",
            "Size of demand subject to shift",
            "unit",
            "Average price (regulated)",
            "unit",
        ],
        ["TST", 2023, 100.0, "TWh", 40.0, "TWh", 8.0, "cEUR/kWh"],
        [None, None, None, None, None, None, None, None],
        ["Scenario 1 ", None, None, None, "Price (regulated)", "unit", None, None],
        ["Lower bound ", None, None, None, None, None, None, None],
        ["TST", None, None, None, 96.0, "EUR/MWh", None, None],
        [None, None, None, None, None, None, None, None],
        ["Upper bound", None, None, None, None, None, None, None],
        ["TST", None, None, None, 160.0, "EUR/MWh", None, None],
    ]
    pd.DataFrame(rows).to_excel(path, index=False, header=False)


def _reform_base_cfg(workbook: Path) -> dict:
    return {
        "source": str(workbook),
        "country_code": "TST",
        "demand_column": "Total electricity demand",
        "year": 2023,
    }


@pytest.fixture
def reference_workbook_100_twh(tmp_path: Path) -> Path:
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook, value=100.0, unit="TWh")
    return workbook


@pytest.fixture
def emission_factors_path(tmp_path: Path) -> Path:
    factors_path = tmp_path / "factors.csv"
    pd.DataFrame(
        {
            "technology": ["coal"],
            "co2_mt_per_twh": [1.0],
        }
    ).to_csv(factors_path, index=False)
    return factors_path


def test_load_base_electricity_demand_converts_gwh_to_twh(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook)

    demand = load_base_electricity_demand_twh(
        {
            "source": str(workbook),
            "country_code": "TST",
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
            "country_code": "TST",
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
            "country_code": "TST",
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

    expected_2025 = 30.021 * (0.52 / 0.5) * (110.0 / 100.0) ** 1.1 * (90.0 / 100.0) ** -0.4
    expected_2030 = 30.021 * (0.57 / 0.5) * (120.0 / 100.0) ** 1.1 * (110.0 / 100.0) ** -0.4
    assert demand.loc[2025] == pytest.approx(expected_2025)
    assert demand.loc[2030] == pytest.approx(expected_2030)


def test_dynamic_demand_supports_linear_income_electrification(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook)
    cfg = {
        "base_demand": {
            "source": str(workbook),
            "country_code": "TST",
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


def test_dynamic_demand_population_uses_per_capita_income_and_scales_demand(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook)
    cfg = {
        "base_demand": {
            "source": str(workbook),
            "country_code": "TST",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        # Total GDP grows 21%, population 10% => GDP per capita grows 10%.
        "income": {"values": {2023: 100.0, 2025: 121.0}},
        "population": {"values": {2023: 1.0, 2025: 1.1}},
        "price": {"values": {2023: 100.0, 2025: 100.0}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.0},
        "income_elasticity": 1.0,
        "price_elasticity": -0.2,
    }

    demand = build_dynamic_demand_series(
        cfg,
        [2025],
        config_path=tmp_path / "config.yaml",
    )

    expected = 30.021 * 1.1 * (110.0 / 100.0) ** 1.0
    assert demand.loc[2025] == pytest.approx(expected)


def test_dynamic_demand_population_can_omit_price_path(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook)
    cfg = {
        "base_demand": {
            "source": str(workbook),
            "country_code": "TST",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        # Total GDP grows 21%, population 10% => GDP per capita grows 10%.
        "income": {"values": {2023: 100.0, 2025: 121.0}},
        "population": {"values": {2023: 1.0, 2025: 1.1}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.0},
        "income_elasticity": 1.0,
    }

    demand = build_dynamic_demand_series(
        cfg,
        [2025],
        config_path=tmp_path / "config.yaml",
    )

    expected = 30.021 * 1.1 * (110.0 / 100.0) ** 1.0
    assert demand.loc[2025] == pytest.approx(expected)


def test_dynamic_demand_rejects_price_elasticity_without_price_path(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook)
    cfg = {
        "base_demand": {
            "source": str(workbook),
            "country_code": "TST",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        "income": {"values": {2023: 100.0, 2025: 121.0}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.0},
        "income_elasticity": 1.0,
        "price_elasticity": -0.2,
    }

    with pytest.raises(ValueError, match="price_elasticity requires a price path"):
        build_dynamic_demand_series(
            cfg,
            [2025],
            config_path=tmp_path / "config.yaml",
        )


def test_dynamic_demand_filters_driver_sources(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook)
    income_path = tmp_path / "income.csv"
    pd.DataFrame(
        {
            "country_code": ["TST", "TST", "TST", "TST"],
            "scenario": ["SSP1", "SSP1", "SSP2", "SSP2"],
            "year": [2023, 2025, 2023, 2025],
            "value": [50.0, 55.0, 100.0, 120.0],
        }
    ).to_csv(income_path, index=False)
    cfg = {
        "base_demand": {
            "source": str(workbook),
            "country_code": "TST",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        "income": {
            "source": str(income_path),
            "country_column": "country_code",
            "country_code": "TST",
            "value_column": "value",
            "filters": {"scenario": "SSP2"},
        },
        "price": {"values": {2023: 100.0, 2025: 100.0}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.0},
        "income_elasticity": 1.0,
        "price_elasticity": -0.2,
    }

    demand = build_dynamic_demand_series(
        cfg,
        [2025],
        config_path=tmp_path / "config.yaml",
    )

    assert demand.loc[2025] == pytest.approx(30.021 * 1.2)


def test_dynamic_demand_rejects_duplicate_driver_years_after_filtering(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook)
    income_path = tmp_path / "income.csv"
    pd.DataFrame(
        {
            "country_code": ["TST", "TST", "TST"],
            "scenario": ["SSP2", "SSP2", "SSP2"],
            "year": [2023, 2025, 2025],
            "value": [100.0, 120.0, 121.0],
        }
    ).to_csv(income_path, index=False)
    cfg = {
        "base_demand": {
            "source": str(workbook),
            "country_code": "TST",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        "income": {
            "source": str(income_path),
            "country_column": "country_code",
            "country_code": "TST",
            "value_column": "value",
            "filters": {"scenario": "SSP2"},
        },
        "price": {"values": {2023: 100.0, 2025: 100.0}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.0},
        "income_elasticity": 1.0,
        "price_elasticity": -0.2,
    }

    with pytest.raises(ValueError, match="duplicate rows.*2025"):
        build_dynamic_demand_series(
            cfg,
            [2025],
            config_path=tmp_path / "config.yaml",
        )


def test_dynamic_demand_components_returns_electrification_series(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reference_workbook(workbook)
    cfg = {
        "base_demand": {
            "source": str(workbook),
            "country_code": "TST",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        "income": {"values": {2023: 100.0, 2025: 100.0, 2030: 100.0}},
        "price": {"values": {2023: 100.0, 2025: 100.0, 2030: 100.0}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.01},
        "income_elasticity": 1.0,
        "price_elasticity": -0.2,
    }

    demand, electrification = build_dynamic_demand_components(
        cfg,
        [2025, 2030],
        config_path=tmp_path / "config.yaml",
    )

    assert electrification.loc[2025] == pytest.approx(0.52)
    assert electrification.loc[2030] == pytest.approx(0.57)
    assert demand.loc[2025] == pytest.approx(30.021 * (0.52 / 0.5))


def test_load_workbook_shiftable_share_and_price_index(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reform_workbook(workbook)
    base_cfg = _reform_base_cfg(workbook)
    config_path = tmp_path / "config.yaml"

    share = load_workbook_shiftable_share(base_cfg, config_path=config_path)
    assert share == pytest.approx(0.4)

    # 8.0 cEUR/kWh == 80 EUR/MWh, so unit conversion must be handled.
    lower = load_workbook_reform_price_index(base_cfg, bound="lower", config_path=config_path)
    upper = load_workbook_reform_price_index(base_cfg, bound="upper", config_path=config_path)
    assert lower == pytest.approx(96.0 / 80.0)
    assert upper == pytest.approx(160.0 / 80.0)


def test_two_stage_reform_with_workbook_inputs_and_default_post_reform_price(
    tmp_path: Path,
):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reform_workbook(workbook)
    cfg = {
        "mode": "two_stage_reform",
        "base_year": 2023,
        "reform_year": 2027,
        "base_demand": _reform_base_cfg(workbook),
        "reform": {
            "shiftable_share": {"source": "workbook"},
            "price_index": {"source": "workbook", "bound": "lower"},
            "price_elasticity": -0.5,
        },
        "income": {"values": {2023: 100.0, 2027: 100.0, 2030: 100.0}},
        # post_reform_price intentionally omitted: no post-2027 price term is applied.
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.0},
        "income_elasticity": 1.0,
    }

    demand = build_dynamic_demand_series(
        cfg,
        [2025, 2027, 2030],
        config_path=tmp_path / "config.yaml",
    )

    expected_reform = 60.0 + 40.0 * (96.0 / 80.0) ** -0.5
    assert demand.loc[2025] == pytest.approx(100.0)
    assert demand.loc[2027] == pytest.approx(expected_reform)
    assert demand.loc[2030] == pytest.approx(expected_reform)


def test_two_stage_reform_population_scales_post_reform_demand(tmp_path: Path):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reform_workbook(workbook)
    cfg = {
        "mode": "two_stage_reform",
        "base_year": 2023,
        "reform_year": 2027,
        "base_demand": _reform_base_cfg(workbook),
        "reform": {
            "shiftable_share": {"source": "workbook"},
            "price_index": {"source": "workbook", "bound": "lower"},
            "price_elasticity": -0.5,
        },
        # Total GDP and population both grow 10% => GDP per capita is flat,
        # so post-reform growth comes from population scaling alone.
        "income": {"values": {2023: 100.0, 2027: 100.0, 2030: 110.0}},
        "population": {"values": {2023: 1.0, 2027: 1.0, 2030: 1.1}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.0},
        "income_elasticity": 1.0,
    }

    demand = build_dynamic_demand_series(
        cfg,
        [2027, 2030],
        config_path=tmp_path / "config.yaml",
    )

    expected_reform = 60.0 + 40.0 * (96.0 / 80.0) ** -0.5
    assert demand.loc[2027] == pytest.approx(expected_reform)
    assert demand.loc[2030] == pytest.approx(expected_reform * 1.1)


def test_two_stage_reform_rejects_post_reform_price_elasticity_without_price_path(
    tmp_path: Path,
):
    workbook = tmp_path / "Electricity_OECD.xlsx"
    _write_reform_workbook(workbook)
    cfg = {
        "mode": "two_stage_reform",
        "base_year": 2023,
        "reform_year": 2027,
        "base_demand": _reform_base_cfg(workbook),
        "reform": {
            "shiftable_share": {"source": "workbook"},
            "price_index": {"source": "workbook", "bound": "lower"},
            "price_elasticity": -0.5,
        },
        "income": {"values": {2023: 100.0, 2027: 100.0, 2030: 100.0}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.0},
        "income_elasticity": 1.0,
        "post_reform_price_elasticity": {"value": -0.4},
    }

    with pytest.raises(
        ValueError,
        match="post_reform_price_elasticity requires a post_reform_price path",
    ):
        build_dynamic_demand_series(
            cfg,
            [2027, 2030],
            config_path=tmp_path / "config.yaml",
        )


def test_two_stage_reform_holds_pre_reform_years_at_workbook_base_and_derives_segments(
    tmp_path: Path,
    reference_workbook_100_twh: Path,
):
    cfg = {
        "mode": "two_stage_reform",
        "base_year": 2023,
        "reform_year": 2027,
        "base_demand": {
            "source": str(reference_workbook_100_twh),
            "country_code": "TST",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        "reform": {
            "shiftable_share": 0.4,
            "price_index": 1.5,
            "price_elasticity": -0.4,
        },
        "income": {"values": {2023: 100.0, 2027: 100.0, 2030: 100.0}},
        "post_reform_price": {"values": {2027: 100.0, 2030: 100.0}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.0},
        "income_elasticity": 1.0,
        "post_reform_price_elasticity": {"value": -0.4},
    }

    demand = build_dynamic_demand_series(
        cfg,
        [2025, 2026, 2027, 2030],
        config_path=tmp_path / "config.yaml",
    )

    expected_reform = 60.0 + 40.0 * 1.5**-0.4
    assert demand.loc[2025] == pytest.approx(100.0)
    assert demand.loc[2026] == pytest.approx(100.0)
    assert demand.loc[2027] == pytest.approx(expected_reform)
    assert demand.loc[2030] == pytest.approx(expected_reform)


def test_two_stage_reform_demand_can_rise_after_2027_with_growth(
    tmp_path: Path,
    reference_workbook_100_twh: Path,
):
    cfg = {
        "mode": "two_stage_reform",
        "base_year": 2023,
        "reform_year": 2027,
        "base_demand": {
            "source": str(reference_workbook_100_twh),
            "country_code": "TST",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        "reform": {
            "residual_demand_twh": 60.0,
            "shiftable_demand_twh": 40.0,
            "price_index": 1.0,
            "price_elasticity": -0.4,
        },
        "income": {"values": {2023: 95.0, 2027: 100.0, 2030: 110.0}},
        "post_reform_price": {"values": {2027: 100.0, 2030: 100.0}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.01},
        "income_elasticity": 1.2,
        "post_reform_price_elasticity": {"value": -0.4},
    }

    demand = build_dynamic_demand_series(
        cfg,
        [2027, 2030],
        config_path=tmp_path / "config.yaml",
    )

    expected_2030 = 100.0 * (0.57 / 0.54) * (110.0 / 100.0) ** 1.2
    assert demand.loc[2027] == pytest.approx(100.0)
    assert demand.loc[2030] == pytest.approx(expected_2030)
    assert demand.loc[2030] > demand.loc[2027]


def test_two_stage_reform_supports_year_indexed_post_reform_price_elasticity(
    tmp_path: Path,
    reference_workbook_100_twh: Path,
):
    cfg = {
        "mode": "two_stage_reform",
        "base_year": 2023,
        "reform_year": 2027,
        "base_demand": {
            "source": str(reference_workbook_100_twh),
            "country_code": "TST",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        "reform": {
            "residual_demand_twh": 60.0,
            "shiftable_demand_twh": 40.0,
            "price_index": 1.0,
            "price_elasticity": -0.4,
        },
        "income": {"values": {2023: 100.0, 2027: 100.0, 2030: 100.0}},
        "post_reform_price": {"values": {2027: 100.0, 2030: 125.0}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.0},
        "income_elasticity": 1.0,
        "post_reform_price_elasticity": {
            "values": {
                2027: -0.6,
                2030: -0.2,
            }
        },
    }

    demand = build_dynamic_demand_series(
        cfg,
        [2027, 2030],
        config_path=tmp_path / "config.yaml",
    )

    assert demand.loc[2027] == pytest.approx(100.0)
    assert demand.loc[2030] == pytest.approx(100.0 * (125.0 / 100.0) ** -0.2)


@pytest.mark.parametrize(
    "bad_update",
    [
        {"reform": {"price_elasticity": -0.19}},
        {"post_reform_price_elasticity": {"values": {2027: -0.2, 2030: -0.81}}},
    ],
)
def test_two_stage_reform_validates_price_elasticity_bounds(
    tmp_path: Path,
    reference_workbook_100_twh: Path,
    bad_update: dict,
):
    cfg = {
        "mode": "two_stage_reform",
        "base_year": 2023,
        "reform_year": 2027,
        "base_demand": {
            "source": str(reference_workbook_100_twh),
            "country_code": "TST",
            "demand_column": "Total electricity demand",
            "year": 2023,
        },
        "reform": {
            "residual_demand_twh": 60.0,
            "shiftable_demand_twh": 40.0,
            "price_index": 1.0,
            "price_elasticity": -0.4,
        },
        "income": {"values": {2023: 100.0, 2027: 100.0, 2030: 100.0}},
        "post_reform_price": {"values": {2027: 100.0, 2030: 100.0}},
        "electrification": {"form": "linear_time", "A": 0.5, "B": 0.0},
        "income_elasticity": 1.0,
        "post_reform_price_elasticity": {"value": -0.4},
    }
    for key, value in bad_update.items():
        if key == "reform" and isinstance(value, dict):
            cfg[key].update(value)
        else:
            cfg[key] = value

    with pytest.raises(ValueError, match="price_elasticity"):
        build_dynamic_demand_series(cfg, [2027, 2030], config_path=tmp_path / "config.yaml")


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
            "country_code": "TST",
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
                            "country_code": "TST",
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


def test_run_from_config_keeps_static_demand_scenarios(
    tmp_path: Path,
    emission_factors_path: Path,
):
    config = {
        "calc_emissions": {
            "emission_factors_file": str(emission_factors_path),
            "demand_scenarios": {
                "base_demand": {"values": {2025: 10.0, 2030: 12.0}},
                "scen1_lower": {"values": {2025: 9.0, 2030: 11.0}},
            },
            "mix_scenarios": {"base_mix": {"shares": {"coal": 1.0}}},
        }
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps(config))

    results = run_from_config(
        config_path,
        default_years={"start": 2025, "end": 2030, "step": 5},
    )

    assert results["base_mix__base_demand"].demand_twh.loc[2030] == pytest.approx(12.0)
    assert results["base_mix__scen1_lower"].demand_twh.loc[2030] == pytest.approx(11.0)
