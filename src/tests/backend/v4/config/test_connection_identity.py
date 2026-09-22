"""A plan's socket receives only that plan's messages: identity, no user map, no queue.

Measured 2026-09-22: the per-user pending queue flushed plan 988b2933's approval
request onto the freshly opened socket of plan 7804a8f4, and the user→process
map put a1e2fd6f's approval request on 6b204c5e's page, whose approve then
answered 404 "No active plan found for approval".
"""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from v4.config.settings import ConnectionConfig
from v4.models.messages import WebsocketMessageType


def _socket():
    s = Mock()
    s.send_text = AsyncMock()
    s.close = AsyncMock()
    return s


@pytest.mark.asyncio
async def test_a_message_reaches_only_the_socket_of_its_plan():
    cc = ConnectionConfig()
    a, b = _socket(), _socket()
    cc.add_connection("plan-a", a, "u1")
    cc.add_connection("plan-b", b, "u1")

    await cc.send_status_update_async(
        {"question": "¿Qué trimestre?", "request_id": "req-1"},
        "u1",
        WebsocketMessageType.USER_CLARIFICATION_REQUEST,
        process_id="plan-a",
    )

    a.send_text.assert_awaited_once()
    b.send_text.assert_not_called()
    payload = json.loads(a.send_text.await_args.args[0])
    assert payload["type"] == "user_clarification_request"
    assert payload["data"]["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_a_plan_without_socket_is_never_served_to_another_socket_later():
    cc = ConnectionConfig()
    await cc.send_status_update_async(
        {"plan": "old"},
        "u1",
        WebsocketMessageType.PLAN_APPROVAL_REQUEST,
        process_id="plan-old",
    )
    fresh = _socket()
    cc.add_connection("plan-new", fresh, "u1")

    fresh.send_text.assert_not_called()
    assert not hasattr(cc, "pending_messages")
    assert not hasattr(cc, "user_to_process")


@pytest.mark.asyncio
async def test_a_message_without_plan_identity_is_not_delivered():
    cc = ConnectionConfig()
    s = _socket()
    cc.add_connection("plan-a", s, "u1")

    await cc.send_status_update_async(
        {"content": "x"}, "u1", WebsocketMessageType.AGENT_MESSAGE
    )

    s.send_text.assert_not_called()


def test_two_plans_of_one_user_keep_their_own_sockets():
    cc = ConnectionConfig()
    a, b = _socket(), _socket()
    cc.add_connection("plan-a", a, "u1")
    cc.add_connection("plan-b", b, "u1")

    a.close.assert_not_called()
    assert cc.get_connection("plan-a") is a
    assert cc.get_connection("plan-b") is b


@pytest.mark.asyncio
async def test_a_new_socket_for_the_same_plan_replaces_the_old_one():
    import asyncio

    cc = ConnectionConfig()
    old, new = _socket(), _socket()
    cc.add_connection("plan-a", old, "u1")
    cc.add_connection("plan-a", new, "u1")
    await asyncio.sleep(0)  # let the scheduled close of the old socket run

    assert cc.get_connection("plan-a") is new
    old.close.assert_awaited_once()
