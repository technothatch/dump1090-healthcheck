"""State machine transitions, verdict mapping and restart cooldown tests."""

from __future__ import annotations

import subprocess
from typing import NoReturn

import pytest

from dump1090_healthcheck.state import (
    EXIT_CODES,
    NAGIOS_STATES,
    STATE_ORDER,
    CheckResult,
    HealthStateMachine,
    RestartController,
    exit_code,
    worst,
)


def test_worst_orders_failed_over_degraded_over_healthy() -> None:
    assert worst("healthy", "degraded", "failed") == "failed"
    assert worst("failed", "healthy") == "failed"
    assert worst("healthy", "degraded") == "degraded"
    assert worst("healthy", "healthy") == "healthy"


def test_worst_of_nothing_is_healthy() -> None:
    assert worst() == "healthy"


def test_exit_codes_follow_the_0_1_2_convention() -> None:
    assert exit_code("healthy") == 0
    assert exit_code("degraded") == 1
    assert exit_code("failed") == 2
    assert EXIT_CODES == {"healthy": 0, "degraded": 1, "failed": 2}


def test_nagios_and_ordering_tables_agree_with_the_state_names() -> None:
    assert NAGIOS_STATES == {"healthy": "OK", "degraded": "WARNING", "failed": "CRITICAL"}
    assert STATE_ORDER == {"healthy": 0, "degraded": 1, "failed": 2}


def test_check_result_defaults() -> None:
    result = CheckResult("reachable", "healthy", "HTTP reachable")
    assert (result.value, result.unit, result.metric) == (None, "", "")


def test_invalid_hysteresis_thresholds_are_rejected() -> None:
    with pytest.raises(ValueError, match="enter_after"):
        HealthStateMachine(enter_after=0)
    with pytest.raises(ValueError, match="recover_after"):
        HealthStateMachine(recover_after=0)


def test_single_bad_poll_enters_bad_state_immediately() -> None:
    machine = HealthStateMachine(enter_after=1, recover_after=2)
    assert machine.state == "healthy"
    assert machine.update("failed", ["reachable: down"]) == "failed"
    assert machine.reasons == ("reachable: down",)
    assert machine.transitions == 1


def test_enter_after_two_needs_two_consecutive_bad_polls() -> None:
    machine = HealthStateMachine(enter_after=2, recover_after=1)
    assert machine.update("failed", ["reachable: down"]) == "healthy"
    assert machine.reasons == ()
    assert machine.update("failed", ["reachable: down"]) == "failed"
    assert machine.transitions == 1


def test_healthy_poll_during_entry_streak_resets_it() -> None:
    machine = HealthStateMachine(enter_after=2, recover_after=1)
    machine.update("failed", ["x"])
    assert machine.update("healthy", []) == "healthy"
    assert machine.update("failed", ["x"]) == "healthy"
    assert machine.update("failed", ["x"]) == "failed"


def test_recovery_requires_recover_after_healthy_polls() -> None:
    machine = HealthStateMachine(enter_after=1, recover_after=2)
    machine.update("failed", ["reachable: down"])
    assert machine.update("healthy", []) == "failed"
    assert machine.reasons == ("awaiting recovery: 1/2 consecutive healthy checks",)
    assert machine.update("healthy", []) == "healthy"
    assert machine.reasons == ()
    assert machine.transitions == 2


def test_bad_poll_while_awaiting_recovery_resets_the_streak() -> None:
    machine = HealthStateMachine(enter_after=1, recover_after=2)
    machine.update("failed", ["reachable: down"])
    machine.update("healthy", [])
    machine.update("failed", ["freshness: stale"])
    assert machine.state == "failed"
    assert machine.reasons == ("freshness: stale",)
    assert machine.update("healthy", []) == "failed"
    assert "1/2" in machine.reasons[0]


def test_bad_states_escalate_and_deescalate_on_the_spot() -> None:
    machine = HealthStateMachine(enter_after=1, recover_after=2)
    assert machine.update("degraded", ["rate: low"]) == "degraded"
    assert machine.update("failed", ["reachable: down"]) == "failed"
    assert machine.update("degraded", ["rate: low"]) == "degraded"
    assert machine.update("failed", ["reachable: down"]) == "failed"
    assert machine.transitions == 4


def test_transitions_counts_only_actual_state_changes() -> None:
    machine = HealthStateMachine(enter_after=1, recover_after=1)
    machine.update("failed", ["a"])
    machine.update("failed", ["b"])
    machine.update("healthy", [])
    machine.update("healthy", [])
    assert machine.transitions == 2


