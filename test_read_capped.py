from pathlib import Path
from eval.services.suggest_service import _read_capped

p = Path("huge.txt")
p.write_text("a" * 10_000_000)
_read_capped(p)
