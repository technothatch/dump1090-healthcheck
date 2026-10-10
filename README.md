# dump1090-healthcheck

[![CI](https://github.com/technothatch/dump1090-healthcheck/actions/workflows/ci.yml/badge.svg)](https://github.com/technothatch/dump1090-healthcheck/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/technothatch/dump1090-healthcheck/blob/main/LICENSE)
[![Latest Release](https://img.shields.io/github/v/release/technothatch/dump1090-healthcheck)](https://github.com/technothatch/dump1090-healthcheck/releases)
[![Issues](https://img.shields.io/github/issues/technothatch/dump1090-healthcheck)](https://github.com/technothatch/dump1090-healthcheck/issues)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/technothatch/dump1090-healthcheck/pulls)
[![Code size](https://img.shields.io/github/languages/code-size/technothatch/dump1090-healthcheck)](https://github.com/technothatch/dump1090-healthcheck)
[![Last commit](https://img.shields.io/github/last-commit/technothatch/dump1090-healthcheck)](https://github.com/technothatch/dump1090-healthcheck/commits/main)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen.svg)](https://github.com/technothatch/dump1090-healthcheck)

A watchdog for [dump1090](https://github.com/flightaware/dump1090) and
[readsb](https://github.com/wiedehopf/readsb) ADS-B decoders that catches the
failures a process check never sees. A decoder can die *silently*: the HTTP
port still answers while `now` is frozen an hour ago, the message rate has
collapsed because the dongle dropped off, `stats.json` counts baseband blocks
being thrown away, or the JSON directory has filled the disk — all with the
service "up". `dump1090-healthcheck` polls the real documents
(`aircraft.json`, `stats.json`), evaluates freshness, message rate, aircraft
count sanity, blocks-dropped growth, disk space and TCP outputs, folds them
into a hysteretic `healthy | degraded | failed` verdict, and exits `0/1/2` so
the same command works as a Docker `HEALTHCHECK`, a Nagios/Icinga plugin and
a Prometheus textfile producer. One stdlib-only Python process, no runtime
dependencies.

## Features

- **Seven checks against the documents that matter** — HTTP reachability of
  `aircraft.json`, age of the `now` field (a frozen `now` means a hung
  decoder), message rate inside an expected band, aircraft count sanity,
  `stats.json` `blocks_dropped` growth per minute (handles both dump1090-fa's
  `<bucket>.local` nesting and flat readsb buckets), free disk on
  `--data-dir`, and optional TCP connects to the beast/SBS outputs
  (`30005`/`30003`).
- **Three states with reasons** — `healthy`, `degraded`, `failed`, each check
  contributing its own one-line explanation, e.g.
  `rate: message rate 0.2/s below minimum 1.0/s`.
- **Hysteresis** — leaving `healthy` is cheap (`--enter-after 1`), returning
  takes `--recover-after 2` consecutive good polls, so a flapping decoder
  cannot oscillate the verdict.
- **Exit codes `0/1/2`** — healthy / degraded / failed, matching the
  Nagios plugin convention, so the CLI doubles as a Docker `HEALTHCHECK` and
  a monitoring-plugin check with no wrapper.
- **Automatic restart action** — `--restart-cmd "systemctl restart readsb"`
  gated by a cooldown *and* a health confirmation: a restart only happens
  again after the decoder has been observed healthy once since the previous
  attempt, so a permanently dead decoder is restarted once per cooldown
  window instead of in a tight loop.
- **Three output formats** — `--format json` for humans and scripts,
  `--format nagios` (`--nagios`) as a single `TEXT | perfdata` plugin line
  with warn/crit thresholds, `--format prometheus` as a node_exporter
  textfile (`dump1090_healthcheck_state`, `dump1090_healthcheck_check_result`,
  `dump1090_healthcheck_messages_per_second`, …).
- **Optional webhook** — `--webhook URL` POSTs a JSON summary the moment the
  verdict enters `failed`.
- **Zero runtime dependencies** — `urllib.request`, `json`, `argparse`,
  `shutil` only. Fully typed (`py.typed`), 122 tests at 100% coverage.

## Install

```bash
pip install dump1090-healthcheck
```

Or from source:

```bash
git clone https://github.com/technothatch/dump1090-healthcheck.git
cd dump1090-healthcheck
pip install -e ".[dev]"
```

Requires Python 3.11+.

## Quickstart

The output below is verbatim from this machine; the healthy runs were
captured against a local stub decoder serving realistic documents on port
8080, the CRITICAL one with that decoder stopped. Substitute your own
decoder's URL (or set `DUMP1090_URL`).

**Everything the CLI knows how to do:**

```console
$ dump1090-healthcheck --help
usage: dump1090-healthcheck [-h] [--version] [--url URL] [--timeout TIMEOUT]
                            [--once] [--interval INTERVAL]
                            [--max-polls MAX_POLLS]
                            [--format {json,nagios,prometheus}] [--nagios]
                            [--max-now-age MAX_NOW_AGE] [--min-rate MIN_RATE]
                            [--max-rate MAX_RATE]
                            [--min-aircraft MIN_AIRCRAFT]
                            [--max-aircraft MAX_AIRCRAFT]
                            [--max-blocks-dropped-per-min MAX_BLOCKS_DROPPED_PER_MIN]
                            [--data-dir DATA_DIR]
                            [--min-disk-free-mb MIN_DISK_FREE_MB]
                            [--tcp HOST:PORT] [--enter-after ENTER_AFTER]
                            [--recover-after RECOVER_AFTER]
                            [--restart-cmd RESTART_CMD]
                            [--restart-cooldown RESTART_COOLDOWN]
                            [--webhook WEBHOOK]

Watchdog for dump1090 / readsb decoders: HTTP, freshness, message-rate,
aircraft-count, blocks-dropped, disk and TCP checks.

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  --url URL             aircraft.json URL (default: $DUMP1090_URL or
                        http://localhost:8080/data/aircraft.json)
  --timeout TIMEOUT     HTTP timeout seconds
  --once                evaluate once and exit
  --interval INTERVAL   poll interval seconds in loop mode
  --max-polls MAX_POLLS
                        stop after N polls (0 = run forever; used by tests)
  --format {json,nagios,prometheus}
                        output format (default json)
  --nagios              shorthand for --format nagios

freshness and rate:
  --max-now-age MAX_NOW_AGE
                        max age of the 'now' field, seconds
  --min-rate MIN_RATE   minimum messages/second
  --max-rate MAX_RATE   maximum messages/second

sanity checks:
  --min-aircraft MIN_AIRCRAFT
                        minimum aircraft count
  --max-aircraft MAX_AIRCRAFT
                        maximum aircraft count
  --max-blocks-dropped-per-min MAX_BLOCKS_DROPPED_PER_MIN
                        allowed blocks_dropped growth per minute
  --data-dir DATA_DIR   decoder data dir to check for free disk space
  --min-disk-free-mb MIN_DISK_FREE_MB
                        minimum free megabytes on --data-dir
  --tcp HOST:PORT       require a TCP connect to succeed (repeatable, e.g.
                        127.0.0.1:30005)

state machine and actions:
  --enter-after ENTER_AFTER
                        bad polls required to leave healthy
  --recover-after RECOVER_AFTER
                        healthy polls required to recover
  --restart-cmd RESTART_CMD
                        shell command to run when unhealthy (e.g. systemctl)
  --restart-cooldown RESTART_COOLDOWN
                        seconds between restart attempts
  --webhook WEBHOOK     POST a JSON alert here on entering failed

Exit codes: 0 healthy, 1 degraded, 2 failed — usable as a Docker HEALTHCHECK.
```

**Two polls six seconds apart — baseline first, real rate second:**

```console
$ dump1090-healthcheck --url http://127.0.0.1:8080/data/aircraft.json --format json --max-polls 2 --interval 6
{
  "state": "healthy",
  "reasons": [],
  "ts": 1791507148.8050632,
  "restarts": 0,
  "checks": [
    {
      "name": "reachable",
      "state": "healthy",
      "reason": "HTTP reachable",
      "value": null,
      "unit": ""
    },
    {
      "name": "freshness",
      "state": "healthy",
      "reason": "0.4s old",
      "value": 0.35436272621154785,
      "unit": "s"
    },
    {
      "name": "rate",
      "state": "healthy",
      "reason": "collecting rate baseline",
      "value": null,
      "unit": ""
    },
    {
      "name": "aircraft",
      "state": "healthy",
      "reason": "3 aircraft",
      "value": 3.0,
      "unit": ""
    },
    {
      "name": "blocks_dropped",
      "state": "healthy",
      "reason": "collecting baseline",
      "value": null,
      "unit": ""
    },
    {
      "name": "disk",
      "state": "healthy",
      "reason": "skipped: no --data-dir",
      "value": null,
      "unit": ""
    }
  ],
  "metrics": {
    "aircraft": 3.0,
    "now_age_seconds": 0.35436272621154785
  }
}
{
  "state": "healthy",
  "reasons": [],
  "ts": 1791507154.957442,
  "restarts": 0,
  "checks": [
    {
      "name": "reachable",
      "state": "healthy",
      "reason": "HTTP reachable",
      "value": null,
      "unit": ""
    },
    {
      "name": "freshness",
      "state": "healthy",
      "reason": "0.5s old",
      "value": 0.4974334239959717,
      "unit": "s"
    },
    {
      "name": "rate",
      "state": "healthy",
      "reason": "19.5 msg/s",
      "value": 19.504650794283748,
      "unit": "msg/s"
    },
    {
      "name": "aircraft",
      "state": "healthy",
      "reason": "3 aircraft",
      "value": 3.0,
      "unit": ""
    },
    {
      "name": "blocks_dropped",
      "state": "healthy",
      "reason": "0.0 blocks/min",
      "value": 0.0,
      "unit": "blocks/min"
    },
    {
      "name": "disk",
      "state": "healthy",
      "reason": "skipped: no --data-dir",
      "value": null,
      "unit": ""
    }
  ],
  "metrics": {
    "aircraft": 3.0,
    "blocks_dropped_per_minute": 0.0,
    "messages_per_second": 19.504650794283748,
    "now_age_seconds": 0.4974334239959717
  }
}
[exit 0]
```

The first poll only collects a rate baseline (a single sample can't produce a
rate); the second already reports `19.5 msg/s` and `0.0 blocks/min`.

**As a Nagios/Icinga plugin:**

```console
$ dump1090-healthcheck --url http://127.0.0.1:8080/data/aircraft.json --nagios --once
dump1090 OK - all checks passed | state=0;1;2 aircraft=3;0;500 now_age_seconds=0.3728768825531006;;15
[exit 0]
```

**…and with the decoder down:**

```console
$ dump1090-healthcheck --url http://127.0.0.1:8080/data/aircraft.json --nagios --once
dump1090 CRITICAL - reachable: http://127.0.0.1:8080/data/aircraft.json: <urlopen error [WinError 10061] No connection could be made because the target machine actively refused it> | state=2;1;2
[exit 2]
```

### Exit codes

| Code | Verdict | Meaning |
| --- | --- | --- |
| `0` | `healthy` | every check passed (checks that were skipped are reported as healthy) |
| `1` | `degraded` | something is wrong but data still flows — message rate low/high, aircraft count out of band, blocks dropped too fast, disk low, TCP output refusing connections |
| `2` | `failed` | the decoder is unusable — `aircraft.json` unreachable or stale, `now` frozen/not numeric |

## Configuration reference

All flags can be given on one command line; environment variables are shown
in the *Environment* column.

| Flag | Environment | Meaning | Default |
| --- | --- | --- | --- |
| `--url URL` | `DUMP1090_URL` | `aircraft.json` URL to poll | `http://localhost:8080/data/aircraft.json` |
| `--timeout SECS` | — | HTTP timeout per request | `5.0` |
| `--once` | — | evaluate once and exit (instead of looping) | off |
| `--interval SECS` | — | seconds between polls in loop mode | `10.0` |
| `--max-polls N` | — | stop after N polls (`0` = run forever) | `0` |
| `--format {json,nagios,prometheus}` | — | output format | `json` |
| `--nagios` | — | shorthand for `--format nagios` | off |
| `--max-now-age SECS` | — | max age of the `now` field before `failed` | `15.0` |
| `--min-rate RATE` | — | minimum message rate (msg/s) | `1.0` |
| `--max-rate RATE` | — | maximum message rate (msg/s) | `5000.0` |
| `--min-aircraft N` | — | minimum aircraft count | `0` |
| `--max-aircraft N` | — | maximum aircraft count | `500` |
| `--max-blocks-dropped-per-min N` | — | allowed `blocks_dropped` growth per minute | `120.0` |
| `--data-dir PATH` | — | decoder data directory for the disk check | off (check skipped) |
| `--min-disk-free-mb MB` | — | minimum free space on `--data-dir` | `100` |
| `--tcp HOST:PORT` | — | require a TCP connect to succeed (repeatable, e.g. `127.0.0.1:30005`) | none |
| `--enter-after N` | — | consecutive bad polls required to leave `healthy` | `1` |
| `--recover-after N` | — | consecutive healthy polls required to recover | `2` |
| `--restart-cmd CMD` | — | shell command run when unhealthy | off (restarts disabled) |
| `--restart-cooldown SECS` | — | minimum seconds between restart attempts | `300.0` |
| `--webhook URL` | — | POST a JSON alert here when entering `failed` | off |
| `--version` | — | print version and exit | — |

## How it works

```
        every --interval seconds (or a single evaluation with --once)
 ┌──────────────────────────────────────────────────────────────────────┐
 │ runner.py — the only module that performs I/O                        │
 │   GET /data/aircraft.json   GET /data/stats.json                     │
 │   connect 127.0.0.1:30005 / 30003 (if --tcp)   disk_usage(--data-dir)│
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ Observation (pure data)
 ┌──────────────────────────────▼───────────────────────────────────────┐
 │ checks.py — pure functions, no I/O, no clock                         │
 │   reachable · freshness · rate · aircraft · blocks_dropped · disk    │
 │   tcp:host:port …            → CheckResult per check + worst()       │
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ raw verdict + reasons
 ┌──────────────────────────────▼───────────────────────────────────────┐
 │ state.py — hysteretic state machine + restart controller             │
 │   healthy │ degraded │ failed          (enter_after / recover_after) │
 │   RestartController: cooldown + health confirmation                  │
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ Evaluation
 ┌──────────────────────────────▼───────────────────────────────────────┐
 │ output.py — json │ nagios │ prometheus textfile   → exit 0 / 1 / 2   │
 └──────────────────────────────────────────────────────────────────────┘
```

- **Checks are pure.** Every decision in `checks.py` is a function of an
  `Observation` plus a `CheckConfig`, which is why the whole check matrix is
  unit-tested from fixtures without a decoder, network or disk.
- **Rates come from a windowed counter.** `RateTracker` reports units/second
  only once `rate_min_span` (5 s) separates the oldest and newest sample, so
  the first poll never shows a wild number; samples older than
  `rate_window` (60 s) are evicted (keeping one anchor), and a counter that
  goes *backwards* — the decoder restarted — drops the history and starts a
  fresh window instead of reporting a negative spike.
- **The state machine is deliberately lopsided.** A single bad poll is
  usually a real problem for a decoder, so `--enter-after` defaults to `1`;
  recovery needs `--recover-after 2` consecutive healthy polls and reports
  `awaiting recovery: 1/2 consecutive healthy checks` until then. Escalating
  (`degraded` → `failed`) or improving between the two bad states happens on
  the spot — only the final step back to `healthy` is gated.
- **Restarts are gated twice.** `--restart-cmd` runs on the first bad poll,
  then requires (1) a `healthy` observation since the previous attempt —
  restarting a decoder that never came back is pointless — and (2) the
  `--restart-cooldown` (default 300 s) to have elapsed. A failing restart
  command still starts the cooldown, so it cannot spin.

### Docker `HEALTHCHECK`

Exit code `0` keeps the container `healthy`, `1`/`2` mark it `unhealthy`
(`|| exit 1` normalises the verdict for Docker):

```dockerfile
FROM python:3.12-slim
RUN pip install dump1090-healthcheck
# ... your decoder image ...
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD dump1090-healthcheck --once --nagios || exit 1
```

Or from `compose.yaml`:

```yaml
services:
  readsb:
    healthcheck:
      test: ["CMD", "dump1090-healthcheck", "--once"]
      interval: 30s
      timeout: 10s
      retries: 3
```

### Nagios / Icinga plugin

`--nagios` prints one line of `TEXT | perfdata` with per-metric `warn;crit`
columns taken from the configured thresholds, and the exit code is the
plugin state (`0` OK, `1` WARNING, `2` CRITICAL):

```
define command {
    command_name    check_dump1090
    command_line    $USER1$/dump1090-healthcheck --nagios --url http://localhost:8080/data/aircraft.json --tcp 127.0.0.1:30005
}
```

### Prometheus / node_exporter textfile

`--format prometheus` emits exposition-format text with the metrics
`dump1090_healthcheck_state`, `dump1090_healthcheck_check_result{check=…}`,
`dump1090_healthcheck_messages_per_second`, `dump1090_healthcheck_aircraft`,
`dump1090_healthcheck_now_age_seconds`,
`dump1090_healthcheck_blocks_dropped_per_minute`,
`dump1090_healthcheck_disk_free_bytes` and
`dump1090_healthcheck_timestamp_seconds`. Point the node_exporter
`textfile` collector at a directory and write atomically:

```bash
#!/bin/sh
set -e
dir=/var/lib/node_exporter/textfile
dump1090-healthcheck --once --format prometheus > "$dir/dump1090_healthcheck.prom.tmp"
mv "$dir/dump1090_healthcheck.prom.tmp" "$dir/dump1090_healthcheck.prom"
```

## Development

```bash
git clone https://github.com/technothatch/dump1090-healthcheck.git
cd dump1090-healthcheck
pip install -e ".[dev]"

make lint     # ruff check + ruff format --check + mypy
make test     # pytest with coverage (fail-under 80)
make check    # everything, the same gate CI runs
```

No `make`? The underlying commands are:

```bash
ruff check .
ruff format --check .
mypy src
python -m pytest -q --cov --cov-report=term-missing --cov-fail-under=80
```

The suite — 122 tests at 100% line and branch coverage — covers the state
machine's enter/recover hysteresis, rate-window math (min-span gating,
window eviction, counter resets), the restart cooldown and health
confirmation, exact Nagios/Prometheus/JSON renderings, every check's
pass/fail matrix built from `Observation` fixtures, and the CLI end to end
against a threaded stdlib stub decoder on an ephemeral port (exit codes
`0/1/2`, `--nagios`, `--format` dispatch, webhooks, restart gating).
**No test contacts a real decoder**; every document is written inline.

## Related projects

- [dump1090-fa](https://github.com/flightaware/dump1090) — FlightAware's decoder
- [readsb](https://github.com/wiedehopf/readsb) — high-performance fork, JSON docs in-repo
- [tar1090](https://github.com/wiedehopf/tar1090) — web UI for readsb
- [graphs1090](https://github.com/wiedehopf/graphs1090) — statistics graphs
- [dump1090-alerts](https://github.com/technothatch/dump1090-alerts) — rule engine over `aircraft.json`
- [dump1090-exporter](https://github.com/technothatch/dump1090-exporter) — Prometheus exporter for decoder internals
- [dump1090-replay](https://github.com/technothatch/dump1090-replay) — replay recorded `aircraft.json` history
- [dump1090-simulator](https://github.com/technothatch/dump1090-simulator) — synthetic traffic generator

## License

MIT © technothatch. See [LICENSE](LICENSE).
