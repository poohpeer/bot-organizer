# Story S7: Proactive dormant-mode trigger

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** Let a dormant chat notice an organizing opening on its own —
cheaply, rarely, and easy to ignore — without ever running the full
active-mode pipeline on ordinary chatter.
**Satisfies:** R5
**Depends on:** S1, S2, S3 (`bot.proactive` calls `bot.decision_log.log_decision`)
**Parallel-safe with:** S6, S8
**Requirements & global constraints:** see `../EPIC.md`

---

### Task 1: Keyword filter + suggestion flow

**Satisfies:** R5

**Files:**
- Create: `bot/proactive.py`
- Create: `tests/test_proactive.py`

**Interfaces:**
- Consumes: `bot.ai.classify.classify` (S2), `bot.decision_log.log_decision`
  (S3), `proactive_suggestions` table (S1).
- Produces: `bot.proactive.match_topic(text: str) -> str | None`,
  `async bot.proactive.maybe_suggest(pool, telegram_bot, chat_id, text) -> dict` —
  consumed by S9 for every message in a dormant chat.

- [ ] **Step 1: Write the failing test** — create `tests/test_proactive.py`:
  ```python
  from unittest.mock import AsyncMock

  import bot.proactive as proactive


  def test_match_topic_finds_a_keyword_category():
      assert proactive.match_topic("давно мы не были на пикнике") == "havent_in_a_while"
      assert proactive.match_topic("может, на выходных махнём куда-то") == "should_go_somewhere"
      assert proactive.match_topic("какая сегодня погода") is None


  async def test_maybe_suggest_skips_when_no_keyword_match(db_pool):
      result = await proactive.maybe_suggest(db_pool, AsyncMock(), chat_id=1, text="какая сегодня погода")

      assert result == {"suggested": False, "reason": "no_keyword_match"}


  async def test_maybe_suggest_posts_when_classifier_confirms(monkeypatch, db_pool):
      monkeypatch.setattr(proactive, "classify", AsyncMock(return_value=True))
      telegram_bot = AsyncMock()

      result = await proactive.maybe_suggest(
          db_pool, telegram_bot, chat_id=1, text="давно мы не собирались все вместе"
      )

      assert result["suggested"] is True
      telegram_bot.send_message.assert_awaited_once()
      row = await db_pool.fetchrow("SELECT * FROM proactive_suggestions WHERE id = $1", result["suggestion_id"])
      assert row["topic_key"] == "havent_in_a_while"
      assert row["response"] is None


  async def test_maybe_suggest_stays_silent_when_classifier_says_not_a_lead(monkeypatch, db_pool):
      monkeypatch.setattr(proactive, "classify", AsyncMock(return_value=False))
      telegram_bot = AsyncMock()

      result = await proactive.maybe_suggest(
          db_pool, telegram_bot, chat_id=1, text="давно мы не виделись с дядей Колей, земля ему пухом"
      )

      assert result == {"suggested": False, "reason": "not_a_lead"}
      telegram_bot.send_message.assert_not_awaited()


  async def test_maybe_suggest_rate_limited_within_seven_days(monkeypatch, db_pool):
      monkeypatch.setattr(proactive, "classify", AsyncMock(return_value=True))
      await db_pool.execute(
          "INSERT INTO proactive_suggestions (chat_id, topic_key) VALUES (1, 'lets_go')"
      )

      result = await proactive.maybe_suggest(
          db_pool, AsyncMock(), chat_id=1, text="давно мы не собирались"
      )

      assert result == {"suggested": False, "reason": "rate_limited"}


  async def test_maybe_suggest_topic_suppressed_after_decline(monkeypatch, db_pool):
      monkeypatch.setattr(proactive, "classify", AsyncMock(return_value=True))
      await db_pool.execute(
          """
          INSERT INTO proactive_suggestions (chat_id, topic_key, suggested_at, response)
          VALUES (1, 'havent_in_a_while', now() - interval '10 days', 'declined')
          """
      )

      result = await proactive.maybe_suggest(
          db_pool, AsyncMock(), chat_id=1, text="давно мы не собирались все вместе"
      )

      assert result == {"suggested": False, "reason": "topic_suppressed"}
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_proactive.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'bot.proactive'`.

