# Story S5: External grounding tools — web search, maps, weather

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** Give the model grounded, honest external lookups — search,
place resolution, and weather — that say "not found" instead of
fabricating an answer when a source has nothing.
**Satisfies:** R6, R7, R9, R10
**Depends on:** S2
**Parallel-safe with:** S3, S4
**Requirements & global constraints:** see `../EPIC.md`

---

### Task 1: Web search

**Satisfies:** R6, R10

**Files:**
- Create: `bot/tools/external.py`
- Create: `tests/test_tools_external.py`

**Interfaces:**
- Consumes: `bot.ai.client.gemini`, `bot.ai.client.MODELS` (S2).
- Produces: `async bot.tools.external.web_search(query: str) -> dict` —
  `{"found": bool, "summary": str | None, "sources": list[str]}`.

- [ ] **Step 1: Write the failing test** — create `tests/test_tools_external.py`:
  ```python
  from unittest.mock import AsyncMock, MagicMock

  from google.genai import errors

  import bot.tools.external as external


  def _resp(text, sources=()):
      resp = MagicMock()
      resp.text = text
      chunk_mocks = []
      for uri in sources:
          chunk = MagicMock()
          chunk.web.uri = uri
          chunk_mocks.append(chunk)
      candidate = MagicMock()
      candidate.grounding_metadata.grounding_chunks = chunk_mocks
      resp.candidates = [candidate]
      return resp


  async def test_web_search_found_returns_summary_and_sources(monkeypatch):
      monkeypatch.setattr(
          external.gemini.aio.models, "generate_content",
          AsyncMock(return_value=_resp("Grilling is allowed on weekends.", ["https://example.com/rules"])),
      )

      result = await external.web_search("can we grill at Hanania meadow")

      assert result["found"] is True
      assert result["summary"] == "Grilling is allowed on weekends."
      assert result["sources"] == ["https://example.com/rules"]


  async def test_web_search_not_found_when_empty_text(monkeypatch):
      monkeypatch.setattr(
          external.gemini.aio.models, "generate_content",
          AsyncMock(return_value=_resp("")),
      )

      result = await external.web_search("something obscure nobody wrote about")

      assert result["found"] is False
      assert result["summary"] is None
      assert result["sources"] == []


  async def test_web_search_not_found_on_api_error(monkeypatch):
      async def boom(*args, **kwargs):
          raise errors.APIError(code=500, response_json={}, response=None)

      monkeypatch.setattr(external.gemini.aio.models, "generate_content", boom)

      result = await external.web_search("anything")

      assert result["found"] is False
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  uv run pytest tests/test_tools_external.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'bot.tools.external'`.

- [ ] **Step 3: Implement `bot/tools/external.py`**
  ```python
  import logging

  from google.genai import errors, types

  from bot.ai.client import MODELS, gemini

  log = logging.getLogger(__name__)


  def _extract_sources(resp) -> list[str]:
      sources = []
      for candidate in getattr(resp, "candidates", None) or []:
          gm = getattr(candidate, "grounding_metadata", None)
          if not gm:
              continue
          for chunk in gm.grounding_chunks or []:
              uri = getattr(getattr(chunk, "web", None), "uri", None)
              if uri:
                  sources.append(uri)
      return sources


  async def web_search(query: str) -> dict:
      """Grounded search via Gemini's built-in Google Search tool. Never
      invents an answer: an empty result or an API failure both come back
      as found=False rather than falling through to the model's own
      (possibly stale or wrong) knowledge."""
      try:
          resp = await gemini.aio.models.generate_content(
              model=MODELS[0],
              contents=query,
              config=types.GenerateContentConfig(
                  tools=[types.Tool(google_search=types.GoogleSearch())]
              ),
          )
      except errors.APIError:
          log.warning("web_search failed for query=%r", query, exc_info=True)
          return {"found": False, "summary": None, "sources": []}

      text = (resp.text or "").strip()
      if not text:
          return {"found": False, "summary": None, "sources": []}
      return {"found": True, "summary": text, "sources": _extract_sources(resp)}
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  uv run pytest tests/test_tools_external.py -v
  ```
  Expected: `3 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/tools/external.py tests/test_tools_external.py
  git commit -m "Add grounded web_search tool"
  ```

---

### Task 2: Maps and weather

**Satisfies:** R7, R9, R10

**Files:**
- Modify: `bot/tools/external.py`
- Modify: `tests/test_tools_external.py`

**Interfaces:**
- Consumes: `GOOGLE_MAPS_API_KEY` env var (fake value set in
  `tests/conftest.py` by S1).
