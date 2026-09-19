"""Reconciliador de ``work_event``: el único que transiciona planes parqueados.

Los endpoints sólo escriben el evento (``EventStore.append``) y despiertan al
loop en proceso (``wake()``). La durabilidad está en el documento: si el
proceso muere entre el append y la transición, el siguiente arranque lo
encuentra ``pending`` y lo aplica. La señal en proceso sólo acorta la espera.

Un poseedor a la vez: lease por etag en el mismo contenedor. Quien no tiene
el lease no lee eventos.

Transición por evento (``kind`` = ``waiting_for.kind``):
- ``clarification``  → ``resume_orchestration`` con ``Content.from_text``.
- ``plan_review``    → ``approve``/``revise`` reanudan con
  ``MagenticPlanReviewResponse``; ``reject`` → ``cancel_parked``.
Un evento cuyo plan ya no espera esa causa se cierra ``applied`` (la
transición ya ocurrió: el evento es la causa, no la entrega).
- ``incident_expiry`` → revalidación del INC (incremento 4): ejecuta la sonda
  por la capacidad inyectada y persiste ``reconciled``; ``human_authority`` y
  ``reconciled`` son hechos que esa transición consume por identidad.
El loop además origina trabajo: con un ``registry`` de INC, cada iteración
apila ``incident_expiry`` por vencimiento (``rearm_due``).
"""

import asyncio
import logging
from datetime import datetime
from time import monotonic
from typing import Any, Callable, Optional

from agent_framework import Content
from agent_framework_orchestrations._magentic import MagenticPlanReviewResponse

from common.database.database_base import DatabaseBase
from common.database.database_factory import DatabaseFactory
from common.models.messages_af import Plan
from common.services.event_store import (
    STATUS_APPLIED,
    STATUS_FAILED,
    EventStore,
    TransitionError,
    get_event_store,
    new_holder_id,
)
from v4.common.services.team_service import TeamService
from v4.config.settings import orchestration_config
from v4.control.incident_revalidation import (
    KIND_AUTHORITY,
    KIND_EXPIRY,
    KIND_RECONCILED,
    Executor,
    Registry,
    apply_incident_expiry,
    rearm_due,
    utcnow,
)
from v4.orchestration.orchestration_manager import OrchestrationManager

logger = logging.getLogger(__name__)

LEASE_TTL_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 5.0
# El loop de eventos late cada 5 s porque una decisión humana debe aplicarse ya.
# El registro de INC no: sus vencimientos son de días y escanearlo cada vuelta
# haría glob y lectura de todos los JSON sobre el share cada 5 s.
REGISTRY_SCAN_INTERVAL_SECONDS = 300.0


async def find_parked_plan(
    memory_store: DatabaseBase, *, kind: str, request_id: str
) -> Optional[Plan]:
    for plan in await memory_store.get_all_plans():
        waiting_for = plan.waiting_for or {}
        if (
            waiting_for.get("kind") == kind
            and waiting_for.get("request_id") == request_id
        ):
            return plan
    return None


def _response_for(kind: str, payload: dict[str, Any]) -> Any:
    if kind == "clarification":
        return Content.from_text(text=str(payload.get("answer") or ""))
    decision = payload["decision"]
    if decision == "revise":
        return MagenticPlanReviewResponse.revise(str(payload.get("feedback") or ""))
    return MagenticPlanReviewResponse.approve()


async def apply_event(
    event: dict[str, Any],
    *,
    user_access_token: Optional[str] = None,
    store: Optional[EventStore] = None,
    execute: Optional[Executor] = None,
) -> None:
    """Una transición por evento. Lanza ``TransitionError`` si no puede aún."""
    kind, request_id, payload = event["kind"], event["identity"], event["payload"]
    if kind == KIND_EXPIRY:
        await apply_incident_expiry(
            event, store=store or get_event_store(), execute=execute
        )
        return
    if kind in (KIND_AUTHORITY, KIND_RECONCILED):
        return  # hechos consumidos por identidad desde la transición de incident_expiry
    user_id = payload["user_id"]
    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=payload.get("tenant_id")
    )
    plan = await find_parked_plan(memory_store, kind=kind, request_id=request_id)
    if plan is None:
        logger.info("Event %s: plan no longer parked; already applied", event["id"])
        return
    manager = OrchestrationManager()
    if kind == "plan_review" and payload.get("decision") == "reject":
        await manager.cancel_parked(user_id, plan.plan_id, request_id)
        return

    team_id = plan.team_id or (plan.waiting_for or {}).get("team_id")
    team = await memory_store.get_team_by_id(team_id=team_id) if team_id else None
    if team is None:
        raise TransitionError(f"team '{team_id}' not found for plan {plan.plan_id}")
    session_id = plan.session_id
    if orchestration_config.is_run_active(session_id):
        raise TransitionError(f"run already active for session {session_id}")
    await OrchestrationManager.get_current_or_new_orchestration(
        user_id=user_id,
        team_config=team,
        team_switched=False,
        team_service=TeamService(memory_store),
        user_access_token=user_access_token,  # sólo en memoria; nunca en el documento
        # El workspace viaja en el aparcado: al reanudar, los agentes vuelven a
        # saber sobre cuál trabajan.
        workspace_id=(plan.waiting_for or {}).get("workspace_id"),
    )
    orchestration_config.mark_run_active(session_id)
    try:
        await manager.resume_orchestration(
            user_id,
            session_id,
            plan.plan_id,
            request_id,
            _response_for(kind, payload),
        )
    finally:
        orchestration_config.clear_run_active(session_id)


