"""CosmosCheckpointStorage contra el framework REAL: un workflow mínimo con dos
supersteps guarda checkpoints en el contenedor Cosmos falso del conftest (la
superficie exacta que usa el storage), otra instancia del mismo grafo se
reanuda por checkpoint_id, y un grafo distinto es rechazado por el framework
(graph_signature_hash): la precondición que el work item debe hacer explícita.
"""

from typing import Never

import pytest
from agent_framework import (
    Executor,
    WorkflowBuilder,
    WorkflowCheckpointException,
    WorkflowContext,
    handler,
)

from common.services.checkpoint_storage import CosmosCheckpointStorage


class Upper(Executor):
    @handler
    async def run(self, text: str, ctx: WorkflowContext[str]) -> None:
        await ctx.send_message(text.upper())


class Emit(Executor):
    @handler
    async def run(self, text: str, ctx: WorkflowContext[Never, str]) -> None:
        await ctx.yield_output(text + "!")


def _build(storage, changed_graph: bool = False):
    upper, emit = Upper(id="upper"), Emit(id="emit")
    builder = WorkflowBuilder(start_executor=upper, checkpoint_storage=storage).add_edge(upper, emit)
    if changed_graph:
        builder = builder.add_edge(emit, Upper(id="upper2"))
    return builder.build()


@pytest.fixture
def container(fake_cosmos_container):
    return fake_cosmos_container


@pytest.fixture
def storage(container):
    return CosmosCheckpointStorage(container=container)


@pytest.mark.asyncio
async def test_checkpoints_persist_and_same_graph_resumes(storage, container):
    workflow = _build(storage)
    result = await workflow.run("hola")
    assert result.get_outputs() == ["HOLA!"]

    ids = await storage.list_checkpoint_ids(workflow_name=workflow.name)
    assert ids and set(ids) == set(container.docs)
    first = (await storage.list_checkpoints(workflow_name=workflow.name))[0]
    assert first.workflow_name == workflow.name and first.graph_signature_hash

    # "Otro proceso": una instancia nueva del MISMO grafo reanuda por checkpoint_id.
    resumed = await _build(storage).run(checkpoint_id=first.checkpoint_id, checkpoint_storage=storage)
    assert resumed.get_outputs() == ["HOLA!"]


@pytest.mark.asyncio
async def test_resume_rejects_a_different_graph(storage):
    workflow = _build(storage)
    await workflow.run("hola")
    checkpoint_id = (await storage.list_checkpoint_ids(workflow_name=workflow.name))[0]

    with pytest.raises(WorkflowCheckpointException, match="graph has changed"):
        await _build(storage, changed_graph=True).run(checkpoint_id=checkpoint_id, checkpoint_storage=storage)


@pytest.mark.asyncio
async def test_load_delete_and_latest(storage):
    workflow = _build(storage)
    await workflow.run("hola")
    checkpoints = await storage.list_checkpoints(workflow_name=workflow.name)
    latest = await storage.get_latest(workflow_name=workflow.name)
    assert latest is not None and latest.checkpoint_id == checkpoints[-1].checkpoint_id

    loaded = await storage.load(checkpoints[0].checkpoint_id)
    assert loaded.to_dict() == checkpoints[0].to_dict()

    deleted = await storage.delete(checkpoints[0].checkpoint_id)
    assert deleted is True
    deleted_again = await storage.delete(checkpoints[0].checkpoint_id)
    assert deleted_again is False
    with pytest.raises(WorkflowCheckpointException, match="No checkpoint found"):
        await storage.load(checkpoints[0].checkpoint_id)
    assert await storage.get_latest(workflow_name="otro") is None
