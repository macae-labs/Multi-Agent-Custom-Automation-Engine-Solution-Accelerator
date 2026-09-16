"""Aparcar y reanudar sobre request_info (incremento 2, clarificación): waiting_for
durable en el plan, aviso a la UI, y reanudación por checkpoint con la respuesta."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agent_framework import Content

import common.database.database_factory as database_factory
import common.services.chat_cosmos_service as chat_cosmos_service
import v4.orchestration.orchestration_manager as orchestration_manager
from v4.models.messages import WebsocketMessageType
from v4.orchestration.orchestration_manager import OrchestrationManager


class FakeStore:
    def __init__(self, plan) -> None:
        self.plan = plan
        self.updates: list = []

    async def get_plan_by_plan_id(self, plan_id):
        return self.plan if self.plan.plan_id == plan_id else None

    async def update_plan(self, plan):
        self.updates.append(dict(plan.waiting_for or {}))


@pytest.fixture
def parked(monkeypatch):
    plan = SimpleNamespace(plan_id="p1", team_id="t1", session_id="s1", waiting_for=None)
    store = FakeStore(plan)
    monkeypatch.setattr(database_factory.DatabaseFactory, "get_database", AsyncMock(return_value=store))
    checkpoint = SimpleNamespace(checkpoint_id="cp-9", pending_request_info_events={"req-1": object()})
    monkeypatch.setattr(orchestration_manager, "get_checkpoint_storage", lambda: SimpleNamespace(get_latest=AsyncMock(return_value=checkpoint)))
    sender = Mock()
    sender.send_status_update_async = AsyncMock()
    monkeypatch.setattr(orchestration_manager, "connection_config", sender)
    chat = Mock()
    chat.add_message = AsyncMock()
    monkeypatch.setattr(chat_cosmos_service, "get_chat_cosmos_service", AsyncMock(return_value=chat))
    return SimpleNamespace(plan=plan, store=store, sender=sender, chat=chat)


def _event(request_id="req-1", content_id="c-1", question="¿Cuál?"):
    return SimpleNamespace(request_id=request_id, data=Content("text", text=question, id=content_id, user_input_request=True))


@pytest.mark.asyncio
async def test_park_persists_waiting_for_and_notifies_the_ui(parked):
    await OrchestrationManager()._park_on_request_info(
        workflow=SimpleNamespace(name="wf-1"), event=_event(), user_id="u1", session_id="s1", plan_id="p1", workspace_id="ws-1"
    )
    assert parked.plan.waiting_for == {
        "kind": "clarification", "request_id": "req-1", "checkpoint_id": "cp-9", "workflow_name": "wf-1",
        "question": "¿Cuál?", "content_id": "c-1", "workspace_id": "ws-1", "team_id": "t1",
    }
    assert parked.store.updates == [parked.plan.waiting_for]
    parked.sender.send_status_update_async.assert_awaited_once_with(
        {"question": "¿Cuál?", "request_id": "req-1"}, user_id="u1", message_type=WebsocketMessageType.USER_CLARIFICATION_REQUEST
    )
    assert parked.chat.add_message.await_args.kwargs["metadata"]["clarification_id"] == "req-1"


@pytest.mark.asyncio
async def test_park_refuses_a_request_the_checkpoint_does_not_hold(parked):
    with pytest.raises(RuntimeError, match="sin checkpoint"):
        await OrchestrationManager()._park_on_request_info(
            workflow=SimpleNamespace(name="wf-1"), event=_event(request_id="other"), user_id="u1", session_id="s1", plan_id="p1", workspace_id=None
        )


@pytest.mark.asyncio
async def test_resume_restores_the_checkpoint_with_the_answer_and_clears_waiting_for(parked, monkeypatch):
    parked.plan.waiting_for = {"kind": "clarification", "request_id": "req-1", "checkpoint_id": "cp-9", "workspace_id": "ws-1"}
    manager = OrchestrationManager()
    run = AsyncMock()
    monkeypatch.setattr(manager, "run_orchestration", run)
    await manager.resume_orchestration("u1", "s1", "p1", "req-1", Content.from_text("42"))
    assert parked.plan.waiting_for is None and parked.store.updates[-1] == {}
    kwargs = run.await_args.kwargs
    assert kwargs["workspace_id"] == "ws-1" and kwargs["_resume"]["checkpoint_id"] == "cp-9"
    [(request_id, content)] = kwargs["_resume"]["responses"].items()
    assert request_id == "req-1" and content.type == "text" and content.text == "42"


@pytest.mark.asyncio
async def test_resume_refuses_a_request_the_plan_is_not_waiting_for(parked):
    parked.plan.waiting_for = {"kind": "clarification", "request_id": "req-1", "checkpoint_id": "cp-9"}
    with pytest.raises(ValueError, match="not waiting for request"):
        await OrchestrationManager().resume_orchestration("u1", "s1", "p1", "req-X", Content.from_text("42"))
