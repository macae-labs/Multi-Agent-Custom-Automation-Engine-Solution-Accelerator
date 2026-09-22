"""A chat message answers a clarification only when it names the question.

Prod 2026-09-22, session autonoma-001: plan e5b31dda had been parked on a
clarification since the day before. Two new tasks typed in that session were
routed as "the answer" to c817f2a3 and df7940b6 — questions the user never
saw — and the resumed run ended in a 400. The session is not the identity of
an answer; the request_id is.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from common.models.messages_af import ChatMessageRequest
from v4.api.router import _clarification_answer_target


class FakeStore:
    def __init__(self, plans):
        self.plans = plans

    async def get_all_plans(self):
        return list(self.plans)

    async def get_plan_by_plan_id(self, plan_id):
        return next((p for p in self.plans if p.plan_id == plan_id), None)


def _parked(plan_id="e5b31dda", session_id="autonoma-001", request_id="c817f2a3"):
    return SimpleNamespace(
        plan_id=plan_id,
        session_id=session_id,
        waiting_for={
            "kind": "clarification",
            "request_id": request_id,
            "question": "¿Qué trimestre fiscal quieres analizar?",
        },
    )


@pytest.mark.asyncio
async def test_a_new_task_in_a_session_with_a_parked_plan_is_not_an_answer():
    store = FakeStore([_parked()])
    req = ChatMessageRequest(
        session_id="autonoma-001",
        message="Realicemos las mismas validaciones previas mediante run_plan",
    )
    assert await _clarification_answer_target(store, req) is None


@pytest.mark.asyncio
async def test_an_answer_names_its_question():
    store = FakeStore([_parked()])
    req = ChatMessageRequest(
        session_id="autonoma-001",
        message="El tercer trimestre de 2026",
        clarification_request_id="c817f2a3",
    )
    target = await _clarification_answer_target(store, req)
    assert target is not None and target.plan_id == "e5b31dda"


@pytest.mark.asyncio
async def test_an_answer_to_a_question_nobody_waits_for_is_a_contract_error():
    store = FakeStore([_parked()])
    req = ChatMessageRequest(
        session_id="autonoma-001",
        message="Q3",
        clarification_request_id="req-gone",
    )
    with pytest.raises(HTTPException) as exc:
        await _clarification_answer_target(store, req)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_an_answer_does_not_cross_sessions():
    store = FakeStore([_parked(session_id="other-session")])
    req = ChatMessageRequest(
        session_id="autonoma-001",
        message="Q3",
        clarification_request_id="c817f2a3",
    )
    with pytest.raises(HTTPException):
        await _clarification_answer_target(store, req)
