"""Shared test fixtures: inline ADS-B payloads and a threaded stub decoder.

No test ever talks to a real decoder — every document is written inline and
served from a stdlib ``ThreadingHTTPServer`` bound to an ephemeral port.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from dump1090_healthcheck.checks import Observation

#: 2026-10-03 12:00:00 UTC — the same epoch family as the sibling repos.
T0 = 1791028800.0


def aircraft(**fields: Any) -> dict[str, Any]:
    """One realistic aircraft object (SPEC.md field list subset)."""
    base: dict[str, Any] = {
        "hex": "4ca918",
        "flight": "RYR22GK ",
        "type": "B738",
        "alt_baro": 34000,
        "gs": 458,
        "track": 210.5,
        "squawk": "1200",
        "emergency": "none",
        "lat": 51.4712,
        "lon": -0.4543,
        "seen": 0.5,
        "rssi": -4.2,
        "messages": 100,
    }
    base.update(fields)
    return base


def payload(**overrides: Any) -> dict[str, Any]:
    """A healthy ``aircraft.json`` document; override any field per test."""
    base: dict[str, Any] = {
        "now": T0,
        "messages": 1000,
        "aircraft": [aircraft(), aircraft(hex="40621d", flight="BAW117 "), aircraft(hex="3c673a")],
    }
    base.update(overrides)
    return base


def served_payload(**overrides: Any) -> dict[str, Any]:
    """A healthy payload without ``now``, so the stub serves wall-clock time.

    CLI tests run against the real clock; only documents whose ``now`` is
    missing or fresh can pass the freshness check there.
    """
    doc = payload(**overrides)
    doc.pop("now", None)
    return doc


def stats_doc(*, dropped: float = 0, processed: float = 918273) -> dict[str, Any]:
    """A realistic ``stats.json`` with dump1090-fa's ``<bucket>.local`` nesting."""
    bucket = {
        "local": {
            "blocks_processed": processed,
            "blocks_dropped": dropped,
            "modes": 41230,
            "bad": 118,
            "unknown_icao": 12,
            "signal": -3.2,
            "peak_signal": 0.4,
            "strong_signals": 8,
        },
        "remote": {},
        "cpu": {"demod": 12.5, "reader": 4.1, "background": 1.7},
        "cpr": {"recent": 4, "forward": 1, "single": 0, "rejected": 0},
        "tracks": {"all": 96, "single_message": 4},
        "messages": 47118,
    }
    return {
        "latest": dict(bucket),
        "last1min": dict(bucket),
        "last5min": dict(bucket),
        "last15min": dict(bucket),
        "total": dict(bucket),
    }


def observation(**overrides: Any) -> Observation:
    """An :class:`Observation` defaulting to a perfectly healthy poll."""
    defaults: dict[str, Any] = {
        "ts": T0,
        "payload": payload(),
        "payload_error": "",
        "stats": stats_doc(),
        "stats_error": "",
        "tcp": {},
        "disk_free": None,
        "disk_total": None,
        "disk_error": "",
    }
    defaults.update(overrides)
    return Observation(**defaults)


def closed_port() -> int:
    """A TCP port that is (almost certainly) closed right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return port


class DecoderStub:
    """Threaded HTTP stub serving fixture documents on an ephemeral port.

    ``aircraft_seq`` is consumed one entry per ``aircraft.json`` request (the
    last entry repeats), which is how tests script per-poll decoder health.
    A document without an explicit ``now`` gets wall time at serve time, so
    the freshness check sees fresh data.
    """

    def __init__(
        self,
        *,
        aircraft_seq: list[dict[str, Any]] | None = None,
        stats: dict[str, Any] | None = None,
        raw: dict[str, bytes] | None = None,
    ) -> None:
        self.aircraft_seq = aircraft_seq if aircraft_seq is not None else [served_payload()]
        self.stats_doc = stats if stats is not None else stats_doc()
        self.raw = raw if raw is not None else {}
        self.aircraft_requests = 0
        self.requests: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                stub._serve(self)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                if self.path.endswith("/hook"):
                    stub._send(self, b"")
                else:
                    self.send_error(404)

            def log_message(self, *args: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    @property
    def url(self) -> str:
        return f"{self.origin}/data/aircraft.json"

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        path = handler.path
        self.requests.append(path)
        if path in self.raw:
            self._send(handler, self.raw[path])
            return
        if path.endswith("aircraft.json"):
            index = min(self.aircraft_requests, len(self.aircraft_seq) - 1)
            self.aircraft_requests += 1
            doc = dict(self.aircraft_seq[index])
            doc.setdefault("now", time.time())
            body = json.dumps(doc).encode("utf-8")
        elif path.endswith("stats.json"):
            body = json.dumps(self.stats_doc).encode("utf-8")
        else:
            handler.send_error(404)
            return
        self._send(handler, body)

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, body: bytes) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def make_stub() -> Iterator[Callable[..., DecoderStub]]:
    """Factory for :class:`DecoderStub` instances, all closed after the test."""
    created: list[DecoderStub] = []

    def factory(**kwargs: Any) -> DecoderStub:
        stub = DecoderStub(**kwargs)
        created.append(stub)
        return stub

    yield factory
    for stub in created:
        stub.close()


@pytest.fixture
def stub(make_stub: Callable[..., DecoderStub]) -> DecoderStub:
    """A single healthy stub decoder."""
    return make_stub()
