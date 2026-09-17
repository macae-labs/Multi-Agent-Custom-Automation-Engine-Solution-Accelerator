"""Harness del incremento 3, medido antes del producto.

(1) Retención: la cadena ``previous_checkpoint_id`` NO cruza una reanudación en
    instancia nueva (la corrida reanudada nace con ``workflow_name`` nuevo y su
    primer checkpoint no apunta al restaurado). La retención va por linaje de
    ``workflow_name`` registrado en el work item: cada segmento es una partición
    y se borra entero con el storage real.
(2) Idempotencia del evento: el id del ``work_event`` es su identidad; la
    segunda entrega choca en ``create_item`` (409 del SDK) y no produce transición.
(3) Lease: ``replace_item`` con ``etag`` + ``IfNotModified``; el poseedor con
    etag viejo recibe 412 y no escribe. Ambas semánticas son las del SDK
    (``CosmosResourceExistsError`` / ``CosmosAccessConditionFailedError``), aquí
    sobre el doble del conftest que las reproduce; la verificación contra Cosmos
    de producción es write-shared y queda declarada.
"""

import json

import pytest
from agent_framework import Agent, BaseChatClient, ChatResponse, Message
from agent_framework_orchestrations._magentic import (
    MagenticBuilder,
    MagenticPlanReviewResponse,
    StandardMagenticManager,
)
from azure.core import MatchConditions
from azure.cosmos import exceptions

from common.services.checkpoint_storage import CosmosCheckpointStorage


class ManagerClient(BaseChatClient):
    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        assert not stream
        return self._respond(messages)

    async def _respond(self, messages) -> ChatResponse:
        prompt = (messages[-1].text or "").lower()
        if "is_request_satisfied" in prompt:
            text = json.dumps({
                "is_request_satisfied": {"reason": "r", "answer": True},
                "is_in_loop": {"reason": "r", "answer": False},
                "is_progress_being_made": {"reason": "r", "answer": True},
                "next_speaker": {"reason": "r", "answer": "Worker"},
                "instruction_or_question": {"reason": "r", "answer": "fin"},
            })
        elif "final answer" in prompt:
            text = "FINAL"
        elif "plan" in prompt and "fact" not in prompt:
            text = "- **Worker** to do the thing"
        else:
            text = "Hechos."
        return ChatResponse(messages=[Message(role="assistant", text=text)])


def _workflow(storage):
    manager = StandardMagenticManager(agent=Agent(
        client=ManagerClient(), name="MagenticManager"), max_round_count=3)
    return MagenticBuilder(
        participants=[Agent(client=ManagerClient(), name="Worker")],
        manager=manager,
        enable_plan_review=True,
        checkpoint_storage=storage,
    ).build()


async def _park(storage):
    workflow = _workflow(storage)
    asks = [e async for e in workflow.run("tarea", stream=True) if e.type == "request_info"]
    assert len(asks) == 1
    return workflow, asks[0]


@pytest.mark.asyncio
async def test_checkpoint_chain_does_not_cross_a_resume_so_retention_goes_by_lineage(fake_cosmos_container):
    storage = CosmosCheckpointStorage(container=fake_cosmos_container)
    first, ask = await _park(storage)
    restored = await storage.get_latest(workflow_name=first.name)
    second = _workflow(storage)
    await second.run(checkpoint_id=restored.checkpoint_id, checkpoint_storage=storage, responses={ask.request_id: MagenticPlanReviewResponse.approve()})

    segment2 = await storage.list_checkpoints(workflow_name=second.name)
    assert second.name != first.name and segment2
    # Hecho del framework: la cadena no enlaza con el checkpoint restaurado.
    assert segment2[0].previous_checkpoint_id != restored.checkpoint_id
    walked, current = [], segment2[-1]
    while current is not None:
        walked.append(current.checkpoint_id)
        current = await storage.load(current.previous_checkpoint_id) if current.previous_checkpoint_id else None
    # caminar la cadena deja huérfanos
    assert len(walked) < len(fake_cosmos_container.docs)

    # Regla de retención: el linaje de workflow_name (uno por segmento) borra todo.
    lineage = [first.name, second.name]
    for name in lineage:
        for checkpoint_id in await storage.list_checkpoint_ids(workflow_name=name):
            assert await storage.delete(checkpoint_id) is True
    assert fake_cosmos_container.docs == {}


@pytest.mark.asyncio
async def test_event_identity_makes_the_second_delivery_a_conflict(fake_cosmos_container_factory):
    events = fake_cosmos_container_factory(partition_path="pk")
    # Identidad = causa: id = f"{kind}:{identity}", pk = kind (el id es único
    # por partición, y la partición sale del evento, no de quién lo entrega).
    # Literal hasta que exista EventStore.append; entonces el harness lo usa.
    event = {"id": "plan_review:req-1", "pk": "plan_review", "kind": "plan_review",
             "identity": "req-1", "payload": {"decision": "approve"}, "status": "pending"}
    await events.create_item(body=event)
    with pytest.raises(exceptions.CosmosResourceExistsError) as conflict:
        # otra decisión, misma causa: choca
        await events.create_item(body={**event, "payload": {"decision": "reject"}})
    assert conflict.value.status_code == 409
    # una entrega registrada, una transición
    assert [d["status"] for d in events.docs.values()] == ["pending"]


@pytest.mark.asyncio
async def test_lease_by_etag_rejects_the_stale_holder(fake_cosmos_container_factory):
    leases = fake_cosmos_container_factory(partition_path="pk")
    doc = await leases.create_item(body={"id": "reconciler", "pk": "lease", "holder": "A"})
    etag_a = doc["_etag"]
    held_by_b = await leases.replace_item("reconciler", {"id": "reconciler", "pk": "lease", "holder": "B"}, etag=etag_a, match_condition=MatchConditions.IfNotModified)
    assert held_by_b["holder"] == "B" and held_by_b["_etag"] != etag_a
    with pytest.raises(exceptions.CosmosAccessConditionFailedError) as stale:
        await leases.replace_item("reconciler", {"id": "reconciler", "pk": "lease", "holder": "A"}, etag=etag_a, match_condition=MatchConditions.IfNotModified)
    assert stale.value.status_code == 412
    assert (await leases.read_item("reconciler", partition_key="lease"))["holder"] == "B"
