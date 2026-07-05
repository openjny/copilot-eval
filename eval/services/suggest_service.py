"""`suggest-evaluators` business logic: LLM-assisted rubric authoring (issue #93).

Authoring good judge rubrics is the hardest part of setting up a new eval (the
blank-page problem). This service asks the judge model, via a meta-prompt, to
propose an evaluator set for a task and writes it as a ready-to-edit task file:

- structured **judge** rubrics (a scoring ``criterion`` + integer score→anchor
  ``rubric`` map, the same shape ``eval.config`` composes judge prompts from),
- deterministic **regex/contains** anchors that pin objective, LLM-free signals,
- **metric** gates (cost/duration/turn thresholds) where applicable.

The judge model is reached through the shared
:meth:`eval.judge_executor.JudgeExecutor.complete` path (same CLI command,
``--model`` override, and OTel-disabled environment as scoring calls) rather
than a second, divergent invocation. Every proposed evaluator is validated
through :func:`eval.config._parse_evaluators` (dropping any the model got wrong)
and re-serialized from the validated dataclasses, so the emitted task file is
guaranteed to pass ``copilot-eval validate``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from eval.config import (
    _NAME_RE,
    Config,
    ConfigError,
    Evaluator,
    _parse_evaluators,
    _parse_task,
)
from eval.env_utils import collect_secrets, mask_secrets
from eval.exceptions import EvalError, JudgeParseError
from eval.judge_executor import JudgeExecutor, _parse_json

# Deterministic evaluator types: non-judge criteria that anchor the judge with
# objective, reproducible signals (issue #93 requires both to be present).
_DETERMINISTIC_TYPES = ("regex", "contains", "metric")

# Cap the emitted evaluator set so a runaway model response can't produce an
# unwieldy task file. Judge + deterministic fallbacks are added within this cap.
_MAX_EVALUATORS = 10

# Read at most this many characters from each sample-output / fixture file, so a
# large artifact can't blow up the meta-prompt (and the judge's context window).
_MAX_SAMPLE_CHARS = 4000
_MAX_FIXTURE_FILES = 20


@dataclass
class SuggestionResult:
    """Outcome of a ``suggest-evaluators`` run.

    ``evaluators`` are the validated, coverage-guaranteed criteria; ``yaml_text``
    is the task file exactly as written to ``--output``; ``prompt_only`` records
    whether the meta-prompt ran without any sample outputs.
    """

    task_name: str
    evaluators: list[Evaluator]
    yaml_text: str
    prompt_only: bool


# --- meta-prompt construction ---------------------------------------------


def _read_capped(path: Path) -> str:
    text = path.read_text(errors="replace")
    if len(text) > _MAX_SAMPLE_CHARS:
        return text[:_MAX_SAMPLE_CHARS] + "\n... [truncated]"
    return text


def _is_hidden(path: Path, root: Path) -> bool:
    """True when any path segment below ``root`` starts with a dot.

    Dotfiles (``.env``, ``.git/…``, ``.aws/credentials``) commonly hold secrets;
    they're skipped from the fixture summary so they never enter the meta-prompt.
    """
    return any(part.startswith(".") for part in path.relative_to(root).parts)


def summarize_fixture(fixture_dir: Path) -> str:
    """Summarize a fixture directory as a file listing plus small text snippets.

    Only the first :data:`_MAX_FIXTURE_FILES` non-hidden files are listed, and
    each text file contributes a capped snippet, so the meta-prompt stays
    bounded. Hidden files/directories (dotfiles) are skipped to avoid leaking
    credentials into the prompt.
    """
    if not fixture_dir.is_dir():
        return ""
    files = sorted(
        p for p in fixture_dir.rglob("*") if p.is_file() and not _is_hidden(p, fixture_dir)
    )
    if not files:
        return ""
    lines: list[str] = []
    for p in files[:_MAX_FIXTURE_FILES]:
        rel = p.relative_to(fixture_dir).as_posix()
        try:
            snippet = _read_capped(p).strip()
        except (OSError, UnicodeError):
            snippet = "[unreadable]"
        lines.append(f"### {rel}\n{snippet}" if snippet else f"### {rel}\n[empty]")
    if len(files) > _MAX_FIXTURE_FILES:
        lines.append(f"... (+{len(files) - _MAX_FIXTURE_FILES} more files)")
    return "\n\n".join(lines)


def build_meta_prompt(
    task_prompt: str,
    fixture_summary: str = "",
    sample_outputs: list[str] | None = None,
) -> str:
    """Compose the meta-prompt that asks the judge model to propose evaluators.

    Works in prompt-only mode: ``fixture_summary``/``sample_outputs`` are simply
    omitted from the prompt when empty. The strict-JSON output contract asks for
    an ``evaluators`` array whose entries match the ``eval.config`` evaluator
    shapes so the response maps directly onto the config schema.
    """
    sections: list[str] = [
        "You are an expert at designing evaluations for AI coding agents. Given a "
        "TASK PROMPT (and optionally sample outputs), propose a concise, high-signal "
        "set of evaluators that measure how well an agent completed the task.\n"
        "\n"
        "Return BOTH kinds of evaluator:\n"
        "1. JUDGE rubrics: an LLM scores the output. Use a scoring `criterion` and a "
        "`rubric` object mapping SINGLE INTEGER scores (e.g. 1, 3, 5, 7, 10) to anchor "
        'descriptions. Do NOT use ranges like "1-3" as keys. Anchor the extremes and '
        "a midpoint so scores are calibrated and reproducible.\n"
        "2. DETERMINISTIC anchors: objective, LLM-free checks.\n"
        "   - `regex`: a Python regular expression the output should match (`value`).\n"
        "   - `contains`: a literal substring the output should contain (`value`).\n"
        "   - `metric`: a resource gate on a run metric (`metric`, `op`, numeric "
        "`value`). Valid metrics include cost, duration, turn_count, tool_count, "
        "total_tokens. Ops: <, <=, >, >=, ==, !=.\n"
        "\n"
        "Include at least one JUDGE rubric AND at least one DETERMINISTIC anchor. "
        "Prefer 3-6 evaluators total. Names must be short kebab-case slugs "
        "(letters, digits, '-', '.', '_'), unique, and starting with a letter/digit.",
    ]
    sections.append(f"--- TASK PROMPT ---\n{task_prompt.strip()}\n--- END TASK PROMPT ---")
    if fixture_summary.strip():
        sections.append(
            f"--- FIXTURE (task input) ---\n{fixture_summary.strip()}\n--- END FIXTURE ---"
        )
    for i, sample in enumerate(sample_outputs or [], start=1):
        if sample.strip():
            sections.append(
                f"--- SAMPLE OUTPUT {i} ---\n{sample.strip()}\n--- END SAMPLE OUTPUT {i} ---"
            )
    sections.append(
        "Output ONLY valid JSON of this exact shape (no prose, no code fence):\n"
        '{"evaluators": [\n'
        '  {"name": "slug", "type": "judge", "criterion": "Rate ...", '
        '"rubric": {"1": "worst-case anchor", "5": "partial anchor", '
        '"10": "best-case anchor"}},\n'
        '  {"name": "slug", "type": "regex", "value": "pattern"},\n'
        '  {"name": "slug", "type": "contains", "value": "substring"},\n'
        '  {"name": "slug", "type": "metric", "metric": "cost", "op": "<=", "value": 0.10}\n'
        "]}"
    )
    return "\n\n".join(sections)


# --- response parsing + normalization -------------------------------------


def _coerce_score_key(key: object) -> int | None:
    """Coerce a rubric key to an integer anchor.

    Accepts ints and numeric strings; for a range-like key the model may emit
    despite instructions (e.g. ``"7-9"``), falls back to the first integer found
    (the band's lower bound), so the anchor stays meaningful and validates.
    """
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return key
    if isinstance(key, str):
        try:
            return int(key.strip())
        except ValueError:
            m = re.search(r"-?\d+", key)
            return int(m.group()) if m else None
    return None


def _normalize_raw_evaluator(raw: Any) -> dict[str, Any] | None:
    """Normalize one model-proposed evaluator into a config-shaped dict.

    Returns ``None`` for structurally unusable entries. Rubric keys are coerced
    to integers; unknown/extra keys are dropped. The result is *not* yet
    validated — that happens in :func:`_validate_evaluators`.
    """
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    etype = str(raw.get("type", "judge")).strip() or "judge"
    out: dict[str, Any] = {"name": name.strip(), "type": etype}

    if etype == "judge":
        rubric = raw.get("rubric")
        if isinstance(rubric, dict) and rubric:
            norm: dict[int, str] = {}
            for k, v in rubric.items():
                score = _coerce_score_key(k)
                if score is not None and isinstance(v, str) and v.strip():
                    norm.setdefault(score, v.strip())
            if norm:
                out["criterion"] = str(raw.get("criterion", "")).strip() or "Rate the output."
                out["rubric"] = norm
                return out
        prompt = raw.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            out["prompt"] = prompt.strip()
            return out
        return None

    if etype in ("regex", "contains"):
        value = raw.get("value")
        if value is None or not str(value).strip():
            return None
        out["value"] = str(value)
        return out

    if etype == "metric":
        for field in ("metric", "op"):
            if not raw.get(field):
                return None
            out[field] = str(raw[field])
        value = raw.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        out["value"] = value
        return out

    # Unknown/unsupported type (e.g. script/python, which reference on-disk
    # files a suggestion can't produce) — skip it.
    return None


def _loose_json_array(text: str) -> list[Any] | None:
    """Recover a top-level JSON array from raw or code-fenced text.

    Some models return the evaluators as a bare ``[ ... ]`` array (no wrapping
    object), optionally inside a ```` ```json ... ``` ```` fence. Try the fenced
    content first, then the raw text, then a first-``[`` .. last-``]`` slice.
    """
    stripped = text.strip()
    candidates: list[str] = []
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(stripped)
    start, end = stripped.find("["), stripped.rfind("]")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            return data
    return None


def parse_suggestions(text: str) -> list[dict[str, Any]]:
    """Extract the ``evaluators`` array from a (possibly noisy) judge response.

    Accepts a JSON object with an ``evaluators`` list, or a bare top-level array
    (raw or code-fenced). Raises :class:`JudgeParseError` when no evaluator list
    can be recovered at all.
    """
    data = _parse_json(text, require_keys=("evaluators",))
    if data is None:
        loose = _loose_json_array(text)
        if loose is not None:
            data = {"evaluators": loose}
        else:
            raise JudgeParseError(
                "judge response did not contain a JSON object with an 'evaluators' list"
            )
    raw_list = data.get("evaluators")
    if not isinstance(raw_list, list):
        raise JudgeParseError("judge response 'evaluators' field is not a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_list:
        ev = _normalize_raw_evaluator(raw)
        if ev is None:
            continue
        if ev["name"] in seen:
            continue
        seen.add(ev["name"])
        normalized.append(ev)
    return normalized


def _validate_evaluators(raw: list[dict[str, Any]]) -> list[Evaluator]:
    """Validate each proposed evaluator independently, dropping invalid ones.

    Uses the same parser the config loader uses, one evaluator at a time so a
    single malformed suggestion doesn't discard the whole set. The survivors are
    guaranteed to round-trip through the config schema.
    """
    valid: list[Evaluator] = []
    for ev in raw:
        try:
            parsed = _parse_evaluators([ev], context="suggested evaluators")
        except ConfigError:
            continue
        valid.extend(parsed)
    return valid


# --- coverage guarantees --------------------------------------------------

_DEFAULT_JUDGE = Evaluator(
    name="overall-quality",
    type="judge",
    criterion="Rate how completely and correctly the output accomplishes the task.",
    rubric={
        1: "Does not address the task or is incorrect.",
        5: "Partially addresses the task; notable gaps or errors.",
        8: "Addresses the task correctly with minor gaps.",
        10: "Fully and correctly accomplishes the task, with clear supporting detail.",
    },
)

_DEFAULT_METRIC_GATE = Evaluator(
    name="cost-gate",
    type="metric",
    metric="cost",
    op="<=",
    threshold=0.50,
)


def _unique_name(base: str, taken: set[str]) -> str:
    """Return ``base`` or ``base-2``, ``base-3``, … so it doesn't collide."""
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def ensure_coverage(evaluators: list[Evaluator]) -> list[Evaluator]:
    """Guarantee both a judge rubric and a deterministic anchor are present.

    Issue #93 requires the output to include BOTH. When the model omits one, a
    sensible, valid default is added (an overall-quality judge rubric and/or a
    cost gate) so the emitted task file is immediately runnable and editable.
    Default names are de-duplicated against the model-proposed evaluators so a
    name clash can never produce a task file that fails ``validate``.
    """
    result = list(evaluators[:_MAX_EVALUATORS])
    names = {e.name for e in result}
    if not any(e.type == "judge" for e in result):
        judge = replace(_DEFAULT_JUDGE, name=_unique_name(_DEFAULT_JUDGE.name, names))
        result.insert(0, judge)
        names.add(judge.name)
    if not any(e.type in _DETERMINISTIC_TYPES for e in result):
        gate = replace(_DEFAULT_METRIC_GATE, name=_unique_name(_DEFAULT_METRIC_GATE.name, names))
        result.append(gate)
        names.add(gate.name)
    return result


# --- serialization --------------------------------------------------------


class _BlockDumper(yaml.SafeDumper):
    """SafeDumper that renders multi-line strings as literal blocks (``|``).

    Keeps the emitted task file readable and consistent with the hand-written
    task files under ``examples/`` (whose prompts use ``prompt: |`` blocks).
    """


def _represent_str(dumper: _BlockDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_BlockDumper.add_representer(str, _represent_str)


def _evaluator_to_dict(ev: Evaluator) -> dict[str, Any]:
    """Serialize a validated Evaluator back to a minimal config-shaped dict.

    Emits the structured judge form (``criterion``/``rubric``) when present so
    the anchors stay human-editable, and only the fields each type needs.
    """
    out: dict[str, Any] = {"name": ev.name, "type": ev.type}
    if ev.type == "judge":
        if ev.rubric is not None:
            out["criterion"] = ev.criterion
            out["rubric"] = {int(k): v for k, v in sorted(ev.rubric.items())}
        else:
            out["prompt"] = ev.prompt
    elif ev.type in ("regex", "contains"):
        out["value"] = ev.value
    elif ev.type == "metric":
        out["metric"] = ev.metric
        out["op"] = ev.op
        out["value"] = ev.threshold
    return out


def slugify_task_name(raw: str) -> str:
    """Coerce an arbitrary string into a valid task/evaluator name.

    Task names must match ``eval.config._NAME_RE``
    (``^[A-Za-z0-9][A-Za-z0-9._-]*$``). A name derived from a filename or
    ``--task-name`` (e.g. ``"Security Review"``) would otherwise make the
    generated task file fail ``validate``, so it is slugified: invalid runs
    become ``-``, leading junk is stripped, and an empty result falls back to a
    safe default.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    slug = slug.lstrip("._-")
    if not slug or not _NAME_RE.match(slug):
        return "suggested-task"
    return slug


def build_task_yaml(
    task_name: str,
    task_prompt: str,
    evaluators: list[Evaluator],
    fixture: str | None = None,
) -> str:
    """Render a standalone ``tasks/<name>.yaml`` task file.

    The file bundles the task prompt with the suggested evaluators so it can be
    dropped straight into a project's ``tasks/`` directory and validated/run.
    Multi-line strings (notably the prompt) are emitted as literal blocks (``|``)
    to match the hand-written task files under ``examples/``. When ``fixture`` is
    given, a ``fixture:`` field records the input the rubric was designed against
    (otherwise a run would default to ``fixtures/<task-name>/``).
    """
    doc: dict[str, Any] = {
        "name": task_name,
        "prompt": task_prompt.rstrip() + "\n",
    }
    if fixture:
        doc["fixture"] = fixture
    doc["evaluators"] = [_evaluator_to_dict(e) for e in evaluators]
    body = yaml.dump(
        doc,
        Dumper=_BlockDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    header = (
        "# Suggested by `copilot-eval suggest-evaluators` (issue #93).\n"
        "# Review the rubric anchors and deterministic checks before running —\n"
        "# these are a starting point, not a final rubric.\n"
    )
    return header + body


# --- orchestration --------------------------------------------------------


def suggest_evaluators(
    *,
    task_prompt: str,
    task_name: str,
    output_path: Path,
    config: Config,
    token: str | None,
    fixture_dir: Path | None = None,
    sample_output_paths: list[Path] | None = None,
    executor: JudgeExecutor | None = None,
) -> SuggestionResult:
    """Generate and write a suggested evaluator task file.

    Builds the meta-prompt, invokes the judge model through
    :meth:`JudgeExecutor.complete`, validates the proposed evaluators, guarantees
    judge + deterministic coverage, and writes the resulting task file to
    ``output_path``. ``executor`` is injectable for testing.

    The task name is slugified and the fully assembled task is re-parsed through
    the config loader before writing, so the emitted file is guaranteed to pass
    ``validate`` (or a clear :class:`EvalError` is raised instead of writing an
    invalid file). Fixture/sample text fed into the meta-prompt is secret-masked.
    """
    secrets = collect_secrets(config, token)
    fixture_summary = ""
    if fixture_dir is not None:
        fixture_summary = mask_secrets(summarize_fixture(fixture_dir), secrets) or ""
    try:
        sample_texts = [
            mask_secrets(_read_capped(p), secrets) or "" for p in (sample_output_paths or [])
        ]
    except OSError as exc:
        raise EvalError(f"could not read sample output: {exc}") from exc
    prompt_only = not fixture_summary.strip() and not any(t.strip() for t in sample_texts)

    meta_prompt = build_meta_prompt(task_prompt, fixture_summary, sample_texts)
    executor = executor or JudgeExecutor(config)
    response = executor.complete(meta_prompt, token)

    raw = parse_suggestions(response)
    validated = _validate_evaluators(raw)
    evaluators = ensure_coverage(validated)

    name = slugify_task_name(task_name)
    fixture_ref = None
    if fixture_dir is not None and _NAME_RE.match(fixture_dir.name):
        fixture_ref = fixture_dir.name
    yaml_text = build_task_yaml(name, task_prompt, evaluators, fixture=fixture_ref)

    # Final guarantee: the assembled task must load cleanly (name, evaluators,
    # and cross-evaluator invariants like unique names) — anything else is a bug
    # in this service, surfaced before an invalid file is written.
    try:
        _parse_task(yaml.safe_load(yaml_text))
    except ConfigError as exc:  # pragma: no cover - defensive; fixes above prevent this
        raise EvalError(f"internal error: generated task would not validate: {exc}") from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml_text)

    return SuggestionResult(
        task_name=name,
        evaluators=evaluators,
        yaml_text=yaml_text,
        prompt_only=prompt_only,
    )
