import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from air_pollution import run_from_config as run_air_pollution
from calc_emissions import EmissionScenarioResult


def test_air_pollution_run_from_config_creates_outputs(tmp_path: Path):
    root = tmp_path
    (root / "resources").mkdir()
    (root / "results").mkdir()

    emission_factors = pd.DataFrame(
        {
            "technology": ["coal", "solar"],
            "co2_mt_per_twh": [1.0, 0.0],
            "so2_kt_per_twh": [2.0, 0.0],
            "nox_kt_per_twh": [1.0, 0.0],
            "pm25_kt_per_twh": [0.5, 0.0],
        }
    )
    factors_path = root / "factors.csv"
    emission_factors.to_csv(factors_path, index=False)

    pollution_stats = pd.DataFrame(
        {
            "country": ["Testland_A", "Testland_B"],
            "count": [1, 1],
            "mean": [10.0, 30.0],
            "median": [10.0, 30.0],
            "std": [None, None],
            "min": [10.0, 30.0],
            "max": [10.0, 30.0],
        }
    )
    stats_path = root / "pm25.csv"
    pollution_stats.to_csv(stats_path, index=False)

    config = {
        "calc_emissions": {
            "emission_factors_file": str(factors_path),
            "output_directory": str(root / "resources"),
            "results_directory": str(root / "results"),
            "years": {"start": 2020, "end": 2025, "step": 5},
            "demand_scenarios": {},
            "mix_scenarios": {},
            "baseline": {
                "demand_custom": {2020: 100.0, 2025: 110.0},
                "mix_custom": {"shares": {"coal": 0.7, "solar": 0.3}},
            },
            "scenarios": [
                {
                    "name": "policy",
                    "demand_custom": {2020: 80.0, 2025: 90.0},
                    "mix_custom": {"shares": {"coal": 0.2, "solar": 0.8}},
                }
            ],
        },
        "air_pollution": {
            "output_directory": str(root / "results" / "air_pollution"),
            "electricity_share": 1.0,
            "country_weights": {"Testland_A": 3.0, "Testland_B": 1.0},
            "pollutants": {
                "pm25": {
                    "stats_file": str(stats_path),
                    "relative_risk": 1.08,
                    "reference_delta": 10.0,
                },
                "nox": {
                    "stats_file": str(stats_path),
                    "relative_risk": 1.03,
                    "reference_delta": 10.0,
                },
            },
            "baseline_deaths": {"per_year": 7000.0},
        },
    }

    config_path = root / "config.yaml"
    config_path.write_text(json.dumps(config))

    results = run_air_pollution(config_path=config_path)

    assert "policy" in results
    output_file = (
        Path(config["air_pollution"]["output_directory"]) / "policy" / "pm25_health_impact.csv"
    )
    assert output_file.exists()

    df = pd.read_csv(output_file)
    expected_columns = {
        "country",
        "year",
        "baseline_concentration",
        "emission_ratio",
        "delta_fraction",
        "new_concentration",
        "delta_concentration",
        "percent_change_mortality",
    }
    assert set(df.columns) == expected_columns

    beta = math.log(1.08) / 10.0
    ratio_2020 = 0.008 / 0.035
    ratio_2025 = 0.009 / 0.0385
    baseline_concentrations = {"Testland_A": 10.0, "Testland_B": 30.0}
    country_weights = {"Testland_A": 3.0 / 4.0, "Testland_B": 1.0 / 4.0}

    def expected_pct_change(concentration: float, ratio: float, beta_value: float) -> float:
        delta_conc = concentration * (ratio - 1.0)
        return math.exp(beta_value * delta_conc) - 1.0

    expected_pm25_2020_per_country = {
        country: expected_pct_change(conc, ratio_2020, beta)
        for country, conc in baseline_concentrations.items()
    }
    expected_pm25_2025_per_country = {
        country: expected_pct_change(conc, ratio_2025, beta)
        for country, conc in baseline_concentrations.items()
    }

    for country, _conc in baseline_concentrations.items():
        country_series = (
            df.loc[df["country"] == country].set_index("year")["percent_change_mortality"].to_dict()
        )
        assert math.isclose(
            country_series[2020], expected_pm25_2020_per_country[country], rel_tol=1e-6
        )
        assert math.isclose(
            country_series[2025], expected_pm25_2025_per_country[country], rel_tol=1e-6
        )

    expected_weighted_pm25_2020 = sum(
        country_weights[country] * value
        for country, value in expected_pm25_2020_per_country.items()
    )
    expected_weighted_pm25_2025 = sum(
        country_weights[country] * value
        for country, value in expected_pm25_2025_per_country.items()
    )

    summary_file = (
        Path(config["air_pollution"]["output_directory"]) / "policy" / "pm25_mortality_summary.csv"
    )
    assert not summary_file.exists()

    impact_pm25 = results["policy"].pollutant_results["pm25"]
    pm25_weights_dict = impact_pm25.country_weights.to_dict()
    for country, weight in country_weights.items():
        assert math.isclose(pm25_weights_dict[country], weight, rel_tol=1e-6)
    assert math.isclose(
        impact_pm25.weighted_percent_change.loc[2020], expected_weighted_pm25_2020, rel_tol=1e-6
    )
    assert math.isclose(
        impact_pm25.weighted_percent_change.loc[2025], expected_weighted_pm25_2025, rel_tol=1e-6
    )

    beta_nox = math.log(1.03) / 10.0
    expected_nox_2020_per_country = {
        country: expected_pct_change(conc, ratio_2020, beta_nox)
        for country, conc in baseline_concentrations.items()
    }
    expected_nox_2025_per_country = {
        country: expected_pct_change(conc, ratio_2025, beta_nox)
        for country, conc in baseline_concentrations.items()
    }
    expected_weighted_nox_2020 = sum(
        country_weights[country] * value for country, value in expected_nox_2020_per_country.items()
    )
    expected_weighted_nox_2025 = sum(
        country_weights[country] * value for country, value in expected_nox_2025_per_country.items()
    )

    impact_nox = results["policy"].pollutant_results["nox"]
    assert math.isclose(
        impact_nox.weighted_percent_change.loc[2020], expected_weighted_nox_2020, rel_tol=1e-6
    )
    assert math.isclose(
        impact_nox.weighted_percent_change.loc[2025], expected_weighted_nox_2025, rel_tol=1e-6
    )

    total_file = (
        Path(config["air_pollution"]["output_directory"]) / "policy" / "total_mortality_summary.csv"
    )
    assert total_file.exists()
    total_df = pd.read_csv(total_file)
    expected_percent_change_2020 = (expected_weighted_pm25_2020 + expected_weighted_nox_2020) / 2.0
    expected_percent_change_2025 = (expected_weighted_pm25_2025 + expected_weighted_nox_2025) / 2.0
    expected_total_2020 = 7000.0 * expected_percent_change_2020
    expected_total_2025 = 7000.0 * expected_percent_change_2025
    total_delta_by_year = dict(
        zip(total_df["year"], total_df["delta_deaths_per_year"], strict=False)
    )
    assert math.isclose(total_delta_by_year[2020], expected_total_2020, rel_tol=1e-6)
    assert math.isclose(total_delta_by_year[2025], expected_total_2025, rel_tol=1e-6)
    baseline_by_year = dict(
        zip(total_df["year"], total_df["baseline_deaths_per_year"], strict=False)
    )
    assert baseline_by_year[2020] == pytest.approx(7000.0)


