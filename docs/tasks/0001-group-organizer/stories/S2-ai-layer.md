# Story S2: AI layer — Gemini, fallback, tool-calling harness, cheap classifier

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** Give every later story a Gemini client with model fallback, a
generic function-calling loop that executes whatever tools it's handed,
the single shared tool-schema module, and a cheap binary classifier —
all decoupled from any specific tool implementation.
**Satisfies:** R5, R6, R10
**Depends on:** none
**Parallel-safe with:** S1
**Requirements & global constraints:** see `../EPIC.md`

---

### Task 1: Gemini client, model fallback, cheap classifier

**Satisfies:** R5, R10

**Files:**
- Create: `bot/ai/client.py`
- Create: `bot/ai/classify.py`
- Create: `tests/test_classify.py`

**Interfaces:**
- Consumes: `GEMINI_API_KEY` env var (Task 1 of S1 already makes tests set
  a fake value).
- Produces:
  - `bot.ai.client.gemini` (`genai.Client`), `bot.ai.client.MODELS: list[str]`,
    `bot.ai.client.fallback: ModelFallback`, `bot.ai.client.CLASSIFIER_MODEL: str`.
  - `class bot.ai.client.ModelFallback` with
    `async def send_message(self, text, *, config=None, history=None) -> tuple[str, Chat, Response]`.
  - `async bot.ai.classify.extract(instruction: str, text: str, schema: types.Schema) -> dict` —
    generic cheap structured-JSON extraction against `CLASSIFIER_MODEL`;
    fails closed (returns `{}`) on any API error. Consumed directly by S9
    for explicit session-start detection and closing-question reply
    parsing (both need more than a single boolean back).
  - `async bot.ai.classify.classify(instruction: str, text: str) -> bool` —
    built on `extract()` with a fixed boolean schema; fails closed
    (returns `False`).

- [ ] **Step 1: Write the failing test** — create `tests/test_classify.py`:
  ```python
  import json
  from unittest.mock import AsyncMock

  import pytest
  from google.genai import errors

  from bot.ai import classify as classify_module


  async def test_classify_parses_true(monkeypatch):
      resp = AsyncMock()
      resp.text = json.dumps({"result": True})
      generate = AsyncMock(return_value=resp)
      monkeypatch.setattr(classify_module.gemini.aio.models, "generate_content", generate)

      result = await classify_module.classify("Is this about food?", "let's get pizza")

      assert result is True
      generate.assert_awaited_once()


  async def test_classify_parses_false(monkeypatch):
      resp = AsyncMock()
      resp.text = json.dumps({"result": False})
      monkeypatch.setattr(
          classify_module.gemini.aio.models, "generate_content", AsyncMock(return_value=resp)
      )

      assert await classify_module.classify("Is this about food?", "nice weather today") is False


  async def test_classify_fails_closed_on_api_error(monkeypatch):
      async def boom(*args, **kwargs):
          raise errors.APIError(code=500, response_json={}, response=None)

      monkeypatch.setattr(classify_module.gemini.aio.models, "generate_content", boom)

      assert await classify_module.classify("Is this about food?", "anything") is False


  async def test_extract_returns_parsed_json(monkeypatch):
      from google.genai import types

      resp = AsyncMock()
      resp.text = json.dumps({"is_start": True, "activity_type": "picnic"})
      monkeypatch.setattr(classify_module.gemini.aio.models, "generate_content", AsyncMock(return_value=resp))
      schema = types.Schema(
          type=types.Type.OBJECT,
          properties={"is_start": types.Schema(type=types.Type.BOOLEAN), "activity_type": types.Schema(type=types.Type.STRING)},
          required=["is_start"],
      )

      result = await classify_module.extract("...", "let's track the picnic", schema)

      assert result == {"is_start": True, "activity_type": "picnic"}


  async def test_extract_fails_closed_to_empty_dict(monkeypatch):
      from google.genai import types

      async def boom(*args, **kwargs):
          raise errors.APIError(code=500, response_json={}, response=None)

      monkeypatch.setattr(classify_module.gemini.aio.models, "generate_content", boom)
      schema = types.Schema(type=types.Type.OBJECT, properties={}, required=[])

      assert await classify_module.extract("...", "anything", schema) == {}
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  uv run pytest tests/test_classify.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'bot.ai.classify'`.

