"""Tests for the DockerCLIRunner abstraction."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from eval.runners import DockerCLIRunner
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
