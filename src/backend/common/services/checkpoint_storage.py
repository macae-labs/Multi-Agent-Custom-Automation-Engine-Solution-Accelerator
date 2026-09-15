"""Checkpoints de workflow (agent_framework) persistidos en Cosmos DB.

Implementa el protocolo ``agent_framework.CheckpointStorage`` (seis métodos)
sobre un contenedor propio con clave de partición ``/workflow_name`` y la
misma codificación que ``FileCheckpointStorage`` del framework
(``encode_checkpoint_value`` / ``decode_checkpoint_value`` sobre
``WorkflowCheckpoint.to_dict`` / ``from_dict``). Sustituye al
``InMemoryCheckpointStorage`` de ``init_orchestration``: un checkpoint
sobrevive al proceso y una corrida se reanuda con
``workflow.run(checkpoint_id=..., checkpoint_storage=...)``.

Precondición de reanudación (la impone el framework, ``_runner.py``): el
grafo reconstruido debe tener el mismo ``graph_signature_hash`` que el
checkpoint; si no, ``WorkflowCheckpointException``. En Magentic el grafo lo
determinan los participantes (nombres y orden) de la team-config, así que
quien reanude debe verificar antes que la team-config no cambió.
"""

import logging
from typing import Any, Optional

from agent_framework import (
    CheckpointStorage,
    InMemoryCheckpointStorage,
    WorkflowCheckpoint,
    WorkflowCheckpointException,
)
from agent_framework._workflows._checkpoint_encoding import (
    decode_checkpoint_value,
    encode_checkpoint_value,
)
from azure.cosmos import PartitionKey
from azure.cosmos.aio import CosmosClient

from common.config.app_config import config

logger = logging.getLogger(__name__)

CHECKPOINTS_CONTAINER_NAME = "workflow_checkpoints"
# Campos que Cosmos añade al documento y que no pertenecen al checkpoint.
_COSMOS_FIELDS = frozenset({"id", "_rid", "_self", "_etag", "_attachments", "_ts"})


class CosmosCheckpointStorage:
    """``CheckpointStorage`` sobre Cosmos: un documento por checkpoint.

    ``id`` = ``checkpoint_id``; partición = ``workflow_name``. El cliente se
    crea en la primera operación (nada de red al construir el objeto).
    """

    def __init__(self, container: Any = None) -> None:
        self._container = container
        self._client: Optional[CosmosClient] = None

    async def _ensure_initialized(self) -> Any:
        if self._container is not None:
            return self._container
        endpoint, db_name = config.COSMOSDB_ENDPOINT, config.COSMOSDB_DATABASE
        if not endpoint or not db_name:
            raise WorkflowCheckpointException(
                "COSMOSDB_ENDPOINT / COSMOSDB_DATABASE no configurados: sin checkpoints durables"
            )
        credential = (
            config.COSMOSDB_KEY
            if config.APP_ENV == "dev" and config.COSMOSDB_KEY
            else config.get_azure_credential_async(config.AZURE_CLIENT_ID)
        )
        self._client = CosmosClient(url=endpoint, credential=credential)
        database = self._client.get_database_client(db_name)
        self._container = await database.create_container_if_not_exists(
            id=CHECKPOINTS_CONTAINER_NAME,
            partition_key=PartitionKey(path="/workflow_name"),
        )
        logger.info(
            "CosmosCheckpointStorage listo (container=%s)", CHECKPOINTS_CONTAINER_NAME
        )
        return self._container

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    @staticmethod
    def _to_checkpoint(doc: dict[str, Any]) -> WorkflowCheckpoint:
        encoded = {k: v for k, v in doc.items() if k not in _COSMOS_FIELDS}
        return WorkflowCheckpoint.from_dict(decode_checkpoint_value(encoded))

    async def _find(self, checkpoint_id: str) -> Optional[dict[str, Any]]:
        container = await self._ensure_initialized()
        items = container.query_items(
            query="SELECT * FROM c WHERE c.id = @id",
            parameters=[{"name": "@id", "value": checkpoint_id}],
        )
        async for doc in items:
            return dict(doc)
        return None

    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        container = await self._ensure_initialized()
        body = encode_checkpoint_value(checkpoint.to_dict())
        body["id"] = checkpoint.checkpoint_id
        await container.upsert_item(body=body)
        logger.debug(
            "Checkpoint %s guardado (%s)",
            checkpoint.checkpoint_id,
            checkpoint.workflow_name,
        )
        return checkpoint.checkpoint_id

    async def load(self, checkpoint_id: str) -> WorkflowCheckpoint:
        doc = await self._find(checkpoint_id)
        if doc is None:
            raise WorkflowCheckpointException(
                f"No checkpoint found with ID {checkpoint_id}"
            )
        return self._to_checkpoint(doc)

    async def list_checkpoints(self, *, workflow_name: str) -> list[WorkflowCheckpoint]:
        container = await self._ensure_initialized()
        items = container.query_items(
            query="SELECT * FROM c WHERE c.workflow_name = @workflow_name",
            parameters=[{"name": "@workflow_name", "value": workflow_name}],
            partition_key=workflow_name,
        )
        checkpoints = [self._to_checkpoint(dict(doc)) async for doc in items]
        checkpoints.sort(key=lambda c: c.timestamp)
        return checkpoints

    async def delete(self, checkpoint_id: str) -> bool:
        doc = await self._find(checkpoint_id)
        if doc is None:
            return False
        container = await self._ensure_initialized()
        await container.delete_item(
            item=checkpoint_id, partition_key=doc["workflow_name"]
        )
        return True

    async def get_latest(self, *, workflow_name: str) -> Optional[WorkflowCheckpoint]:
        checkpoints = await self.list_checkpoints(workflow_name=workflow_name)
        return checkpoints[-1] if checkpoints else None

    async def list_checkpoint_ids(self, *, workflow_name: str) -> list[str]:
        return [
            c.checkpoint_id
            for c in await self.list_checkpoints(workflow_name=workflow_name)
        ]


_storage: Optional[CosmosCheckpointStorage] = None


def get_checkpoint_storage() -> CheckpointStorage:
    """Cosmos cuando hay ``COSMOSDB_ENDPOINT``; sin él, en memoria (como antes)."""
    global _storage
    if not config.COSMOSDB_ENDPOINT:
        return InMemoryCheckpointStorage()
    if _storage is None:
        _storage = CosmosCheckpointStorage()
    return _storage


async def close_checkpoint_storage() -> None:
    """Cierra el cliente compartido; lo llama el lifespan de la app."""
    global _storage
    if _storage is not None:
        await _storage.aclose()
        _storage = None