- [ ] **Step 3: Implement `bot/ai/client.py`**
  ```python
  import logging
  import os

  from google.genai import errors

  from google import genai

  log = logging.getLogger(__name__)

  gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

  # Priority order: strongest first, Gemma as last resort. Sticky fallback
  # on 429/5xx, never rolls back — same pattern validated in the sibling
  # general-telegram-bot project's gemini_fallback.py.
  MODELS = [
      "gemini-3.6-flash",
      "gemini-3.5-flash",
      "gemini-3.5-flash-lite",
      "gemini-3.1-flash-lite",
      "gemma-4-31b-it",
  ]

  # Cheapest model in the chain — used for the two-stage cheap-filter
  # checks (R5, active-mode relevance) so the primary model is never
  # spent on a binary "is this even relevant" question.
  CLASSIFIER_MODEL = MODELS[-1]


  class ModelFallback:
      """Sends messages through a list of Gemini/Gemma models, switching to
      the next one in the list on 429 (rate limit) or 5xx (overload) and
      remembering the choice — API-key limits are shared across the whole
      process, so it never rolls back."""

      def __init__(self, client, models: list[str]):
          self.client = client
          self.models = models
          self.index = 0

      @property
      def model(self) -> str:
          return self.models[self.index]

      async def send_message(self, text, *, config=None, history=None):
          while True:
              model = self.model
              chat = self.client.aio.chats.create(model=model, config=config, history=history)
              try:
                  resp = await chat.send_message(text)
                  return model, chat, resp
              except errors.APIError as e:
                  is_retryable = e.code == 429 or e.code >= 500
                  if is_retryable and self.index < len(self.models) - 1:
                      log.warning("%s failed (%s), switching to %s", model, e.code, self.models[self.index + 1])
                      self.index += 1
                      continue
                  raise


  fallback = ModelFallback(gemini, MODELS)
  ```

- [ ] **Step 4: Implement `bot/ai/classify.py`**
  ```python
  import json
  import logging

  from google.genai import errors, types

  from bot.ai.client import CLASSIFIER_MODEL, gemini

  log = logging.getLogger(__name__)

  _BOOL_SCHEMA = types.Schema(
      type=types.Type.OBJECT,
      properties={"result": types.Schema(type=types.Type.BOOLEAN)},
      required=["result"],
  )


  async def extract(instruction: str, text: str, schema: types.Schema) -> dict:
      """Cheap structured-JSON extraction against CLASSIFIER_MODEL. Fails
      closed (returns {}) on any error — a classifier failure must never
      cause a proactive suggestion, a session start/stop, or an active-mode
      tool call to fire."""
      try:
          resp = await gemini.aio.models.generate_content(
              model=CLASSIFIER_MODEL,
              contents=text,
              config=types.GenerateContentConfig(
                  system_instruction=instruction,
                  response_mime_type="application/json",
                  response_schema=schema,
              ),
          )
          return json.loads(resp.text)
      except (errors.APIError, ValueError):
          log.warning("extract() failed, defaulting to {}", exc_info=True)
          return {}


  async def classify(instruction: str, text: str) -> bool:
      result = await extract(instruction, text, _BOOL_SCHEMA)
      return bool(result.get("result", False))
  ```

- [ ] **Step 5: Run tests, confirm pass**
  ```bash
  uv run pytest tests/test_classify.py -v
  ```
  Expected: `5 passed`.