def test_recover_after_property_is_exposed() -> None:
    assert HealthStateMachine(recover_after=5).recover_after == 5


def test_gate_blocks_without_a_configured_command() -> None:
    controller = RestartController("", cooldown=300.0)
    assert controller.gate("failed", 0.0) == (False, "restart command not configured")


def test_gate_blocks_while_healthy() -> None:
    controller = RestartController("systemctl restart readsb", cooldown=300.0)
    assert controller.gate("healthy", 0.0) == (False, "state is healthy")
    assert controller.gate("degraded", 0.0) == (True, "")


def test_first_failure_runs_the_restart_once() -> None:
    calls: list[str] = []
    controller = RestartController(
        "restart-decoder", cooldown=300.0, runner=lambda command: calls.append(command) or 0
    )
    outcome = controller.attempt("failed", 100.0)
    assert outcome is not None
    assert outcome.ok is True
    assert outcome.command == "restart-decoder"
    assert outcome.returncode == 0
    assert outcome.ts == 100.0
    assert outcome.detail == ""
    assert calls == ["restart-decoder"]
    assert controller.restarts == 1
    assert controller.last_restart_at == 100.0


def test_health_must_recover_before_the_next_restart_is_allowed() -> None:
    controller = RestartController("cmd", cooldown=0.0, runner=lambda command: 0)
    assert controller.attempt("failed", 100.0) is not None
    allowed, reason = controller.gate("failed", 500.0)
    assert allowed is False
    assert reason == "awaiting healthy confirmation since last restart"
    assert controller.attempt("failed", 500.0) is None
    controller.observe("degraded")
    assert controller.gate("failed", 500.0)[0] is False
    controller.observe("healthy")
    assert controller.gate("failed", 500.0) == (True, "")


def test_cooldown_blocks_until_it_elapses() -> None:
    controller = RestartController("cmd", cooldown=300.0, runner=lambda command: 0)
    controller.attempt("failed", 100.0)
    controller.observe("healthy")
    allowed, reason = controller.gate("failed", 200.0)
    assert allowed is False
    assert reason == "cooldown active (200s remaining)"
    assert controller.attempt("failed", 399.9) is None
    assert controller.gate("failed", 400.0) == (True, "")


def test_failing_restart_command_still_starts_the_cooldown() -> None:
    controller = RestartController("cmd", cooldown=60.0, runner=lambda command: 3)
    outcome = controller.attempt("failed", 10.0)
    assert outcome is not None
    assert outcome.ok is False
    assert outcome.returncode == 3
    assert outcome.detail == "exit 3"
    controller.observe("healthy")
    assert controller.gate("failed", 30.0) == (False, "cooldown active (40s remaining)")


def test_a_raising_runner_cannot_kill_the_watchdog() -> None:
    def boom(command: str) -> int:
        raise RuntimeError("boom")

    controller = RestartController("cmd", cooldown=60.0, runner=boom)
    outcome = controller.attempt("failed", 10.0)
    assert outcome is not None
    assert outcome.ok is False
    assert outcome.returncode is None
    assert outcome.detail == "RuntimeError: boom"
    assert controller.restarts == 1


def test_negative_cooldown_is_rejected() -> None:
    with pytest.raises(ValueError, match="cooldown"):
        RestartController("cmd", cooldown=-1.0)


def test_observe_of_a_bad_state_does_not_confirm() -> None:
    controller = RestartController("cmd", cooldown=0.0, runner=lambda command: 0)
    controller.attempt("failed", 0.0)
    controller.observe("degraded")
    assert controller.gate("failed", 1.0)[0] is False


def test_default_runner_executes_a_real_shell_command() -> None:
    healthy = RestartController("exit 0", cooldown=0.0)
    outcome = healthy.attempt("failed", 0.0)
    assert outcome is not None
    assert outcome.ok is True
    assert outcome.returncode == 0

    failing = RestartController("exit 7", cooldown=0.0)
    outcome = failing.attempt("failed", 0.0)
    assert outcome is not None
    assert outcome.ok is False
    assert outcome.returncode == 7
    assert outcome.detail == "exit 7"


def test_a_hanging_restart_command_is_abandoned_with_code_124(monkeypatch) -> None:
    def hang(command: str, **kwargs: object) -> NoReturn:
        raise subprocess.TimeoutExpired(command, 120)

    monkeypatch.setattr(subprocess, "run", hang)
    controller = RestartController("restart-decoder", cooldown=0.0)
    outcome = controller.attempt("failed", 0.0)
    assert outcome is not None
    assert outcome.ok is False
    assert outcome.returncode == 124
    assert outcome.detail == "exit 124"
