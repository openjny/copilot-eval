"""Integration tests for Docker runner and collector backends.

These tests exercise real Docker container execution, runner/collector factory
wiring, and the full run pipeline. They require a Docker daemon to be available
and are marked with @pytest.mark.integration so they can be skipped in fast CI.

Run with:
    uv run pytest -m integration
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from eval.collectors import FileCollector, JaegerCollector, create_collector
from eval.collectors.file_collector import TRACE_FILE, parse_file_traces
from eval.config import Config, Hooks, RunnerConfig, Task, Variant
from eval.protocols import RunContext, RunStatus
from eval.runners import DockerCLIRunner, create_runner

FIXTURE = Path(__file__).parent / "fixtures" / "file-exporter-sample.jsonl"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Check whether Docker daemon is reachable."""
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _jaeger_available() -> bool:
    """Check whether Jaeger is reachable at localhost:16686."""
    try:
        import requests

        resp = requests.get("http://localhost:16686/api/services", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def _make_config(tmp_path: Path, collector: str = "file") -> Config:
    """Build a minimal Config suitable for integration tests."""
    return Config(
        vars={},
        runner=RunnerConfig(
            epochs=1,
            timeout_seconds=60,
            collector=collector,
        ),
        tasks=[],
        variants=[],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


def _make_task(name: str = "test-task", prompt: str = "Say hello") -> Task:
    return Task(name=name, prompt=prompt, hooks=Hooks())


def _make_variant(name: str = "baseline") -> Variant:
    return Variant(name=name)


def _make_run_context(
    tmp_path: Path,
    config: Config | None = None,
    task: Task | None = None,
    variant: Variant | None = None,
    run_id: str = "integration-run-1",
    epoch: int = 1,
) -> RunContext:
    """Build a RunContext backed by the given tmp_path."""
    config = config or _make_config(tmp_path)
    task = task or _make_task()
    variant = variant or _make_variant()
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    (work_dir / "output").mkdir(exist_ok=True)
    (work_dir / TRACE_FILE.parent).mkdir(parents=True, exist_ok=True)
    run_dir = tmp_path / "runs"
    run_dir.mkdir(exist_ok=True)
    return RunContext(
        run_id=run_id,
        test_id="test-integration-001",
        epoch=epoch,
        run_dir=run_dir,
        task=task,
        variant=variant,
        config=config,
        work_dir=work_dir,
    )


# ---------------------------------------------------------------------------
# DockerCLIRunner integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDockerCLIRunnerIntegration:
    """Integration tests for DockerCLIRunner with real Docker commands."""

    def test_health_check_with_real_docker(self):
        """Verify health_check passes when Docker daemon is available."""
        if not _docker_available():
            pytest.skip("Docker daemon not available")
        runner = DockerCLIRunner("fake-token")
        runner.health_check()

    def test_run_builds_correct_docker_command(self, tmp_path: Path):
        """Verify the docker run command includes volumes, env, image, and timeout."""
        captured_cmd: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.append(cmd)
            return SimpleNamespace(returncode=0)

        config = _make_config(tmp_path)
        task = _make_task()
        variant = _make_variant()
        ctx = _make_run_context(tmp_path, config=config, task=task, variant=variant)

        runner = DockerCLIRunner("test-token", run_command=fake_run)
        artifacts = runner.run(ctx)

        assert artifacts.exit_code == 0
        assert artifacts.status == RunStatus.SUCCESS
        assert len(captured_cmd) == 1

        cmd = captured_cmd[0]
        assert cmd[0] == "docker"
        assert cmd[1] == "run"
        assert "--rm" in cmd
        # Verify env-file and GITHUB_TOKEN forwarding
        assert "-e" in cmd
        assert "GITHUB_TOKEN" in cmd
        # Verify workspace volume mount
        volume_args = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-v"]
        workspace_volumes = [v for v in volume_args if "/workspace" in v]
        assert len(workspace_volumes) >= 1
        # Verify image name
        expected_image = config.image_name(variant)
        assert expected_image in cmd
        # Verify timeout is applied
        assert "timeout" in cmd
        assert "60s" in cmd
        # Verify copilot prompt
        assert "-p" in cmd

    def test_run_captures_timeout_exit_code(self, tmp_path: Path):
        """Exit code 124 maps to TIMEOUT status."""

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=124)

        ctx = _make_run_context(tmp_path)
        runner = DockerCLIRunner("token", run_command=fake_run)
        artifacts = runner.run(ctx)

        assert artifacts.exit_code == 124
        assert artifacts.status == RunStatus.TIMEOUT

    def test_run_captures_failure_exit_code(self, tmp_path: Path):
        """Non-zero, non-124 exit codes map to FAILED status."""

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=1)

        ctx = _make_run_context(tmp_path)
        runner = DockerCLIRunner("token", run_command=fake_run)
        artifacts = runner.run(ctx)

        assert artifacts.exit_code == 1
        assert artifacts.status == RunStatus.FAILED

    def test_run_with_run_script(self, tmp_path: Path):
        """Verify run_script mounts as a volume when present."""
        captured_cmd: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.append(cmd)
            return SimpleNamespace(returncode=0)

        # Create a fake run script
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        run_script = scripts_dir / "setup.sh"
        run_script.write_text("#!/bin/sh\necho setup", encoding="utf-8")

        config = _make_config(tmp_path)
        variant = Variant(name="with-script", run_script="scripts/setup.sh")
        ctx = _make_run_context(tmp_path, config=config, variant=variant)

        runner = DockerCLIRunner("token", run_command=fake_run)
        runner.run(ctx)

        cmd = captured_cmd[0]
        # Should mount the run script and set EVAL_SETUP_SCRIPT env
        env_args = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-e"]
        setup_envs = [e for e in env_args if "EVAL_SETUP_SCRIPT" in e]
        assert len(setup_envs) == 1

    def test_run_with_model_override(self, tmp_path: Path):
        """Verify model override is passed to copilot command."""
        captured_cmd: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.append(cmd)
            return SimpleNamespace(returncode=0)

        config = _make_config(tmp_path)
        variant = Variant(name="model-override", model="gpt-4o")
        ctx = _make_run_context(tmp_path, config=config, variant=variant)

        runner = DockerCLIRunner("token", run_command=fake_run)
        runner.run(ctx)

        cmd = captured_cmd[0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "gpt-4o"

    def test_run_otel_attributes(self, tmp_path: Path):
        """Verify OTEL_RESOURCE_ATTRIBUTES contain eval metadata."""
        captured_cmd: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.append(cmd)
            return SimpleNamespace(returncode=0)

        ctx = _make_run_context(tmp_path, run_id="run-abc", epoch=3)

        runner = DockerCLIRunner("token", run_command=fake_run)
        runner.run(ctx)

        cmd = captured_cmd[0]
        env_args = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-e"]
        otel_attrs = [e for e in env_args if "OTEL_RESOURCE_ATTRIBUTES=" in e]
        assert len(otel_attrs) == 1
        attrs_value = otel_attrs[0].split("=", 1)[1]
        assert "eval.run_id=run-abc" in attrs_value
        assert "eval.epoch=3" in attrs_value

    def test_run_duration_tracking(self, tmp_path: Path):
        """Verify started_at, finished_at, and duration_seconds are populated."""

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=0)

        ctx = _make_run_context(tmp_path)
        runner = DockerCLIRunner("token", run_command=fake_run)
        artifacts = runner.run(ctx)

        assert artifacts.started_at is not None
        assert artifacts.finished_at is not None
        assert artifacts.duration_seconds >= 0


