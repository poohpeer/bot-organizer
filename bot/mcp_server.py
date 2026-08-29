"""The bot's tools, offered to a CLI model over MCP.

Groq and Gemini receive tool declarations in the request and hand back tool
calls in the response, so bot/ai/tool_loop.py can run the loop. The Codex and
Claude CLIs cannot: they drive their own loop and reach for tools themselves,
through MCP. To use them at all, the tools have to be somewhere they can
call — and since every one of them touches this bot's database, that
somewhere is this process, not ai-proxy.

Hence an HTTP server here, and a grant to go with it. The alternative — a
tool-relay through ai-proxy — would need CLI sessions kept alive between
stateless HTTP requests; this keeps ai-proxy stateless and the loop where
the CLI already runs it.

Security is the whole design. Every tool is scoped to one session, and
bot/router.py deliberately never trusts a session_id from the model
(_SESSION_BOUND_TOOLS). Over a network that stops being a correctness
measure and becomes an access-control one: anything that could name its own
session could write into another group's chat. So:

  * a caller presents a grant token, and the token — not the request body —
    decides which session and which user the call runs as;
  * grants are unguessable, short-lived, and issued per model request;
  * session_id and the current user are stripped from the advertised schema
    entirely, so there is nothing for a caller to set;
  * a tool that is not in the registry is refused rather than dispatched.
"""

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass

from aiohttp import web

log = logging.getLogger(__name__)

MCP_PORT = int(os.environ.get("MCP_PORT", "8081"))

# Long enough for one model turn including its tool calls, short enough that a
# leaked token is worth little. A turn that outlives this fails its remaining
# tool calls, which is the safe direction.
GRANT_TTL_SECONDS = int(os.environ.get("MCP_GRANT_TTL_SECONDS", "600"))

# Never advertised and never accepted from a caller. bot/router.py binds both
# from the message being handled; exposing them here would hand the one
# decision that keeps chats apart to whoever holds a token.
_BOUND_PARAMETERS = frozenset({"session_id", "current_user_id"})


@dataclass(frozen=True)
class Grant:
    session_id: int
    user_id: int | None
    display_name: str | None
    expires_at: float


class GrantStore:
    """Tokens a CLI presents to act as one session, for a short while.

    In memory on purpose: the bot runs as a single pod with a Recreate
    strategy, so there is no second process to share with, and a grant that
    does not survive a restart is a grant that cannot be replayed after one.
    """

    def __init__(self, ttl_seconds: int = GRANT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._grants: dict[str, Grant] = {}

    def issue(self, session_id: int, current_user=None) -> str:
        self._drop_expired()
        token = secrets.token_urlsafe(32)
        self._grants[token] = Grant(
            session_id=session_id,
            user_id=getattr(current_user, "id", None),
            display_name=_display_name_of(current_user),
            expires_at=time.monotonic() + self._ttl,
        )
        return token

    def resolve(self, token: str) -> Grant | None:
        self._drop_expired()
        return self._grants.get(token)

    def revoke(self, token: str) -> None:
        """Called when a turn ends, so a token outlives its use by as little
        as possible rather than sitting until its TTL."""
        self._grants.pop(token, None)

    def _drop_expired(self) -> None:
        now = time.monotonic()
        for token, grant in list(self._grants.items()):
            if grant.expires_at <= now:
                del self._grants[token]


def _display_name_of(user) -> str | None:
    if user is None:
        return None
    name = getattr(user, "full_name", None) or getattr(user, "first_name", None)
    return name or getattr(user, "username", None)


def advertised_tools(declarations) -> list[dict]:
    """The tool list an MCP client sees.

    Router-bound parameters are removed rather than left for the caller to
    fill in: a schema that mentions session_id invites a value for it, and
    the only correct value is the one the grant already carries.
    """
    from bot.ai.tool_schema_openai import _function

    tools = []
    for declaration in declarations:
        rendered = _function(declaration)["function"]
        parameters = rendered.get("parameters") or {}
        properties = {
            name: schema for name, schema in (parameters.get("properties") or {}).items()
            if name not in _BOUND_PARAMETERS
        }
        tools.append({
            "name": rendered["name"],
            "description": rendered.get("description") or "",
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": [
                    name for name in (parameters.get("required") or [])
                    if name not in _BOUND_PARAMETERS
                ],
            },
        })
    return tools