def test_air_pollution_with_baseline_deaths_and_vsl(tmp_path: Path):
    root = tmp_path
    (root / "resources").mkdir()
    (root / "results").mkdir()

    emission_factors = pd.DataFrame(
        {
            "technology": ["coal"],
            "co2_mt_per_twh": [1.0],
            "so2_kt_per_twh": [1.0],
            "nox_kt_per_twh": [1.0],
            "pm25_kt_per_twh": [1.0],
        }
    )
    factors_path = root / "factors.csv"
    emission_factors.to_csv(factors_path, index=False)

    stats = pd.DataFrame(
        {
            "country": ["A", "B"],
            "median": [1.0, 2.0],
            "baseline_deaths_per_year": [100.0, 300.0],
        }
    )
    stats_path = root / "stats.csv"
    stats.to_csv(stats_path, index=False)

    config = {
        "calc_emissions": {
            "emission_factors_file": str(factors_path),
            "output_directory": str(root / "resources"),
            "results_directory": str(root / "results"),
            "years": {"start": 2020, "end": 2025, "step": 5},
            "demand_scenarios": {},
            "mix_scenarios": {},
            "baseline": {
                "demand_custom": {2020: 100.0, 2025: 100.0},
                "mix_custom": {"shares": {"coal": 1.0}},
            },
            "scenarios": [
                {
                    "name": "policy",
                    "demand_custom": {2020: 50.0, 2025: 50.0},
                    "mix_custom": {"shares": {"coal": 1.0}},
                }
            ],
        },
        "air_pollution": {
            "output_directory": str(root / "results" / "air_pollution"),
            "electricity_share": 1.0,
            "value_of_statistical_life_usd": 1_000_000.0,
            "pollutants": {
                "pm25": {"stats_file": str(stats_path)},
                "nox": {
                    "stats_file": str(stats_path),
                    "relative_risk": 1.03,
                    "reference_delta": 10,
                },
            },
        },
    }
    config_path = root / "config.yaml"
    config_path.write_text(json.dumps(config))

    results = run_air_pollution(config_path=config_path)
    pm25_summary_path = (
        Path(config["air_pollution"]["output_directory"]) / "policy" / "pm25_mortality_summary.csv"
    )
    assert pm25_summary_path.exists()
    pm25_summary = pd.read_csv(pm25_summary_path)
    assert "delta_value_usd" in pm25_summary.columns
    assert math.isclose(
        pm25_summary["delta_value_usd"].iloc[0],
        pm25_summary["delta_deaths_per_year"].iloc[0] * 1_000_000.0,
    )
    impact_pm25 = results["policy"].pollutant_results["pm25"]
    expected_weights = {"A": 0.25, "B": 0.75}
    for country, weight in expected_weights.items():
        assert math.isclose(impact_pm25.country_weights[country], weight, rel_tol=1e-6)

    total_path = (
        Path(config["air_pollution"]["output_directory"]) / "policy" / "total_mortality_summary.csv"
    )
    assert total_path.exists()
    total_summary = pd.read_csv(total_path)
    assert "delta_value_usd" in total_summary.columns
    assert math.isclose(
        total_summary["delta_value_usd"].iloc[0],
        total_summary["delta_deaths_per_year"].iloc[0] * 1_000_000.0,
    )
    assert math.isclose(total_summary["baseline_deaths_per_year"].iloc[0], 800.0)

    assert results["policy"].total_mortality_summary is not None