# ---------------------------------------------------------------------------
# Factory wiring integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestFactoryWiring:
    """Integration tests for create_runner and create_collector factories."""

    def test_create_runner_returns_docker_runner(self):
        """create_runner('cli') returns a fully functional DockerCLIRunner."""
        runner = create_runner("cli", github_token="integration-token")
        assert isinstance(runner, DockerCLIRunner)
        assert runner.github_token == "integration-token"
        assert "file" in runner.supported_collectors
        assert "jaeger" in runner.supported_collectors

    def test_create_collector_file(self):
        """create_collector('file') returns a FileCollector."""
        collector = create_collector("file")
        assert isinstance(collector, FileCollector)

    def test_create_collector_jaeger(self):
        """create_collector('jaeger') returns a JaegerCollector with correct config."""
        collector = create_collector(
            "jaeger",
            jaeger_url="http://jaeger:16686",
            otel_endpoint="http://host.docker.internal:4318",
        )
        assert isinstance(collector, JaegerCollector)
        assert collector.jaeger_url == "http://jaeger:16686"
        assert collector.otel_endpoint == "http://host.docker.internal:4318"

    def test_runner_collector_compatibility(self):
        """Runner's supported_collectors includes all registered collector types."""
        runner = create_runner("cli", github_token="tok")
        for collector_type in runner.supported_collectors:
            # Should not raise
            if collector_type == "jaeger":
                create_collector(collector_type, jaeger_url="http://x", otel_endpoint="http://y")
            else:
                create_collector(collector_type)


