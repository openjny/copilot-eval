#!/usr/bin/env python3
"""Regenerate `schemas/eval-config.schema.json` from `eval/schema.py`.

Run this after changing any config dataclass/constant in `eval/config.py`
that affects the shape of `eval-config.yaml`:

    uv run python scripts/generate_schema.py

`tests/test_schema.py` fails CI if the committed schema drifts from what this
script would produce, so forgetting to run it is caught automatically.
"""

from __future__ import annotations

import json
from pathlib import Path

from eval.schema import generate_schema

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "schemas" / "eval-config.schema.json"


def main() -> None:
    schema = generate_schema()
    OUTPUT_PATH.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
