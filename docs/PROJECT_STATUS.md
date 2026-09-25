# Project status handoff — Recipe Cooking Assistant

Handoff for a new Cursor agent. Last updated after **serving scaling as a separate cooking view**. **Do not expose `.env`, API keys, session secrets, copyrighted screenshots, or ignored local evaluation files.**

## User problem and V1 scope

**Problem:** Recipes in social captions or screenshots are static. Cooks need help with quantities, substitutions, vague steps, and things going wrong—while clearly separating what the creator said from what AI suggests.

**V1 product flow (target):**
1. Paste recipe/caption text and/or upload multiple screenshots of the same recipe.
2. Multimodal extraction → structured ingredients, quantities, steps, notes, uncertainties; retain original source.
3. Detect gaps/inconsistencies; user accept/edit/reject on the recipe page (**milestone 3, done**).
4. Mobile Cooking Mode from the reviewed working recipe (**milestone 4, done**).
5. Contextual quick actions + freeform grounded chat inside Cooking Mode (**milestone 6, done**).

**Hard rules:**
- Do not invent a recipe from a finished-dish photo alone → `insufficient_source`.
- Do not present AI amounts/substitutions as creator facts.
- Do not claim visual appearance proves food safety.
- One recipe per import; no web scraping; no video ingestion.
- Content provenance is separate from review-decision status. Accepting a recommendation does not relabel source-derived content as `ai_suggestion`.

## Intentionally deferred

- Voice, accounts, saved library, broad visual redesign, and the Apple-design review of Cooking Mode
- Creating the Render service, GitHub secrets, or an OpenAI budget (the blueprint is in-repo; the user creates the service)
- LLM-generated quantity/substitution patches (review uses deterministic recommendations only)
- Social scraping, share integration, accounts, saved library, custom OCR/CV, advanced personalization
- In-progress cooking photo analysis

## Architecture

- **Stack:** FastAPI + Jinja2 + vanilla CSS (no React/Tailwind), session cookie + ephemeral SQLite + `uploads/`
- **Model:** OpenAI **`gpt-4.1-mini`** via **Responses API only** (`client.responses.create`) with JSON Schema structured outputs and `input_text` / `input_image`
- **API key constraint:** Project key is restricted to **`/v1/responses` Write**. Never switch to Chat Completions. Never ask the user to broaden key permissions or paste keys into chat.
- **Secrets:** `OPENAI_API_KEY`, `SESSION_SECRET` from the environment only. Locally that may be a gitignored `.env`. On Render they are service env vars. The app refuses to start when `RENDER=true` and either value is missing or `SESSION_SECRET` is still a placeholder.

### Important paths

| Path | Role |
|------|------|
| `src/recipe_cooking_assistant/app.py` | App factory |
| `src/recipe_cooking_assistant/extraction.py` | Responses API client, logging, cost estimate |
| `src/recipe_cooking_assistant/extraction_schema.py` | JSON schema + system prompt (`source` \| `needs_review` only) |
| `src/recipe_cooking_assistant/models.py` | Extraction + review-decision data model |
| `src/recipe_cooking_assistant/normalize.py` | Post-extract normalization, findings |
| `src/recipe_cooking_assistant/quantity_consistency.py` | List-vs-step contradiction + source-amount parse |
| `src/recipe_cooking_assistant/review.py` | Working-recipe overlay from review decisions |
| `src/recipe_cooking_assistant/edits.py` | Direct user edits applied after review decisions |
| `src/recipe_cooking_assistant/timer.py` | Source-duration parser and timer clock contract |
| `src/recipe_cooking_assistant/scaling.py` | Deterministic serving scale for a separate view |
| `src/recipe_cooking_assistant/cooking.py` | Step cursor, session progress, listed-id ingredient links, one-time guide token |
| `src/recipe_cooking_assistant/guidance.py` | Grounded Responses API guidance client, context bounds, navigation phrases |
| `src/recipe_cooking_assistant/ingredient_display.py` | Qualifiers, alternatives grouping, display lines |
| `src/recipe_cooking_assistant/db.py` | SQLite: bundles, extractions, `review_decisions`, `cook_messages` |
| `src/recipe_cooking_assistant/routes/` | Import, recipe, review, and cooking HTML routes |
| `evals/harness/` | Deterministic + live eval runners/assertions |
| `evals/cases/*/case.json` | Eval cases + expects |
| `evals/local/` | **Gitignored** private/synthetic images for manual live runs |
| `evals/results/` | **Gitignored** saved live outputs |
| `docs/PROJECT_STATUS.md` | This handoff |

## Completed milestones

