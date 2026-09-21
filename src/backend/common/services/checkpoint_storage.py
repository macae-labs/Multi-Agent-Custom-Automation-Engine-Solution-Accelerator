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

import json
import logging
from typing import Any, Optional
from uuid import uuid4

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
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from common.config.app_config import config

logger = logging.getLogger(__name__)

CHECKPOINTS_CONTAINER_NAME = "workflow_checkpoints"
# Campos que Cosmos añade al documento y que no pertenecen al checkpoint.
_COSMOS_FIELDS = frozenset(
    {"id", "parts_token", "_rid", "_self", "_etag", "_attachments", "_ts"}
)

# Cosmos rechaza un item de más de 2 MB (413 RequestEntityTooLarge). El
# checkpoint del framework es el ESTADO ENTERO del workflow —cada mensaje y
# cada salida de tool—, así que un ``cat`` de 2,5 MB dentro de una conversación
# lo desborda. Medido en producción (rev 132, 2026-09-20/21): el runner
# registra un WARNING y sigue sin checkpoint; al aparcar, nada conserva el
# ``request_info`` y el plan muere ("sin checkpoint que lo conserve").
#
# El documento principal es una PROYECCIÓN del checkpoint: cabecera, petición
# pendiente y referencia. El cuerpo voluminoso (``messages`` y ``state``) va en
# partes hermanas bajo el límite, en la misma partición, y ``load`` reconstruye
# el checkpoint exacto. Un checkpoint pequeño sigue siendo un solo documento.
COSMOS_MAX_ITEM_BYTES = 2 * 1024 * 1024
_PART_BYTES = 1_500_000  # margen para el sobre del documento y los campos de sistema
_PART_KIND = "checkpoint_part"
_BULK_FIELDS = ("messages", "state")


