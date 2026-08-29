"""The MCP endpoint, and the grant that keeps one chat out of another's data."""

import json
import time
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

import bot.mcp_server as mcp
from bot.tools.schema import ALL_TOOLS


class _User:
    def __init__(self, user_id=7, name="Alex"):
        self.id = user_id
        self.full_name = name
        self.first_name = name
        self.username = None


def _registry_spy(calls):
    """Stands in for bot.router._build_registry, recording how it was bound."""

    def build(session_id, current_user):
        async def tool(**kwargs):
            calls.append({"session_id": session_id,
                          "user_id": getattr(current_user, "id", None),
                          "kwargs": kwargs})
            return {"status": "ok"}

        return {"list_add": tool, "get_facts": tool}

    return build


@pytest.fixture
async def client(aiohttp_client):
    async def make(grants, calls=None):
        build = _registry_spy(calls if calls is not None else [])
        app = mcp.build_app(build, ALL_TOOLS.function_declarations, grants)
        return await aiohttp_client(app)

    return make


async def _rpc(cli, token, method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return await cli.post(f"/mcp/{token}", json=body)


# --- the grant ------------------------------------------------------------

def test_a_grant_carries_the_session_and_the_person():
    grants = mcp.GrantStore()
    token = grants.issue(41, _User(user_id=7, name="Alex"))

    grant = grants.resolve(token)

    assert (grant.session_id, grant.user_id, grant.display_name) == (41, 7, "Alex")


def test_two_grants_are_not_guessable_from_each_other():
    grants = mcp.GrantStore()

    first, second = grants.issue(1), grants.issue(1)

    assert first != second
    assert len(first) > 30, "a short token is a guessable token"


def test_a_grant_expires():
    """A leaked token is worth as little as possible."""
    grants = mcp.GrantStore(ttl_seconds=0)
    token = grants.issue(41)
    time.sleep(0.01)

    assert grants.resolve(token) is None


def test_a_revoked_grant_stops_working_at_once():
    grants = mcp.GrantStore()
    token = grants.issue(41)

    grants.revoke(token)

    assert grants.resolve(token) is None


# --- access control -------------------------------------------------------

async def test_an_unknown_token_is_refused(client):
    cli = await client(mcp.GrantStore())

    response = await _rpc(cli, "not-a-real-token", "tools/list")

    assert response.status == 403


async def test_an_expired_token_is_refused(client):
    grants = mcp.GrantStore(ttl_seconds=0)
    token = grants.issue(41)
    cli = await client(grants)
    time.sleep(0.01)

    response = await _rpc(cli, token, "tools/list")

    assert response.status == 403


async def test_the_grant_decides_the_session_not_the_caller(client):
    """The security property this whole design exists for. bot/router.py never
    trusts a session_id from the model; over a network that stops being a
    correctness measure and becomes an access-control one, because a caller
    that could name its own session could write into another group's chat."""
    calls = []
    grants = mcp.GrantStore()
    token = grants.issue(41, _User())
    cli = await client(grants, calls)

    await _rpc(cli, token, "tools/call", {
        "name": "list_add",
        "arguments": {"name": "пиво", "session_id": 999},
    })

    assert calls[0]["session_id"] == 41
    assert calls[0]["kwargs"].get("session_id") == 41, "the caller's 999 must not survive"


async def test_the_grant_decides_who_is_talking(client):
    calls = []
    grants = mcp.GrantStore()
    token = grants.issue(41, _User(user_id=7))
    cli = await client(grants, calls)

    await _rpc(cli, token, "tools/call", {
        "name": "list_add", "arguments": {"name": "пиво", "current_user_id": 999},
    })

    assert calls[0]["user_id"] == 7
    assert "current_user_id" not in calls[0]["kwargs"]


# --- the protocol ---------------------------------------------------------

async def test_initialize_announces_tools(client):
    grants = mcp.GrantStore()
    token = grants.issue(41)
    cli = await client(grants)

    body = await (await _rpc(cli, token, "initialize")).json()

    assert body["result"]["capabilities"] == {"tools": {}}


async def test_tools_list_hides_the_bound_parameters(client):
    """A schema that mentions session_id invites a value for it, and the only
    correct value is the one the grant already carries."""
    grants = mcp.GrantStore()
    token = grants.issue(41)
    cli = await client(grants)

    body = await (await _rpc(cli, token, "tools/list")).json()

    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert "list_add" in tools
    for tool in tools.values():
        assert "session_id" not in tool["inputSchema"]["properties"], tool["name"]
        assert "session_id" not in tool["inputSchema"]["required"], tool["name"]


async def test_a_tool_call_returns_its_result_as_text(client):
    grants = mcp.GrantStore()
    token = grants.issue(41)
    cli = await client(grants)

    body = await (await _rpc(cli, token, "tools/call", {
        "name": "list_add", "arguments": {"name": "пиво"},
    })).json()

    assert json.loads(body["result"]["content"][0]["text"]) == {"status": "ok"}


async def test_an_unknown_tool_is_refused_not_dispatched(client):
    grants = mcp.GrantStore()
    token = grants.issue(41)
    cli = await client(grants)

    body = await (await _rpc(cli, token, "tools/call", {"name": "rm_rf", "arguments": {}})).json()

    assert body["result"]["isError"] is True
    assert "unknown tool" in body["result"]["content"][0]["text"]


async def test_a_failing_tool_ends_the_call_not_the_conversation(aiohttp_client):
    """The same discipline bot/ai/tool_loop.py already follows."""
    grants = mcp.GrantStore()
    token = grants.issue(41)

    def build(session_id, current_user):
        return {"list_add": AsyncMock(side_effect=RuntimeError("database is on fire"))}

    app = mcp.build_app(build, ALL_TOOLS.function_declarations, grants)
    cli = await aiohttp_client(app)

    body = await (await _rpc(cli, token, "tools/call", {"name": "list_add", "arguments": {}})).json()

    assert body["result"]["isError"] is True
    assert "database is on fire" in body["result"]["content"][0]["text"]


async def test_a_notification_gets_no_body(client):
    """It has no id, so JSON-RPC says there is nothing to answer."""
    grants = mcp.GrantStore()
    token = grants.issue(41)
    cli = await client(grants)

    response = await cli.post(f"/mcp/{token}",
                              json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert response.status == 202