- [ ] **Step 3: Implement `bot/proactive.py`**
  ```python
  import re

  import bot.decision_log as decision_log
  from bot.ai.classify import classify

  # Category -> patterns. The category itself becomes the suppression
  # topic_key (R5): a decline on one category doesn't suppress the others.
  KEYWORD_TOPICS = {
      "havent_in_a_while": [r"давно мы не", r"давно не (были|ездили|собирались|виделись)"],
      "should_go_somewhere": [
          r"надо бы .*(съездить|сходить|выбраться)",
          r"может.*(выходных|выходные).*(махн|съезд)",
          r"куда-нибудь.*съездить",
      ],
      "lets_go": [r"\bпоехали\b", r"\bмахнём\b", r"\bмахнем\b"],
  }

  _SUGGESTION_TEXT = "О, хотите, помогу организовать?"

  _CLASSIFY_INSTRUCTION = (
      "The message below was flagged by a cheap keyword filter as possibly "
      "about organizing a group event (a trip, outing, or gathering). "
      "Answer true only if it's a genuine, present-tense opening to organize "
      "something together now. Answer false for nostalgia, grief, an "
      "unrelated conversation that happens to share the words, or a heated "
      "argument — anything where a cheerful 'want help organizing?' would "
      "land badly."
  )


  def match_topic(text: str) -> str | None:
      lowered = text.lower()
      for topic, patterns in KEYWORD_TOPICS.items():
          if any(re.search(p, lowered) for p in patterns):
              return topic
      return None


  async def maybe_suggest(pool, telegram_bot, chat_id: int, text: str) -> dict:
      topic_key = match_topic(text)
      if topic_key is None:
          return {"suggested": False, "reason": "no_keyword_match"}

      rate_limited = await pool.fetchval(
          """
          SELECT EXISTS (
              SELECT 1 FROM proactive_suggestions
              WHERE chat_id = $1 AND suggested_at > now() - interval '7 days'
          )
          """,
          chat_id,
      )
      if rate_limited:
          return {"suggested": False, "reason": "rate_limited"}

      suppressed = await pool.fetchval(
          """
          SELECT EXISTS (
              SELECT 1 FROM proactive_suggestions
              WHERE chat_id = $1 AND topic_key = $2
                AND suggested_at > now() - interval '30 days'
                AND response IS DISTINCT FROM 'accepted'
          )
          """,
          chat_id, topic_key,
      )
      if suppressed:
          return {"suggested": False, "reason": "topic_suppressed"}

      is_lead = await classify(_CLASSIFY_INSTRUCTION, text)
      await decision_log.log_decision(
          pool, chat_id=chat_id, user_id=None, raw_text=text, stage="proactive_filter",
          decision={"topic_key": topic_key, "result": "lead" if is_lead else "not_a_lead"},
      )
      if not is_lead:
          return {"suggested": False, "reason": "not_a_lead"}

      row = await pool.fetchrow(
          "INSERT INTO proactive_suggestions (chat_id, topic_key) VALUES ($1, $2) RETURNING id",
          chat_id, topic_key,
      )
      await telegram_bot.send_message(chat_id=chat_id, text=_SUGGESTION_TEXT)
      return {"suggested": True, "suggestion_id": row["id"]}
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_proactive.py -v
  ```
  Expected: `6 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/proactive.py tests/test_proactive.py
  git commit -m "Add proactive dormant-mode keyword filter and suggestion flow"
  ```

---

### Task 2: Suggestion response resolution

**Satisfies:** R5

**Files:**
- Modify: `bot/proactive.py`
- Modify: `tests/test_proactive.py`

**Interfaces:**
- Consumes: `proactive_suggestions` table (S1), Task 1.
- Produces: `async bot.proactive.get_pending_suggestion(pool, chat_id) -> asyncpg.Record | None`,
  `async bot.proactive.resolve_suggestion(pool, suggestion_id, response: str) -> None` —
  both consumed by S9 to detect and record a reply to an outstanding
  suggestion ("давай" -> accepted, a decline/ignore -> declined).

