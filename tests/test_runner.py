"""Tests for side-effect-free helpers in runner.py.

Covers status mapping, JSON parsing, env-file parsing (including quote
stripping), directory reading, RunResult serialization, secret collection,
and secret masking.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from eval.config import Config, RunnerConfig, Variant
from eval.runner import (
    EvalScore,
    RunResult,
    _load_env_file,
    _mask_log_file,
    _parse_json,
    _strip_quotes,
    _write_sanitized_env_file,
    collect_secrets,
    mask_secrets,
    read_files_from_dir,
    status_from_exit_code,
)


def _config(tmp_path: Path) -> Config:
    return Config(
        vars={}, runner=RunnerConfig(), tasks=[], variants=[],
        project_dir=tmp_path, config_dir=tmp_path,
    )


# --- status_from_exit_code ---

@pytest.mark.parametrize("code,expected", [
    (0, "completed"),
    (124, "timeout"),
    (1, "failed"),
    (2, "failed"),
    (137, "failed"),
    (-1, "failed"),
])
def test_status_from_exit_code(code, expected):
    assert status_from_exit_code(code) == expected


# --- _parse_json ---

def test_parse_json_single_line():
    assert _parse_json('{"score": 5, "reason": "ok"}') == {"score": 5, "reason": "ok"}


def test_parse_json_whole_text_multiline():
    text = '{\n  "score": 3,\n  "reason": "fine"\n}'
    assert _parse_json(text) == {"score": 3, "reason": "fine"}


def test_parse_json_code_fence_with_lang():
    text = 'Here is the verdict:\n```json\n{"score": 7, "reason": "good"}\n```'
    assert _parse_json(text) == {"score": 7, "reason": "good"}


def test_parse_json_code_fence_no_lang():
    text = '```\n{"score": 1}\n```'
    assert _parse_json(text) == {"score": 1}


def test_parse_json_embedded_in_prose_multiline():
    text = (
        "The model evaluated the output and concluded:\n"
        '{\n  "score": 4,\n  "reason": "acceptable"\n}\n'
        "That is the final answer."
    )
    assert _parse_json(text) == {"score": 4, "reason": "acceptable"}


def test_parse_json_fence_is_only_valid_candidate():
    # Surrounding prose contains brace-noise that is not valid JSON, so the
    # match must come from the code-fence branch specifically.
    text = 'noise {not json}\n```json\n{\n  "score": 7\n}\n```\nmore {bad}'
    assert _parse_json(text) == {"score": 7}


def test_parse_json_only_first_fence_is_considered():
    # The fence regex is non-greedy and only the first fenced block is captured.
    # The first block lacks `score`; since require_keys filters it out and the
    # later (valid) fenced block is never extracted (the brace/single-line
    # fallbacks can't isolate a multiline object after noise), the result is
    # None. This documents the current first-fence-only behavior.
    text = (
        '```json\n{"note": "thinking"}\n```\n'
        '```json\n{\n  "score": 5\n}\n```'
    )
    assert _parse_json(text, require_keys=("score",)) is None


def test_parse_json_require_keys_accepts_matching():
    text = '{"score": 9, "reason": "great"}'
    assert _parse_json(text, require_keys=("score",)) == {"score": 9, "reason": "great"}


def test_parse_json_require_keys_rejects_fragment_then_finds_valid():
    # A stray object without `score` precedes the real verdict; require_keys
    # should skip the fragment and accept the object that has the key.
    text = (
        '{"note": "thinking"}\n'
        '{"score": 8, "reason": "valid"}'
    )
    assert _parse_json(text, require_keys=("score",)) == {"score": 8, "reason": "valid"}


def test_parse_json_require_keys_all_missing_returns_none():
    assert _parse_json('{"reason": "no score here"}', require_keys=("score",)) is None


def test_parse_json_rejects_non_dict():
    assert _parse_json("[1, 2, 3]") is None
    assert _parse_json("42") is None


def test_parse_json_empty_returns_none():
    assert _parse_json("") is None
    assert _parse_json("   ") is None


def test_parse_json_unparsable_returns_none():
    assert _parse_json("not json at all") is None


# --- _load_env_file ---

def test_load_env_file_basic(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "\n"
        "FOO=bar\n"
        "  BAZ = qux  \n"
    )
    assert _load_env_file(env_file) == {"FOO": "bar", "BAZ": "qux"}


def test_load_env_file_value_with_equals(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("URL=http://h/?a=1&b=2\n")
    assert _load_env_file(env_file) == {"URL": "http://h/?a=1&b=2"}


def test_load_env_file_skips_comments_and_blanks(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("#KEY=ignored\n\n   \nLINE_WITHOUT_EQUALS\nA=1\n")
    assert _load_env_file(env_file) == {"A": "1"}


def test_load_env_file_missing_returns_empty(tmp_path: Path):
    assert _load_env_file(tmp_path / "nope.env") == {}


def test_load_env_file_strips_quotes(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        '# comment\n'
        'PLAIN=value\n'
        'DQ="quoted value"\n'
        "SQ='single quoted'\n"
        '\n'
        'EMPTY=\n'
    )
    parsed = _load_env_file(env)
    assert parsed == {
        "PLAIN": "value",
        "DQ": "quoted value",
        "SQ": "single quoted",
        "EMPTY": "",
    }


# --- _strip_quotes ---

def test_strip_quotes_removes_matching_pairs():
    assert _strip_quotes('"value"') == "value"
    assert _strip_quotes("'value'") == "value"


def test_strip_quotes_leaves_unmatched_or_bare():
    assert _strip_quotes("value") == "value"
    assert _strip_quotes('"value') == '"value'
    assert _strip_quotes("'value\"") == "'value\""
    assert _strip_quotes('"') == '"'


# --- read_files_from_dir ---

def test_read_files_from_dir_none_or_not_dir(tmp_path: Path):
    assert read_files_from_dir(None) is None
    assert read_files_from_dir(tmp_path / "missing") is None


def test_read_files_from_dir_empty_dir(tmp_path: Path):
    d = tmp_path / "empty"
    d.mkdir()
    assert read_files_from_dir(d) is None


def test_read_files_from_dir_concatenates_sorted_with_headers(tmp_path: Path):
    d = tmp_path / "out"
    d.mkdir()
    (d / "b.txt").write_text("second")
    (d / "a.txt").write_text("first")
    result = read_files_from_dir(d)
    assert result == "=== a.txt ===\nfirst\n\n=== b.txt ===\nsecond"


def test_read_files_from_dir_includes_nested_files(tmp_path: Path):
    d = tmp_path / "out"
    d.mkdir()
    (d / "top.txt").write_text("top")
    (d / "sub").mkdir()
    (d / "sub" / "x.txt").write_text("nested")
    result = read_files_from_dir(d)
    assert "=== sub/x.txt ===\nnested" in result
    assert "=== top.txt ===\ntop" in result


def test_read_files_from_dir_truncates_at_max_chars(tmp_path: Path):
    d = tmp_path / "out"
    d.mkdir()
    (d / "a.txt").write_text("0123456789")
    (d / "b.txt").write_text("ABCDEFGHIJ")
    # a.txt fills 10 chars (total==10), b.txt would exceed max_chars=15.
    result = read_files_from_dir(d, max_chars=15)
    assert "=== a.txt ===\n0123456789" in result
    assert result.rstrip().endswith("... (truncated)")
    # Only the remaining 5 chars of b.txt are included.
    assert "ABCDE" in result
    assert "ABCDEF" not in result


def test_read_files_from_dir_stops_when_no_remaining_budget(tmp_path: Path):
    d = tmp_path / "out"
    d.mkdir()
    (d / "a.txt").write_text("0123456789")
    (d / "b.txt").write_text("ABCDEFGHIJ")
    # a.txt exactly fills max_chars; b.txt has zero remaining budget. Its content
    # is dropped, but the omission is surfaced (not silent) so the judge context
    # and truncation metadata reflect the missing file.
    result = read_files_from_dir(d, max_chars=10)
    assert "=== a.txt ===\n0123456789" in result
    assert "ABCDEFGHIJ" not in result
    assert "omitted 1 file(s): b.txt" in result
    assert result.rstrip().endswith("... (truncated)")


# --- RunResult.passed / to_dict ---

def _make_result(status: str, scores: list[EvalScore], exit_code: int = 0) -> RunResult:
    return RunResult(
        task="t", variant="v", epoch=1, test_id="tid", run_id="rid",
        log_file=Path("/tmp/x.log"), exit_code=exit_code, status=status, scores=scores,
    )


def test_passed_false_when_status_not_completed():
    r = _make_result("failed", [EvalScore(name="s", type="contains", score=1, passed=True)])
    assert r.passed is False


def test_passed_true_when_completed_and_all_scores_pass():
    scores = [
        EvalScore(name="a", type="contains", score=1, passed=True),
        EvalScore(name="b", type="regex", score=1, passed=True),
    ]
    assert _make_result("completed", scores).passed is True


def test_passed_false_when_any_score_fails():
    scores = [
        EvalScore(name="a", type="contains", score=1, passed=True),
        EvalScore(name="b", type="regex", score=0, passed=False),
    ]
    assert _make_result("completed", scores).passed is False


def test_passed_true_when_completed_and_no_scores():
    assert _make_result("completed", []).passed is True


def test_to_dict_structure():
    scores = [EvalScore(name="a", type="contains", score=1, reason="found", passed=True)]
    d = _make_result("completed", scores, exit_code=0).to_dict()
    assert d == {
        "task": "t",
        "variant": "v",
        "epoch": 1,
        "test_id": "tid",
        "run_id": "rid",
        "exit_code": 0,
        "status": "completed",
        "passed": True,
        "scores": [
            {"name": "a", "type": "contains", "score": 1, "reason": "found", "passed": True},
        ],
    }


# --- collect_secrets / mask_secrets ---

def test_collect_secrets_filters_short_values(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text('FLAG=1\nBOOL=true\nSECRET="supersecretvalue"\n')
    secrets = collect_secrets(_config(tmp_path))
    assert "supersecretvalue" in secrets
    assert "1" not in secrets
    assert "true" not in secrets


def test_collect_secrets_includes_token(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    (tmp_path / ".env").write_text("")
    secrets = collect_secrets(_config(tmp_path), token="ghp_tokenvalue123")
    assert "ghp_tokenvalue123" in secrets


def test_mask_secrets_replaces_all_occurrences():
    secrets = ["supersecretvalue", "ghp_tokenvalue123"]
    text = "key=supersecretvalue and token ghp_tokenvalue123 here supersecretvalue"
    masked = mask_secrets(text, secrets)
    assert "supersecretvalue" not in masked
    assert "ghp_tokenvalue123" not in masked
    assert masked.count("***REDACTED***") == 3


def test_mask_secrets_noop_for_empty():
    assert mask_secrets("", ["x"]) == ""
    assert mask_secrets("text", []) == "text"
    assert mask_secrets(None, ["x"]) is None


def test_write_sanitized_env_file_strips_quotes(tmp_path):
    config = _config(tmp_path)
    (tmp_path / ".env").write_text('DQ="quoted value"\nPLAIN=value\n')
    out = _write_sanitized_env_file(config)
    try:
        assert out != config.env_file
        assert (out.stat().st_mode & 0o777) == 0o600
        content = out.read_text()
        assert "DQ=quoted value\n" in content
        assert "PLAIN=value\n" in content
        assert '"' not in content
    finally:
        out.unlink(missing_ok=True)


def test_write_sanitized_env_file_handles_missing_env(tmp_path):
    out = _write_sanitized_env_file(_config(tmp_path))
    try:
        assert out.exists()
        assert out.read_text() == ""
    finally:
        out.unlink(missing_ok=True)


# --- run_one failure isolation & hook policy ---

def _stub_no_docker(monkeypatch, runner_mod, *, docker_rc: int = 0):
    """Stub out everything in run_one that touches Docker / external tools so the
    function can be driven end-to-end in a unit test."""
    monkeypatch.setattr(runner_mod, "_run_health_check", lambda *a, **k: True)
    monkeypatch.setattr(runner_mod, "_run_evaluators", lambda *a, **k: [])
    monkeypatch.setattr(runner_mod, "_persist_output_files", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "_print_summary", lambda *a, **k: None)

    class _Proc:
        returncode = docker_rc

    monkeypatch.setattr(runner_mod.subprocess, "run", lambda *a, **k: _Proc())


def test_run_one_setup_exception_returns_setup_failed(tmp_path, monkeypatch):
    """An exception raised during setup must be isolated, not propagated."""
    from eval import runner as runner_mod
    from eval.config import Task

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config = _config(tmp_path)
    run_dir = tmp_path / "results"
    run_dir.mkdir()

    def boom(*a, **k):
        raise RuntimeError("docker binary missing")

    monkeypatch.setattr(runner_mod, "_run_hook", boom)

    task = Task(name="t", prompt="p")
    result = runner_mod.run_one(
        task, Variant(name="v"), epoch=1, config=config, run_id="r",
        run_dir=run_dir, github_token="tok",
    )

    assert result.status == "setup_failed"
    assert result.exit_code == -1
    assert "docker binary missing" in result.log_file.read_text()


def test_run_one_before_run_fail_aborts(tmp_path, monkeypatch):
    """before_run hook returning non-zero with on_failure=fail -> setup_failed."""
    from eval import runner as runner_mod
    from eval.config import Hooks, Task

    config = _config(tmp_path)
    run_dir = tmp_path / "results"
    run_dir.mkdir()
    monkeypatch.setattr(runner_mod, "_run_hook", lambda *a, **k: 1)

    task = Task(name="t", prompt="p", hooks=Hooks(before_run="b.sh", on_failure="fail"))
    result = runner_mod.run_one(
        task, Variant(name="v"), epoch=1, config=config, run_id="r",
        run_dir=run_dir, github_token="tok",
    )

    assert result.status == "setup_failed"
    assert result.exit_code == -1


def test_run_one_before_run_warn_continues(tmp_path, monkeypatch):
    """before_run failure with on_failure=warn lets the run proceed to completion."""
    from eval import runner as runner_mod
    from eval.config import Hooks, Task

    config = _config(tmp_path)
    run_dir = tmp_path / "results"
    run_dir.mkdir()
    _stub_no_docker(monkeypatch, runner_mod, docker_rc=0)
    # before_run fails (1), after_run absent (0)
    monkeypatch.setattr(
        runner_mod, "_run_hook",
        lambda script, *a, **k: 1 if script == "b.sh" else 0,
    )

    task = Task(name="t", prompt="p", hooks=Hooks(before_run="b.sh", on_failure="warn"))
    result = runner_mod.run_one(
        task, Variant(name="v"), epoch=1, config=config, run_id="r",
        run_dir=run_dir, github_token="tok",
    )

    assert result.status == "completed"
    assert result.passed


def test_run_one_after_run_failure_surfaces_in_scores(tmp_path, monkeypatch):
    """after_run hook failure is surfaced as a failing score (run not passed)."""
    from eval import runner as runner_mod
    from eval.config import Hooks, Task

    config = _config(tmp_path)
    run_dir = tmp_path / "results"
    run_dir.mkdir()
    _stub_no_docker(monkeypatch, runner_mod, docker_rc=0)
    monkeypatch.setattr(
        runner_mod, "_run_hook",
        lambda script, *a, **k: 3 if script == "a.sh" else 0,
    )

    task = Task(name="t", prompt="p", hooks=Hooks(after_run="a.sh"))
    result = runner_mod.run_one(
        task, Variant(name="v"), epoch=1, config=config, run_id="r",
        run_dir=run_dir, github_token="tok",
    )

    assert result.status == "completed"
    assert not result.passed
    hook_scores = [s for s in result.scores if s.type == "hook"]
    assert len(hook_scores) == 1
    assert hook_scores[0].passed is False


def test_run_one_post_processing_exception_preserves_run_status(tmp_path, monkeypatch):
    """An exception after the container ran keeps the container's exit status
    (not setup_failed) and surfaces a failing infra score."""
    from eval import runner as runner_mod
    from eval.config import Task

    config = _config(tmp_path)
    run_dir = tmp_path / "results"
    run_dir.mkdir()
    _stub_no_docker(monkeypatch, runner_mod, docker_rc=0)
    monkeypatch.setattr(runner_mod, "_run_hook", lambda *a, **k: 0)

    def boom(*a, **k):
        raise RuntimeError("evaluator crashed")

    monkeypatch.setattr(runner_mod, "_run_evaluators", boom)

    result = runner_mod.run_one(
        Task(name="t", prompt="p"), Variant(name="v"), epoch=1, config=config,
        run_id="r", run_dir=run_dir, github_token="tok",
    )

    assert result.status == "completed"  # docker exited 0
    assert result.exit_code == 0
    assert not result.passed  # infra failure score
    assert any(s.type == "infra" and not s.passed for s in result.scores)


def test_mask_log_file_redacts_in_place(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("output contains supersecretvalue in the logs\n")
    _mask_log_file(log, ["supersecretvalue"])
    text = log.read_text()
    assert "supersecretvalue" not in text
    assert "***REDACTED***" in text


def test_run_one_masks_log_on_setup_failed(tmp_path, monkeypatch):
    """Early setup_failed return must still redact secrets from the log."""
    from eval import runner as runner_mod
    from eval.config import Task

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    (tmp_path / ".env").write_text('SECRET="supersecretvalue"\n')
    config = _config(tmp_path)
    run_dir = tmp_path / "results"
    run_dir.mkdir()

    def fake_before_run(script, cfg, task, variant, log_file, label):
        log_file.write_text("before_run printed supersecretvalue\n")
        return 0

    monkeypatch.setattr(runner_mod, "_run_hook", fake_before_run)
    monkeypatch.setattr(runner_mod, "_run_health_check", lambda *a, **k: False)

    task = Task(name="t", prompt="p", health_check="hc.sh")
    variant = Variant(name="v")
    result = runner_mod.run_one(
        task, variant, epoch=1, config=config, run_id="r", run_dir=run_dir,
        github_token="ghp_tokenvalue123",
    )

    assert result.status == "setup_failed"
    text = result.log_file.read_text()
    assert "supersecretvalue" not in text
    assert "***REDACTED***" in text
    # No leftover temp env files.
    assert not list(Path(tempfile.gettempdir()).glob("eval-env-*"))


# --- host_copilot_version / run_judge ---

class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _judge_config(tmp_path: Path, **runner_kw) -> Config:
    return Config(
        vars={}, runner=RunnerConfig(**runner_kw), tasks=[], variants=[],
        project_dir=tmp_path, config_dir=tmp_path,
    )


def _reset_version_cache(monkeypatch):
    from eval import runner as runner_mod
    monkeypatch.setattr(runner_mod, "_host_copilot_version_cache", None, raising=False)


def test_host_copilot_version_parses_first_line(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    _reset_version_cache(monkeypatch)
    monkeypatch.setattr(runner_mod.subprocess, "run",
                        lambda *a, **k: _FakeProc(stdout="copilot/1.0.18\nextra banner\n"))
    assert runner_mod.host_copilot_version() == "copilot/1.0.18"


def test_host_copilot_version_caches(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    _reset_version_cache(monkeypatch)
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _FakeProc(stdout="copilot/2.0.0")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    assert runner_mod.host_copilot_version() == "copilot/2.0.0"
    assert runner_mod.host_copilot_version() == "copilot/2.0.0"
    assert calls["n"] == 1


def test_host_copilot_version_none_on_failure(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    _reset_version_cache(monkeypatch)

    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(runner_mod.subprocess, "run", boom)
    assert runner_mod.host_copilot_version() is None


def _patch_judge(monkeypatch, proc=None, exc=None, version="copilot/1.0.18"):
    from eval import runner as runner_mod
    monkeypatch.setattr(runner_mod, "host_copilot_version", lambda: version)

    def fake_run(*a, **k):
        if exc is not None:
            raise exc
        return proc

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)


def _ev():
    from eval.config import Evaluator
    return Evaluator(name="quality", type="judge", prompt="Rate it.")


def test_run_judge_ok_records_meta(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    _patch_judge(monkeypatch, proc=_FakeProc(stdout='{"score": 8, "reason": "good"}'))
    s = runner_mod.run_judge(_ev(), "conversation", _judge_config(tmp_path), token=None)
    assert s.score == 8
    assert s.reason == "good"
    assert s.meta["outcome"] == "ok"
    assert s.meta["judge_version"] == "copilot/1.0.18"
    assert s.meta["returncode"] == 0


def test_run_judge_parse_error(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    _patch_judge(monkeypatch, proc=_FakeProc(stdout="not json at all", returncode=0))
    s = runner_mod.run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert s.score is None
    assert s.reason == "parse_error"
    assert s.meta["outcome"] == "parse_error"


def test_run_judge_error_returncode_surfaces_stderr(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    _patch_judge(monkeypatch, proc=_FakeProc(stdout="", stderr="boom failed", returncode=3))
    s = runner_mod.run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert s.score is None
    assert s.meta["outcome"] == "error"
    assert s.meta["returncode"] == 3
    assert s.meta["stderr"] == "boom failed"
    assert "rc=3" in s.reason
    assert "boom failed" in s.reason


def test_run_judge_timeout(tmp_path, monkeypatch):
    import subprocess as sp

    from eval import runner as runner_mod
    _patch_judge(monkeypatch, exc=sp.TimeoutExpired(cmd="copilot", timeout=60))
    s = runner_mod.run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert s.score is None
    assert s.meta["outcome"] == "timeout"


def test_run_judge_not_found(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    _patch_judge(monkeypatch, exc=FileNotFoundError())
    s = runner_mod.run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert s.score is None
    assert s.meta["outcome"] == "not_found"


def test_run_judge_version_mismatch(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    _patch_judge(monkeypatch, proc=_FakeProc(stdout='{"score": 5}'), version="copilot/1.0.18")
    config = _judge_config(tmp_path, judge_copilot_version="copilot/9.9.9")
    s = runner_mod.run_judge(_ev(), "c", config, token=None)
    assert s.meta["judge_version_mismatch"] == {"expected": "copilot/9.9.9", "actual": "copilot/1.0.18"}


def test_run_judge_passes_through_extra_meta(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    _patch_judge(monkeypatch, proc=_FakeProc(stdout='{"score": 7}'))
    s = runner_mod.run_judge(_ev(), "c", _judge_config(tmp_path), token=None,
                             extra_meta={"truncation": {"conversation": 8000}})
    assert s.meta["truncation"] == {"conversation": 8000}


def test_evalscore_to_dict_includes_meta():
    s = EvalScore(name="j", type="judge", score=5, reason="ok", meta={"outcome": "ok"})
    assert s.to_dict() == {
        "name": "j", "type": "judge", "score": 5, "reason": "ok",
        "passed": True, "meta": {"outcome": "ok"},
    }


def test_evalscore_to_dict_omits_empty_meta():
    s = EvalScore(name="c", type="contains", score=1, reason="found")
    assert "meta" not in s.to_dict()


def test_run_judge_invalid_score_value(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    _patch_judge(monkeypatch, proc=_FakeProc(stdout='{"score": "N/A", "reason": "x"}'))
    s = runner_mod.run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert s.score is None
    assert s.meta["outcome"] == "invalid_score"


def test_run_judge_nonzero_exit_with_valid_json(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    _patch_judge(monkeypatch, proc=_FakeProc(stdout='{"score": 6}', returncode=1))
    s = runner_mod.run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    # Verdict is kept but the anomaly (non-zero exit) is flagged, not counted ok.
    assert s.score == 6
    assert s.meta["outcome"] == "ok_nonzero"


def test_run_judge_mismatch_when_version_unavailable(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    _patch_judge(monkeypatch, proc=_FakeProc(stdout='{"score": 5}'), version=None)
    config = _judge_config(tmp_path, judge_copilot_version="copilot/1.0.18")
    s = runner_mod.run_judge(_ev(), "c", config, token=None)
    assert s.meta["judge_version_mismatch"] == {"expected": "copilot/1.0.18", "actual": None}


def test_run_judge_masks_secrets_in_stderr(tmp_path, monkeypatch):
    from eval import runner as runner_mod
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    (tmp_path / ".env").write_text('SECRET="supersecretvalue"\n')
    _patch_judge(monkeypatch, proc=_FakeProc(stdout="nope", stderr="boom supersecretvalue", returncode=2))
    s = runner_mod.run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert "supersecretvalue" not in s.meta["stderr"]
    assert "supersecretvalue" not in s.reason