1. **App shell + import UI** — sessions, uploads, limits, temporary storage
2. **Extraction + eval harness** — messy sources, provenance, findings, semantic normalization, deterministic/live evals, contradiction post-validation
3. **Gap review UI** — accept/edit/reject findings on `/recipes/{id}`; original payload immutable; working recipe derived at read time
4. **Cooking Mode** — one reviewed step at a time after findings are decided; session-scoped progress
5. **Deployment preparation** — `render.yaml`, `GET /health`, GitHub Actions free tests. The Render service itself is not created.
6. **Cooking conversation** — freeform questions and quick actions on each Cooking Mode step, grounded on the reviewed working recipe
7. **Edits, preparation, substitution, timer** — direct edits on the working recipe, a preparation checklist, ingredient substitution chat, and a source-duration timer
8. **Serving scale** — `/recipes/{id}/scale` derives a discardable view. The working recipe and `payload_json` stay unchanged.

## Provenance and review

- **Content provenance** (Python working copy): `source` | `needs_review` | `ai_suggestion` | `user_edit`
- OpenAI extraction schema still emits only `source` | `needs_review`
- Per-field overrides: `name_provenance`, `quantity_provenance`, `unit_provenance`, `text_provenance` (null = inherit item provenance)
- Unchanged source-derived fields stay `source`. Edited name/qty/unit → `user_edit` on that field only
- **`ai_suggestion` is reserved** for content actually generated or inferred by AI. Milestone 3 does not set it (promoting or choosing a source fact is not an AI suggestion)
- Review-decision status lives in `review_decisions` (`accepted` | `edited` | `rejected`), not on content provenance
- Qualifiers (`to taste`, `as needed`, `for garnish`, `divided`) → notes; display like `Salt — to taste`
- Uncertain extraction → `needs_review`, not asserted as fact
- Instruction-only amounts: keep qty/unit when the instruction states them (e.g. “Add 2 cloves garlic”); clear invented amounts; never invent

## Schema concepts (current)

- **`list_status`:** `listed` vs `instruction_only` (step mentions absent from the list → finding; not shown as listed Source until the user adds them)
- **`alternative_group_id`:** OR alternatives (shared non-null id)
- **Package fields** + **`source_text`:** preserve can/count wording
- **`optional`:** garnish/optional groups
- **Steps:** atomic split allowed; keep `source_direction_text` + evidence
- **Findings:** `instruction_only_ingredient`, `contradiction`, `missing_quantity` (listed items with no amount and no qualifier note), `uncertain_ordering`, `uncertain_extraction`, `other`
- **Servings:** normalize `"4 servings"` → `"4"`

## Milestone 3 review behavior

- GET `/recipes/{id}` shows pending finding cards with specific primaries: **Add to ingredients**, **Keep listed amount**, **Keep unspecified**, **Keep as extracted**, plus Edit and Reject
- POST `/recipes/{id}/review/{finding_id}` (session-scoped); 404 for other sessions; 400 on invalid action/fields
- Original `extracted_recipes.payload_json` is never updated. Working recipe = original + accepted/edited decisions
- Rejected decisions remain visible under **Decided** and are not applied
- **Start cooking** appears when `pending_review` is empty, including recipes that never had findings

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
| Creamy Tomato Pasta (UI) | Partial → schema fix | (interactive) | Instruction-only garlic/Parmesan must not look like listed Source until review; no invented Parmesan qty; olive oil may lack quantity |
| Chili ×3 screenshots | Technical pass; semantic issues → schema + harness fixes → **PASS on recheck** | ~$0.005–$0.009 | Alternatives share group id; keep package `source_text`; optional garnishes; don’t require exact ignored role `ad` if chrome simply omitted |
| `CONTRADICTION_QUANTITY` live | Fail (wrong finding) → **PASS after deterministic post-validation recheck** | ~$0.0013 | Model linked step to listed `ing_1` but emitted `instruction_only_ingredient`; post-validation reclassifies clear 2-cups-vs-3-cups as `contradiction` |

## Contradiction post-check (milestone 2)

**Module:** `quantity_consistency.py`, applied in `normalize_result`.

1. Strip `instruction_only_ingredient` findings/flags whose `related_ids` include a **listed** ingredient.
2. For steps with `related_ingredient_ids` → listed ingredient, parse explicit `N unit name` from step text.
3. If normalized qty+compatible unit clearly conflict with listed amount → add `contradiction` (evidence preserved); skip ambiguous language / incompatible units.
4. Dedupe per ingredient.

**Recheck:** `uv run python evals/recheck_saved_result.py CONTRADICTION_QUANTITY` → **PASS** (no API call).

## Current passing counts

- **108** pytest tests passed
- **16/16** deterministic eval cases passed

## Milestone 3 follow-up (duplicate findings)

