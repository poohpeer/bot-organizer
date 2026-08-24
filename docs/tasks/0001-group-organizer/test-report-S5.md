# Test report: S5 — External grounding tools (web search, maps, weather)

**Design:** `docs/tasks/0001-group-organizer/stories/S5-external-tools.md`
**Checked against:** branch `0001-S5-external-tools`
**Date:** 2026-08-24
**Verdict:** PASS — after fixing 6 defects; one path is mock-only (see limitations)

## Requirement checklist

**R6/R7 (S5's share) — grounded lookups:** PASS
- `weather_lookup` verified **live against the real Open-Meteo API** (keyless):
  a real forecast for 2026-08-27 at 32.79,35.05 returned
  `weather_code=2, temp_max_c=31.4, precipitation_probability_max=0`.
- `maps_lookup` covered by 12 mocked tests — **not verified live** (no Google
  Maps API key available in this environment). See limitations.

**R7/R10 — "says it couldn't find anything rather than fabricating":** PASS
This is S5's whole reason to exist, and it is verified rather than assumed:
- **Live, under genuine failure:** an out-of-forecast-range date (2027-09-28)
  and nonsense coordinates (lat/lon 999) both produced real HTTP 400s from
  Open-Meteo and returned `{"found": False}` — no exception, no invented
  forecast.
- **Live, under genuine quota exhaustion:** while verifying `web_search`, the
  Gemini free-tier quota ran out mid-run (429 RESOURCE_EXHAUSTED). The tool
  returned `{"found": False, "summary": None, "sources": []}` rather than
  raising or falling through to the model's own stale knowledge — exactly the
  contract, confirmed by accident under the most realistic possible condition.
- Mocked coverage for every remaining failure mode: non-200, malformed JSON,
  `ConnectError`, `ReadTimeout`, missing fields, short parallel arrays.

## Defects found during verification (all fixed)

1. **[Medium] `web_search` had no model fallback.** Pinned `MODELS[0]`.
   **Observed live**: once the shared key hit its quota, every search reported
   "couldn't find anything" while the rest of the bot degraded fine to a later
   model. Now walks `SEARCH_MODELS` — the Gemini-only subset, since Gemma has
   no Google Search tool.
2. **[Medium] No timeout on the grounded-search call**, unlike maps/weather. A
   hung search would block the tool loop and the user's Telegram reply
   indefinitely.
3. **[Medium] `maps_lookup` could return `name=None`** — a cross-story defect:
   S6 inserts that into `places.name` (`TEXT NOT NULL`), so the insert would
   raise, and `None` also breaks S6's `lower(name)` cache lookup. Now falls
   back to `formattedAddress`, else not-found.
4. **[Low] `web_search` caught only `APIError`**, so transport failures escaped
   the documented "any failure → found=False" contract.
5. **[Low] `weather_lookup` reported an all-null row as found** — Open-Meteo
   returns nulls past the forecast horizon, so `temp_max_c=None` was being
   presented as a real forecast.
6. **[Low] Per-call `httpx.AsyncClient`** contradicted the epic's "one shared
   client" constraint; now lazily created and reused, with `aclose()`.

## Limitations of this report

- **`maps_lookup` is not live-verified.** No `GOOGLE_MAPS_API_KEY` is
  available here, so its 12 tests all mock the HTTP layer. The response
  shape is taken from the Places API v1 field mask; the first real call
  should be treated as unverified. This is the weakest evidence in the epic
  so far and matters because R7 (place-checking) leans on it.
- **`web_search`'s success path is not live-verified** — the Gemini quota was
  exhausted by earlier verification runs. Its *failure* path is verified live
  (above); the grounded-success path is mocked only.

## Verification methods used

`weather_lookup`: run for real against the live API, including two genuine
error responses. `web_search`: failure path verified live under real quota
exhaustion, success path mocked. `maps_lookup`: mocked only. Suite:
**109 passed** (82 baseline + 27 in this module, of which 9 are regressions
for the defects above).
