"""Typed exception hierarchy for the eval runner and services.

Replaces bare ``except Exception`` catches in :mod:`eval.runner` and
:mod:`eval.services` with narrow, typed catches so that expected failure
domains (a failed Docker command, missing auth, an unusable judge response, a
failed hook script, or a fixture that couldn't be prepared) are
distinguishable from genuinely unexpected bugs -- and so their error messages
carry enough context (which command, which file, which judge) to debug
without digging through logs. See issue #90.

Every exception here derives from :class:`EvalError`, so callers that don't
need type-level distinctions can catch that one base class to handle any
*expected* eval-framework failure, while unrelated/unexpected exceptions
(programming errors, etc.) are left to propagate.
"""

from __future__ import annotations


class EvalError(Exception):
    """Base class for all errors raised by the eval framework itself."""


class DockerError(EvalError):
    """A Docker command (build, run, or health check) failed."""


class AuthError(EvalError):
    """GitHub/Copilot authentication is missing or invalid."""


class JudgeParseError(EvalError):
    """A judge evaluator's response could not be parsed into a usable score."""


class JudgeInvocationError(EvalError):
    """A judge-model Copilot invocation failed (timeout, missing CLI, non-zero exit).

    Distinct from :class:`JudgeParseError`: the model was never reached or
    exited abnormally, so there is no response to parse. Raised by
    :meth:`eval.judge_executor.JudgeExecutor.complete` and surfaced by callers
    such as the ``suggest-evaluators`` command (issue #93).
    """


class HookError(EvalError):
    """A before_run/after_run/health_check hook script failed to execute."""


class FixtureError(EvalError):
    """A task's fixture directory/files could not be prepared for a run."""


class ReplayError(EvalError):
    """A replay (offline/mock) runner could not find or read recorded fixtures.

    Raised by :class:`eval.runners.replay_runner.ReplayRunner` when the
    directory of pre-recorded agent outputs/traces it is asked to replay is
    missing or empty. The replay runner is a *test/dev-only* harness (issue
    #132): it exists to drive the evaluator → report pipeline offline (no
    Docker, no Copilot auth), never to produce a real, isolated measurement, so
    a misconfigured recording should fail loudly rather than silently emit an
    empty "run".
    """


class RemoteFixtureError(FixtureError):
    """A remote "dataset-as-code" fixture could not be fetched or verified.

    Covers a failed download, an unsupported/corrupt archive, or — most
    importantly — a checksum mismatch, which must fail the run *closed* so an
    eval never executes against unverified inputs (issue #122).
    """