Review queue is `result.findings` after `canonicalize_findings` in `normalize_result` (and again on recipe GET/POST). Semantic key is finding **type + related target**, not generated ids or exact message punctuation. Model `find_1` + deterministic `find_ing_6` for the same garlic mention collapse to one card; alias ids share one review decision. Stronger evidence (quote/image) is kept.

**Ingredient display:** Formatter-only conventional recipe lines: lowercase name after quantity/unit (`1 tablespoon olive oil`), leading capital when there is no numeric prefix (`Olive oil`), `Salt — to taste`, prep notes as `, minced`. Proper nouns (e.g. Parmesan) and source evidence quotes are preserved. Stored source fields and provenance are not mutated. `to taste` still does not create a missing-quantity finding.

**Parmesan on the live Creamy Tomato extract** (`finish with grated Parmesan`) was **absent from extraction**, not dropped later. The step text includes Parmesan but `related_ingredient_ids` pointed at penne; no Parmesan ingredient row exists. Do not invent a quantity. Repairing that requires a new extract (or a later gap detector), not a review-queue fix.

## Milestone 4 cooking behavior

- **Start cooking** on `/recipes/{id}` when no finding is pending. `GET /recipes/{id}/cook` resumes this browser session.
- `GET /recipes/{id}/cook/{n}` shows step `n`. `POST /recipes/{id}/cook` handles Back, Next, Finish, and Cook again.
- Each request reads `prepare_recipe()` → `apply_decisions()`. `payload_json` is not updated. Rejected decisions are not applied.
- Step ingredients come from `related_ingredient_ids` that resolve to listed working ingredients, including that ingredient’s alternative group. Step text is not scanned for extra links.
- Progress is `{index, done}` in the signed session cookie, keyed by recipe id. An invalid step URL returns 400 and does not replace a valid cursor.
- Unresolved review returns 400 with **Back to recipe**. It does not accept, edit, or reject findings.
- Expired recipes and other sessions get the same 404 as the recipe page.
- Zero steps: empty cooking page. One step: Back disabled, Finish. Last step: Finish. Completion links back to the reviewed recipe.

## Milestone 5 deployment

- Blueprint: `render.yaml`. Branch `main`. `autoDeployTrigger: checksPass`. Plan `0.5c-512mb`. Health path `/health`.
- Build: `pip install uv && uv sync --frozen --no-dev`
- Start: `uv run uvicorn recipe_cooking_assistant.app:app --host 0.0.0.0 --port $PORT --proxy-headers`
- Local `uv run recipe-cooking-assistant` still binds `127.0.0.1:8000` with reload. On Render it binds `0.0.0.0` and `$PORT`.
- Required env names: `OPENAI_API_KEY` (dashboard, `sync: false`), `SESSION_SECRET` (Render `generateValue`). Also set in the blueprint, not secret: `OPENAI_MODEL=gpt-4.1-mini`, `SESSION_TTL_HOURS=24`, `MAX_IMAGES=6`, `MAX_UPLOAD_BYTES=4194304`, `DATA_DIR=/tmp/recipe-cooking-assistant`, `PYTHON_VERSION=3.11.11`.
- `GET /health` returns `{"status":"ok"}` and does not call OpenAI.
- CI: `.github/workflows/ci.yml` runs `uv run pytest` and `uv run python evals/run_deterministic_eval.py` on push to `main` and on pull requests. It does not set `OPENAI_API_KEY` and does not run live evals.
- **Ephemeral disk:** No persistent disk is attached. SQLite (`DATA_DIR/app.db`) and `uploads/` disappear on restart or redeploy. Session cookies and old `/recipes/{id}` links then 404. Startup still purges expired rows and deletes their files when the process is still the same disk. `uploads/`, `*.db`, and `.env` stay gitignored.
- **Import cap (in-memory, this process only):** 8 imports per client per hour and 30 per process per hour, checked before extraction. A restart clears the counters. Empty import submissions do not count. An OpenAI project budget or usage alert is a separate notice to configure in the OpenAI dashboard; this repo does not verify that it hard-stops spending. The key must stay restricted to `/v1/responses`.
- Unhandled errors return a generic HTML message. Logs redact `sk-` keys, image data URLs, and absolute home/tmp paths.

## Milestone 6 cooking conversation

