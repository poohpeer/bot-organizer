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

## Quiet hours (added after the first review of this story)

Getting the local day right had a side effect: the closing question became due
the moment the date rolled over, so the bot would open a conversation at about
00:05 local. `QUIET_UNTIL_HOUR` (9) and `QUIET_FROM_HOUR` (21) now gate both
the closing question and the auto-close notice, evaluated in the chat's own
zone. This **delays rather than skips** — the worker polls every minute, so a
question that comes due overnight goes out at the start of the window.

Tested by putting a chat in whichever IANA zone is currently at the hour under
test, rather than by faking the clock, so these stay real queries against a
real database:
`::test_the_bot_does_not_start_a_conversation_at_local_midnight`,
`::test_the_bot_does_not_start_a_conversation_late_at_night`,
`::test_the_question_goes_out_once_the_group_is_awake`.

Both "stay silent" tests were confirmed to fail with the window removed
(`bot would have posted at 00:00 in Europe/Minsk`,
`bot would have posted at 22:00 in Etc/GMT-1`).

An autouse fixture in `conftest.py` widens the window to 0–24 for the rest of
the suite: those tests are about *whether* a session is due, and without it
every one of them would pass or fail depending on the wall clock when the suite
happened to run.

Suite after this addition: **184 passed**.
