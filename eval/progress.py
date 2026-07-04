"""Live progress reporting for parallel eval runs and judge scoring (issue #82).

Three reporter implementations share one interface (:class:`ProgressReporter`):

- :class:`RichProgress` -- a live-updating progress bar + per-cell status
  lines, used interactively (TTY) when the optional ``rich`` dependency is
  installed.
- :class:`PlainProgress` -- one log line per completed/failed cell, used in
  non-TTY contexts (CI logs) or when ``rich`` isn't installed.
- :class:`NullProgress` -- no output at all, used for ``--no-progress`` and as
  the safe default for direct (non-CLI) callers.

:func:`create_reporter` auto-selects the right implementation; callers
(`eval.services.orchestrator`, `eval.services.judge_service`) only depend on
the `ProgressReporter` protocol so the choice of backend never affects
scheduling logic.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import IO, TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from rich.console import Group
    from rich.live import Live


class ProgressReporter(Protocol):
    """Callback interface for reporting progress of a batch of "cells".

    A "cell" is one unit of work: an eval run (task/variant/epoch/fixture) or
    a judge scoring call. Implementations decide how (or whether) to render
    these events; callers just report them as they happen.
    """

    def start(self, total: int, *, label: str = "eval matrix", workers: int = 1) -> None:
        """Begin tracking a batch of ``total`` cells. ``workers`` is the
        expected concurrency, used for ETA estimation."""
        ...

    def cell_started(self, name: str) -> None:
        """A cell began executing."""
        ...

    def cell_completed(
        self, name: str, *, duration: float | None = None, status: str = "completed"
    ) -> None:
        """A cell finished successfully."""
        ...

    def cell_failed(self, name: str, *, duration: float | None = None, reason: str = "") -> None:
        """A cell finished unsuccessfully (failed/timed out/errored)."""
        ...

    def notice(self, message: str) -> None:
        """A secondary informational message (e.g. "Evaluating: foo (judge)...").

        Rendered alongside the progress display without disrupting it.
        """
        ...

    def finish(self) -> None:
        """Stop tracking; flush/clear any live display."""
        ...


class NullProgress:
    """No-op reporter. Used for ``--no-progress`` and as the default for
    direct (non-CLI) callers so library behavior never changes silently."""

    def start(self, total: int, *, label: str = "eval matrix", workers: int = 1) -> None:
        pass

    def cell_started(self, name: str) -> None:
        pass

    def cell_completed(
        self, name: str, *, duration: float | None = None, status: str = "completed"
    ) -> None:
        pass

    def cell_failed(self, name: str, *, duration: float | None = None, reason: str = "") -> None:
        pass

    def notice(self, message: str) -> None:
        pass

    def finish(self) -> None:
        pass


class PlainProgress:
    """One log line per event. Safe for CI logs / pipes: no cursor control,
    no partial-line overwrites, every event is a single flushed line."""

    def __init__(self, *, stream: IO[str] | None = None) -> None:
        self._stream = stream or sys.stderr
        self._total = 0
        self._completed = 0
        self._lock = threading.Lock()

    def start(self, total: int, *, label: str = "eval matrix", workers: int = 1) -> None:
        self._total = total
        self._completed = 0
        self._echo(f"Running {label}: {total} run(s) scheduled")

    def cell_started(self, name: str) -> None:
        pass  # non-TTY output only logs completions, to keep it compact

    def cell_completed(
        self, name: str, *, duration: float | None = None, status: str = "completed"
    ) -> None:
        with self._lock:
            self._completed += 1
            n = self._completed
        dur = f"{duration:.0f}s" if duration is not None else "?"
        self._echo(f"[{n}/{self._total}] completed: {name} ({dur}, {status})")

    def cell_failed(self, name: str, *, duration: float | None = None, reason: str = "") -> None:
        with self._lock:
            self._completed += 1
            n = self._completed
        self._echo(f"[{n}/{self._total}] FAILED: {name} ({reason})")

    def notice(self, message: str) -> None:
        self._echo(message)

    def finish(self) -> None:
        pass

    def _echo(self, message: str) -> None:
        print(message, file=self._stream, flush=True)


def _format_duration(seconds: float) -> str:
    """Format seconds as a compact human duration (e.g. "45s", "14m", "1h5m")."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if secs < 30 else f"{minutes + 1}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m"


