"""Tests for the DockerCLIRunner abstraction."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from eval.config import Config, Hooks, RunnerConfig, Task, Variant
from eval.protocols import RunContext, RunStatus
from eval.runners import DockerCLIRunner, create_runner
from eval.runners import docker_cli_runner as docker_runner_mod


def test_docker_cli_runner_supported_collectors():
    assert DockerCLIRunner("token").supported_collectors == ("file", "jaeger")


def test_docker_cli_runner_build_is_noop(tmp_path):
    runner = DockerCLIRunner("token")
    variant = SimpleNamespace(name="cli")
    config = SimpleNamespace(project_dir=tmp_path)

    runner.build(variant, config)


def test_docker_cli_runner_health_check_success(monkeypatch):
    calls = []

    def fake_run(cmd, capture_output, timeout):
        calls.append((cmd, capture_output, timeout))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(docker_runner_mod.subprocess, "run", fake_run)

    DockerCLIRunner("token").health_check()

    assert calls == [(["docker", "info"], True, 10)]


def test_docker_cli_runner_health_check_failure(monkeypatch):
    def fake_run(cmd, capture_output, timeout):
        assert cmd == ["docker", "info"]
        assert capture_output is True
        assert timeout == 10
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(docker_runner_mod.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Docker daemon is not available"):
        DockerCLIRunner("token").health_check()


def test_create_runner_cli():
    runner = create_runner("cli", github_token="tok")

    assert isinstance(runner, DockerCLIRunner)


def test_create_runner_unknown():
    with pytest.raises(ValueError, match="Unknown runner type"):
        create_runner("unknown", github_token="tok")


# ---------------------------------------------------------------------------
# Edge case tests for DockerCLIRunner
# ---------------------------------------------------------------------------


def _make_run_context(tmp_path: Path, *, variant_kwargs: dict | None = None) -> RunContext:
    """Helper to build a minimal RunContext for testing."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    task = Task(name="test-task", prompt="hello")
    v_kwargs = {"name": "test-variant"}
    if variant_kwargs:
        v_kwargs.update(variant_kwargs)
    variant = Variant(**v_kwargs)

    env_file = tmp_path / ".env"
    env_file.write_text("")

    config = Config(
        vars={},
        runner=RunnerConfig(timeout_seconds=60),
        tasks=[task],
        variants=[variant],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )

    run_dir = tmp_path / "runs"
    run_dir.mkdir()

    return RunContext(
        run_id="run-001",
        test_id="test-001",
        epoch=1,
        run_dir=run_dir,
        task=task,
        variant=variant,
        config=config,
        work_dir=work_dir,
    )


class TestDockerDaemonUnavailable:
    """Test Docker daemon unavailable (health_check failure) scenarios."""

    @pytest.mark.parametrize(
        "exception,match",
        [
            (subprocess.TimeoutExpired(cmd=["docker", "info"], timeout=10), "Docker daemon"),
            (FileNotFoundError("docker not found"), "docker not found"),
            (OSError("Connection refused"), "Connection refused"),
        ],
        ids=["timeout", "not-installed", "connection-refused"],
    )
    def test_health_check_exceptions(self, monkeypatch, exception, match):
        """health_check should propagate exceptions when Docker is unreachable."""

        def fake_run(cmd, capture_output, timeout):
            raise exception

        monkeypatch.setattr(docker_runner_mod.subprocess, "run", fake_run)

        with pytest.raises((RuntimeError, subprocess.TimeoutExpired, FileNotFoundError, OSError)):
            DockerCLIRunner("token").health_check()

    @pytest.mark.parametrize("returncode", [1, 125, 127])
    def test_health_check_non_zero_exit(self, monkeypatch, returncode):
        """health_check raises RuntimeError for any non-zero exit code."""

        def fake_run(cmd, capture_output, timeout):
            return SimpleNamespace(returncode=returncode)

        monkeypatch.setattr(docker_runner_mod.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="Docker daemon is not available"):
            DockerCLIRunner("token").health_check()


