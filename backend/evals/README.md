# Evals

Two kinds of evals live here: deterministic checks that run with no model and no API spend, and a live LangSmith runner that scores the real agent against a golden dataset. Both are seeded from bugs I actually hit during the build, so a green run means those regressions stay fixed.

Run everything from `backend/` with the venv active.

## Files

- **`run_evals.py`**: the local regression harness. Runs the deterministic checks (and optionally the live cases). This is the 67-check suite that gates changes.
- **`langsmith_eval.py`**: the live runner. Pulls the golden dataset from LangSmith, runs each case through the real agent turn, and scores it with 6 reference evaluators (terminator kind, found, report-ready, sanctions status with labeling discipline, expected-entity recall, a must-not structural guard), an optional LLM judge, and per-case deterministic checks.
- **`multiturn.py`**: the multi-turn memory eval. Runs sequential turns in one conversation through the real write path and asserts a finding survives and is recoverable without re-running the tool that produced it (the Rosneft recall regression).
- **`branching.py`**: the branching eval. Pins fork isolation, path-graph accumulation, and linear regression for the turn tree.
- **`source_mix.py`**: the router eval. Proves the intent router can never strand the cross-source corroboration tools (`check_sanctions` + `search_entity`) on an investigative turn.
- **`sayari-demo-golden.jsonl`**: the 12-case golden dataset.

## Deterministic (no model, no spend)

```bash
.venv/bin/python -m evals.run_evals --deterministic-only
```

This runs the in-process checks (terminator routing, sanctions gate, not-found honesty, clarify routing, provenance, plus the multi-turn, branching, and source-mix suites). No network, no credits. Should print 67/67.

## Live (spends Anthropic credits)

```bash
.venv/bin/python -m evals.run_evals                 # deterministic + live cases locally
.venv/bin/python -m evals.run_evals --push          # also upload to LangSmith (needs LANGCHAIN_API_KEY)
.venv/bin/python -m evals.langsmith_eval --live     # full LangSmith experiment upload
.venv/bin/python -m evals.langsmith_eval --live --judge --limit 1   # cheap smoke with the LLM judge
```

## The `--model` flag

Both runners take `--model` to pick the main-agent model for the live cases. It's allowlisted (anything off the list falls back to the default Sonnet 4.5), so you can compare model families on the same cases:

```bash
.venv/bin/python -m evals.langsmith_eval --live --model claude-sonnet-4-5-20250929
.venv/bin/python -m evals.langsmith_eval --live --model claude-haiku-4-5-20251001
```

Each model uploads as its own named experiment (`sayari-demo-sonnet-4-5-*`, `sayari-demo-haiku-4-5-*`) so you can compare them side by side in LangSmith. The measured side-by-side results are in [`../../PROJECT_SUMMARY.md`](../../PROJECT_SUMMARY.md).

## Outputs

Live run artifacts land in `results/`, which is gitignored. The golden dataset and the runners above are the only tracked eval assets.
