"""Reconciliador (incremento 3) sobre el doble Cosmos del conftest.

Cada test fija un hecho del contrato:
- un evento ⇒ una transición, y la segunda entrega no transiciona;
- un evento cuyo plan ya no espera esa causa se cierra ``applied`` sin tocar
  el manager;
- ``reject`` va por ``cancel_parked``; ``revise`` reanuda con feedback;
- sin lease no se leen eventos; el lease se renueva por etag;
- un evento ``pending`` sobrevive al proceso: una instancia nueva lo aplica;
- una transición que falla marca ``failed`` y no bloquea las demás;
- al plegar a terminal el linaje de checkpoints queda vacío.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from agent_framework_orchestrations._magentic import MagenticPlanReviewResponse

import v4.control.reconciler as reconciler_mod
from common.models.messages_af import Plan
from common.services.checkpoint_storage import CosmosCheckpointStorage
from common.services.event_store import STATUS_APPLIED, STATUS_FAILED, EventStore
from v4.control.reconciler import Reconciler, TransitionError, apply_event

USER = "user-1"


def _plan(kind="clarification", request_id="req-1", **extra) -> Plan:
    fields = dict(
        id="p1",
        plan_id="p1",
        session_id="s1",
        user_id=USER,
        initial_goal="g",
        team_id="team-1",
        waiting_for={"kind": kind, "request_id": request_id, "checkpoint_id": "c"},
    )
    fields.update(extra)
    return Plan(**fields)


class _Store:
    def __init__(self, plans):
        self.plans = plans
        self.team = SimpleNamespace(team_id="team-1")

    async def get_all_plans(self):
        return list(self.plans)

    async def get_team_by_id(self, team_id):
        return self.team if team_id == "team-1" else None

    async def get_plan_by_plan_id(self, plan_id):
        return next((p for p in self.plans if p.plan_id == plan_id), None)

    async def update_plan(self, plan):
        return plan


@pytest.fixture
def events(fake_cosmos_container_factory):
    return EventStore(container=fake_cosmos_container_factory(partition_path="pk"))


@pytest.fixture
def manager():
    """OrchestrationManager doblado: ``resume_orchestration``/``cancel_parked``
    consumen ``waiting_for`` como el real."""
    plans: list[Plan] = []
    store = _Store(plans)
    m = SimpleNamespace(resume_orchestration=AsyncMock(), cancel_parked=AsyncMock())

    async def _consume(user_id, *rest, **kw):
        for p in plans:
            p.waiting_for = None

    m.resume_orchestration.side_effect = _consume
    m.cancel_parked.side_effect = _consume
    get_or_new = AsyncMock()
    with (
        patch.object(
            reconciler_mod.DatabaseFactory,
            "get_database",
            AsyncMock(return_value=store),
        ),
        patch.object(reconciler_mod, "OrchestrationManager") as om,
    ):
        om.return_value = m
        om.get_current_or_new_orchestration = get_or_new
        yield SimpleNamespace(plans=plans, mock=m, build=get_or_new)


@pytest.fixture(autouse=True)
def _no_run_active():
    reconciler_mod.orchestration_config.active_runs.clear()
    yield
    reconciler_mod.orchestration_config.active_runs.clear()


@pytest.mark.asyncio
async def test_one_event_one_transition_and_duplicate_does_nothing(events, manager):
    manager.plans.append(_plan())
    rec = Reconciler(store=events)
    first_append = await events.append(
        "clarification", "req-1", {"user_id": USER, "answer": "sí"}
    )
    assert not first_append.duplicate
    second_append = await events.append(
        "clarification", "req-1", {"user_id": USER, "answer": "otra"}
    )
    assert second_append.duplicate

    assert await rec.run_once() == 1
    manager.mock.resume_orchestration.assert_awaited_once()
    args = manager.mock.resume_orchestration.await_args.args
    assert args[:4] == (USER, "s1", "p1", "req-1") and args[4].text == "sí"
    assert [
        d["status"] for d in events._container.docs.values() if d["pk"] != "lease"
    ] == [STATUS_APPLIED]
    # segunda vuelta: nada pendiente, nada transiciona
    assert await rec.run_once() == 0
    manager.mock.resume_orchestration.assert_awaited_once()


@pytest.mark.asyncio
async def test_event_for_a_plan_no_longer_parked_is_closed_without_transition(
    events, manager
):
    manager.plans.append(_plan(request_id="other"))
    await events.append("clarification", "req-1", {"user_id": USER, "answer": "x"})
    assert await Reconciler(store=events).run_once() == 1
    manager.mock.resume_orchestration.assert_not_awaited()
    manager.build.assert_not_awaited()


@pytest.mark.asyncio
async def test_reject_cancels_and_revise_resumes_with_feedback(events, manager):
    manager.plans.append(_plan(kind="plan_review", request_id="r1"))
    await events.append("plan_review", "r1", {"user_id": USER, "decision": "reject"})
    rec = Reconciler(store=events)
    await rec.run_once()
    manager.mock.cancel_parked.assert_awaited_once_with(USER, "p1", "r1")
    manager.mock.resume_orchestration.assert_not_awaited()

    manager.plans[0] = _plan(kind="plan_review", request_id="r2")
    await events.append(
        "plan_review",
        "r2",
        {"user_id": USER, "decision": "revise", "feedback": "más corto"},
    )
    await rec.run_once()  # mismo poseedor del lease
    response = manager.mock.resume_orchestration.await_args.args[4]
    assert isinstance(response, MagenticPlanReviewResponse)
    # revise(feedback) envuelve el feedback como Message; el manager replanifica con él.
    assert [m.text for m in response.review] == ["más corto"]
    manager.build.assert_awaited_once()


@pytest.mark.asyncio
async def test_without_the_lease_no_event_is_read(events, manager):
    manager.plans.append(_plan())
    await events.append("clarification", "req-1", {"user_id": USER, "answer": "x"})
    other = await events.acquire_lease("other-holder", ttl_seconds=60)
    assert other.held
    assert await Reconciler(store=events).run_once() == 0
    manager.mock.resume_orchestration.assert_not_awaited()
    # el poseedor renueva por etag; el ajeno no la roba mientras no venza
    renewed = await events.acquire_lease("other-holder", ttl_seconds=60)
    assert renewed.held and renewed.etag != other.etag


@pytest.mark.asyncio
async def test_pending_event_survives_the_process_and_a_new_instance_applies_it(
    events, manager
):
    manager.plans.append(_plan())
    await events.append("clarification", "req-1", {"user_id": USER, "answer": "x"})
    first = Reconciler(store=events)
    await first.stop()  # el proceso muere antes de iterar; libera el lease
    second = Reconciler(store=events)
    assert await second.run_once() == 1
    manager.mock.resume_orchestration.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_transition_is_marked_failed_and_does_not_block_others(
    events, manager
):
    manager.plans.append(_plan(request_id="bad"))
    manager.plans.append(_plan(request_id="good"))
    manager.plans[1].plan_id = "p2"

    async def _resume(user_id, session_id, plan_id, request_id, response):
        if request_id == "bad":
            raise RuntimeError("boom")
        for p in manager.plans:
            if p.plan_id == plan_id:
                p.waiting_for = None

    manager.mock.resume_orchestration.side_effect = _resume
    await events.append("clarification", "bad", {"user_id": USER, "answer": "x"})
    await events.append("clarification", "good", {"user_id": USER, "answer": "y"})
    assert await Reconciler(store=events).run_once() == 1
    statuses = {
        d["id"]: d["status"]
        for d in events._container.docs.values()
        if d["pk"] != "lease"
    }
    assert statuses == {
        "clarification:bad": STATUS_FAILED,
        "clarification:good": STATUS_APPLIED,
    }
    assert (
        "boom"
        in next(
            d for d in events._container.docs.values() if d["id"] == "clarification:bad"
        )["error"]
    )


@pytest.mark.asyncio
async def test_deferred_transition_stays_pending(events, manager):
    manager.plans.append(_plan(team_id="missing"))
    await events.append("clarification", "req-1", {"user_id": USER, "answer": "x"})
    with pytest.raises(TransitionError):
        await apply_event((await events.pending())[0])
    assert await Reconciler(store=events).run_once() == 0
    assert (await events.pending())[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_terminal_fold_purges_the_checkpoint_lineage(fake_cosmos_container):
    from v4.orchestration.orchestration_manager import OrchestrationManager

    storage = CosmosCheckpointStorage(container=fake_cosmos_container)
    for name in ("wf-a", "wf-b"):
        for i in range(2):
            await fake_cosmos_container.upsert_item(
                body={
                    "id": f"{name}-{i}",
                    "checkpoint_id": f"{name}-{i}",
                    "workflow_name": name,
                    "timestamp": f"2026-01-0{i + 1}",
                    "graph_signature_hash": "h",
                    "messages": {},
                    "state": {},
                    "iteration_count": i,
                    "metadata": {},
                    "version": "1.0",
                    "pending_request_info_events": {},
                    "previous_checkpoint_id": None,
                }
            )
    await fake_cosmos_container.upsert_item(
        body={
            "id": "other-0",
            "checkpoint_id": "other-0",
            "workflow_name": "other",
            "timestamp": "2026-01-01",
            "graph_signature_hash": "h",
            "messages": {},
            "state": {},
            "iteration_count": 0,
            "metadata": {},
            "version": "1.0",
            "pending_request_info_events": {},
            "previous_checkpoint_id": None,
        }
    )
    plan = _plan(workflow_names=["wf-a", "wf-b"])
    with patch(
        "v4.orchestration.orchestration_manager.get_checkpoint_storage",
        return_value=storage,
    ):
        await OrchestrationManager()._purge_checkpoint_lineage(plan)
    assert set(fake_cosmos_container.docs) == {"other-0"}


@pytest.mark.asyncio
async def test_token_never_reaches_the_document_and_travels_in_memory_to_the_transition(
    events, manager
):
    manager.plans.append(_plan())
    result = await events.append(
        "clarification", "req-1", {"user_id": USER, "answer": "x"}
    )
    rec = Reconciler(store=events)
    rec.wake(result.id, "obo-token")
    assert await rec.run_once() == 1
    assert manager.build.await_args.kwargs["user_access_token"] == "obo-token"
    stored = events._container.docs[result.id]
    assert "user_access_token" not in stored and "obo-token" not in str(stored)
    assert rec._tokens == {}  # consumido; un reinicio corre sin token


@pytest.mark.asyncio
async def test_event_container_name_comes_from_config_per_environment(monkeypatch):
    """Un contenedor de eventos por entorno: el lease es global por contenedor y un
    backend de desarrollo no puede compartir `work_events` con producción."""
    from common.config.app_config import config as app_config
    from common.services import event_store as es

    opened = []

    class _DB:
        def get_container_client(self, name):
            opened.append(name)

            class _C:
                async def read(self):
                    return {}

            return _C()

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def get_database_client(self, name):
            return _DB()

    monkeypatch.setattr(
        app_config, "COSMOSDB_ENDPOINT", "https://x.documents.azure.com:443/"
    )
    monkeypatch.setattr(app_config, "COSMOSDB_DATABASE", "db")
    monkeypatch.setattr(app_config, "WORK_EVENTS_CONTAINER", "work_events_dev")
    monkeypatch.setattr(app_config, "get_cosmos_credential_async", lambda: "key")
    monkeypatch.setattr(es, "CosmosClient", _Client)
    await EventStore()._ensure_initialized()
    assert opened == ["work_events_dev"]


@pytest.mark.asyncio
async def test_lease_document_names_the_revision_that_holds_it(events, monkeypatch):
    """INC-2026-008: el lease es ciego a la revisión; el holder al menos la nombra."""
    monkeypatch.setenv("CONTAINER_APP_REVISION", "ca-backend--0000127")
    rec = Reconciler(store=events)
    assert rec.holder.startswith("ca-backend--0000127:")
    await rec.run_once()
    lease = await events._container.read_item("reconciler", partition_key="lease")
    assert lease["holder"] == rec.holder
    monkeypatch.delenv("CONTAINER_APP_REVISION")
    assert Reconciler(store=events).holder.startswith("local:")
