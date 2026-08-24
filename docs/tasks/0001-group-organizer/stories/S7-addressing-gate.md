# Story S7: Addressing gate — the only trigger for acting

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** One model-free function that answers "may the bot act on this
message?", so the decision to speak can never be made by a model, a prompt,
or a keyword list.
**Satisfies:** R5
**Depends on:** none (imports only `python-telegram-bot`)
**Parallel-safe with:** every other story
**Requirements & global constraints:** see `../EPIC.md`

> **Replaces the original S7 (proactive dormant-mode trigger).** That story
> shipped and was merged as PR #8, then removed here: the product decision is
> that the bot never decides on its own that help is wanted. R5 was rewritten
> accordingly and `bot/proactive.py` deleted. See the epic's R5 and the note
> in `progress.md`.

---

### Task 1: The gate

**Satisfies:** R5

**Files:**
- Create: `bot/addressing.py`
- Create: `tests/test_addressing.py`

**Interfaces:**
- Consumes: `telegram.MessageEntity`, `telegram.constants.ChatMemberStatus`.
- Produces:
  - `bot.addressing.addressed_to_bot(message, bot_id: int, bot_username: str) -> bool`
  - `bot.addressing.bot_was_added(chat_member_updated, bot_id: int) -> bool`

  Both consumed by S9's router. Neither touches the database or the model.

**Why this shape.** Three facts drive the implementation, each verified
against the Telegram documentation or the library rather than assumed:

1. **Privacy mode hides plain @mentions.** A bot with privacy mode enabled
   (the default) receives only slash commands aimed at it, replies to its own
   messages, and service messages — *not* an ordinary `@mention`. Since the
   epic forbids commands, privacy mode must be disabled in BotFather
   (`/setprivacy` -> Disable, then re-add the bot) or R5's trigger is
   unreachable and R1's silent capture impossible.
2. **An administrator bot receives every message regardless of that
   setting.** So privacy mode can never be the guarantee — promoting the bot
   would silently switch listening back on. The gate has to live in code.
3. **Entity offsets are UTF-16 code units.** Slicing `message.text` by
   Telegram's offset lands one character off for every non-BMP character
   (any emoji) earlier in the message. Use `parse_entity` /
   `parse_caption_entity`, which convert correctly. Verified: for
   `"🎉 @orgbot привет"` a naive slice yields `'orgbot '`, `parse_entity`
   yields `'@orgbot'`.

- [ ] **Step 1: Write the failing test** — create `tests/test_addressing.py`
  covering, at minimum: a plain mention; a mention mid-sentence; ordinary
  chatter; a message with no text at all; a reply to the bot; a reply to
  another person; `@orgbot_test` when the bot is `@orgbot` (prefix collision);
  a mention after one emoji and after several; case-insensitive matching; a
  `text_mention` of the bot and of someone else; a mention in a photo caption;
  and for `bot_was_added`: joining, being promoted, leaving, and another user
  joining.

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  uv run pytest tests/test_addressing.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'bot.addressing'`.

- [ ] **Step 3: Implement `bot/addressing.py`** — see the module on this
  branch. Note the two parser sources: `message.parse_entity` raises
  `RuntimeError: This Message has no 'text'` on a caption-only message, so
  caption entities must go through `parse_caption_entity`.

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  uv run pytest tests/test_addressing.py -v
  ```
  Expected: `17 passed`. No database needed — this story touches none.

- [ ] **Step 5: Commit**

---

## Implementation notes (added during S7)

Written in the parent session rather than dispatched to a subagent: the unit
is one small module and the design work had just been done in conversation.

Two defects were caught by the tests before the code was committed:

1. **`parse_entity` raises on caption-only messages.** A photo captioned
   "@orgbot вот это место" would have crashed the handler instead of being
   answered. Text and caption entities now go through their respective
   parsers.
2. **The UTF-16 offset problem is real, not theoretical.** Demonstrated
   directly: `"🎉 @orgbot погнали"` sliced naively gives `'orgbot '`, which
   would never match `@orgbot`. Locked in by two tests (one emoji, three
   emoji).

The prefix-collision case (`@orgbot_test` vs `@orgbot`) is why matching goes
through entities at all rather than `"@orgbot" in text`.
