# Test report: S11 — Per-chat timezone

**Design:** added mid-epic at the user's request; requirement folded into R4/R11
**Checked against:** branch `0001-S11-per-chat-timezone`
**Date:** 2026-08-24
**Verdict:** PASS

Closes the debt carried since S3 and made visible by S8: naive wall-clock times
were read as UTC, so a UTC+3 group's "напомни в 9 утра" fired at 12:00 local.

## What was built

Telegram exposes no timezone at all — a message carries a UTC `date` and
nothing else — so the zone is resolved in layers, cheapest first:

| Layer | Source | Reliability |
|-------|--------|-------------|
| 1 | `DEFAULT_TIMEZONE` env var | always available |
| 2 | `chats.timezone` column | once anything better is known |
| 3 | Open-Meteo lookup at a resolved place's coordinates | whenever a place is resolved |
| 4 | `set_timezone` tool — a human says where they are | outranks all of the above |
| 5 | `reminder_set` reports `timezone_assumed` so the bot asks | when it actually matters |

**Layer 3 is called from code on place resolution, not from `weather_lookup`.**
Piggybacking on the weather tool was the original idea and was rejected on
inspection: `weather_lookup` only runs when the model decides weather is
relevant, needs a place to already be resolved, and never runs when a reminder
is set before a place is chosen — which is exactly when the zone is needed.

**The conversion happens once, at write time.** Everything stored is absolute
UTC, so the worker never deals with zones.

## Requirement checklist

- *A naive wall-clock time is read in the chat's zone* —
  `::test_a_naive_wall_clock_time_is_read_in_the_chats_zone`,
  `::test_reminder_set_does_not_ask_when_the_zone_is_known` (asserts the stored
  UTC instant, not just the return value).
- *An explicit offset is respected* — `::test_an_explicit_offset_is_respected_not_overridden`.
- *Only real IANA names are accepted* — `::test_only_real_iana_names_are_accepted`,
  `::test_set_chat_timezone_refuses_a_made_up_zone`,
  `::test_set_timezone_tool_refuses_a_made_up_zone`. The model will offer "MSK"
  or "UTC+3"; neither is a zone and both are refused rather than stored.
- *Learned from a resolved place* — `::test_timezone_is_learned_from_a_resolved_place`.
- *Never overwrites a human's statement* — `::test_learning_never_overwrites_what_a_human_said`
  (asserts the HTTP client is never even constructed).
- *A failed lookup doesn't break the caller* — `::test_a_failed_lookup_does_not_break_the_caller`.
- *A corrupt stored zone falls back rather than raising* —
  `::test_a_corrupt_stored_zone_falls_back_rather_than_raising`. Being an hour
  off beats the whole reminder path breaking.
- *The bot says when it had to assume* — `::test_reminder_set_says_when_it_had_to_assume_a_zone`.
- *Naming the zone corrects reminders already scheduled* —
  `::test_naming_the_timezone_corrects_reminders_already_scheduled`.
- *Correction is exact across a DST boundary* —
  `::test_correction_is_exact_across_a_dst_boundary`. Re-anchoring recomputes
  from the stored wall-clock time rather than shifting by an offset delta, so a
  reminder on the far side of a DST change still lands at the hour asked for.
- *An already-sent reminder is not moved* — `::test_an_already_sent_reminder_is_not_moved`.
- *"The day after the event" follows the chat's local day* —
  `::test_the_closing_question_follows_the_chats_local_day`, over four zones
  spanning 25 hours. `current_date` was the server's date, which asks a
  far-east chat a day late and a far-west chat a day early.

## Verification notes

The local-day test was **confirmed to fail against the previous
`current_date` logic** before being kept (`assert {1,3,5,7} <= {3,5,7}` — the
Kiritimati session was missing). An earlier attempt at that check failed for
the wrong reason (a type error in the sabotage expression, not the behavior)
and was redone properly rather than reported as proof.

Two facts checked live rather than assumed: Open-Meteo returns
`timezone`/`utc_offset_seconds` for a bare coordinate query (~190 bytes, no
key), and `zoneinfo` resolves zones inside the S10 deployment base image
(`ghcr.io/astral-sh/uv:python3.12-bookworm-slim` ships tzdata — 599 zones), so
no `tzdata` dependency is needed.

Suite: **181 passed**.

## Known limitation

The closing question fires as soon as the local date rolls over — i.e. around
local midnight. R4 only says "the day after", and that is now correct, but
messaging a group at 00:0x is poor manners. Worth adding a "not before 09:00
local" floor when convenient; it is a UX refinement, not a correctness bug.
