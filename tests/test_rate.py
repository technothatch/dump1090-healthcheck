"""Rate-window math: min-span gating, window eviction and counter resets."""

from __future__ import annotations

import pytest

from dump1090_healthcheck.checks import RateTracker


def test_window_and_min_span_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        RateTracker(window=0.0, min_span=5.0)
    with pytest.raises(ValueError, match="positive"):
        RateTracker(window=60.0, min_span=0.0)
    with pytest.raises(ValueError, match="positive"):
        RateTracker(window=-1.0, min_span=-1.0)


def test_first_poll_never_produces_a_rate() -> None:
    tracker = RateTracker(window=60.0, min_span=5.0)
    assert tracker.sample(1000.0, 0.0) is None


def test_rate_only_appears_once_min_span_separates_the_samples() -> None:
    tracker = RateTracker(window=60.0, min_span=5.0)
    assert tracker.sample(1000.0, 0.0) is None
    assert tracker.sample(1100.0, 4.9) is None
    assert tracker.sample(1200.0, 9.9) == pytest.approx(200.0 / 9.9)


def test_constant_counter_yields_zero_rate() -> None:
    tracker = RateTracker(window=60.0, min_span=5.0)
    tracker.sample(500.0, 0.0)
    assert tracker.sample(500.0, 5.0) == 0.0


def test_rate_exactly_at_min_span_counts() -> None:
    tracker = RateTracker(window=60.0, min_span=5.0)
    tracker.sample(100.0, 0.0)
    assert tracker.sample(200.0, 5.0) == 20.0


def test_samples_outside_the_window_are_evicted_keeping_one_anchor() -> None:
    tracker = RateTracker(window=60.0, min_span=5.0)
    # A burst early on, then a flat counter: only the recent window may count.
    tracker.sample(0.0, 0.0)
    for ts in range(10, 101, 10):
        tracker.sample(1000.0, float(ts))
    # Full-span slope would be (1000 - 0) / 100 = 10.0; the window says 0.0.
    assert tracker.sample(1000.0, 100.0) == 0.0


def test_spike_leaves_the_window_once_it_is_old_enough() -> None:
    tracker = RateTracker(window=60.0, min_span=5.0)
    tracker.sample(0.0, 0.0)
    tracker.sample(5000.0, 5.0)
    # The burst's start still anchors the window: 5000 messages over 60s.
    assert tracker.sample(5000.0, 60.0) == pytest.approx(5000.0 / 60.0)
    # Once t=0 is evicted the window only sees the flat tail: 0/s.
    assert tracker.sample(5000.0, 65.0) == 0.0


def test_counter_going_backwards_starts_a_fresh_window() -> None:
    tracker = RateTracker(window=60.0, min_span=5.0)
    tracker.sample(1000.0, 0.0)
    assert tracker.sample(1500.0, 5.0) == 100.0
    # The decoder restarted: history is dropped, so no bogus negative rate.
    assert tracker.sample(10.0, 6.0) is None
    assert tracker.sample(60.0, 11.0) == 10.0
    assert tracker.sample(5.0, 12.0) is None


def test_reset_forgets_every_sample() -> None:
    tracker = RateTracker(window=60.0, min_span=5.0)
    tracker.sample(100.0, 0.0)
    tracker.sample(200.0, 10.0)
    tracker.reset()
    assert tracker.sample(300.0, 20.0) is None
    assert tracker.sample(400.0, 30.0) == pytest.approx(100.0 / 10.0)
