# Test report: S7 — Addressing gate (replaces the proactive trigger)

**Design:** `docs/tasks/0001-group-organizer/stories/S7-addressing-gate.md`
(requirement R5 in `../EPIC.md`, rewritten for this story)
**Checked against:** branch `0001-S7-addressing-gate`
**Date:** 2026-08-24
**Verdict:** PASS

This story **replaces** the original S7 (proactive dormant-mode trigger,
merged as PR #8). The product decision changed: the bot must never decide on
its own that help is wanted. R5 was rewritten, `bot/proactive.py` and its 16
tests were deleted, and the now-unused `proactive_suggestions` table was
dropped from the schema.

## Requirement checklist

**R5 — Explicit addressing is the only trigger:** PASS

- *"...when it receives the membership update, then it posts one short
  message saying how to call it and does not start a session"* — the gate
  half is covered by `::test_bot_joining_the_chat_is_detected`; the greeting
  and the "no session" half are S9's router (its brief now specifies both).
- *"...when it is promoted, demoted or its permissions change, then it says
  nothing"* — `::test_bot_being_promoted_is_not_a_join`. `bot_was_added`
  compares old and new status rather than reading the new one, because a
  promotion arrives as the same `my_chat_member` update and would otherwise
  re-greet the chat. `::test_bot_leaving_is_not_a_join` and
  `::test_another_user_joining_is_not_the_bot` cover the other two shapes.
- *"...when a message arrives that does not address the bot, then the bot
  neither replies nor calls the model at all"* —
  `::test_ordinary_chatter_is_not_addressed`,
  `::test_message_without_text_is_not_addressed`,
  `::test_reply_to_another_person_is_not_addressed`. The "no model call"
  half is enforced structurally: `bot/addressing.py` imports nothing from
  `bot.ai`, so no model call is reachable from this path.
- *"...when someone @mentions the bot by its exact username or replies to one
  of the bot's own messages, then a session starts"* — the gate is covered by
  `::test_plain_mention_is_addressed`,
  `::test_mention_in_the_middle_is_addressed`,
  `::test_username_match_is_case_insensitive`,
  `::test_reply_to_the_bot_is_addressed`,
  `::test_text_mention_of_the_bot_is_addressed`, and
  `::test_mention_in_a_photo_caption_is_addressed`. Starting the session is
  S9's.
- *"...a different bot whose username merely starts with this bot's username
  (`@orgbot_test` vs `@orgbot`) does not count"* —
  `::test_a_similarly_named_bot_is_not_us`. This is why matching goes through
  entities rather than `"@orgbot" in text`.
- *"...with emoji before the mention, the mention is still recognised —
  entity offsets are UTF-16"* —
  `::test_mention_after_an_emoji_is_addressed` and
  `::test_mention_after_several_emoji_is_addressed`.

**R5 (listening vs speaking)** — *"an active session records facts from
unaddressed messages but stays silent"* — belongs to S9's router; its brief
now specifies the split and the tests for it. Not verifiable in this story,
which delivers only the gate.

## Defects found during verification

Both were caught by the tests before the code was committed, so neither
reached a commit.

1. **[High] `parse_entity` raises on a caption-only message.**
   `RuntimeError: This Message has no 'text'` — a photo captioned
   "@orgbot вот это место" would have crashed the handler rather than being
   answered. Text and caption entities now go through `parse_entity` and
   `parse_caption_entity` respectively.
2. **[High] The UTF-16 offset problem is real, not theoretical.**
   Demonstrated directly before writing the module: for `"🎉 @orgbot привет"`,
   Telegram's offset is 3 while Python's index is 2, so a naive slice yields
   `'orgbot '` and never matches `@orgbot`. One emoji anywhere earlier in a
   message would have silently disabled the bot for that message.

## QA findings beyond the stated criteria

- **[Critical for deployment — no code fix possible] Privacy mode must be
  disabled in BotFather.** Verified against Telegram's own documentation:
  a bot with privacy mode enabled (**the default**) receives only slash
  commands aimed at it, replies to its own messages, and service messages —
  **a plain `@mention` never arrives**. Since the epic forbids commands, the
  bot would be completely unreachable out of the box. `/setprivacy` ->
  Disable, then re-add the bot to the group. Recorded as a global constraint
  and belongs in S10's deployment docs.
- **[Info] An administrator bot receives every message regardless of the
  privacy setting.** So privacy mode can never *be* the guarantee — promoting
  the bot would silently switch listening back on. This is the reason the
  gate lives in code rather than relying on the platform.
- **[Important for S9] `bot_username` must come from `bot.get_me()` at
  startup, never a hardcoded constant or env var.** If the bot is renamed,
  a stale username makes every mention stop matching — the bot goes
  permanently silent with no error in the logs, which is the hardest possible
  failure to diagnose. Flagged for S9's `bot/main.py` task.
- **[Info] Messages from other bots.** The gate answers only "was this
  addressed to me". S9 should additionally ignore messages whose sender is a
  bot, or two bots mentioning each other could loop.

## Verification methods used

No database and no model — this story touches neither. All 17 tests build
real `python-telegram-bot` objects (`Message`, `MessageEntity`,
`ChatMemberUpdated`, the `ChatMember*` status classes) and exercise the real
library parsers, so the UTF-16 behavior tested is the library's own, not a
reimplementation. The two Telegram platform facts above were checked against
`core.telegram.org` documentation rather than recalled. Suite: **148 passed**
(was 147; −16 proactive, +17 addressing).
