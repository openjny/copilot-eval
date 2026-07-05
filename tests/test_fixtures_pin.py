"""Tests for fixture content-hash pinning (issue #89):

- ``pin-fixtures`` lockfile generation (shape + determinism)
- run-time drift warning
- ``run --strict-fixtures`` failure on mismatch
- fixture hashes recorded in the run manifest
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from eval.cli import main
from eval.services.fixtures_service import (
    FIXTURE_LOCKFILE_NAME,
    compute_fixture_hashes,
    hash_fixture,
    lockfile_path,
    pin_fixtures,
    verify_fixtures,
)
from tests.conftest import load_inline


def _write_fixture(config_dir: Path, name: str, files: dict[str, str]) -> Path:
    fixture_dir = config_dir / "fixtures" / name
    for rel, content in files.items():
        path = fixture_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return fixture_dir


def _base_config(fixture: str = "app") -> dict:
    return {"tasks": [{"name": "t1", "prompt": "hi", "fixture": fixture}]}


# --- hashing -----------------------------------------------------------------


def test_hash_fixture_is_deterministic_and_order_independent(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    for root in (a, b):
        (root / "src").mkdir(parents=True)
    # Same content, written in different order — digest must match.
    (a / "src" / "app.py").write_text("print('hi')\n")
    (a / "readme.md").write_text("docs\n")
    (b / "readme.md").write_text("docs\n")
    (b / "src" / "app.py").write_text("print('hi')\n")

    ha = hash_fixture(a)
    hb = hash_fixture(b)
    assert ha["sha256"] == hb["sha256"]
    assert ha["total_size"] == hb["total_size"] == len("print('hi')\n") + len("docs\n")
    assert [f["path"] for f in ha["files"]] == ["readme.md", "src/app.py"]


def test_hash_fixture_changes_with_content(tmp_path: Path):
    root = tmp_path / "fx"
    root.mkdir()
    (root / "a.txt").write_text("one")
    first = hash_fixture(root)["sha256"]
    (root / "a.txt").write_text("two")
    assert hash_fixture(root)["sha256"] != first


# --- pin-fixtures (lockfile generation) --------------------------------------


def test_pin_fixtures_writes_human_readable_lockfile(tmp_path: Path):
    config = load_inline(tmp_path, _base_config("app"))
    _write_fixture(tmp_path, "app", {"main.py": "x = 1\n", "sub/b.txt": "hello"})

    result = pin_fixtures(config)
    assert result.path == lockfile_path(config)
    assert result.path.exists()

    data = yaml.safe_load(result.path.read_text())
    assert data["version"] == 1
    entry = data["fixtures"]["app"]
    assert set(entry) == {"sha256", "total_size", "files"}
    assert entry["total_size"] == len("x = 1\n") + len("hello")
    paths = [f["path"] for f in entry["files"]]
    assert paths == ["main.py", "sub/b.txt"]
    for f in entry["files"]:
        assert set(f) == {"path", "sha256", "size"}
    # Header comment keeps it self-documenting.
    assert result.path.read_text().startswith("# fixtures.lock")


def test_pin_fixtures_is_deterministic(tmp_path: Path):
    config = load_inline(tmp_path, _base_config("app"))
    _write_fixture(tmp_path, "app", {"main.py": "x = 1\n"})
    first = pin_fixtures(config).path.read_text()
    second = pin_fixtures(config).path.read_text()
    assert first == second


def test_pin_fixtures_reports_missing(tmp_path: Path):
    config = load_inline(tmp_path, _base_config("does-not-exist"))
    result = pin_fixtures(config)
    assert result.missing == ["does-not-exist"]
    assert result.fixtures == {}


def test_pin_fixtures_cli(tmp_path: Path):
    load_inline(tmp_path, _base_config("app"))
    _write_fixture(tmp_path, "app", {"main.py": "x = 1\n"})
    res = CliRunner().invoke(main, ["pin-fixtures", "--config-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "Pinned 1 fixture(s)" in res.output
    assert (tmp_path / FIXTURE_LOCKFILE_NAME).exists()


# --- verification ------------------------------------------------------------


def test_verify_fixtures_no_lockfile(tmp_path: Path):
    config = load_inline(tmp_path, _base_config("app"))
    _write_fixture(tmp_path, "app", {"main.py": "x = 1\n"})
    verification = verify_fixtures(config)
    assert verification.lockfile_present is False
    assert not verification.has_problems


def test_verify_fixtures_matches(tmp_path: Path):
    config = load_inline(tmp_path, _base_config("app"))
    _write_fixture(tmp_path, "app", {"main.py": "x = 1\n"})
    pin_fixtures(config)
    verification = verify_fixtures(config)
    assert verification.lockfile_present is True
    assert verification.matched == ["app"]
    assert not verification.has_problems


def test_verify_fixtures_detects_drift(tmp_path: Path):
    config = load_inline(tmp_path, _base_config("app"))
    fx = _write_fixture(tmp_path, "app", {"main.py": "x = 1\n"})
    pin_fixtures(config)
    (fx / "main.py").write_text("x = 2\n")  # mutate after pinning

    verification = verify_fixtures(config)
    assert verification.has_problems
    assert any("app" in m and "changed" in m for m in verification.drifted)
    assert "main.py" in verification.drifted[0]


def test_verify_fixtures_flags_unpinned(tmp_path: Path):
    config = load_inline(tmp_path, _base_config("app"))
    _write_fixture(tmp_path, "app", {"main.py": "x = 1\n"})
    pin_fixtures(config)
    # Add a second fixture referenced by a new task but never pinned.
    config.tasks[0].fixtures = ["app", "extra"]
    _write_fixture(tmp_path, "extra", {"y.py": "y = 1\n"})

    verification = verify_fixtures(config)
    assert any("extra" in m for m in verification.unpinned)


# --- manifest hashes ---------------------------------------------------------


def test_compute_fixture_hashes_compact(tmp_path: Path):
    config = load_inline(tmp_path, _base_config("app"))
    _write_fixture(tmp_path, "app", {"main.py": "x = 1\n"})
    hashes = compute_fixture_hashes(config, config.tasks)
    assert set(hashes) == {"app"}
    entry = hashes["app"]
    assert set(entry) == {"sha256", "total_size", "file_count"}
    assert entry["file_count"] == 1
    # Matches the full hash's digest.
    assert entry["sha256"] == hash_fixture(tmp_path / "fixtures" / "app")["sha256"]


# --- run-time enforcement (_verify_fixtures_or_abort) ------------------------


def test_strict_fixtures_aborts_on_drift(tmp_path: Path):
    import pytest
    from click import ClickException

    from eval.services.orchestrator import _verify_fixtures_or_abort

    config = load_inline(tmp_path, _base_config("app"))
    fx = _write_fixture(tmp_path, "app", {"main.py": "x = 1\n"})
    pin_fixtures(config)
    (fx / "main.py").write_text("x = 999\n")

    with pytest.raises(ClickException, match="Fixture drift"):
        _verify_fixtures_or_abort(config, config.tasks, strict_fixtures=True)


def test_strict_fixtures_aborts_without_lockfile(tmp_path: Path):
    import pytest
    from click import ClickException

    from eval.services.orchestrator import _verify_fixtures_or_abort

    config = load_inline(tmp_path, _base_config("app"))
    _write_fixture(tmp_path, "app", {"main.py": "x = 1\n"})
    with pytest.raises(ClickException, match="no fixtures.lock"):
        _verify_fixtures_or_abort(config, config.tasks, strict_fixtures=True)


def test_drift_only_warns_without_strict(tmp_path: Path, capsys):
    from eval.services.orchestrator import _verify_fixtures_or_abort

    config = load_inline(tmp_path, _base_config("app"))
    fx = _write_fixture(tmp_path, "app", {"main.py": "x = 1\n"})
    pin_fixtures(config)
    (fx / "main.py").write_text("x = 999\n")

    # Must not raise; drift is surfaced as a warning only.
    _verify_fixtures_or_abort(config, config.tasks, strict_fixtures=False)
    err = capsys.readouterr().err
    assert "fixture drift" in err.lower()


# --- manifest recording ------------------------------------------------------


def test_manifest_records_fixture_hashes(tmp_path: Path):
    from eval.services.manifest import MANIFEST_NAME, write_manifest

    config = load_inline(tmp_path, _base_config("app"))
    _write_fixture(tmp_path, "app", {"main.py": "x = 1\n"})
    hashes = compute_fixture_hashes(config, config.tasks)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_manifest(run_dir, "run-1", [], fixtures=hashes)

    manifest = json.loads((run_dir / MANIFEST_NAME).read_text())
    assert manifest["fixtures"] == hashes
    assert manifest["fixtures"]["app"]["sha256"]
