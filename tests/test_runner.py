"""Tests for .env parsing, secret collection, and masking."""
from __future__ import annotations

from pathlib import Path

from eval.config import Config, RunnerConfig
from eval.runner import (
    _load_env_file,
    _mask_log_file,
    _strip_quotes,
    _write_sanitized_env_file,
    collect_secrets,
    mask_secrets,
)


def _config(tmp_path: Path) -> Config:
    return Config(
        vars={}, runner=RunnerConfig(), tasks=[], variants=[],
        project_dir=tmp_path, config_dir=tmp_path,
    )


def test_strip_quotes_removes_matching_pairs():
    assert _strip_quotes('"value"') == "value"
    assert _strip_quotes("'value'") == "value"


def test_strip_quotes_leaves_unmatched_or_bare():
    assert _strip_quotes("value") == "value"
    assert _strip_quotes('"value') == '"value'
    assert _strip_quotes("'value\"") == "'value\""
    assert _strip_quotes('"') == '"'


def test_load_env_file_strips_quotes(tmp_path):
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


def test_mask_log_file_redacts_in_place(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("output contains supersecretvalue in the logs\n")
    _mask_log_file(log, ["supersecretvalue"])
    text = log.read_text()
    assert "supersecretvalue" not in text
    assert "***REDACTED***" in text
