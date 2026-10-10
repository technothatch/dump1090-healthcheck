"""Fixture-driven pass/fail evaluation of every check in ``checks.py``.

Every case is built from an :class:`Observation` dataclass — no network, no
decoder, no real disk.
"""

from __future__ import annotations

import pytest

from conftest import T0, observation, payload, stats_doc
from dump1090_healthcheck.checks import (
    DEFAULT_URL,
    CheckConfig,
    check_aircraft,
    check_blocks_dropped,
    check_disk,
    check_freshness,
    check_rate,
    check_reachable,
    check_tcp,
    extract_blocks_dropped,
)


def test_config_defaults_point_at_a_local_dump1090() -> None:
    cfg = CheckConfig()
    assert cfg.url == DEFAULT_URL == "http://localhost:8080/data/aircraft.json"
    assert (cfg.enter_after, cfg.recover_after) == (1, 2)
    assert (cfg.rate_window, cfg.rate_min_span) == (60.0, 5.0)
    assert cfg.tcp_endpoints == ()


def test_reachable_when_aircraft_json_answers() -> None:
    result = check_reachable(observation())
    assert (result.name, result.state, result.reason) == ("reachable", "healthy", "HTTP reachable")


def test_reachable_reports_the_fetch_error_when_it_fails() -> None:
    result = check_reachable(observation(payload=None, payload_error="http://x: refused"))
    assert (result.name, result.state) == ("reachable", "failed")
    assert result.reason == "http://x: refused"


def test_reachable_without_an_error_still_says_unreachable() -> None:
    result = check_reachable(observation(payload=None))
    assert result.state == "failed"
    assert result.reason == "endpoint unreachable"


def test_freshness_skips_when_there_is_no_payload() -> None:
    result = check_freshness(observation(payload=None), CheckConfig())
    assert (result.state, result.reason) == ("healthy", "skipped: no data")


def test_freshness_fails_when_now_is_missing_or_not_numeric() -> None:
    without_now = check_freshness(
        observation(payload={"messages": 1, "aircraft": []}), CheckConfig()
    )
    assert without_now.state == "failed"
    assert without_now.reason == "aircraft.json has no numeric 'now' field"
    for broken in ("2026-10-03T12:00:00Z", True, [T0], None):
        broken_now = check_freshness(observation(payload=payload(now=broken)), CheckConfig())
        assert broken_now.state == "failed"
        assert broken_now.reason == "aircraft.json has no numeric 'now' field"


def test_freshness_passes_for_fresh_data_and_carries_the_age_metric() -> None:
    result = check_freshness(observation(ts=T0 + 3.0), CheckConfig(max_now_age=15.0))
    assert result.state == "healthy"
    assert result.reason == "3.0s old"
    assert result.value == pytest.approx(3.0)
    assert (result.unit, result.metric) == ("s", "now_age_seconds")


def test_freshness_fails_when_now_is_frozen() -> None:
    result = check_freshness(observation(ts=T0 + 42.0), CheckConfig(max_now_age=15.0))
    assert result.state == "failed"
    assert result.reason == "data is 42.0s old (max 15s)"
    assert result.value == pytest.approx(42.0)


def test_freshness_accepts_an_integer_now() -> None:
    result = check_freshness(
        observation(payload=payload(now=int(T0) + 1)), CheckConfig(max_now_age=15.0)
    )
    assert result.state == "healthy"


def test_rate_skips_without_a_payload_or_counter() -> None:
    cfg = CheckConfig()
    assert check_rate(observation(payload=None), 10.0, cfg).reason == "skipped: no data"
    assert check_rate(observation(payload={"now": T0}), 10.0, cfg).reason == (
        "skipped: no messages counter"
    )


def test_rate_is_healthy_while_the_baseline_collects() -> None:
    result = check_rate(observation(), None, CheckConfig())
    assert (result.state, result.reason) == ("healthy", "collecting rate baseline")


def test_rate_below_minimum_is_degraded() -> None:
    result = check_rate(observation(), 0.2, CheckConfig(min_rate=1.0, max_rate=5000.0))
    assert result.state == "degraded"
    assert result.reason == "message rate 0.2/s below minimum 1.0/s"
    assert result.metric == "messages_per_second"
    assert result.unit == "msg/s"


def test_rate_above_maximum_is_degraded() -> None:
    result = check_rate(observation(), 9000.0, CheckConfig(max_rate=5000.0))
    assert result.state == "degraded"
    assert "above maximum 5000.0/s" in result.reason


def test_rate_inside_the_band_is_healthy() -> None:
    result = check_rate(observation(), 42.0, CheckConfig())
    assert (result.state, result.value, result.metric) == ("healthy", 42.0, "messages_per_second")
    assert result.reason == "42.0 msg/s"


def test_aircraft_skips_without_a_payload() -> None:
    result = check_aircraft(observation(payload=None), CheckConfig())
    assert (result.state, result.reason) == ("healthy", "skipped: no data")


def test_aircraft_degrades_when_the_list_is_missing() -> None:
    result = check_aircraft(observation(payload={"now": T0, "aircraft": "nope"}), CheckConfig())
    assert (result.state, result.reason) == ("degraded", "aircraft.json has no aircraft list")


def test_aircraft_count_below_minimum_is_degraded() -> None:
    obs = observation(payload=payload(aircraft=[]))
    result = check_aircraft(obs, CheckConfig(min_aircraft=5))
    assert result.state == "degraded"
    assert result.reason == "0 aircraft below minimum 5"
    assert result.metric == "aircraft"
    assert result.value == 0.0


