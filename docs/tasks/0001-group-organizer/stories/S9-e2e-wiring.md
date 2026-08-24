# Story S9: Message router / end-to-end wiring

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** The single place that decides, for every incoming Telegram
message, whether the chat is dormant or active and which of the special
cases (session start, session stop, closing-question reply, pending
destructive-action confirmation) applies before falling through to the
general tool-calling loop — then the `python-telegram-bot` `Application`
that drives it.
**Satisfies:** R1, R2, R3, R5, R6, R10
**Depends on:** S2, S3, S4, S5, S6, S7
**Parallel-safe with:** none (final integration)
**Requirements & global constraints:** see `../EPIC.md`

**One documented v1 simplification:** when a session starts from an
*accepted proactive suggestion* (R5), the `activity_type` is taken from a
small static mapping keyed by the suggestion's keyword topic (not
re-extracted from the original trigger message) — good enough for a
coarse label, and the model fills in real details (place, date, etc.) as
facts once the session is active. An *explicit* start request ("start
watching for the picnic") gets a real extracted label, since that's the
more common and more visible path.

---

### Task 1: Dormant-chat routing

**Satisfies:** R3, R5

**Files:**
- Create: `bot/router.py`
- Create: `tests/test_router.py`

**Interfaces:**
- Consumes: `bot.session.start_session`/`SessionAlreadyActiveError` (S3),
  `bot.proactive.get_pending_suggestion`/`resolve_suggestion`/`maybe_suggest`
  (S7), `bot.ai.classify.classify`/`extract` (S2), `bot.decision_log.log_decision` (S3).
- Produces: `async bot.router.handle_dormant_message(pool, telegram_bot, chat_id, user_id, text) -> None`.

- [ ] **Step 1: Write the failing test** — create `tests/test_router.py`:
  ```python
  from unittest.mock import AsyncMock, MagicMock

  import bot.router as router
  import bot.session as session


  def _pool():
      return MagicMock(name="pool")


  async def test_dormant_accepts_pending_suggestion_and_starts_session(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      pending = {"id": 7, "topic_key": "lets_go"}
      monkeypatch.setattr(router.proactive, "get_pending_suggestion", AsyncMock(return_value=pending))
      monkeypatch.setattr(router.proactive, "resolve_suggestion", AsyncMock())
      monkeypatch.setattr(router, "classify", AsyncMock(return_value=True))
      started = {"id": 42, "chat_id": 1}
      monkeypatch.setattr(router.session, "start_session", AsyncMock(return_value=started))
      monkeypatch.setattr(router.decision_log, "log_decision", AsyncMock())

      await router.handle_dormant_message(pool, telegram_bot, chat_id=1, user_id=99, text="давай")

      router.proactive.resolve_suggestion.assert_awaited_once_with(pool, 7, "accepted")
      router.session.start_session.assert_awaited_once_with(pool, 1, "trip")
      telegram_bot.send_message.assert_awaited_once()


  async def test_dormant_declines_pending_suggestion_when_not_accepted(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      pending = {"id": 7, "topic_key": "lets_go"}
      monkeypatch.setattr(router.proactive, "get_pending_suggestion", AsyncMock(return_value=pending))
      monkeypatch.setattr(router.proactive, "resolve_suggestion", AsyncMock())
      monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
      monkeypatch.setattr(router.session, "start_session", AsyncMock())
      monkeypatch.setattr(router.decision_log, "log_decision", AsyncMock())

      await router.handle_dormant_message(pool, telegram_bot, chat_id=1, user_id=99, text="не сегодня")

      router.proactive.resolve_suggestion.assert_awaited_once_with(pool, 7, "declined")
      router.session.start_session.assert_not_awaited()
      telegram_bot.send_message.assert_not_awaited()


  async def test_dormant_explicit_start_when_no_pending_suggestion(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      monkeypatch.setattr(router.proactive, "get_pending_suggestion", AsyncMock(return_value=None))
      monkeypatch.setattr(router, "extract", AsyncMock(return_value={"is_start": True, "activity_type": "picnic"}))
      started = {"id": 42, "chat_id": 1}
      monkeypatch.setattr(router.session, "start_session", AsyncMock(return_value=started))
      monkeypatch.setattr(router.decision_log, "log_decision", AsyncMock())
      monkeypatch.setattr(router.proactive, "maybe_suggest", AsyncMock())

      await router.handle_dormant_message(pool, telegram_bot, chat_id=1, user_id=99, text="start watching for the picnic")

      router.session.start_session.assert_awaited_once_with(pool, 1, "picnic")
      telegram_bot.send_message.assert_awaited_once()
      router.proactive.maybe_suggest.assert_not_awaited()


  async def test_dormant_explicit_start_race_reports_already_active(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      monkeypatch.setattr(router.proactive, "get_pending_suggestion", AsyncMock(return_value=None))
      monkeypatch.setattr(router, "extract", AsyncMock(return_value={"is_start": True, "activity_type": "picnic"}))
      monkeypatch.setattr(
          router.session, "start_session",
          AsyncMock(side_effect=session.SessionAlreadyActiveError("already active")),
      )

      await router.handle_dormant_message(pool, telegram_bot, chat_id=1, user_id=99, text="start watching for the picnic")

      telegram_bot.send_message.assert_awaited_once()
      assert "уже" in telegram_bot.send_message.await_args.kwargs["text"].lower()


  async def test_dormant_falls_through_to_proactive_filter(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      monkeypatch.setattr(router.proactive, "get_pending_suggestion", AsyncMock(return_value=None))
      monkeypatch.setattr(router, "extract", AsyncMock(return_value={"is_start": False}))
      monkeypatch.setattr(router.proactive, "maybe_suggest", AsyncMock(return_value={"suggested": False, "reason": "no_keyword_match"}))
      monkeypatch.setattr(router.decision_log, "log_decision", AsyncMock())

      await router.handle_dormant_message(pool, telegram_bot, chat_id=1, user_id=99, text="nice weather today")

      router.proactive.maybe_suggest.assert_awaited_once_with(pool, telegram_bot, 1, "nice weather today")
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  uv run pytest tests/test_router.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'bot.router'`.

- [ ] **Step 3: Implement `bot/router.py`**
  ```python
  import logging

  from google.genai import types

  import bot.decision_log as decision_log
  import bot.proactive as proactive
  import bot.session as session
  from bot.ai.classify import classify, extract

  log = logging.getLogger(__name__)

  _TOPIC_TO_ACTIVITY_TYPE = {
      "havent_in_a_while": "gathering",
      "should_go_somewhere": "trip",
      "lets_go": "trip",
  }

  _STARTED_TEXT_TEMPLATE = "Принял, слежу за: {activity_type}. Скажите «хватит», когда закончим."
  _ALREADY_ACTIVE_TEXT = "Уже слежу за чем-то в этом чате — сначала закончим то."

  _ACCEPT_SUGGESTION_INSTRUCTION = (
      "The bot just asked in a group chat: 'Want help organizing that?'. "
      "Given the reply below, answer true only if it's a clear yes/go-ahead. "
      "Answer false for anything else (a no, a brush-off, or an unrelated message)."
  )

  _START_SCHEMA = types.Schema(
      type=types.Type.OBJECT,
      properties={
          "is_start": types.Schema(type=types.Type.BOOLEAN),
          "activity_type": types.Schema(
              type=types.Type.STRING,
              description="A short 1-3 word label, e.g. 'picnic', 'birthday', 'trip'.",
          ),
      },
      required=["is_start"],
  )
  _START_INSTRUCTION = (
      "The bot is currently dormant in a group chat. Answer is_start=true "
      "only if this message is an explicit, direct instruction telling the "
      "bot to start tracking/organizing something (e.g. 'start watching for "
      "the picnic', 'keep an eye on this, we are planning a trip'). Ordinary "
      "chat about an event that isn't addressed to the bot is is_start=false. "
      "If is_start is true, also give a short activity_type label."
  )


  async def handle_dormant_message(pool, telegram_bot, chat_id: int, user_id: int | None, text: str) -> None:
      pending = await proactive.get_pending_suggestion(pool, chat_id)
      if pending is not None:
          accepted = await classify(_ACCEPT_SUGGESTION_INSTRUCTION, text)
          if accepted:
              await proactive.resolve_suggestion(pool, pending["id"], "accepted")
              activity_type = _TOPIC_TO_ACTIVITY_TYPE.get(pending["topic_key"], "gathering")
              row = await session.start_session(pool, chat_id, activity_type)
              await telegram_bot.send_message(
                  chat_id=chat_id, text=_STARTED_TEXT_TEMPLATE.format(activity_type=activity_type)
              )
              await decision_log.log_decision(
                  pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="session_start",
                  decision={"trigger": "proactive_accept", "session_id": row["id"]},
              )
          else:
              await proactive.resolve_suggestion(pool, pending["id"], "declined")
              await decision_log.log_decision(
                  pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="proactive_response",
                  decision={"result": "declined_or_unrelated"},
              )
          return

      start = await extract(_START_INSTRUCTION, text, _START_SCHEMA)
      if start.get("is_start"):
          activity_type = start.get("activity_type") or "gathering"
          try:
              row = await session.start_session(pool, chat_id, activity_type)
          except session.SessionAlreadyActiveError:
              await telegram_bot.send_message(chat_id=chat_id, text=_ALREADY_ACTIVE_TEXT)
              return
          await telegram_bot.send_message(
              chat_id=chat_id, text=_STARTED_TEXT_TEMPLATE.format(activity_type=activity_type)
          )
          await decision_log.log_decision(
              pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="session_start",
              decision={"trigger": "explicit", "session_id": row["id"]},
          )
          return

      result = await proactive.maybe_suggest(pool, telegram_bot, chat_id, text)
      await decision_log.log_decision(
          pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="dormant_router", decision=result
      )
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  uv run pytest tests/test_router.py -v
  ```
  Expected: `5 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/router.py tests/test_router.py
  git commit -m "Add dormant-chat message routing (session start, proactive delegation)"
  ```

---

### Task 2: Active-session routing

**Satisfies:** R1, R2, R3, R6, R10

**Files:**
- Modify: `bot/router.py`
- Modify: `tests/test_router.py`

**Interfaces:**
- Consumes: `bot.session.close_session`/`record_closing_reply`/`touch_activity`
  (S3), `bot.tools.core.get_pending_confirmation`/`resolve_confirmation`/
  `execute_confirmed_action`/`list_show`/`get_participants` (S4),
  `bot.tools.core.build_core_registry`, `bot.tools.external.build_external_registry`,
  `bot.tools.composed.build_composed_registry` (S4/S5/S6),
  `bot.ai.tool_loop.run_tool_loop`, `bot.ai.client.fallback` (S2).
- Produces: `async bot.router.handle_active_message(pool, telegram_bot, active_session, user_id, text) -> None`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_router.py`:
  ```python
  def _active_session(session_id=1, chat_id=1, closing_question_asked_at=None):
      return {"id": session_id, "chat_id": chat_id, "closing_question_asked_at": closing_question_asked_at}


  async def test_active_explicit_stop_closes_and_summarizes(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      monkeypatch.setattr(router, "classify", AsyncMock(return_value=True))
      monkeypatch.setattr(router, "_summarize_session", AsyncMock(return_value="SUMMARY"))
      monkeypatch.setattr(router.session, "close_session", AsyncMock())
      monkeypatch.setattr(router.decision_log, "log_decision", AsyncMock())

      await router.handle_active_message(pool, telegram_bot, _active_session(), user_id=1, text="that's it, thanks")

      router.session.close_session.assert_awaited_once_with(pool, 1, reason="explicit_stop")
      telegram_bot.send_message.assert_awaited_once_with(chat_id=1, text="SUMMARY")


  async def test_active_closing_reply_yes_closes_session(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))  # not an explicit stop
      monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "yes"}))
      monkeypatch.setattr(router, "_summarize_session", AsyncMock(return_value="SUMMARY"))
      monkeypatch.setattr(router.session, "record_closing_reply", AsyncMock())

      from datetime import datetime
      active = _active_session(closing_question_asked_at=datetime(2026, 8, 1))

      await router.handle_active_message(pool, telegram_bot, active, user_id=1, text="yep all done")

      router.session.record_closing_reply.assert_awaited_once_with(pool, 1, continued=False)
      telegram_bot.send_message.assert_awaited_once_with(chat_id=1, text="SUMMARY")


  async def test_active_closing_reply_no_stays_active(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
      monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "no"}))
      monkeypatch.setattr(router.session, "record_closing_reply", AsyncMock())

      from datetime import datetime
      active = _active_session(closing_question_asked_at=datetime(2026, 8, 1))

      await router.handle_active_message(pool, telegram_bot, active, user_id=1, text="not yet, next week")

      router.session.record_closing_reply.assert_awaited_once_with(pool, 1, continued=True)
      telegram_bot.send_message.assert_awaited_once()


  async def test_active_closing_reply_unrelated_falls_through_to_tool_loop(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
      monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "unrelated"}))
      monkeypatch.setattr(router.core_tools, "get_pending_confirmation", AsyncMock(return_value=None))
      monkeypatch.setattr(router.session, "touch_activity", AsyncMock())
      monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="tomatoes added"))
      monkeypatch.setattr(router.decision_log, "log_decision", AsyncMock())
      monkeypatch.setattr(router, "_build_registry", lambda pool, bot: {})

      from datetime import datetime
      active = _active_session(closing_question_asked_at=datetime(2026, 8, 1))

      await router.handle_active_message(pool, telegram_bot, active, user_id=1, text="also we need tomatoes")

      router.run_tool_loop.assert_awaited_once()
      telegram_bot.send_message.assert_awaited_once_with(chat_id=1, text="tomatoes added")


  async def test_active_pending_confirmation_yes_executes(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
      confirmation = {"id": 5, "action_type": "list_remove_item", "action_params": {"name": "tomatoes"}}
      monkeypatch.setattr(router.core_tools, "get_pending_confirmation", AsyncMock(return_value=confirmation))
      monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "yes"}))
      monkeypatch.setattr(router.core_tools, "resolve_confirmation", AsyncMock(return_value=confirmation))
      monkeypatch.setattr(router.core_tools, "execute_confirmed_action", AsyncMock(return_value={"status": "executed"}))

      await router.handle_active_message(pool, telegram_bot, _active_session(), user_id=1, text="yes go ahead")

      router.core_tools.execute_confirmed_action.assert_awaited_once()
      telegram_bot.send_message.assert_awaited_once_with(chat_id=1, text="Готово.")


  async def test_active_pending_confirmation_no_cancels(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
      confirmation = {"id": 5, "action_type": "list_remove_item", "action_params": {"name": "tomatoes"}}
      monkeypatch.setattr(router.core_tools, "get_pending_confirmation", AsyncMock(return_value=confirmation))
      monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "no"}))
      monkeypatch.setattr(router.core_tools, "resolve_confirmation", AsyncMock())

      await router.handle_active_message(pool, telegram_bot, _active_session(), user_id=1, text="no don't")

      router.core_tools.resolve_confirmation.assert_awaited_once_with(pool, 5, confirmed=False)
      telegram_bot.send_message.assert_awaited_once_with(chat_id=1, text="Отменил.")


  async def test_active_normal_message_runs_tool_loop_and_replies(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
      monkeypatch.setattr(router.core_tools, "get_pending_confirmation", AsyncMock(return_value=None))
      monkeypatch.setattr(router.session, "touch_activity", AsyncMock())
      monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="Here's the list: tomatoes"))
      monkeypatch.setattr(router.decision_log, "log_decision", AsyncMock())
      monkeypatch.setattr(router, "_build_registry", lambda pool, bot: {})

      await router.handle_active_message(pool, telegram_bot, _active_session(), user_id=1, text="what's on the list")

      router.session.touch_activity.assert_awaited_once_with(pool, 1)
      telegram_bot.send_message.assert_awaited_once_with(chat_id=1, text="Here's the list: tomatoes")


  async def test_active_silent_capture_sends_nothing(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
      monkeypatch.setattr(router.core_tools, "get_pending_confirmation", AsyncMock(return_value=None))
      monkeypatch.setattr(router.session, "touch_activity", AsyncMock())
      monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="   "))
      monkeypatch.setattr(router.decision_log, "log_decision", AsyncMock())
      monkeypatch.setattr(router, "_build_registry", lambda pool, bot: {})

      await router.handle_active_message(pool, telegram_bot, _active_session(), user_id=1, text="we need tomatoes")

      telegram_bot.send_message.assert_not_awaited()


  async def test_active_tool_loop_failure_sends_fallback_message(monkeypatch):
      pool, telegram_bot = _pool(), AsyncMock()
      monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
      monkeypatch.setattr(router.core_tools, "get_pending_confirmation", AsyncMock(return_value=None))
      monkeypatch.setattr(router.session, "touch_activity", AsyncMock())
      monkeypatch.setattr(router, "run_tool_loop", AsyncMock(side_effect=RuntimeError("boom")))
      monkeypatch.setattr(router.decision_log, "log_decision", AsyncMock())
      monkeypatch.setattr(router, "_build_registry", lambda pool, bot: {})

      await router.handle_active_message(pool, telegram_bot, _active_session(), user_id=1, text="???")

      telegram_bot.send_message.assert_awaited_once_with(chat_id=1, text=router._FALLBACK_MESSAGE)
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  uv run pytest tests/test_router.py -v
  ```
  Expected: `AttributeError: module 'bot.router' has no attribute 'handle_active_message'`.

- [ ] **Step 3: Append to `bot/router.py`**
  ```python
  import bot.tools.composed as composed_tools
  import bot.tools.core as core_tools
  import bot.tools.external as external_tools
  from bot.ai.client import fallback
  from bot.ai.tool_loop import run_tool_loop

  _FALLBACK_MESSAGE = "Не понял, переформулируй, пожалуйста."

  _STOP_INSTRUCTION = (
      "The bot is actively tracking an event for this chat. Answer true "
      "only if this message is an explicit, direct instruction telling the "
      "bot to stop (e.g. 'that's it, thanks', 'you can stop now', 'hatit'). "
      "Chat that merely sounds like the event is wrapping up, without "
      "directly telling the bot to stop, is false."
  )

  _YES_NO_UNRELATED_SCHEMA = types.Schema(
      type=types.Type.OBJECT,
      properties={"reply": types.Schema(type=types.Type.STRING, enum=["yes", "no", "unrelated"])},
      required=["reply"],
  )
  _CLOSING_REPLY_INSTRUCTION = (
      "The bot just asked in the group chat: 'How did it go — still need "
      "me, or good to close?'. Classify the message below relative to that "
      "question. 'yes' = it's over, close. 'no' = not yet, stay active. "
      "'unrelated' = this message isn't actually answering that question."
  )
  _CONFIRMATION_REPLY_TEMPLATE = (
      "The bot proposed a sensitive action and is waiting for confirmation: "
      "{action_type} {action_params}. Classify the message below. 'yes' = "
      "clearly confirms doing it. 'no' = clearly declines/cancels it. "
      "'unrelated' = this message isn't actually answering that."
  )
  _ACTIVE_MODE_SYSTEM_INSTRUCTION = (
      "You are a group-chat organizing assistant, currently actively "
      "tracking one event. Use the available tools to remember facts, "
      "manage the shared list, track participant confirmations, schedule "
      "reminders, and answer questions using grounded lookups — never "
      "invent a fact that wasn't found by a tool. When a message only gives "
      "you something to silently record (a fact, a list item) and doesn't "
      "ask a question or need clarification, respond with an empty string — "
      "do not narrate what you just recorded."
  )


  def _build_registry(pool, telegram_bot) -> dict:
      return {
          **core_tools.build_core_registry(pool, telegram_bot),
          **external_tools.build_external_registry(),
          **composed_tools.build_composed_registry(pool, telegram_bot),
      }


  async def _summarize_session(pool, session_id: int) -> str:
      list_result = await core_tools.list_show(pool, session_id)
      participants_result = await core_tools.get_participants(pool, session_id)
      items = ", ".join(i["name"] for i in list_result["items"]) or "пусто"
      confirmed = [p["display_name"] for p in participants_result["participants"] if p["status"] == "confirmed"]
      return f"Готово, сессию закрываю. Список: {items}. Подтвердили: {', '.join(confirmed) or 'никто'}."


  async def handle_active_message(pool, telegram_bot, active_session, user_id: int | None, text: str) -> None:
      session_id, chat_id = active_session["id"], active_session["chat_id"]

      if await classify(_STOP_INSTRUCTION, text):
          summary = await _summarize_session(pool, session_id)
          await session.close_session(pool, session_id, reason="explicit_stop")
          await telegram_bot.send_message(chat_id=chat_id, text=summary)
          await decision_log.log_decision(
              pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="session_stop",
              decision={"trigger": "explicit", "session_id": session_id},
          )
          return

      if active_session["closing_question_asked_at"] is not None:
          reply = (await extract(_CLOSING_REPLY_INSTRUCTION, text, _YES_NO_UNRELATED_SCHEMA)).get("reply")
          if reply == "yes":
              summary = await _summarize_session(pool, session_id)
              await session.record_closing_reply(pool, session_id, continued=False)
              await telegram_bot.send_message(chat_id=chat_id, text=summary)
              return
          if reply == "no":
              await session.record_closing_reply(pool, session_id, continued=True)
              await telegram_bot.send_message(chat_id=chat_id, text="Понял, продолжаю следить.")
              return
          # "unrelated" -> fall through to normal processing below

      pending_confirmation = await core_tools.get_pending_confirmation(pool, chat_id)
      if pending_confirmation is not None:
          instruction = _CONFIRMATION_REPLY_TEMPLATE.format(
              action_type=pending_confirmation["action_type"],
              action_params=pending_confirmation["action_params"],
          )
          reply = (await extract(instruction, text, _YES_NO_UNRELATED_SCHEMA)).get("reply")
          if reply == "yes":
              resolved = await core_tools.resolve_confirmation(pool, pending_confirmation["id"], confirmed=True)
              await core_tools.execute_confirmed_action(pool, telegram_bot, resolved)
              await telegram_bot.send_message(chat_id=chat_id, text="Готово.")
              return
          if reply == "no":
              await core_tools.resolve_confirmation(pool, pending_confirmation["id"], confirmed=False)
              await telegram_bot.send_message(chat_id=chat_id, text="Отменил.")
              return
          # "unrelated" -> fall through to normal processing below

      await session.touch_activity(pool, session_id)
      registry = _build_registry(pool, telegram_bot)
      try:
          reply_text = await run_tool_loop(
              fallback, text, registry, system_instruction=_ACTIVE_MODE_SYSTEM_INSTRUCTION
          )
      except Exception:
          log.exception("Tool loop failed for chat_id=%s session_id=%s", chat_id, session_id)
          reply_text = _FALLBACK_MESSAGE

      await decision_log.log_decision(
          pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="tool_call",
          decision={"session_id": session_id, "reply": reply_text},
      )
      if reply_text.strip():
          await telegram_bot.send_message(chat_id=chat_id, text=reply_text)
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  uv run pytest tests/test_router.py -v
  ```
  Expected: `14 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/router.py tests/test_router.py
  git commit -m "Add active-session routing: stop, closing-question reply, confirmation gate, tool loop"
  ```

---

### Task 3: `bot/main.py` — Telegram application wiring

**Satisfies:** R10

**Files:**
- Create: `bot/main.py`
- Create: `tests/test_main.py`

**Interfaces:**
- Consumes: `bot.router.handle_dormant_message`/`handle_active_message`
  (Tasks 1–2), `bot.dedup.is_duplicate` (S3), `db.pool.create_pool`/`init_db` (S1).
- Produces: `async bot.main.route_update(pool, telegram_bot, *, update_id, chat_id, chat_title, user_id, text) -> None`,
  `bot.main.main()` — the bot process entrypoint.

- [ ] **Step 1: Write the failing test** — create `tests/test_main.py`:
  ```python
  from unittest.mock import AsyncMock, MagicMock

  import bot.main as main


  async def test_route_update_drops_duplicate(monkeypatch):
      pool = MagicMock()
      monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=True))
      monkeypatch.setattr(main.router, "handle_dormant_message", AsyncMock())
      monkeypatch.setattr(main.router, "handle_active_message", AsyncMock())

      await main.route_update(
          pool, AsyncMock(), update_id=1, chat_id=1, chat_title="Chat", user_id=1, text="hi"
      )

      main.router.handle_dormant_message.assert_not_awaited()
      main.router.handle_active_message.assert_not_awaited()


  async def test_route_update_dormant_dispatches_to_dormant_handler(monkeypatch):
      pool = MagicMock()
      pool.execute = AsyncMock()
      telegram_bot = AsyncMock()
      monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=False))
      monkeypatch.setattr(main.session, "get_active_session", AsyncMock(return_value=None))
      monkeypatch.setattr(main.router, "handle_dormant_message", AsyncMock())

      await main.route_update(
          pool, telegram_bot, update_id=1, chat_id=1, chat_title="Chat", user_id=1, text="hi"
      )

      main.router.handle_dormant_message.assert_awaited_once_with(pool, telegram_bot, 1, 1, "hi")


  async def test_route_update_active_dispatches_to_active_handler(monkeypatch):
      pool = MagicMock()
      pool.execute = AsyncMock()
      active = {"id": 1, "chat_id": 1}
      monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=False))
      monkeypatch.setattr(main.session, "get_active_session", AsyncMock(return_value=active))
      monkeypatch.setattr(main.router, "handle_active_message", AsyncMock())

      await main.route_update(
          pool, AsyncMock(), update_id=1, chat_id=1, chat_title="Chat", user_id=1, text="hi"
      )

      main.router.handle_active_message.assert_awaited_once()


  async def test_route_update_ignores_empty_text(monkeypatch):
      pool = MagicMock()
      monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=False))
      monkeypatch.setattr(main.router, "handle_dormant_message", AsyncMock())

      await main.route_update(
          pool, AsyncMock(), update_id=1, chat_id=1, chat_title="Chat", user_id=1, text=""
      )

      main.router.handle_dormant_message.assert_not_awaited()
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  uv run pytest tests/test_main.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'bot.main'`.

- [ ] **Step 3: Implement `bot/main.py`**
  ```python
  import logging
  import os

  from telegram import Update
  from telegram.ext import Application, ContextTypes, MessageHandler, filters

  import bot.dedup as dedup
  import bot.router as router
  import bot.session as session
  import db.pool as db_pool_module

  logging.basicConfig(
      format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
  )
  logging.getLogger("google_genai").setLevel(logging.WARNING)
  logging.getLogger("httpx").setLevel(logging.WARNING)
  log = logging.getLogger(__name__)


  async def route_update(pool, telegram_bot, *, update_id, chat_id, chat_title, user_id, text) -> None:
      if await dedup.is_duplicate(pool, update_id):
          return
      if not text:
          return

      await pool.execute(
          "INSERT INTO chats (chat_id, title) VALUES ($1, $2) ON CONFLICT (chat_id) DO NOTHING",
          chat_id, chat_title,
      )

      active = await session.get_active_session(pool, chat_id)
      if active is None:
          await router.handle_dormant_message(pool, telegram_bot, chat_id, user_id, text)
      else:
          await router.handle_active_message(pool, telegram_bot, active, user_id, text)


  async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
      pool = context.bot_data["pool"]
      await route_update(
          pool, context.bot,
          update_id=update.update_id,
          chat_id=update.effective_chat.id,
          chat_title=update.effective_chat.title or update.effective_chat.first_name or "",
          user_id=update.effective_user.id if update.effective_user else None,
          text=update.effective_message.text or "",
      )


  async def post_init(app: Application) -> None:
      pool = await db_pool_module.create_pool(os.environ["DATABASE_URL"])
      await db_pool_module.init_db(pool)
      app.bot_data["pool"] = pool


  def main() -> None:
      app = (
          Application.builder()
          .token(os.environ["BOT_TOKEN"])
          .concurrent_updates(True)
          .post_init(post_init)
          .build()
      )
      app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
      log.info("Starting bot (long polling)")
      app.run_polling()


  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  uv run pytest tests/test_main.py -v
  ```
  Expected: `4 passed`.

- [ ] **Step 5: Manual smoke test**
  ```bash
  uv run python -m py_compile bot/main.py bot/router.py
  DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bot_organizer \
    BOT_TOKEN=<real token> GEMINI_API_KEY=<real key> GOOGLE_MAPS_API_KEY=<real key> \
    uv run python -m bot.main
  ```
  In Telegram, add the bot to a test group. Confirm: (1) ordinary chat
  produces no reply while dormant; (2) "start watching for X" gets a
  confirmation reply and a session row appears in `sessions`; (3) "we need
  tomatoes" while active is captured silently (no reply) and appears in
  `list_items`; (4) "what's on the list" replies with the list; (5) "that's
  it, thanks" closes the session with a summary.

- [ ] **Step 6: Commit**
  ```bash
  git add bot/main.py tests/test_main.py
  git commit -m "Add bot/main.py: Telegram application wiring and update routing"
  ```
