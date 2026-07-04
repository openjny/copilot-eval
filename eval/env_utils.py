"""Environment file helpers shared by runners and orchestration."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eval.config import Config


def strip_quotes(value: str) -> str:
    """Remove a single pair of matching surrounding quotes from a value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_env_file(env_file: Path) -> dict[str, str]:
    """Parse a .env file into a dict, ignoring comments and empty lines."""
    env: dict[str, str] = {}
    if not env_file.exists():
        return env
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = strip_quotes(value.strip())
    return env


# Values shorter than this are not treated as secrets to mask, to avoid
# redacting trivial non-sensitive values like "1", "true", or short flags.
_MIN_SECRET_LEN = 6
_SECRET_PLACEHOLDER = "***REDACTED***"


def collect_secrets(config: Config, token: str | None = None) -> list[str]:
    """Collect secret values to redact from logs and judge input."""
    candidates = list(load_env_file(config.env_file).values())
    candidates.append(os.environ.get("GITHUB_TOKEN", ""))
    if token:
        candidates.append(token)
    secrets: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        value = (value or "").strip()
        if len(value) >= _MIN_SECRET_LEN and value not in seen:
            seen.add(value)
            secrets.append(value)
    secrets.sort(key=len, reverse=True)
    return secrets


def mask_secrets(text: str | None, secrets: list[str]) -> str | None:
    """Replace any occurrence of a secret value in ``text`` with a placeholder."""
    if not text or not secrets:
        return text
    for secret in secrets:
        if secret:
            text = text.replace(secret, _SECRET_PLACEHOLDER)
    return text


def write_sanitized_env_file(config: Config) -> Path:
    """Write a quote-stripped copy of the project's .env for ``docker --env-file``.

    Returns a temp file path (mode 0600) so the container receives the same
    normalized values as hooks/evaluators, without exposing them via argv. The
    caller is responsible for deleting the returned file. If no .env exists, an
    empty temp file is returned so ``--env-file`` still gets a valid path.
    """
    parsed = load_env_file(config.env_file)
    fd, name = tempfile.mkstemp(prefix="eval-env-", suffix=".env")
    path = Path(name)
    os.chmod(path, 0o600)
    with os.fdopen(fd, "w") as f:
        for key, value in parsed.items():
            f.write(f"{key}={value}\n")
    return path


# Backward-compatible helper names for callers that have not moved to the
# public names yet.
_strip_quotes = strip_quotes
_load_env_file = load_env_file
_write_sanitized_env_file = write_sanitized_env_file
