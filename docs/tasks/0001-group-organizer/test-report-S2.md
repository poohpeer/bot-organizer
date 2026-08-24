# Test report: S2 — AI layer (Gemini, fallback, tool-calling, cheap classifier)

**Design:** `docs/tasks/0001-group-organizer/stories/S2-ai-layer.md`
(requirements defined in `../EPIC.md`)
**Checked against:** branch `0001-S2-ai-layer`, commits `dabe8d7..5b1da69`
**Date:** 2026-08-24
**Verdict:** PASS (S2's share of R5/R6/R10 — see scope note)

## Scope note

S2 declares `Satisfies: R5, R6, R10`. Those requirements are stated
end-to-end ("the bot posts a suggestion", "the bot replies with a fixed
message"), and their outer halves belong to stories that don't exist yet
— the keyword pre-filter and suggestion posting are S7, the reply
plumbing and dedup/decision-log are S3/S9. What S2 owns is the model-side
half: the cheap classifier that decides *whether* a flagged message is a
real lead (R5), the tool-calling loop that lets the model compose tools
it was never hard-wired to combine (R6), and fail-safe degradation in the
AI layer (R10). This report verifies that share against the live Gemini
API, and marks the outer halves as deferred rather than passing them.

## Requirement checklist

**R5 (S2's share) — second-stage cheap model check:** PASS
- *"...then and only then does the bot run a second, low-cost model check
  to confirm it's really an organizing lead and not nostalgia or
  unrelated chat."* — verified by **running against the live Gemini API**
  with the exact discrimination cases from the original idea doc:
  - `"давно мы не были на пикнике, может соберёмся?"` → `True` (genuine lead)
  - `"давно мы не виделись с дедушкой, земля ему пухом"` → `False`
  - `"какая сегодня погода"` → `False`
  The grief case is the one the idea doc explicitly worried about ("может
  быть сказано с грустью про человека, которого больше нет") — the
  classifier correctly declines to treat it as an organizing opening.
- `extract()` (used by S9 for session start detection) also verified live:
  `"слушай, начинай следить, мы собираемся на пикник"` →
  `{'is_start': True, 'activity_type': 'пикник'}`.
- Deferred to S7: the keyword pre-filter, the "and only then" ordering,
  the 1/week + 30-day suppression limits.

**R6 — Composable fact memory:** PASS
- *"...the bot retrieves the stored fact and calls the relevant lookup
  tool itself, without a purpose-built 'check destination weather'
  function existing in the codebase."* — verified by **running the real
  tool loop against the live API** with a registry of three independent
  tools and the idea doc's own question, "will it be sunny where we're
  going?". The model chained, unprompted:
  `get_facts(session_id=1)` → `maps_lookup("Hanania meadow")` →
  `weather_lookup(lat=32.79, lon=35.05, date="2026-09-05")`,
  then answered "unlikely to be sunny... up to 80% chance of
  precipitation". No `check_destination_weather` function exists —
  confirmed: `grep -rn "destination_weather" bot/` returns nothing.
  Notably it composed *three* steps, working out on its own that it
  needed coordinates before it could ask for weather.
- *"Given no matching fact exists... the bot says it doesn't have that
  information rather than guessing."* — verified live with all tools
  returning empty/not-found: reply was *"I don't know where or when you
  are going, as I couldn't find any saved details or location facts for
  this session"* — no invented place, date, or forecast.

**R10 (S2's share) — degrade toward inaction:** PASS
- *"Given a Gemini call or tool-calling parse fails for any reason...
  performs no side-effecting action."* — S2's contract is to raise so the
  caller can apply the fixed message (S9 owns the reply text itself).
  Verified by `tests/test_tool_loop.py::test_run_tool_loop_reraises_when_no_model_left_to_fall_back_to`
  and `::test_run_tool_loop_raises_after_max_iterations`.
- The classifier helpers fail **closed** on every failure mode, each
  covered by a test that asserts `False`/`{}` rather than an exception:
  API error, `resp.text is None` (safety-blocked / text-less candidate),
  non-object JSON, and httpx transport errors
  (`tests/test_classify.py`, 8 tests).
- Deferred to S3/S4/S9: destructive-action confirmation, update dedup,
  decision-log writes.

## QA findings beyond the stated criteria

All seven genuine findings from `code-review:code-review` were fixed on
this branch before this report, each with a regression test (see commit
`5b1da69`). Two are worth recording here because they were confirmed
against live traffic, not just unit tests:

- **[Confirmed live — now fixed]** Mid-conversation model fallback. During
  the R6 no-fact run, a real `429` hit on turn 2 and the log shows
  `Tool loop turn failed (429), retrying on next model` followed by a
  successful completion. On the pre-fix code that request would have
  aborted with an `APIError`, and the user would have received the R10
  "didn't understand" fallback despite nothing being wrong with their
  message. This is exactly the case the reviewer predicted, observed in
  the wild within a handful of calls — later turns are when the shared
  key's quota is most likely exhausted.
- **[Corrected review claim]** The review asserted the whole cheap-filter
  layer was silently dead because Gemma supports neither system
  instructions, structured output, nor function calling. **Verified false
  against the live API**: `gemma-4-31b-it` accepted `system_instruction`,
  returned valid schema-conforming JSON (`{"result": true}`), and emitted
  a proper `function_call`. `CLASSIFIER_MODEL` was still moved off
  `MODELS[-1]` — but for the reason the evidence actually supports:
  Gemma was the slowest and least reliable model probed (repeated `504
  DEADLINE_EXCEEDED`, empty completions), and because these helpers fail
  closed, a flaky classifier degrades R5 into "never suggest anything"
  silently rather than erroring visibly.

Open, non-blocking:

- **[Minor]** `bot/ai/client.py:10` reads `os.environ["GEMINI_API_KEY"]`
  at import time, so importing `bot.ai.*` without the env var raises a
  bare `KeyError`. This is deliberate (matches the epic's crash-on-missing
  convention and the sibling project), but S9/S10 must ensure dotenv/secret
  loading happens before first import or the failure will be an opaque
  traceback rather than a clear message.
- **[Minor]** `ModelFallback.index` is process-global and never rolls
  back, by design. Once a transient 429 pushes it forward, every later
  request in that process stays on the weaker model until restart. Fine
  for v1 (matches the sibling project's proven behavior) but worth
  revisiting if quality drift is noticed in production.

## Verification methods used

Stronger than S1's report: the headline criteria (R5 classification, R6
composition and no-invention) were **run for real against the live Gemini
API** using the user's existing key, not mocked and not merely inspected.
The failure-mode criteria (R10 fail-closed paths, fallback exhaustion,
iteration cap) are covered by the 23-test suite
(`uv run pytest tests/ -q` → `23 passed`), where each behavior has a test
asserting the specific outcome, not just absence of a crash. Library-level
claims (`resp.text` returning `None`; `Part.from_function_response`
rejecting non-dicts; `APIError.code` being `None`) were confirmed against
the installed `google-genai` source and by direct execution, not assumed.
No criterion in this report is inspection-only.
