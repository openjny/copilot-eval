"""Tests for eval/schema.py: the generated JSON Schema for eval-config.yaml.

Covers three things:
- the generator produces a schema that is itself valid JSON Schema
  (Draft 2020-12) and matches the committed `schemas/eval-config.schema.json`
  (catches "forgot to re-run scripts/generate_schema.py after editing
  eval/config.py");
- every real eval-config.yaml in the repo (root + examples/) validates
  against it;
- the schema actually rejects the class of typos that motivated it (e.g.
  `timeout_secods`, `judge_batch: "tru"`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

jsonschema = pytest.importorskip("jsonschema")

from eval.schema import generate_schema  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schemas" / "eval-config.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture(scope="module")
def validator(schema: dict):
    return jsonschema.Draft202012Validator(schema)


def test_schema_file_is_valid_json() -> None:
    json.loads(SCHEMA_PATH.read_text())


def test_schema_is_valid_draft_2020_12(schema: dict) -> None:
    jsonschema.Draft202012Validator.check_schema(schema)


def test_committed_schema_matches_generator(schema: dict) -> None:
    """Fails if eval/config.py changed without re-running
    scripts/generate_schema.py."""
    assert schema == generate_schema(), (
        "schemas/eval-config.schema.json is stale. Regenerate with: "
        "uv run python scripts/generate_schema.py"
    )


EXAMPLE_CONFIGS = sorted(REPO_ROOT.glob("**/eval-config.yaml"))


def test_found_example_configs() -> None:
    # Sanity check the glob actually found the real configs, so a refactor
    # that moves them doesn't silently turn this into a no-op test suite.
    assert len(EXAMPLE_CONFIGS) >= 4


@pytest.mark.parametrize(
    "config_path", EXAMPLE_CONFIGS, ids=[str(p.relative_to(REPO_ROOT)) for p in EXAMPLE_CONFIGS]
)
def test_example_config_validates(validator, config_path: Path) -> None:
    raw = yaml.safe_load(config_path.read_text()) or {}
    errors = list(validator.iter_errors(raw))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def test_schema_rejects_unknown_runner_key(validator) -> None:
    raw = {"runner": {"timeout_secods": 300}}
    errors = list(validator.iter_errors(raw))
    assert any("timeout_secods" in e.message for e in errors)


def test_schema_rejects_wrong_type_for_bool_field(validator) -> None:
    raw = {"runner": {"judge_batch": "tru"}}
    errors = list(validator.iter_errors(raw))
    assert any(list(e.path) == ["runner", "judge_batch"] for e in errors)


def test_schema_rejects_invalid_enum_value(validator) -> None:
    raw = {"runner": {"parallel": "everything"}}
    errors = list(validator.iter_errors(raw))
    assert any(list(e.path) == ["runner", "parallel"] for e in errors)


def test_schema_rejects_evaluator_missing_type_specific_field(validator) -> None:
    raw = {
        "tasks": [
            {
                "name": "t1",
                "prompt": "hi",
                "evaluators": [{"name": "e1", "type": "regex"}],  # missing 'value'
            }
        ]
    }
    assert list(validator.iter_errors(raw))


def test_schema_accepts_minimal_valid_config(validator) -> None:
    raw = {"tasks": [{"name": "t1", "prompt": "hi"}]}
    assert not list(validator.iter_errors(raw))
