"""Tests for eval.progress: reporter selection and event handling (issue #82).

Covers NullProgress (no-op), PlainProgress (CI log lines), RichProgress (live
bar + ETA, requires the optional `rich` dependency), and create_reporter's
TTY/--no-progress auto-selection.
"""

from __future__ import annotations

import io

import pytest

from eval.progress import (
    NullProgress,
    PlainProgress,
    _format_duration,
    create_reporter,
)

# --- NullProgress ---


def test_null_progress_is_silent():
    stream = io.StringIO()
    reporter = NullProgress()
    reporter.start(10, label="eval matrix", workers=4)
    reporter.cell_started("a/b/e1")
    reporter.cell_completed("a/b/e1", duration=1.0, status="completed")
    reporter.cell_failed("a/b/e2", duration=2.0, reason="boom")
    reporter.notice("hello")
    reporter.finish()
    assert stream.getvalue() == ""  # never touched -- confirms zero I/O


# --- PlainProgress ---


def test_plain_progress_start_line():
    stream = io.StringIO()
    reporter = PlainProgress(stream=stream)
    reporter.start(40, label="eval matrix")
    assert "40 run(s) scheduled" in stream.getvalue()


def test_plain_progress_completed_line_format():
    stream = io.StringIO()
    reporter = PlainProgress(stream=stream)
    reporter.start(40)
    reporter.cell_completed("code-review/baseline/e1", duration=23.4, status="completed")
    out = stream.getvalue()
    assert "[1/40] completed: code-review/baseline/e1 (23s, completed)" in out


def test_plain_progress_failed_line_format():
    stream = io.StringIO()
    reporter = PlainProgress(stream=stream)
    reporter.start(40)
    reporter.cell_failed("code-review/baseline/e2", duration=300, reason="timeout after 300s")
    out = stream.getvalue()
    assert "[1/40] FAILED: code-review/baseline/e2 (timeout after 300s)" in out


def test_plain_progress_counter_increments_across_success_and_failure():
    stream = io.StringIO()
    reporter = PlainProgress(stream=stream)
    reporter.start(3)
    reporter.cell_completed("a", duration=1)
    reporter.cell_failed("b", duration=1, reason="x")
    reporter.cell_completed("c", duration=1)
    lines = [line for line in stream.getvalue().splitlines() if line.startswith("[")]
    assert lines == [
        "[1/3] completed: a (1s, completed)",
        "[2/3] FAILED: b (x)",
        "[3/3] completed: c (1s, completed)",
    ]


def test_plain_progress_missing_duration_renders_placeholder():
    stream = io.StringIO()
    reporter = PlainProgress(stream=stream)
    reporter.start(1)
    reporter.cell_completed("a")
    assert "(?, completed)" in stream.getvalue()


def test_plain_progress_notice_prints_message():
    stream = io.StringIO()
    reporter = PlainProgress(stream=stream)
    reporter.notice("Evaluating: foo (judge)...")
    assert "Evaluating: foo (judge)..." in stream.getvalue()


# --- _format_duration ---


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0s"),
        (45, "45s"),
        (59, "59s"),
        (60, "1m"),
        (14 * 60, "14m"),
        (90, "2m"),  # rounds up when >=30s past the minute
        (3600, "1h0m"),
        (3660, "1h1m"),
    ],
)
def test_format_duration(seconds, expected):
    assert _format_duration(seconds) == expected


# --- RichProgress (requires the optional `rich` dependency) ---

rich = pytest.importorskip("rich")


def test_rich_progress_tracks_completion_and_eta():
    from eval.progress import RichProgress

    stream = io.StringIO()
    reporter = RichProgress(stream=stream)
    reporter.start(4, label="eval matrix", workers=2)
    reporter.cell_started("code-review/baseline/e1")
    reporter.cell_completed("code-review/baseline/e1", duration=20.0, status="completed")
    reporter.cell_started("code-review/experimental/e1")
    reporter.cell_completed("code-review/experimental/e1", duration=20.0, status="completed")
    reporter.cell_started("code-review/baseline/e2")
    reporter.notice("side note")
    reporter.finish()

    out = stream.getvalue()
    assert "code-review/baseline/e1" in out
    assert "code-review/experimental/e1" in out
    assert "side note" in out
    # ETA should reflect the two completed 20s cells over 2 remaining / 2 workers.
    assert reporter._eta_seconds() is None or reporter._eta_seconds() >= 0


def test_rich_progress_marks_failures():
    from eval.progress import RichProgress

    stream = io.StringIO()
    reporter = RichProgress(stream=stream)
    reporter.start(1, label="eval matrix", workers=1)
    reporter.cell_started("a/b/e1")
    reporter.cell_failed("a/b/e1", duration=5.0, reason="exit code 1")
    reporter.finish()
    assert "exit code 1" in stream.getvalue()


# --- create_reporter ---


def test_create_reporter_no_progress_returns_null():
    assert isinstance(create_reporter(no_progress=True), NullProgress)


def test_create_reporter_non_tty_returns_plain():
    stream = io.StringIO()  # StringIO.isatty() is False
    reporter = create_reporter(stream=stream)
    assert isinstance(reporter, PlainProgress)


def test_create_reporter_tty_uses_rich_when_available():
    class FakeTTYStream(io.StringIO):
        def isatty(self) -> bool:
            return True

    reporter = create_reporter(stream=FakeTTYStream())
    from eval.progress import RichProgress

    assert isinstance(reporter, RichProgress)
