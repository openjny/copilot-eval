"""Tests for progress-reporter integration in the orchestrator (issue #82).

Monkeypatches ``eval.services.orchestrator.run_one`` so scheduling logic is
exercised without Docker; verifies that a fake reporter receives
start/cell_started/cell_completed/cell_failed/finish for each of the three
parallel strategies (``off``/serial, ``per_task``, ``full``).
"""

from __future__ import annotations

from pathlib import Path

from eval.config import Config, RunnerConfig, Task, Variant
from eval.protocols import RunStatus
from eval.runner import RunResult
from eval.services import orchestrator


class FakeReporter:
    """Records every progress event for later assertions."""

    def __init__(self) -> None:
        self.started_total: int | None = None
        self.started_workers: int | None = None
        self.cells_started: list[str] = []
        self.cells_completed: list[tuple[str, float | None, str]] = []
        self.cells_failed: list[tuple[str, float | None, str]] = []
        self.notices: list[str] = []
        self.finished = False

    def start(self, total, *, label="eval matrix", workers=1):
        self.started_total = total
        self.started_workers = workers

    def cell_started(self, name):
        self.cells_started.append(name)

    def cell_completed(self, name, *, duration=None, status="completed"):
        self.cells_completed.append((name, duration, status))

    def cell_failed(self, name, *, duration=None, reason=""):
        self.cells_failed.append((name, duration, reason))

    def notice(self, message):
        self.notices.append(message)

    def finish(self):
        self.finished = True


def _config(tmp_path: Path, parallel: str = "off", max_workers: int = 4) -> Config:
    return Config(
        vars={},
        runner=RunnerConfig(parallel=parallel, max_workers=max_workers, timeout_seconds=300),
        tasks=[],
        variants=[Variant(name="baseline"), Variant(name="experimental")],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


def _task(name: str = "code-review") -> Task:
    return Task(name=name, prompt="do the thing")


def _fake_run_one(status: RunStatus = RunStatus.SUCCESS, exit_code: int = 0):
    def _run(
        task, variant, epoch, config, run_id, run_dir, github_token, order_index=None, fixture=None
    ):
        return RunResult(
            task=task.name,
            variant=variant.name,
            epoch=epoch,
            test_id="t",
            run_id=run_id,
            log_file=run_dir / "x.log",
            exit_code=exit_code,
            status=status,
            order_index=order_index,
            duration_seconds=1.5,
        )

    return _run


def test_serial_schedule_reports_each_cell(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one())
    config = _config(tmp_path, parallel="off")
    reporter = FakeReporter()

    results = orchestrator._execute_schedule(
        config,
        [_task()],
        epochs=1,
        run_id="r1",
        run_dir=tmp_path,
        github_token="tok",
        reporter=reporter,
    )

    assert len(results) == 2  # 2 variants x 1 epoch
    assert reporter.started_total == 2
    assert reporter.started_workers == 1
    assert sorted(reporter.cells_started) == [
        "code-review/baseline/e1",
        "code-review/experimental/e1",
    ]
    assert len(reporter.cells_completed) == 2
    assert reporter.cells_failed == []
    assert reporter.finished


def test_full_parallel_schedule_reports_each_cell(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one())
    config = _config(tmp_path, parallel="full", max_workers=4)
    reporter = FakeReporter()

    results = orchestrator._execute_schedule(
        config,
        [_task()],
        epochs=1,
        run_id="r1",
        run_dir=tmp_path,
        github_token="tok",
        reporter=reporter,
    )

    assert len(results) == 2
    assert reporter.started_total == 2
    assert reporter.started_workers == 4
    assert len(reporter.cells_started) == 2
    assert len(reporter.cells_completed) == 2
    assert reporter.finished


def test_per_task_schedule_reports_each_cell(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one())
    config = _config(tmp_path, parallel="per_task", max_workers=4)
    reporter = FakeReporter()
    tasks = [_task("code-review"), _task("summarize")]

    results = orchestrator._execute_schedule(
        config,
        tasks,
        epochs=1,
        run_id="r1",
        run_dir=tmp_path,
        github_token="tok",
        reporter=reporter,
    )

    assert len(results) == 4  # 2 tasks x 2 variants
    assert reporter.started_total == 4
    assert reporter.started_workers == 2  # min(len(tasks), max_workers)
    assert len(reporter.cells_started) == 4
    assert len(reporter.cells_completed) == 4
    assert reporter.finished


def test_failed_cell_reports_cell_failed_with_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(
        orchestrator, "run_one", _fake_run_one(status=RunStatus.TIMEOUT, exit_code=124)
    )
    config = _config(tmp_path, parallel="off")
    reporter = FakeReporter()

    orchestrator._execute_schedule(
        config,
        [_task()],
        epochs=1,
        run_id="r1",
        run_dir=tmp_path,
        github_token="tok",
        reporter=reporter,
    )

    assert reporter.cells_completed == []
    assert len(reporter.cells_failed) == 2
    name, duration, reason = reporter.cells_failed[0]
    assert reason == "timeout after 300s"
    assert duration == 1.5


def test_execute_schedule_defaults_to_null_progress_without_reporter(tmp_path, monkeypatch):
    """No reporter passed -- must not raise, and must behave exactly as before."""
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one())
    config = _config(tmp_path, parallel="off")

    results = orchestrator._execute_schedule(
        config, [_task()], epochs=1, run_id="r1", run_dir=tmp_path, github_token="tok"
    )

    assert len(results) == 2
