"""Harness del hueco nativo de HITL del framework, medido sin Foundry.

MagenticBuilder(enable_plan_review=True) + CosmosCheckpointStorage sobre el
contenedor falso: el orquestador real emite `request_info` con
MagenticPlanReviewRequest, el superstep termina, el checkpoint queda escrito
con la petición pendiente, y una instancia NUEVA del mismo grafo reanuda con
run(checkpoint_id, responses={request_id: approve()}) hasta la respuesta
final; revise() produce un replan y una petición nueva; un participante
renombrado es rechazado (graph_signature_hash). El modelo es un
ScriptedChatClient del framework que responde según el prompt que recibe.
"""

import json

import pytest
from agent_framework import Agent, BaseChatClient, ChatResponse, Message, WorkflowCheckpointException
from agent_framework_orchestrations._magentic import (
    MagenticBuilder,
    MagenticPlanReviewRequest,
    MagenticPlanReviewResponse,
    StandardMagenticManager,
)

from common.services.checkpoint_storage import CosmosCheckpointStorage

LEDGER_DONE = {
    "is_request_satisfied": {"reason": "todo listo", "answer": True},
    "is_in_loop": {"reason": "no", "answer": False},
    "is_progress_being_made": {"reason": "sí", "answer": True},
    "next_speaker": {"reason": "nadie", "answer": "Researcher"},
    "instruction_or_question": {"reason": "fin", "answer": "Concluir"},
}


class ScriptedChatClient(BaseChatClient):
    """Modelo guionado: responde por el contenido del último prompt del manager."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        assert not stream, "el manager llama al agente sin streaming"
        prompt = messages[-1].text or ""
        self.prompts.append(prompt)
        return self._respond(prompt)

    async def _respond(self, prompt: str) -> ChatResponse:
        low = prompt.lower()
        if "is_request_satisfied" in low:
            text = json.dumps(LEDGER_DONE)
        elif "final answer" in low or "respuesta final" in low:
            text = "RESPUESTA FINAL GUIONADA"
        elif "plan" in low and "fact" not in low:
            text = "- **Researcher** to gather the data\n- **Researcher** to summarize it"
        else:
            text = "Hechos conocidos: ninguno."
        return ChatResponse(messages=[Message(role="assistant", text=text)])


def _workflow(storage, participant_name: str = "Researcher"):
    manager_agent = Agent(client=ScriptedChatClient(), name="MagenticManager")
    participant = Agent(client=ScriptedChatClient(), name=participant_name)
    return MagenticBuilder(
        participants=[participant],
        manager=StandardMagenticManager(agent=manager_agent, max_round_count=3),
        enable_plan_review=True,
        checkpoint_storage=storage,
    ).build()


@pytest.fixture
def storage(fake_cosmos_container):
    return CosmosCheckpointStorage(container=fake_cosmos_container)


async def _run_until_review(workflow):
    """Corre hasta la petición de revisión del plan: (request_id, request)."""
    reviews = []
    async for event in workflow.run("Investiga el tema X", stream=True):
        if event.type == "request_info":
            reviews.append((event.request_id, event.data))
    assert len(reviews) == 1, reviews
    return reviews[0]


@pytest.mark.asyncio
async def test_plan_review_pauses_checkpoints_and_resumes_in_new_instance(storage, fake_cosmos_container):
    workflow = _workflow(storage)
    request_id, request = await _run_until_review(workflow)
    assert isinstance(request, MagenticPlanReviewRequest)
    assert "Researcher" in (request.plan.text or "") and request.is_stalled is False

    latest = await storage.get_latest(workflow_name=workflow.name)
    assert latest is not None and latest.pending_request_info_events, "el checkpoint debe conservar la petición pendiente"
    # dict[request_id, WorkflowEvent]; la petición sobrevive a la codificación de Cosmos como objeto.
    assert list(latest.pending_request_info_events) == [request_id]
    assert isinstance(latest.pending_request_info_events[request_id].data, MagenticPlanReviewRequest)

    # "Otro proceso": instancia nueva del mismo grafo, restaurar y entregar la aprobación en una llamada.
    resumed = _workflow(storage)
    result = await resumed.run(
        checkpoint_id=latest.checkpoint_id,
        checkpoint_storage=storage,
        responses={request_id: MagenticPlanReviewResponse.approve()},
    )
    # La salida final es la conversación (lista de Message); la última es la respuesta del manager.
    texts = [m.text for out in result.get_outputs() for m in (out if isinstance(out, list) else [out]) if isinstance(m, Message)]
    assert texts and "RESPUESTA FINAL GUIONADA" in (texts[-1] or ""), texts
    assert not result.get_request_info_events()
    assert len(fake_cosmos_container.docs) > 1  # la reanudación siguió escribiendo checkpoints


@pytest.mark.asyncio
async def test_revise_replans_and_asks_again(storage):
    workflow = _workflow(storage)
    request_id, _ = await _run_until_review(workflow)
    latest = await storage.get_latest(workflow_name=workflow.name)
    assert latest is not None

    resumed = _workflow(storage)
    result = await resumed.run(
        checkpoint_id=latest.checkpoint_id,
        checkpoint_storage=storage,
        responses={request_id: MagenticPlanReviewResponse.revise("usa dos pasos distintos")},
    )
    again = result.get_request_info_events()
    assert len(again) == 1 and again[0].request_id != request_id
    assert isinstance(again[0].data, MagenticPlanReviewRequest)


@pytest.mark.asyncio
async def test_renamed_participant_cannot_resume(storage):
    workflow = _workflow(storage)
    request_id, _ = await _run_until_review(workflow)
    latest = await storage.get_latest(workflow_name=workflow.name)
    assert latest is not None

    with pytest.raises(WorkflowCheckpointException, match="graph has changed"):
        await _workflow(storage, participant_name="Analyst").run(
            checkpoint_id=latest.checkpoint_id,
            checkpoint_storage=storage,
            responses={request_id: MagenticPlanReviewResponse.approve()},
        )
