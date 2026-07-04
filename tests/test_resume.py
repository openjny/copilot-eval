"""Tests for `run --resume` (issue #67): scanning a prior run's manifest,
filtering the schedule down to failed/missing cells, merging new results
back into the existing run directory, and the CLI/orchestrator wiring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.config import Config, RunnerConfig, Task, Variant
from eval.protocols import RunStatus
from eval.runner import RunResult
from eval.services import orchestrator
from eval.services.manifest import MANIFEST_NAME, write_manifest
from eval.services.resume_service import (
    cell_key,
    completed_cells,
    filter_schedule,
    is_cell_complete,
    merge_manifest_runs,
    scan_run_results,
    warn_if_schedule_changed,
)

# --- shared helpers -----------------------------------------------------


def _config(tmp_path: Path, parallel: str = "off", max_workers: int = 4) -> Config:
    return Config(
        vars={},
        runner=RunnerConfig(parallel=parallel, max_workers=max_workers, timeout_seconds=300),
        tasks=[],
        variants=[Variant(name="baseline"), Variant(name="experimental")],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


def _task(name: str = "code-review") -> Task:
    return Task(name=name, prompt="do the thing")


def _result(
    task: str,
    variant: str,
    epoch: int,
    status: RunStatus = RunStatus.SUCCESS,
    run_id: str = "r1",
    fixture: str = "",
) -> RunResult:
    return RunResult(
        task=task,
        variant=variant,
        epoch=epoch,
        test_id=f"{task}-{variant}-{epoch}",
        run_id=run_id,
        log_file=Path(f"{task}_{variant}_epoch{epoch}.log"),
        exit_code=0 if status == RunStatus.SUCCESS else 1,
        status=status,
        fixture=fixture,
    )


# --- scan_run_results / completed_cells / is_cell_complete --------------


def test_scan_run_results_indexes_by_cell(tmp_path):
    run_dir = tmp_path / "results" / "r1"
    run_dir.mkdir(parents=True)
    results = [
        _result("t1", "baseline", 1, RunStatus.SUCCESS),
        _result("t1", "experimental", 1, RunStatus.FAILED),
    ]
    write_manifest(run_dir, "r1", results, schedule={})

    index = scan_run_results(run_dir)

    assert set(index.keys()) == {("t1", "baseline", 1, ""), ("t1", "experimental", 1, "")}
    assert index[("t1", "baseline", 1, "")]["status"] == "completed"
    assert index[("t1", "experimental", 1, "")]["status"] == "failed"


def test_scan_run_results_missing_manifest_returns_empty(tmp_path):
    run_dir = tmp_path / "results" / "r1"
    run_dir.mkdir(parents=True)

    assert scan_run_results(run_dir) == {}


def test_scan_run_results_corrupted_manifest_returns_empty(tmp_path):
    """A corrupted results.json must not crash resume -- everything is
    treated as missing (safe default: re-run rather than silently skip)."""
    run_dir = tmp_path / "results" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / MANIFEST_NAME).write_text("{not valid json")

    assert scan_run_results(run_dir) == {}


def test_scan_run_results_skips_malformed_rows(tmp_path):
    run_dir = tmp_path / "results" / "r1"
    run_dir.mkdir(parents=True)
    manifest = {
        "run_id": "r1",
        "schedule": {},
        "runs": [
            {"task": "t1", "variant": "baseline", "epoch": 1, "status": "completed"},
            {"task": "t1"},  # missing variant/epoch -- malformed
            "not-a-dict",  # wrong shape entirely
        ],
    }
    (run_dir / MANIFEST_NAME).write_text(json.dumps(manifest))

    index = scan_run_results(run_dir)

    assert list(index.keys()) == [("t1", "baseline", 1, "")]


@pytest.mark.parametrize(
    "record,expected",
    [
        (None, False),
        ({"status": "completed"}, True),
        ({"status": "failed"}, False),
        ({"status": "timeout"}, False),
        ({"status": "setup_failed"}, False),
    ],
)
def test_is_cell_complete(record, expected):
    assert is_cell_complete(record) is expected


def test_completed_cells_only_includes_successes(tmp_path):
    run_dir = tmp_path / "results" / "r1"
    run_dir.mkdir(parents=True)
    results = [
        _result("t1", "baseline", 1, RunStatus.SUCCESS),
        _result("t1", "experimental", 1, RunStatus.TIMEOUT),
        _result("t1", "baseline", 2, RunStatus.SETUP_FAILED),
    ]
    write_manifest(run_dir, "r1", results, schedule={})

    index = scan_run_results(run_dir)
    done = completed_cells(index)

    assert done == {("t1", "baseline", 1, "")}


# --- filter_schedule -----------------------------------------------------


def test_filter_schedule_drops_completed_cells():
    baseline = Variant(name="baseline")
    experimental = Variant(name="experimental")
    task = _task("t1")
    work = [(task, baseline, 1, ""), (task, experimental, 1, "")]

    filtered = filter_schedule(work, {("t1", "baseline", 1, "")})

    assert filtered == [(task, experimental, 1, "")]


def test_filter_schedule_no_completed_returns_full_list():
    baseline = Variant(name="baseline")
    task = _task("t1")
    work = [(task, baseline, 1, "")]

    assert filter_schedule(work, set()) == work


def test_filter_schedule_accepts_bare_name_strings():
    """Task/variant elements may already be plain strings (not objects)."""
    work = [("t1", "baseline", 1, ""), ("t1", "experimental", 1, "")]

    filtered = filter_schedule(work, {("t1", "baseline", 1, "")})

    assert filtered == [("t1", "experimental", 1, "")]


# --- merge_manifest_runs --------------------------------------------------


def test_merge_manifest_runs_overwrites_only_rerun_cells(tmp_path):
    run_dir = tmp_path / "results" / "r1"
    run_dir.mkdir(parents=True)
    original = [
        _result("t1", "baseline", 1, RunStatus.SUCCESS),
        _result("t1", "experimental", 1, RunStatus.FAILED),
    ]
    write_manifest(run_dir, "r1", original, schedule={})
    index = scan_run_results(run_dir)

    rerun = [_result("t1", "experimental", 1, RunStatus.SUCCESS)]
    merged = merge_manifest_runs(index, rerun)

    by_cell = {cell_key(r["task"], r["variant"], r["epoch"], r.get("fixture")): r for r in merged}
    assert len(merged) == 2
    assert by_cell[("t1", "baseline", 1, "")]["status"] == "completed"
    assert by_cell[("t1", "experimental", 1, "")]["status"] == "completed"  # now fixed


def test_merge_manifest_runs_adds_new_cells_not_in_original():
    merged = merge_manifest_runs({}, [_result("t1", "baseline", 1, RunStatus.SUCCESS)])
    assert len(merged) == 1
    assert merged[0]["task"] == "t1"


# --- warn_if_schedule_changed ---------------------------------------------


def test_warn_if_schedule_changed_prints_warning_on_diff(tmp_path, capsys):
    run_dir = tmp_path / "results" / "r1"
    run_dir.mkdir(parents=True)
    write_manifest(run_dir, "r1", [], schedule={"parallel": "off", "max_workers": 4})

    config = _config(tmp_path, parallel="full", max_workers=8)
    warn_if_schedule_changed(run_dir, config)

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "parallel" in captured.err


def test_warn_if_schedule_changed_silent_when_unchanged(tmp_path, capsys):
    run_dir = tmp_path / "results" / "r1"
    run_dir.mkdir(parents=True)
    write_manifest(
        run_dir,
        "r1",
        [],
        schedule={
            "parallel": "off",
            "max_workers": 4,
            "variant_order": "fixed",
            "seed": None,
        },
    )

    config = _config(tmp_path, parallel="off", max_workers=4)
    warn_if_schedule_changed(run_dir, config)

    assert capsys.readouterr().err == ""


def test_warn_if_schedule_changed_no_manifest_is_silent(tmp_path, capsys):
    run_dir = tmp_path / "results" / "r1"
    run_dir.mkdir(parents=True)

    warn_if_schedule_changed(run_dir, _config(tmp_path))

    assert capsys.readouterr().err == ""


# --- _execute_schedule with skip_cells ------------------------------------


def _fake_run_one_by_variant(fail_variants: set[str]):
    def _run(
        task, variant, epoch, config, run_id, run_dir, github_token, order_index=None, fixture=None
    ):
        status = RunStatus.FAILED if variant.name in fail_variants else RunStatus.SUCCESS
        return RunResult(
            task=task.name,
            variant=variant.name,
            epoch=epoch,
            test_id="t",
            run_id=run_id,
            log_file=run_dir / "x.log",
            exit_code=0 if status == RunStatus.SUCCESS else 1,
            status=status,
            order_index=order_index,
            duration_seconds=1.0,
        )

    return _run


@pytest.mark.parametrize("parallel", ["off", "full", "per_task"])
def test_execute_schedule_skips_completed_cells(tmp_path, monkeypatch, parallel):
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one_by_variant(set()))
    tasks = [_task("t1"), _task("t2")] if parallel == "per_task" else [_task("t1")]
    config = _config(tmp_path, parallel=parallel)

    skip_cells = {("t1", "baseline", 1, "")}
    results = orchestrator._execute_schedule(
        config,
        tasks,
        epochs=1,
        run_id="r1",
        run_dir=tmp_path,
        github_token="tok",
        skip_cells=skip_cells,
    )

    executed = {(r.task, r.variant, r.epoch) for r in results}
    assert ("t1", "baseline", 1) not in executed
    assert ("t1", "experimental", 1) in executed


def test_execute_schedule_no_skip_cells_runs_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one_by_variant(set()))
    config = _config(tmp_path, parallel="off")

    results = orchestrator._execute_schedule(
        config, [_task("t1")], epochs=1, run_id="r1", run_dir=tmp_path, github_token="tok"
    )

    assert len(results) == 2


# --- run_command end-to-end resume flow -----------------------------------


def _run_command_kwargs(config_dir=None, **overrides):
    base = dict(
        task=None,
        epochs=1,
        dry_run=False,
        no_build=True,
        skip_preflight=True,
        config_dir=config_dir,
        no_progress=True,
    )
    base.update(overrides)
    return base


def test_resume_reruns_only_failed_and_missing_cells(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")

    config = _config(tmp_path, parallel="off")
    config.tasks.append(_task("t1"))

    # First run: "experimental" fails.
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one_by_variant({"experimental"}))
    orchestrator.run_command(config, **_run_command_kwargs())

    run_dirs = list((tmp_path / "results").iterdir())
    assert len(run_dirs) == 1
    run_id = run_dirs[0].name

    manifest_path = run_dirs[0] / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    statuses = {(r["task"], r["variant"]): r["status"] for r in manifest["runs"]}
    assert statuses[("t1", "baseline")] == "completed"
    assert statuses[("t1", "experimental")] == "failed"

    # Resume: only the previously-failed "experimental" cell should re-run,
    # and this time it succeeds.
    calls: list[str] = []

    def _tracking_run_one(
        task, variant, epoch, config, run_id, run_dir, github_token, order_index=None, fixture=None
    ):
        calls.append(variant.name)
        return RunResult(
            task=task.name,
            variant=variant.name,
            epoch=epoch,
            test_id="t",
            run_id=run_id,
            log_file=run_dir / "x.log",
            exit_code=0,
            status=RunStatus.SUCCESS,
        )

    monkeypatch.setattr(orchestrator, "run_one", _tracking_run_one)
    orchestrator.run_command(config, **_run_command_kwargs(), resume=True, run_id=run_id)

    assert calls == ["experimental"]  # only the failed cell re-ran

    merged = json.loads(manifest_path.read_text())
    merged_statuses = {(r["task"], r["variant"]): r["status"] for r in merged["runs"]}
    assert merged_statuses[("t1", "baseline")] == "completed"
    assert merged_statuses[("t1", "experimental")] == "completed"
    assert len(merged["runs"]) == 2  # merged, not duplicated

    # Resume again: fully complete now, must be a no-op.
    calls.clear()
    orchestrator.run_command(config, **_run_command_kwargs(), resume=True, run_id=run_id)
    assert calls == []


def test_resume_requires_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    config = _config(tmp_path)
    config.tasks.append(_task("t1"))

    import click

    with pytest.raises(click.ClickException, match="--run-id"):
        orchestrator.run_command(config, **_run_command_kwargs(), resume=True, run_id=None)


def test_resume_unknown_run_id_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    config = _config(tmp_path)
    config.tasks.append(_task("t1"))

    import click

    with pytest.raises(click.ClickException, match="not found"):
        orchestrator.run_command(config, **_run_command_kwargs(), resume=True, run_id="nope")


def test_resume_with_corrupted_manifest_reruns_everything(tmp_path, monkeypatch):
    """A corrupted results.json must not block resume: since scan_run_results
    treats it as empty, every cell is (safely) considered missing and gets
    re-executed."""
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    config = _config(tmp_path, parallel="off")
    config.tasks.append(_task("t1"))

    run_dir = tmp_path / "results" / "existing-run"
    run_dir.mkdir(parents=True)
    (run_dir / MANIFEST_NAME).write_text("{not valid json")

    calls: list[str] = []

    def _tracking_run_one(
        task, variant, epoch, config, run_id, run_dir, github_token, order_index=None, fixture=None
    ):
        calls.append(variant.name)
        return RunResult(
            task=task.name,
            variant=variant.name,
            epoch=epoch,
            test_id="t",
            run_id=run_id,
            log_file=run_dir / "x.log",
            exit_code=0,
            status=RunStatus.SUCCESS,
        )

    monkeypatch.setattr(orchestrator, "run_one", _tracking_run_one)
    orchestrator.run_command(config, **_run_command_kwargs(), resume=True, run_id="existing-run")

    assert sorted(calls) == ["baseline", "experimental"]
