"""Watchdog that catches silent failures of dump1090 / readsb ADS-B decoders."""

from __future__ import annotations

from dump1090_healthcheck.checks import CheckConfig, Observation, RateTracker
from dump1090_healthcheck.state import (
    CheckResult,
    Evaluation,
    HealthStateMachine,
    RestartController,
)

__all__ = [
    "CheckConfig",
    "CheckResult",
    "Evaluation",
    "HealthStateMachine",
    "Observation",
    "RateTracker",
    "RestartController",
    "__version__",
]

__version__ = "0.1.0"
