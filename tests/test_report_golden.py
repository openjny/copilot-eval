"""Golden-file tests for eval.report's statistical calculations and formatters.

Each scenario in tests/fixtures/golden_reports/<name>.json describes the
RunMetrics results (and optionally manifest_runs / trace_test_ids / judge
"*.scores.json" fixtures) that feed eval.report.build_report(). The formatted
table/json/markdown output is compared byte-for-byte against the corresponding
tests/fixtures/golden_reports/<name>.{table,json,md}.expected file.

A bug in the bootstrap CI, paired-delta pairing, or aggregation would silently
corrupt every A/B decision this framework produces, so these golden files pin
down the exact numeric output of build_report()/format_*() for a range of
scenarios (small-N warnings, all-failed survivorship bias, single variant,
metric evaluators, judge self-consistency, seeded bootstrap determinism, and
counterbalanced epoch ordering).

Regenerate the expected files after an *intentional* change to eval/report.py:

    uv run pytest tests/test_report_golden.py --update-golden

Always review the resulting diff before committing regenerated golden files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eval.report import Report, build_report, format_json, format_markdown, format_table
from eval.trace import RunMetrics

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "golden_reports"

# Fields accepted by RunMetrics(); anything else in a fixture dict is ignored,
# which lets scenario JSON carry a top-level "description" alongside "results".
_RUN_METRICS_FIELDS = frozenset(RunMetrics.__dataclass_fields__)


def _scenario_names() -> list[str]:
    return sorted(p.stem for p in FIXTURES_DIR.glob("*.json"))


def _load_scenario(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _make_run_metrics(d: dict[str, Any]) -> RunMetrics:
    kwargs = {k: v for k, v in d.items() if k in _RUN_METRICS_FIELDS}
    return RunMetrics(**kwargs)


def _build_reports(data: dict[str, Any], results_dir: Path) -> list[Report]:
    """Build reports for one scenario, writing any judge "*.scores.json" fixtures first."""
    for slug, scores in data.get("scores", {}).items():
        (results_dir / f"{slug}.scores.json").write_text(json.dumps(scores), encoding="utf-8")

    results = [_make_run_metrics(r) for r in data.get("results", [])]
    trace_test_ids = set(data["trace_test_ids"]) if data.get("trace_test_ids") is not None else None
    return build_report(
        results,
        results_dir=results_dir,
        variant_order=data.get("variant_order"),
        aggregate=data.get("aggregate", "paired"),
        manifest_runs=data.get("manifest_runs"),
        trace_test_ids=trace_test_ids,
    )


def _assert_or_update(expected_path: Path, actual: str, update_golden: bool) -> None:
    if update_golden:
        expected_path.write_text(actual, encoding="utf-8")
        return
    assert expected_path.exists(), (
        f"Missing golden file {expected_path}. "
        "Run `uv run pytest tests/test_report_golden.py --update-golden` to generate it."
    )
    expected = expected_path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{expected_path.name} output changed. If this is intentional, regenerate with "
        "`uv run pytest tests/test_report_golden.py --update-golden` and review the diff."
    )


@pytest.mark.parametrize("name", _scenario_names())
def test_golden_report(name: str, tmp_path: Path, update_golden: bool) -> None:
    data = _load_scenario(name)
    reports = _build_reports(data, tmp_path)

    outputs = {
        "table": format_table(reports),
        "json": format_json(reports),
        "md": format_markdown(reports),
    }
    for ext, content in outputs.items():
        _assert_or_update(FIXTURES_DIR / f"{name}.{ext}.expected", content, update_golden)


# --- Bootstrap CI determinism (seeded RNG) ---
#
# The bootstrap CI uses a fixed seed (_BOOTSTRAP_SEED) so identical inputs must
# yield byte-identical output on every call. This is a property test on top of
# the "bootstrap_ci_determinism" fixture's golden files above: it calls
# build_report() twice on the same input and asserts the formatted output
# (including the bootstrap CI bounds) is exactly the same both times.


def test_bootstrap_ci_is_deterministic_across_repeated_runs(tmp_path: Path) -> None:
    data = _load_scenario("bootstrap_ci_determinism")

    reports_a = _build_reports(data, tmp_path / "run_a")
    reports_b = _build_reports(data, tmp_path / "run_b")

    assert format_json(reports_a) == format_json(reports_b)
    assert format_table(reports_a) == format_table(reports_b)
    assert format_markdown(reports_a) == format_markdown(reports_b)

    dur_row = next(r for r in reports_a[0].summary if r.metric == "Duration (s)")
    assert dur_row.ci_low is not None
    assert dur_row.ci_high is not None
    assert dur_row.significant is True  # consistent ~15% speedup across n=8 paired epochs


def test_bootstrap_ci_is_deterministic_across_repeated_calls_same_dir(tmp_path: Path) -> None:
    """Same results_dir across two build_report() calls must also be stable."""
    data = _load_scenario("bootstrap_ci_determinism")

    reports_a = _build_reports(data, tmp_path)
    reports_b = _build_reports(data, tmp_path)

    dur_a = next(r for r in reports_a[0].summary if r.metric == "Duration (s)")
    dur_b = next(r for r in reports_b[0].summary if r.metric == "Duration (s)")
    assert (dur_a.ci_low, dur_a.ci_high) == (dur_b.ci_low, dur_b.ci_high)


# --- Counterbalanced epoch ordering is independent of input order ---


def test_counterbalanced_ordering_sorted_by_variant_then_epoch(tmp_path: Path) -> None:
    data = _load_scenario("counterbalanced_epoch_ordering")
    reports = _build_reports(data, tmp_path)
    report = reports[0]

    # Regardless of the (scrambled) input order, runs are grouped by variant
    # (in variant_order) and sorted numerically by epoch within each variant.
    assert [(r.variant, r.epoch) for r in report.runs] == [
        ("base", "1"),
        ("base", "2"),
        ("base", "3"),
        ("base", "4"),
        ("base", "5"),
        ("new", "1"),
        ("new", "2"),
        ("new", "3"),
        ("new", "4"),
        ("new", "5"),
    ]
    # All 5 epochs are shared between variants -> fully paired.
    assert report.paired_n == 5
