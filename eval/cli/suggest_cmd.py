"""`suggest-evaluators` command: routing only — logic in eval.services.suggest_service.

Asks the judge model to propose an evaluator set (judge rubrics + deterministic
anchors + metric gates) for a task prompt and writes it as a ready-to-edit task
file (issue #93). Works in prompt-only mode and the generated YAML is guaranteed
to pass `validate`.
"""

from __future__ import annotations

from pathlib import Path

import click

from eval.config import Config, RunnerConfig, load_config
from eval.exceptions import EvalError
from eval.judge_executor import JudgeExecutor
from eval.services.suggest_service import (
    _read_capped,
    build_meta_prompt,
    suggest_evaluators,
    summarize_fixture,
)


def _load_or_default_config(config_dir: str | None) -> Config:
    """Load the project config when given, else a defaults-only config.

    ``suggest-evaluators`` only needs the runner's judge settings (model,
    timeout), so it works without an eval-config.yaml on disk — handy for
    authoring a brand-new eval before any config exists.
    """
    if config_dir:
        return load_config(Path(config_dir))
    project_dir = Path(__file__).resolve().parent.parent.parent
    return Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[],
        variants=[],
        project_dir=project_dir,
        config_dir=project_dir,
    )


def _resolve_token() -> str | None:
    """Best-effort GitHub token for the judge call; None when unavailable.

    A missing token isn't fatal here — the Copilot CLI may still use host auth —
    so we fall back to None rather than aborting, and let the invocation surface
    an actionable error if it genuinely can't authenticate.
    """
    from eval.exceptions import AuthError
    from eval.runner import get_github_token

    try:
        return get_github_token()
    except AuthError:
        return None


@click.command(name="suggest-evaluators")
@click.option("--task-prompt", default=None, help="The task prompt to design evaluators for.")
@click.option(
    "--task-prompt-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Read the task prompt from a file (alternative to --task-prompt).",
)
@click.option(
    "--output",
    "-o",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to write the suggested task YAML (e.g. tasks/security-review.yaml).",
)
@click.option(
    "--task-name",
    default=None,
    help="Name for the generated task (default: derived from the output filename).",
)
@click.option(
    "--fixture",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Fixture directory to summarize as task input context.",
)
@click.option(
    "--sample-output",
    "sample_outputs",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Sample output file(s) to inform the rubric (repeatable). Omit for prompt-only mode.",
)
@click.option(
    "--judge-model",
    default=None,
    help="Override the judge model used to propose evaluators (default: from config).",
)
@click.option("--config-dir", default=None, type=click.Path(exists=True), help="Project directory")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the meta-prompt that would be sent to the judge and exit (no model call).",
)
def suggest_evaluators_cmd(
    task_prompt: str | None,
    task_prompt_file: Path | None,
    output: Path,
    task_name: str | None,
    fixture: Path | None,
    sample_outputs: tuple[Path, ...],
    judge_model: str | None,
    config_dir: str | None,
    dry_run: bool,
) -> None:
    """Suggest evaluator YAML for a task using the judge model.

    \b
        copilot-eval suggest-evaluators \\
          --task-prompt "Review this PR for security vulnerabilities" \\
          --fixture fixtures/sample-pr/ \\
          --output tasks/security-review.yaml

    The output bundles the task prompt with judge rubrics (structured scoring
    anchors) plus deterministic regex/contains and metric-gate evaluators, and
    is guaranteed to pass `copilot-eval validate`.
    """
    if bool(task_prompt) == bool(task_prompt_file):
        raise click.UsageError("Provide exactly one of --task-prompt or --task-prompt-file.")
    prompt_text = task_prompt if task_prompt else task_prompt_file.read_text()  # type: ignore[union-attr]
    if not prompt_text.strip():
        raise click.UsageError("The task prompt is empty.")

    name = task_name or output.stem
    config = _load_or_default_config(config_dir)
    if judge_model:
        config.runner.judge_model = judge_model

    if dry_run:
        fixture_summary = summarize_fixture(fixture) if fixture else ""
        sample_texts = [_read_capped(p) for p in sample_outputs]
        click.echo(build_meta_prompt(prompt_text, fixture_summary, sample_texts))
        return

    try:
        result = suggest_evaluators(
            task_prompt=prompt_text,
            task_name=name,
            output_path=output,
            config=config,
            token=_resolve_token(),
            fixture_dir=fixture,
            sample_output_paths=list(sample_outputs),
            executor=JudgeExecutor(config),
        )
    except EvalError as exc:
        raise click.ClickException(str(exc)) from exc

    n_judge = sum(1 for e in result.evaluators if e.type == "judge")
    n_det = len(result.evaluators) - n_judge
    mode = "prompt-only" if result.prompt_only else "with context"
    click.echo(f"Wrote {output} ({mode})")
    click.echo(f"  task: {result.task_name}")
    click.echo(f"  evaluators: {len(result.evaluators)} ({n_judge} judge, {n_det} deterministic)")
    for ev in result.evaluators:
        click.echo(f"    - {ev.name} ({ev.type})")
    click.echo("\nNext steps:")
    click.echo("  1. Review the rubric anchors and deterministic checks in the file.")
    click.echo("  2. Move it into your project's tasks/ dir, then: copilot-eval validate")
