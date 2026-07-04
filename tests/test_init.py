"""Tests for the `init` command (issue #81): scaffolds a minimal, runnable
eval project and the generated config must pass `copilot-eval validate`."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from eval.cli import main

EXPECTED_FILES = [
    ".env.example",
    ".gitignore",
    "docker/Dockerfile.experimental",
    "eval-config.yaml",
    "fixtures/hello-world/README.md",
    "tasks/hello-world.yaml",
    "variants/baseline.yaml",
    "variants/experimental.yaml",
]


def test_init_scaffolds_all_expected_files(tmp_path: Path):
    target = tmp_path / "my-eval"
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--config-dir", str(target)])
    assert result.exit_code == 0, result.output
    for rel in EXPECTED_FILES:
        assert (target / rel).is_file(), f"missing {rel}"


def test_init_creates_target_directory_when_missing(tmp_path: Path):
    target = tmp_path / "nested" / "my-eval"
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--config-dir", str(target)])
    assert result.exit_code == 0, result.output
    assert target.is_dir()


def test_init_generated_config_passes_validate(tmp_path: Path):
    target = tmp_path / "my-eval"
    runner = CliRunner()
    init_result = runner.invoke(main, ["init", "--config-dir", str(target)])
    assert init_result.exit_code == 0, init_result.output

    validate_result = runner.invoke(main, ["validate", "--config-dir", str(target)])
    assert validate_result.exit_code == 0, validate_result.output
    assert "All checks passed." in validate_result.output or "passed" in validate_result.output


def test_init_schema_header_is_relative_and_resolves(tmp_path: Path):
    target = tmp_path / "my-eval"
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--config-dir", str(target)])
    assert result.exit_code == 0, result.output

    header = (target / "eval-config.yaml").read_text().splitlines()[0]
    assert header.startswith("# yaml-language-server: $schema=")
    schema_ref = header.split("$schema=", 1)[1].strip()
    assert (target / schema_ref).resolve().is_file()


def test_init_dockerfile_path_resolves_from_project_root(tmp_path: Path):
    """`build.dockerfile` is resolved relative to the repo root by
    eval.config/eval.validation (not --config-dir), regardless of where
    --config-dir points — see eval.cli.init_cmd module docstring."""
    target = tmp_path / "my-eval"
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--config-dir", str(target)])
    assert result.exit_code == 0, result.output

    project_dir = Path(__file__).resolve().parent.parent
    experimental = (target / "variants" / "experimental.yaml").read_text()
    dockerfile_rel = experimental.split("dockerfile:", 1)[1].strip().splitlines()[0]
    assert (project_dir / dockerfile_rel).resolve() == (
        target / "docker" / "Dockerfile.experimental"
    ).resolve()


def test_init_refuses_to_overwrite_without_force(tmp_path: Path):
    target = tmp_path / "my-eval"
    runner = CliRunner()
    first = runner.invoke(main, ["init", "--config-dir", str(target)])
    assert first.exit_code == 0, first.output

    second = runner.invoke(main, ["init", "--config-dir", str(target)])
    assert second.exit_code != 0
    assert "overwrite" in second.output.lower()


def test_init_force_overwrites_existing_files(tmp_path: Path):
    target = tmp_path / "my-eval"
    runner = CliRunner()
    first = runner.invoke(main, ["init", "--config-dir", str(target)])
    assert first.exit_code == 0, first.output

    (target / "eval-config.yaml").write_text("# tampered\n")
    second = runner.invoke(main, ["init", "--config-dir", str(target), "--force"])
    assert second.exit_code == 0, second.output
    assert "tampered" not in (target / "eval-config.yaml").read_text()


def test_init_unknown_template_errors(tmp_path: Path):
    target = tmp_path / "my-eval"
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--config-dir", str(target), "--template", "bogus"])
    assert result.exit_code != 0


def test_init_requires_config_dir():
    runner = CliRunner()
    result = runner.invoke(main, ["init"])
    assert result.exit_code != 0
    assert "config-dir" in result.output.lower() or "config_dir" in result.output.lower()


def test_init_does_not_overwrite_unrelated_files(tmp_path: Path):
    """A pre-existing, unrelated file in --config-dir must not block init."""
    target = tmp_path / "my-eval"
    target.mkdir(parents=True)
    (target / "notes.txt").write_text("keep me\n")

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--config-dir", str(target)])
    assert result.exit_code == 0, result.output
    assert (target / "notes.txt").read_text() == "keep me\n"
    assert (target / "eval-config.yaml").is_file()
