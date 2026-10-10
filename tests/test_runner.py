"""Collection and orchestration: URL derivation, I/O helpers and full polls."""

from __future__ import annotations

import socket
import threading
from typing import Any

import pytest

from conftest import T0, closed_port, payload, stats_doc
from dump1090_healthcheck.checks import CheckConfig, RateTracker
from dump1090_healthcheck.runner import (
    FetchFn,
    Healthcheck,
    disk_usage,
    fetch_json,
    stats_url,
    tcp_probe,
)


class FakeClock:
    """Deterministic stand-in for ``time.time``."""

    def __init__(self, start: float = T0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_healthcheck(
    documents: list[dict[str, Any]],
    *,
    stats_seq: list[dict[str, Any]] | None = None,
    clock: FakeClock | None = None,
    **overrides: Any,
) -> Healthcheck:
    """A :class:`Healthcheck` with scripted per-poll fetch responses.

    ``documents`` is consumed one entry per ``aircraft.json`` request and
    ``stats_seq`` one per ``stats.json`` request; the last entry repeats.
    """
    clock = clock if clock is not None else FakeClock()
    stats_seq = stats_seq if stats_seq is not None else [stats_doc()]
    counts = {"aircraft": 0, "stats": 0}

    def fetch(url: str, timeout: float) -> tuple[dict[str, Any] | None, str]:
        if url.endswith("stats.json"):
            index = min(counts["stats"], len(stats_seq) - 1)
            counts["stats"] += 1
            return stats_seq[index], ""
        index = min(counts["aircraft"], len(documents) - 1)
        counts["aircraft"] += 1
        return documents[index], ""

    config = CheckConfig(url="http://decoder/data/aircraft.json", **overrides)
    return Healthcheck(config, clock=clock, fetch=fetch)


def always(payload_value: dict[str, Any] | None) -> FetchFn:
    """A fetcher that always answers with ``payload_value`` (or an error)."""
    error = "" if payload_value is not None else "scripted failure"
    return lambda url, timeout: (payload_value, error)


def test_stats_url_is_derived_next_to_aircraft_json() -> None:
    assert stats_url("http://localhost:8080/data/aircraft.json") == (
        "http://localhost:8080/data/stats.json"
    )
    assert stats_url("http://readsb.lan:8080/data/aircraft.json?x=1#frag") == (
        "http://readsb.lan:8080/data/stats.json"
    )


def test_stats_url_falls_back_to_the_standard_path() -> None:
    assert stats_url("http://localhost:8080/data/receiver.json") == (
        "http://localhost:8080/data/stats.json"
    )
    assert stats_url("http://localhost:8080/") == "http://localhost:8080/data/stats.json"


def test_fetch_json_success(stub) -> None:
    data, error = fetch_json(stub.url, timeout=2.0)
    assert error == ""
    assert data is not None
    assert data["messages"] == 1000


def test_fetch_json_reports_a_closed_port() -> None:
    data, error = fetch_json(f"http://127.0.0.1:{closed_port()}/data/aircraft.json", timeout=1.0)
    assert data is None
    assert "http://" in error


def test_fetch_json_rejects_invalid_json_and_non_objects(make_stub) -> None:
    broken = make_stub(raw={"/data/aircraft.json": b"{not json"})
    _, error = fetch_json(broken.url, timeout=2.0)
    assert "invalid JSON" in error

    listing = make_stub(raw={"/data/aircraft.json": b"[1, 2, 3]"})
    _, error = fetch_json(listing.url, timeout=2.0)
    assert "expected a JSON object, got list" in error


def test_tcp_probe_connects_and_reports_failures() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def accept_and_close() -> None:
        connection, _ = listener.accept()
        connection.close()

    thread = threading.Thread(target=accept_and_close, daemon=True)
    thread.start()
    try:
        assert tcp_probe("127.0.0.1", port, timeout=2.0) is None
        thread.join(timeout=5)
    finally:
        listener.close()

    error = tcp_probe("127.0.0.1", port, timeout=1.0)
    assert error is not None
    assert error != ""


def test_disk_usage_reads_the_filesystem_holding_the_path(tmp_path) -> None:
    free, total = disk_usage(str(tmp_path))
    assert free > 0
    assert total > 0
    assert free <= total


def test_first_poll_reports_a_baseline_instead_of_a_wild_rate() -> None:
    healthcheck = make_healthcheck([payload(messages=1000)])
    ev = healthcheck.poll()
    assert ev.state == "healthy"
    rates = [item for item in ev.results if item.name == "rate"]
    assert rates[0].reason == "collecting rate baseline"
    assert "messages_per_second" not in ev.metrics


def test_second_poll_computes_message_rate_and_blocks_per_minute() -> None:
    clock = FakeClock()
    healthcheck = make_healthcheck(
        [payload(messages=1000), payload(messages=1300)],
        stats_seq=[stats_doc(dropped=0), stats_doc(dropped=12)],
        clock=clock,
    )
    healthcheck.poll()
    clock.advance(10.0)
    ev = healthcheck.poll()
    assert ev.metrics["messages_per_second"] == pytest.approx(30.0)
    assert ev.metrics["blocks_dropped_per_minute"] == pytest.approx(72.0)
    assert ev.state == "healthy"


def test_blocks_dropped_growth_over_the_limit_degrades_the_verdict() -> None:
    clock = FakeClock()
    healthcheck = make_healthcheck(
        [payload()],
        stats_seq=[stats_doc(dropped=0), stats_doc(dropped=30)],
        clock=clock,
    )
    healthcheck.poll()
    clock.advance(10.0)
    ev = healthcheck.poll()
    assert ev.metrics["blocks_dropped_per_minute"] == pytest.approx(180.0)
    assert ev.state == "degraded"
    assert any(item.name == "blocks_dropped" and item.state == "degraded" for item in ev.results)


def test_stats_failures_just_skip_the_blocks_check() -> None:
    def failing_fetch(url: str, timeout: float) -> tuple[dict[str, Any] | None, str]:
        if url.endswith("stats.json"):
            return None, "stats.json: refused"
        return payload(), ""

    healthcheck = Healthcheck(
        CheckConfig(url="http://decoder/data/aircraft.json"),
        clock=FakeClock(),
        fetch=failing_fetch,
    )
    ev = healthcheck.poll()
    blocks = next(item for item in ev.results if item.name == "blocks_dropped")
    assert blocks.state == "healthy"
    assert blocks.reason == "skipped: stats.json: refused"


def test_raising_fetch_is_just_an_unreachable_decoder() -> None:
    def boom(url: str, timeout: float) -> tuple[dict[str, Any] | None, str]:
        raise OSError("network is gone")

    healthcheck = Healthcheck(
        CheckConfig(url="http://decoder/data/aircraft.json"), clock=FakeClock(), fetch=boom
    )
    ev = healthcheck.poll()
    assert ev.state == "failed"
    reachable = next(item for item in ev.results if item.name == "reachable")
    assert reachable.reason.endswith("OSError: network is gone")


def test_tcp_endpoints_are_probed_and_failures_surface() -> None:
    def probe(host: str, port: int, timeout: float) -> str | None:
        return None if port == 30005 else "connection refused"

    healthcheck = Healthcheck(
        CheckConfig(
            url="http://decoder/data/aircraft.json",
            tcp_endpoints=(("127.0.0.1", 30005), ("127.0.0.1", 30003)),
        ),
        clock=FakeClock(),
        fetch=always(payload()),
        tcp_connect=probe,
    )
    ev = healthcheck.poll()
    tcp_states = {item.name: item.state for item in ev.results if item.name.startswith("tcp:")}
    assert tcp_states == {
        "tcp:127.0.0.1:30003": "failed",
        "tcp:127.0.0.1:30005": "healthy",
    }
    assert ev.state == "failed"


def test_raising_tcp_probe_is_reported_as_a_failure() -> None:
    def boom(host: str, port: int, timeout: float) -> str | None:
        raise RuntimeError("probe exploded")

    healthcheck = Healthcheck(
        CheckConfig(url="http://decoder/data/aircraft.json", tcp_endpoints=(("127.0.0.1", 30005),)),
        clock=FakeClock(),
        fetch=always(payload()),
        tcp_connect=boom,
    )
    ev = healthcheck.poll()
    failed = next(item for item in ev.results if item.name == "tcp:127.0.0.1:30005")
    assert failed.state == "failed"
    assert failed.reason == "127.0.0.1:30005: RuntimeError: probe exploded"


def test_disk_stat_errors_degrade_the_verdict() -> None:
    def failing_disk(path: str) -> tuple[int, int]:
        raise OSError("No such file or directory")

    healthcheck = Healthcheck(
        CheckConfig(url="http://decoder/data/aircraft.json", data_dir="/run/readsb"),
        clock=FakeClock(),
        fetch=always(payload()),
        disk_probe=failing_disk,
    )
    ev = healthcheck.poll()
    disk = next(item for item in ev.results if item.name == "disk")
    assert disk.state == "degraded"
    assert "No such file or directory" in disk.reason


def test_disk_readings_are_collected_when_data_dir_is_configured(tmp_path) -> None:
    healthcheck = Healthcheck(
        CheckConfig(url="http://decoder/data/aircraft.json", data_dir=str(tmp_path)),
        clock=FakeClock(),
        fetch=always(payload()),
        disk_probe=lambda path: (512 * 1024 * 1024, 1024 * 1024 * 1024),
    )
    ev = healthcheck.poll()
    assert ev.metrics["disk_free_bytes"] == 512 * 1024 * 1024
    assert ev.state == "healthy"


def test_state_machine_hysteresis_applies_across_polls() -> None:
    healthcheck = make_healthcheck(
        [payload(aircraft=[]), payload(aircraft=[]), payload()],
        enter_after=1,
        recover_after=2,
        min_aircraft=1,
    )
    first = healthcheck.poll()
    second = healthcheck.poll()
    third = healthcheck.poll()
    assert (first.state, second.state, third.state) == ("degraded", "degraded", "degraded")
    assert "1/2" in third.reasons[0]
    fourth = healthcheck.poll()
    assert fourth.state == "healthy"
    assert healthcheck.machine.transitions == 2


def test_thresholds_expose_nagios_warn_and_crit_columns() -> None:
    ev = make_healthcheck([payload()]).poll()
    assert ev.thresholds["messages_per_second"] == ("1", "5000")
    assert ev.thresholds["aircraft"] == ("0", "500")
    assert ev.thresholds["now_age_seconds"] == ("", "15")
    assert ev.thresholds["blocks_dropped_per_minute"] == ("", "120")
    assert ev.thresholds["disk_free_bytes"] == ("", str(100 * 1024 * 1024))


def test_fractional_thresholds_keep_their_decimal_form() -> None:
    ev = make_healthcheck([payload()], min_rate=1.5, max_rate=2500.5, max_now_age=7.5).poll()
    assert ev.thresholds["messages_per_second"] == ("1.5", "2500.5")
    assert ev.thresholds["now_age_seconds"] == ("", "7.5")


def test_end_to_end_poll_against_the_stub_decoder(stub) -> None:
    healthcheck = Healthcheck(CheckConfig(url=stub.url, timeout=2.0))
    ev = healthcheck.poll()
    assert ev.state == "healthy"
    names = [item.name for item in ev.results]
    assert names == ["reachable", "freshness", "rate", "aircraft", "blocks_dropped", "disk"]
    assert stub.requests == ["/data/aircraft.json", "/data/stats.json"]


def test_each_poll_fetches_both_documents_once(stub) -> None:
    healthcheck = Healthcheck(CheckConfig(url=stub.url, timeout=2.0))
    healthcheck.poll()
    healthcheck.poll()
    assert stub.requests.count("/data/aircraft.json") == 2
    assert stub.requests.count("/data/stats.json") == 2


def test_rate_tracker_rejects_non_positive_windows() -> None:
    with pytest.raises(ValueError, match="positive"):
        RateTracker(window=0.0)
    with pytest.raises(ValueError, match="positive"):
        RateTracker(min_span=-3.0)


def test_fetch_fn_signature_is_accepted_as_a_plain_callable() -> None:
    fetcher: FetchFn = always(payload())
    data, error = fetcher("http://decoder/data/aircraft.json", 1.0)
    assert error == ""
    assert data is not None and data["messages"] == 1000
