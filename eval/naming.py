"""Shared run-slug naming used by both the write side (runner/docker) and the
read side (cli/report), so persisted file names always agree on how the
`variant × fixture × epoch` axes map to a filename stem.

The legacy slug is ``{task}_{variant}_epoch{epoch}``. When a task declares
multiple fixtures, each run needs a distinct stem, so the fixture is appended as
``..._epoch{epoch}__fixture__{fixture}``. Single-fixture / legacy tasks keep the
old stem verbatim (fixture label is an empty string), preserving backward
compatibility with older result directories and their parsers.
"""

from __future__ import annotations

# Delimiter separating the fixture label from the rest of the slug. Chosen to be
# extremely unlikely to appear in a task/variant/fixture name (which are limited
# to letters, digits, '.', '_' and '-').
FIXTURE_MARKER = "__fixture__"


def run_slug(task: str, variant: str, epoch: object, fixture: str = "") -> str:
    """Build the filename stem for a single run.

    ``fixture`` is the *reporting* fixture label: pass an empty string for
    single-fixture / legacy tasks (keeps the legacy stem) and the fixture name
    for multi-fixture tasks.
    """
    base = f"{task}_{variant}_epoch{epoch}"
    if fixture:
        return f"{base}{FIXTURE_MARKER}{fixture}"
    return base


def split_fixture(epoch_part: str) -> tuple[str, str]:
    """Split the post-``_epoch`` slug token into ``(epoch, fixture)``.

    The fixture is an empty string for legacy stems that carry no fixture label.
    """
    if FIXTURE_MARKER in epoch_part:
        epoch, fixture = epoch_part.split(FIXTURE_MARKER, 1)
        return epoch, fixture
    return epoch_part, ""


def parse_slug(stem: str, variants: list[str]) -> tuple[str, str, str] | None:
    """Parse a run stem into ``(variant, fixture, epoch)``.

    Returns ``None`` when no known variant matches. ``variants`` is the set of
    configured variant names; the longest match wins so a shorter name (e.g.
    ``v``) can't claim a file that belongs to ``my_v``.
    """
    parts = stem.rsplit("_epoch", 1)
    if len(parts) < 2:
        return None
    name_variant = parts[0]
    epoch, fixture = split_fixture(parts[1])
    matches = [v for v in variants if name_variant.endswith(f"_{v}")]
    if not matches:
        return None
    variant = max(matches, key=len)
    return variant, fixture, epoch
