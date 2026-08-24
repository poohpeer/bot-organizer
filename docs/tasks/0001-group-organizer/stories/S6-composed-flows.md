# Story S6: Composed flows — place-check, "as usual" archive ranking, venue messages

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** The higher-level primitives that make R7/R8/R9 work: resolving
a place once and remembering it, sending it back as a real Telegram
location card, and ranking past places by recency-weighted frequency for
"where do we usually go".
**Satisfies:** R7, R8, R9
**Depends on:** S1, S5 (not S4 — the model composes `resolve_and_save_place`
with S4's `get_facts`/etc. only at the S9 wiring layer; no code import)
**Parallel-safe with:** S7, S8
**Requirements & global constraints:** see `../EPIC.md`

---

### Task 1: Resolve-and-save place (capture once, reuse after)

**Satisfies:** R7, R9

**Files:**
- Create: `bot/tools/composed.py`
- Create: `tests/test_tools_composed.py`

**Interfaces:**
- Consumes: `places` table (S1), `bot.tools.external.maps_lookup` (S5).
- Produces: `async bot.tools.composed.resolve_and_save_place(pool, session_id, place_query) -> dict` —
  `{"found": bool, "name", "address", "lat", "lon", "cached": bool}`.

- [ ] **Step 1: Write the failing test** — create `tests/test_tools_composed.py`:
  ```python
  from unittest.mock import AsyncMock, patch

  import bot.tools.composed as composed


  async def _new_session(db_pool, chat_id=1, activity_type="picnic", status="active"):
      await db_pool.execute(
          "INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id
      )
      row = await db_pool.fetchrow(
          "INSERT INTO sessions (chat_id, activity_type, status) VALUES ($1, $2, $3) RETURNING id",
          chat_id, activity_type, status,
      )
      return row["id"]


  async def test_resolve_and_save_place_queries_maps_when_uncached(db_pool):
      session_id = await _new_session(db_pool)
      maps_result = {
          "found": True, "name": "Hanania Meadow", "address": "Route 1",
          "lat": 32.79, "lon": 35.05, "rating": 4.2, "review_snippets": [],
      }

      with patch("bot.tools.composed.maps_lookup", AsyncMock(return_value=maps_result)) as mocked:
          result = await composed.resolve_and_save_place(db_pool, session_id, "Hanania meadow")

      mocked.assert_awaited_once_with("Hanania meadow")
      assert result == {
          "found": True, "name": "Hanania Meadow", "address": "Route 1",
          "lat": 32.79, "lon": 35.05, "cached": False,
      }
      saved = await db_pool.fetchrow("SELECT * FROM places WHERE session_id = $1", session_id)
      assert saved["name"] == "Hanania Meadow"


  async def test_resolve_and_save_place_reuses_cached_match(db_pool):
      session_id = await _new_session(db_pool)
      await db_pool.execute(
          "INSERT INTO places (session_id, name, address, lat, lon) VALUES ($1, 'Hanania Meadow', 'Route 1', 32.79, 35.05)",
          session_id,
      )

      with patch("bot.tools.composed.maps_lookup", AsyncMock()) as mocked:
          result = await composed.resolve_and_save_place(db_pool, session_id, "hanania meadow")

      mocked.assert_not_awaited()
      assert result == {
          "found": True, "name": "Hanania Meadow", "address": "Route 1",
          "lat": 32.79, "lon": 35.05, "cached": True,
      }


  async def test_resolve_and_save_place_not_found(db_pool):
      session_id = await _new_session(db_pool)

      with patch("bot.tools.composed.maps_lookup", AsyncMock(return_value={"found": False})):
          result = await composed.resolve_and_save_place(db_pool, session_id, "nowhere in particular")

      assert result == {"found": False}
      assert await db_pool.fetchrow("SELECT * FROM places WHERE session_id = $1", session_id) is None
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_composed.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'bot.tools.composed'`.

- [ ] **Step 3: Implement `bot/tools/composed.py`**
  ```python
  from bot.tools.external import maps_lookup


  async def resolve_and_save_place(pool, session_id, place_query) -> dict:
      cached = await pool.fetchrow(
          "SELECT name, address, lat, lon FROM places WHERE session_id = $1 AND lower(name) = lower($2)",
          session_id, place_query,
      )
      if cached:
          return {
              "found": True, "name": cached["name"], "address": cached["address"],
              "lat": cached["lat"], "lon": cached["lon"], "cached": True,
          }

      looked_up = await maps_lookup(place_query)
      if not looked_up.get("found"):
          return {"found": False}

      await pool.execute(
          "INSERT INTO places (session_id, name, address, lat, lon) VALUES ($1, $2, $3, $4, $5)",
          session_id, looked_up["name"], looked_up["address"], looked_up["lat"], looked_up["lon"],
      )
      return {
          "found": True, "name": looked_up["name"], "address": looked_up["address"],
          "lat": looked_up["lat"], "lon": looked_up["lon"], "cached": False,
      }
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_composed.py -v
  ```
  Expected: `3 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/tools/composed.py tests/test_tools_composed.py
  git commit -m "Add resolve_and_save_place (resolve once, reuse cached coordinates)"
  ```

---

### Task 2: Native Telegram location card

**Satisfies:** R9

**Files:**
- Modify: `bot/tools/composed.py`
- Modify: `tests/test_tools_composed.py`

**Interfaces:**
- Consumes: `places` table (S1), a `telegram.Bot`-like object with
  `async send_venue(chat_id, latitude, longitude, title, address)`.
- Produces: `async bot.tools.composed.send_location(pool, telegram_bot, session_id, chat_id, place_name) -> dict`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_tools_composed.py`:
  ```python
  async def test_send_location_sends_venue_for_saved_place(db_pool):
      session_id = await _new_session(db_pool)
      await db_pool.execute(
          "INSERT INTO places (session_id, name, address, lat, lon) VALUES ($1, 'Hanania Meadow', 'Route 1', 32.79, 35.05)",
          session_id,
      )
      telegram_bot = AsyncMock()

      result = await composed.send_location(db_pool, telegram_bot, session_id, chat_id=1, place_name="hanania meadow")

      assert result == {"status": "ok"}
      telegram_bot.send_venue.assert_awaited_once_with(
          chat_id=1, latitude=32.79, longitude=35.05, title="Hanania Meadow", address="Route 1",
      )


  async def test_send_location_not_found_for_unresolved_place(db_pool):
      session_id = await _new_session(db_pool)
      telegram_bot = AsyncMock()

      result = await composed.send_location(db_pool, telegram_bot, session_id, chat_id=1, place_name="nowhere")

      assert result == {"status": "not_found"}
      telegram_bot.send_venue.assert_not_awaited()
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_composed.py -v
  ```
  Expected: `AttributeError: module 'bot.tools.composed' has no attribute 'send_location'`.

- [ ] **Step 3: Append to `bot/tools/composed.py`**
  ```python
  async def send_location(pool, telegram_bot, session_id, chat_id, place_name) -> dict:
      row = await pool.fetchrow(
          "SELECT name, address, lat, lon FROM places WHERE session_id = $1 AND lower(name) = lower($2)",
          session_id, place_name,
      )
      if row is None:
          return {"status": "not_found"}

      await telegram_bot.send_venue(
          chat_id=chat_id, latitude=row["lat"], longitude=row["lon"],
          title=row["name"], address=row["address"] or "",
      )
      return {"status": "ok"}
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_composed.py -v
  ```
  Expected: `5 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/tools/composed.py tests/test_tools_composed.py
  git commit -m "Add send_location: native Telegram venue card for a resolved place"
  ```

---

### Task 3: "As usual" recency-weighted archive lookup

**Satisfies:** R8

**Files:**
- Modify: `bot/tools/composed.py`
- Modify: `tests/test_tools_composed.py`

**Interfaces:**
- Consumes: `places`/`sessions` tables (S1, closed sessions only).
- Produces:
  - `async bot.tools.composed.archive_lookup(pool, chat_id, activity_type) -> dict` —
    `{"pattern": "no_history" | "no_pattern" | "dominant" | "tied", "places": [...]}`,
    each place entry `{"name", "visit_count", "last_visited", "score"}`.
  - `bot.tools.composed.build_composed_registry(pool, telegram_bot) -> dict[str, Callable]` —
    consumed by S9.

  **Ranking rule (concrete, not left to the model):** each visit
  contributes `0.5 ** (days_since_visit / 180)` to that place's score —
  a visit 180 days ago counts half as much as one today, so recency
  measurably outweighs raw count. A place is **dominant** if it's the
  only one, or its score is at least double the runner-up's. Otherwise,
  places within 80% of the top score are **tied** and shown together.
  If every place in the archive has exactly one visit, the pattern is
  **no_pattern** and the full list is returned unranked (R8's "no
  meaningful pattern" case).

- [ ] **Step 1: Write the failing test** — append to `tests/test_tools_composed.py`:
  ```python
  import datetime as dt


  async def _closed_session_with_place(db_pool, chat_id, activity_type, place_name, days_ago):
      await db_pool.execute(
          "INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id
      )
      visited_on = dt.date.today() - dt.timedelta(days=days_ago)
      row = await db_pool.fetchrow(
          """
          INSERT INTO sessions (chat_id, activity_type, status, event_date, closed_at)
          VALUES ($1, $2, 'closed', $3, now()) RETURNING id
          """,
          chat_id, activity_type, visited_on,
      )
      await db_pool.execute(
          "INSERT INTO places (session_id, name, lat, lon) VALUES ($1, $2, 0, 0)",
          row["id"], place_name,
      )


  async def test_archive_lookup_no_history(db_pool):
      result = await composed.archive_lookup(db_pool, chat_id=1, activity_type="picnic")

      assert result == {"pattern": "no_history", "places": []}


  async def test_archive_lookup_dominant_place_wins_on_recency(db_pool):
      # Visited 5 times but stale (2 years ago) vs 1 recent visit — recency wins.
      for _ in range(5):
          await _closed_session_with_place(db_pool, 1, "picnic", "Old Spot", days_ago=730)
      await _closed_session_with_place(db_pool, 1, "picnic", "Fresh Spot", days_ago=10)

      result = await composed.archive_lookup(db_pool, chat_id=1, activity_type="picnic")

      assert result["pattern"] == "dominant"
      assert result["places"][0]["name"] == "Fresh Spot"


  async def test_archive_lookup_tied_shows_top_options(db_pool):
      await _closed_session_with_place(db_pool, 1, "picnic", "Spot A", days_ago=10)
      await _closed_session_with_place(db_pool, 1, "picnic", "Spot A", days_ago=20)
      await _closed_session_with_place(db_pool, 1, "picnic", "Spot B", days_ago=12)
      await _closed_session_with_place(db_pool, 1, "picnic", "Spot B", days_ago=22)

      result = await composed.archive_lookup(db_pool, chat_id=1, activity_type="picnic")

      assert result["pattern"] == "tied"
      assert {p["name"] for p in result["places"]} == {"Spot A", "Spot B"}


  async def test_archive_lookup_no_pattern_when_every_place_visited_once(db_pool):
      await _closed_session_with_place(db_pool, 1, "picnic", "Spot A", days_ago=100)
      await _closed_session_with_place(db_pool, 1, "picnic", "Spot B", days_ago=200)
      await _closed_session_with_place(db_pool, 1, "picnic", "Spot C", days_ago=300)

      result = await composed.archive_lookup(db_pool, chat_id=1, activity_type="picnic")

      assert result["pattern"] == "no_pattern"
      assert len(result["places"]) == 3


  async def test_archive_lookup_ignores_other_activity_types_and_open_sessions(db_pool):
      await _closed_session_with_place(db_pool, 1, "birthday", "Wrong Activity", days_ago=1)
      session_id = await _new_session(db_pool, chat_id=1, activity_type="picnic", status="active")
      await db_pool.execute(
          "INSERT INTO places (session_id, name, lat, lon) VALUES ($1, 'Still Open', 0, 0)", session_id
      )

      result = await composed.archive_lookup(db_pool, chat_id=1, activity_type="picnic")

      assert result == {"pattern": "no_history", "places": []}


  def test_build_composed_registry_covers_every_composed_tool(db_pool):
      registry = composed.build_composed_registry(db_pool, AsyncMock())

      assert set(registry) == {"resolve_and_save_place", "send_location", "archive_lookup"}
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_composed.py -v
  ```
  Expected: `AttributeError: module 'bot.tools.composed' has no attribute 'archive_lookup'`.

- [ ] **Step 3: Append to `bot/tools/composed.py`**
  ```python
  import functools
  from datetime import date

  _HALF_LIFE_DAYS = 180


  async def archive_lookup(pool, chat_id, activity_type) -> dict:
      rows = await pool.fetch(
          """
          SELECT pl.name, COALESCE(s.event_date, s.closed_at::date) AS visited_on
          FROM places pl
          JOIN sessions s ON s.id = pl.session_id
          WHERE s.chat_id = $1 AND s.activity_type = $2 AND s.status = 'closed'
          """,
          chat_id, activity_type,
      )
      if not rows:
          return {"pattern": "no_history", "places": []}

      today = date.today()
      visits_by_name: dict[str, list[date]] = {}
      for r in rows:
          visits_by_name.setdefault(r["name"], []).append(r["visited_on"])

      places = []
      for name, visits in visits_by_name.items():
          score = sum(0.5 ** ((today - v).days / _HALF_LIFE_DAYS) for v in visits)
          places.append({
              "name": name,
              "visit_count": len(visits),
              "last_visited": max(visits).isoformat(),
              "score": round(score, 4),
          })
      places.sort(key=lambda p: p["score"], reverse=True)

      if all(p["visit_count"] == 1 for p in places):
          return {"pattern": "no_pattern", "places": places}

      top = places[0]
      if len(places) == 1 or top["score"] >= 2 * places[1]["score"]:
          return {"pattern": "dominant", "places": [top]}

      tied = [p for p in places if p["score"] >= top["score"] * 0.8]
      return {"pattern": "tied", "places": tied}


  def build_composed_registry(pool, telegram_bot) -> dict:
      return {
          "resolve_and_save_place": functools.partial(resolve_and_save_place, pool),
          "send_location": functools.partial(send_location, pool, telegram_bot),
          "archive_lookup": functools.partial(archive_lookup, pool),
      }
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_composed.py -v
  ```
  Expected: `11 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/tools/composed.py tests/test_tools_composed.py
  git commit -m "Add recency-weighted archive_lookup and build_composed_registry"
  ```