- [ ] **Step 6: Commit**
  ```bash
  git add bot/ai/client.py bot/ai/classify.py tests/test_classify.py
  git commit -m "Add Gemini client, model fallback, and cheap binary classifier"
  ```

---

### Task 2: Tool schema + function-calling loop

**Satisfies:** R6, R10

**Files:**
- Create: `bot/tools/schema.py`
- Create: `bot/ai/tool_loop.py`
- Create: `tests/test_tool_loop.py`

**Interfaces:**
- Consumes: `bot.ai.client.ModelFallback` (Task 1).
- Produces:
  - `bot.tools.schema.ALL_TOOLS: types.Tool` — every tool declaration used
    by S4/S5/S6, in one place per the epic's global constraint.
  - `bot.tools.schema.ToolRegistry = dict[str, Callable]` (type alias).
  - `async bot.ai.tool_loop.run_tool_loop(fallback, prompt, registry, *, history=None, system_instruction=None) -> str` —
    drives the send → execute-function-calls → send-results loop to
    completion and returns the final text reply (empty string if the
    model chose not to say anything — e.g. a silent fact/list capture
    per R1; S9 treats an empty/whitespace reply as "send nothing").
    Raises on Gemini API failure or on exceeding the iteration cap —
    callers (S9) catch this and apply the R10 "didn't understand"
    fallback reply. Tool side effects already executed before a failure
    are **not** rolled back — they're additive user data (a remembered
    fact, a list item), not a wrong action.

- [ ] **Step 1: Write the failing test** — create `tests/test_tool_loop.py`:
  ```python
  from unittest.mock import AsyncMock, MagicMock

  import pytest

  from bot.ai.tool_loop import run_tool_loop


  def _make_call(name, args):
      call = MagicMock()
      call.name = name
      call.args = args
      return call


  async def test_run_tool_loop_executes_function_call_and_returns_text():
      chat = MagicMock()
      first_resp = MagicMock(function_calls=[_make_call("list_add", {"session_id": 1, "name": "tomatoes"})])
      second_resp = MagicMock(function_calls=None, text="Added tomatoes.")
      chat.send_message = AsyncMock(return_value=second_resp)

      fb = MagicMock()
      fb.send_message = AsyncMock(return_value=("gemini-3.6-flash", chat, first_resp))

      list_add = AsyncMock(return_value={"status": "ok"})
      registry = {"list_add": list_add}

      result = await run_tool_loop(fb, "add tomatoes to the list", registry)

      assert result == "Added tomatoes."
      list_add.assert_awaited_once_with(session_id=1, name="tomatoes")


  async def test_run_tool_loop_returns_text_directly_with_no_tool_calls():
      chat = MagicMock()
      resp = MagicMock(function_calls=None, text="Hello.")
      fb = MagicMock()
      fb.send_message = AsyncMock(return_value=("gemini-3.6-flash", chat, resp))

      result = await run_tool_loop(fb, "hi", {})

      assert result == "Hello."


  async def test_run_tool_loop_reports_unknown_tool_without_crashing():
      chat = MagicMock()
      first_resp = MagicMock(function_calls=[_make_call("no_such_tool", {})])
      second_resp = MagicMock(function_calls=None, text="ok")
      chat.send_message = AsyncMock(return_value=second_resp)
      fb = MagicMock()
      fb.send_message = AsyncMock(return_value=("gemini-3.6-flash", chat, first_resp))

      result = await run_tool_loop(fb, "do something weird", {})

      assert result == "ok"
      sent_parts = chat.send_message.await_args.args[0]
      assert "unknown tool" in sent_parts[0].function_response.response["error"]


  async def test_run_tool_loop_passes_system_instruction_through():
      chat = MagicMock()
      resp = MagicMock(function_calls=None, text="ok")
      fb = MagicMock()
      fb.send_message = AsyncMock(return_value=("gemini-3.6-flash", chat, resp))

      await run_tool_loop(fb, "hi", {}, system_instruction="Stay quiet unless asked.")

      sent_config = fb.send_message.await_args.kwargs["config"]
      assert sent_config.system_instruction == "Stay quiet unless asked."


  async def test_run_tool_loop_raises_after_max_iterations():
      chat = MagicMock()
      looping_resp = MagicMock(function_calls=[_make_call("list_add", {"session_id": 1, "name": "x"})])
      chat.send_message = AsyncMock(return_value=looping_resp)
      fb = MagicMock()
      fb.send_message = AsyncMock(return_value=("gemini-3.6-flash", chat, looping_resp))
      registry = {"list_add": AsyncMock(return_value={"status": "ok"})}

      with pytest.raises(RuntimeError, match="max iterations"):
          await run_tool_loop(fb, "loop forever", registry)
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  uv run pytest tests/test_tool_loop.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'bot.ai.tool_loop'`.