def _make_emission_result(
    name: str,
    demand_case: str,
    mix_case: str,
    years: list[int],
    pm25_values: list[float],
    electrification: dict[int, float] | None = None,
) -> EmissionScenarioResult:
    index = pd.Index(years)
    pm25 = pd.Series(pm25_values, index=index, dtype=float)
    demand = pd.Series(100.0, index=index, dtype=float)
    elec = (
        pd.Series(electrification, dtype=float).sort_index()
        if electrification is not None
        else None
    )
    return EmissionScenarioResult(
        name=name,
        demand_case=demand_case,
        mix_case=mix_case,
        years=years,
        demand_twh=demand,
        generation_twh=pd.DataFrame({"coal": demand}),
        technology_emissions_mt={"pm25": pd.DataFrame({"coal": pm25})},
        total_emissions_mt={"pm25": pm25, "co2": pm25.copy()},
        delta_mtco2=pd.Series(np.zeros(len(index)), index=index, dtype=float),
        electrification=elec,
    )


def test_air_pollution_indoor_module_and_health_costs(tmp_path: Path):
    root = tmp_path

    stats = pd.DataFrame(
        {
            "country": ["Albania"],
            "median": [10.0],
            "baseline_deaths_per_year": [1000.0],
        }
    )
    stats_path = root / "stats.csv"
    stats.to_csv(stats_path, index=False)

    indoor_stats = pd.DataFrame(
        {
            "country": ["Albania"],
            "baseline_deaths_per_year": [1000.0],
            "base_electrification": [0.5],
        }
    )
    indoor_path = root / "indoor.csv"
    indoor_stats.to_csv(indoor_path, index=False)

    output_dir = root / "air_pollution"
    config = {
        "calc_emissions": {},
        "air_pollution": {
            "output_directory": str(output_dir),
            "electricity_share": 1.0,
            "value_of_statistical_life_usd": 1_000_000.0,
            "pollutants": {
                "pm25": {
                    "stats_file": str(stats_path),
                    "relative_risk": 1.08,
                    "reference_delta": 10.0,
                }
            },
            "indoor": {"stats_file": str(indoor_path)},
            "health_costs": {
                "healthcare_cost_usd_per_death": 10_000.0,
                "income_loss_usd_per_death": 20_000.0,
            },
        },
    }
    config_path = root / "config.yaml"
    config_path.write_text(json.dumps(config))

    years = [2030, 2040]
    emission_results = {
        "base_mix__base_demand": _make_emission_result(
            "base_mix__base_demand", "base_demand", "base_mix", years, [1.0, 1.0]
        ),
        "base_mix__policy": _make_emission_result(
            "base_mix__policy",
            "policy",
            "base_mix",
            years,
            [0.5, 0.5],
            electrification={2030: 0.5, 2040: 0.75},
        ),
    }

    results = run_air_pollution(config_path=config_path, emission_results=emission_results)
    policy = results["base_mix__policy"]

    # Indoor deaths scale with the non-electrified share: (1 - e_t) / (1 - 0.5).
    assert policy.indoor_summary is not None
    indoor = policy.indoor_summary.set_index("year")
    assert indoor.loc[2030, "indoor_deaths_per_year"] == pytest.approx(1000.0)
    assert indoor.loc[2030, "delta_indoor_deaths_per_year"] == pytest.approx(0.0)
    assert indoor.loc[2040, "indoor_deaths_per_year"] == pytest.approx(500.0)
    assert indoor.loc[2040, "delta_indoor_deaths_per_year"] == pytest.approx(-500.0)
    assert indoor.loc[2040, "delta_value_usd"] == pytest.approx(-500.0 * 1_000_000.0)
    assert indoor.loc[2040, "delta_healthcare_cost_usd"] == pytest.approx(-500.0 * 10_000.0)
    assert indoor.loc[2040, "delta_income_loss_usd"] == pytest.approx(-500.0 * 20_000.0)
    assert indoor.loc[2040, "delta_total_cost_usd"] == pytest.approx(
        -500.0 * (1_000_000.0 + 10_000.0 + 20_000.0)
    )

    # Ambient mortality from the log-linear model with a halved emission ratio.
    beta = math.log(1.08) / 10.0
    pct_change = math.exp(beta * 10.0 * -0.5) - 1.0
    ambient_delta = 1000.0 * pct_change

    assert policy.total_mortality_summary is not None
    total = policy.total_mortality_summary.set_index("year")
    assert total.loc[2040, "delta_deaths_per_year"] == pytest.approx(ambient_delta)
    assert total.loc[2040, "delta_indoor_deaths_per_year"] == pytest.approx(-500.0)
    assert total.loc[2040, "delta_deaths_total_per_year"] == pytest.approx(ambient_delta - 500.0)

    # Combined monetised summary covers ambient + indoor deaths.
    assert policy.health_cost_summary is not None
    costs = policy.health_cost_summary.set_index("year")
    combined_2040 = ambient_delta - 500.0
    assert costs.loc[2040, "ambient_delta_deaths_per_year"] == pytest.approx(ambient_delta)
    assert costs.loc[2040, "indoor_delta_deaths_per_year"] == pytest.approx(-500.0)
    assert costs.loc[2040, "total_delta_deaths_per_year"] == pytest.approx(combined_2040)
    assert costs.loc[2040, "delta_value_usd"] == pytest.approx(combined_2040 * 1_000_000.0)
    assert costs.loc[2040, "delta_healthcare_cost_usd"] == pytest.approx(combined_2040 * 10_000.0)
    assert costs.loc[2040, "delta_income_loss_usd"] == pytest.approx(combined_2040 * 20_000.0)
    assert costs.loc[2040, "delta_total_cost_usd"] == pytest.approx(
        combined_2040 * (1_000_000.0 + 10_000.0 + 20_000.0)
    )

    # Per-pollutant mortality summaries also carry the new cost columns.
    pm25_summary = policy.pollutant_results["pm25"].deaths_summary
    assert pm25_summary is not None
    assert "delta_healthcare_cost_usd" in pm25_summary.columns
    assert "delta_total_cost_usd" in pm25_summary.columns

    scenario_dir = output_dir / "base_mix__policy"
    assert (scenario_dir / "indoor_health_impact.csv").exists()
    assert (scenario_dir / "indoor_mortality_summary.csv").exists()
    assert (scenario_dir / "health_cost_summary.csv").exists()

    impacts = pd.read_csv(scenario_dir / "indoor_health_impact.csv")
    assert set(impacts["country"]) == {"Albania"}
    row_2040 = impacts.loc[impacts["year"] == 2040].iloc[0]
    assert row_2040["electrification_share"] == pytest.approx(0.75)


