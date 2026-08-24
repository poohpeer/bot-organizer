# Test report: S6 — Composed flows (place-check, "as usual" ranking, venue cards)

**Design:** `docs/tasks/0001-group-organizer/stories/S6-composed-flows.md`
(requirements in `../EPIC.md`)
**Checked against:** branch `0001-S6-composed-flows`, commits `7a0813b..HEAD`
**Date:** 2026-08-24
**Verdict:** PASS — after fixing 6 defects found during verification

This is the first story verified against the **live Google Places API** — S5
could only mock it. That closed the weakest evidence gap carried so far.

## Requirement checklist

**R7 — Grounded place-check tied to session context:** PASS (S6's share)
- *"...when someone asks the bot to check a named place, then the bot looks it
  up via maps/search"* — verified **live**: `maps_lookup("Sarona Market Tel
  Aviv")` returned `Sarona Market`, `Aluf Kalman Magen St 3, Tel Aviv-Yafo,
  Israel`, `32.0712912/34.7871708`, rating `4.3`.
- *"...answers against those specific constraints (…rain forecast for
  Saturday)"* — the composition R7 actually needs was run end-to-end:
  the coordinates `maps_lookup` returned were fed straight into
  `weather_lookup`, which answered for a real future date
  (`{'found': True, 'weather_code': 2, 'temp_max_c': 31.8, 'temp_min_c': 25.0,
  'precipitation_probability_max': 0}`). Answering *against* those facts is the
  model's job at the S9 wiring layer; the grounding they depend on works.
- *"...given a place that returns no usable results, then the bot says it
  couldn't find anything rather than fabricating details"* — verified **live**
  with a genuine nonsense query: `{'found': False}`, no fabricated fields.
- **Caveat, see QA findings:** review snippets are always empty with this key,
  so constraint-checking must lean on `web_search`.

**R8 — "As usual" recency-weighted archive lookup:** PASS (after fixes)
- *"...one place clearly dominates when weighted by recency … names that place
  along with the evidence (visit count and how recent)"* —
  `::test_archive_lookup_dominant_place_wins_on_recency`; the returned entry
  carries `visit_count` and `last_visited`, which is the evidence the criterion
  asks for.
- *"...two or more places roughly tied … lists the top options and asks which
  one"* — `::test_archive_lookup_tied_shows_top_options`, plus
  `::test_archive_lookup_tied_never_returns_a_single_option`, which locks in
  Defect 6: "tied" used to be reportable with one option.
- *"...every past place visited about once, no meaningful pattern … lists every
  place"* — `::test_archive_lookup_no_pattern_when_every_place_visited_once`.
- *"...recency measurably affects the ranking — a stale-but-frequent place does
  not automatically outrank a fresh-but-rarer one"* — the dominance test is
  exactly this shape: 5 visits two years ago (score 0.30) lose to 1 visit ten
  days ago (0.96).

**R9 — Native location card, captured once:** PASS (after fixes)
- *"...when it's resolved via maps, its name and coordinates are stored once
  against that session/place"* — verified **live** end-to-end: the first
  resolve inserted one row; a second resolve of the same phrasing returned
  `cached=True` **without** calling the API; `places` still held exactly 1 row.
  Before the fix this same sequence produced 2 API calls and 2 rows.
- *"...sends a native Telegram venue message …, not a plain-text address, and
  does not re-query maps for coordinates it already has"* — `send_venue` was
  called with the real resolved coordinates
  (`{'chat_id': 500, 'latitude': 32.0712912, 'longitude': 34.7871708,
  'title': 'Sarona Market', 'address': 'Aluf Kalman Magen St 3, …'}`) and no
  maps call was made. Unit coverage in
  `::test_send_location_sends_venue_for_saved_place` and
  `::test_send_location_finds_the_place_by_the_original_phrasing`.

## Defects found during verification (all fixed on this branch)

Each was **reproduced before being fixed** — against a real Postgres, and for
the caching defects against the live Places API. The implementation matched the
brief verbatim, so all six were defects in the design, not transcription
errors. Full rationale in the story's "Implementation notes" section.

1. **[High — broke R9] Cache matched only the canonical maps name.** Resolving
   "поляна Ханания" twice called maps twice and wrote two `places` rows. Added
   `places.query` + a per-session unique index on the name.
2. **[High] `send_location` took a model-supplied `chat_id`** — the same
   cross-chat write hole S4 closed on `reminder_set`/`broadcast_message`.
   Derived from the session; removed from the declaration.
3. **[High] `archive_lookup` took a model-supplied `chat_id`** — the read-side
   version: a wrong id surfaces another group's history. Now takes `session_id`.
4. **[Medium] `NULL` visit date crashed the scorer** with `TypeError` on a
   closed session having neither `event_date` nor `closed_at`. Added
   `started_at` as the final `COALESCE` fallback.
5. **[Medium — broke R8] A future `event_date` outscored real history.** An
   abandoned future-dated plan scored 4.08 against 2.83 for a place actually
   visited three times in the last month. Age clamped at 0.
6. **[Medium — broke R8] `pattern: "tied"` could return one place.** The 2×
   dominance cut and the 0.8 tie cut left a dead band, so the bot was told to
   ask which one while holding a single option. Both now use the same ratio.

## QA findings beyond the stated criteria

- **[Limitation — Medium, no code change] `review_snippets` is always empty.**
  Confirmed live: Sarona Market has `userRatingCount: 34149`, yet both
  `places:searchText` and Place Details return zero `reviews` (HTTP 200, field
  omitted — it requires the Enterprise + Atmosphere SKU). R7's
  "no official grilling allowed" style grounding must come from `web_search`.
  The field degrades to `[]` rather than failing, and would light up if the SKU
  is enabled, so nothing was changed in code.
- **[Info] The unique index assumes a clean `places` table.**
  `one_place_name_per_session` is created via `CREATE UNIQUE INDEX IF NOT
  EXISTS` in `_SCHEMA_SQL`. On a database that already contains duplicate
  (session_id, lower(name)) rows, that statement fails and `init_db` raises.
  Harmless here — no such database exists yet — but a deployment that
  predates this commit would need the duplicates cleared first. Worth
  remembering at S10.
- **[Deferred — Medium] Per-chat timezone, still open.** `archive_lookup` uses
  `date.today()` on the server's clock to compute visit ages. At a 180-day
  half-life a few hours of skew is immaterial, so this does not affect S6's
  correctness — but it is the same missing per-chat timezone that S3's
  `event_date` and S4's `reminder_set` are waiting on, where it *does* matter.
- **[Info] Tie/dominance thresholds are now a single knob.** `_DOMINANCE_RATIO`
  governs both cuts, so the two can no longer drift apart into a dead band.

## Verification methods used

R7 and R9 were exercised **against the live Google Places API and the live
Open-Meteo API**, driving the real composed flow against a real Postgres —
including the not-found path, the cache-hit path, and the venue payload.
R8 is covered by unit tests against a real database (no mocked DB anywhere;
maps and Telegram are the only mocked boundaries). All 6 defects were
reproduced with concrete failing output before being fixed and are locked in by
regression tests. `test_declared_parameters_match_the_bound_signatures` pins
every composed tool's declaration to its bound signature, and was itself
verified by re-introducing Defect 2 and confirming the test fails.
Suite: **131 passed**.
