"""Docker-based Copilot CLI runner."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from eval.config import Config, Variant
from eval.env_utils import write_sanitized_env_file
from eval.naming import run_slug
from eval.protocols import RunArtifacts, RunContext, status_from_exit_code

_TRACE_FILE = Path(".traces") / "traces.jsonl"
_CONTAINER_TRACE_DIR = "/workspace/.traces"
_CONTAINER_RUN_SCRIPT = "/workspace/eval-setup.sh"


class DockerCLIRunner:
    """Docker-based Copilot CLI runner."""

    def __init__(
        self,
        github_token: str,
        run_command: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    ) -> None:
        self.github_token = github_token
        self._run_command = run_command

    @property
    def supported_collectors(self) -> tuple[str, ...]:
        return ("file", "jaeger")

    def build(self, variant: Variant, config: Config) -> None:
        """Build Docker image for a variant."""
        del variant, config

    def health_check(self) -> None:
        """Check Docker daemon is available."""
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError("Docker daemon is not available")

    def run(self, run_context: RunContext) -> RunArtifacts:
        """Execute Copilot in a Docker container."""
        if run_context.work_dir is None:
            raise ValueError("run_context.work_dir is required for DockerCLIRunner.run()")

        task = run_context.task
        variant = run_context.variant
        config = run_context.config
        work_dir = run_context.work_dir

        log_file = run_context.run_dir / (
            run_slug(task.name, variant.name, run_context.epoch, run_context.fixture_label) + ".log"
        )
        log_file.parent.mkdir(parents=True, exist_ok=True)

        output_dir = work_dir / "output"
        trace_file = work_dir / _TRACE_FILE

        started_at = datetime.now().isoformat(timespec="microseconds")
        started_monotonic = time.monotonic()

        prompt = config.resolve_prompt(task, variant)
        image = config.image_name(variant)
        timeout = task.timeout_seconds or config.runner.timeout_seconds

        otel_attrs = ",".join(
            [
                f"eval.test_id={run_context.test_id}",
                f"eval.scenario={task.name}",
                f"eval.variant={variant.name}",
                f"eval.epoch={run_context.epoch}",
                f"eval.fixture={run_context.fixture_label}",
                f"eval.run_id={run_context.run_id}",
            ]
        )

        env_values = {
            "COPILOT_OTEL_ENABLED": "true",
            "COPILOT_OTEL_CAPTURE_CONTENT": ("true" if config.runner.capture_content else "false"),
            "OTEL_RESOURCE_ATTRIBUTES": otel_attrs,
            "OTEL_SERVICE_NAME": "github-copilot",
            **run_context.extra_env,
        }

        env_file_arg = write_sanitized_env_file(config)
        try:
            cmd = [
                "docker",
                "run",
                "--rm",
                "--add-host=host.docker.internal:host-gateway",
                "--env-file",
                str(env_file_arg),
                "-e",
                "GITHUB_TOKEN",
            ]
            for key, value in env_values.items():
                cmd.extend(["-e", f"{key}={value}"])

            copilot_home = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot")).resolve()
            if copilot_home.is_dir():
                cmd.extend(["-v", f"{copilot_home}:/copilot-home-src:ro"])

            cmd.extend(["-v", f"{work_dir}:/workspace"])

            if variant.run_script:
                run_script_path = (config.project_dir / variant.run_script).resolve()
                if run_script_path.exists():
                    cmd.extend(
                        [
                            "-v",
                            f"{run_script_path}:{_CONTAINER_RUN_SCRIPT}:ro",
                            "-e",
                            f"EVAL_SETUP_SCRIPT={_CONTAINER_RUN_SCRIPT}",
                        ]
                    )

            copilot_args = ["copilot", "-p", prompt, "--yolo"]
            model = variant.model or config.runner.model
            if model:
                copilot_args.extend(["--model", model])
            if config.runner.reasoning_effort:
                copilot_args.extend(["--effort", config.runner.reasoning_effort])
            if config.runner.max_turns:
                copilot_args.extend(["--max-autopilot-continues", str(config.runner.max_turns)])
            if config.runner.output_format == "json":
                copilot_args.extend(["--output-format", "json"])

            cmd.extend([image, "timeout", f"{timeout}s", *copilot_args])

            run_env = {**os.environ, "GITHUB_TOKEN": self.github_token}
            with open(log_file, "a", encoding="utf-8") as lf:
                run_command = self._run_command or subprocess.run
                proc = run_command(cmd, stdout=lf, stderr=subprocess.STDOUT, env=run_env)
        finally:
            env_file_arg.unlink(missing_ok=True)

        finished_at = datetime.now().isoformat(timespec="microseconds")
        duration_seconds = round(time.monotonic() - started_monotonic, 3)
        return RunArtifacts(
            exit_code=proc.returncode,
            log_file=log_file,
            trace_file=trace_file,
            output_dir=output_dir,
            duration_seconds=duration_seconds,
            status=status_from_exit_code(proc.returncode),
            started_at=started_at,
            finished_at=finished_at,
        )
