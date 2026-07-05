"""Tests for remote "dataset-as-code" fixtures (issue #122):

- config parsing of the `{url, sha256}` fixture form (singular + list)
- content-addressed fetch + cache-hit (no re-download) + offline reuse
- checksum-mismatch fail-closed
- archive (.tar.gz / .zip) and plain-single-file extraction
- path-traversal safety
- interoperation with fixtures.lock (pin/verify) and the run manifest
- the `run --dry-run` path materializing + failing closed via the CLI

The network is always mocked — no test hits a real URL.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from eval import services
from eval.cli import main
from eval.config import ConfigError, RemoteFixture
from eval.exceptions import RemoteFixtureError
from eval.services import fixtures_service as fx
from tests.conftest import load_inline

# --- helpers -----------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_targz(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class CountingOpener:
    """A fake downloader that serves canned bytes and records every call."""

    def __init__(self, mapping: dict[str, bytes]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        return self.mapping[url]


def _remote(name: str, url: str, data: bytes) -> RemoteFixture:
    return RemoteFixture(name=name, url=url, sha256=_sha256(data))


class _Config:
    """Minimal stand-in exposing just `config_dir` for the cache functions."""

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir


# --- config parsing ----------------------------------------------------------


def test_singular_fixture_mapping_derives_name_from_url(tmp_path: Path) -> None:
    config = load_inline(
        tmp_path,
        {
            "tasks": [
                {
                    "name": "code-review",
                    "prompt": "review",
                    "fixture": {
                        "url": "https://example.com/data/code-review-v3.tar.gz",
                        "sha256": "a" * 64,
                    },
                }
            ]
        },
    )
    task = config.tasks[0]
    assert list(task.remote_fixtures) == ["code-review-v3"]
    assert task.fixture_names() == ["code-review-v3"]
    assert task.is_multi_fixture is False
    rf = task.remote_fixtures["code-review-v3"]
    assert rf.url == "https://example.com/data/code-review-v3.tar.gz"
    assert rf.sha256 == "a" * 64


def test_explicit_name_overrides_url_derivation(tmp_path: Path) -> None:
    config = load_inline(
        tmp_path,
        {
            "tasks": [
                {
                    "name": "t",
                    "prompt": "p",
                    "fixture": {
                        "url": "https://example.com/blob?id=99",
                        "sha256": "b" * 64,
                        "name": "canonical",
                    },
                }
            ]
        },
    )
    assert list(config.tasks[0].remote_fixtures) == ["canonical"]


def test_sha256_must_be_64_hex(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="64-character hex digest"):
        load_inline(
            tmp_path,
            {"tasks": [{"name": "t", "prompt": "p", "fixture": {"url": "u", "sha256": "nope"}}]},
        )


def test_unknown_remote_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t",
                        "prompt": "p",
                        "fixture": {"url": "u", "sha256": "c" * 64, "verify": True},
                    }
                ]
            },
        )


def test_undrivable_name_requires_explicit_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="explicit 'name'"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t",
                        "prompt": "p",
                        "fixture": {"url": "https://x/", "sha256": "d" * 64},
                    }
                ]
            },
        )


def test_fixtures_list_mixes_local_and_remote(tmp_path: Path) -> None:
    config = load_inline(
        tmp_path,
        {
            "tasks": [
                {
                    "name": "t",
                    "prompt": "p",
                    "fixtures": [
                        "local-app",
                        {"url": "https://example.com/remote-set.zip", "sha256": "e" * 64},
                    ],
                }
            ]
        },
    )
    task = config.tasks[0]
    assert task.fixture_names() == ["local-app", "remote-set"]
    assert list(task.remote_fixtures) == ["remote-set"]
    assert task.is_multi_fixture is True


# --- blob fetch / cache / checksum ------------------------------------------


def test_fetch_then_cache_hit_downloads_once(tmp_path: Path) -> None:
    data = _make_targz({"a.txt": "hello"})
    rf = _remote("ds", "https://example.com/ds.tar.gz", data)
    config = _Config(tmp_path)
    opener = CountingOpener({rf.url: data})

    blob1 = fx.ensure_remote_blob(config, rf, opener=opener)
    blob2 = fx.ensure_remote_blob(config, rf, opener=opener)

    assert blob1 == blob2
    assert blob1.exists()
    assert opener.calls == [rf.url], "second call must be a cache hit (no re-download)"


def test_checksum_mismatch_fails_closed(tmp_path: Path) -> None:
    served = _make_targz({"a.txt": "hello"})
    # Declare a wrong sha256 so verification of the served bytes must fail.
    rf = RemoteFixture(name="ds", url="https://example.com/ds.tar.gz", sha256="f" * 64)
    config = _Config(tmp_path)
    opener = CountingOpener({rf.url: served})

    with pytest.raises(RemoteFixtureError, match="checksum mismatch"):
        fx.ensure_remote_blob(config, rf, opener=opener)

    # Nothing unverified must be left in the cache.
    assert not fx._blob_path(config, rf.sha256).exists()


def test_download_failure_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.error import URLError

    def boom(url: str, *args: object, **kwargs: object) -> object:
        raise URLError("connection refused")

    monkeypatch.setattr(fx, "urlopen", boom)
    with pytest.raises(RemoteFixtureError, match="failed to download"):
        fx._download("https://example.com/missing.tar.gz")


# --- extraction --------------------------------------------------------------


def test_targz_extraction(tmp_path: Path) -> None:
    data = _make_targz({"src/app.py": "print(1)\n", "readme.md": "hi\n"})
    rf = _remote("ds", "https://example.com/ds.tar.gz", data)
    config = _Config(tmp_path)

    content = fx.ensure_remote_fixture(config, rf, opener=CountingOpener({rf.url: data}))

    assert (content / "src" / "app.py").read_text() == "print(1)\n"
    assert (content / "readme.md").read_text() == "hi\n"
    # The completion marker lives OUTSIDE content/ so it is never hashed as
    # part of the fixture.
    assert (content.parent / ".ok").exists()
    hashed = {f["path"] for f in fx.hash_fixture(content)["files"]}
    assert hashed == {"src/app.py", "readme.md"}


def test_zip_extraction(tmp_path: Path) -> None:
    data = _make_zip({"data/one.json": "{}", "two.txt": "x"})
    rf = _remote("ds", "https://example.com/ds.zip", data)
    config = _Config(tmp_path)

    content = fx.ensure_remote_fixture(config, rf, opener=CountingOpener({rf.url: data}))

    assert (content / "data" / "one.json").read_text() == "{}"
    assert (content / "two.txt").read_text() == "x"


def test_plain_single_file(tmp_path: Path) -> None:
    data = b'{"rows": 3}'
    rf = _remote("ds", "https://example.com/dataset.json", data)
    config = _Config(tmp_path)

    content = fx.ensure_remote_fixture(config, rf, opener=CountingOpener({rf.url: data}))

    assert (content / "dataset.json").read_bytes() == data


def test_offline_reuse_after_first_fetch(tmp_path: Path) -> None:
    data = _make_targz({"a.txt": "hi"})
    rf = _remote("ds", "https://example.com/ds.tar.gz", data)
    config = _Config(tmp_path)

    fx.ensure_remote_fixture(config, rf, opener=CountingOpener({rf.url: data}))

    def offline(url: str) -> bytes:
        raise AssertionError("must not download when already cached")

    content = fx.ensure_remote_fixture(config, rf, opener=offline)
    assert (content / "a.txt").read_text() == "hi"


def test_path_traversal_member_is_skipped(tmp_path: Path) -> None:
    # Craft a tar with a member escaping the extraction dir.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        payload = b"pwned"
        info = tarfile.TarInfo("../evil.txt")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
        safe = b"ok"
        info2 = tarfile.TarInfo("safe.txt")
        info2.size = len(safe)
        tf.addfile(info2, io.BytesIO(safe))
    data = buf.getvalue()
    rf = _remote("ds", "https://example.com/ds.tar.gz", data)
    config = _Config(tmp_path)

    content = fx.ensure_remote_fixture(config, rf, opener=CountingOpener({rf.url: data}))

    assert (content / "safe.txt").read_text() == "ok"
    assert not (content.parent / "evil.txt").exists()
    assert not (tmp_path / "evil.txt").exists()
    assert not (content / ".." / "evil.txt").exists()


# --- registry / materialize --------------------------------------------------


def test_registry_rejects_conflicting_same_name(tmp_path: Path) -> None:
    a = _make_targz({"a": "1"})
    b = _make_targz({"a": "2"})
    config = load_inline(
        tmp_path,
        {
            "tasks": [
                {
                    "name": "t1",
                    "prompt": "p",
                    "fixture": {"url": "u1", "sha256": _sha256(a), "name": "shared"},
                },
                {
                    "name": "t2",
                    "prompt": "p",
                    "fixture": {"url": "u2", "sha256": _sha256(b), "name": "shared"},
                },
            ]
        },
    )
    with pytest.raises(RemoteFixtureError, match="conflicting"):
        fx.remote_fixture_registry(config.tasks)


# --- interop: fixtures.lock (pin/verify) + manifest --------------------------


def _write_remote_config(tmp_path: Path, rf: RemoteFixture) -> object:
    return load_inline(
        tmp_path,
        {
            "tasks": [
                {
                    "name": "code-review",
                    "prompt": "review",
                    "fixture": {"url": rf.url, "sha256": rf.sha256, "name": rf.name},
                }
            ]
        },
    )


def test_pin_and_verify_remote_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _make_targz({"src/app.py": "print(1)\n"})
    rf = _remote("code-review", "https://example.com/cr.tar.gz", data)
    config = _write_remote_config(tmp_path, rf)
    monkeypatch.setattr(fx, "_download", CountingOpener({rf.url: data}))

    result = fx.pin_fixtures(config)
    assert "code-review" in result.fixtures
    entry = result.fixtures["code-review"]
    assert entry["remote"] == {"url": rf.url, "sha256": rf.sha256}

    # The lockfile round-trips and verification matches on a second fetch.
    verification = fx.verify_fixtures(config)
    assert verification.matched == ["code-review"]
    assert verification.has_problems is False


def test_manifest_records_remote_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _make_zip({"input.txt": "payload"})
    rf = _remote("code-review", "https://example.com/cr.zip", data)
    config = _write_remote_config(tmp_path, rf)
    monkeypatch.setattr(fx, "_download", CountingOpener({rf.url: data}))

    verification = fx.verify_fixtures(config)
    compact = fx.compact_hashes(verification.current_hashes)
    assert "code-review" in compact
    manifest_entry = compact["code-review"]
    assert manifest_entry["remote"] == {"url": rf.url, "sha256": rf.sha256}
    assert "sha256" in manifest_entry and "file_count" in manifest_entry


# --- CLI `run --dry-run` fetch + fail-closed ---------------------------------


def _write_config_file(tmp_path: Path, rf: RemoteFixture, declared_sha: str) -> None:
    (tmp_path / "eval-config.yaml").write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "name": "code-review",
                        "prompt": "review",
                        "fixture": {"url": rf.url, "sha256": declared_sha, "name": rf.name},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_run_dry_run_materializes_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _make_targz({"a.txt": "hi"})
    rf = _remote("code-review", "https://example.com/cr.tar.gz", data)
    _write_config_file(tmp_path, rf, rf.sha256)
    monkeypatch.setattr(fx, "_download", CountingOpener({rf.url: data}))

    result = CliRunner().invoke(
        main, ["run", "--dry-run", "--config-dir", str(tmp_path)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert "Remote fixtures ready" in result.output


def test_run_dry_run_checksum_mismatch_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _make_targz({"a.txt": "hi"})
    rf = _remote("code-review", "https://example.com/cr.tar.gz", data)
    wrong = "0" * 64
    _write_config_file(tmp_path, rf, wrong)
    monkeypatch.setattr(fx, "_download", CountingOpener({rf.url: data}))

    result = CliRunner().invoke(main, ["run", "--dry-run", "--config-dir", str(tmp_path)])
    assert result.exit_code != 0
    assert "checksum mismatch" in result.output


def test_services_module_importable() -> None:
    # Guard against accidental import cycles introduced by wiring the resolver
    # into the runner.
    assert services is not None
