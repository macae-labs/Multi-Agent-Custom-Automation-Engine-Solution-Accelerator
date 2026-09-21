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
    builder = WorkflowBuilder(
        start_executor=upper, checkpoint_storage=storage
    ).add_edge(upper, emit)
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
    resumed = await _build(storage).run(
        checkpoint_id=first.checkpoint_id, checkpoint_storage=storage
    )
    assert resumed.get_outputs() == ["HOLA!"]


@pytest.mark.asyncio
async def test_resume_rejects_a_different_graph(storage):
    workflow = _build(storage)
    await workflow.run("hola")
    checkpoint_id = (await storage.list_checkpoint_ids(workflow_name=workflow.name))[0]

    with pytest.raises(WorkflowCheckpointException, match="graph has changed"):
        await _build(storage, changed_graph=True).run(
            checkpoint_id=checkpoint_id, checkpoint_storage=storage
        )


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


# ---------------------------------------------------------------------------
# Proyección: el checkpoint de producción que Cosmos rechazaba (2026-09-20/21,
# RequestEntityTooLarge tras un `cat infra/main.json` de 2,5 MB dentro de una
# conversación). El doble del conftest aplica el mismo límite de 2 MB.
# ---------------------------------------------------------------------------

import json  # noqa: E402

from agent_framework import WorkflowCheckpoint  # noqa: E402
from agent_framework._workflows._checkpoint_encoding import (  # noqa: E402
    encode_checkpoint_value,
)
from azure.cosmos import exceptions as cosmos_exceptions  # noqa: E402

from common.services.checkpoint_storage import COSMOS_MAX_ITEM_BYTES  # noqa: E402


