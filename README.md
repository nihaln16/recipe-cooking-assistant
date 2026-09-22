# Recipe Cooking Assistant

Turn recipe text or screenshots into a structured recipe and an interactive, step-by-step cooking experience.

## Status

**Milestone 3 complete** (gap review UI: accept/edit/reject findings, original payload immutable). Cooking Mode, chat, and deploy are paused.

See [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) for the full handoff.

## Run locally

```bash
cp .env.example .env
# Set SESSION_SECRET and OPENAI_API_KEY privately (never paste keys into chat).

uv sync --extra dev
uv run recipe-cooking-assistant
```

Open http://127.0.0.1:8000 · Health: `GET /health`

## Tests (free / CI-safe)

```bash
uv run pytest
uv run python evals/run_deterministic_eval.py
```

Deterministic evals use mocked model outputs only — no API credits.

## Live evaluation (manual, paid)

Never runs in pytest/CI. Plan first, then execute explicitly:

```bash
uv run python evals/run_live_eval.py --plan
uv run python evals/run_live_eval.py --case CREAMY_TOMATO_INSTRUCTION_ONLY --execute
```

Copyrighted screenshots belong only under gitignored `evals/local/` for manual runs. Checked-in fixtures are synthetic or text-only.

## Behavior notes

- One recipe per import. Responses API only (`/v1/responses`).
- Screenshots processed in upload order; uncertain order is flagged.
- Instruction-only mentions (in steps but not the ingredient list) become findings for review — not ordinary listed ingredients until the user adds them. Source-stated amounts (e.g. “2 cloves garlic”) are preserved; invented amounts are not.
- Alternatives share an `alternative_group_id` (OR, not both required).
- Package text and `source_text` preserve canned/count wording; qualifiers render as `Salt — to taste`.
- Default model: `gpt-4.1-mini`.
