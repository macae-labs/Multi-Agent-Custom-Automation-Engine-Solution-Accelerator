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
        # El commit del registro: sin él un "sano" no dice contra qué árbol.
        "source": "",
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


@pytest.mark.asyncio
async def test_the_registry_is_not_rescanned_on_every_beat(events):
    """El loop late cada 5 s por los eventos humanos; el registro no se escanea
    cada vuelta, o haría glob y lectura de todos los JSON sobre el share."""
    inc = incident()
    scans = 0

    async def counting_registry():
        nonlocal scans
        scans += 1
        return [inc]

    rec = Reconciler(
        store=events, registry=counting_registry, execute=None, now=lambda: NOW
    )
    for _ in range(5):
        await rec.run_once()

    assert scans == 1


# ── vinculación en caliente ──────────────────────────────────────────────────
# El registro es un clon en el share: existe cuando alguien lo crea desde la UI,
# que puede ser mucho después del arranque. Medido en prod: la revisión viva
# arrancó con "ningún workspace contiene docs/incidents" y quedó sin registro ni
# ejecutor, con los 22 archivos del registro versionados en el repo.


def late_provider(pair, appears_on_call: int):
    """Devuelve ``None`` hasta la llamada ``appears_on_call``; después el par."""
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return pair if calls["n"] >= appears_on_call else None

    provider.calls = calls  # type: ignore[attr-defined]
    return provider


@pytest.mark.asyncio
async def test_a_registry_that_appears_after_startup_is_bound_and_originates_work(
    events, tmp_path
):
    inc = incident()

    async def registry():
        return [inc]

    provider = late_provider((registry, local_shell(tmp_path)), appears_on_call=3)
    rec = Reconciler(
        store=events, provider=provider, now=lambda: NOW, scan_interval=0.0
    )

    # Mientras el registro no existe no se origina trabajo y no se rompe nada.
    assert await rec.run_once() == 0
    assert await rec.run_once() == 0
    assert await events.pending() == []

    # Aparece: el barrido lo vincula, apila el vencimiento y lo aplica con el
    # ejecutor que vino en el mismo par.
    assert await rec.run_once() == 1
    reconciled = await _event(events, ir.KIND_RECONCILED, inc)
    assert reconciled["payload"]["operational"] is True
    assert (await _event(events, ir.KIND_EXPIRY, inc))["status"] == STATUS_APPLIED


@pytest.mark.asyncio
async def test_the_provider_stops_being_called_once_the_registry_is_bound(
    events, tmp_path
):
    async def registry():
        return []

    provider = late_provider((registry, local_shell(tmp_path)), appears_on_call=1)
    rec = Reconciler(
        store=events, provider=provider, now=lambda: NOW, scan_interval=0.0
    )
    for _ in range(4):
        await rec.run_once()

    assert provider.calls["n"] == 1


@pytest.mark.asyncio
async def test_an_injected_registry_wins_over_the_provider(events, tmp_path):
    """Quien arma el loop ya vinculado no paga descubrimiento: el provider del
    arranque no debe pisar una inyección explícita (tests y harnesses)."""
    inc = incident()

    async def registry():
        return [inc]

    provider = late_provider((registry, local_shell(tmp_path)), appears_on_call=1)
    rec = Reconciler(
        store=events,
        registry=registry,
        execute=local_shell(tmp_path),
        provider=provider,
        now=lambda: NOW,
        scan_interval=0.0,
    )
    await rec.run_once()

    assert provider.calls["n"] == 0


# ── carril reactivo: señal viva → candidato → confirmación por sonda ─────────
# El carril proactivo entra por el reloj. Éste entra por una alerta ya
# disparada. `alert_match` SELECCIONA candidatos; la sonda CONFIRMA, porque
# structural_match es semántico y el código no puede evaluarlo.


def con_binding(rule="macae-api-5xx", dims=None, **kw):
    inc = incident(**kw)
    inc["signature"]["alert_match"] = [
        {"rule": rule, "dimensions": dims or {"Name": "GET /x", "ResultCode": "500"}}
    ]
    return inc


def alerta(aid="alerta-1", rule="macae-api-5xx", **dims):
    return {"id": aid, "rule": rule, "dimensions": dims or {}}


def test_a_binding_matches_when_its_dimensions_are_a_subset_of_the_alert():
    inc = con_binding(dims={"ResultCode": "500"})
    hit = ir.candidates(
        "macae-api-5xx", {"Name": "GET /x", "ResultCode": "500"}, [inc]
    )
    assert [i["incident_id"] for i in hit] == [inc["incident_id"]]


def test_a_different_rule_or_value_is_not_a_candidate():
    inc = con_binding()
    assert ir.candidates("macae-excepciones", {"Name": "GET /x", "ResultCode": "500"}, [inc]) == []
    assert ir.candidates("macae-api-5xx", {"Name": "GET /x", "ResultCode": "504"}, [inc]) == []
    assert ir.candidates("macae-api-5xx", {}, [inc]) == []


def test_an_incident_without_alert_match_is_never_a_candidate():
    assert ir.candidates("macae-api-5xx", {"ResultCode": "500"}, [incident()]) == []