- [ ] **Step 3: Implement `bot/tools/schema.py`**
  ```python
  from google.genai import types

  Type = types.Type


  def _fn(name, description, properties, required):
      return types.FunctionDeclaration(
          name=name,
          description=description,
          parameters=types.Schema(type=Type.OBJECT, properties=properties, required=required),
      )


  _S = lambda t, desc=None: types.Schema(type=t, description=desc)  # noqa: E731

  ALL_TOOLS = types.Tool(function_declarations=[
      _fn("remember_fact", "Store an arbitrary fact about the current session (place, time, who brings what, allergies, anything).",
          {"session_id": _S(Type.INTEGER), "key": _S(Type.STRING), "value": _S(Type.STRING)},
          ["session_id", "key", "value"]),
      _fn("get_facts", "Retrieve previously remembered facts for the session, optionally filtered by key.",
          {"session_id": _S(Type.INTEGER), "key": _S(Type.STRING, "Optional exact key to filter by.")},
          ["session_id"]),
      _fn("list_add", "Add an item to the session's shared list.",
          {"session_id": _S(Type.INTEGER), "name": _S(Type.STRING)},
          ["session_id", "name"]),
      _fn("list_show", "Show the current state of the session's shared list.",
          {"session_id": _S(Type.INTEGER)}, ["session_id"]),
      _fn("list_check_off", "Mark a list item as done/acquired, matched by name.",
          {"session_id": _S(Type.INTEGER), "name": _S(Type.STRING)},
          ["session_id", "name"]),
      _fn("list_remove_item", "Permanently delete an item from the list. Destructive — requires human confirmation before it takes effect.",
          {"session_id": _S(Type.INTEGER), "name": _S(Type.STRING)},
          ["session_id", "name"]),
      _fn("set_participant", "Record or update a participant's confirmation status for the session.",
          {"session_id": _S(Type.INTEGER), "display_name": _S(Type.STRING),
           "status": _S(Type.STRING, "One of: unknown, confirmed, declined."),
           "user_id": _S(Type.INTEGER, "Telegram user id, if known.")},
          ["session_id", "display_name", "status"]),
      _fn("get_participants", "List participants and their confirmation status for the session.",
          {"session_id": _S(Type.INTEGER)}, ["session_id"]),
      _fn("nudge_unconfirmed_participants", "Privately message every participant with unknown status, asking if they're coming. Only call this when a human explicitly asked to check on/chase confirmations.",
          {"session_id": _S(Type.INTEGER)}, ["session_id"]),
      _fn("reminder_set", "Schedule a reminder to be delivered later, to one person or the whole chat.",
          {"session_id": _S(Type.INTEGER), "chat_id": _S(Type.INTEGER), "message": _S(Type.STRING),
           "remind_at": _S(Type.STRING, "ISO-8601 datetime."),
           "target_user_id": _S(Type.INTEGER, "Telegram user id, or omit to post in the group chat.")},
          ["session_id", "chat_id", "message", "remind_at"]),
      _fn("reminder_cancel", "Cancel a previously scheduled reminder.",
          {"reminder_id": _S(Type.INTEGER)}, ["reminder_id"]),
      _fn("broadcast_message", "Send an arbitrary message to the whole chat outside of a normal reply. Destructive — requires human confirmation before it takes effect.",
          {"session_id": _S(Type.INTEGER), "chat_id": _S(Type.INTEGER), "text": _S(Type.STRING)},
          ["session_id", "chat_id", "text"]),
      _fn("web_search", "Search the web for up-to-date information not already known as a fact.",
          {"query": _S(Type.STRING)}, ["query"]),
      _fn("maps_lookup", "Look up a place by name/description via maps: resolves address, coordinates, and available details/reviews.",
          {"query": _S(Type.STRING)}, ["query"]),
      _fn("weather_lookup", "Get a weather forecast for a coordinate and date.",
          {"lat": _S(Type.NUMBER), "lon": _S(Type.NUMBER), "date": _S(Type.STRING, "ISO-8601 date.")},
          ["lat", "lon", "date"]),
      _fn("resolve_and_save_place", "Resolve a place name to coordinates for this session, reusing a previously saved match instead of re-querying maps if one exists.",
          {"session_id": _S(Type.INTEGER), "place_query": _S(Type.STRING)},
          ["session_id", "place_query"]),
      _fn("send_location", "Send a native map location card for a place already resolved for this session.",
          {"session_id": _S(Type.INTEGER), "chat_id": _S(Type.INTEGER), "place_name": _S(Type.STRING)},
          ["session_id", "chat_id", "place_name"]),
      _fn("archive_lookup", "Look up where this chat has gone before for a given activity type, ranked by recency-weighted frequency, across closed sessions.",
          {"chat_id": _S(Type.INTEGER), "activity_type": _S(Type.STRING)},
          ["chat_id", "activity_type"]),
  ])
  ```

