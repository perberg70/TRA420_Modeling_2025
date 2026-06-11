"""Air-pollution health impact calculator."""

from .impact import (
    AirPollutionResult,
    HealthCostAssumptions,
    PollutantImpact,
    run_from_config,
)

__all__ = [
    "AirPollutionResult",
    "HealthCostAssumptions",
    "PollutantImpact",
    "run_from_config",
]
