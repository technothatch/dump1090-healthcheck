"""Exact-output tests for the JSON, Nagios and Prometheus renderers."""

from __future__ import annotations

import json

from dump1090_healthcheck.output import render_json, render_nagios, render_prometheus
from dump1090_healthcheck.state import CheckResult, Evaluation

TS = 1791028800.0


def evaluation(
    *,
    state: str = "healthy",
    reasons: tuple[str, ...] = (),
    results: tuple[CheckResult, ...] = (),
    metrics: dict[str, float] | None = None,
    thresholds: dict[str, tuple[str, str]] | None = None,
    restarts: int = 0,
) -> Evaluation:
    return Evaluation(
        state=state,
        reasons=reasons,
        results=results,
        metrics=metrics or {},
        thresholds=thresholds or {},
        ts=TS,
        restarts=restarts,
    )


def healthy_results() -> tuple[CheckResult, ...]:
    return (
        CheckResult("reachable", "healthy", "HTTP reachable"),
        CheckResult(
            "freshness",
            "healthy",
            "0.1s old",
            value=0.1,
            unit="s",
            metric="now_age_seconds",
        ),
    )


def test_json_contains_state_reasons_checks_and_sorted_metrics() -> None:
    ev = evaluation(
        state="degraded",
        reasons=("rate: low",),
        results=healthy_results(),
        metrics={"messages_per_second": 0.5, "aircraft": 3.0},
        restarts=2,
    )
    text = render_json(ev)
    parsed = json.loads(text)
    assert parsed["state"] == "degraded"
    assert parsed["reasons"] == ["rate: low"]
    assert parsed["ts"] == TS
    assert parsed["restarts"] == 2
    assert [item["name"] for item in parsed["checks"]] == ["reachable", "freshness"]
    assert parsed["checks"][1] == {
        "name": "freshness",
        "state": "healthy",
        "reason": "0.1s old",
        "value": 0.1,
        "unit": "s",
    }
    assert list(parsed["metrics"]) == ["aircraft", "messages_per_second"]
    assert text.startswith("{\n")


def test_nagios_ok_line_for_a_healthy_verdict() -> None:
    text = render_nagios(evaluation())
    assert text == "dump1090 OK - all checks passed | state=0;1;2"


def test_nagios_warning_line_carries_reasons_and_perfdata() -> None:
    ev = evaluation(
        state="degraded",
        reasons=("rate: message rate 0.2/s below minimum 1.0/s", "aircraft: 0 aircraft"),
        metrics={"messages_per_second": 0.5, "aircraft": 0.0},
        thresholds={
            "messages_per_second": ("1", "5000"),
            "aircraft": ("1", "500"),
        },
    )
    assert render_nagios(ev) == (
        "dump1090 WARNING - rate: message rate 0.2/s below minimum 1.0/s; "
        "aircraft: 0 aircraft | state=1;1;2 aircraft=0;1;500 "
        "messages_per_second=0.5;1;5000"
    )


def test_nagios_critical_line_for_a_failed_verdict() -> None:
    ev = evaluation(state="failed", reasons=("reachable: endpoint unreachable",))
    assert render_nagios(ev).startswith("dump1090 CRITICAL - reachable: endpoint unreachable |")


def test_nagios_omits_thresholds_when_none_are_configured() -> None:
    ev = evaluation(metrics={"custom": 4.0})
    assert render_nagios(ev) == "dump1090 OK - all checks passed | state=0;1;2 custom=4;;"


def test_prometheus_emits_state_and_per_check_gauges() -> None:
    ev = evaluation(state="failed", results=healthy_results())
    text = render_prometheus(ev)
    lines = text.splitlines()
    assert lines[0] == (
        "# HELP dump1090_healthcheck_state Verdict: 0 healthy, 1 degraded, 2 failed."
    )
    assert lines[1] == "# TYPE dump1090_healthcheck_state gauge"
    assert lines[2] == "dump1090_healthcheck_state 2"
    assert (
        lines[3] == "# HELP dump1090_healthcheck_check_result "
        "Per-check verdict: 0 healthy, 1 degraded, 2 failed."
    )
    assert 'dump1090_healthcheck_check_result{check="freshness"} 0' in lines
    assert 'dump1090_healthcheck_check_result{check="reachable"} 0' in lines
    assert text.endswith("\n")


def test_prometheus_orders_check_gauges_by_name() -> None:
    ev = evaluation(
        results=(
            CheckResult("reachable", "healthy", "ok"),
            CheckResult("aircraft", "degraded", "few"),
        )
    )
    checks = [line for line in render_prometheus(ev).splitlines() if "check_result{" in line]
    assert checks == [
        'dump1090_healthcheck_check_result{check="aircraft"} 1',
        'dump1090_healthcheck_check_result{check="reachable"} 0',
    ]


def test_prometheus_escapes_quotes_and_backslashes_in_labels() -> None:
    ev = evaluation(results=(CheckResult('tcp:we"ird\\host:30005', "failed", "nope"),))
    text = render_prometheus(ev)
    assert 'check="tcp:we\\"ird\\\\host:30005"' in text


def test_prometheus_emits_metric_families_with_help_and_type() -> None:
    ev = evaluation(
        metrics={
            "messages_per_second": 12.5,
            "aircraft": 3.0,
            "now_age_seconds": 0.1,
            "blocks_dropped_per_minute": 0.0,
            "disk_free_bytes": 104857600.0,
        }
    )
    text = render_prometheus(ev)
    assert "# HELP dump1090_healthcheck_messages_per_second " in text
    assert "# TYPE dump1090_healthcheck_messages_per_second gauge" in text
    assert "dump1090_healthcheck_messages_per_second 12.5" in text
    assert "dump1090_healthcheck_aircraft 3" in text
    assert "dump1090_healthcheck_disk_free_bytes 104857600" in text
    assert "dump1090_healthcheck_blocks_dropped_per_minute 0" in text


def test_prometheus_skips_unsafe_metric_names_and_defaults_help_text() -> None:
    ev = evaluation(metrics={"Bad-Metric": 1.0, "custom_thing": 2.0})
    text = render_prometheus(ev)
    assert "Bad-Metric" not in text
    assert "# HELP dump1090_healthcheck_custom_thing custom_thing." in text
    assert "dump1090_healthcheck_custom_thing 2" in text


def test_prometheus_always_reports_the_evaluation_timestamp() -> None:
    text = render_prometheus(evaluation())
    assert "dump1090_healthcheck_timestamp_seconds 1791028800" in text
    assert "# TYPE dump1090_healthcheck_timestamp_seconds gauge" in text