- [ ] **Step 4: Implement `bot/ai/tool_loop.py`**
  ```python
  import logging

  from google.genai import types

  log = logging.getLogger(__name__)

  MAX_TOOL_ITERATIONS = 6


  async def run_tool_loop(fallback, prompt, registry: dict, *, history=None, system_instruction=None) -> str:
      """Sends `prompt`, executes any function calls the model returns against
      `registry`, feeds the results back, and repeats until the model returns
      plain text. Raises RuntimeError if it never converges within
      MAX_TOOL_ITERATIONS turns; raises whatever the underlying Gemini call
      raises on API failure. Neither case is caught here — see R10, callers
      apply the fixed fallback reply."""
      from bot.tools.schema import ALL_TOOLS

      config = types.GenerateContentConfig(tools=[ALL_TOOLS], system_instruction=system_instruction)
      _model, chat, resp = await fallback.send_message(prompt, config=config, history=history)

      for _ in range(MAX_TOOL_ITERATIONS):
          calls = resp.function_calls
          if not calls:
              return resp.text or ""

          parts = []
          for call in calls:
              fn = registry.get(call.name)
              if fn is None:
                  result = {"error": f"unknown tool {call.name!r}"}
              else:
                  try:
                      result = await fn(**call.args)
                  except Exception as e:  # tool errors become a result, not a crash
                      log.exception("Tool %s raised", call.name)
                      result = {"error": str(e)}
              parts.append(types.Part.from_function_response(name=call.name, response=result))

          resp = await chat.send_message(parts)

      raise RuntimeError(f"tool loop exceeded max iterations ({MAX_TOOL_ITERATIONS})")
  ```

- [ ] **Step 5: Run tests, confirm pass**
  ```bash
  uv run pytest tests/test_tool_loop.py -v
  ```
  Expected: `5 passed`.

- [ ] **Step 6: Commit**
  ```bash
  git add bot/tools/schema.py bot/ai/tool_loop.py tests/test_tool_loop.py
  git commit -m "Add shared tool schema and generic function-calling loop"
  ```