def test_aircraft_count_above_maximum_is_degraded() -> None:
    entries = [payload()["aircraft"][0]] * 501
    result = check_aircraft(observation(payload=payload(aircraft=entries)), CheckConfig())
    assert result.state == "degraded"
    assert result.reason == "501 aircraft above maximum 500"


def test_aircraft_count_inside_the_band_is_healthy() -> None:
    result = check_aircraft(observation(), CheckConfig())
    assert (result.state, result.value) == ("healthy", 3.0)
    assert result.reason == "3 aircraft"


def test_extract_blocks_dropped_prefers_the_dump1090_fa_local_nesting() -> None:
    assert extract_blocks_dropped(stats_doc(dropped=17)) == 17.0
    assert extract_blocks_dropped(stats_doc(dropped=0)) == 0.0


def test_extract_blocks_dropped_accepts_readsb_flat_buckets() -> None:
    flat = {"latest": {"blocks_dropped": 4}, "total": {"blocks_dropped": 9}}
    assert extract_blocks_dropped(flat) == 4.0
    assert extract_blocks_dropped({"total": {"blocks_dropped": 9}}) == 9.0
    assert extract_blocks_dropped({"last1min": {"blocks_dropped": 3}}) == 3.0


def test_extract_blocks_dropped_skips_broken_buckets_and_missing_counters() -> None:
    assert extract_blocks_dropped(None) is None
    assert extract_blocks_dropped({}) is None
    assert extract_blocks_dropped({"latest": "oops", "total": {"blocks_dropped": 5}}) == 5.0
    assert extract_blocks_dropped({"latest": {"local": {"nope": 1}}}) is None
    assert extract_blocks_dropped({"latest": {"blocks_processed": 10}}) is None
    assert extract_blocks_dropped({"latest": {"local": "nope"}}) is None


def test_blocks_dropped_skips_without_stats_or_counter() -> None:
    cfg = CheckConfig()
    missing = check_blocks_dropped(observation(stats=None), 10.0, cfg)
    assert (missing.state, missing.reason) == ("healthy", "skipped: stats.json unavailable")
    errored = check_blocks_dropped(observation(stats=None, stats_error="refused"), 10.0, cfg)
    assert errored.reason == "skipped: refused"
    no_counter = check_blocks_dropped(observation(stats={"total": {}}), 10.0, cfg)
    assert no_counter.reason == "skipped: no blocks_dropped counter"


def test_blocks_dropped_is_healthy_while_the_baseline_collects() -> None:
    result = check_blocks_dropped(observation(), None, CheckConfig())
    assert (result.state, result.reason) == ("healthy", "collecting baseline")


def test_blocks_dropped_growth_above_the_limit_is_degraded() -> None:
    result = check_blocks_dropped(observation(), 180.5, CheckConfig())
    assert result.state == "degraded"
    assert result.reason == "dropping 180.5 blocks/min (max 120)"
    assert (result.unit, result.metric) == ("blocks/min", "blocks_dropped_per_minute")


def test_blocks_dropped_growth_inside_the_limit_is_healthy() -> None:
    result = check_blocks_dropped(observation(), 0.0, CheckConfig())
    assert (result.state, result.value) == ("healthy", 0.0)
    assert result.reason == "0.0 blocks/min"


def test_disk_is_skipped_without_a_data_dir() -> None:
    result = check_disk(observation(disk_free=10), CheckConfig(data_dir=""))
    assert (result.state, result.reason) == ("healthy", "skipped: no --data-dir")


def test_disk_degrades_on_stat_errors_and_missing_readings() -> None:
    cfg = CheckConfig(data_dir="/run/readsb")
    errored = check_disk(observation(disk_error="permission denied"), cfg)
    assert errored.state == "degraded"
    assert errored.reason == "cannot stat /run/readsb: permission denied"
    empty = check_disk(observation(disk_free=None), cfg)
    assert empty.reason == "no disk reading for /run/readsb"


def test_disk_degrades_when_free_space_drops_below_the_minimum(tmp_path) -> None:
    cfg = CheckConfig(data_dir=str(tmp_path), min_disk_free_bytes=100 * 1024 * 1024)
    result = check_disk(observation(disk_free=50 * 1024 * 1024), cfg)
    assert result.state == "degraded"
    assert result.reason == "50 MB free (min 100 MB)"
    assert (result.value, result.unit, result.metric) == (
        float(50 * 1024 * 1024),
        "bytes",
        "disk_free_bytes",
    )


def test_disk_healthy_when_space_remains(tmp_path) -> None:
    cfg = CheckConfig(data_dir=str(tmp_path), min_disk_free_bytes=1024)
    result = check_disk(observation(disk_free=4096 * 1024 * 1024), cfg)
    assert (result.state, result.reason) == ("healthy", "4096 MB free")
    assert result.metric == "disk_free_bytes"


def test_tcp_reports_one_result_per_endpoint_sorted() -> None:
    obs = observation(tcp={"127.0.0.1:30003": None, "127.0.0.1:30005": "connection refused"})
    results = check_tcp(obs)
    assert [item.name for item in results] == ["tcp:127.0.0.1:30003", "tcp:127.0.0.1:30005"]
    assert results[0].state == "healthy"
    assert results[0].reason == "127.0.0.1:30003 accepting"
    assert results[1].state == "failed"
    assert results[1].reason == "127.0.0.1:30005: connection refused"


def test_tcp_returns_no_results_when_nothing_is_configured() -> None:
    assert check_tcp(observation()) == []