class RichProgress:
    """Live-updating progress bar + rolling per-cell status, backed by
    ``rich``. Only instantiate this after confirming ``rich`` is importable
    (see :func:`create_reporter`)."""

    _MAX_VISIBLE = 10

    def __init__(self, *, stream: IO[str] | None = None) -> None:
        from rich.console import Console

        self._console = Console(file=stream or sys.stderr)
        self._lock = threading.Lock()
        self._live: Live | None = None
        self._total = 0
        self._completed = 0
        self._workers = 1
        self._label = "eval matrix"
        self._durations: list[float] = []
        self._start_time = 0.0
        # Insertion-ordered cell state: name -> {"status", "start", "duration", "detail"}
        self._order: list[str] = []
        self._state: dict[str, dict[str, object]] = {}
        self._notices: list[str] = []

    def start(self, total: int, *, label: str = "eval matrix", workers: int = 1) -> None:
        from rich.live import Live

        self._total = total
        self._completed = 0
        self._workers = max(1, workers)
        self._label = label
        self._start_time = time.monotonic()
        live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=4,
            transient=False,
        )
        self._live = live
        live.start()

    def cell_started(self, name: str) -> None:
        with self._lock:
            if name not in self._state:
                self._order.append(name)
            self._state[name] = {"status": "running", "start": time.monotonic()}
        self._refresh()

    def cell_completed(
        self, name: str, *, duration: float | None = None, status: str = "completed"
    ) -> None:
        with self._lock:
            self._completed += 1
            if duration is not None:
                self._durations.append(duration)
            self._state[name] = {"status": "completed", "duration": duration, "detail": status}
        self._refresh()

    def cell_failed(self, name: str, *, duration: float | None = None, reason: str = "") -> None:
        with self._lock:
            self._completed += 1
            if duration is not None:
                self._durations.append(duration)
            self._state[name] = {"status": "failed", "duration": duration, "detail": reason}
        self._refresh()

    def notice(self, message: str) -> None:
        # Console.print() is Live-aware: while a Live display is active this
        # prints *above* it and lets the live region keep redrawing below.
        self._console.print(message)

    def finish(self) -> None:
        self._refresh()
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _eta_seconds(self) -> float | None:
        if not self._durations or self._completed >= self._total:
            return None
        avg = sum(self._durations) / len(self._durations)
        remaining = self._total - self._completed
        return avg * remaining / self._workers

    def _render(self) -> Group:
        from rich.console import Group
        from rich.text import Text

        with self._lock:
            total = max(1, self._total)
            pct = int(self._completed / total * 100)
            bar_width = 30
            filled = int(bar_width * self._completed / total)
            bar = "\u2501" * filled + "\u2500" * (bar_width - filled)
            eta = self._eta_seconds()
            eta_str = f" | ETA {_format_duration(eta)}" if eta is not None else ""
            header = Text(
                f"Running {self._label} [{self._completed}/{self._total}] {bar} {pct}%{eta_str}"
            )

            lines: list[Text] = []
            visible = self._order[-self._MAX_VISIBLE :]
            for name in visible:
                st = self._state.get(name, {})
                status = st.get("status")
                if status == "completed":
                    dur = st.get("duration")
                    dur_str = f"{dur:.0f}s" if isinstance(dur, (int, float)) else "?"
                    lines.append(Text(f"  \u2713 {name:<32} {dur_str}", style="green"))
                elif status == "failed":
                    detail = st.get("detail") or ""
                    lines.append(Text(f"  \u2717 {name:<32} {detail}", style="red"))
                else:
                    start = st.get("start")
                    elapsed = time.monotonic() - start if isinstance(start, (int, float)) else 0
                    lines.append(
                        Text(f"  \u25cf {name:<32} running ({elapsed:.0f}s)", style="yellow")
                    )

        return Group(header, *lines)

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())


def _stream_is_tty(stream: IO[str] | None) -> bool:
    stream = stream or sys.stderr
    return bool(getattr(stream, "isatty", None) and stream.isatty())


def create_reporter(
    *, no_progress: bool = False, stream: IO[str] | None = None
) -> ProgressReporter:
    """Auto-select a progress reporter.

    - ``no_progress=True`` (``--no-progress``) always returns :class:`NullProgress`.
    - Interactive TTY + ``rich`` installed -> :class:`RichProgress`.
    - Otherwise (non-TTY / CI, or ``rich`` not installed) -> :class:`PlainProgress`.
    """
    if no_progress:
        return NullProgress()
    if _stream_is_tty(stream):
        try:
            import rich  # noqa: F401
        except ImportError:
            pass
        else:
            return RichProgress(stream=stream)
    return PlainProgress(stream=stream)