class TestContainerTimeout:
    """Test container timeout (exit code 124) handling."""

    def test_timeout_exit_code_returns_timeout_status(self, tmp_path):
        """Exit code 124 should produce RunStatus.TIMEOUT."""

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=124)

        runner = DockerCLIRunner("token", run_command=fake_run)
        ctx = _make_run_context(tmp_path)

        with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
            result = runner.run(ctx)

        assert result.exit_code == 124
        assert result.status == RunStatus.TIMEOUT

    @pytest.mark.parametrize("timeout_seconds", [10, 300, 900])
    def test_timeout_value_passed_to_container(self, tmp_path, timeout_seconds):
        """The configured timeout_seconds appears in the docker run command."""
        captured_cmd: list[str] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return SimpleNamespace(returncode=0)

        runner = DockerCLIRunner("token", run_command=fake_run)
        ctx = _make_run_context(tmp_path)
        ctx.config.runner.timeout_seconds = timeout_seconds
        ctx.task.timeout_seconds = None  # rely on runner default

        with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
            runner.run(ctx)

        assert f"{timeout_seconds}s" in captured_cmd


class TestOOMKill:
    """Test OOM kill scenario (exit code 137)."""

    def test_oom_exit_code_returns_failed_status(self, tmp_path):
        """Exit code 137 (OOM/SIGKILL) should produce RunStatus.FAILED."""

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=137)

        runner = DockerCLIRunner("token", run_command=fake_run)
        ctx = _make_run_context(tmp_path)

        with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
            result = runner.run(ctx)

        assert result.exit_code == 137
        assert result.status == RunStatus.FAILED

    @pytest.mark.parametrize(
        "exit_code,expected_status",
        [
            (137, RunStatus.FAILED),
            (139, RunStatus.FAILED),
            (143, RunStatus.FAILED),
        ],
        ids=["SIGKILL-137", "SIGSEGV-139", "SIGTERM-143"],
    )
    def test_signal_exit_codes(self, tmp_path, exit_code, expected_status):
        """Signal-based exit codes (OOM, segfault, SIGTERM) should all be FAILED."""

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=exit_code)

        runner = DockerCLIRunner("token", run_command=fake_run)
        ctx = _make_run_context(tmp_path)

        with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
            result = runner.run(ctx)

        assert result.exit_code == exit_code
        assert result.status == expected_status


class TestInvalidEnvironmentVariables:
    """Test invalid environment variables handling."""

    @pytest.mark.parametrize(
        "extra_env",
        [
            {"": "value"},
            {"KEY WITH SPACES": "value"},
            {"KEY=EMBEDDED": "value"},
        ],
        ids=["empty-key", "spaces-in-key", "equals-in-key"],
    )
    def test_invalid_env_keys_passed_to_docker(self, tmp_path, extra_env):
        """Invalid env var keys are passed through to docker (docker validates)."""
        captured_cmd: list[str] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return SimpleNamespace(returncode=0)

        runner = DockerCLIRunner("token", run_command=fake_run)
        ctx = _make_run_context(tmp_path)
        ctx.extra_env = extra_env

        with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
            result = runner.run(ctx)

        # Runner does not validate env vars itself; it delegates to docker
        assert result.exit_code == 0

    def test_env_with_special_characters_in_values(self, tmp_path):
        """Env vars with special characters in values are properly passed."""
        captured_cmd: list[str] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return SimpleNamespace(returncode=0)

        runner = DockerCLIRunner("token", run_command=fake_run)
        ctx = _make_run_context(tmp_path)
        ctx.extra_env = {"MY_VAR": "val=with=equals\nnewline"}

        with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
            runner.run(ctx)

        # Verify the value is included in the -e flag
        env_args = [captured_cmd[i + 1] for i, a in enumerate(captured_cmd) if a == "-e"]
        matching = [a for a in env_args if a.startswith("MY_VAR=")]
        assert len(matching) == 1
        assert "val=with=equals\nnewline" in matching[0]


