"""Renders the tool declarations in `bot.tools.schema` as OpenAI-style tools.

The declarations are written once, in Gemini's `FunctionDeclaration` form, and
the epic requires exactly one place where a tool is described. Groq speaks the
OpenAI dialect, so rather than maintaining a second hand-written copy — which
would drift, and drift silently — the single source is translated here.
"""

from google.genai import types

from bot.tools.schema import ALL_TOOLS

_TYPES = {
    types.Type.STRING: "string",
    types.Type.INTEGER: "integer",
    types.Type.NUMBER: "number",
    types.Type.BOOLEAN: "boolean",
    types.Type.OBJECT: "object",
    types.Type.ARRAY: "array",
}


def _property(schema: types.Schema) -> dict:
    """One parameter, in the OpenAI dialect.

    Renders nested shapes too. It used to stop at type and description, which
    was enough while every parameter was a scalar — the first array-of-objects
    declared (list_add's amounts) came out as a bare {"type": "array"} with no
    item schema at all, leaving the model to guess what belongs inside.
    """
    rendered = {"type": _TYPES[schema.type]}
    if schema.description:
        rendered["description"] = schema.description
    if schema.type == types.Type.ARRAY and schema.items is not None:
        rendered["items"] = _property(schema.items)
    if schema.type == types.Type.OBJECT:
        rendered["properties"] = {
            name: _property(prop) for name, prop in (schema.properties or {}).items()
        }
        rendered["required"] = list(schema.required or [])
    return rendered


def _function(declaration) -> dict:
    parameters = declaration.parameters
    return {
        "type": "function",
        "function": {
            "name": declaration.name,
            "description": declaration.description,
            "parameters": {
                "type": "object",
                "properties": {
                    name: _property(schema)
                    for name, schema in (parameters.properties or {}).items()
                },
                "required": list(parameters.required or []),
            },
        },
    }


def openai_tools() -> list[dict]:
    return [_function(d) for d in ALL_TOOLS.function_declarations]
