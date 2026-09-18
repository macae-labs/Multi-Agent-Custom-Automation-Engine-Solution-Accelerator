"""``work_event`` append-only en Cosmos: la decisión humana como hecho durable.

Identidad = causa. Un evento es ``id = f"{kind}:{identity}"`` con partición
``pk = kind`` (el ``id`` es único por partición). ``append`` es un solo
``create_item``: la segunda entrega de la misma causa choca (409 del SDK) y se
devuelve ``duplicate=True`` sin escribir nada. La entrega no es la identidad:
quién entrega, cuándo o con qué contenido no cambia el ``id``.

El mismo contenedor guarda el lease del reconciliador (``pk = "lease"``): un
documento ``id="reconciler"`` que se toma y renueva con ``replace_item`` +
``etag`` + ``IfNotModified`` (412 al poseedor con etag viejo).

Sin ``COSMOSDB_ENDPOINT`` el contenedor es en memoria con la misma semántica
(409/412/404) para que el reconciliador tenga un único camino de código.
"""

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from azure.core import MatchConditions
from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient

from common.config.app_config import config

logger = logging.getLogger(__name__)

EVENT_KINDS = frozenset(
    {
        "clarification",
        "plan_review",
        # Incremento 4 (v4/control/incident_revalidation.py): la ocurrencia del
        # vencimiento de un INC, la decisión humana por techo y la evidencia.
        "incident_expiry",
        "human_authority",
        "reconciled",
    }
)
LEASE_PK = "lease"
LEASE_ID = "reconciler"

STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"


def event_id(kind: str, identity: str) -> str:
    return f"{kind}:{identity}"


class TransitionError(Exception):
    """El evento es válido pero no puede transicionar ahora: se difiere, no se cierra."""


@dataclass(frozen=True)
class AppendResult:
    id: str
    duplicate: bool


