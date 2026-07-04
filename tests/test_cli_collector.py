"""Integration tests for collector type switching in CLI commands.

Exercises the branching logic in `run` and `analyze` commands that routes
between file and jaeger collectors, including --jaeger-url override behavior.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from click.testing import CliRunner

from eval.cli import main


def _write_config(tmp_path: Path, collector: str = "file", **runner_kwargs) -> Path:
    """Write a minimal eval-config.yaml and return the config dir."""
    runner = {"collector": collector, **runner_kwargs}
    config = {
        "runner": runner,
        "variants": [{"name": "baseline"}],
        "tasks": [{"name": "hello", "prompt": "say hello"}],
    }
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "eval-config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return config_dir


class TestRunCollectorSwitching:
    """Test that the `run` command correctly routes based on collector config."""

    def test_file_collector_does_not_call_ensure_jaeger(self, tmp_path: Path):
        """collector: file (default) should NOT call _ensure_jaeger."""
        config_dir = _write_config(tmp_path, collector="file")
        runner = CliRunner()

        with (
            patch("eval.cli._ensure_jaeger") as mock_ensure,
            patch("eval.cli.get_github_token", return_value="fake-token"),
            patch("eval.cli._ensure_images"),
            patch("eval.cli.run_one") as mock_run_one,
        ):
            mock_run_one.return_value = MagicMock(
                status=MagicMock(value="success"),
                task="hello",
                variant="baseline",
                epoch=1,
                to_dict=lambda: {
                    "status": "success",
                    "task": "hello",
                    "variant": "baseline",
                    "epoch": 1,
                },
            )
            result = runner.invoke(
                main, ["run", "--config-dir", str(config_dir), "--task", "hello"]
            )

        assert result.exit_code == 0, result.output
        mock_ensure.assert_not_called()

    def test_jaeger_collector_calls_ensure_jaeger(self, tmp_path: Path):
        """collector: jaeger should trigger _ensure_jaeger."""
        config_dir = _write_config(tmp_path, collector="jaeger")
        runner = CliRunner()

        with (
            patch("eval.cli._ensure_jaeger") as mock_ensure,
            patch("eval.cli.get_github_token", return_value="fake-token"),
            patch("eval.cli._ensure_images"),
            patch("eval.cli.run_one") as mock_run_one,
        ):
            mock_run_one.return_value = MagicMock(
                status=MagicMock(value="success"),
                task="hello",
                variant="baseline",
                epoch=1,
                to_dict=lambda: {
                    "status": "success",
                    "task": "hello",
                    "variant": "baseline",
                    "epoch": 1,
                },
            )
            result = runner.invoke(
                main, ["run", "--config-dir", str(config_dir), "--task", "hello"]
            )

        assert result.exit_code == 0, result.output
        mock_ensure.assert_called_once()


class TestAnalyzeCollectorRouting:
    """Test that `analyze` routes correctly to file vs jaeger collector."""

    def test_analyze_file_collector_reads_from_files(self, tmp_path: Path):
        """collector: file should call _collect_file_traces, not jaeger."""
        config_dir = _write_config(tmp_path, collector="file")
        # Create a results directory with an empty traces dir
        results_dir = tmp_path / "config" / "results" / "test-run-123"
        traces_dir = results_dir / ".traces"
        traces_dir.mkdir(parents=True)

        runner = CliRunner()

        with (
            patch("eval.cli._collect_file_traces", return_value=[]) as mock_file,
            patch("eval.cli._ensure_jaeger") as mock_ensure,
            patch("eval.cli._fetch_traces_for_run") as mock_jaeger_fetch,
        ):
            result = runner.invoke(
                main,
                ["analyze", "--run-id", "test-run-123", "--config-dir", str(config_dir)],
            )

        # File collector path taken
        mock_file.assert_called_once()
        # Jaeger path NOT taken
        mock_ensure.assert_not_called()
        mock_jaeger_fetch.assert_not_called()

    def test_analyze_jaeger_collector_fetches_from_jaeger(self, tmp_path: Path):
        """collector: jaeger should call _ensure_jaeger and _fetch_traces_for_run."""
        config_dir = _write_config(tmp_path, collector="jaeger")
        # Create results directory
        results_dir = tmp_path / "config" / "results" / "test-run-456"
        results_dir.mkdir(parents=True)

        runner = CliRunner()

        with (
            patch("eval.cli._collect_file_traces") as mock_file,
            patch("eval.cli._ensure_jaeger") as mock_ensure,
            patch("eval.cli._fetch_traces_for_run", return_value=[]) as mock_jaeger_fetch,
        ):
            result = runner.invoke(
                main,
                ["analyze", "--run-id", "test-run-456", "--config-dir", str(config_dir)],
            )

        # Jaeger path taken
        mock_ensure.assert_called_once()
        mock_jaeger_fetch.assert_called_once()
        # File collector NOT used
        mock_file.assert_not_called()

    def test_analyze_jaeger_url_override_forces_jaeger(self, tmp_path: Path):
        """--jaeger-url flag should force jaeger collector even when config says file."""
        config_dir = _write_config(tmp_path, collector="file")
        # Create results directory
        results_dir = tmp_path / "config" / "results" / "test-run-789"
        results_dir.mkdir(parents=True)

        runner = CliRunner()

        with (
            patch("eval.cli._collect_file_traces") as mock_file,
            patch("eval.cli._ensure_jaeger") as mock_ensure,
            patch("eval.cli._fetch_traces_for_run", return_value=[]) as mock_jaeger_fetch,
        ):
            result = runner.invoke(
                main,
                [
                    "analyze",
                    "--run-id",
                    "test-run-789",
                    "--jaeger-url",
                    "http://custom-jaeger:16686",
                    "--config-dir",
                    str(config_dir),
                ],
            )

        # Jaeger path taken due to --jaeger-url override
        mock_ensure.assert_called_once()
        # Verify the custom URL is passed to _ensure_jaeger
        call_args = mock_ensure.call_args
        assert (
            call_args[0][1] == "http://custom-jaeger:16686"
            or call_args[1].get("jaeger_url") == "http://custom-jaeger:16686"
            or "http://custom-jaeger:16686" in str(call_args)
        )
        mock_jaeger_fetch.assert_called_once()
        # File collector NOT used
        mock_file.assert_not_called()
