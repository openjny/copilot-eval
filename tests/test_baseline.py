"""Tests for cross-run baseline save/list/delete and `analyze --baseline`
regression tracking (issue #65)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from eval.cli import main
from eval.config import Config, load_config
from eval.report import build_baseline_comparisons
from eval.services.baseline_service import (
    BaselineError,
    delete_baseline,
    list_baselines,
    load_baseline,
    save_baseline,
)
from tests.conftest import make_metrics, write_config


@pytest.fixture(autouse=True)
def _isolated_results_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`Config.results_dir` always resolves relative to the framework's own
    install root (`eval/config.py`'s `load_config` hardcodes `project_dir`),
    not `--config-dir`. Redirect it into `tmp_path` so baseline read/write
    tests never touch this repo's real `results/` directory.
    """
    monkeypatch.setattr(Config, "results_dir", property(lambda self: tmp_path / "results"))


def _config(tmp_path: Path) -> Config:
    write_config(
        tmp_path,
        {
            "variants": [{"name": "current"}],
            "tasks": [{"name": "hello", "prompt": "say hello"}],
        },
    )
    return load_config(tmp_path)


# --- baseline_service: save/load/list/delete ---


def test_save_baseline_writes_json_snapshot(tmp_path: Path):
    config = _config(tmp_path)
    metrics = [
        make_metrics("hello", "current", "1", duration=1.0, cost=0.01),
        make_metrics("hello", "current", "2", duration=1.2, cost=0.012),
    ]
    path = save_baseline(config, "run-1", "prod", metrics)

    assert path == config.results_dir / ".baselines" / "prod.json"
    data = json.loads(path.read_text())
    assert data["name"] == "prod"
    assert data["run_id"] == "run-1"
    runs = data["tasks"]["hello"]["variants"]["current"]["runs"]
    assert len(runs) == 2
    assert {r["duration"] for r in runs} == {1.0, 1.2}


def test_save_baseline_rejects_empty_metrics(tmp_path: Path):
    config = _config(tmp_path)
    with pytest.raises(BaselineError):
        save_baseline(config, "run-1", "prod", [])


def test_load_baseline_missing_raises(tmp_path: Path):
    config = _config(tmp_path)
    with pytest.raises(BaselineError):
        load_baseline(config, "does-not-exist")


def test_load_baseline_roundtrip(tmp_path: Path):
    config = _config(tmp_path)
    metrics = [make_metrics("hello", "current", "1", duration=1.0)]
    save_baseline(config, "run-1", "prod", metrics)
    data = load_baseline(config, "prod")
    assert data["run_id"] == "run-1"


def test_list_baselines_empty(tmp_path: Path):
    config = _config(tmp_path)
    assert list_baselines(config) == []


def test_list_baselines_reports_counts(tmp_path: Path):
    config = _config(tmp_path)
    metrics = [
        make_metrics("hello", "current", "1"),
        make_metrics("hello", "current", "2"),
    ]
    save_baseline(config, "run-1", "prod", metrics)
    save_baseline(config, "run-2", "staging", metrics)

    entries = {e["name"]: e for e in list_baselines(config)}
    assert set(entries) == {"prod", "staging"}
    assert entries["prod"]["run_id"] == "run-1"
    assert entries["prod"]["tasks"] == 1
    assert entries["prod"]["variants"] == 1
    assert entries["prod"]["runs"] == 2


def test_delete_baseline_removes_file(tmp_path: Path):
    config = _config(tmp_path)
    metrics = [make_metrics("hello", "current", "1")]
    path = save_baseline(config, "run-1", "prod", metrics)
    assert path.exists()

    delete_baseline(config, "prod")
    assert not path.exists()


def test_delete_baseline_missing_raises(tmp_path: Path):
    config = _config(tmp_path)
    with pytest.raises(BaselineError):
        delete_baseline(config, "does-not-exist")


# --- report.build_baseline_comparisons: unpaired bootstrap comparison ---


def _baseline_payload(*, duration_vals: list[float], variant: str = "current") -> dict:
    return {
        "name": "prod",
        "run_id": "baseline-run",
        "tasks": {
            "hello": {
                "variants": {
                    variant: {
                        "runs": [
                            {"epoch": str(i), "duration": v} for i, v in enumerate(duration_vals)
                        ]
                    }
                }
            }
        },
    }


def test_build_baseline_comparisons_no_regression_when_similar():
    baseline_data = _baseline_payload(duration_vals=[1.0] * 10)
    current = [make_metrics("hello", "current", str(i), duration=1.0) for i in range(10)]

    comparisons, missing = build_baseline_comparisons(current, baseline_data, ["current"])

    assert missing == []
    assert len(comparisons) == 1
    comp = comparisons[0]
    assert comp.task == "hello"
    assert comp.variant == "current"
    assert comp.has_regression is False


