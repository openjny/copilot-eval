"""Tests for run_one's retry mechanism (issue #69).

Transient failures (DockerError, subprocess.TimeoutExpired) raised by the
injected AgentRunner's `run()` should be retried up to `runner.retries` times
with exponential backoff, while deterministic failures (AuthError, HookError,
FixtureError) must never be retried. Uses the same dependency-injection seam
(`runner=`) as tests/test_runner.py's `_FakeAgentRunner` (issue #90) so this
exercises real retry/backoff logic without touching Docker or sleeping for
real.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from eval.config import Config, RunnerConfig, Task, Variant
from eval.exceptions import FixtureError, HookError
from eval.protocols import RunArtifacts, RunStatus


def _config(tmp_path: Path, *, retries: int = 0, retry_delay: float = 5.0) -> Config:
    return Config(
        vars={},
        runner=RunnerConfig(retries=retries, retry_delay=retry_delay),
        tasks=[],
        variants=[],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


class _FlakyAgentRunner:
    """AgentRunner test double whose `run()` raises `raise_error` for the
    first `fail_times` calls, then succeeds. Records every call's `run_context`
    so tests can assert the retry count."""

    def __init__(
        self,
        *,
        raise_error: Exception,
        fail_times: int,
        exit_code: int = 0,
        supported_collectors: tuple[str, ...] = ("file", "jaeger"),
    ) -> None:
        self.raise_error = raise_error
        self.fail_times = fail_times
        self.exit_code = exit_code
        self._supported_collectors = supported_collectors
        self.calls: list[Any] = []

    def build(self, variant, config) -> None:
        pass

    def health_check(self) -> None:
        pass

    @property
    def supported_collectors(self) -> tuple[str, ...]:
        return self._supported_collectors

    def run(self, run_context):
        self.calls.append(run_context)
        if len(self.calls) <= self.fail_times:
            raise self.raise_error
        return RunArtifacts(
            exit_code=self.exit_code,
            log_file=run_context.run_dir / "fake.log",
            trace_file=None,
            output_dir=None,
            duration_seconds=0.01,
            status=RunStatus.SUCCESS,
            started_at="2024-01-01T00:00:00",
            finished_at="2024-01-01T00:00:01",
        )


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Retry backoff uses time.sleep; stub it out so tests run instantly."""
    from eval import runner as runner_mod

    sleeps: list[float] = []
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def test_retry_disabled_by_default_docker_error_returns_setup_failed(tmp_path):
    """runner.retries defaults to 0: a single transient DockerError is not
    retried and immediately becomes setup_failed."""
    from eval import runner as runner_mod
    from eval.exceptions import DockerError

    fake = _FlakyAgentRunner(raise_error=DockerError("daemon hiccup"), fail_times=1)
    config = _config(tmp_path)  # retries=0
    run_dir = tmp_path / "results"
    run_dir.mkdir()

    result = runner_mod.run_one(
        Task(name="t", prompt="p"),
        Variant(name="v"),
        epoch=1,
        config=config,
        run_id="r",
        run_dir=run_dir,
        github_token="tok",
        runner=fake,
    )

    assert result.status == "setup_failed"
    assert result.retry_count == 0
    assert len(fake.calls) == 1


def test_retry_on_docker_error_succeeds_after_retries(tmp_path, _no_real_sleep):
    """A DockerError that clears up after 2 attempts succeeds on the 3rd,
    recording retry_count=2."""
    from eval import runner as runner_mod
    from eval.exceptions import DockerError

    fake = _FlakyAgentRunner(raise_error=DockerError("daemon hiccup"), fail_times=2)
    config = _config(tmp_path, retries=3, retry_delay=1.0)
    run_dir = tmp_path / "results"
    run_dir.mkdir()

    result = runner_mod.run_one(
        Task(name="t", prompt="p"),
        Variant(name="v"),
        epoch=1,
        config=config,
        run_id="r",
        run_dir=run_dir,
        github_token="tok",
        runner=fake,
    )

    assert result.status == "completed"
    assert result.retry_count == 2
    assert len(fake.calls) == 3
    # Exponential backoff: delay * 2**attempt for attempts 0 and 1.
    assert _no_real_sleep == [1.0, 2.0]


