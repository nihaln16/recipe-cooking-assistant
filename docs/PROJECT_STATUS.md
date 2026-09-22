# Project status handoff — Recipe Cooking Assistant

Handoff for a new Cursor agent. Last updated after the contradiction post-validation fix (session end). **Do not expose `.env`, API keys, session secrets, copyrighted screenshots, or ignored local evaluation files.**

## User problem and V1 scope

**Problem:** Recipes in social captions or screenshots are static. Cooks need help with quantities, substitutions, vague steps, and things going wrong—while clearly separating what the creator said from what AI suggests.

**V1 product flow (target):**
1. Paste recipe/caption text and/or upload multiple screenshots of the same recipe.
2. Multimodal extraction → structured ingredients, quantities, steps, notes, uncertainties; retain original source.
3. Detect gaps/inconsistencies; label source vs AI; user accept/edit/reject (review UI = milestone 3, **not started**).
4. Clean recipe view + mobile Cooking Mode (milestone 4+, **not started**).
5. Contextual quick actions + freeform grounded chat (later).

**Hard rules:**
- Do not invent a recipe from a finished-dish photo alone → `insufficient_source`.
- Do not present AI amounts/substitutions as creator facts.
- Do not claim visual appearance proves food safety.
- One recipe per import; no web scraping; no video ingestion.

## Intentionally deferred

- Milestone 3 gap accept/edit/reject UI and Cooking Mode / chat / deploy / broad visual redesign
- Social scraping, share integration, accounts, saved library, custom OCR/CV, advanced personalization
- In-progress cooking photo analysis

## Architecture

- **Stack:** FastAPI + Jinja2 + vanilla CSS (no React/Tailwind), session cookie + ephemeral SQLite + `uploads/`
- **Model:** OpenAI **`gpt-4.1-mini`** via **Responses API only** (`client.responses.create`) with JSON Schema structured outputs and `input_text` / `input_image`
- **API key constraint:** Project key is restricted to **`/v1/responses` Write**. Never switch to Chat Completions. Never ask the user to broaden key permissions or paste keys into chat.
- **Secrets:** `OPENAI_API_KEY`, `SESSION_SECRET` in local `.env` / host env only (gitignored)

### Important paths

| Path | Role |
|------|------|
| `src/recipe_cooking_assistant/app.py` | App factory |
| `src/recipe_cooking_assistant/extraction.py` | Responses API client, logging, cost estimate |
| `src/recipe_cooking_assistant/extraction_schema.py` | JSON schema + system prompt |
| `src/recipe_cooking_assistant/models.py` | Extraction data model |
| `src/recipe_cooking_assistant/normalize.py` | Post-extract normalization |
| `src/recipe_cooking_assistant/quantity_consistency.py` | List-vs-step contradiction validation |
| `src/recipe_cooking_assistant/ingredient_display.py` | Qualifiers, alternatives grouping, display lines |
| `src/recipe_cooking_assistant/routes/` | Import + recipe HTML routes |
| `evals/harness/` | Deterministic + live eval runners/assertions |
| `evals/cases/*/case.json` | Eval cases + expects |
| `evals/local/` | **Gitignored** private/synthetic images for manual live runs |
| `evals/results/` | **Gitignored** saved live outputs |
| `docs/PROJECT_STATUS.md` | This handoff |

## Completed milestones

1. **App shell + import UI** — sessions, uploads, limits, temporary storage
2. **Extraction + eval harness** — messy sources, provenance, findings, semantic normalization, deterministic/live evals
3. **Not started:** gap review UI, Cooking Mode, chat, deployment packaging

## Provenance and uncertainty

- Field provenance: `source` | `needs_review` (later: `ai_suggestion` | `user_edit`)
- Prefer `source_text` + evidence (quote / image_index)
- Qualifiers (`to taste`, `as needed`, `for garnish`, `divided`) → notes; display like `Salt — to taste`
- Uncertain → `needs_review`, not asserted as fact

## Schema concepts (current)

- **`list_status`:** `listed` vs `instruction_only` (step mentions absent from ingredient list → finding, not ordinary listed Source item; quantities cleared for instruction-only)
- **`alternative_group_id`:** OR alternatives (shared non-null id; do not require a specific string like `alt_protein`)
- **Package fields** + **`source_text`:** preserve can/count wording
- **`optional`:** garnish/optional groups
- **Steps:** atomic split allowed; keep `source_direction_text` + evidence
- **Findings / review_flags:** `instruction_only_ingredient`, `contradiction`, `uncertain_ordering`, etc.
- **Servings:** normalize `"4 servings"` → `"4"`

## Evaluation commands

**Free / CI-safe (no API):**
```bash
uv run pytest
uv run python evals/run_deterministic_eval.py
uv run python evals/recheck_saved_result.py CASE_ID
```

**Live (manual, paid — plan first):**
```bash
uv run python evals/run_live_eval.py --plan
uv run python evals/run_live_eval.py --case CASE_ID --execute
```
Uses Responses API only. Results → `evals/results/` (gitignored). Copyrighted screenshots only under `evals/local/` (gitignored).

## Live tests completed (approximate)

| Case / event | Result | Approx. cost | Lesson |
|--------------|--------|--------------|--------|
| First live extract | 502 | — | Must use Responses API, not Chat Completions |
| Qualifier rendering | Fixed | — | `to taste` is a note → `Salt — to taste` |
| Creamy Tomato Pasta (UI) | Partial → schema fix | (interactive) | Instruction-only garlic/Parmesan must not look like listed Source; no invented Parmesan qty; olive oil may lack quantity |
| Chili ×3 screenshots | Technical pass; semantic issues → schema + harness fixes → **PASS on recheck** | ~$0.005–$0.009 | Alternatives share group id; keep package `source_text`; optional garnishes; don’t require exact ignored role `ad` if chrome simply omitted |
| `CONTRADICTION_QUANTITY` live | Fail (wrong finding) → **PASS after deterministic post-validation recheck** | ~$0.0013 | Model linked step to listed `ing_1` but emitted `instruction_only_ingredient`; post-validation reclassifies clear 2-cups-vs-3-cups as `contradiction` |

## Contradiction fix (just completed)

**Module:** `quantity_consistency.py`, applied in `normalize_result`.

1. Strip `instruction_only_ingredient` findings/flags whose `related_ids` include a **listed** ingredient.
2. For steps with `related_ingredient_ids` → listed ingredient, parse explicit `N unit name` from step text.
3. If normalized qty+compatible unit clearly conflict with listed amount → add `contradiction` (evidence preserved); skip ambiguous language / incompatible units.
4. Dedupe per ingredient.

**Recheck:** `uv run python evals/recheck_saved_result.py CONTRADICTION_QUANTITY` → **PASS** (no API call).

## Current passing counts

- **33** pytest tests passed
- **16/16** deterministic eval cases passed

## Exact next recommended task

Run a **targeted live** case to confirm end-to-end model + post-validation behavior, e.g.:

```bash
uv run python evals/run_live_eval.py --case CONTRADICTION_QUANTITY --plan
# after user approval:
uv run python evals/run_live_eval.py --case CONTRADICTION_QUANTITY --execute
```

Or proceed to **milestone 3** (narrow gap review UI: accept/edit/reject findings) only after the user explicitly asks.

## Security / privacy for the next agent

- Never read aloud, commit, or paste `.env`, API keys, or `SESSION_SECRET`
- Never commit `evals/local/`, `evals/results/`, `uploads/`, `*.db`
- Never ask the user to paste secrets into chat
- Never broaden OpenAI key scopes; stay on `/v1/responses`
