import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_SCC_PATH = REPO_ROOT / "scripts" / "run_scc.py"

spec = importlib.util.spec_from_file_location("run_scc_module", RUN_SCC_PATH)
run_scc = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(run_scc)  # type: ignore[attr-defined]


def _write_csv(path: Path, data: list[dict]) -> None:
    frame = pd.DataFrame(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def _build_config(tmp_path: Path, include_horizon: bool) -> dict:
    years = [2025, 2030]

    gdp_path = tmp_path / "gdp.csv"
    _write_csv(
        gdp_path,
        [
            {"year": year, "gdp_trillion_usd": 1.0 + 0.1 * idx, "population_million": 5.0}
            for idx, year in enumerate(years)
        ],
    )

    emission_root = tmp_path / "results" / "emissions" / "All_countries"
    temperature_root = tmp_path / "results" / "climate"
    _write_csv(
        emission_root / "base_mix" / "co2.csv",
        [
            {
                "year": year,
                "absolute_base_demand": 0.0,
                "absolute_policy": 0.0 if idx == 0 else -5.0,
                "delta_base_demand": 0.0,
                "delta_policy": 0.0 if idx == 0 else -5.0,
            }
            for idx, year in enumerate(years)
        ],
    )

    _write_csv(
        temperature_root / "base_mix__base_demand_ssp245.csv",
        [
            {
                "year": year,
                "temperature_adjusted": 3.0 + 0.1 * idx,
                "temperature_baseline": 3.0 + 0.1 * idx,
                "climate_scenario": "ssp245",
            }
            for idx, year in enumerate(years)
        ],
    )
    _write_csv(
        temperature_root / "base_mix__policy_ssp245.csv",
        [
            {
                "year": year,
                "temperature_adjusted": 2.8 + 0.1 * idx,
                "temperature_baseline": 3.0 + 0.1 * idx,
                "climate_scenario": "ssp245",
            }
            for idx, year in enumerate(years)
        ],
    )

    output_dir = tmp_path / "results" / "economic"

    economic_cfg = {
        "gdp_series": str(gdp_path),
        "reference_scenario": "base_mix__base_demand",
        "evaluation_scenarios": ["base_mix__policy"],
        "base_year": 2025,
        "aggregation": "average",
        "run": {
            "pulse": {},
        },
        "methods": {"run": "ramsey_discount"},
        "output_directory": str(output_dir),
        "data_sources": {
            "emission_root": str(emission_root),
            "temperature_root": str(temperature_root),
            "climate_scenarios": ["ssp245"],
        },
    }

    if include_horizon:
        economic_cfg["aggregation_horizon"] = {"start": 2025, "end": 2030}

    config = {
        "time_horizon": {"start": 2025, "end": 2030, "step": 5},
        "economic_module": economic_cfg,
        "calc_emissions": {
            "countries": {
                "baseline_demand_case": "base_demand",
                "demand_scenarios": ["base_demand", "policy"],
                "mix_scenarios": ["base_mix"],
                "aggregate_output_directory": str(emission_root),
                "resources_root": str(tmp_path / "results" / "emissions"),
            }
        },
    }
    return config


@pytest.mark.network
@pytest.mark.parametrize("include_horizon", [False, True])
def test_run_scc_aggregation_horizon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, include_horizon: bool, capsys
):
    config = _build_config(tmp_path, include_horizon=include_horizon)

    monkeypatch.setattr(run_scc, "_load_config", lambda: config)
    monkeypatch.setattr(sys, "argv", ["run_scc.py"])

    if not include_horizon:
        with pytest.raises(SystemExit):
            run_scc.main()
        captured = capsys.readouterr()
        assert "aggregation" in captured.err.lower()
    else:
        run_scc.main()
        output_dir = Path(config["economic_module"]["output_directory"])
        timeseries = output_dir / "pulse_scc_timeseries_ramsey_discount_ssp245.csv"
        assert timeseries.exists()
        df = pd.read_csv(timeseries)
        assert set(df["year"].astype(int)) == set(range(2025, 2031))


@pytest.mark.network
def test_run_scc_pulse_outputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = _build_config(tmp_path, include_horizon=True)
    config["economic_module"]["methods"] = {
        "run": "ramsey_discount",
        "ramsey_discount": {"rho": 0.01, "eta": 1.0},
    }

    monkeypatch.setattr(run_scc, "_load_config", lambda: config)
    monkeypatch.setattr(sys, "argv", ["run_scc.py", "--discount-methods", "ramsey_discount"])

    run_scc.main()
    output_dir = Path(config["economic_module"]["output_directory"])
    mix_case = "base_mix"
    scenario_label = "base_mix__policy_ssp245"
    base = "ramsey_discount_ssp245"

    pulse_scc = output_dir / f"pulse_scc_timeseries_{base}.csv"
    pulse_damage = output_dir / mix_case / f"damages_ramsey_discount_{scenario_label}.csv"
    assert pulse_scc.exists()
    assert pulse_damage.exists()

    pulse_df = pd.read_csv(pulse_scc)
    assert set(pulse_df.columns) == {
        "year",
        "discount_factor",
        "pv_damage_per_pulse_usd",
        "scc_usd_per_tco2",
        "pulse_size_tco2",
        "delta_emissions_tco2",
        "discounted_delta_usd",
    }
    assert set(pulse_df["year"].astype(int)) == set(range(2025, 2031))

    damage_df = pd.read_csv(pulse_damage)
    assert set(damage_df.columns) >= {
        "year",
        "delta_emissions_mtco2",
        "delta_emissions_tco2",
        "scc_usd_per_tco2",
        "discount_factor",
        "damage_usd",
        "discounted_damage_usd",
    }
    assert set(damage_df["year"].astype(int)) == set(range(2025, 2031))