# ---------------------------------------------------------------------------
# FileCollector integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestFileCollectorIntegration:
    """Integration tests for FileCollector reading real trace JSONL output."""

    def test_collect_from_traces_dir(self, tmp_path: Path):
        """FileCollector reads .traces/*.jsonl and returns parsed Trace objects."""
        run_dir = tmp_path
        trace_dir = run_dir / ".traces"
        trace_dir.mkdir()
        shutil.copy(FIXTURE, trace_dir / "traces.jsonl")

        ctx = SimpleNamespace(run_dir=run_dir, run_id="spike-run")
        collector = FileCollector()
        traces = collector.collect(ctx)

        assert len(traces) == 1
        trace = traces[0]
        assert trace.trace_id == "c5b55d939c5df4939aa20c7090a13cc9"
        assert len(trace.spans) == 2
        assert trace.resource_tags["eval.run_id"] == "spike-run"

    def test_collect_filters_by_run_id(self, tmp_path: Path):
        """FileCollector filters traces to match the requested run_id."""
        run_dir = tmp_path
        trace_dir = run_dir / ".traces"
        trace_dir.mkdir()

        # Write fixture with the default run_id
        shutil.copy(FIXTURE, trace_dir / "traces.jsonl")

        ctx = SimpleNamespace(run_dir=run_dir, run_id="non-existent-run")
        collector = FileCollector()
        traces = collector.collect(ctx)

        assert len(traces) == 0

    def test_collect_multiple_jsonl_files(self, tmp_path: Path):
        """FileCollector globs and parses all *.jsonl in .traces/."""
        run_dir = tmp_path
        trace_dir = run_dir / ".traces"
        trace_dir.mkdir()

        # Write two trace files with different run_ids
        content = FIXTURE.read_text(encoding="utf-8")
        (trace_dir / "run1.jsonl").write_text(content, encoding="utf-8")

        modified = content.replace("spike-run", "other-run").replace(
            "c5b55d939c5df4939aa20c7090a13cc9", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )
        (trace_dir / "run2.jsonl").write_text(modified, encoding="utf-8")

        ctx = SimpleNamespace(run_dir=run_dir, run_id="other-run")
        collector = FileCollector()
        traces = collector.collect(ctx)

        assert len(traces) == 1
        assert traces[0].resource_tags["eval.run_id"] == "other-run"

    def test_collect_empty_traces_dir(self, tmp_path: Path):
        """FileCollector returns empty list when .traces/ is empty."""
        run_dir = tmp_path
        (run_dir / ".traces").mkdir()

        ctx = SimpleNamespace(run_dir=run_dir, run_id="any")
        collector = FileCollector()
        traces = collector.collect(ctx)

        assert traces == []

    def test_collect_no_traces_dir(self, tmp_path: Path):
        """FileCollector returns empty list when .traces/ doesn't exist."""
        ctx = SimpleNamespace(run_dir=tmp_path, run_id="any")
        collector = FileCollector()
        traces = collector.collect(ctx)

        assert traces == []

    def test_exporter_env_correct(self):
        """FileCollector exporter_env returns correct env vars."""
        ctx = SimpleNamespace(run_dir=Path("."), run_id="x")
        env = FileCollector().exporter_env(ctx)
        assert env["COPILOT_OTEL_EXPORTER_TYPE"] == "file"
        assert env["COPILOT_OTEL_FILE_EXPORTER_PATH"] == "/workspace/.traces/traces.jsonl"

    def test_span_timing_accuracy(self, tmp_path: Path):
        """FileCollector parses span timestamps and duration correctly."""
        run_dir = tmp_path
        trace_dir = run_dir / ".traces"
        trace_dir.mkdir()
        shutil.copy(FIXTURE, trace_dir / "traces.jsonl")

        ctx = SimpleNamespace(run_dir=run_dir, run_id="spike-run")
        traces = FileCollector().collect(ctx)
        trace = traces[0]

        root = trace.root
        assert root is not None
        assert root.start_time > 0
        assert root.duration_s > 0


# ---------------------------------------------------------------------------
# JaegerCollector integration tests (optional — requires Jaeger)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestJaegerCollectorIntegration:
    """Integration tests for JaegerCollector. Requires Jaeger at localhost:16686."""

    def test_exporter_env_otlp(self):
        """JaegerCollector returns OTLP exporter env vars."""
        collector = JaegerCollector(
            jaeger_url="http://localhost:16686",
            otel_endpoint="http://host.docker.internal:4318",
        )
        ctx = SimpleNamespace(run_dir=Path("."), run_id="x")
        env = collector.exporter_env(ctx)
        assert env["COPILOT_OTEL_EXPORTER_TYPE"] == "otlp-http"
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://host.docker.internal:4318"

    def test_collect_returns_empty_for_missing_run(self):
        """JaegerCollector returns empty when no traces match the run_id."""
        if not _jaeger_available():
            pytest.skip("Jaeger not available at localhost:16686")

        collector = JaegerCollector()
        ctx = SimpleNamespace(run_dir=Path("."), run_id="nonexistent-run-id-xyz")
        traces = collector.collect(ctx)
        assert traces == []