def _session_bound_tools() -> frozenset:
    """The router's own list, not a copy of it.

    Imported here rather than at module scope only to keep the import one
    directional — bot/router.py knows nothing about this module, and a copy
    of the list would be a second place to forget a new tool.
    """
    from bot.router import _SESSION_BOUND_TOOLS

    return _SESSION_BOUND_TOOLS


_PROTOCOL_VERSION = "2024-11-05"


def _result(msg_id, result) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id, code: int, message: str) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": msg_id,
                              "error": {"code": code, "message": message}})


def build_app(build_registry, declarations, grants: GrantStore) -> web.Application:
    """An MCP endpoint whose tools run as whoever the grant says.

    build_registry(session_id, current_user) is bot.router._build_registry —
    passed in rather than imported so the binding rules have exactly one
    implementation, the one the Groq and Gemini paths already go through.
    """

    async def handle(request: web.Request) -> web.Response:
        grant = grants.resolve(request.match_info["token"])
        if grant is None:
            # No detail: a caller with a bad token learns only that it is bad.
            log.warning("MCP call with an unknown or expired grant")
            return web.json_response({"error": "unknown or expired grant"}, status=403)

        try:
            message = await request.json()
        except (json.JSONDecodeError, ValueError):
            return _error(None, -32700, "parse error")
        if not isinstance(message, dict):
            return _error(None, -32600, "invalid request")

        method, msg_id = message.get("method"), message.get("id")
        if msg_id is None:
            # A notification wants no reply at all, only an acknowledgement.
            return web.Response(status=202)

        if method == "initialize":
            return _result(msg_id, {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "bot-organizer", "version": "1.0"},
            })
        if method == "tools/list":
            return _result(msg_id, {"tools": advertised_tools(declarations)})
        if method == "tools/call":
            return await _call_tool(message, msg_id, grant, build_registry)
        return _error(msg_id, -32601, f"method not found: {method}")

    async def _call_tool(message, msg_id, grant: Grant, build_registry) -> web.Response:
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}

        registry = build_registry(grant.session_id, _UserOfGrant(grant))
        tool = registry.get(name)
        if tool is None:
            # Refused rather than dispatched: an unknown name is either a
            # stale client or someone probing.
            log.warning("MCP call for unknown tool %r", name)
            return _result(msg_id, {
                "content": [{"type": "text", "text": f"unknown tool {name!r}"}],
                "isError": True,
            })

        # Whatever the caller sent for these, the grant decides. Dropping them
        # here as well as from the advertised schema means a client that
        # invents the parameter still cannot use it.
        arguments = {k: v for k, v in arguments.items() if k not in _BOUND_PARAMETERS}
        if name in _session_bound_tools():
            arguments["session_id"] = grant.session_id

        try:
            result = await tool(**arguments)
        except Exception as e:
            # Reported to the model rather than raised: a failing tool must
            # end that call, not the conversation — the same discipline
            # bot/ai/tool_loop.py already follows.
            log.exception("MCP tool %s failed", name)
            return _result(msg_id, {
                "content": [{"type": "text", "text": f"error: {e}"}], "isError": True,
            })

        return _result(msg_id, {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]
        })

    app = web.Application()
    app.router.add_post("/mcp/{token}", handle)
    return app


class _UserOfGrant:
    """What the grant knows about who is talking, shaped like the telegram
    user the router would otherwise pass."""

    def __init__(self, grant: Grant) -> None:
        self.id = grant.user_id
        self.full_name = grant.display_name
        self.first_name = grant.display_name
        self.username = None


async def serve(build_registry, declarations, grants: GrantStore, port: int = MCP_PORT):
    """Start the endpoint alongside the polling loop and leave it running."""
    runner = web.AppRunner(build_app(build_registry, declarations, grants))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("MCP server listening on :%s", port)
    return runner