def test_build_baseline_comparisons_flags_regression_on_clear_slowdown():
    # Baseline: consistently fast. Current: consistently much slower.
    baseline_data = _baseline_payload(duration_vals=[1.0] * 10)
    current = [make_metrics("hello", "current", str(i), duration=5.0) for i in range(10)]

    comparisons, missing = build_baseline_comparisons(
        current, baseline_data, ["current"], mc_correction="none"
    )

    assert missing == []
    comp = comparisons[0]
    assert comp.has_regression is True
    duration_row = next(r for r in comp.rows if r.metric == "Duration (s)")
    assert duration_row.regression is True
    assert duration_row.significant is True
    assert duration_row.ci_low is not None and duration_row.ci_low > 0


def test_build_baseline_comparisons_flags_improvement_not_regression():
    baseline_data = _baseline_payload(duration_vals=[5.0] * 10)
    current = [make_metrics("hello", "current", str(i), duration=1.0) for i in range(10)]

    comparisons, _missing = build_baseline_comparisons(
        current, baseline_data, ["current"], mc_correction="none"
    )
    comp = comparisons[0]
    assert comp.has_regression is False
    duration_row = next(r for r in comp.rows if r.metric == "Duration (s)")
    assert duration_row.improved is True
    assert duration_row.regression is False


def test_build_baseline_comparisons_insufficient_samples_not_significant():
    # Below MIN_RELIABLE_N (5): no claim of significance even with a big gap.
    baseline_data = _baseline_payload(duration_vals=[1.0, 1.0])
    current = [make_metrics("hello", "current", str(i), duration=5.0) for i in range(2)]

    comparisons, _missing = build_baseline_comparisons(current, baseline_data, ["current"])
    duration_row = next(r for r in comparisons[0].rows if r.metric == "Duration (s)")
    assert duration_row.significant is None
    assert duration_row.regression is False


def test_build_baseline_comparisons_missing_task_reported():
    baseline_data = _baseline_payload(duration_vals=[1.0] * 5)
    current = [make_metrics("other-task", "current", str(i), duration=1.0) for i in range(5)]

    comparisons, missing = build_baseline_comparisons(current, baseline_data, ["current"])
    assert comparisons == []
    assert missing == ["other-task"]


def test_build_baseline_comparisons_missing_variant_reported():
    baseline_data = _baseline_payload(duration_vals=[1.0] * 5, variant="old-variant")
    current = [make_metrics("hello", "new-variant", str(i), duration=1.0) for i in range(5)]

    comparisons, missing = build_baseline_comparisons(current, baseline_data, ["new-variant"])
    assert comparisons == []
    assert missing == ["hello/new-variant"]


# --- CLI: baseline save/list/delete ---


def _write_analyze_config(tmp_path: Path) -> Path:
    import yaml

    config = {
        "runner": {"collector": "file"},
        "variants": [{"name": "current"}],
        "tasks": [{"name": "hello", "prompt": "say hello"}],
    }
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "eval-config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return config_dir


