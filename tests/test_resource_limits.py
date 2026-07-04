"""Tests for configurable Docker container resource limits (issue #72).

Covers:
- `runner.resources` config parsing (defaults + explicit values).
- Validation of `cpus` / `memory` / `pids_limit` at config-load time.
- `DockerCLIRunner.run()` mapping resource limits to `--cpus` / `--memory` /
  `--pids-limit` docker flags.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from eval.config import Config, ConfigError, ResourceLimits, RunnerConfig, Task, Variant
from eval.protocols import RunContext
from eval.runners import DockerCLIRunner
from tests.conftest import load_inline

# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_resources_default_is_unset(tmp_path):
    cfg = load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "hi"}]})
    assert cfg.runner.resources == ResourceLimits(cpus=None, memory=None, pids_limit=None)


def test_resources_explicit_values(tmp_path):
    cfg = load_inline(
        tmp_path,
        {
            "runner": {"resources": {"cpus": "2.0", "memory": "4g", "pids_limit": 100}},
            "tasks": [{"name": "t1", "prompt": "hi"}],
        },
    )
    assert cfg.runner.resources == ResourceLimits(cpus="2.0", memory="4g", pids_limit=100)


@pytest.mark.parametrize("cpus", ["1", "0.5", "4.25", "16"])
def test_resources_cpus_valid_formats(tmp_path, cpus):
    cfg = load_inline(
        tmp_path,
        {"runner": {"resources": {"cpus": cpus}}, "tasks": [{"name": "t1", "prompt": "hi"}]},
    )
    assert cfg.runner.resources.cpus == cpus


@pytest.mark.parametrize("cpus", ["", "abc", "-1", "1,5", "1.2.3", "1x"])
def test_resources_cpus_invalid_formats_rejected(tmp_path, cpus):
    with pytest.raises(ConfigError, match="runner.resources.cpus"):
        load_inline(
            tmp_path,
            {"runner": {"resources": {"cpus": cpus}}, "tasks": [{"name": "t1", "prompt": "hi"}]},
        )


def test_resources_cpus_zero_rejected(tmp_path):
    with pytest.raises(ConfigError, match="cpus must be > 0"):
        load_inline(
            tmp_path,
            {"runner": {"resources": {"cpus": "0"}}, "tasks": [{"name": "t1", "prompt": "hi"}]},
        )


@pytest.mark.parametrize("memory", ["512m", "2g", "1073741824", "256k", "1b", "2.5g", "4G", "8M"])
def test_resources_memory_valid_formats(tmp_path, memory):
    cfg = load_inline(
        tmp_path,
        {"runner": {"resources": {"memory": memory}}, "tasks": [{"name": "t1", "prompt": "hi"}]},
    )
    assert cfg.runner.resources.memory == memory


@pytest.mark.parametrize("memory", ["", "abc", "-1g", "4gb", "1.2.3m", "512 m"])
def test_resources_memory_invalid_formats_rejected(tmp_path, memory):
    with pytest.raises(ConfigError, match="runner.resources.memory"):
        load_inline(
            tmp_path,
            {
                "runner": {"resources": {"memory": memory}},
                "tasks": [{"name": "t1", "prompt": "hi"}],
            },
        )


@pytest.mark.parametrize("pids_limit", [1, 100, 4096])
def test_resources_pids_limit_valid(tmp_path, pids_limit):
    cfg = load_inline(
        tmp_path,
        {
            "runner": {"resources": {"pids_limit": pids_limit}},
            "tasks": [{"name": "t1", "prompt": "hi"}],
        },
    )
    assert cfg.runner.resources.pids_limit == pids_limit


@pytest.mark.parametrize("pids_limit", [0, -1, -100])
def test_resources_pids_limit_non_positive_rejected(tmp_path, pids_limit):
    with pytest.raises(ConfigError, match="runner.resources.pids_limit"):
        load_inline(
            tmp_path,
            {
                "runner": {"resources": {"pids_limit": pids_limit}},
                "tasks": [{"name": "t1", "prompt": "hi"}],
            },
        )


def test_resources_pids_limit_non_integer_rejected(tmp_path):
    with pytest.raises(ConfigError, match="runner.resources.pids_limit"):
        load_inline(
            tmp_path,
            {
                "runner": {"resources": {"pids_limit": "100"}},
                "tasks": [{"name": "t1", "prompt": "hi"}],
            },
        )


def test_resources_must_be_a_mapping(tmp_path):
    with pytest.raises(ConfigError, match="runner.resources must be a mapping"):
        load_inline(
            tmp_path,
            {"runner": {"resources": ["cpus", "2.0"]}, "tasks": [{"name": "t1", "prompt": "hi"}]},
        )


# ---------------------------------------------------------------------------
# DockerCLIRunner: resource limits -> docker run flags
# ---------------------------------------------------------------------------


def _make_run_context(tmp_path: Path, resources: ResourceLimits) -> RunContext:
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    task = Task(name="test-task", prompt="hello")
    variant = Variant(name="test-variant")

    env_file = tmp_path / ".env"
    env_file.write_text("")

    config = Config(
        vars={},
        runner=RunnerConfig(timeout_seconds=60, resources=resources),
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


def _run_and_capture_cmd(tmp_path: Path, resources: ResourceLimits) -> list[str]:
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return SimpleNamespace(returncode=0)

    runner = DockerCLIRunner("token", run_command=fake_run)
    ctx = _make_run_context(tmp_path, resources)

    with patch.dict("os.environ", {"COPILOT_HOME": str(tmp_path / "nonexistent")}):
        runner.run(ctx)

    return captured_cmd


def test_no_resource_limits_omits_flags(tmp_path):
    """Default (no limits) preserves current behavior: no resource flags at all."""
    cmd = _run_and_capture_cmd(tmp_path, ResourceLimits())

    assert "--cpus" not in cmd
    assert "--memory" not in cmd
    assert "--pids-limit" not in cmd


def test_cpus_flag_included_when_set(tmp_path):
    cmd = _run_and_capture_cmd(tmp_path, ResourceLimits(cpus="2.0"))

    assert "--cpus" in cmd
    assert cmd[cmd.index("--cpus") + 1] == "2.0"


def test_memory_flag_included_when_set(tmp_path):
    cmd = _run_and_capture_cmd(tmp_path, ResourceLimits(memory="4g"))

    assert "--memory" in cmd
    assert cmd[cmd.index("--memory") + 1] == "4g"


def test_pids_limit_flag_included_when_set(tmp_path):
    cmd = _run_and_capture_cmd(tmp_path, ResourceLimits(pids_limit=100))

    assert "--pids-limit" in cmd
    assert cmd[cmd.index("--pids-limit") + 1] == "100"


def test_all_resource_flags_included_together(tmp_path):
    cmd = _run_and_capture_cmd(tmp_path, ResourceLimits(cpus="1.5", memory="512m", pids_limit=50))

    assert cmd[cmd.index("--cpus") + 1] == "1.5"
    assert cmd[cmd.index("--memory") + 1] == "512m"
    assert cmd[cmd.index("--pids-limit") + 1] == "50"

    # Resource flags must precede the image name so docker treats them as run
    # options rather than arguments passed to the container's entrypoint.
    image_index = cmd.index("copilot-eval:test-variant")
    assert cmd.index("--cpus") < image_index
    assert cmd.index("--memory") < image_index
    assert cmd.index("--pids-limit") < image_index
