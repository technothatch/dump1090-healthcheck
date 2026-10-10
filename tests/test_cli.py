"""CLI parsing, output formats and exit codes end to end (no real decoder)."""

from __future__ import annotations

import json

import pytest

from conftest import closed_port, payload, served_payload
from dump1090_healthcheck import cli as cli_module
from dump1090_healthcheck import state as state_module
from dump1090_healthcheck.cli import _build_parser, _config_from, main, post_webhook
from dump1090_healthcheck.state import Evaluation


def test_version_flag_prints_the_version(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == "dump1090-healthcheck 0.1.0"


def test_help_documents_the_exit_codes(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "Exit codes: 0 healthy, 1 degraded, 2 failed" in out
    assert "--restart-cmd" in out
    assert "--nagios" in out


def test_bad_tcp_endpoint_is_an_argument_error(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--tcp", "30005"])
    assert excinfo.value.code == 2
    assert "expected host:port" in capsys.readouterr().err


def test_unknown_flag_exits_two(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--nope"])
    assert excinfo.value.code == 2


def test_healthy_run_exits_zero_with_a_json_document(stub, capsys) -> None:
    code = main(["--url", stub.url, "--timeout", "2", "--once"])
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["state"] == "healthy"
    assert document["reasons"] == []
    assert document["restarts"] == 0
    assert [item["name"] for item in document["checks"]] == [
        "reachable",
        "freshness",
        "rate",
        "aircraft",
        "blocks_dropped",
        "disk",
    ]


def test_nagios_flag_is_a_shorthand_for_format_nagios(stub, capsys) -> None:
    assert main(["--url", stub.url, "--timeout", "2", "--once", "--nagios"]) == 0
    shorthand = capsys.readouterr().out.strip()
    assert main(["--url", stub.url, "--timeout", "2", "--once", "--format", "nagios"]) == 0
    explicit = capsys.readouterr().out.strip()
    # The perfdata carries a wall-clock age that differs per run; the plugin
    # line itself must be identical.
    assert shorthand.split(" | ")[0] == explicit.split(" | ")[0]
    assert shorthand.startswith("dump1090 OK - all checks passed | state=0;1;2 ")
    assert explicit.startswith("dump1090 OK - all checks passed | state=0;1;2 ")
    assert "aircraft=3;0;500" in shorthand and "aircraft=3;0;500" in explicit


def test_degraded_verdict_exits_one_with_a_nagios_warning(stub, capsys) -> None:
    code = main(["--url", stub.url, "--timeout", "2", "--once", "--nagios", "--min-aircraft", "50"])
    assert code == 1
    out = capsys.readouterr().out
    assert out.startswith("dump1090 WARNING - ")
    assert "aircraft: 3 aircraft below minimum 50" in out


def test_failed_verdict_exits_two_with_reasons(stub, capsys) -> None:
    url = f"http://127.0.0.1:{closed_port()}/data/aircraft.json"
    code = main(["--url", url, "--timeout", "1", "--once"])
    assert code == 2
    document = json.loads(capsys.readouterr().out)
    assert document["state"] == "failed"
    assert any(reason.startswith("reachable:") for reason in document["reasons"])


def test_prometheus_format_is_reachable_from_the_cli(stub, capsys) -> None:
    code = main(["--url", stub.url, "--timeout", "2", "--once", "--format", "prometheus"])
    assert code == 0
    out = capsys.readouterr().out
    assert "dump1090_healthcheck_state 0" in out
    assert 'dump1090_healthcheck_check_result{check="reachable"} 0' in out
    assert "dump1090_healthcheck_timestamp_seconds " in out


def test_max_polls_emits_one_document_per_poll(stub, capsys) -> None:
    code = main(
        ["--url", stub.url, "--timeout", "2", "--max-polls", "2", "--interval", "0", "--nagios"]
    )
    assert code == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(lines) == 2
    assert all(line.startswith("dump1090 OK - ") for line in lines)


def test_restart_command_is_not_invoked_when_healthy(stub, monkeypatch, capsys) -> None:
    calls: list[str] = []

    def recorder(command: str) -> int:
        calls.append(command)
        return 0

    monkeypatch.setattr(state_module, "_run_command", recorder)
    code = main(["--url", stub.url, "--timeout", "2", "--once", "--restart-cmd", "restart-decoder"])
    assert code == 0
    assert calls == []
    assert "# restart attempt" not in capsys.readouterr().err


def test_restart_needs_health_confirmation_between_attempts(make_stub, monkeypatch, capsys) -> None:
    """degraded, degraded, healthy, healthy, degraded -> exactly two restarts."""
    empty = served_payload(aircraft=[])
    busy = served_payload(aircraft=payload()["aircraft"] * 10)
    stub = make_stub(aircraft_seq=[empty, empty, busy, busy, empty])
    calls: list[str] = []

    def recorder(command: str) -> int:
        calls.append(command)
        return 0

    monkeypatch.setattr(state_module, "_run_command", recorder)
    code = main(
        [
            "--url",
            stub.url,
            "--timeout",
            "2",
            "--min-aircraft",
            "5",
            "--max-polls",
            "5",
            "--interval",
            "0",
            "--restart-cmd",
            "restart-decoder",
            "--restart-cooldown",
            "0",
        ]
    )
    assert code == 1
    assert calls == ["restart-decoder", "restart-decoder"]
    err = capsys.readouterr().err
    assert "# restart attempt 1: ok" in err
    assert "# restart attempt 2: ok" in err
    assert "# restart attempt 3" not in err


def test_webhook_success_posts_without_reporting_an_error(make_stub, capsys) -> None:
    stub = make_stub()
    url = f"http://127.0.0.1:{closed_port()}/data/aircraft.json"
    code = main(["--url", url, "--timeout", "1", "--once", "--webhook", stub.origin + "/hook"])
    assert code == 2
    assert "# webhook error:" not in capsys.readouterr().err


def test_webhook_error_is_reported_on_stderr(make_stub, capsys) -> None:
    stub = make_stub()
    url = f"http://127.0.0.1:{closed_port()}/data/aircraft.json"
    code = main(["--url", url, "--timeout", "1", "--once", "--webhook", stub.origin + "/missing"])
    assert code == 2
    assert "# webhook error:" in capsys.readouterr().err


def test_webhook_posts_once_when_entering_failed(make_stub, monkeypatch, capsys) -> None:
    stub = make_stub()
    url = f"http://127.0.0.1:{closed_port()}/data/aircraft.json"
    sent: list[tuple[str, Evaluation]] = []

    def sender(hook: str, ev: Evaluation) -> str | None:
        sent.append((hook, ev))
        return None

    code = main(
        [
            "--url",
            url,
            "--timeout",
            "1",
            "--max-polls",
            "2",
            "--interval",
            "0",
            "--webhook",
            stub.origin + "/hook",
        ],
        webhook=sender,
    )
    assert code == 2
    assert len(sent) == 1
    assert sent[0][0] == stub.origin + "/hook"
    assert sent[0][1].state == "failed"
    assert "# webhook error:" not in capsys.readouterr().err


def test_post_webhook_success_returns_none(stub) -> None:
    ev = Evaluation(
        state="failed",
        reasons=("reachable: down",),
        results=(),
        metrics={},
        thresholds={},
        ts=1791028800.0,
    )
    assert post_webhook(stub.origin + "/hook", ev, timeout=2.0) is None


def test_post_webhook_returns_an_error_string_on_http_and_network_failures(make_stub) -> None:
    stub = make_stub()
    ev = Evaluation(
        state="failed",
        reasons=("reachable: down",),
        results=(),
        metrics={},
        thresholds={},
        ts=1791028800.0,
    )
    assert post_webhook(stub.origin + "/missing", ev, timeout=2.0) is not None
    error = post_webhook(f"http://127.0.0.1:{closed_port()}/hook", ev, timeout=1.0)
    assert error is not None
    assert error != ""


def test_keyboard_interrupt_stops_the_loop_cleanly(stub, monkeypatch, capsys) -> None:
    class InterruptingClock:
        def sleep(self, seconds: float) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "time", InterruptingClock())
    code = main(["--url", stub.url, "--timeout", "2", "--interval", "1"])
    assert code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["state"] == "healthy"
    assert "# stopped" in captured.err


def test_config_from_maps_every_flag_onto_check_config() -> None:
    args = _build_parser().parse_args(
        [
            "--url",
            "http://readsb.lan:8080/data/aircraft.json",
            "--timeout",
            "2.5",
            "--max-now-age",
            "30",
            "--min-rate",
            "2",
            "--max-rate",
            "100",
            "--min-aircraft",
            "1",
            "--max-aircraft",
            "200",
            "--max-blocks-dropped-per-min",
            "60",
            "--data-dir",
            "/run/readsb",
            "--min-disk-free-mb",
            "250",
            "--tcp",
            "127.0.0.1:30005",
            "--tcp",
            "readsb.lan:30003",
            "--enter-after",
            "3",
            "--recover-after",
            "4",
        ]
    )
    cfg = _config_from(args)
    assert cfg.url == "http://readsb.lan:8080/data/aircraft.json"
    assert cfg.timeout == 2.5
    assert cfg.max_now_age == 30.0
    assert (cfg.min_rate, cfg.max_rate) == (2.0, 100.0)
    assert (cfg.min_aircraft, cfg.max_aircraft) == (1, 200)
    assert cfg.max_blocks_dropped_per_min == 60.0
    assert cfg.data_dir == "/run/readsb"
    assert cfg.min_disk_free_bytes == 250 * 1024 * 1024
    assert cfg.tcp_endpoints == (("127.0.0.1", 30005), ("readsb.lan", 30003))
    assert (cfg.enter_after, cfg.recover_after) == (3, 4)


def test_default_url_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("DUMP1090_URL", "http://readsb.lan:8080/data/aircraft.json")
    assert cli_module._default_url() == "http://readsb.lan:8080/data/aircraft.json"
    monkeypatch.delenv("DUMP1090_URL")
    assert cli_module._default_url() == "http://localhost:8080/data/aircraft.json"


def test_parser_defaults_match_the_documented_configuration() -> None:
    args = _build_parser().parse_args([])
    assert args.format == "json"
    assert args.nagios is False
    assert args.once is False
    assert args.interval == 10.0
    assert args.max_polls == 0
    assert args.enter_after == 1
    assert args.recover_after == 2
    assert args.restart_cmd == ""
    assert args.restart_cooldown == 300.0
    assert args.webhook == ""
    assert args.data_dir == ""
    assert args.tcp == []


def test_formatter_dispatch_covers_every_documented_format(stub, capsys) -> None:
    for fmt in ("json", "nagios", "prometheus"):
        assert main(["--url", stub.url, "--timeout", "2", "--once", "--format", fmt]) == 0
    out = capsys.readouterr().out
    assert '"state": "healthy"' in out
    assert "dump1090 OK - " in out
    assert "dump1090_healthcheck_state 0" in out
