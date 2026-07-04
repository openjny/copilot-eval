"""Tests for progress-reporter integration in judge scoring (issue #82).

Monkeypatches the judge invocation internals so `_run_judges` runs without a
live Copilot CLI / GitHub token, and verifies that a fake reporter receives
start/cell_started/cell_completed|cell_failed/finish for each judge call.
"""

from __future__ import annotations

from pathlib import Path

from eval.config import Config, Evaluator, RunnerConfig, Task
from eval.protocols import EvalScore
from eval.services import judge_service
from eval.trace import Trace


class FakeReporter:
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


def _config() -> Config:
    task = Task(
        name="code-review",
        prompt="do the thing",
        evaluators=[Evaluator(name="quality", type="judge", prompt="rate this")],
    )
    return Config(
        vars={},
        runner=RunnerConfig(max_workers=4),
        tasks=[task],
        variants=[],
        project_dir=Path("."),
        config_dir=Path("."),
    )


def _trace() -> Trace:
    return Trace(
        trace_id="t1",
        spans=[],
        resource_tags={
            "eval.scenario": "code-review",
            "eval.variant": "baseline",
            "eval.epoch": "1",
            "eval.fixture": "",
        },
    )


class _FakeJudgeEvaluator:
    """Stand-in for eval.evaluators.JudgeEvaluator: always scores 5."""

    def __init__(self, config: Evaluator) -> None:
        self.config = config

    @classmethod
    def from_config(cls, config: Evaluator) -> _FakeJudgeEvaluator:
        return cls(config)

    def evaluate(self, context) -> EvalScore:
        return EvalScore(name=self.config.name, type="judge", score=5, reason="looks good")


def _patch_common(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(judge_service, "get_github_token", lambda: "fake-token")
    monkeypatch.setattr(judge_service, "extract_conversation", lambda trace, max_chars: "hi")
    monkeypatch.setattr(judge_service, "read_files_from_dir", lambda path, max_chars: "")
    monkeypatch.setattr(judge_service, "JudgeEvaluator", _FakeJudgeEvaluator)


def test_run_judges_reports_success(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)
    reporter = FakeReporter()

    judge_service._run_judges(_config(), [_trace()], tmp_path, reporter=reporter)

    assert reporter.started_total == 1
    assert reporter.started_workers == 4
    assert len(reporter.cells_started) == 1
    assert len(reporter.cells_completed) == 1
    name, duration, status = reporter.cells_completed[0]
    assert "code-review/baseline/e1" in name
    assert status == "scored"
    assert reporter.cells_failed == []
    assert reporter.finished
    assert any("Evaluating" in n for n in reporter.notices)


def test_run_judges_reports_failure(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)

    class _RaisingEvaluator(_FakeJudgeEvaluator):
        def evaluate(self, context):
            raise RuntimeError("judge exploded")

    monkeypatch.setattr(judge_service, "JudgeEvaluator", _RaisingEvaluator)
    reporter = FakeReporter()

    judge_service._run_judges(_config(), [_trace()], tmp_path, reporter=reporter)

    assert reporter.cells_completed == []
    assert len(reporter.cells_failed) == 1
    name, duration, reason = reporter.cells_failed[0]
    assert "judge exploded" in reason
    assert reporter.finished


def test_run_judges_no_reporter_defaults_to_null(tmp_path, monkeypatch):
    """No reporter passed -- must not raise, and must behave exactly as before."""
    _patch_common(monkeypatch, tmp_path)

    judge_service._run_judges(_config(), [_trace()], tmp_path)

    scores_file = next(tmp_path.glob("*.scores.json"))
    assert scores_file.exists()


def test_run_judges_skips_reporter_when_no_work(tmp_path, monkeypatch):
    """An empty work queue (e.g. all scores cached) must not call reporter.start."""
    _patch_common(monkeypatch, tmp_path)
    config = _config()
    reporter = FakeReporter()

    # First pass writes the cached score...
    judge_service._run_judges(config, [_trace()], tmp_path, reporter=reporter)
    reporter2 = FakeReporter()
    # ...second pass has nothing pending, so reporter.start must never fire.
    judge_service._run_judges(config, [_trace()], tmp_path, reporter=reporter2)

    assert reporter2.started_total is None
    assert not reporter2.finished