class TestMalformedRunScript:
    """Test malformed run_script path handling."""

    def test_nonexistent_run_script_is_skipped(self, tmp_path):
        """A run_script that doesn't exist is silently skipped."""
        captured_cmd: list[str] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return SimpleNamespace(returncode=0)

        runner = DockerCLIRunner("token", run_command=fake_run)
        ctx = _make_run_context(tmp_path, variant_kwargs={"run_script": "nonexistent/setup.sh"})

        with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
            runner.run(ctx)

        # The EVAL_SETUP_SCRIPT env should NOT be in the command
        env_args = [captured_cmd[i + 1] for i, a in enumerate(captured_cmd) if a == "-e"]
        assert not any("EVAL_SETUP_SCRIPT" in a for a in env_args)

    def test_run_script_with_path_traversal(self, tmp_path):
        """A run_script with path traversal is resolved and skipped if not existing."""
        captured_cmd: list[str] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return SimpleNamespace(returncode=0)

        runner = DockerCLIRunner("token", run_command=fake_run)
        ctx = _make_run_context(tmp_path, variant_kwargs={"run_script": "../../../etc/passwd"})

        with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
            runner.run(ctx)

        env_args = [captured_cmd[i + 1] for i, a in enumerate(captured_cmd) if a == "-e"]
        assert not any("EVAL_SETUP_SCRIPT" in a for a in env_args)

    def test_existing_run_script_is_mounted(self, tmp_path):
        """A valid existing run_script should be mounted into the container."""
        captured_cmd: list[str] = []
        script_path = tmp_path / "setup.sh"
        script_path.write_text("#!/bin/bash\necho setup")

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return SimpleNamespace(returncode=0)

        runner = DockerCLIRunner("token", run_command=fake_run)
        ctx = _make_run_context(tmp_path, variant_kwargs={"run_script": "setup.sh"})

        with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
            runner.run(ctx)

        env_args = [captured_cmd[i + 1] for i, a in enumerate(captured_cmd) if a == "-e"]
        assert any("EVAL_SETUP_SCRIPT" in a for a in env_args)


class TestWorkDirErrors:
    """Test work_dir permission errors and edge cases."""

    def test_work_dir_none_raises_value_error(self, tmp_path):
        """run() raises ValueError when work_dir is None."""
        runner = DockerCLIRunner("token")
        ctx = _make_run_context(tmp_path)
        ctx.work_dir = None

        with pytest.raises(ValueError, match="work_dir is required"):
            runner.run(ctx)

    def test_work_dir_nonexistent_still_runs(self, tmp_path):
        """run() does not validate work_dir existence (Docker handles it)."""

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=0)

        runner = DockerCLIRunner("token", run_command=fake_run)
        ctx = _make_run_context(tmp_path)
        ctx.work_dir = tmp_path / "does_not_exist"

        with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
            result = runner.run(ctx)

        assert result.exit_code == 0

    def test_work_dir_permission_error_from_docker(self, tmp_path):
        """Docker returning exit code 125 indicates container start failure (e.g. permissions)."""

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=125)

        runner = DockerCLIRunner("token", run_command=fake_run)
        ctx = _make_run_context(tmp_path)

        with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
            result = runner.run(ctx)

        assert result.exit_code == 125
        assert result.status == RunStatus.FAILED

    def test_work_dir_mounted_in_docker_command(self, tmp_path):
        """work_dir should be mounted as /workspace in the container."""
        captured_cmd: list[str] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return SimpleNamespace(returncode=0)

        runner = DockerCLIRunner("token", run_command=fake_run)
        ctx = _make_run_context(tmp_path)

        with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
            runner.run(ctx)

        volume_args = [captured_cmd[i + 1] for i, a in enumerate(captured_cmd[:-1]) if a == "-v"]
        workspace_mounts = [v for v in volume_args if ":/workspace" in v]
        assert len(workspace_mounts) == 1
        assert str(ctx.work_dir) in workspace_mounts[0]
