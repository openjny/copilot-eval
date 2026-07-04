"""Tests for the shared run-slug naming helpers (fixture-aware)."""

from __future__ import annotations

from eval.naming import FIXTURE_MARKER, parse_slug, run_slug, split_fixture


def test_run_slug_legacy_no_fixture():
    # Empty fixture keeps the legacy stem so old result dirs stay compatible.
    assert run_slug("task", "variant", 1) == "task_variant_epoch1"
    assert run_slug("task", "variant", 1, "") == "task_variant_epoch1"


def test_run_slug_with_fixture():
    assert run_slug("task", "variant", 2, "legacy-api") == (
        f"task_variant_epoch2{FIXTURE_MARKER}legacy-api"
    )


def test_split_fixture():
    assert split_fixture("1") == ("1", "")
    assert split_fixture(f"3{FIXTURE_MARKER}sample-app") == ("3", "sample-app")


def test_parse_slug_legacy():
    assert parse_slug("code-review_baseline_epoch1", ["baseline", "plugin"]) == (
        "baseline",
        "",
        "1",
    )


def test_parse_slug_with_fixture():
    stem = f"code-review_plugin_epoch2{FIXTURE_MARKER}legacy-api"
    assert parse_slug(stem, ["baseline", "plugin"]) == ("plugin", "legacy-api", "2")


def test_parse_slug_matches_longest_variant():
    # "v" and "my_v": a stem for "my_v" must not be claimed by "v".
    assert parse_slug("t_my_v_epoch1", ["v", "my_v"]) == ("my_v", "", "1")


def test_parse_slug_unknown_variant_returns_none():
    assert parse_slug("t_unknown_epoch1", ["v", "my_v"]) is None


def test_parse_slug_no_epoch_returns_none():
    assert parse_slug("no-epoch-here", ["v"]) is None


def test_run_slug_parse_slug_round_trip():
    for fixture in ["", "sample-app", "legacy.api", "micro-x"]:
        stem = run_slug("my-task", "my_v", 5, fixture)
        assert parse_slug(stem, ["v", "my_v"]) == ("my_v", fixture, "5")
