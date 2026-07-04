"""Tests for eval/validation.py: static config checks and readiness checks."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from click.testing import CliRunner

from eval.cli import main
from eval.config import ConfigError
from eval.validation import (
    any_failed,
    check_base_image,
    check_config_schema,
    check_disk_space,
    check_docker_daemon,
    check_fixtures,
    check_github_token,
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
    # configured, task falls back to task name -> missing, but that's a
    # non-blocking warning) + disk_space = 4 results.
    assert len(results) == 4
    assert any_failed(results)
    fixture_result = next(r for r in results if r.name == "fixture:t1")
    assert not fixture_result.blocking


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
            "eval.cli.validate_readiness",
            return_value=[MagicMock(passed=False, format=lambda: "  ✗ docker_daemon: down")],
        ),
        patch("eval.cli._ensure_images") as mock_ensure_images,
        patch("eval.cli._ensure_jaeger") as mock_ensure_jaeger,
        patch("eval.cli.get_github_token") as mock_token,
    ):
        result = runner.invoke(
            main, ["run", "--config-dir", str(config_dir), "--task", "t1"]
        )

    assert result.exit_code == 1
    mock_ensure_images.assert_not_called()
    mock_ensure_jaeger.assert_not_called()
    mock_token.assert_not_called()


def test_cli_run_dry_run_skips_preflight(tmp_path: Path):
    """--dry-run should not invoke pre-flight checks at all (no Docker/env needed)."""
    config_dir = tmp_path / "cfg"
    _write_yaml_config(config_dir, {"tasks": [{"name": "t1", "prompt": "hi"}]})
    runner = CliRunner()

    with patch("eval.cli.validate_readiness") as mock_validate:
        result = runner.invoke(
            main, ["run", "--config-dir", str(config_dir), "--task", "t1", "--dry-run"]
        )

    assert result.exit_code == 0, result.output
    mock_validate.assert_not_called()


def test_cli_run_proceeds_despite_missing_fixture_warning(tmp_path: Path):
    """Regression test for the reviewer-flagged azure-skills `compliance-audit`
    scenario: a task with no fixture dir on disk (relying solely on a
    `before_run` hook) must still be allowed to run — a missing fixture is a
    warning, not a blocking pre-flight failure."""
    config_dir = tmp_path / "cfg"
    _write_yaml_config(
        config_dir, {"tasks": [{"name": "compliance-audit", "prompt": "audit"}]}
    )
    runner = CliRunner()

    with (
        patch("eval.validation.check_docker_daemon", return_value=MagicMock(passed=True)),
        patch("eval.validation.check_github_token", return_value=MagicMock(passed=True)),
        patch("eval.validation.check_disk_space", return_value=MagicMock(passed=True)),
        patch("eval.cli.get_github_token", return_value="fake-token"),
        patch("eval.cli._ensure_images"),
        patch("eval.cli.run_one") as mock_run_one,
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
                "--no-build",
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
        patch("eval.cli.validate_readiness") as mock_validate,
        patch("eval.cli.get_github_token", return_value="fake-token"),
        patch("eval.cli._ensure_images"),
        patch("eval.cli.run_one") as mock_run_one,
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

