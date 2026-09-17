"""Aparcar y reanudar sobre request_info (incremento 2b, aprobación de plan).

Mismo mecanismo que la clarificación (`_park_on_request_info` / `resume_orchestration`):
el `MagenticPlanReviewRequest` del framework se aparca en el checkpoint, `waiting_for`
(con el MPlan) se persiste en el plan, la UI recibe PLAN_APPROVAL_REQUEST, y la decisión
humana reanuda (`approve`) o cancela (`cancel_parked`). Nada espera en proceso.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agent_framework import Message
from agent_framework_orchestrations import (
    MagenticPlanReviewRequest,
    MagenticPlanReviewResponse,
)

import common.database.database_factory as database_factory
import v4.orchestration.orchestration_manager as orchestration_manager
from common.models.messages_af import PlanStatus
from v4.models.messages import WebsocketMessageType
from v4.models.models import MPlan
from v4.orchestration.orchestration_manager import OrchestrationManager


class FakeStore:
    def __init__(self, plan) -> None:
        self.plan = plan
        self.updates: list = []

    async def get_plan_by_plan_id(self, plan_id):
        return self.plan if self.plan.plan_id == plan_id else None

    async def update_plan(self, plan):
        self.updates.append((plan.overall_status, dict(plan.waiting_for or {})))


@pytest.fixture
def parked(monkeypatch):
    plan = SimpleNamespace(
        plan_id="p1",
        team_id="t1",
        session_id="s1",
        waiting_for=None, workflow_names=[],
        overall_status=PlanStatus.in_progress,
    )
    store = FakeStore(plan)
    monkeypatch.setattr(
        database_factory.DatabaseFactory,
        "get_database",
        AsyncMock(return_value=store),
    )
    checkpoint = SimpleNamespace(
        checkpoint_id="cp-9", pending_request_info_events={"req-1": object()}
    )
    monkeypatch.setattr(
        orchestration_manager,
        "get_checkpoint_storage",
        lambda: SimpleNamespace(get_latest=AsyncMock(return_value=checkpoint)),
    )
    sender = Mock()
    sender.send_status_update_async = AsyncMock()
    monkeypatch.setattr(orchestration_manager, "connection_config", sender)
    mplan = MPlan(id="m-1", user_id="u1", user_request="haz X")
    manager = SimpleNamespace(magentic_plan=mplan)
    monkeypatch.setattr(
        orchestration_manager.orchestration_config, "managers", {"u1": manager}
    )
    return SimpleNamespace(plan=plan, store=store, sender=sender, mplan=mplan)


def _event(request_id="req-1", is_stalled=False):
    return SimpleNamespace(
        request_id=request_id,
        data=MagenticPlanReviewRequest(
            plan=Message(role="assistant", text="plan"),
            current_progress=None,
            is_stalled=is_stalled,
        ),
    )


@pytest.mark.asyncio
async def test_park_persists_mplan_in_waiting_for_and_asks_the_ui_once(parked):
    await OrchestrationManager()._park_on_request_info(
        workflow=SimpleNamespace(name="wf-1"),
        event=_event(),
        user_id="u1",
        session_id="s1",
        plan_id="p1",
        workspace_id="ws-1",
    )
    wf = parked.plan.waiting_for
    assert wf["kind"] == "plan_review"
    assert wf["request_id"] == "req-1" and wf["checkpoint_id"] == "cp-9"
    assert wf["m_plan_id"] == "m-1" and wf["is_stalled"] is False
    assert wf["team_id"] == "t1" and wf["workspace_id"] == "ws-1"
    # The MPlan the UI saw is in the store (re-sent on WS reconnect).
    assert wf["m_plan"]["id"] == "m-1"
    assert wf["m_plan"]["plan_id"] == "p1" and wf["m_plan"]["team_id"] == "t1"
    # One durable write, complete: no half-written waiting_for.
    assert parked.store.updates == [(PlanStatus.in_progress, wf)]

    parked.sender.send_status_update_async.assert_awaited_once()
    kwargs = parked.sender.send_status_update_async.await_args.kwargs
    assert kwargs["message_type"] == WebsocketMessageType.PLAN_APPROVAL_REQUEST
    assert kwargs["user_id"] == "u1"
    msg = kwargs["message"]
    assert msg.plan is parked.mplan
    assert msg.context == {"request_id": "req-1", "is_stalled": False}


@pytest.mark.asyncio
async def test_park_refuses_a_plan_review_without_an_mplan(parked):
    orchestration_manager.orchestration_config.managers["u1"].magentic_plan = None
    with pytest.raises(RuntimeError, match="without an MPlan"):
        await OrchestrationManager()._park_on_request_info(
            workflow=SimpleNamespace(name="wf-1"),
            event=_event(),
            user_id="u1",
            session_id="s1",
            plan_id="p1",
            workspace_id=None,
        )
    assert parked.store.updates == []
    parked.sender.send_status_update_async.assert_not_called()


@pytest.mark.asyncio
async def test_approve_resumes_the_checkpoint_with_the_review_response(
    parked, monkeypatch
):
    parked.plan.waiting_for = {
        "kind": "plan_review",
        "request_id": "req-1",
        "checkpoint_id": "cp-9",
        "workspace_id": "ws-1",
    }
    manager = OrchestrationManager()
    run = AsyncMock()
    monkeypatch.setattr(manager, "run_orchestration", run)

    await manager.resume_orchestration(
        "u1", "s1", "p1", "req-1", MagenticPlanReviewResponse.approve()
    )

    assert parked.plan.waiting_for is None
    kwargs = run.await_args.kwargs
    assert kwargs["_resume"]["checkpoint_id"] == "cp-9"
    [(request_id, response)] = kwargs["_resume"]["responses"].items()
    assert request_id == "req-1"
    assert isinstance(response, MagenticPlanReviewResponse)
    # Empty review == approve: the orchestrator goes straight to the outer loop.
    assert response.review == []


@pytest.mark.asyncio
async def test_reject_cancels_the_parked_review_and_keeps_the_plan_as_canceled(
    parked,
):
    parked.plan.waiting_for = {
        "kind": "plan_review",
        "request_id": "req-1",
        "checkpoint_id": "cp-9",
    }
    await OrchestrationManager().cancel_parked("u1", "p1", "req-1")

    assert parked.plan.waiting_for is None
    assert parked.plan.overall_status == PlanStatus.canceled
    assert parked.store.updates[-1] == (PlanStatus.canceled, {})
    kwargs = parked.sender.send_status_update_async.await_args.kwargs
    assert kwargs["message_type"] == WebsocketMessageType.FINAL_RESULT_MESSAGE
    args = parked.sender.send_status_update_async.await_args.args
    assert args[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_refuses_a_request_the_plan_is_not_waiting_for(parked):
    parked.plan.waiting_for = {"kind": "plan_review", "request_id": "req-1"}
    with pytest.raises(ValueError, match="not waiting for request"):
        await OrchestrationManager().cancel_parked("u1", "p1", "req-X")
    assert parked.store.updates == []