@pytest.mark.network
def test_run_scc_skip_damages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = _build_config(tmp_path, include_horizon=True)
    monkeypatch.setattr(run_scc, "_load_config", lambda: config)
    monkeypatch.setattr(sys, "argv", ["run_scc.py", "--skip-damages"])

    run_scc.main()
    output_dir = Path(config["economic_module"]["output_directory"])
    pulse_scc = output_dir / "pulse_scc_timeseries_ramsey_discount_ssp245.csv"
    damage_dir = output_dir / "base_mix"
    assert pulse_scc.exists()
    assert not any(damage_dir.glob("damages_*.csv"))


@pytest.mark.network
def test_run_scc_custom_damage_requires_terms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    config = _build_config(tmp_path, include_horizon=True)
    config["economic_module"]["damage_function"] = {"mode": "custom"}

    monkeypatch.setattr(run_scc, "_load_config", lambda: config)
    monkeypatch.setattr(sys, "argv", ["run_scc.py"])

    with pytest.raises(SystemExit):
        run_scc.main()
    assert "custom_terms" in capsys.readouterr().err.lower()


@pytest.mark.network
def test_run_scc_custom_damage_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = _build_config(tmp_path, include_horizon=True)
    config["economic_module"]["damage_function"] = {
        "mode": "custom",
        "custom_terms": [{"coefficient": 0.01, "exponent": 1.0}],
    }

    monkeypatch.setattr(run_scc, "_load_config", lambda: config)
    monkeypatch.setattr(sys, "argv", ["run_scc.py"])

    run_scc.main()
    output_dir = Path(config["economic_module"]["output_directory"])
    pulse_scc = output_dir / "pulse_scc_timeseries_ramsey_discount_ssp245.csv"
    assert pulse_scc.exists()


def test_dice_projection_can_follow_climate_scenario(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, str] = {}

    class DummyDice:
        @classmethod
        def from_config(cls, cfg, base_path):
            captured["scenario"] = cfg["scenario"]

            class _Model:
                def project(self, end_year: int) -> pd.DataFrame:
                    years = np.arange(2019, end_year + 1)
                    return pd.DataFrame(
                        {
                            "year": years,
                            "population_million": np.ones_like(years, dtype=float),
                            "population_persons": np.ones_like(years, dtype=float) * 1e6,
                            "gdp_trillion_usd": np.ones_like(years, dtype=float),
                            "gdp_per_capita_usd": np.ones_like(years, dtype=float),
                        }
                    )

            return _Model()

    monkeypatch.setattr(run_scc, "DiceSocioeconomics", DummyDice)

    root_cfg = {
        "socioeconomics": {
            "mode": "dice",
            "dice": {
                "scenario": "as_climate_scenario",
            },
        }
    }

    frame = run_scc._build_socioeconomic_projection(
        root_cfg,
        end_year=2020,
        climate_label="ssp370",
    )
    assert captured["scenario"] == "SSP3"
    assert frame.iloc[-1]["year"] == 2020


def test_run_scc_scenario_suite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = _build_config(tmp_path, include_horizon=True)
    scenario_file = tmp_path / "suite.yaml"
    scenario_file.write_text(
        "\n".join(
            [
                "default_case: {}",
                "alt_case:",
                "  run:",
                "    output_subdir: alt",
            ]
        )
    )
    config["run"] = {"mode": "scenarios", "scenario_file": str(scenario_file)}
    config_path = tmp_path / "config.yaml"
    config_path.write_text("run: scenarios\n")

    captured: list[str | None] = []

    def _fake_run(cfg: dict, argv: list[str]) -> None:
        results_cfg = cfg.get("results", {}) if isinstance(cfg, dict) else {}
        captured.append(results_cfg.get("run_directory"))

    monkeypatch.setattr(run_scc, "_run_single_configuration", _fake_run)
    monkeypatch.setattr(run_scc, "_load_config", lambda: config)
    monkeypatch.setattr(run_scc, "get_config_path", lambda default: config_path)
    monkeypatch.setattr(sys, "argv", ["run_scc.py"])

    run_scc.main()

    assert captured == ["suite/default_case", "suite/alt"]