def test_cli_baseline_save_list_delete(tmp_path: Path):
    config_dir = _write_analyze_config(tmp_path)
    runner = CliRunner()

    metrics = [make_metrics("hello", "current", "1", duration=1.0)]
    with patch("eval.cli.baseline_cmd.load_run_metrics", return_value=metrics):
        result = runner.invoke(
            main,
            [
                "baseline",
                "save",
                "--run-id",
                "run-1",
                "--name",
                "prod",
                "--config-dir",
                str(config_dir),
            ],
        )
    assert result.exit_code == 0, result.output
    assert "Saved baseline 'prod'" in result.output

    result = runner.invoke(main, ["baseline", "list", "--config-dir", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert "prod" in result.output
    assert "run-1" in result.output

    result = runner.invoke(
        main, ["baseline", "delete", "--name", "prod", "--config-dir", str(config_dir)]
    )
    assert result.exit_code == 0, result.output
    assert "Deleted baseline 'prod'" in result.output

    result = runner.invoke(main, ["baseline", "list", "--config-dir", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert "No baselines saved." in result.output


def test_cli_baseline_save_no_metrics_fails(tmp_path: Path):
    config_dir = _write_analyze_config(tmp_path)
    runner = CliRunner()

    with patch("eval.cli.baseline_cmd.load_run_metrics", return_value=[]):
        result = runner.invoke(
            main,
            [
                "baseline",
                "save",
                "--run-id",
                "run-1",
                "--name",
                "prod",
                "--config-dir",
                str(config_dir),
            ],
        )
    assert result.exit_code != 0
    assert "nothing to save" in result.output


def test_cli_baseline_delete_missing_fails(tmp_path: Path):
    config_dir = _write_analyze_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main, ["baseline", "delete", "--name", "nope", "--config-dir", str(config_dir)]
    )
    assert result.exit_code != 0


# --- CLI: analyze --baseline regression gating ---


def _fake_traces_for(metrics_list):
    """Fake Trace-like objects (only `.resource_tags` is read by run_analysis
    before judge/metric evaluation) paired with their extracted RunMetrics via
    a patched `extract_metrics`.
    """
    from types import SimpleNamespace

    traces = [SimpleNamespace(resource_tags={}) for _ in metrics_list]
    mapping = {id(t): m for t, m in zip(traces, metrics_list, strict=True)}
    return traces, mapping


def test_cli_analyze_baseline_regression_fails_when_gated(tmp_path: Path):
    config_dir = _write_analyze_config(tmp_path)
    config = load_config(config_dir)

    baseline_metrics = [make_metrics("hello", "current", str(i), duration=1.0) for i in range(10)]
    save_baseline(config, "baseline-run", "prod", baseline_metrics)

    results_dir = config_dir / "results" / "run-2"
    results_dir.mkdir(parents=True)

    current_metrics = [make_metrics("hello", "current", str(i), duration=5.0) for i in range(10)]
    traces, mapping = _fake_traces_for(current_metrics)

    runner = CliRunner()
    with (
        patch("eval.services.analyze_service._collect_file_traces", return_value=traces),
        patch(
            "eval.services.analyze_service.extract_metrics",
            side_effect=lambda t: mapping[id(t)],
        ),
        patch("eval.services.analyze_service._run_judges"),
        patch("eval.services.analyze_service._warn_unscored_judges"),
        patch("eval.services.analyze_service._run_metric_evaluators", return_value=[]),
        patch("eval.services.analyze_service._report_judge_reliability"),
    ):
        result = runner.invoke(
            main,
            [
                "analyze",
                "--run-id",
                "run-2",
                "--config-dir",
                str(config_dir),
                "--baseline",
                "prod",
                "--fail-on-regression",
                "--no-mc-correction",
            ],
        )

    assert result.exit_code != 0, result.output
    assert "Regression vs baseline" in str(result.output) or "Regression vs baseline" in str(
        result.exception
    )


def test_cli_analyze_baseline_no_gate_without_fail_flag(tmp_path: Path):
    """Same clear regression, but without --fail-on-regression (and no $CI env
    var) the command should still exit 0 -- gating is opt-in outside CI."""
    config_dir = _write_analyze_config(tmp_path)
    config = load_config(config_dir)

    baseline_metrics = [make_metrics("hello", "current", str(i), duration=1.0) for i in range(10)]
    save_baseline(config, "baseline-run", "prod", baseline_metrics)

    results_dir = config_dir / "results" / "run-2"
    results_dir.mkdir(parents=True)

    current_metrics = [make_metrics("hello", "current", str(i), duration=5.0) for i in range(10)]
    traces, mapping = _fake_traces_for(current_metrics)

    runner = CliRunner(env={"CI": ""})
    with (
        patch("eval.services.analyze_service._collect_file_traces", return_value=traces),
        patch(
            "eval.services.analyze_service.extract_metrics",
            side_effect=lambda t: mapping[id(t)],
        ),
        patch("eval.services.analyze_service._run_judges"),
        patch("eval.services.analyze_service._warn_unscored_judges"),
        patch("eval.services.analyze_service._run_metric_evaluators", return_value=[]),
        patch("eval.services.analyze_service._report_judge_reliability"),
    ):
        result = runner.invoke(
            main,
            [
                "analyze",
                "--run-id",
                "run-2",
                "--config-dir",
                str(config_dir),
                "--baseline",
                "prod",
                "--no-mc-correction",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Baseline comparison" in result.output


def test_cli_analyze_baseline_missing_name_fails(tmp_path: Path):
    config_dir = _write_analyze_config(tmp_path)
    results_dir = config_dir / "results" / "run-2"
    results_dir.mkdir(parents=True)

    metrics = [make_metrics("hello", "current", str(i), duration=1.0) for i in range(3)]
    traces, mapping = _fake_traces_for(metrics)

    runner = CliRunner()
    with (
        patch("eval.services.analyze_service._collect_file_traces", return_value=traces),
        patch(
            "eval.services.analyze_service.extract_metrics",
            side_effect=lambda t: mapping[id(t)],
        ),
        patch("eval.services.analyze_service._run_judges"),
        patch("eval.services.analyze_service._warn_unscored_judges"),
        patch("eval.services.analyze_service._run_metric_evaluators", return_value=[]),
        patch("eval.services.analyze_service._report_judge_reliability"),
    ):
        result = runner.invoke(
            main,
            [
                "analyze",
                "--run-id",
                "run-2",
                "--config-dir",
                str(config_dir),
                "--baseline",
                "does-not-exist",
            ],
        )
    assert result.exit_code != 0
