"""Harness del incremento 4: la revalidación de un INC como transición del reconciliador.

Clases reales: ``EventStore`` sobre el contenedor doble del conftest (409/412/404
del SDK), ``Reconciler`` y ``incident_revalidation``. El registro es una entrada
REAL de ``docs/incidents`` con la fecha vencida y la sonda sustituida por un
comando de shell; la capacidad de ejecución es un shell local, que en producción
es ``workspace_exec`` de ca-mcp. Sin reloj: el vencimiento es el valor de la
fecha comparado con un ``now`` fijo.
"""

import asyncio
import json
import pathlib
from datetime import datetime, timezone

import pytest

from common.services.event_store import (
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_PENDING,
    EventStore,
)
from v4.control import incident_revalidation as ir
from v4.control.reconciler import Reconciler

ROOT = pathlib.Path(__file__).resolve().parents[5]
ENTRY = ROOT / "docs/incidents/INC-2026-007.store-singleton-first-caller-identity.json"
NOW = datetime(2026, 12, 31, tzinfo=timezone.utc)


def incident(
    *, command="echo sano", probe_class="read-only", expires="2026-12-01T00:00:00Z"
):
    doc = json.loads(ENTRY.read_text())
    doc["learn"]["expires_if_not_reverified_by"] = expires
    doc["learn"]["executable_probe"].update(
        {"command_or_test": command, "cwd": ".", "class": probe_class}
    )
    return doc


def local_shell(root: pathlib.Path) -> ir.Executor:
    async def execute(command: str, cwd: str) -> ir.Evidence:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(root / cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return ir.Evidence(proc.returncode or 0, out.decode(), err.decode())

    return execute


@pytest.fixture
def events(fake_cosmos_container_factory):
    return EventStore(container=fake_cosmos_container_factory(partition_path="pk"))


def reconciler(events, incidents, execute) -> Reconciler:
    async def registry():
        return incidents

    return Reconciler(store=events, registry=registry, execute=execute, now=lambda: NOW)


async def _event(events, kind, inc):
    return await events.find(kind, ir.expiry_identity(inc))


@pytest.mark.asyncio
async def test_a_due_incident_produces_one_expiry_event_per_expiry_value(events):
    due, future = incident(), incident(expires="2027-06-01T00:00:00Z")

    assert await ir.rearm_due([due, future], events, NOW) == 1
    assert await ir.rearm_due([due, future], events, NOW) == 0  # segunda entrega: 409

    pending = await events.pending()
    assert [e["id"] for e in pending] == [
        f"incident_expiry:{due['incident_id']}:2026-12-01T00:00:00Z"
    ]
    assert await _event(events, ir.KIND_EXPIRY, future) is None


@pytest.mark.asyncio
async def test_healthy_probe_records_evidence_and_operational_state(events, tmp_path):
    inc = incident(command="echo sano; echo aviso >&2")

    applied = await reconciler(events, [inc], local_shell(tmp_path)).run_once()

    assert (
        applied == 1
    )  # el incident_expiry; el reconciled se cierra en la siguiente vuelta
    expiry = await _event(events, ir.KIND_EXPIRY, inc)
    assert expiry["status"] == STATUS_APPLIED
    state = await ir.operational_state(events, inc)
    assert state["operational"] is True and state["status"] == "verified"
    assert state["evidence"] == {
        "exit_code": 0,
        "stdout": "sano\n",
        "stderr": "aviso\n",
    }
    assert state["last_verified"]


@pytest.mark.asyncio
async def test_probe_without_cwd_runs_from_the_workspace_root(events):
    """``cwd`` es opcional en incident.v1: ausente no es un fallo, es la raíz."""
    inc = incident(command="pwd")
    del inc["learn"]["executable_probe"]["cwd"]
    seen: list[str] = []

    async def capturing(command, cwd):
        seen.append(cwd)
        return ir.Evidence(0, "", "")

    await reconciler(events, [inc], capturing).run_once()

    assert seen == [""]
    assert (await _event(events, ir.KIND_EXPIRY, inc))["status"] == STATUS_APPLIED
    assert (await ir.operational_state(events, inc))["operational"] is True


@pytest.mark.asyncio
async def test_failing_probe_marks_needs_revalidation(events, tmp_path):
    inc = incident(command="echo roto >&2; exit 3")

    await reconciler(events, [inc], local_shell(tmp_path)).run_once()

    state = await ir.operational_state(events, inc)
    assert state["operational"] is False and state["status"] == "needs_revalidation"
    assert (
        state["evidence"]["exit_code"] == 3 and state["evidence"]["stderr"] == "roto\n"
    )


@pytest.mark.asyncio
async def test_probe_above_the_ceiling_waits_for_human_authority(
    events, tmp_path, caplog
):
    inc = incident(
        command="echo escribe", probe_class="write-shared"
    )  # techo: write-scratch
    calls: list[str] = []
    shell = local_shell(tmp_path)

    async def counted(command, cwd):
        calls.append(command)
        return await shell(command, cwd)

    rec = reconciler(events, [inc], counted)
    with caplog.at_level("INFO"):
        assert await rec.run_once() == 0
    expiry = await _event(events, ir.KIND_EXPIRY, inc)
    assert expiry["status"] == STATUS_PENDING and calls == []
    assert "excede el techo write-scratch; espera human_authority" in caplog.text

    await events.append(
        ir.KIND_AUTHORITY, ir.expiry_identity(inc), {"decision": "approve"}
    )
    await rec.run_once()

    assert calls == ["echo escribe"]
    assert (await _event(events, ir.KIND_EXPIRY, inc))["status"] == STATUS_APPLIED
    assert (await ir.operational_state(events, inc))["operational"] is True


@pytest.mark.asyncio
async def test_rejected_authority_closes_the_expiry_as_failed_without_running(
    events, tmp_path
):
    inc = incident(command="echo escribe", probe_class="write-shared")
    calls: list[str] = []

    async def counted(command, cwd):
        calls.append(command)
        return ir.Evidence(0, "", "")

    await events.append(
        ir.KIND_AUTHORITY, ir.expiry_identity(inc), {"decision": "reject"}
    )
    await reconciler(events, [inc], counted).run_once()

    expiry = await _event(events, ir.KIND_EXPIRY, inc)
    assert expiry["status"] == STATUS_FAILED and "rechazada" in expiry["error"]
    assert calls == [] and await ir.operational_state(events, inc) is None


@pytest.mark.asyncio
async def test_without_executor_the_expiry_is_deferred_not_failed(events, caplog):
    inc = incident()

    with caplog.at_level("INFO"):
        await reconciler(events, [inc], None).run_once()

    assert (await _event(events, ir.KIND_EXPIRY, inc))["status"] == STATUS_PENDING
    assert "sin capacidad de ejecución configurada" in caplog.text


@pytest.mark.asyncio
async def test_reconciler_without_registry_originates_nothing(events):
    assert await Reconciler(store=events).run_once() == 0
    assert await events.pending() == []
