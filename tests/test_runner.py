"""Tests for side-effect-free helpers in runner.py (status mapping, JSON
parsing, env-file parsing, directory reading, and RunResult serialization)."""
from __future__ import annotations

from pathlib import Path

import pytest

from eval.runner import (
    EvalScore,
    RunResult,
    _load_env_file,
    _parse_json,
    read_files_from_dir,
    status_from_exit_code,
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


def test_read_files_from_dir_truncates_at_max_chars(tmp_path: Path):
    d = tmp_path / "out"
    d.mkdir()
    (d / "a.txt").write_text("0123456789")
    (d / "b.txt").write_text("ABCDEFGHIJ")
    # a.txt fills 10 chars (total==10), b.txt would exceed max_chars=15.
    result = read_files_from_dir(d, max_chars=15)
    assert "=== a.txt ===\n0123456789" in result
    assert "... (truncated)" in result
    # Only the remaining 5 chars of b.txt are included.
    assert "ABCDE\n... (truncated)" in result
    assert "ABCDEF" not in result


def test_read_files_from_dir_stops_when_no_remaining_budget(tmp_path: Path):
    d = tmp_path / "out"
    d.mkdir()
    (d / "a.txt").write_text("0123456789")
    (d / "b.txt").write_text("ABCDEFGHIJ")
    # a.txt exactly fills max_chars; b.txt has zero remaining budget so it is
    # dropped entirely (no truncated marker, no partial content).
    result = read_files_from_dir(d, max_chars=10)
    assert result == "=== a.txt ===\n0123456789"


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
