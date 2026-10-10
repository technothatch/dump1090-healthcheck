"""Render an :class:`Evaluation` as JSON, a Nagios/Icinga plugin line or a
Prometheus textfile for node_exporter's ``textfile`` collector.

All three formats are pure string builders — exact-output tests pin them.
"""

from __future__ import annotations

import json
import re

from dump1090_healthcheck.state import NAGIOS_STATES, STATE_ORDER, Evaluation

_METRIC_HELP = {
    "messages_per_second": "Mode-S messages per second observed at the decoder.",
    "aircraft": "Aircraft present in the latest aircraft.json.",
    "now_age_seconds": "Age of the decoder's now field in seconds.",
    "blocks_dropped_per_minute": "Baseband blocks dropped per minute.",
    "disk_free_bytes": "Free bytes on the filesystem holding --data-dir.",
}

_SAFE_METRIC_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def render_json(ev: Evaluation) -> str:
    """Pretty JSON with state, reasons, per-check results and metrics."""
    payload = {
        "state": ev.state,
        "reasons": list(ev.reasons),
        "ts": ev.ts,
        "restarts": ev.restarts,
        "checks": [
            {
                "name": item.name,
                "state": item.state,
                "reason": item.reason,
                "value": item.value,
                "unit": item.unit,
            }
            for item in ev.results
        ],
        "metrics": dict(sorted(ev.metrics.items())),
    }
    return json.dumps(payload, indent=2)


def render_nagios(ev: Evaluation) -> str:
    """Single-line Nagios/Icinga plugin output: ``TEXT - detail | perfdata``."""
    label = NAGIOS_STATES[ev.state]
    detail = "; ".join(ev.reasons) if ev.reasons else "all checks passed"
    parts = [f"state={STATE_ORDER[ev.state]};1;2"]
    for name in sorted(ev.metrics):
        warn, crit = ev.thresholds.get(name, ("", ""))
        parts.append(f"{name}={_number(ev.metrics[name])};{warn};{crit}")
    return f"dump1090 {label} - {detail} | {' '.join(parts)}"


def render_prometheus(ev: Evaluation) -> str:
    """Prometheus text exposition format, one metric family per line group."""
    lines = [
        "# HELP dump1090_healthcheck_state Verdict: 0 healthy, 1 degraded, 2 failed.",
        "# TYPE dump1090_healthcheck_state gauge",
        f"dump1090_healthcheck_state {STATE_ORDER[ev.state]}",
        "# HELP dump1090_healthcheck_check_result "
        "Per-check verdict: 0 healthy, 1 degraded, 2 failed.",
        "# TYPE dump1090_healthcheck_check_result gauge",
    ]
    for item in sorted(ev.results, key=lambda result: result.name):
        label = item.name.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(
            f'dump1090_healthcheck_check_result{{check="{label}"}} {STATE_ORDER[item.state]}'
        )
    for name in sorted(ev.metrics):
        if not _SAFE_METRIC_RE.match(name):
            continue
        lines.append(f"# HELP dump1090_healthcheck_{name} {_METRIC_HELP.get(name, name + '.')}")
        lines.append(f"# TYPE dump1090_healthcheck_{name} gauge")
        lines.append(f"dump1090_healthcheck_{name} {_number(ev.metrics[name])}")
    lines.append("# HELP dump1090_healthcheck_timestamp_seconds Time of this evaluation.")
    lines.append("# TYPE dump1090_healthcheck_timestamp_seconds gauge")
    lines.append(f"dump1090_healthcheck_timestamp_seconds {_number(ev.ts)}")
    return "\n".join(lines) + "\n"


def _number(value: float) -> str:
    """Format a float the way both Nagios and Prometheus expect it."""
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)
