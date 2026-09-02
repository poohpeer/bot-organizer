"""A schema rendered for strict structured outputs must not ask for omissions.

bot/ai/classify.py puts every declared property into `required`, because
Groq's strict mode demands it. A field description that tells the model to
*omit* the field therefore contradicts the schema it travels with — and the
model obeying the description is what breaks the request, not what saves it.

Live at 14:22 today, asked for an activity the title did not name:

    groq answered  {"event_date": "2026-08-31", "place": "Маленькая прага"}
    groq rejected  json_validate_failed: missing properties: 'activity_type'

Exactly the right extraction, thrown away by the rule its own prompt told it
to break. The whole chain then 400ed and a chat title change was lost.

This holds the two ends together: every schema that goes through extract()
has to say "empty string", never "omit".
"""

import pytest

import bot.ai.classify as classify
import bot.group_info as group_info
import bot.router as router

# Every schema passed to extract(). A new one added without being listed here
# is the gap this file exists to close, so the list is asserted against the
# modules rather than trusted.
SCHEMAS = {
    "group_info._EVENT_SCHEMA": group_info._EVENT_SCHEMA,
    "router._START_SCHEMA": router._START_SCHEMA,
    "router._SILENT_CAPTURE_SCHEMA": router._SILENT_CAPTURE_SCHEMA,
    "router._YES_NO_UNRELATED_SCHEMA": router._YES_NO_UNRELATED_SCHEMA,
    "router._INTENT_SCHEMA": router._INTENT_SCHEMA,
    "classify._BOOL_SCHEMA": classify._BOOL_SCHEMA,
}

FORBIDDEN = ("omit", "otherwise omitted", "leave it out", "leave out")


def _descriptions(rendered: dict):
    """Every description string anywhere in a rendered JSON schema."""
    if isinstance(rendered, dict):
        if isinstance(rendered.get("description"), str):
            yield rendered["description"]
        for value in rendered.values():
            yield from _descriptions(value)
    elif isinstance(rendered, list):
        for item in rendered:
            yield from _descriptions(item)


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_no_field_asks_the_model_to_omit_it(name):
    rendered = classify._json_schema_of(SCHEMAS[name])

    for description in _descriptions(rendered):
        lowered = description.lower()
        for phrase in FORBIDDEN:
            assert phrase not in lowered, (
                f"{name} tells the model to {phrase!r}, but every property is in "
                f"`required` — the model doing as it is told gets the request "
                f"rejected. Say 'empty string' instead. Description: {description!r}"
            )


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_property_is_required_and_closed(name):
    """The other half of the contract. If this ever stops being true, the
    test above is guarding a rule that no longer exists."""
    rendered = classify._json_schema_of(SCHEMAS[name])

    assert rendered["required"] == list(rendered["properties"]), \
        "strict mode requires every declared property to be listed"
    assert rendered["additionalProperties"] is False


def test_the_list_above_covers_every_schema_extract_is_called_with():
    """A schema added later and not listed here would never be checked, which
    is precisely how the contradiction got in."""
    import ast
    import pathlib

    found = set()
    for path in pathlib.Path(".").glob("bot/**/*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "extract"):
                continue
            if len(node.args) < 3:
                continue
            schema_arg = node.args[2]
            if isinstance(schema_arg, ast.Name):
                found.add(schema_arg.id)
            elif isinstance(schema_arg, ast.Attribute):
                found.add(schema_arg.attr)

    listed = {name.split(".")[-1] for name in SCHEMAS}
    assert found <= listed, f"schemas passed to extract() but not checked: {found - listed}"
