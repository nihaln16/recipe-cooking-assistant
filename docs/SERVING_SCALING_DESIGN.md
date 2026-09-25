# Serving scaling design

The scaled view is implemented at `/recipes/{id}/scale`. It does not change the working recipe.

## Product distinction

**Correct serving label** changes an incorrect serving count on the working recipe. Changing `Servings: 3` to `Servings: 4` does not change ingredient quantities. That correction already exists as a direct edit.

**Scale recipe** is a later action. The cook chooses a target count, and the app derives a separate scaled cooking view. The reviewed working recipe stays as saved.

## Architecture

```text
Immutable extraction
        ↓
Review decisions + direct user edits
        ↓
Working recipe
        ↓
Deterministic serving calculations
        ↓
Optional AI scaling guidance
        ↓
User confirmation
        ↓
Scaled cooking view
```

The scaled view is read from the working recipe plus a saved target count. It is not written back into `extracted_recipes.payload_json`, review decisions, or direct edits.

## Deterministic behavior

Scaling parses the working serving label as a positive number. If the label is missing or not a single count, the app does not calculate and asks the cook to correct the label first.

- **Simple numeric quantities.** Multiply by `target / current` and keep the unit.
- **Fractions and mixed numbers.** Parse `1/2`, `1½`, and `1 1/2`. Show a simplified fraction when it stays readable, otherwise a decimal rounded to a cooking-friendly precision.
- **Counts.** Scale counts the same way, then round to a practical whole number when the ingredient is a discrete item (cloves, eggs, cans). Show the unrounded value beside an awkward result.
- **Package counts and package sizes.** Scale the count of packages when the size is fixed (`2 cans`, 15 oz each). Do not scale the labeled package size. If only a package size is present, leave it and flag it for guidance.
- **Alternative groups.** Scale each option independently. Keep the “choose one” grouping. Do not add the options together.
- **Optional ingredients.** Scale them, and keep them optional.
- **Missing quantities.** Leave the quantity blank. Do not invent one.
- **`to taste`, `as needed`, and garnish qualifiers.** Do not scale. Keep the qualifier.
- **Awkward fractional results.** Keep the calculated value visible and mark it calculated. Do not silently replace it with a “nice” number.
- **Temperature.** Do not scale.
- **Cooking time.** Do not scale automatically. A range stays a range.
- **Pan size.** Do not scale automatically.
- **Ambiguous values.** If a quantity, unit, or serving label cannot be parsed, leave that field unscaled and say why.

## AI-guidance behavior

**Get scaling guidance** runs only after the cook chooses a target serving count.

The request receives the working recipe and the deterministic calculations. It may help with spices, salt, acid, sweeteners, leavening, thickeners, batch size, cookware, timing, and awkward quantities.

It must not:

- claim the guidance came from the source
- invent an original missing quantity
- automatically double temperature or cooking time
- write the scaled view by itself

The reply is structured proposals. Each proposal names the ingredient or step, the deterministic value, the suggested change, and why. Applying one requires confirmation. A proposal the cook then edits becomes a `user_edit` on the scaled view only.

Cache a successful response for the recipe id plus target count. A change to the working recipe invalidates that cache. Reuse the existing guidance limiter. Empty, invalid, and rate-limited requests make no model call. Use the Responses API only. Do not add a model.

## Provenance

| Label | Meaning |
| --- | --- |
| Source | Unchanged source-backed field on the working recipe |
| User edit | A direct edit, or a cook’s change to a scaling proposal |
| Calculated | A deterministic scaled quantity or count |
| AI guidance | A model proposal that has not been edited by the cook |

Accepted model-generated text stays `ai_suggestion` until the cook changes it. Deterministic values stay visibly calculated. The original extraction and the reviewed working recipe remain recoverable. Scaling never mutates either one.

## Step presentation

Do not replace numbers inside direction text.

```text
Source step:
Add 2 cloves garlic and cook for 30 seconds.

For 6 servings:
Use 4 cloves garlic.

AI scaling guidance:
Keep the same temperature. A larger batch may take longer to become fragrant.
```

The scaled cooking view shows the source step, then calculated ingredient lines, then any confirmed guidance under **AI guidance**.

## Routes, persistence, and confirmation

Routes:

- `GET /recipes/{id}/scale` shows the current label, a target-count field, and calculated preview
- `POST /recipes/{id}/scale` with `target` stores the target and calculations for this session
- `POST /recipes/{id}/scale/guide` runs **Get scaling guidance** for that stored target
- `POST /recipes/{id}/scale/confirm` writes confirmed proposals onto the scaled view
- `POST /recipes/{id}/scale/rollback` drops the scaled view for that target

Persist the scaled view in a session-scoped table that cascade-deletes with the recipe. Another session receives the existing 404. Rollback deletes only the scaled view.

## Tests and live evaluation

Deterministic tests should cover each quantity class above, alternative groups, optional ingredients, blank quantities, unchanged temperature and time, and confirmation that the extraction payload and working recipe are unchanged.

A later live evaluation, only with approval, would be one Responses call on a fixture recipe at a new target count. It would check that the reply is labeled guidance, does not invent a missing quantity, and does not change temperature. Do not run that call as part of this design.

## Known risks

- Serving labels such as “serves 4 to 6” are not a single current count.
- Discrete rounding can disagree with a cook’s judgment. The calculated value has to stay visible.
- Package wording is easy to scale twice if both count and size are multiplied.
- Cached guidance goes stale if the working recipe changes after the call.
- The model can still give poor batch advice. The app can require confirmation; it cannot prove the advice is right.
