"""Tests for eval.judge_executor: JudgeExecutor, prompt/response handling,
runtime metadata, and self-consistency sampling — single-judge (execute_single
/ run_judge) and batched (execute_batch / run_judges_batch) paths.

Both paths are thin wrappers around :class:`eval.judge_executor.JudgeExecutor`
(see issue #80), so tests exercise them through the public ``eval.runner``
entry points (``run_judge`` / ``run_judges_batch``) while patching the seams
that actually live in ``eval.judge_executor`` (subprocess, host version cache,
and single-sample invocation).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.config import Config, Evaluator, RunnerConfig
from eval.judge_executor import (
    JudgeContext,
    JudgeExecutor,
    _aggregate_scores,
)
from eval.runner import run_judge, run_judges_batch

# --- judge aggregation / self-consistency ---


@pytest.mark.parametrize(
    "samples,method,expected",
    [
        ([8], "median", 8),
        ([4, 8, 9], "median", 8),
        ([4, 8, 9], "mean", 7),
        ([5, 5, 9], "majority", 5),
        ([5, 9], "majority", 5),
        ([6, 7], "median", 7),
        ([6, 7], "mean", 7),
        ([7, 8], "mean", 8),
    ],
)
def test_aggregate_scores(samples, method, expected):
    assert _aggregate_scores(samples, method) == expected


# --- host_copilot_version / run_judge runtime metadata ---


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _judge_config(tmp_path: Path, **runner_kw) -> Config:
    return Config(
        vars={},
        runner=RunnerConfig(**runner_kw),
        tasks=[],
        variants=[],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


def _reset_version_cache(monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.setattr(je_mod, "_host_copilot_version_cache", None, raising=False)


def test_host_copilot_version_parses_first_line(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    _reset_version_cache(monkeypatch)
    monkeypatch.setattr(
        je_mod.subprocess,
        "run",
        lambda *a, **k: _FakeProc(stdout="copilot/1.0.18\nextra banner\n"),
    )
    assert je_mod.host_copilot_version() == "copilot/1.0.18"


def test_host_copilot_version_caches(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    _reset_version_cache(monkeypatch)
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _FakeProc(stdout="copilot/2.0.0")

    monkeypatch.setattr(je_mod.subprocess, "run", fake_run)
    assert je_mod.host_copilot_version() == "copilot/2.0.0"
    assert je_mod.host_copilot_version() == "copilot/2.0.0"
    assert calls["n"] == 1


def test_host_copilot_version_none_on_failure(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    _reset_version_cache(monkeypatch)

    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(je_mod.subprocess, "run", boom)
    assert je_mod.host_copilot_version() is None


def _patch_judge(monkeypatch, proc=None, exc=None, version="copilot/1.0.18"):
    from eval import judge_executor as je_mod

    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: version)

    def fake_run(*a, **k):
        if exc is not None:
            raise exc
        return proc

    monkeypatch.setattr(je_mod.subprocess, "run", fake_run)


def _ev():
    return Evaluator(name="quality", type="judge", prompt="Rate it.")


def test_run_judge_ok_records_meta(tmp_path, monkeypatch):
    _patch_judge(monkeypatch, proc=_FakeProc(stdout='{"score": 8, "reason": "good"}'))
    s = run_judge(_ev(), "conversation", _judge_config(tmp_path), token=None)
    assert s.score == 8
    assert s.reason == "good"
    assert s.meta["outcome"] == "ok"
    assert s.meta["judge_version"] == "copilot/1.0.18"
    assert s.meta["returncode"] == 0


def test_run_judge_parse_error(tmp_path, monkeypatch):
    _patch_judge(monkeypatch, proc=_FakeProc(stdout="not json at all", returncode=0))
    s = run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert s.score is None
    assert s.reason == "parse_error"
    assert s.meta["outcome"] == "parse_error"


def test_run_judge_error_returncode_surfaces_stderr(tmp_path, monkeypatch):
    _patch_judge(monkeypatch, proc=_FakeProc(stdout="", stderr="boom failed", returncode=3))
    s = run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert s.score is None
    assert s.meta["outcome"] == "error"
    assert s.meta["returncode"] == 3
    assert s.meta["stderr"] == "boom failed"
    assert "rc=3" in s.reason
    assert "boom failed" in s.reason


def test_run_judge_timeout(tmp_path, monkeypatch):
    import subprocess as sp

    _patch_judge(monkeypatch, exc=sp.TimeoutExpired(cmd="copilot", timeout=60))
    s = run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert s.score is None
    assert s.meta["outcome"] == "timeout"


def test_run_judge_not_found(tmp_path, monkeypatch):
    _patch_judge(monkeypatch, exc=FileNotFoundError())
    s = run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert s.score is None
    assert s.meta["outcome"] == "not_found"


def test_run_judge_version_mismatch(tmp_path, monkeypatch):
    _patch_judge(monkeypatch, proc=_FakeProc(stdout='{"score": 5}'), version="copilot/1.0.18")
    config = _judge_config(tmp_path, judge_copilot_version="copilot/9.9.9")
    s = run_judge(_ev(), "c", config, token=None)
    assert s.meta["judge_version_mismatch"] == {
        "expected": "copilot/9.9.9",
        "actual": "copilot/1.0.18",
    }


def test_run_judge_passes_through_extra_meta(tmp_path, monkeypatch):
    _patch_judge(monkeypatch, proc=_FakeProc(stdout='{"score": 7}'))
    s = run_judge(
        _ev(),
        "c",
        _judge_config(tmp_path),
        token=None,
        extra_meta={"truncation": {"conversation": 8000}},
    )
    assert s.meta["truncation"] == {"conversation": 8000}


def test_run_judge_invalid_score_value(tmp_path, monkeypatch):
    _patch_judge(monkeypatch, proc=_FakeProc(stdout='{"score": "N/A", "reason": "x"}'))
    s = run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert s.score is None
    assert s.meta["outcome"] == "invalid_score"


def test_run_judge_nonzero_exit_with_valid_json(tmp_path, monkeypatch):
    _patch_judge(monkeypatch, proc=_FakeProc(stdout='{"score": 6}', returncode=1))
    s = run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert s.score == 6
    assert s.meta["outcome"] == "ok_nonzero"


def test_run_judge_mismatch_when_version_unavailable(tmp_path, monkeypatch):
    _patch_judge(monkeypatch, proc=_FakeProc(stdout='{"score": 5}'), version=None)
    config = _judge_config(tmp_path, judge_copilot_version="copilot/1.0.18")
    s = run_judge(_ev(), "c", config, token=None)
    assert s.meta["judge_version_mismatch"] == {"expected": "copilot/1.0.18", "actual": None}


def test_run_judge_masks_secrets_in_stderr(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    (tmp_path / ".env").write_text('SECRET="supersecretvalue"\n')
    _patch_judge(
        monkeypatch, proc=_FakeProc(stdout="nope", stderr="boom supersecretvalue", returncode=2)
    )
    s = run_judge(_ev(), "c", _judge_config(tmp_path), token=None)
    assert "supersecretvalue" not in s.meta["stderr"]
    assert "supersecretvalue" not in s.reason


# --- judge self-consistency (repeated sampling) ---


def test_run_judge_single_sample_legacy(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "1.2.3")
    monkeypatch.setattr(
        je_mod.JudgeExecutor,
        "_invoke_single",
        lambda self, *a, **k: (7, "solid", "ok", {"returncode": 0}),
    )
    s = run_judge(_ev(), "conversation", _judge_config(tmp_path, judge_model="test-model"), "tok")
    assert s.score == 7
    assert s.reason == "solid"
    assert s.samples == [7]
    assert s.n_samples == 1
    assert s.score_stddev == 0.0
    assert s.outcomes == {"ok": 1}
    assert s.judge_model == "test-model"
    assert s.judge_version == "1.2.3"
    assert s.passed is True


def test_run_judge_repeated_sampling_median(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "v")
    results = iter([(4, "weak", "ok", {}), (8, "good", "ok", {}), (9, "great", "ok", {})])
    monkeypatch.setattr(je_mod.JudgeExecutor, "_invoke_single", lambda self, *a, **k: next(results))
    s = run_judge(
        _ev(), "conv", _judge_config(tmp_path, judge_model="test-model", judge_samples=3), "tok"
    )
    assert s.score == 8
    assert sorted(s.samples) == [4, 8, 9]
    assert s.n_samples == 3
    assert s.score_stddev and s.score_stddev > 0
    assert "median of 3/3" in s.reason
    assert s.outcomes["ok"] == 3


def test_run_judge_partial_failures(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "v")
    results = iter(
        [(6, "ok-ish", "ok", {}), (None, "timeout", "timeout", {}), (8, "good", "ok", {})]
    )
    monkeypatch.setattr(je_mod.JudgeExecutor, "_invoke_single", lambda self, *a, **k: next(results))
    s = run_judge(
        _ev(), "conv", _judge_config(tmp_path, judge_model="test-model", judge_samples=3), "tok"
    )
    assert s.score == 7
    assert s.outcomes == {"ok": 2, "timeout": 1}
    assert "median of 2/3" in s.reason


def test_run_judge_all_failures_returns_dominant_outcome(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "v")
    results = iter(
        [
            (None, "parse_error", "parse_error", {}),
            (None, "timeout", "timeout", {}),
            (None, "parse_error", "parse_error", {}),
        ]
    )
    monkeypatch.setattr(je_mod.JudgeExecutor, "_invoke_single", lambda self, *a, **k: next(results))
    s = run_judge(
        _ev(), "conv", _judge_config(tmp_path, judge_model="test-model", judge_samples=3), "tok"
    )
    assert s.score is None
    assert s.passed is False
    assert s.reason == "parse_error"
    assert s.score_stddev is None


# --- run_judges_batch (opt-in batched judging) ---


def _evs():
    return [
        Evaluator(name="thoroughness", type="judge", prompt="Rate thoroughness."),
        Evaluator(name="actionability", type="judge", prompt="Rate actionability."),
    ]


def test_run_judges_batch_splits_single_call(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "v")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _FakeProc(
            stdout=json.dumps(
                {
                    "thoroughness": {"score": 8, "reason": "covers cases"},
                    "actionability": {"score": 6, "reason": "some vague steps"},
                }
            )
        )

    monkeypatch.setattr(je_mod.subprocess, "run", fake_run)
    scores = run_judges_batch(_evs(), "conv", _judge_config(tmp_path, judge_model="m"), "tok")
    # One LLM call scored both judges.
    assert calls["n"] == 1
    by_name = {s.name: s for s in scores}
    assert by_name["thoroughness"].score == 8
    assert by_name["thoroughness"].reason == "covers cases"
    assert by_name["actionability"].score == 6
    assert all(s.meta["outcome"] == "ok" for s in scores)
    assert all(s.n_samples == 1 for s in scores)


def test_run_judges_batch_call_count_is_judge_samples(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "v")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _FakeProc(
            stdout=json.dumps(
                {
                    "thoroughness": {"score": 7, "reason": "ok"},
                    "actionability": {"score": 7, "reason": "ok"},
                }
            )
        )

    monkeypatch.setattr(je_mod.subprocess, "run", fake_run)
    scores = run_judges_batch(_evs(), "conv", _judge_config(tmp_path, judge_samples=3), "tok")
    # 2 judges × 3 samples would be 6 calls independently; batched is just 3.
    assert calls["n"] == 3
    assert all(s.n_samples == 3 for s in scores)
    assert all(len(s.samples) == 3 for s in scores)


def test_run_judges_batch_parse_error_fails_all(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "v")
    monkeypatch.setattr(
        je_mod.subprocess, "run", lambda *a, **k: _FakeProc(stdout="not json", returncode=0)
    )
    scores = run_judges_batch(_evs(), "conv", _judge_config(tmp_path), "tok")
    # Failure blast radius: one bad response fails every criterion.
    assert all(s.score is None for s in scores)
    assert all(s.passed is False for s in scores)
    assert all(s.reason == "parse_error" for s in scores)


def test_run_judges_batch_missing_key_fails_only_that_judge(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "v")
    monkeypatch.setattr(
        je_mod.subprocess,
        "run",
        lambda *a, **k: _FakeProc(
            stdout=json.dumps({"thoroughness": {"score": 9, "reason": "great"}})
        ),
    )
    scores = run_judges_batch(_evs(), "conv", _judge_config(tmp_path), "tok")
    by_name = {s.name: s for s in scores}
    assert by_name["thoroughness"].score == 9
    assert by_name["actionability"].score is None
    assert by_name["actionability"].meta["outcome"] == "parse_error"


def test_run_judges_batch_single_evaluator_delegates(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "v")
    monkeypatch.setattr(
        je_mod.JudgeExecutor,
        "_invoke_single",
        lambda self, *a, **k: (5, "fine", "ok", {"returncode": 0}),
    )
    scores = run_judges_batch([_ev()], "conv", _judge_config(tmp_path), "tok")
    assert len(scores) == 1
    assert scores[0].score == 5
    assert scores[0].name == "quality"


def test_run_judges_batch_invalid_score_flags_one_judge(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "v")
    monkeypatch.setattr(
        je_mod.subprocess,
        "run",
        lambda *a, **k: _FakeProc(
            stdout=json.dumps(
                {
                    "thoroughness": {"score": "abc", "reason": "bad"},
                    "actionability": {"score": 7, "reason": "ok"},
                }
            )
        ),
    )
    scores = run_judges_batch(_evs(), "conv", _judge_config(tmp_path), "tok")
    by_name = {s.name: s for s in scores}
    assert by_name["thoroughness"].score is None
    assert by_name["thoroughness"].meta["outcome"] == "invalid_score"
    assert by_name["actionability"].score == 7


# --- JudgeExecutor unit tests (direct, no eval.runner indirection) ---


def test_judge_executor_execute_single_no_copilot_cli_needed(tmp_path, monkeypatch):
    """JudgeExecutor.execute_single is fully unit-testable: mock subprocess.run
    and never invoke the real Copilot CLI."""
    from eval import judge_executor as je_mod

    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "copilot/1.0.18")
    monkeypatch.setattr(
        je_mod.subprocess,
        "run",
        lambda *a, **k: _FakeProc(stdout='{"score": 9, "reason": "excellent"}'),
    )
    executor = JudgeExecutor(_judge_config(tmp_path))
    context = JudgeContext(conversation="hello world")
    score = executor.execute_single(_ev(), context)
    assert score.score == 9
    assert score.reason == "excellent"
    assert score.name == "quality"
    assert score.type == "judge"


def test_judge_executor_execute_batch_delegates_for_single_evaluator(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "v")
    monkeypatch.setattr(
        je_mod.subprocess,
        "run",
        lambda *a, **k: _FakeProc(stdout='{"score": 4, "reason": "meh"}'),
    )
    executor = JudgeExecutor(_judge_config(tmp_path))
    context = JudgeContext(conversation="hi")
    scores = executor.execute_batch([_ev()], context)
    assert len(scores) == 1
    assert scores[0].score == 4


def test_judge_executor_uses_custom_copilot_cmd(tmp_path, monkeypatch):
    """The copilot_cmd argument controls the CLI invocation, used for e.g. an
    alternate entrypoint or wrapper script."""
    from eval import judge_executor as je_mod

    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "v")
    seen_cmd = {}

    def fake_run(cmd, **k):
        seen_cmd["cmd"] = cmd
        return _FakeProc(stdout='{"score": 1, "reason": "x"}')

    monkeypatch.setattr(je_mod.subprocess, "run", fake_run)
    executor = JudgeExecutor(_judge_config(tmp_path), copilot_cmd=["custom-copilot"])
    executor.execute_single(_ev(), JudgeContext(conversation="c"))
    assert seen_cmd["cmd"][0] == "custom-copilot"


# --- complete() shared judge-invocation path (issue #93) ---


def test_complete_returns_stdout_on_success(tmp_path, monkeypatch):
    _patch_judge(monkeypatch, proc=_FakeProc(stdout="raw model text", returncode=0))
    executor = JudgeExecutor(_judge_config(tmp_path))
    assert executor.complete("meta prompt", token=None) == "raw model text"


def test_complete_uses_model_override_and_disables_otel(tmp_path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "copilot/1.0.18")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env", {})
        return _FakeProc(stdout="ok", returncode=0)

    monkeypatch.setattr(je_mod.subprocess, "run", fake_run)
    config = _judge_config(tmp_path, judge_model="gpt-4.1")
    JudgeExecutor(config).complete("hi", token="tok")
    assert "--model" in seen["cmd"]
    assert "gpt-4.1" in seen["cmd"]
    assert seen["env"].get("COPILOT_OTEL_ENABLED") == "false"


def test_complete_raises_on_nonzero_returncode(tmp_path, monkeypatch):
    from eval.exceptions import JudgeInvocationError

    _patch_judge(monkeypatch, proc=_FakeProc(stdout="", stderr="boom detail", returncode=2))
    executor = JudgeExecutor(_judge_config(tmp_path))
    with pytest.raises(JudgeInvocationError) as excinfo:
        executor.complete("p", token=None)
    assert "rc=2" in str(excinfo.value)
    assert "boom detail" in str(excinfo.value)


def test_complete_masks_secret_in_stderr(tmp_path, monkeypatch):
    from eval.exceptions import JudgeInvocationError

    secret = "ghp_SECRETTOKENVALUE1234567890"
    _patch_judge(
        monkeypatch,
        proc=_FakeProc(stdout="", stderr=f"auth failed for {secret}", returncode=1),
    )
    executor = JudgeExecutor(_judge_config(tmp_path))
    with pytest.raises(JudgeInvocationError) as excinfo:
        executor.complete("p", token=secret)
    assert secret not in str(excinfo.value)


def test_complete_raises_on_timeout(tmp_path, monkeypatch):
    import subprocess as sp

    from eval.exceptions import JudgeInvocationError

    _patch_judge(monkeypatch, exc=sp.TimeoutExpired(cmd="copilot", timeout=60))
    executor = JudgeExecutor(_judge_config(tmp_path))
    with pytest.raises(JudgeInvocationError) as excinfo:
        executor.complete("p", token=None)
    assert "timed out" in str(excinfo.value)


def test_complete_raises_when_cli_missing(tmp_path, monkeypatch):
    from eval.exceptions import JudgeInvocationError

    _patch_judge(monkeypatch, exc=FileNotFoundError())
    executor = JudgeExecutor(_judge_config(tmp_path))
    with pytest.raises(JudgeInvocationError):
        executor.complete("p", token=None)
