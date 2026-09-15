"""CosmosCheckpointStorage contra el framework REAL: un workflow mínimo con dos
supersteps guarda checkpoints en un contenedor Cosmos falso en memoria (la
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


class FakeContainer:
    """Lo que CosmosCheckpointStorage usa de un ContainerProxy, en memoria."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def upsert_item(self, body):
        self.docs[body["id"]] = dict(body)
        return body

    async def delete_item(self, item, partition_key):
        assert self.docs[item]["workflow_name"] == partition_key
        del self.docs[item]

    def query_items(self, query, parameters=None, partition_key=None):
        params = {p["name"]: p["value"] for p in parameters or []}

        async def _gen():
            for doc in list(self.docs.values()):
                if "@id" in params and doc["id"] != params["@id"]:
                    continue
                if "@workflow_name" in params and doc["workflow_name"] != params["@workflow_name"]:
                    continue
                yield {**doc, "_rid": "rid", "_etag": "etag", "_ts": 1}

        return _gen()


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
def container():
    return FakeContainer()


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

    assert await storage.delete(checkpoints[0].checkpoint_id) is True
    assert await storage.delete(checkpoints[0].checkpoint_id) is False
    with pytest.raises(WorkflowCheckpointException, match="No checkpoint found"):
        await storage.load(checkpoints[0].checkpoint_id)
    assert await storage.get_latest(workflow_name="otro") is None