- [ ] **Step 1: Write the failing test** — append to `tests/test_proactive.py`:
  ```python
  async def test_get_pending_suggestion_returns_unresolved_one(db_pool):
      row = await db_pool.fetchrow(
          "INSERT INTO proactive_suggestions (chat_id, topic_key) VALUES (1, 'lets_go') RETURNING id"
      )

      found = await proactive.get_pending_suggestion(db_pool, chat_id=1)

      assert found["id"] == row["id"]


  async def test_get_pending_suggestion_none_when_already_resolved(db_pool):
      await db_pool.execute(
          "INSERT INTO proactive_suggestions (chat_id, topic_key, response) VALUES (1, 'lets_go', 'accepted')"
      )

      assert await proactive.get_pending_suggestion(db_pool, chat_id=1) is None


  async def test_resolve_suggestion_sets_response(db_pool):
      row = await db_pool.fetchrow(
          "INSERT INTO proactive_suggestions (chat_id, topic_key) VALUES (1, 'lets_go') RETURNING id"
      )

      await proactive.resolve_suggestion(db_pool, row["id"], "declined")

      updated = await db_pool.fetchrow("SELECT response FROM proactive_suggestions WHERE id = $1", row["id"])
      assert updated["response"] == "declined"
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_proactive.py -v
  ```
  Expected: `AttributeError: module 'bot.proactive' has no attribute 'get_pending_suggestion'`.

- [ ] **Step 3: Append to `bot/proactive.py`**
  ```python
  async def get_pending_suggestion(pool, chat_id: int):
      return await pool.fetchrow(
          """
          SELECT * FROM proactive_suggestions
          WHERE chat_id = $1 AND response IS NULL AND suggested_at > now() - interval '2 days'
          ORDER BY suggested_at DESC LIMIT 1
          """,
          chat_id,
      )


  async def resolve_suggestion(pool, suggestion_id: int, response: str) -> None:
      await pool.execute(
          "UPDATE proactive_suggestions SET response = $2 WHERE id = $1",
          suggestion_id, response,
      )
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_proactive.py -v
  ```
  Expected: `9 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/proactive.py tests/test_proactive.py
  git commit -m "Add proactive suggestion response resolution (accept/decline)"
  ```

---

## Implementation notes (added during S7, after code review)

The brief was transcribed faithfully and its 9 tests passed on the first pass.
Verification against a real database then found three defects in the design
itself, each reproduced before it was fixed.

1. **[High — broke R5] The weekly rate limit was not actually enforced.**
   `maybe_suggest` read the 7-day window, then inserted in a separate
   statement. Two messages arriving together both saw an empty window and both
   suggested. Reproduced with `asyncio.gather` on two candidate messages:
   **2 rows, 2 Telegram messages** in the same week. R5 is explicit that this
   limit is enforced in code, so a check a race walks straight through does not
   satisfy it. The claim is now taken inside a transaction under
   `pg_advisory_xact_lock(chat_id)`, with the 7-day window re-tested as part of
   a conditional `INSERT ... WHERE NOT EXISTS`. The pre-checks are kept ahead
   of it: they produce the precise reason codes and, per R5's last criterion,
   keep the model call off the rate-limited path.

2. **[High — broke R5] A suggestion that failed to send still silenced the
   chat.** The row was inserted before `send_message`. Reproduced with a
   Telegram refusal (`Forbidden: bot was blocked`): the exception propagated
   out of `maybe_suggest`, a row for a message **nobody ever saw** stayed in
   the table, the chat was rate-limited for 7 days, and
   `get_pending_suggestion` returned that row — so the next unrelated message
   in the chat would have been read as a reply to an invisible suggestion. The
   send is now wrapped: on failure the claim is deleted and
   `{"suggested": False, "reason": "send_failed"}` is returned. It deliberately
   does not re-raise — S9 calls `maybe_suggest` for *every* message in a
   dormant chat, so letting this escape would take ordinary message handling
   down with it, which R10's "fail toward inaction" forbids.

3. **[Low] `resolve_suggestion` accepted values the schema rejects.**
   Reproduced: `resolve_suggestion(pool, id, "maybe")` raised
   `CheckViolationError` from inside asyncpg. Now validated against
   `_RESPONSES` and raised as a `ValueError` at the call site.

**Verified correct, no change needed:** R5's "the rate limit is enforced in
code before any model call" — with a spy on `classify`, both the rate-limited
and topic-suppressed paths make **zero** model calls. Locked in by two tests
rather than left as an assumption, since it is the criterion most likely to
regress silently.

Each of the three regression tests was confirmed to fail against the original
implementation (`git stash` on `bot/proactive.py`) before being kept.