def _checkpoint_with_tool_output(name: str, size: int) -> WorkflowCheckpoint:
    """Una conversación con una salida de tool de ``size`` caracteres multibyte:
    el troceo no puede partir un carácter."""
    return WorkflowCheckpoint(
        workflow_name=name,
        graph_signature_hash="sig",
        state={
            "_executor_state": {
                "RepositoryAuditAgent": {"conversation": ["ñé" * (size // 2)]}
            }
        },
        metadata={"superstep": 7},
    )


@pytest.mark.asyncio
async def test_the_double_rejects_what_production_rejects(container):
    """Control negativo: el checkpoint entero como UN documento —lo que hacía
    ``save`` hasta ahora— no cabe, y el doble lo dice como el servicio."""
    checkpoint = _checkpoint_with_tool_output("wf-grande", 3 * 1024 * 1024)
    body = encode_checkpoint_value(checkpoint.to_dict())
    body["id"] = checkpoint.checkpoint_id

    with pytest.raises(cosmos_exceptions.CosmosHttpResponseError) as failure:
        await container.upsert_item(body=body)
    assert failure.value.status_code == 413


@pytest.mark.asyncio
async def test_a_checkpoint_over_the_limit_round_trips_exactly(storage, container):
    checkpoint = _checkpoint_with_tool_output("wf-grande", 3 * 1024 * 1024)

    await storage.save(checkpoint)

    assert all(
        len(json.dumps(doc).encode("utf-8")) <= COSMOS_MAX_ITEM_BYTES
        for doc in container.docs.values()
    )
    assert len(container.docs) > 1
    # La petición pendiente y la cabecera viven en el documento principal.
    head = container.docs[checkpoint.checkpoint_id]
    assert "state" not in head and head["parts"] == len(container.docs) - 1
    assert "parts_token" in head
    assert "parts_token" not in await storage._hydrate(dict(head))

    loaded = await storage.load(checkpoint.checkpoint_id)
    assert loaded.to_dict() == checkpoint.to_dict()
    latest = await storage.get_latest(workflow_name="wf-grande")
    assert latest is not None and latest.to_dict() == checkpoint.to_dict()
    # Las partes no son checkpoints.
    assert await storage.list_checkpoint_ids(workflow_name="wf-grande") == [
        checkpoint.checkpoint_id
    ]


@pytest.mark.asyncio
async def test_a_checkpoint_below_the_cosmos_limit_stays_in_one_document(storage, container):
    checkpoint = _checkpoint_with_tool_output("wf-grande", 275_000)

    await storage.save(checkpoint)

    assert set(container.docs) == {checkpoint.checkpoint_id}
    assert len(json.dumps(container.docs[checkpoint.checkpoint_id]).encode("utf-8")) <= (
        COSMOS_MAX_ITEM_BYTES
    )
    loaded = await storage.load(checkpoint.checkpoint_id)
    assert loaded.to_dict() == checkpoint.to_dict()


@pytest.mark.asyncio
async def test_save_tolerates_missing_bulk_fields(storage, container, monkeypatch):
    checkpoint = WorkflowCheckpoint(workflow_name="wf", graph_signature_hash="sig")

    monkeypatch.setattr(
        "common.services.checkpoint_storage.encode_checkpoint_value",
        lambda _checkpoint: {
            "workflow_name": checkpoint.workflow_name,
            "graph_signature_hash": checkpoint.graph_signature_hash,
            "timestamp": "2026-09-21T00:00:00Z",
        },
    )

    await storage.save(checkpoint)

    assert {
        key: container.docs[checkpoint.checkpoint_id][key]
        for key in ("id", "workflow_name", "graph_signature_hash", "timestamp")
    } == {
        "id": checkpoint.checkpoint_id,
        "workflow_name": checkpoint.workflow_name,
        "graph_signature_hash": checkpoint.graph_signature_hash,
        "timestamp": "2026-09-21T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_a_lineage_mixes_small_and_large_and_purges_whole(storage, container):
    small = WorkflowCheckpoint(workflow_name="wf", graph_signature_hash="sig")
    large = _checkpoint_with_tool_output("wf", 3 * 1024 * 1024)
    large.previous_checkpoint_id = small.checkpoint_id
    await storage.save(small)
    await storage.save(large)

    lineage = await storage.list_checkpoints(workflow_name="wf")
    assert [c.checkpoint_id for c in lineage] == [
        small.checkpoint_id,
        large.checkpoint_id,
    ]
    assert lineage[1].to_dict() == large.to_dict()

    # La purga del manager: ids + delete; se lleva cabeceras y partes.
    for checkpoint_id in await storage.list_checkpoint_ids(workflow_name="wf"):
        deleted = await storage.delete(checkpoint_id)
        assert deleted is True
    assert container.docs == {}


@pytest.mark.asyncio
async def test_rewriting_a_large_checkpoint_with_a_small_one_purges_old_parts(
    storage, container
):
    large = _checkpoint_with_tool_output("wf", 3 * 1024 * 1024)
    await storage.save(large)
    old_part_ids = set(container.docs) - {large.checkpoint_id}
    assert old_part_ids

    small = WorkflowCheckpoint(workflow_name="wf", graph_signature_hash="sig")
    small.checkpoint_id = large.checkpoint_id
    await storage.save(small)

    assert set(container.docs) == {large.checkpoint_id}
    loaded = await storage.load(large.checkpoint_id)
    assert loaded.to_dict() == small.to_dict()


@pytest.mark.asyncio
async def test_failed_large_rewrite_cleans_staged_parts_and_keeps_previous_checkpoint(
    storage, container
):
    checkpoint = _checkpoint_with_tool_output("wf", 3 * 1024 * 1024)
    await storage.save(checkpoint)
    original_docs = {doc_id: dict(doc) for doc_id, doc in container.docs.items()}
    original_upsert = container.upsert_item

    async def fail_on_new_head(*args, **kwargs):
        body = kwargs.get("body") or args[0]
        if body.get("id") == checkpoint.checkpoint_id and body.get("parts_token"):
            raise RuntimeError("boom")
        return await original_upsert(*args, **kwargs)

    container.upsert_item = fail_on_new_head
    rewritten = _checkpoint_with_tool_output("wf", 3 * 1024 * 1024)
    rewritten.checkpoint_id = checkpoint.checkpoint_id

    with pytest.raises(RuntimeError, match="boom"):
        await storage.save(rewritten)

    assert container.docs == original_docs
    loaded = await storage.load(checkpoint.checkpoint_id)
    assert loaded.to_dict() == checkpoint.to_dict()


@pytest.mark.asyncio
async def test_delete_purges_superseded_parts_from_older_tokens(storage, container):
    checkpoint = _checkpoint_with_tool_output("wf", 3 * 1024 * 1024)
    await storage.save(checkpoint)
    await container.upsert_item(
        body={
            "id": f"{checkpoint.checkpoint_id}:stale:0",
            "workflow_name": "wf",
            "kind": "checkpoint_part",
            "checkpoint_id": checkpoint.checkpoint_id,
            "parts_token": "stale",
            "index": 0,
            "data": "{}",
        }
    )

    deleted = await storage.delete(checkpoint.checkpoint_id)

    assert deleted is True
    assert container.docs == {}
