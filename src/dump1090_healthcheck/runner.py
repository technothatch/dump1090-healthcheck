"""Collection and orchestration: fetch from the decoder, run checks, verdict.

The only module that performs I/O. Every dependency (HTTP fetch, TCP probe,
disk usage, clock) is injectable, so tests drive the whole thing against a
local stub server or plain callables.
"""

from __future__ import annotations

import json
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from dump1090_healthcheck.checks import (
    CheckConfig,
    Observation,
    RateTracker,
    check_aircraft,
    check_blocks_dropped,
    check_disk,
    check_freshness,
    check_rate,
    check_reachable,
    check_tcp,
    extract_blocks_dropped,
)
from dump1090_healthcheck.state import CheckResult, Evaluation, HealthStateMachine, worst

FetchFn = Callable[[str, float], tuple[Mapping[str, Any] | None, str]]
TcpFn = Callable[[str, int, float], str | None]
DiskFn = Callable[[str], tuple[int, int]]


def fetch_json(url: str, *, timeout: float = 5.0) -> tuple[Mapping[str, Any] | None, str]:
    """GET ``url`` and decode a JSON object.

    Returns ``(payload, "")`` on success or ``(None, error)`` on any network
    or decoding problem — the healthcheck must never raise just because the
    decoder is down.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return None, f"{url}: {exc}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return None, f"{url}: invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, f"{url}: expected a JSON object, got {type(data).__name__}"
    return data, ""


def tcp_probe(host: str, port: int, *, timeout: float = 3.0) -> str | None:
    """Open a TCP connection to ``host:port``; ``None`` means success."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return str(exc)


def disk_usage(path: str) -> tuple[int, int]:
    """``(free_bytes, total_bytes)`` for the filesystem holding ``path``."""
    usage = shutil.disk_usage(path)
    return usage.free, usage.total


def stats_url(url: str) -> str:
    """Derive the ``stats.json`` URL next to an ``aircraft.json`` URL."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.path.endswith("aircraft.json"):
        path = parsed.path[: -len("aircraft.json")] + "stats.json"
    else:
        path = "/data/stats.json"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _safe_fetch(fetch: FetchFn, url: str, timeout: float) -> tuple[Mapping[str, Any] | None, str]:
    try:
        return fetch(url, timeout)
    except Exception as exc:  # a raising fetcher is still just "unreachable"
        return None, f"{url}: {type(exc).__name__}: {exc}"


class Healthcheck:
    """One polling healthcheck against a dump1090 / readsb decoder.

    Args:
        config: Thresholds and endpoints.
        clock: Returns the current time in epoch seconds (injectable).
        fetch: ``(url, timeout) -> (payload, error)`` HTTP fetcher.
        tcp_connect: ``(host, port, timeout) -> error|None`` TCP prober.
        disk_probe: ``path -> (free, total)`` disk usage reader.
    """

    def __init__(
        self,
        config: CheckConfig,
        *,
        clock: Callable[[], float] = time.time,
        fetch: FetchFn | None = None,
        tcp_connect: TcpFn | None = None,
        disk_probe: DiskFn | None = None,
    ) -> None:
        self.config = config
        self._clock = clock
        self._fetch: FetchFn = (
            fetch if fetch is not None else (lambda url, timeout: fetch_json(url, timeout=timeout))
        )
        self._tcp_connect: TcpFn = (
            tcp_connect
            if tcp_connect is not None
            else (lambda host, port, timeout: tcp_probe(host, port, timeout=timeout))
        )
        self._disk: DiskFn = disk_probe if disk_probe is not None else disk_usage
        self.machine = HealthStateMachine(
            enter_after=config.enter_after, recover_after=config.recover_after
        )
        self._messages = RateTracker(window=config.rate_window, min_span=config.rate_min_span)
        self._blocks = RateTracker(window=config.rate_window, min_span=config.rate_min_span)

    def _gather(self) -> Observation:
        cfg = self.config
        ts = self._clock()
        payload, payload_error = _safe_fetch(self._fetch, cfg.url, cfg.timeout)
        stats, stats_error = _safe_fetch(self._fetch, stats_url(cfg.url), cfg.timeout)
        tcp: dict[str, str | None] = {}
        for host, port in cfg.tcp_endpoints:
            label = f"{host}:{port}"
            tcp[label] = _safe_tcp(self._tcp_connect, host, port, cfg.timeout)
        disk_free: int | None = None
        disk_total: int | None = None
        disk_error = ""
        if cfg.data_dir:
            try:
                disk_free, disk_total = self._disk(cfg.data_dir)
            except OSError as exc:
                disk_error = str(exc)
        return Observation(
            ts=ts,
            payload=payload,
            payload_error=payload_error,
            stats=stats,
            stats_error=stats_error,
            tcp=tcp,
            disk_free=disk_free,
            disk_total=disk_total,
            disk_error=disk_error,
        )

    def poll(self) -> Evaluation:
        """Run one full poll: gather, check, fold into the state machine."""
        cfg = self.config
        obs = self._gather()

        rate: float | None = None
        if obs.payload is not None and isinstance(obs.payload.get("messages"), (int, float)):
            rate = self._messages.sample(float(obs.payload["messages"]), obs.ts)

        blocks_rate: float | None = None
        if obs.stats is not None and extract_blocks_dropped(obs.stats) is not None:
            per_second = self._blocks.sample(extract_blocks_dropped(obs.stats) or 0.0, obs.ts)
            blocks_rate = per_second * 60.0 if per_second is not None else None

        results: list[CheckResult] = [
            check_reachable(obs),
            check_freshness(obs, cfg),
            check_rate(obs, rate, cfg),
            check_aircraft(obs, cfg),
            check_blocks_dropped(obs, blocks_rate, cfg),
            check_disk(obs, cfg),
            *check_tcp(obs),
        ]
        bad = [item for item in results if item.state != "healthy"]
        raw = worst(*(item.state for item in results))
        reasons = tuple(f"{item.name}: {item.reason}" for item in bad)
        state = self.machine.update(raw, reasons)
        metrics = {
            item.metric: item.value for item in results if item.metric and item.value is not None
        }
        return Evaluation(
            state=state,
            reasons=self.machine.reasons,
            results=tuple(results),
            metrics=metrics,
            thresholds=_thresholds(cfg),
            ts=obs.ts,
        )


def _safe_tcp(probe: TcpFn, host: str, port: int, timeout: float) -> str | None:
    try:
        return probe(host, port, timeout)
    except Exception as exc:  # a raising prober is still "not reachable"
        return f"{type(exc).__name__}: {exc}"


def _fmt_number(value: float) -> str:
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def _thresholds(cfg: CheckConfig) -> dict[str, tuple[str, str]]:
    """Nagios ``warn;crit`` columns per metric (empty string = no threshold)."""
    return {
        "messages_per_second": (_fmt_number(cfg.min_rate), _fmt_number(cfg.max_rate)),
        "aircraft": (str(cfg.min_aircraft), str(cfg.max_aircraft)),
        "now_age_seconds": ("", _fmt_number(cfg.max_now_age)),
        "blocks_dropped_per_minute": ("", _fmt_number(cfg.max_blocks_dropped_per_min)),
        "disk_free_bytes": ("", str(cfg.min_disk_free_bytes)),
    }