def test_retry_exhausted_returns_setup_failed_with_retry_count(tmp_path, _no_real_sleep):
    """A DockerError that never clears exhausts the retry budget and is
    reported as setup_failed with the full retry_count."""
    from eval import runner as runner_mod
    from eval.exceptions import DockerError

    fake = _FlakyAgentRunner(raise_error=DockerError("daemon down"), fail_times=99)
    config = _config(tmp_path, retries=2, retry_delay=1.0)
    run_dir = tmp_path / "results"
    run_dir.mkdir()

    result = runner_mod.run_one(
        Task(name="t", prompt="p"),
        Variant(name="v"),
        epoch=1,
        config=config,
        run_id="r",
        run_dir=run_dir,
        github_token="tok",
        runner=fake,
    )

    assert result.status == "setup_failed"
    assert result.retry_count == 2
    # Initial attempt + 2 retries = 3 calls total.
    assert len(fake.calls) == 3
    assert _no_real_sleep == [1.0, 2.0]


def test_retry_on_timeout_succeeds_after_retries(tmp_path, _no_real_sleep):
    """subprocess.TimeoutExpired is retried the same way as DockerError."""
    from eval import runner as runner_mod

    fake = _FlakyAgentRunner(
        raise_error=subprocess.TimeoutExpired(cmd=["copilot"], timeout=300),
        fail_times=1,
    )
    config = _config(tmp_path, retries=2, retry_delay=2.0)
    run_dir = tmp_path / "results"
    run_dir.mkdir()

    result = runner_mod.run_one(
        Task(name="t", prompt="p"),
        Variant(name="v"),
        epoch=1,
        config=config,
        run_id="r",
        run_dir=run_dir,
        github_token="tok",
        runner=fake,
    )

    assert result.status == "completed"
    assert result.retry_count == 1
    assert len(fake.calls) == 2
    assert _no_real_sleep == [2.0]


def test_retry_backoff_is_capped_at_60_seconds(tmp_path, _no_real_sleep):
    """delay = retry_delay * 2**attempt is capped at 60s even with a large
    retry_delay/attempt count."""
    from eval import runner as runner_mod
    from eval.exceptions import DockerError

    fake = _FlakyAgentRunner(raise_error=DockerError("daemon hiccup"), fail_times=3)
    config = _config(tmp_path, retries=3, retry_delay=30.0)
    run_dir = tmp_path / "results"
    run_dir.mkdir()

    result = runner_mod.run_one(
        Task(name="t", prompt="p"),
        Variant(name="v"),
        epoch=1,
        config=config,
        run_id="r",
        run_dir=run_dir,
        github_token="tok",
        runner=fake,
    )

    assert result.status == "completed"
    assert result.retry_count == 3
    # 30, 60 (would be 120, capped), 60 (would be 240, capped).
    assert _no_real_sleep == [30.0, 60.0, 60.0]


@pytest.mark.parametrize(
    "exc",
    [
        HookError("before_run script failed to execute"),
        FixtureError("failed to copy fixture"),
    ],
)
def test_deterministic_errors_are_never_retried(tmp_path, _no_real_sleep, exc):
    """AuthError/HookError/FixtureError are deterministic failures and must
    propagate immediately regardless of runner.retries."""
    from eval import runner as runner_mod

    fake = _FlakyAgentRunner(raise_error=exc, fail_times=99)
    config = _config(tmp_path, retries=5, retry_delay=1.0)
    run_dir = tmp_path / "results"
    run_dir.mkdir()

    result = runner_mod.run_one(
        Task(name="t", prompt="p"),
        Variant(name="v"),
        epoch=1,
        config=config,
        run_id="r",
        run_dir=run_dir,
        github_token="tok",
        runner=fake,
    )

    assert result.status == "setup_failed"
    assert result.retry_count == 0
    assert len(fake.calls) == 1  # no retry attempted
    assert _no_real_sleep == []


def test_retry_count_recorded_in_to_dict(tmp_path, _no_real_sleep):
    """retry_count is serialized into the result JSON via to_dict()."""
    from eval import runner as runner_mod
    from eval.exceptions import DockerError

    fake = _FlakyAgentRunner(raise_error=DockerError("daemon hiccup"), fail_times=1)
    config = _config(tmp_path, retries=1, retry_delay=0.1)
    run_dir = tmp_path / "results"
    run_dir.mkdir()

    result = runner_mod.run_one(
        Task(name="t", prompt="p"),
        Variant(name="v"),
        epoch=1,
        config=config,
        run_id="r",
        run_dir=run_dir,
        github_token="tok",
        runner=fake,
    )

    assert result.retry_count == 1
    assert result.to_dict()["retry_count"] == 1
