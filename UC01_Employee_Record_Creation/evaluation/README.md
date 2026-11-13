# evaluation

Evaluation datasets and evaluators.

[`run_evals.py`](run_evals.py) is the entry point and what CI runs:

```
uv run python evaluation/run_evals.py                          # all scenarios
uv run python evaluation/run_evals.py --list                   # names only
uv run python evaluation/run_evals.py --scenario t01_happy_path
```

It exits 0 only when every selected scenario passes, so CI can gate on
it. A full run writes `uc01/results.json`.

The scenarios and the scoring live in [`uc01/`](uc01/) -- see
[uc01/README.md](uc01/README.md) for the scenario format and what every
run asserts. `tests/test_evals.py` runs the same scenarios in-process,
so `uv run pytest` covers them too.
