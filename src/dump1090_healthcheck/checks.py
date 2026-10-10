"""Pure check evaluations for the dump1090 healthcheck.

Every function here is a pure transform from an :class:`Observation` (what
the collector gathered this poll) plus a :class:`CheckConfig` to a
:class:`~dump1090_healthcheck.state.CheckResult`. No I/O, no clock — which is
why the whole check matrix can be exercised from tests without a decoder.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from dump1090_healthcheck.state import CheckResult

DEFAULT_URL = "http://localhost:8080/data/aircraft.json"


@dataclass(frozen=True, slots=True)
class CheckConfig:
    """Thresholds and endpoints for one healthcheck instance.

    Defaults are deliberately generous: they flag a *dead* decoder, not a
    quiet antenna on a Tuesday night.
    """

    url: str = DEFAULT_URL
    timeout: float = 5.0
    max_now_age: float = 15.0
    min_rate: float = 1.0
    max_rate: float = 5000.0
    min_aircraft: int = 0
    max_aircraft: int = 500
    max_blocks_dropped_per_min: float = 120.0
    data_dir: str = ""
    min_disk_free_bytes: int = 100 * 1024 * 1024
    tcp_endpoints: tuple[tuple[str, int], ...] = ()
    enter_after: int = 1
    recover_after: int = 2
    rate_window: float = 60.0
    rate_min_span: float = 5.0


@dataclass(frozen=True, slots=True)
class Observation:
    """Raw material gathered during one poll.

    ``tcp`` maps ``"host:port"`` to an error message, or to ``None`` when the
    probe connected successfully.
    """

    ts: float
    payload: Mapping[str, Any] | None
    payload_error: str
    stats: Mapping[str, Any] | None
    stats_error: str
    tcp: Mapping[str, str | None]
    disk_free: int | None
    disk_total: int | None
    disk_error: str


class RateTracker:
    """Rate (units/second) of a monotonically increasing counter.

    The rate is only reported once ``min_span`` seconds separate the oldest
    and newest sample, so the first poll never produces a wild number. A
    counter that goes *backwards* means the decoder restarted (or wrapped):
    the history is dropped and a fresh window starts.
    """

    def __init__(self, *, window: float = 60.0, min_span: float = 5.0) -> None:
        if window <= 0 or min_span <= 0:
            raise ValueError("window and min_span must be positive")
        self._window = window
        self._min_span = min_span
        self._samples: deque[tuple[float, float]] = deque()

    def sample(self, value: float, ts: float) -> float | None:
        """Add a counter reading and return the current rate, or ``None``."""
        if self._samples and value < self._samples[-1][1]:
            self._samples.clear()
        self._samples.append((ts, value))
        # Keep an anchor sample just outside the window plus everything newer.
        while len(self._samples) > 2 and self._samples[1][0] <= ts - self._window:
            self._samples.popleft()
        first_ts, first_value = self._samples[0]
        last_ts, last_value = self._samples[-1]
        span = last_ts - first_ts
        if span < self._min_span:
            return None
        return (last_value - first_value) / span

    def reset(self) -> None:
        """Forget all samples."""
        self._samples.clear()


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------


def check_reachable(obs: Observation) -> CheckResult:
    """HTTP reachability of ``aircraft.json``."""
    if obs.payload is None:
        return CheckResult("reachable", "failed", obs.payload_error or "endpoint unreachable")
    return CheckResult("reachable", "healthy", "HTTP reachable")


def check_freshness(obs: Observation, cfg: CheckConfig) -> CheckResult:
    """Age of the decoder's ``now`` field — a frozen ``now`` means a hung decoder."""
    if obs.payload is None:
        return CheckResult("freshness", "healthy", "skipped: no data")
    now = obs.payload.get("now")
    if not isinstance(now, (int, float)) or isinstance(now, bool):
        return CheckResult("freshness", "failed", "aircraft.json has no numeric 'now' field")
    age = obs.ts - float(now)
    if age > cfg.max_now_age:
        return CheckResult(
            "freshness",
            "failed",
            f"data is {age:.1f}s old (max {cfg.max_now_age:.0f}s)",
            value=age,
            unit="s",
            metric="now_age_seconds",
        )
    return CheckResult(
        "freshness",
        "healthy",
        f"{age:.1f}s old",
        value=age,
        unit="s",
        metric="now_age_seconds",
    )


def check_rate(obs: Observation, rate: float | None, cfg: CheckConfig) -> CheckResult:
    """Message rate within the configured band (samples come from the runner)."""
    if obs.payload is None:
        return CheckResult("rate", "healthy", "skipped: no data")
    if not isinstance(obs.payload.get("messages"), (int, float)):
        return CheckResult("rate", "healthy", "skipped: no messages counter")
    if rate is None:
        return CheckResult("rate", "healthy", "collecting rate baseline")
    if rate < cfg.min_rate:
        return CheckResult(
            "rate",
            "degraded",
            f"message rate {rate:.1f}/s below minimum {cfg.min_rate:.1f}/s",
            value=rate,
            unit="msg/s",
            metric="messages_per_second",
        )
    if rate > cfg.max_rate:
        return CheckResult(
            "rate",
            "degraded",
            f"message rate {rate:.1f}/s above maximum {cfg.max_rate:.1f}/s",
            value=rate,
            unit="msg/s",
            metric="messages_per_second",
        )
    return CheckResult(
        "rate",
        "healthy",
        f"{rate:.1f} msg/s",
        value=rate,
        unit="msg/s",
        metric="messages_per_second",
    )


