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
    rendered = {"type": _TYPES[schema.type]}
    if schema.description:
        rendered["description"] = schema.description
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
