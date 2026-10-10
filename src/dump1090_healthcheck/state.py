"""Health state machine and restart control for the dump1090 healthcheck.

The state machine is where the raw result of every check is turned into the
single ``healthy`` / ``degraded`` / ``failed`` verdict that the CLI exits with.
It applies hysteresis: entering a bad state is deliberately cheap (a single bad
poll is usually a real problem for a decoder), while leaving one requires
``recover_after`` consecutive healthy polls so a flapping decoder cannot
oscillate between states.

The restart controller implements the "restart only when it can help" policy:
a restart needs a healthy confirmation since the previous restart *and* the
cooldown to have elapsed, so a permanently dead decoder is restarted once per
cooldown window instead of in a tight loop.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

State = Literal["healthy", "degraded", "failed"]

STATE_ORDER: dict[str, int] = {"healthy": 0, "degraded": 1, "failed": 2}
EXIT_CODES: dict[str, int] = {"healthy": 0, "degraded": 1, "failed": 2}
NAGIOS_STATES: dict[str, str] = {"healthy": "OK", "degraded": "WARNING", "failed": "CRITICAL"}


def worst(*states: State) -> State:
    """Return the most severe of ``states`` (failed > degraded > healthy)."""
    if not states:
        return "healthy"
    return max(states, key=lambda state: STATE_ORDER[state])


def exit_code(state: State) -> int:
    """Process exit code for a verdict: 0 healthy, 1 degraded, 2 failed."""
    return EXIT_CODES[state]


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of a single check.

    Args:
        name: Stable check identifier (``reachable``, ``freshness``, ...).
        state: What this check contributes to the overall verdict.
        reason: One-line human explanation, safe to show in Nagios output.
        value: Numeric reading when the check produced one; ``None`` for
            checks that are binary or were skipped.
        unit: Unit of ``value`` for perfdata (``s``, ``bytes``, ...).
        metric: Prometheus/Nagios metric slug for ``value``; empty when the
            reading has no numeric metric.
    """

    name: str
    state: State
    reason: str
    value: float | None = None
    unit: str = ""
    metric: str = ""


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Everything one poll produced, ready to render in any output format."""

    state: State
    reasons: tuple[str, ...]
    results: tuple[CheckResult, ...]
    metrics: dict[str, float]
    thresholds: dict[str, tuple[str, str]]
    ts: float
    restarts: int = 0


class HealthStateMachine:
    """Convert raw per-poll verdicts into a hysteretic reported state.

    Args:
        enter_after: Consecutive bad polls required to leave ``healthy``.
            ``1`` (the default) reports trouble immediately.
        recover_after: Consecutive healthy polls required to return to
            ``healthy`` once bad. While waiting, the reported state stays bad
            with an ``awaiting recovery`` reason.
    """

    def __init__(self, *, enter_after: int = 1, recover_after: int = 2) -> None:
        if enter_after < 1:
            raise ValueError("enter_after must be >= 1")
        if recover_after < 1:
            raise ValueError("recover_after must be >= 1")
        self._enter_after = enter_after
        self._recover_after = recover_after
        self._state: State = "healthy"
        self._reasons: tuple[str, ...] = ()
        self._bad_streak = 0
        self._healthy_streak = 0
        self.transitions = 0

    @property
    def state(self) -> State:
        """Current reported state."""
        return self._state

    @property
    def reasons(self) -> tuple[str, ...]:
        """Reasons backing the current reported state."""
        return self._reasons

    @property
    def recover_after(self) -> int:
        """The configured recovery threshold."""
        return self._recover_after

    def _set(self, state: State, reasons: tuple[str, ...]) -> None:
        if state != self._state:
            self._state = state
            self.transitions += 1
        self._reasons = reasons

    def update(self, raw: State, reasons: Sequence[str]) -> State:
        """Fold one raw verdict into the machine.

        Args:
            raw: Combined verdict of all checks for this poll.
            reasons: Non-empty reason strings for a bad ``raw`` verdict.

        Returns:
            The reported state after folding this poll in.
        """
        if raw == "healthy":
            self._healthy_streak += 1
            self._bad_streak = 0
            if self._state == "healthy":
                self._reasons = ()
            elif self._healthy_streak >= self._recover_after:
                self._set("healthy", ())
            else:
                self._reasons = (
                    f"awaiting recovery: {self._healthy_streak}/{self._recover_after} "
                    "consecutive healthy checks",
                )
            return self._state
        self._bad_streak += 1
        self._healthy_streak = 0
        if self._state == "healthy":
            if self._bad_streak >= self._enter_after:
                self._set(raw, tuple(reasons))
        else:
            # Escalating (or improving to a less severe bad state) happens on
            # the poll that produced it; only the final step to healthy is gated.
            self._set(raw, tuple(reasons))
        return self._state


@dataclass(frozen=True, slots=True)
class RestartOutcome:
    """Record of one executed restart attempt."""

    command: str
    ts: float
    ok: bool
    returncode: int | None
    detail: str


def _run_command(command: str) -> int:
    """Run a restart command through the shell and return its exit code."""
    try:
        completed = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=120, check=False
        )
    except subprocess.TimeoutExpired:
        return 124
    return completed.returncode


class RestartController:
    """Gate restart attempts behind a cooldown and a health confirmation.

    Two conditions must hold before a command runs:

    1. Health has been observed as ``healthy`` at least once since the last
       restart (the *confirmation* gate) — restarting a decoder that never
       came back is pointless, so the very first failure restarts once and
       then waits.
    2. ``cooldown`` seconds have passed since the previous attempt, which
       bounds the restart rate even if health briefly recovers in between.

    Args:
        command: Shell command to run; empty disables restarting entirely.
        cooldown: Minimum seconds between attempts.
        runner: Injectable command runner (returns an exit code); defaults to
            :func:`_run_command`.
    """

    def __init__(
        self,
        command: str = "",
        *,
        cooldown: float = 300.0,
        runner: Callable[[str], int] | None = None,
    ) -> None:
        if cooldown < 0:
            raise ValueError("cooldown must be >= 0")
        self.command = command
        self.cooldown = cooldown
        self._runner = runner if runner is not None else _run_command
        self.last_restart_at: float | None = None
        self.restarts = 0
        self._confirmed = True

    def observe(self, state: State) -> None:
        """Record that a verdict was reported; healthy unlocks the next restart."""
        if state == "healthy":
            self._confirmed = True

    def gate(self, state: State, now: float) -> tuple[bool, str]:
        """Return ``(allowed, reason_when_blocked)`` for a restart right now."""
        if not self.command:
            return False, "restart command not configured"
        if state == "healthy":
            return False, "state is healthy"
        if not self._confirmed:
            return False, "awaiting healthy confirmation since last restart"
        if self.last_restart_at is not None:
            remaining = self.cooldown - (now - self.last_restart_at)
            if remaining > 0:
                return False, f"cooldown active ({remaining:.0f}s remaining)"
        return True, ""

    def attempt(self, state: State, now: float) -> RestartOutcome | None:
        """Run the restart command if the gates allow it, else return ``None``.

        The cooldown timer starts even when the command fails, because a
        failing restart command will keep failing and must not spin.
        """
        allowed, _reason = self.gate(state, now)
        if not allowed:
            return None
        self.last_restart_at = now
        self.restarts += 1
        self._confirmed = False
        try:
            returncode: int | None = self._runner(self.command)
        except Exception as exc:  # a broken runner must not kill the watchdog
            return RestartOutcome(self.command, now, False, None, f"{type(exc).__name__}: {exc}")
        return RestartOutcome(
            self.command,
            now,
            returncode == 0,
            returncode,
            "" if returncode == 0 else f"exit {returncode}",
        )