@pytest.mark.asyncio
async def test_a_matching_alert_produces_one_durable_fact_per_candidate(events):
    inc = con_binding()
    a = alerta(Name="GET /x", ResultCode="500")

    assert await ir.match_alerts([a], [inc], events) == 1
    assert await ir.match_alerts([a], [inc], events) == 0  # segunda lectura: 409

    doc = await events.find(
        ir.KIND_DETECTED, ir.detection_identity(inc["incident_id"], "alerta-1")
    )
    assert doc["payload"]["rule"] == "macae-api-5xx"
    assert doc["payload"]["dimensions"] == {"Name": "GET /x", "ResultCode": "500"}
    assert doc["payload"]["ambiguous_with"] == []


@pytest.mark.asyncio
async def test_the_identity_carries_the_alert_instance_so_recurrences_are_new_facts(
    events,
):
    """El mismo incidente reaparece: cada aparición es un hecho propio. Con la
    identidad puesta sólo en incident_id, la segunda alerta chocaría con 409 y
    la recurrencia se perdería."""
    inc = con_binding()
    dims = {"Name": "GET /x", "ResultCode": "500"}

    assert await ir.match_alerts([alerta("a1", **dims)], [inc], events) == 1
    assert await ir.match_alerts([alerta("a2", **dims)], [inc], events) == 1

    pendientes = [e["id"] for e in await events.pending()]
    assert sorted(pendientes) == [
        f"{ir.KIND_DETECTED}:{inc['incident_id']}:a1",
        f"{ir.KIND_DETECTED}:{inc['incident_id']}:a2",
    ]


@pytest.mark.asyncio
async def test_ambiguity_is_recorded_not_resolved(events):
    """Con dos firmas candidatas no se adivina: se registran las dos y cada
    hecho dice con quién quedó ambiguo."""
    a = con_binding(dims={"ResultCode": "500"})
    b = con_binding(dims={"Name": "GET /x"})
    b["incident_id"] = "INC-2026-999"

    assert await ir.match_alerts([alerta(Name="GET /x", ResultCode="500")], [a, b], events) == 2

    doc = await events.find(
        ir.KIND_DETECTED, ir.detection_identity(a["incident_id"], "alerta-1")
    )
    assert doc["payload"]["ambiguous_with"] == ["INC-2026-999"]


@pytest.mark.asyncio
async def test_a_red_probe_confirms_that_the_signature_reproduces(events, tmp_path):
    # Vencimiento futuro: así el único trabajo del barrido es el reactivo.
    inc = con_binding(command="exit 3", expires="2027-06-01T00:00:00Z")
    await ir.match_alerts([alerta(Name="GET /x", ResultCode="500")], [inc], events)
    rec = reconciler(events, [inc], local_shell(tmp_path))

    assert await rec.run_once() == 1

    ident = ir.detection_identity(inc["incident_id"], "alerta-1")
    doc = await events.find(ir.KIND_RECONCILED, ident)
    assert doc["payload"]["status"] == "reproduced"
    assert doc["payload"]["operational"] is False
    assert doc["payload"]["evidence"]["exit_code"] == 3
    assert (await events.find(ir.KIND_DETECTED, ident))["status"] == STATUS_APPLIED


@pytest.mark.asyncio
async def test_a_green_probe_refuses_to_attribute_the_alert(events, tmp_path):
    """El invariante se sostiene: esa alerta tiene otra causa. Se deja el hecho
    y NO se le cuelga a este incidente."""
    inc = con_binding(command="true", expires="2027-06-01T00:00:00Z")
    await ir.match_alerts([alerta(Name="GET /x", ResultCode="500")], [inc], events)

    assert await reconciler(events, [inc], local_shell(tmp_path)).run_once() == 1

    doc = await events.find(
        ir.KIND_RECONCILED, ir.detection_identity(inc["incident_id"], "alerta-1")
    )
    assert doc["payload"]["status"] == "not_reproduced"
    assert doc["payload"]["operational"] is True


@pytest.mark.asyncio
async def test_the_loop_originates_and_confirms_in_one_sweep(events, tmp_path):
    """Extremo a extremo por el reconciliador: fuente de alertas inyectada,
    emparejamiento, evento durable y confirmación por sonda, sin reloj."""
    inc = con_binding(command="exit 1", expires="2027-06-01T00:00:00Z")

    async def registry():
        return [inc]

    async def alerts():
        return [alerta("live-1", Name="GET /x", ResultCode="500")]

    rec = Reconciler(
        store=events,
        registry=registry,
        execute=local_shell(tmp_path),
        alerts=alerts,
        now=lambda: NOW,
        scan_interval=0.0,
    )

    assert await rec.run_once() == 1

    doc = await events.find(
        ir.KIND_RECONCILED, ir.detection_identity(inc["incident_id"], "live-1")
    )
    assert doc["payload"]["status"] == "reproduced"
    # El vencimiento es futuro: no se originó nada por reloj.
    assert await events.find(ir.KIND_EXPIRY, ir.expiry_identity(inc)) is None
