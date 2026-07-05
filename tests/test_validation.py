"""Tests for eval/validation.py: static config checks and readiness checks."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from eval.cli import main
from eval.config import ConfigError
from eval.validation import (
    any_failed,
    any_warnings,
    check_base_image,
    check_config_schema,
    check_disk_space,
    check_docker_daemon,
    check_fixtures,
    check_github_token,
    check_json_schema,
    check_script_references,
    check_var_interpolation,
    format_results,
    validate_readiness,
)
from tests.conftest import write_config

# --- check_config_schema ---


def test_check_config_schema_valid(tmp_path: Path):
    write_config(tmp_path, {"tasks": [{"name": "t1", "prompt": "hi"}]})
    config, result = check_config_schema(tmp_path)
    assert config is not None
    assert result.passed
    assert config.tasks[0].name == "t1"


def test_check_config_schema_missing_file(tmp_path: Path):
    config, result = check_config_schema(tmp_path / "nope")
    assert config is None
    assert not result.passed
    assert "not found" in result.message.lower() or "config not found" in result.message.lower()
    assert result.remediation


def test_check_config_schema_invalid_yaml_schema(tmp_path: Path):
    # duplicate task names -> ConfigError inside load_config
    write_config(
        tmp_path,
        {
            "tasks": [
                {"name": "dup", "prompt": "a"},
                {"name": "dup", "prompt": "b"},
            ]
        },
    )
    config, result = check_config_schema(tmp_path)
    assert config is None
    assert not result.passed
    assert "duplicate" in result.message.lower()
    assert result.remediation


def test_check_config_schema_bad_yaml_syntax(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "eval-config.yaml").write_text("tasks: [this is: not: valid", encoding="utf-8")
    config, result = check_config_schema(tmp_path)
    assert config is None
    assert not result.passed
    assert result.remediation


# --- check_fixtures ---


def test_check_fixtures_found(tmp_path: Path):
    write_config(tmp_path, {"tasks": [{"name": "t1", "prompt": "hi", "fixture": "my-app"}]})
    (tmp_path / "fixtures" / "my-app").mkdir(parents=True)
    from eval.config import load_config

    config = load_config(tmp_path)
    results = check_fixtures(config)
    assert len(results) == 1
    assert results[0].passed
    assert "my-app" in results[0].name


def test_check_fixtures_missing(tmp_path: Path):
    write_config(tmp_path, {"tasks": [{"name": "t1", "prompt": "hi", "fixture": "missing-app"}]})
    from eval.config import load_config

    config = load_config(tmp_path)
    results = check_fixtures(config)
    assert len(results) == 1
    assert not results[0].passed
    # Non-blocking: eval.runner tolerates a missing fixture dir, so this must
    # never fail `validate`/`run` pre-flight on its own.
    assert not results[0].blocking
    assert "missing-app" in results[0].message
    assert "fixtures/missing-app" in results[0].remediation


def test_check_fixtures_dedupes_shared_fixture(tmp_path: Path):
    write_config(
        tmp_path,
        {
            "tasks": [
                {"name": "t1", "prompt": "hi", "fixture": "shared"},
                {"name": "t2", "prompt": "yo", "fixture": "shared"},
            ]
        },
    )
    from eval.config import load_config

    config = load_config(tmp_path)
    results = check_fixtures(config)
    assert len(results) == 1


# --- check_script_references ---


def test_check_script_references_variant_dockerfile_missing(tmp_path: Path):
    write_config(
        tmp_path,
        {
            "tasks": [{"name": "t1", "prompt": "hi"}],
            "variants": [{"name": "v1", "build": {"dockerfile": "docker/Dockerfile.missing"}}],
        },
    )
    from eval.config import load_config

    config = load_config(tmp_path)
    results = check_script_references(config)
    dockerfile_results = [r for r in results if "dockerfile" in r.name]
    assert len(dockerfile_results) == 1
    assert not dockerfile_results[0].passed
    assert dockerfile_results[0].remediation


def test_check_script_references_hooks_and_evaluator_script_found(tmp_path: Path):
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts" / "reset.sh").write_text("#!/bin/sh\n")
    (tmp_path / "scripts" / "check.sh").write_text("#!/bin/sh\n")
    write_config(
        tmp_path,
        {
            "tasks": [
                {
                    "name": "t1",
                    "prompt": "hi",
                    "hooks": {"before_run": "scripts/reset.sh"},
                    "evaluators": [
                        {"name": "s", "type": "script", "script": "scripts/check.sh"},
                    ],
                }
            ],
        },
    )
    from eval.config import load_config

    config = load_config(tmp_path)
    results = check_script_references(config)
    assert all(r.passed for r in results)
    assert len(results) == 2


def test_check_script_references_hook_missing(tmp_path: Path):
    write_config(
        tmp_path,
        {
            "tasks": [
                {
                    "name": "t1",
                    "prompt": "hi",
                    "hooks": {"before_run": "scripts/does-not-exist.sh"},
                }
            ],
        },
    )
    from eval.config import load_config

    config = load_config(tmp_path)
    results = check_script_references(config)
    assert len(results) == 1
    assert not results[0].passed
    assert "before_run" in results[0].message


# --- check_var_interpolation ---


def test_check_var_interpolation_all_resolved(tmp_path: Path):
    write_config(
        tmp_path,
        {
            "vars": {"x": "1"},
            "tasks": [{"name": "t1", "prompt": "do {x}"}],
        },
    )
    from eval.config import load_config

    config = load_config(tmp_path)
    results = check_var_interpolation(config)
    assert len(results) == 1
    assert results[0].passed


def test_check_var_interpolation_missing_var(tmp_path: Path):
    write_config(
        tmp_path,
        {"tasks": [{"name": "t1", "prompt": "do {missing_var}"}]},
    )
    from eval.config import load_config

    config = load_config(tmp_path)
    results = check_var_interpolation(config)
    assert len(results) == 1
    assert not results[0].passed
    # Non-blocking: Config.resolve_prompt() leaves unresolved {tokens} as
    # literal text rather than erroring, so this must be a warning, not a
    # hard failure of `validate`/`run` pre-flight.
    assert not results[0].blocking
    assert "missing_var" in results[0].message
    assert "missing_var" in results[0].remediation


def test_check_var_interpolation_no_placeholders_yields_no_result(tmp_path: Path):
    write_config(tmp_path, {"tasks": [{"name": "t1", "prompt": "no placeholders here"}]})
    from eval.config import load_config

    config = load_config(tmp_path)
    results = check_var_interpolation(config)
    assert results == []


def test_check_var_interpolation_per_variant(tmp_path: Path):
    """A var defined only in one variant should fail for the other variant."""
    write_config(
        tmp_path,
        {
            "variants": [
                {"name": "a", "vars": {"lang": "English"}},
                {"name": "b"},
            ],
            "tasks": [{"name": "t1", "prompt": "respond in {lang}"}],
        },
    )
    from eval.config import load_config

    config = load_config(tmp_path)
    results = {r.name: r for r in check_var_interpolation(config)}
    assert results["vars:t1/a"].passed
    assert not results["vars:t1/b"].passed


def test_check_var_interpolation_brace_literal_is_warning_not_error(tmp_path: Path):
    """A prompt asking for literal-looking braces (e.g. JSON) runs fine today
    (Config.resolve_prompt leaves unresolved tokens as-is) — must not block."""
    write_config(
        tmp_path,
        {"tasks": [{"name": "t1", "prompt": "Emit JSON like {status}"}]},
    )
    from eval.config import load_config

    config = load_config(tmp_path)
    results = check_var_interpolation(config)
    assert len(results) == 1
    assert not results[0].passed
    assert not results[0].blocking
    assert any_failed(results) is False


# --- readiness checks ---


def test_check_docker_daemon_not_on_path():
    with patch("eval.validation.shutil.which", return_value=None):
        result = check_docker_daemon()
    assert not result.passed
    assert "PATH" in result.message


def test_check_docker_daemon_not_reachable():
    with (
        patch("eval.validation.shutil.which", return_value="/usr/bin/docker"),
        patch("eval.validation.subprocess.run", return_value=MagicMock(returncode=1)),
    ):
        result = check_docker_daemon()
    assert not result.passed
    assert result.remediation


def test_check_docker_daemon_reachable():
    with (
        patch("eval.validation.shutil.which", return_value="/usr/bin/docker"),
        patch("eval.validation.subprocess.run", return_value=MagicMock(returncode=0)),
    ):
        result = check_docker_daemon()
    assert result.passed


def test_check_docker_daemon_timeout():
    with (
        patch("eval.validation.shutil.which", return_value="/usr/bin/docker"),
        patch(
            "eval.validation.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker info", timeout=10),
        ),
    ):
        result = check_docker_daemon()
    assert not result.passed


def test_check_github_token_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc123")
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    result = check_github_token()
    assert result.passed


def test_check_github_token_from_copilot_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp_xyz456")
    result = check_github_token()
    assert result.passed


def test_check_github_token_from_gh_cli(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    with patch(
        "eval.validation.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="ghp_from_gh\n"),
    ):
        result = check_github_token()
    assert result.passed


def test_check_github_token_missing(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    with patch("eval.validation.subprocess.run", side_effect=FileNotFoundError()):
        result = check_github_token()
    assert not result.passed
    assert "gh auth login" in result.remediation


def test_check_disk_space_enough(tmp_path: Path):
    with patch(
        "eval.validation.shutil.disk_usage",
        return_value=MagicMock(free=1024 * 1024 * 1024),
    ):
        result = check_disk_space(tmp_path)
    assert result.passed


def test_check_disk_space_low(tmp_path: Path):
    with patch(
        "eval.validation.shutil.disk_usage",
        return_value=MagicMock(free=10 * 1024 * 1024),
    ):
        result = check_disk_space(tmp_path)
    assert not result.passed
    assert result.remediation


def test_check_disk_space_oserror(tmp_path: Path):
    with patch("eval.validation.shutil.disk_usage", side_effect=OSError("boom")):
        result = check_disk_space(tmp_path)
    assert not result.passed


def test_check_base_image_present(tmp_path: Path):
    from eval.config import load_config

    write_config(tmp_path, {})
    config = load_config(tmp_path)
    with patch("eval.validation.subprocess.run", return_value=MagicMock(returncode=0)):
        result = check_base_image(config)
    assert result.passed


def test_check_base_image_missing(tmp_path: Path):
    from eval.config import load_config

    write_config(tmp_path, {})
    config = load_config(tmp_path)
    with patch("eval.validation.subprocess.run", return_value=MagicMock(returncode=1)):
        result = check_base_image(config)
    assert not result.passed
    assert "build" in result.remediation.lower()


# --- validate_readiness composition ---


def test_validate_readiness_collects_all_and_skips_build_check(tmp_path: Path):
    from eval.config import load_config

    write_config(tmp_path, {"tasks": [{"name": "t1", "prompt": "hi"}]})
    config = load_config(tmp_path)

    with (
        patch("eval.validation.check_docker_daemon") as mock_docker,
        patch("eval.validation.check_github_token") as mock_token,
        patch("eval.validation.check_disk_space") as mock_disk,
        patch("eval.validation.check_base_image") as mock_base,
    ):
        mock_docker.return_value = MagicMock(passed=False)
        mock_token.return_value = MagicMock(passed=False)
        mock_disk.return_value = MagicMock(passed=True)

        results = validate_readiness(config, check_build=False)

        mock_base.assert_not_called()
    # docker + token (both blocking failures) + 1 fixture (no fixture
    # configured, task falls back to task name -> missing, but that's the
    # benign "no fixture declared" case, a passing check) + disk_space = 4.
    assert len(results) == 4
    assert any_failed(results)
    fixture_result = next(r for r in results if r.name == "fixture:t1")
    assert fixture_result.passed
    assert "No fixture declared" in fixture_result.message


def test_validate_readiness_includes_base_image_when_check_build_true(tmp_path: Path):
    from eval.config import load_config

    write_config(tmp_path, {"tasks": [{"name": "t1", "prompt": "hi"}]})
    (tmp_path / "fixtures" / "t1").mkdir(parents=True)
    config = load_config(tmp_path)

    with (
        patch("eval.validation.check_docker_daemon", return_value=MagicMock(passed=True)),
        patch("eval.validation.check_github_token", return_value=MagicMock(passed=True)),
        patch("eval.validation.check_disk_space", return_value=MagicMock(passed=True)),
        patch("eval.validation.check_base_image") as mock_base,
    ):
        mock_base.return_value = MagicMock(passed=True)
        results = validate_readiness(config, check_build=True)
        mock_base.assert_called_once()
    assert not any_failed(results)


# --- misc helpers ---


def test_any_failed_and_format_results():
    from eval.validation import CheckResult

    ok = CheckResult(name="a", passed=True, message="fine")
    bad = CheckResult(name="b", passed=False, message="broken", remediation="fix it")
    assert any_failed([ok]) is False
    assert any_failed([ok, bad]) is True
    text = format_results([ok, bad])
    assert "a: fine" in text
    assert "b: broken" in text
    assert "fix it" in text


def test_config_error_is_value_error():
    # Sanity check for the exception hierarchy check_config_schema relies on.
    assert issubclass(ConfigError, ValueError)


# --- `validate` CLI command ---


def _write_yaml_config(config_dir: Path, config: dict) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "eval-config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def test_cli_validate_passes_for_valid_config(tmp_path: Path):
    config_dir = tmp_path / "cfg"
    _write_yaml_config(
        config_dir,
        {"tasks": [{"name": "t1", "prompt": "hi"}]},
    )
    (config_dir / "fixtures" / "t1").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(main, ["validate", "--config-dir", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert "passed" in result.output.lower()


def test_cli_validate_warns_but_passes_for_missing_fixture(tmp_path: Path):
    """A missing fixture dir is surfaced as a warning, not a blocking failure —
    `eval.runner.run_one` tolerates it, so `validate` must exit 0 (regression
    test for the azure-skills `compliance-audit` task, which has no fixture
    dir at all and relies solely on a `before_run` hook)."""
    config_dir = tmp_path / "cfg"
    _write_yaml_config(
        config_dir,
        {"tasks": [{"name": "t1", "prompt": "hi", "fixture": "missing"}]},
    )
    runner = CliRunner()
    result = runner.invoke(main, ["validate", "--config-dir", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert "missing" in result.output
    assert "warning" in result.output.lower()


def test_cli_validate_fails_for_bad_schema(tmp_path: Path):
    config_dir = tmp_path / "cfg"
    _write_yaml_config(
        config_dir,
        {"runner": {"parallel": "not-a-real-mode"}, "tasks": [{"name": "t1", "prompt": "hi"}]},
    )
    runner = CliRunner()
    result = runner.invoke(main, ["validate", "--config-dir", str(config_dir)])
    assert result.exit_code == 1
    assert "config_schema" in result.output


# --- `run` pre-flight integration ---


def test_cli_run_fails_fast_on_preflight_without_touching_docker(tmp_path: Path):
    """When pre-flight fails, `run` must exit 1 without calling any Docker helpers."""
    config_dir = tmp_path / "cfg"
    _write_yaml_config(config_dir, {"tasks": [{"name": "t1", "prompt": "hi"}]})
    runner = CliRunner()

    with (
        patch(
            "eval.services.orchestrator.validate_readiness",
            return_value=[MagicMock(passed=False, format=lambda: "  ✗ docker_daemon: down")],
        ),
        patch("eval.services.orchestrator._ensure_images") as mock_ensure_images,
        patch("eval.services.orchestrator._ensure_jaeger") as mock_ensure_jaeger,
        patch("eval.services.orchestrator.get_github_token") as mock_token,
    ):
        result = runner.invoke(main, ["run", "--config-dir", str(config_dir), "--task", "t1"])

    assert result.exit_code == 1
    mock_ensure_images.assert_not_called()
    mock_ensure_jaeger.assert_not_called()
    mock_token.assert_not_called()


def test_cli_run_dry_run_skips_preflight(tmp_path: Path):
    """--dry-run should not invoke pre-flight checks at all (no Docker/env needed)."""
    config_dir = tmp_path / "cfg"
    _write_yaml_config(config_dir, {"tasks": [{"name": "t1", "prompt": "hi"}]})
    runner = CliRunner()

    with patch("eval.services.orchestrator.validate_readiness") as mock_validate:
        result = runner.invoke(
            main, ["run", "--config-dir", str(config_dir), "--task", "t1", "--dry-run"]
        )

    assert result.exit_code == 0, result.output
    mock_validate.assert_not_called()


@pytest.mark.parametrize(
    ("no_build_flag", "expected_check_build"),
    [([], False), (["--no-build"], True)],
)
def test_cli_run_check_build_matches_no_build_flag(
    tmp_path: Path, no_build_flag: list[str], expected_check_build: bool
):
    """Regression test: `run`'s pre-flight must only require the base image to
    already exist when auto-build is disabled (--no-build). When auto-build is
    enabled (the default), `_ensure_images()` builds the image itself, so
    pre-flight must NOT check for it beforehand — that inverted logic
    (`check_build=not no_build`) would hard-fail every first-time `run` on a
    fresh config, since the image can never exist before its first build."""
    config_dir = tmp_path / "cfg"
    _write_yaml_config(config_dir, {"tasks": [{"name": "t1", "prompt": "hi"}]})
    (config_dir / "fixtures" / "t1").mkdir(parents=True)
    runner = CliRunner()

    with (
        patch("eval.services.orchestrator.validate_readiness", return_value=[]) as mock_validate,
        patch("eval.services.orchestrator.get_github_token", return_value="fake-token"),
        patch("eval.services.orchestrator._ensure_images"),
        patch("eval.services.orchestrator.run_one") as mock_run_one,
    ):
        mock_run_one.return_value = MagicMock(
            status=MagicMock(value="success"),
            task="t1",
            variant="baseline",
            epoch=1,
            passed=True,
            to_dict=lambda: {"status": "success"},
        )
        result = runner.invoke(
            main,
            ["run", "--config-dir", str(config_dir), "--task", "t1", *no_build_flag],
        )

    assert result.exit_code == 0, result.output
    _, kwargs = mock_validate.call_args
    assert kwargs["check_build"] is expected_check_build


def test_cli_run_proceeds_despite_missing_fixture_warning(tmp_path: Path):
    """Regression: a task that *explicitly declares* a fixture whose directory is
    absent must still be allowed to run — a missing fixture is a warning, not a
    blocking pre-flight failure."""
    config_dir = tmp_path / "cfg"
    _write_yaml_config(
        config_dir,
        {"tasks": [{"name": "compliance-audit", "prompt": "audit", "fixture": "gone"}]},
    )
    runner = CliRunner()

    with (
        patch("eval.validation.check_docker_daemon", return_value=MagicMock(passed=True)),
        patch("eval.validation.check_github_token", return_value=MagicMock(passed=True)),
        patch("eval.validation.check_disk_space", return_value=MagicMock(passed=True)),
        patch("eval.services.orchestrator.get_github_token", return_value="fake-token"),
        patch("eval.services.orchestrator._ensure_images"),
        patch("eval.services.orchestrator.run_one") as mock_run_one,
    ):
        mock_run_one.return_value = MagicMock(
            status=MagicMock(value="success"),
            task="compliance-audit",
            variant="baseline",
            epoch=1,
            passed=True,
            to_dict=lambda: {"status": "success"},
        )
        result = runner.invoke(
            main,
            [
                "run",
                "--config-dir",
                str(config_dir),
                "--task",
                "compliance-audit",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "warning" in result.output.lower()
    mock_run_one.assert_called_once()


def test_cli_run_skip_preflight_bypasses_checks_entirely(tmp_path: Path):
    config_dir = tmp_path / "cfg"
    _write_yaml_config(config_dir, {"tasks": [{"name": "t1", "prompt": "hi"}]})
    runner = CliRunner()

    with (
        patch("eval.services.orchestrator.validate_readiness") as mock_validate,
        patch("eval.services.orchestrator.get_github_token", return_value="fake-token"),
        patch("eval.services.orchestrator._ensure_images"),
        patch("eval.services.orchestrator.run_one") as mock_run_one,
    ):
        mock_run_one.return_value = MagicMock(
            status=MagicMock(value="success"),
            task="t1",
            variant="baseline",
            epoch=1,
            passed=True,
            to_dict=lambda: {"status": "success"},
        )
        result = runner.invoke(
            main,
            ["run", "--config-dir", str(config_dir), "--task", "t1", "--skip-preflight"],
        )

    assert result.exit_code == 0, result.output
    mock_validate.assert_not_called()
    assert "skipped" in result.output.lower()


# --- check_json_schema: split-file strict validation (issue #127) ---


def _write_split_config(
    config_dir: Path,
    top: dict,
    tasks: dict[str, dict] | None = None,
    variants: dict[str, dict] | None = None,
) -> None:
    """Write an eval-config.yaml plus optional tasks/*.yaml and variants/*.yaml."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "eval-config.yaml").write_text(yaml.safe_dump(top), encoding="utf-8")
    for name, body in (tasks or {}).items():
        (config_dir / "tasks").mkdir(exist_ok=True)
        (config_dir / "tasks" / f"{name}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    for name, body in (variants or {}).items():
        (config_dir / "variants").mkdir(exist_ok=True)
        (config_dir / "variants" / f"{name}.yaml").write_text(
            yaml.safe_dump(body), encoding="utf-8"
        )


def test_check_json_schema_inline_valid(tmp_path: Path):
    write_config(tmp_path, {"tasks": [{"name": "t1", "prompt": "hi"}]})
    result = check_json_schema(tmp_path)
    assert result.passed
    assert "eval-config.yaml" in result.message


def test_check_json_schema_inline_typo_is_blocking(tmp_path: Path):
    write_config(tmp_path, {"runner": {"timeout_secods": 300}})
    result = check_json_schema(tmp_path)
    assert not result.passed
    assert result.blocking
    assert "timeout_secods" in result.message


def test_check_json_schema_split_valid(tmp_path: Path):
    _write_split_config(
        tmp_path,
        {"vars": {}},
        tasks={"good": {"name": "good", "prompt": "do a thing"}},
        variants={"baseline": {"name": "baseline", "description": "x"}},
    )
    result = check_json_schema(tmp_path)
    assert result.passed
    # Split files are covered, not skipped/degraded to a warning.
    assert "tasks/*.yaml" in result.message
    assert "variants/*.yaml" in result.message


def test_check_json_schema_split_task_typo_is_blocking(tmp_path: Path):
    _write_split_config(
        tmp_path,
        {"vars": {}},
        tasks={"typo": {"prompt": "hi", "timeout_secods": 30}},
    )
    result = check_json_schema(tmp_path)
    assert not result.passed
    assert result.blocking
    assert "tasks/typo.yaml" in result.message
    assert "timeout_secods" in result.message


def test_check_json_schema_split_variant_typo_is_blocking(tmp_path: Path):
    _write_split_config(
        tmp_path,
        {"vars": {}},
        variants={"v": {"name": "baseline", "descriptn": "typo"}},
    )
    result = check_json_schema(tmp_path)
    assert not result.passed
    assert result.blocking
    assert "variants/v.yaml" in result.message
    assert "descriptn" in result.message


def test_check_json_schema_split_task_without_name_is_allowed(tmp_path: Path):
    # A split task file may omit `name` (the loader falls back to the file stem),
    # so a name-less-but-otherwise-valid task must not be flagged.
    _write_split_config(
        tmp_path,
        {"vars": {}},
        tasks={"nameless": {"prompt": "do a thing"}},
    )
    result = check_json_schema(tmp_path)
    assert result.passed


def test_cli_validate_fails_for_split_file_typo(tmp_path: Path):
    config_dir = tmp_path / "cfg"
    _write_split_config(
        config_dir,
        {"vars": {}},
        tasks={"typo": {"prompt": "hi", "judge_batch": "tru"}},
    )
    runner = CliRunner()
    result = runner.invoke(main, ["validate", "--config-dir", str(config_dir)])
    assert result.exit_code == 1
    assert "json_schema" in result.output
    assert "tasks/typo.yaml" in result.output


# --- check_fixtures: implicit vs explicit (issue #129) ---


def test_check_fixtures_implicit_missing_is_benign(tmp_path: Path):
    """A task with no declared fixture whose task-name dir is absent is the
    benign fixed-answer case: a passing check, not a warning (issue #129)."""
    write_config(tmp_path, {"tasks": [{"name": "calib-high", "prompt": "hi"}]})
    from eval.config import load_config

    config = load_config(tmp_path)
    results = check_fixtures(config)
    assert len(results) == 1
    assert results[0].passed
    assert "No fixture declared" in results[0].message


def test_check_fixtures_explicit_missing_warns_with_clear_wording(tmp_path: Path):
    write_config(tmp_path, {"tasks": [{"name": "t1", "prompt": "hi", "fixture": "gone"}]})
    from eval.config import load_config

    config = load_config(tmp_path)
    results = check_fixtures(config)
    assert len(results) == 1
    r = results[0]
    assert not r.passed
    assert not r.blocking  # still non-blocking
    # Wording makes the non-blocking nature + fix obvious.
    assert "non-blocking" in r.message
    assert "remove the 'fixture:' declaration" in r.remediation


def test_cli_validate_judge_calibration_example_has_no_warnings():
    """The canonical judge-calibration example must validate cleanly (issue #129)."""
    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / "examples" / "judge-calibration"
    runner = CliRunner()
    result = runner.invoke(main, ["validate", "--config-dir", str(example)])
    assert result.exit_code == 0, result.output
    assert "warning" not in result.output.lower()
    assert "All checks passed" in result.output


# --- --strict flag (issue #128) ---


def _make_config_with_warning(config_dir: Path):
    from eval.config import load_config

    write_config(config_dir, {"tasks": [{"name": "t1", "prompt": "hi", "fixture": "gone"}]})
    return load_config(config_dir)


def test_any_warnings_true_for_missing_fixture(tmp_path: Path):
    config = _make_config_with_warning(tmp_path)
    results = check_fixtures(config)
    assert any_warnings(results)
    assert not any_failed(results)


def test_cli_validate_strict_promotes_warning_to_failure(tmp_path: Path):
    config_dir = tmp_path / "cfg"
    _write_yaml_config(
        config_dir,
        {"tasks": [{"name": "t1", "prompt": "hi", "fixture": "gone"}]},
    )
    runner = CliRunner()
    # Without --strict: warning, exit 0.
    lenient = runner.invoke(main, ["validate", "--config-dir", str(config_dir)])
    assert lenient.exit_code == 0, lenient.output
    # With --strict: same warning is promoted to a non-zero exit.
    strict = runner.invoke(main, ["validate", "--config-dir", str(config_dir), "--strict"])
    assert strict.exit_code == 1, strict.output
    assert "promoted to failure" in strict.output


def test_cli_validate_strict_auto_enabled_under_ci(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "cfg"
    _write_yaml_config(
        config_dir,
        {"tasks": [{"name": "t1", "prompt": "hi", "fixture": "gone"}]},
    )
    runner = CliRunner()
    monkeypatch.setenv("CI", "1")
    # CI set -> strict on by default -> non-zero on the warning.
    auto = runner.invoke(main, ["validate", "--config-dir", str(config_dir)])
    assert auto.exit_code == 1, auto.output
    # --no-strict overrides the CI default.
    override = runner.invoke(main, ["validate", "--config-dir", str(config_dir), "--no-strict"])
    assert override.exit_code == 0, override.output


def test_cli_validate_strict_passes_when_no_warnings(tmp_path: Path):
    config_dir = tmp_path / "cfg"
    _write_yaml_config(config_dir, {"tasks": [{"name": "t1", "prompt": "hi"}]})
    (config_dir / "fixtures" / "t1").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(main, ["validate", "--config-dir", str(config_dir), "--strict"])
    assert result.exit_code == 0, result.output


# --- readable Dockerfile/script paths (issue #130) ---


def test_check_script_references_renders_readable_dockerfile_path(tmp_path: Path):
    """An out-of-tree config dir must not render a deep ../../.. cwd-relative
    walk; the path should be config-dir-relative or absolute (issue #130)."""
    config_dir = tmp_path / "cfg"
    (config_dir / "docker").mkdir(parents=True)
    (config_dir / "docker" / "Dockerfile.exp").write_text("FROM x\n")
    # Store the dockerfile the way `init` does: relative to the project/repo dir
    # (so the Docker build context resolves), which yields a ../../.. walk.
    from eval.config import load_config

    write_config(
        config_dir,
        {
            "tasks": [{"name": "t1", "prompt": "hi"}],
            "variants": [{"name": "exp", "build": {"dockerfile": "docker/Dockerfile.exp"}}],
        },
    )
    config = load_config(config_dir)
    # Force the project_dir/dockerfile round-trip to produce a ../.. relative path.
    import os

    rel = os.path.relpath(config_dir / "docker" / "Dockerfile.exp", config.project_dir)
    config.variants[0] = replace(config.variants[0], dockerfile=rel)

    results = check_script_references(config)
    dockerfile_result = next(r for r in results if r.name.endswith(":dockerfile"))
    assert dockerfile_result.passed
    assert ".." not in dockerfile_result.message
    assert dockerfile_result.message == "Found docker/Dockerfile.exp"