def test_air_pollution_indoor_skipped_for_static_scenarios(tmp_path: Path):
    root = tmp_path

    stats = pd.DataFrame(
        {
            "country": ["Albania"],
            "median": [10.0],
            "baseline_deaths_per_year": [1000.0],
        }
    )
    stats_path = root / "stats.csv"
    stats.to_csv(stats_path, index=False)

    indoor_stats = pd.DataFrame(
        {
            "country": ["Albania"],
            "baseline_deaths_per_year": [1000.0],
            "base_electrification": [0.5],
        }
    )
    indoor_path = root / "indoor.csv"
    indoor_stats.to_csv(indoor_path, index=False)

    config = {
        "calc_emissions": {},
        "air_pollution": {
            "output_directory": str(root / "air_pollution"),
            "electricity_share": 1.0,
            "pollutants": {
                "pm25": {
                    "stats_file": str(stats_path),
                    "relative_risk": 1.08,
                    "reference_delta": 10.0,
                }
            },
            "indoor": {"stats_file": str(indoor_path)},
        },
    }
    config_path = root / "config.yaml"
    config_path.write_text(json.dumps(config))

    years = [2030, 2040]
    emission_results = {
        "base_mix__base_demand": _make_emission_result(
            "base_mix__base_demand", "base_demand", "base_mix", years, [1.0, 1.0]
        ),
        "base_mix__policy": _make_emission_result(
            "base_mix__policy", "policy", "base_mix", years, [0.5, 0.5]
        ),
    }

    results = run_air_pollution(config_path=config_path, emission_results=emission_results)
    policy = results["base_mix__policy"]

    # Without any electrification path the indoor module produces no output.
    assert policy.indoor_summary is None
    assert policy.indoor_impacts is None
    scenario_dir = root / "air_pollution" / "base_mix__policy"
    assert not (scenario_dir / "indoor_health_impact.csv").exists()