class Reconciler:
    def __init__(
        self,
        store: Optional[EventStore] = None,
        *,
        registry: Optional[Registry] = None,
        execute: Optional[Executor] = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        # Incremento 4: el registro de INC y la capacidad de ejecución los inyecta
        # quien arma el loop; sin ellos el reconciliador no origina trabajo y un
        # ``incident_expiry`` pendiente se difiere con su motivo en el log.
        self._registry = registry
        self._execute = execute
        self._now = now
        self._next_scan = 0.0
        self.holder = new_holder_id()
        self._wake = asyncio.Event()
        # Tokens OBO por evento, sólo en proceso: el documento nunca los lleva.
        # Tras un reinicio la transición corre sin token de usuario.
        self._tokens: dict[str, str] = {}
        self._task: Optional[asyncio.Task[None]] = None
        self._stopping = False
        self._last_seen_holder: Optional[str] = None

    @property
    def store(self) -> EventStore:
        return self._store if self._store is not None else get_event_store()

    def wake(
        self, event_id: Optional[str] = None, user_access_token: Optional[str] = None
    ) -> None:
        if event_id and user_access_token:
            self._tokens[event_id] = user_access_token
        self._wake.set()

    async def run_once(self) -> int:
        """Una iteración: lease → pendientes → una transición cada uno."""
        lease = await self.store.acquire_lease(self.holder, LEASE_TTL_SECONDS)
        if not lease.held:
            if lease.holder != self._last_seen_holder:
                logger.info(
                    "Lease held by %s; this instance is %s", lease.holder, self.holder
                )
                self._last_seen_holder = lease.holder
            return 0
        if self._last_seen_holder != self.holder:
            logger.info("Lease acquired by %s", self.holder)
            self._last_seen_holder = self.holder
        if self._registry is not None and monotonic() >= self._next_scan:
            self._next_scan = monotonic() + REGISTRY_SCAN_INTERVAL_SECONDS
            await rearm_due(await self._registry(), self.store, self._now())
        applied = 0
        for event in await self.store.pending():
            try:
                await apply_event(
                    event,
                    user_access_token=self._tokens.pop(event["id"], None),
                    store=self.store,
                    execute=self._execute,
                )
            except TransitionError as te:
                logger.info("Event %s deferred: %s", event["id"], te)
                continue
            except Exception as ex:
                logger.error("Event %s failed: %s", event["id"], ex, exc_info=True)
                await self.store.mark(event, STATUS_FAILED, error=str(ex))
                continue
            if await self.store.mark(event, STATUS_APPLIED):
                applied += 1
        return applied

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.run_once()
            except Exception as ex:
                logger.error("Reconciler iteration failed: %s", ex, exc_info=True)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._loop(), name="work-event-reconciler")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Esperado durante shutdown tras cancel(): no requiere acción.
                pass
            except Exception as ex:
                logger.warning(
                    "Reconciler task ended with error during stop: %s",
                    ex,
                    exc_info=True,
                )
            self._task = None
        try:
            await self.store.release_lease(self.holder)
        except Exception as ex:
            logger.warning("Lease release failed (non-fatal): %s", ex)


_reconciler: Optional[Reconciler] = None


def get_reconciler() -> Reconciler:
    global _reconciler
    if _reconciler is None:
        _reconciler = Reconciler()
    return _reconciler


def set_reconciler(reconciler: Optional[Reconciler]) -> None:
    global _reconciler
    _reconciler = reconciler
