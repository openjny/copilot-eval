"""Service layer for the eval CLI.

Each module here owns one area of business logic that used to live inline in
``eval/cli.py`` (issue #83): run scheduling (:mod:`orchestrator`), Docker image
builds (:mod:`build_service`), judge evaluation (:mod:`judge_service`), metric
evaluation (:mod:`metrics_service`), trace collection (:mod:`trace_service`),
run manifest persistence (:mod:`manifest`), and the `analyze` command pipeline
(:mod:`analyze_service`). CLI command modules in :mod:`eval.cli` stay thin
wrappers that parse options and delegate here.
"""