def check_aircraft(obs: Observation, cfg: CheckConfig) -> CheckResult:
    """Sanity bounds on the number of aircraft in ``aircraft.json``."""
    if obs.payload is None:
        return CheckResult("aircraft", "healthy", "skipped: no data")
    aircraft = obs.payload.get("aircraft")
    if not isinstance(aircraft, list):
        return CheckResult("aircraft", "degraded", "aircraft.json has no aircraft list")
    count = len(aircraft)
    if count < cfg.min_aircraft:
        return CheckResult(
            "aircraft",
            "degraded",
            f"{count} aircraft below minimum {cfg.min_aircraft}",
            value=float(count),
            metric="aircraft",
        )
    if count > cfg.max_aircraft:
        return CheckResult(
            "aircraft",
            "degraded",
            f"{count} aircraft above maximum {cfg.max_aircraft}",
            value=float(count),
            metric="aircraft",
        )
    return CheckResult(
        "aircraft", "healthy", f"{count} aircraft", value=float(count), metric="aircraft"
    )


def extract_blocks_dropped(stats: Mapping[str, Any] | None) -> float | None:
    """Pull the cumulative ``blocks_dropped`` counter out of ``stats.json``.

    dump1090-fa nests counters under ``<bucket>.local`` while older readsb
    builds put them directly on the bucket, so every plausible location is
    tried before giving up. Returns ``None`` when the counter is absent.
    """
    if stats is None:
        return None
    for bucket in ("latest", "last1min", "total"):
        entry = stats.get(bucket)
        if not isinstance(entry, Mapping):
            continue
        local = entry.get("local")
        if isinstance(local, Mapping) and isinstance(local.get("blocks_dropped"), (int, float)):
            return float(local["blocks_dropped"])
        if isinstance(entry.get("blocks_dropped"), (int, float)):
            return float(entry["blocks_dropped"])
    return None


def check_blocks_dropped(
    obs: Observation, rate_per_min: float | None, cfg: CheckConfig
) -> CheckResult:
    """Growth rate of dropped baseband blocks — data loss the decoder hides."""
    if obs.stats is None:
        detail = obs.stats_error or "stats.json unavailable"
        return CheckResult("blocks_dropped", "healthy", f"skipped: {detail}")
    if extract_blocks_dropped(obs.stats) is None:
        return CheckResult("blocks_dropped", "healthy", "skipped: no blocks_dropped counter")
    if rate_per_min is None:
        return CheckResult("blocks_dropped", "healthy", "collecting baseline")
    if rate_per_min > cfg.max_blocks_dropped_per_min:
        return CheckResult(
            "blocks_dropped",
            "degraded",
            f"dropping {rate_per_min:.1f} blocks/min (max {cfg.max_blocks_dropped_per_min:.0f})",
            value=rate_per_min,
            unit="blocks/min",
            metric="blocks_dropped_per_minute",
        )
    return CheckResult(
        "blocks_dropped",
        "healthy",
        f"{rate_per_min:.1f} blocks/min",
        value=rate_per_min,
        unit="blocks/min",
        metric="blocks_dropped_per_minute",
    )


def check_disk(obs: Observation, cfg: CheckConfig) -> CheckResult:
    """Free space on ``--data-dir`` (skipped when not configured)."""
    if not cfg.data_dir:
        return CheckResult("disk", "healthy", "skipped: no --data-dir")
    if obs.disk_error:
        return CheckResult("disk", "degraded", f"cannot stat {cfg.data_dir}: {obs.disk_error}")
    if obs.disk_free is None:
        return CheckResult("disk", "degraded", f"no disk reading for {cfg.data_dir}")
    if obs.disk_free < cfg.min_disk_free_bytes:
        return CheckResult(
            "disk",
            "degraded",
            f"{obs.disk_free // (1024 * 1024)} MB free (min "
            f"{cfg.min_disk_free_bytes // (1024 * 1024)} MB)",
            value=float(obs.disk_free),
            unit="bytes",
            metric="disk_free_bytes",
        )
    return CheckResult(
        "disk",
        "healthy",
        f"{obs.disk_free // (1024 * 1024)} MB free",
        value=float(obs.disk_free),
        unit="bytes",
        metric="disk_free_bytes",
    )


def check_tcp(obs: Observation) -> list[CheckResult]:
    """One result per configured TCP output port (30005 beast, 30003 SBS)."""
    results: list[CheckResult] = []
    for endpoint in sorted(obs.tcp):
        error = obs.tcp[endpoint]
        if error is None:
            results.append(CheckResult(f"tcp:{endpoint}", "healthy", f"{endpoint} accepting"))
        else:
            results.append(CheckResult(f"tcp:{endpoint}", "failed", f"{endpoint}: {error}"))
    return results
