# Evaluation harness

## Deterministic (free, CI-safe)

```bash
uv run pytest
uv run python evals/run_deterministic_eval.py
uv run python evals/run_deterministic_eval.py --case CREAMY_TOMATO_INSTRUCTION_ONLY
```

Uses `mock_output.json` per case. No OpenAI calls.

## Live (manual, paid)

Uses OpenAI **Responses API** only. Plan before execute:

```bash
uv run python evals/run_live_eval.py --plan
uv run python evals/run_live_eval.py --case T1_plain_text --execute
```

Results write to `evals/results/` (gitignored).

### Private screenshots

Place copyrighted or private images only under `evals/local/` (gitignored). Example for a multi-screenshot chili case you add locally:

```text
evals/local/chili/1.png
evals/local/chili/2.png
evals/local/chili/3.png
```

Do not commit those files.