- Produces:
  - `async bot.tools.external.maps_lookup(query: str) -> dict` —
    `{"found": bool, "name", "address", "lat", "lon", "rating", "review_snippets"}`.
  - `async bot.tools.external.weather_lookup(lat: float, lon: float, date: str) -> dict` —
    `{"found": bool, "weather_code", "temp_max_c", "temp_min_c", "precipitation_probability_max"}`.
  - `bot.tools.external.build_external_registry() -> dict[str, Callable]` —
    consumed by S9.

- [ ] **Step 1: Write the failing test** — append to `tests/test_tools_external.py`:
  ```python
  import httpx


  def _http_response(json_body):
      resp = MagicMock()
      resp.json.return_value = json_body
      return resp


  async def test_maps_lookup_found(monkeypatch):
      body = {
          "places": [{
              "displayName": {"text": "Hanania Meadow"},
              "formattedAddress": "Route 1, North District",
              "location": {"latitude": 32.79, "longitude": 35.05},
              "rating": 4.2,
              "reviews": [{"text": {"text": "Great spot, no official grilling though."}}],
          }]
      }
      monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=_http_response(body)))

      result = await external.maps_lookup("Hanania meadow")

      assert result["found"] is True
      assert result["name"] == "Hanania Meadow"
      assert result["lat"] == 32.79
      assert result["review_snippets"] == ["Great spot, no official grilling though."]


  async def test_maps_lookup_not_found(monkeypatch):
      monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=_http_response({"places": []})))

      result = await external.maps_lookup("a place nobody has heard of")

      assert result == {"found": False}


  async def test_weather_lookup_found(monkeypatch):
      body = {"daily": {
          "time": ["2026-09-01"],
          "weathercode": [3],
          "temperature_2m_max": [29.5],
          "temperature_2m_min": [21.0],
          "precipitation_probability_max": [10],
      }}
      monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(return_value=_http_response(body)))

      result = await external.weather_lookup(32.79, 35.05, "2026-09-01")

      assert result["found"] is True
      assert result["temp_max_c"] == 29.5
      assert result["precipitation_probability_max"] == 10


  async def test_weather_lookup_date_out_of_range(monkeypatch):
      monkeypatch.setattr(
          httpx.AsyncClient, "get",
          AsyncMock(return_value=_http_response({"daily": {"time": []}})),
      )

      result = await external.weather_lookup(32.79, 35.05, "2030-01-01")

      assert result == {"found": False}


  def test_build_external_registry_covers_every_external_tool():
      registry = external.build_external_registry()

      assert set(registry) == {"web_search", "maps_lookup", "weather_lookup"}
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  uv run pytest tests/test_tools_external.py -v
  ```
  Expected: `AttributeError: module 'bot.tools.external' has no attribute 'maps_lookup'`.

- [ ] **Step 3: Append to `bot/tools/external.py`**
  ```python
  import os

  import httpx

  _MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
  _TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
  _WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


  async def maps_lookup(query: str) -> dict:
      async with httpx.AsyncClient() as client:
          resp = await client.post(
              _TEXT_SEARCH_URL,
              json={"textQuery": query},
              headers={
                  "X-Goog-Api-Key": _MAPS_API_KEY,
                  "X-Goog-FieldMask": (
                      "places.displayName,places.formattedAddress,"
                      "places.location,places.rating,places.reviews"
                  ),
              },
          )
      places = resp.json().get("places") or []
      if not places:
          return {"found": False}

      p = places[0]
      return {
          "found": True,
          "name": p.get("displayName", {}).get("text"),
          "address": p.get("formattedAddress"),
          "lat": p["location"]["latitude"],
          "lon": p["location"]["longitude"],
          "rating": p.get("rating"),
          "review_snippets": [
              r.get("text", {}).get("text") for r in (p.get("reviews") or [])[:5]
          ],
      }


  async def weather_lookup(lat: float, lon: float, date: str) -> dict:
      async with httpx.AsyncClient() as client:
          resp = await client.get(_WEATHER_URL, params={
              "latitude": lat, "longitude": lon,
              "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
              "timezone": "auto", "start_date": date, "end_date": date,
          })
      daily = resp.json().get("daily") or {}
      dates = daily.get("time") or []
      if date not in dates:
          return {"found": False}

      idx = dates.index(date)
      return {
          "found": True,
          "weather_code": daily["weathercode"][idx],
          "temp_max_c": daily["temperature_2m_max"][idx],
          "temp_min_c": daily["temperature_2m_min"][idx],
          "precipitation_probability_max": daily["precipitation_probability_max"][idx],
      }


  def build_external_registry() -> dict:
      return {
          "web_search": web_search,
          "maps_lookup": maps_lookup,
          "weather_lookup": weather_lookup,
      }
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  uv run pytest tests/test_tools_external.py -v
  ```
  Expected: `8 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/tools/external.py tests/test_tools_external.py
  git commit -m "Add maps_lookup/weather_lookup tools and build_external_registry"
  ```
