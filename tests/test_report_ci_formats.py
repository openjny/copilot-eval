"""Tests for the CI-native report formats: JUnit XML, GitHub Actions step
summary, and self-contained HTML (`analyze -o junit|gha-summary|html`).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from eval.report import (
    build_report,
    format_gha_summary,
    format_html,
    format_junit,
    format_markdown_compact,
    write_gha_summary,
)
from tests.conftest import make_metrics

# --- JUnit XML ---


def _regression_reports():
    # b's duration is ~10x a's across all 5 paired epochs -> clearly
    # significant (CI excludes zero) and unfavorable (duration should be low).
    results = [make_metrics("t1", "a", str(i), duration=10.0 + i) for i in range(1, 6)] + [
        make_metrics("t1", "b", str(i), duration=110.0 + i) for i in range(1, 6)
    ]
    return build_report(results, variant_order=["a", "b"], aggregate="paired")


def _improvement_reports():
    # b's duration is ~10x *lower* than a's -> significant and favorable.
    results = [make_metrics("t1", "a", str(i), duration=110.0 + i) for i in range(1, 6)] + [
        make_metrics("t1", "b", str(i), duration=10.0 + i) for i in range(1, 6)
    ]
    return build_report(results, variant_order=["a", "b"], aggregate="paired")


def test_format_junit_is_valid_xml():
    reports = _regression_reports()
    xml_str = format_junit(reports)
    assert xml_str.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    root = ET.fromstring(xml_str)
    assert root.tag == "testsuites"
    testsuites = root.findall("testsuite")
    assert len(testsuites) == 1
    suite = testsuites[0]
    assert suite.get("name") == "t1"
    testcases = suite.findall("testcase")
    assert len(testcases) == int(suite.get("tests", "0"))
    assert testcases, "expected at least one testcase per task"
    for tc in testcases:
        assert tc.get("classname") == "t1"
        assert tc.get("name")
        # exactly one of failure/system-out per testcase
        assert len(tc.findall("failure")) + len(tc.findall("system-out")) == 1


def test_format_junit_marks_significant_regression_as_failure():
    reports = _regression_reports()
    xml_str = format_junit(reports)
    root = ET.fromstring(xml_str)
    suite = root.find("testsuite")
    assert suite is not None
    assert int(suite.get("failures", "0")) >= 1
    duration_tc = next(tc for tc in suite.findall("testcase") if tc.get("name") == "Duration (s)")
    failure = duration_tc.find("failure")
    assert failure is not None
    assert "regression" in (failure.get("message") or "").lower()
    assert failure.get("type") == "RegressionError"
    # top-level testsuites rollup mirrors the per-suite counts
    assert int(root.get("failures", "0")) == int(suite.get("failures", "0"))
    assert int(root.get("tests", "0")) == int(suite.get("tests", "0"))


def test_format_junit_no_failure_on_significant_improvement():
    reports = _improvement_reports()
    xml_str = format_junit(reports)
    root = ET.fromstring(xml_str)
    suite = root.find("testsuite")
    assert suite is not None
    duration_tc = next(tc for tc in suite.findall("testcase") if tc.get("name") == "Duration (s)")
    assert duration_tc.find("failure") is None
    assert duration_tc.find("system-out") is not None


def test_format_junit_no_failure_without_paired_comparison():
    # Single variant: no CI/significance is computable, so nothing should be
    # marked as a failure -- only informational system-out.
    results = [make_metrics("t1", "a", str(i), duration=10.0 + i) for i in range(1, 4)]
    reports = build_report(results, variant_order=["a"], aggregate="paired")
    xml_str = format_junit(reports)
    root = ET.fromstring(xml_str)
    assert root.get("failures") == "0"
    for tc in root.iter("testcase"):
        assert tc.find("failure") is None


# --- GitHub Actions step summary ---


def test_format_gha_summary_matches_compact_markdown():
    reports = _regression_reports()
    assert format_gha_summary(reports) == format_markdown_compact(reports)


def test_write_gha_summary_returns_false_without_env_var():
    assert write_gha_summary("hello", env={}) is False


def test_write_gha_summary_appends_to_env_path(tmp_path: Path):
    summary_path = tmp_path / "summary.md"
    env = {"GITHUB_STEP_SUMMARY": str(summary_path)}

    assert write_gha_summary("first\n", env=env) is True
    assert write_gha_summary("second\n", env=env) is True

    content = summary_path.read_text(encoding="utf-8")
    assert content == "first\nsecond\n"


def test_write_gha_summary_adds_trailing_newline():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/summary.md"
        write_gha_summary("no trailing newline", env={"GITHUB_STEP_SUMMARY": path})
        assert Path(path).read_text(encoding="utf-8") == "no trailing newline\n"


# --- Self-contained HTML ---


def test_format_html_is_self_contained_single_file():
    reports = _regression_reports()
    html = format_html(reports)
    assert html.startswith("<!DOCTYPE html>")
    assert "<style>" in html
    # No external stylesheet/script/image references -- everything inline.
    assert "http://" not in html
    assert "https://" not in html
    assert "<link " not in html
    assert '<script src="' not in html


def test_format_html_contains_task_and_metric_data():
    reports = _regression_reports()
    html = format_html(reports)
    assert "t1" in html
    assert "Duration (s)" in html
    assert "a" in html and "b" in html


def test_format_html_color_codes_significant_regression():
    reports = _regression_reports()
    html = format_html(reports)
    assert 'class="sig-bad"' in html


def test_format_html_color_codes_significant_improvement():
    reports = _improvement_reports()
    html = format_html(reports)
    assert 'class="sig-good"' in html
