from eval.config import Evaluator, Task, _parse_evaluators
from eval.services.suggest_service import ensure_coverage, _DEFAULT_JUDGE, _DEFAULT_METRIC_GATE

evs = [
    Evaluator(name="cost-gate", type="judge", criterion="rate", rubric={1:"a", 10:"b"}),
    Evaluator(name="cost-budget-gate", type="judge", criterion="rate2", rubric={1:"a", 10:"b"})
]
res = ensure_coverage(evs)
print([e.name for e in res])
