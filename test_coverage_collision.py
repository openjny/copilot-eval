from eval.config import Evaluator
from eval.services.suggest_service import ensure_coverage

evs = [Evaluator(name="overall-quality", type="regex", value="foo")]
res = ensure_coverage(evs)
print([e.name for e in res])
