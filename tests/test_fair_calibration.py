from pathlib import Path

import numpy as np
import pytest

try:  # pragma: no cover - optional dependency guard
    from fair import FAIR

    from climate_module.calibration import apply_fair_calibration, load_fair_calibration
    from climate_module.FaIR import DEFAULT_SPECIES, _species_properties
except ImportError:  # pragma: no cover - skip when FaIR is unavailable
    pytest.skip("FaIR dependency not available.", allow_module_level=True)


def _build_model() -> FAIR:
    model = FAIR()
    model.define_time(1750, 1760, 1.0)
    model.define_scenarios(["ssp245"])
    model.define_configs(["baseline", "adjusted"])
    model.define_species(list(DEFAULT_SPECIES), _species_properties(DEFAULT_SPECIES))
    model.allocate()
    model.fill_species_configs()
    model.fill_from_rcmip()
    return model


@pytest.mark.network
def test_load_and_apply_calibration(tmp_path: Path):
    cfg = {
        "enabled": True,
        "base_path": "data/FaIR_calibration_data/v1.5.0",
        "ensemble_file": "calibrated_constrained_parameters.csv",
        "species_file": "species_configs_properties.csv",
        "ch4_lifetime_file": "CH4_lifetime.csv",
        "historical_emissions_file": "historical_emissions_1750-2023_cmip7.csv",
        "solar_forcing_file": "solar_forcing_timebounds.csv",
        "volcanic_forcing_file": "volcanic_forcing_timebounds.csv",
        "ensemble_member_id": 1299,
        "co2_species_name": "CO2",
        "ch4_lifetime_label": "historical_best",
    }
    calibration = load_fair_calibration(cfg, repo_root=Path.cwd())
    assert calibration is not None

    model = _build_model()
    before = float(model.climate_configs["forcing_4co2"].sel(config="baseline"))
    apply_fair_calibration(model, calibration, "ssp245")
    after = float(model.climate_configs["forcing_4co2"].sel(config="baseline"))
    assert after == pytest.approx(float(calibration.parameters["forcing_4co2"]))
    assert after != before

    selection = {"specie": "CO2 FFI", "scenario": "ssp245", "config": "baseline"}
    first_year = int(np.floor(float(model.timepoints[0])))
    hist_row = calibration.historical_emissions[
        calibration.historical_emissions["variable"] == "CO2 FFI"
    ].iloc[0]
    hist_value = float(hist_row[str(first_year)])
    updated = float(model.emissions.loc[selection].sel(timepoints=model.timepoints[0]).values)
    assert updated == pytest.approx(hist_value)

    solar_first = float(
        model.forcing.loc[{"scenario": "ssp245", "config": "baseline", "specie": "Solar"}]
        .isel(timebounds=0)
        .values
    )
    expected_solar = float(calibration.solar_forcing.iloc[0, 1])
    assert solar_first == pytest.approx(expected_solar)