def _utf8_parts(data: bytes, limit: int) -> list[bytes]:
    """Trocea sin partir un carácter multibyte (retrocede al inicio del carácter)."""
    parts: list[bytes] = []
    start = 0
    while start < len(data):
        end = min(start + limit, len(data))
        while end < len(data) and (data[end] & 0xC0) == 0x80:
            end -= 1
        parts.append(data[start:end])
        start = end
    return parts


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
        # Credencial prestada (config la cierra una sola vez); aquí se cierra sólo el cliente.
        self._client = CosmosClient(
            url=endpoint, credential=config.get_cosmos_credential_async()
        )
        # El ciclo de vida se guarda desde la ADQUISICIÓN: cualquier fallo tras
        # crear el cliente lo cierra, o queda una sesión aiohttp abierta.
        try:
            database = self._client.get_database_client(db_name)
            # Provisionado por infra/main.bicep (la cuenta prohíbe crear
            # contenedores por data-plane). Aquí sólo se abre.
            self._container = database.get_container_client(CHECKPOINTS_CONTAINER_NAME)
            await self._container.read()
        except BaseException as failure:
            self._container = None
            await self.aclose()
            if isinstance(failure, CosmosResourceNotFoundError):
                raise WorkflowCheckpointException(
                    f"Cosmos container '{CHECKPOINTS_CONTAINER_NAME}' is not "
                    "provisioned (infra/main.bicep declares it; deploy the infra first)"
                ) from failure
            raise
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

    async def _part_docs(
        self, workflow_name: str, checkpoint_id: str
    ) -> list[dict[str, Any]]:
        container = await self._ensure_initialized()
        items = container.query_items(
            query=(
                "SELECT * FROM c WHERE c.kind = @kind "
                "AND c.checkpoint_id = @checkpoint_id"
            ),
            parameters=[
                {"name": "@kind", "value": _PART_KIND},
                {"name": "@checkpoint_id", "value": checkpoint_id},
            ],
            partition_key=workflow_name,
        )
        return [dict(doc) async for doc in items]

    async def _parts(self, head: dict[str, Any]) -> list[dict[str, Any]]:
        parts = await self._part_docs(head["workflow_name"], head["id"])
        parts_token = head.get("parts_token")
        if parts_token is not None:
            parts = [part for part in parts if part.get("parts_token") == parts_token]
        parts.sort(key=lambda part: part["index"])
        return parts

    async def _delete_superseded_parts(
        self,
        workflow_name: str,
        checkpoint_id: str,
        *,
        keep_token: Optional[str] = None,
    ) -> None:
        container = await self._ensure_initialized()
        for part in await self._part_docs(workflow_name, checkpoint_id):
            if keep_token is not None and part.get("parts_token") == keep_token:
                continue
            await container.delete_item(item=part["id"], partition_key=workflow_name)

    async def _hydrate(self, head: dict[str, Any]) -> dict[str, Any]:
        """Devuelve el checkpoint completo: la cabecera más su cuerpo, si va aparte."""
        if "parts" not in head:
            return head
        parts = await self._parts(head)
        if [part["index"] for part in parts] != list(range(head["parts"])):
            raise WorkflowCheckpointException(
                f"Checkpoint {head['id']} incompleto: {len(parts)}/{head['parts']} partes"
            )
        doc = {k: v for k, v in head.items() if k not in {"parts", "parts_token"}}
        doc.update(json.loads("".join(part["data"] for part in parts)))
        return doc

    async def _heads(self, workflow_name: str) -> list[dict[str, Any]]:
        container = await self._ensure_initialized()
        items = container.query_items(
            query="SELECT * FROM c WHERE c.workflow_name = @workflow_name",
            parameters=[{"name": "@workflow_name", "value": workflow_name}],
            partition_key=workflow_name,
        )
        heads = [dict(doc) async for doc in items if doc.get("kind") != _PART_KIND]
        heads.sort(key=lambda head: head["timestamp"])
        return heads

    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        container = await self._ensure_initialized()
        head = encode_checkpoint_value(checkpoint.to_dict())
        head["id"] = checkpoint.checkpoint_id
        workflow_name = checkpoint.workflow_name
        bulk = {field: head.pop(field) for field in _BULK_FIELDS if field in head}
        body = json.dumps(bulk, separators=(",", ":")).encode("utf-8")
        inline_doc = dict(head)
        inline_doc.update(bulk)
        if len(json.dumps(inline_doc).encode("utf-8")) <= COSMOS_MAX_ITEM_BYTES:
            await container.upsert_item(body=inline_doc)
            await self._delete_superseded_parts(workflow_name, checkpoint.checkpoint_id)
        else:
            # Primero las partes, después la cabecera que las referencia: un
            # lector nunca ve una cabecera cuyo cuerpo aún no existe.
            parts_token = uuid4().hex
            parts = _utf8_parts(body, _PART_BYTES - 1024)
            written_part_ids: list[str] = []
            try:
                for index, part in enumerate(parts):
                    part_id = f"{checkpoint.checkpoint_id}:{parts_token}:{index}"
                    await container.upsert_item(
                        body={
                            "id": part_id,
                            "workflow_name": workflow_name,
                            "kind": _PART_KIND,
                            "checkpoint_id": checkpoint.checkpoint_id,
                            "parts_token": parts_token,
                            "index": index,
                            "data": part.decode("utf-8"),
                        }
                    )
                    written_part_ids.append(part_id)
                head["parts"] = len(parts)
                head["parts_token"] = parts_token
                await container.upsert_item(body=head)
            except Exception:
                for part_id in written_part_ids:
                    try:
                        await container.delete_item(
                            item=part_id, partition_key=workflow_name
                        )
                    except CosmosResourceNotFoundError:
                        pass
                raise
            await self._delete_superseded_parts(
                workflow_name, checkpoint.checkpoint_id, keep_token=parts_token
            )
        logger.debug(
            "Checkpoint %s guardado (%s, %d bytes de cuerpo)",
            checkpoint.checkpoint_id,
            workflow_name,
            len(body),
        )
        return checkpoint.checkpoint_id

    async def load(self, checkpoint_id: str) -> WorkflowCheckpoint:
        head = await self._find(checkpoint_id)
        if head is None:
            raise WorkflowCheckpointException(
                f"No checkpoint found with ID {checkpoint_id}"
            )
        return self._to_checkpoint(await self._hydrate(head))

    async def list_checkpoints(self, *, workflow_name: str) -> list[WorkflowCheckpoint]:
        return [
            self._to_checkpoint(await self._hydrate(head))
            for head in await self._heads(workflow_name)
        ]

    async def delete(self, checkpoint_id: str) -> bool:
        head = await self._find(checkpoint_id)
        if head is None:
            return False
        container = await self._ensure_initialized()
        await self._delete_superseded_parts(head["workflow_name"], checkpoint_id)
        await container.delete_item(
            item=checkpoint_id, partition_key=head["workflow_name"]
        )
        return True

    async def get_latest(self, *, workflow_name: str) -> Optional[WorkflowCheckpoint]:
        heads = await self._heads(workflow_name)
        if not heads:
            return None
        return self._to_checkpoint(await self._hydrate(heads[-1]))

    async def list_checkpoint_ids(self, *, workflow_name: str) -> list[str]:
        return [head["id"] for head in await self._heads(workflow_name)]


_storage: Optional[CheckpointStorage] = None


def get_checkpoint_storage() -> CheckpointStorage:
    """Cosmos cuando hay ``COSMOSDB_ENDPOINT``; sin él, en memoria.

    Una instancia por proceso en ambos modos: la reanudación
    (``workflow.run(checkpoint_id=..., responses=...)``) debe leer del mismo
    storage con el que se construyó el workflow.
    """
    global _storage
    if _storage is None:
        _storage = (
            CosmosCheckpointStorage()
            if config.COSMOSDB_ENDPOINT
            else InMemoryCheckpointStorage()
        )
    return _storage


async def close_checkpoint_storage() -> None:
    """Cierra el cliente compartido; lo llama el lifespan de la app."""
    global _storage
    if isinstance(_storage, CosmosCheckpointStorage):
        await _storage.aclose()
    _storage = None
