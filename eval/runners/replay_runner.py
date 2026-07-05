"""Offline replay / mock Copilot runner (test/dev harness only — issue #132).

``ReplayRunner`` is a first-class :class:`eval.protocols.AgentRunner` that
*replays* pre-recorded agent outputs and OTel traces through the normal
``run_one`` → evaluator → report pipeline **without launching Docker or calling
Copilot**. It exists so developers can iterate on evaluator config (judge
rubrics, regex/contains anchors, metric gates) and so the framework's own
regression tests can cover the runner → evaluator → report path in
environments that have neither Docker nor Copilot auth (e.g. lightweight CI).

Strict framing (non-negotiable — see docs/vision.md non-goals):

* This is a **dev/test harness for the pipeline**, NOT an eval execution mode.
  It must never be presented as, or produce output that can be mistaken for, a
  *measured* environment / A-B conclusion.
* Every run it produces is stamped as replayed/synthetic: the run log opens
  with a loud banner, and (via ``runner.backend: replay``) the manifest and the
  report are marked ``replayed: true`` — see :mod:`eval.services.manifest` and
  :mod:`eval.report`.
* It does **not** relax or bypass environment isolation for real evals. The
  real ``docker`` backend and its isolation model are completely untouched;
  the replay runner is an additional, clearly-separated backend.

Recording layout
----------------
``run_one`` copies a task's fixture into the run's writable ``work_dir`` before
calling ``runner.run``. The replay runner reads the recorded artifacts from a
``.replay/`` subdirectory of that work_dir (i.e. ``fixtures/<name>/.replay/``),
or from an absolute directory named by the ``EVAL_REPLAY_DIR`` environment
variable::

    <replay>/
      transcript.txt   # optional — becomes the run log body (contains/regex read this)
      traces.jsonl     # optional — file-exporter JSONL; resource tags are
                       #            rewritten to THIS run so the file collector,
                       #            metric evaluators and report pick it up
      output/          # optional — copied into work_dir/output/ (judge evidence)
      meta.json        # optional — {"exit_code": 0}

All parts are optional, but the replay *directory* must exist and contain at
least one recognized artifact (``transcript.txt``, ``traces.jsonl``, a non-empty
``output/`` dir, or ``meta.json``); otherwise a
:class:`eval.exceptions.ReplayError` is raised so a misconfigured or empty
recording fails loudly instead of silently emitting an empty "passing" run.
Present-but-malformed artifacts (a non-object ``meta.json``, a wrong-typed
artifact, or a corrupt ``traces.jsonl`` line) also raise ``ReplayError``.

``EVAL_REPLAY_DIR``, when set, points every matrix cell at the *same* single
recording (re-stamped per run); the per-fixture ``.replay/`` path is the
mechanism for distinct recordings across a multi-cell matrix.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from eval.collectors.file_collector import TRACE_FILE
from eval.config import Config, Variant
from eval.exceptions import ReplayError
from eval.naming import run_slug
from eval.protocols import RunArtifacts, RunContext, status_from_exit_code

# Subdirectory of the (fixture-populated) work_dir that holds recorded
# artifacts. Overridable per-invocation with an absolute path via
# EVAL_REPLAY_DIR (e.g. to point at outputs captured from a prior real run).
REPLAY_DIR_NAME = ".replay"
REPLAY_DIR_ENV = "EVAL_REPLAY_DIR"

_TRANSCRIPT_FILE = "transcript.txt"
_TRACES_FILE = "traces.jsonl"
_OUTPUT_DIR = "output"
_META_FILE = "meta.json"

# Loud, unmistakable header written at the top of every replayed run log so a
# stray log file can never be mistaken for a real, isolated Copilot run.
SYNTHETIC_LOG_BANNER = (
    "================================================================\n"
    " REPLAYED / SYNTHETIC RUN — NOT A REAL MEASUREMENT\n"
    " Produced by the offline replay runner (runner.backend: replay).\n"
    " For evaluator/report pipeline testing only. Do NOT treat these\n"
    " results as an isolated A/B measurement.\n"
    "================================================================\n"
)


class ReplayRunner:
    """Replay recorded agent outputs/traces instead of running a container."""

    # Capability marker (see eval.runners.runner_is_synthetic): a synthetic
    # runner is an offline test/dev harness — it needs no Docker and no GitHub
    # token, and everything it produces is stamped replayed/synthetic so it can
    # never be mistaken for a real, isolated measurement.
    is_synthetic = True

    def __init__(self, github_token: str | None = None) -> None:
        # Accept (and ignore) a token so the plugin registry factory can
        # instantiate every backend uniformly (``runner_cls(github_token)``).
        # The replay runner never authenticates or reaches the network.
        del github_token

    @property
    def supported_collectors(self) -> tuple[str, ...]:
        # Offline only: recorded traces are replayed through the file collector.
        # Jaeger would require a live backend, defeating the purpose.
        return ("file",)

    def build(self, variant: Variant, config: Config) -> None:
        """No image to build — replaying never touches Docker."""
        del variant, config

    def health_check(self) -> None:
        """Always healthy: there is no external dependency to check."""

    def run(self, run_context: RunContext) -> RunArtifacts:
        """Emit recorded artifacts as if a container had produced them."""
        if run_context.work_dir is None:
            raise ValueError("run_context.work_dir is required for ReplayRunner.run()")

        task = run_context.task
        variant = run_context.variant
        work_dir = run_context.work_dir

        log_file = run_context.run_dir / (
            run_slug(task.name, variant.name, run_context.epoch, run_context.fixture_label) + ".log"
        )
        log_file.parent.mkdir(parents=True, exist_ok=True)

        started_at = datetime.now().isoformat(timespec="microseconds")
        started_monotonic = time.monotonic()

        replay_dir = self._resolve_replay_dir(work_dir)

        exit_code = self._replay_exit_code(replay_dir)
        self._replay_output(replay_dir, work_dir)
        self._replay_trace(replay_dir, work_dir, run_context)
        self._write_log(replay_dir, log_file, run_context)

        finished_at = datetime.now().isoformat(timespec="microseconds")
        duration_seconds = round(time.monotonic() - started_monotonic, 3)
        return RunArtifacts(
            exit_code=exit_code,
            log_file=log_file,
            trace_file=work_dir / TRACE_FILE,
            output_dir=work_dir / _OUTPUT_DIR,
            duration_seconds=duration_seconds,
            status=status_from_exit_code(exit_code),
            started_at=started_at,
            finished_at=finished_at,
        )

    # --- internals ---------------------------------------------------------

    def _resolve_replay_dir(self, work_dir: Path) -> Path:
        override = os.environ.get(REPLAY_DIR_ENV)
        replay_dir = Path(override).expanduser() if override else work_dir / REPLAY_DIR_NAME
        if not replay_dir.is_dir():
            raise ReplayError(
                f"replay directory '{replay_dir}' is missing. Provide recorded "
                f"artifacts (transcript.txt / traces.jsonl / output/) under a "
                f"'{REPLAY_DIR_NAME}/' subdir of the fixture, or set {REPLAY_DIR_ENV}."
            )
        # Guard against a silent empty/"passing" run: require at least one
        # *recognized* artifact rather than merely a non-empty directory, so a
        # recording that contains only stray files (e.g. .DS_Store, .gitkeep) or
        # an empty output/ dir fails loudly instead of yielding an empty run.
        if not self._has_usable_artifact(replay_dir):
            raise ReplayError(
                f"replay directory '{replay_dir}' has no usable recording. Expected at "
                f"least one of: {_TRANSCRIPT_FILE}, {_TRACES_FILE}, a non-empty "
                f"{_OUTPUT_DIR}/ dir, or {_META_FILE}."
            )
        return replay_dir

    @staticmethod
    def _has_usable_artifact(replay_dir: Path) -> bool:
        if (replay_dir / _TRANSCRIPT_FILE).is_file():
            return True
        if (replay_dir / _TRACES_FILE).is_file():
            return True
        if (replay_dir / _META_FILE).is_file():
            return True
        output_dir = replay_dir / _OUTPUT_DIR
        return output_dir.is_dir() and any(output_dir.iterdir())

    @staticmethod
    def _require_file_if_exists(path: Path) -> bool:
        """Return True if ``path`` is a usable file. Raise ``ReplayError`` if it
        exists but is the wrong type (e.g. a directory named ``traces.jsonl``),
        so a mis-shaped recording fails loudly instead of being silently
        skipped. Return False when the (optional) artifact is simply absent."""
        if not path.exists():
            return False
        if not path.is_file():
            raise ReplayError(
                f"replay artifact '{path}' exists but is not a regular file "
                f"(found a {'directory' if path.is_dir() else 'special file'})."
            )
        return True

    def _replay_exit_code(self, replay_dir: Path) -> int:
        meta_path = replay_dir / _META_FILE
        if not self._require_file_if_exists(meta_path):
            return 0
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ReplayError(f"failed to read replay meta '{meta_path}': {exc}") from exc
        if not isinstance(meta, dict):
            raise ReplayError(
                f"invalid replay meta '{meta_path}': expected a JSON object, got "
                f"{type(meta).__name__}."
            )
        code = meta.get("exit_code", 0)
        try:
            return int(code)
        except (TypeError, ValueError) as exc:
            raise ReplayError(f"invalid exit_code in '{meta_path}': {code!r}") from exc

    def _replay_output(self, replay_dir: Path, work_dir: Path) -> None:
        src = replay_dir / _OUTPUT_DIR
        dest = work_dir / _OUTPUT_DIR
        dest.mkdir(parents=True, exist_ok=True)
        if src.exists() and not src.is_dir():
            raise ReplayError(
                f"replay artifact '{src}' exists but is not a directory; "
                f"'{_OUTPUT_DIR}' must be a directory of output files."
            )
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)

    def _replay_trace(self, replay_dir: Path, work_dir: Path, run_context: RunContext) -> None:
        src = replay_dir / _TRACES_FILE
        if not self._require_file_if_exists(src):
            return
        dest = work_dir / TRACE_FILE
        dest.parent.mkdir(parents=True, exist_ok=True)
        rewritten = _rewrite_trace_resource_tags(
            src.read_text(encoding="utf-8"), run_context, source=src
        )
        dest.write_text(rewritten, encoding="utf-8")

    def _write_log(self, replay_dir: Path, log_file: Path, run_context: RunContext) -> None:
        transcript_path = replay_dir / _TRANSCRIPT_FILE
        body = ""
        if self._require_file_if_exists(transcript_path):
            body = transcript_path.read_text(encoding="utf-8")
        source = os.environ.get(REPLAY_DIR_ENV) or f"{REPLAY_DIR_NAME}/ (fixture-embedded)"
        header = (
            f"{SYNTHETIC_LOG_BANNER}"
            f"[replay] source={source} task={run_context.task.name} "
            f"variant={run_context.variant.name} epoch={run_context.epoch}\n"
        )
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write(header)
            if body:
                if not body.startswith("\n"):
                    lf.write("\n")
                lf.write(body)
                if not body.endswith("\n"):
                    lf.write("\n")


def _rewrite_trace_resource_tags(
    text: str, run_context: RunContext, *, source: Path | None = None
) -> str:
    """Rewrite ``resource.attributes`` on every recorded span so the file
    collector associates the trace with *this* run.

    The file collector filters traces by ``eval.run_id`` and the report keys
    reliability off the per-run ``eval.*`` tags, so a static recording (whose
    tags belong to whatever run captured it) has to be re-stamped with the
    current run's identity to flow through the pipeline.

    A malformed (non-empty, non-JSON) line raises :class:`ReplayError` rather
    than being silently passed through: a corrupt recording that yielded no
    usable telemetry would otherwise complete as a misleadingly "successful"
    empty run. Blank lines and valid non-span JSON records (e.g. resource/scope
    lines) are preserved untouched.
    """
    tags = {
        "eval.test_id": run_context.test_id,
        "eval.scenario": run_context.task.name,
        "eval.variant": run_context.variant.name,
        "eval.epoch": str(run_context.epoch),
        "eval.fixture": run_context.fixture_label,
        "eval.run_id": run_context.run_id,
    }
    where = f" in '{source}'" if source is not None else ""
    out_lines: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ReplayError(
                f"malformed JSON on line {lineno}{where}: {exc}. Each traces.jsonl "
                f"line must be a single JSON span record (Copilot file-exporter format)."
            ) from exc
        if isinstance(record, dict) and record.get("type") == "span":
            resource = record.get("resource")
            if not isinstance(resource, dict):
                resource = {}
            attributes = resource.get("attributes")
            merged: dict[str, Any] = dict(attributes) if isinstance(attributes, dict) else {}
            merged.update(tags)
            resource["attributes"] = merged
            record["resource"] = resource
            out_lines.append(json.dumps(record, ensure_ascii=False))
        else:
            out_lines.append(line)
    return "\n".join(out_lines) + "\n"