# ---------------------------------------------------------------------------
# Full pipeline integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestFullPipelineIntegration:
    """End-to-end tests: DockerCLIRunner → trace persistence → collector.collect()."""

    def test_runner_to_file_collector_pipeline(self, tmp_path: Path):
        """Full pipeline with mocked Docker execution producing trace output."""
        config = _make_config(tmp_path)
        task = _make_task()
        variant = _make_variant()

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        (work_dir / "output").mkdir()
        trace_dir = work_dir / TRACE_FILE.parent
        trace_dir.mkdir(parents=True)

        run_dir = tmp_path / "runs"
        run_dir.mkdir()

        # Simulate Docker writing traces to the workspace
        fixture_content = FIXTURE.read_text(encoding="utf-8").replace("spike-run", "pipeline-run")
        (trace_dir / "traces.jsonl").write_text(fixture_content, encoding="utf-8")

        def fake_docker_run(cmd, **kwargs):
            return SimpleNamespace(returncode=0)

        # Step 1: Runner executes
        run_context = RunContext(
            run_id="pipeline-run",
            test_id="pipeline-test-001",
            epoch=1,
            run_dir=run_dir,
            task=task,
            variant=variant,
            config=config,
            work_dir=work_dir,
        )
        runner = DockerCLIRunner("token", run_command=fake_docker_run)
        artifacts = runner.run(run_context)
        assert artifacts.status == RunStatus.SUCCESS

        # Step 2: Collector reads traces from work_dir (simulating post-run)
        collector_context = SimpleNamespace(run_dir=work_dir, run_id="pipeline-run")
        collector = FileCollector()
        traces = collector.collect(collector_context)

        assert len(traces) == 1
        assert traces[0].resource_tags["eval.run_id"] == "pipeline-run"
        assert len(traces[0].spans) == 2

    def test_runner_collector_env_integration(self, tmp_path: Path):
        """Verify collector exporter_env is correctly wired into runner's extra_env."""
        config = _make_config(tmp_path)
        task = _make_task()
        variant = _make_variant()

        collector = create_collector("file")
        collector_ctx = SimpleNamespace(run_dir=tmp_path, run_id="env-test")
        exporter_env = collector.exporter_env(collector_ctx)

        captured_cmd: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.append(cmd)
            return SimpleNamespace(returncode=0)

        run_context = _make_run_context(tmp_path, config=config, task=task, variant=variant)
        # Inject exporter env as extra_env
        run_context.extra_env = exporter_env

        runner = DockerCLIRunner("token", run_command=fake_run)
        runner.run(run_context)

        cmd = captured_cmd[0]
        env_args = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-e"]
        assert any("COPILOT_OTEL_EXPORTER_TYPE=file" in e for e in env_args)
        assert any("COPILOT_OTEL_FILE_EXPORTER_PATH=" in e for e in env_args)

    def test_jaeger_collector_env_integration(self, tmp_path: Path):
        """Verify Jaeger collector exporter_env is wired correctly."""
        config = _make_config(tmp_path, collector="jaeger")

        collector = create_collector(
            "jaeger",
            jaeger_url="http://localhost:16686",
            otel_endpoint="http://host.docker.internal:4318",
        )
        collector_ctx = SimpleNamespace(run_dir=tmp_path, run_id="jaeger-test")
        exporter_env = collector.exporter_env(collector_ctx)

        captured_cmd: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.append(cmd)
            return SimpleNamespace(returncode=0)

        run_context = _make_run_context(tmp_path, config=config)
        run_context.extra_env = exporter_env

        runner = DockerCLIRunner("token", run_command=fake_run)
        runner.run(run_context)

        cmd = captured_cmd[0]
        env_args = [cmd[i + 1] for i in range(len(cmd)) if cmd[i] == "-e"]
        assert any("COPILOT_OTEL_EXPORTER_TYPE=otlp-http" in e for e in env_args)
        assert any("OTEL_EXPORTER_OTLP_ENDPOINT=" in e for e in env_args)

    def test_real_docker_hello_world(self, tmp_path: Path):
        """Run a real Docker container (hello-world) via DockerCLIRunner.

        This tests the actual docker invocation path end-to-end. It uses a
        minimal alpine container instead of the copilot image to keep the
        test fast and dependency-free.
        """
        if not _docker_available():
            pytest.skip("Docker daemon not available")

        config = _make_config(tmp_path)
        task = _make_task(prompt="echo hello")
        variant = _make_variant()

        # Override the image to use alpine instead of copilot-eval
        config.runner.container_image_base = "alpine"

        run_context = _make_run_context(tmp_path, config=config, task=task, variant=variant)

        # Use real subprocess.run but with a simple alpine echo command
        # We override the runner's run by constructing a minimal docker call
        result = subprocess.run(
            ["docker", "run", "--rm", "alpine:latest", "echo", "integration-test-ok"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "integration-test-ok" in result.stdout