@dataclass
class Lease:
    holder: str
    expires_at: float
    etag: Optional[str] = None
    held: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class MemoryContainer:
    """Contenedor en memoria con la semántica del SDK que usa este módulo."""

    def __init__(self, partition_path: str = "pk") -> None:
        self.partition_path = partition_path
        self.docs: dict[str, dict[str, Any]] = {}
        self._version = 0

    def _stamp(self, body: dict[str, Any]) -> dict[str, Any]:
        self._version += 1
        doc = dict(body)
        doc["_etag"] = f'"{self._version}"'
        doc["_ts"] = self._version
        self.docs[doc["id"]] = doc
        return dict(doc)

    def _check_etag(self, item_id: str, etag: Any, match_condition: Any) -> None:
        if match_condition is None or etag is None:
            return
        current = self.docs.get(item_id, {}).get("_etag")
        if match_condition == MatchConditions.IfNotModified and current != etag:
            raise exceptions.CosmosAccessConditionFailedError(
                status_code=412, message="etag mismatch"
            )

    async def create_item(self, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if body["id"] in self.docs:
            raise exceptions.CosmosResourceExistsError(
                status_code=409, message="conflict"
            )
        return self._stamp(body)

    async def replace_item(
        self,
        item: str,
        body: dict[str, Any],
        *,
        etag: Any = None,
        match_condition: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if item not in self.docs:
            raise exceptions.CosmosResourceNotFoundError(
                status_code=404, message="not found"
            )
        self._check_etag(item, etag, match_condition)
        return self._stamp(body)

    async def read_item(
        self, item: str, partition_key: Any, **kwargs: Any
    ) -> dict[str, Any]:
        doc = self.docs.get(item)
        if doc is None or doc.get(self.partition_path) != partition_key:
            raise exceptions.CosmosResourceNotFoundError(
                status_code=404, message="not found"
            )
        return dict(doc)

    def query_items(
        self,
        query: str,
        parameters: Any = None,
        partition_key: Any = None,
        **kwargs: Any,
    ) -> Any:
        params = {p["name"]: p["value"] for p in parameters or []}

        async def _gen() -> Any:
            for doc in list(self.docs.values()):
                if (
                    partition_key is not None
                    and doc.get(self.partition_path) != partition_key
                ):
                    continue
                if any(
                    k != "@" + self.partition_path and doc.get(k[1:]) != v
                    for k, v in params.items()
                    if k.startswith("@")
                ):
                    continue
                yield dict(doc)

        return _gen()


class EventStore:
    """Eventos de trabajo + lease del reconciliador sobre un contenedor Cosmos."""

    def __init__(self, container: Any = None) -> None:
        self._container = container
        self._client: Optional[CosmosClient] = None

    async def _ensure_initialized(self) -> Any:
        if self._container is not None:
            return self._container
        endpoint, db_name = config.COSMOSDB_ENDPOINT, config.COSMOSDB_DATABASE
        if not endpoint or not db_name:
            self._container = MemoryContainer()
            logger.warning("EventStore en memoria: sin COSMOSDB_ENDPOINT")
            return self._container
        self._client = CosmosClient(
            url=endpoint, credential=config.get_cosmos_credential_async()
        )
        # El ciclo de vida se guarda desde la ADQUISICIÓN: cualquier fallo tras
        # crear el cliente lo cierra. Con el try empezando en el read, un fallo
        # al resolver la base dejaba la sesión aiohttp abierta.
        try:
            database = self._client.get_database_client(db_name)
            # Provisionado por infra/main.bicep (la cuenta prohíbe crear
            # contenedores por data-plane). Aquí sólo se abre.
            self._container = database.get_container_client(
                config.WORK_EVENTS_CONTAINER
            )
            await self._container.read()
        except BaseException as failure:
            self._container = None
            await self.aclose()
            if isinstance(failure, exceptions.CosmosResourceNotFoundError):
                raise RuntimeError(
                    f"Cosmos container '{config.WORK_EVENTS_CONTAINER}' is not "
                    "provisioned (infra/main.bicep declares work_events and "
                    "work_events_dev; deploy the infra first)"
                ) from failure
            raise
        logger.info("EventStore listo (container=%s)", config.WORK_EVENTS_CONTAINER)
        return self._container

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ── eventos ──────────────────────────────────────────────────────────────

    async def append(
        self, kind: str, identity: str, payload: dict[str, Any]
    ) -> AppendResult:
        if kind not in EVENT_KINDS:
            raise ValueError(f"kind desconocido: {kind!r}")
        if not identity:
            raise ValueError("identity vacía")
        container = await self._ensure_initialized()
        doc_id = event_id(kind, identity)
        body = {
            "id": doc_id,
            "pk": kind,
            "kind": kind,
            "identity": identity,
            "payload": payload,
            "status": STATUS_PENDING,
            "created_at": time.time(),
        }
        try:
            await container.create_item(body=body)
        except exceptions.CosmosResourceExistsError:
            return AppendResult(id=doc_id, duplicate=True)
        return AppendResult(id=doc_id, duplicate=False)

    async def find(self, kind: str, identity: str) -> Optional[dict[str, Any]]:
        """El evento de esa causa o ``None``: lectura por identidad, sin consulta."""
        container = await self._ensure_initialized()
        try:
            doc = await container.read_item(
                event_id(kind, identity), partition_key=kind
            )
        except exceptions.CosmosResourceNotFoundError:
            return None
        return dict(doc)

    async def pending(self) -> list[dict[str, Any]]:
        container = await self._ensure_initialized()
        docs = [
            dict(doc)
            async for doc in container.query_items(
                query="SELECT * FROM c WHERE c.status = @status",
                parameters=[{"name": "@status", "value": STATUS_PENDING}],
            )
        ]
        docs.sort(key=lambda d: (d.get("_ts", 0), d["id"]))
        return docs

    async def mark(
        self, event: dict[str, Any], status: str, *, error: Optional[str] = None
    ) -> bool:
        """Cierra el evento. ``False`` si otro proceso lo cerró antes (412)."""
        container = await self._ensure_initialized()
        body = {k: v for k, v in event.items() if not k.startswith("_") or k == "_etag"}
        etag = body.pop("_etag", None)
        body["status"] = status
        body["finished_at"] = time.time()
        if error is not None:
            body["error"] = error
        try:
            await container.replace_item(
                item=event["id"],
                body=body,
                etag=etag,
                match_condition=MatchConditions.IfNotModified if etag else None,
            )
        except exceptions.CosmosAccessConditionFailedError:
            return False
        return True

    # ── lease ────────────────────────────────────────────────────────────────

    async def acquire_lease(self, holder: str, ttl_seconds: float) -> Lease:
        """Toma o renueva el lease. ``held=False`` si lo tiene otro y no venció."""
        container = await self._ensure_initialized()
        now = time.time()
        expires_at = now + ttl_seconds
        body: dict[str, Any] = {
            "id": LEASE_ID,
            "pk": LEASE_PK,
            "holder": holder,
            "expires_at": expires_at,
        }
        try:
            current = await container.read_item(item=LEASE_ID, partition_key=LEASE_PK)
        except exceptions.CosmosResourceNotFoundError:
            try:
                doc = await container.create_item(body=body)
            except exceptions.CosmosResourceExistsError:
                return Lease(holder=holder, expires_at=0.0, held=False)
            return Lease(
                holder=holder,
                expires_at=expires_at,
                etag=doc["_etag"],
                held=True,
            )
        if current["holder"] != holder and current["expires_at"] > now:
            return Lease(holder=current["holder"], expires_at=current["expires_at"])
        try:
            doc = await container.replace_item(
                item=LEASE_ID,
                body=body,
                etag=current["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except exceptions.CosmosAccessConditionFailedError:
            return Lease(holder=holder, expires_at=0.0, held=False)
        return Lease(holder=holder, expires_at=expires_at, etag=doc["_etag"], held=True)

    async def release_lease(self, holder: str) -> None:
        container = await self._ensure_initialized()
        try:
            current = await container.read_item(item=LEASE_ID, partition_key=LEASE_PK)
        except exceptions.CosmosResourceNotFoundError:
            return
        if current["holder"] != holder:
            return
        try:
            await container.replace_item(
                item=LEASE_ID,
                body={
                    "id": LEASE_ID,
                    "pk": LEASE_PK,
                    "holder": holder,
                    "expires_at": 0.0,
                },
                etag=current["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except exceptions.CosmosAccessConditionFailedError:
            return


def new_holder_id() -> str:
    """``<revision>:<uuid>``: el documento del lease dice qué revisión lo tiene.

    ``CONTAINER_APP_REVISION`` lo inyecta Container Apps; en local el prefijo
    es ``local``. El lease es por contenedor y ciego a la revisión
    (INC-2026-008): el prefijo es diagnóstico, no cerca.
    """
    revision = os.environ.get("CONTAINER_APP_REVISION") or "local"
    return f"{revision}:{uuid.uuid4().hex}"


_store: Optional[EventStore] = None


def get_event_store() -> EventStore:
    global _store
    if _store is None:
        _store = EventStore()
    return _store


def set_event_store(store: Optional[EventStore]) -> None:
    """Inyección para tests y para el lifespan."""
    global _store
    _store = store


async def close_event_store() -> None:
    global _store
    if _store is not None:
        await _store.aclose()
    _store = None
