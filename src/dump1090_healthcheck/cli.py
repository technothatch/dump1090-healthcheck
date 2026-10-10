"""Command-line interface for dump1090-healthcheck."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import replace

from dump1090_healthcheck import __version__
from dump1090_healthcheck.checks import DEFAULT_URL, CheckConfig
from dump1090_healthcheck.output import render_json, render_nagios, render_prometheus
from dump1090_healthcheck.runner import Healthcheck
from dump1090_healthcheck.state import Evaluation, RestartController, State, exit_code

EXIT_OK = 0

WebhookFn = Callable[[str, Evaluation], str | None]

_RENDERERS = {
    "json": render_json,
    "nagios": render_nagios,
    "prometheus": render_prometheus,
}


def _default_url() -> str:
    return os.environ.get("DUMP1090_URL", DEFAULT_URL)


def _parse_endpoint(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit() or not (0 < int(port) < 65536):
        raise argparse.ArgumentTypeError(f"expected host:port, got {value!r}")
    return host, int(port)


def post_webhook(url: str, ev: Evaluation, *, timeout: float = 5.0) -> str | None:
    """POST a JSON summary of a failed verdict; return an error string or ``None``."""
    body = json.dumps(
        {
            "state": ev.state,
            "reasons": list(ev.reasons),
            "ts": ev.ts,
            "restarts": ev.restarts,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return str(exc)
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dump1090-healthcheck",
        description=(
            "Watchdog for dump1090 / readsb decoders: HTTP, freshness, message-rate, "
            "aircraft-count, blocks-dropped, disk and TCP checks."
        ),
        epilog="Exit codes: 0 healthy, 1 degraded, 2 failed — usable as a Docker HEALTHCHECK.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--url",
        default=_default_url(),
        help=f"aircraft.json URL (default: $DUMP1090_URL or {DEFAULT_URL})",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout seconds")
    parser.add_argument("--once", action="store_true", help="evaluate once and exit")
    parser.add_argument(
        "--interval", type=float, default=10.0, help="poll interval seconds in loop mode"
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=0,
        help="stop after N polls (0 = run forever; used by tests)",
    )
    parser.add_argument(
        "--format",
        choices=sorted(_RENDERERS),
        default="json",
        help="output format (default json)",
    )
    parser.add_argument("--nagios", action="store_true", help="shorthand for --format nagios")

    freshness = parser.add_argument_group("freshness and rate")
    freshness.add_argument(
        "--max-now-age", type=float, default=15.0, help="max age of the 'now' field, seconds"
    )
    freshness.add_argument("--min-rate", type=float, default=1.0, help="minimum messages/second")
    freshness.add_argument("--max-rate", type=float, default=5000.0, help="maximum messages/second")

    sanity = parser.add_argument_group("sanity checks")
    sanity.add_argument("--min-aircraft", type=int, default=0, help="minimum aircraft count")
    sanity.add_argument("--max-aircraft", type=int, default=500, help="maximum aircraft count")
    sanity.add_argument(
        "--max-blocks-dropped-per-min",
        type=float,
        default=120.0,
        help="allowed blocks_dropped growth per minute",
    )
    sanity.add_argument(
        "--data-dir", default="", help="decoder data dir to check for free disk space"
    )
    sanity.add_argument(
        "--min-disk-free-mb",
        type=int,
        default=100,
        help="minimum free megabytes on --data-dir",
    )
    sanity.add_argument(
        "--tcp",
        action="append",
        default=[],
        type=_parse_endpoint,
        metavar="HOST:PORT",
        help="require a TCP connect to succeed (repeatable, e.g. 127.0.0.1:30005)",
    )

    state = parser.add_argument_group("state machine and actions")
    state.add_argument(
        "--enter-after", type=int, default=1, help="bad polls required to leave healthy"
    )
    state.add_argument(
        "--recover-after", type=int, default=2, help="healthy polls required to recover"
    )
    state.add_argument(
        "--restart-cmd", default="", help="shell command to run when unhealthy (e.g. systemctl)"
    )
    state.add_argument(
        "--restart-cooldown", type=float, default=300.0, help="seconds between restart attempts"
    )
    state.add_argument("--webhook", default="", help="POST a JSON alert here on entering failed")
    return parser


def _config_from(args: argparse.Namespace) -> CheckConfig:
    return CheckConfig(
        url=args.url,
        timeout=args.timeout,
        max_now_age=args.max_now_age,
        min_rate=args.min_rate,
        max_rate=args.max_rate,
        min_aircraft=args.min_aircraft,
        max_aircraft=args.max_aircraft,
        max_blocks_dropped_per_min=args.max_blocks_dropped_per_min,
        data_dir=args.data_dir,
        min_disk_free_bytes=args.min_disk_free_mb * 1024 * 1024,
        tcp_endpoints=tuple(args.tcp),
        enter_after=args.enter_after,
        recover_after=args.recover_after,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    webhook: WebhookFn | None = None,
) -> int:
    """Entry point. Returns a process exit code (0/1/2 by verdict)."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    renderer = render_nagios if args.nagios else _RENDERERS[args.format]

    healthcheck = Healthcheck(_config_from(args))
    controller = RestartController(args.restart_cmd, cooldown=args.restart_cooldown)
    post = (
        webhook
        if webhook is not None
        else (lambda url, ev: post_webhook(url, ev, timeout=args.timeout))
    )
    if args.webhook and webhook is None:
        post = lambda url, ev: post_webhook(url, ev, timeout=args.timeout)  # noqa: E731

    polls = 0
    last: Evaluation | None = None
    previous_state: State | None = None
    try:
        while True:
            ev = healthcheck.poll()
            controller.observe(ev.state)
            outcome = controller.attempt(ev.state, ev.ts)
            if outcome is not None:
                verdict = "ok" if outcome.ok else f"failed ({outcome.detail})"
                print(f"# restart attempt {controller.restarts}: {verdict}", file=sys.stderr)
            if args.webhook and ev.state == "failed" and previous_state != "failed":
                error = post(args.webhook, ev)
                if error is not None:
                    print(f"# webhook error: {error}", file=sys.stderr)
            previous_state = ev.state
            last = replace(ev, restarts=controller.restarts)
            print(renderer(last), flush=True)
            polls += 1
            if args.once or (args.max_polls and polls >= args.max_polls):
                break
            time.sleep(max(0.0, args.interval))
    except KeyboardInterrupt:
        print("\n# stopped", file=sys.stderr)
    if last is None:  # pragma: no cover - poll() always returns before this
        return 1
    return exit_code(last.state)


if __name__ == "__main__":
    sys.exit(main())