- Each cooking step has quick actions, the current step’s conversation, a question field, and Send. Quick actions submit fixed questions through the same `POST /recipes/{id}/cook/{n}/guide` path as typed text. There are no separate hard-coded answers.
- One Responses API call per non-navigation question, via `OpenAIGuidanceClient` (`client.responses.create`, `store=false`, no tools). Model stays `gpt-4.1-mini`. Tests inject a mock client and do not call OpenAI.
- Context is the reviewed working recipe from `prepare_recipe()` / `apply_decisions()`: title, servings, current step, linked listed ingredients, listed ingredients, steps, creator notes, and a bounded recent transcript. Provenance is `source` or `user_edit`. Rejected findings, rejected additions, ignored boilerplate, evidence quotes, raw caption text, image paths, secrets, and cost fields are omitted.
- The model reply is stored as plain text and rendered escaped under the label **AI guidance**. It is not written into `payload_json`, review decisions, or the working recipe.
- Explicit phrases only (`next`, `next step`, `continue`, `back`, `go back`, `previous`, `previous step`, `repeat this step`, `start over`, `finish`, `finish cooking`) move the existing cursor and make no model call. Any other wording, including questions that contain those words, is a question.
- Messages live in SQLite `cook_messages`, keyed by recipe, owner session, and step. Another browser session cannot load the recipe, so it cannot read the history. Revisiting a step shows that step’s messages. The model also sees at most 12 current-step messages and, until the step already has two user turns, two messages from other steps. Storage keeps 24 messages per step and 80 per recipe. Rows cascade-delete when the recipe expires. The session cookie stores only the cooking cursor and one guide token.
- A separate in-memory limiter runs before the guidance client: 20 questions per session per hour and 60 per process per hour (`GUIDANCE_LIMIT_PER_SESSION`, `GUIDANCE_LIMIT_PER_PROCESS`, `GUIDANCE_LIMIT_WINDOW_SECONDS`). Navigation, empty input, oversized input, invalid quick actions, and duplicate replays are not charged. The import limiter is unchanged.
- Empty, oversized, invalid, rate-limited, unconfigured, and failed model requests keep the recipe, step, and earlier messages. Failures show a short recoverable error and do not store an assistant reply. User and model HTML is escaped.
- Known limits: history is ephemeral with the recipe; the process limiter resets on restart; the model can still give imperfect cooking advice; appearance is not treated as proof of food safety in the prompt, but the app cannot verify the model obeyed; this slice does not redesign Cooking Mode.

## Milestone 7 edits, preparation, substitution, and timer

Working recipe = immutable extraction + review decisions + direct edits in `recipe_edits`. `payload_json` is not updated. Rejected findings stay excluded. Edit, Prepare, Cooking Mode, and substitution grounding all read that working recipe.

- **Edit** is at `GET/POST /recipes/{id}/edit` after review is clear. Pending review returns 400 and does not write edits. Serving changes correct the label only and do not scale quantities. Added ids are `user_ing_N` and `user_step_N`. Removed source rows stay in the extraction and can be restored. Field provenance becomes `user_edit` only on fields that differ from the reviewed value.
- **Prepare** is `GET/POST /recipes/{id}/prepare`. Checklist rows live in `prep_checks` and do not change the recipe. Unchecked ingredients do not block `Start cooking`. The recipe page still links directly to Cooking Mode; Prepare is the primary action.
- **Substitution** reuses `OpenAIGuidanceClient.advise` and the guidance limiter. `GET /recipes/{id}/substitute/{ingredient_id}` shows source alternatives with no model call. `POST` the same path sends a grounded substitution context (`_purpose=substitution`). Replies are stored in `substitution_messages` and labeled **AI guidance**. They do not change the recipe. Replacing an ingredient is a normal edit, so the saved name is `user_edit`.
- **Timer** parses the current step in `timer.py`. One explicit duration shows **Start timer**, labeled from the source step. A range starts at the lower value and shows the full range. Several independent durations do not start a timer. Browser state is `localStorage` key `rca-timer:{recipe}:{step}`. Expiration does not advance the step and does not write recipe data.

Edit, checklist, and substitution rows cascade-delete with the recipe. Another session gets the existing 404.

## Milestone 8 serving scale

`GET/POST /recipes/{id}/scale` builds a session-scoped view in `scaled_views`. Twice the servings uses the current single count. Calculated amounts are labeled Calculated. Qualifiers, blank quantities, temperature, time, and pan size are not multiplied. `POST /scale/guide` asks for guidance only after a target is stored and reuses the guidance limiter. Confirm stores AI guidance or a user edit on that view only. Rollback deletes the view. Pending review and a range such as 4 to 6 do not calculate.

## Exact next recommended task

**Apple-design review of Cooking Mode.** Do not start it unless the user asks. Do not deploy, commit, or run a paid model call unless the user asks.

Optional later: targeted live re-run of `CONTRADICTION_QUANTITY` or Creamy Tomato after user approval.

## Security / privacy for the next agent

- Never read aloud, commit, or paste `.env`, API keys, or `SESSION_SECRET`
- Never commit `evals/local/`, `evals/results/`, `uploads/`, `*.db`
- Never ask the user to paste secrets into chat
- Never broaden OpenAI key scopes; stay on `/v1/responses`
