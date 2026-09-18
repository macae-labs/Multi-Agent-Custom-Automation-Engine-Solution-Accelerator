"""Incremento 4: revalidación de INC, el primer controlador que origina trabajo.

El registro (``docs/incidents/*.json``) es la definición del trabajo: el
invariante, la sonda ejecutable, el techo de autoridad y la fecha de
vencimiento. ``work_events`` es su historial, con identidad
``<incident_id>:<expires_if_not_reverified_by>`` (la ocurrencia es el valor de
la fecha, no el reloj):

- ``incident_expiry``  la fecha venció; una ocurrencia por vencimiento (409).
- ``human_authority``  la decisión humana cuando la sonda excede el techo.
- ``reconciled``       la evidencia y el estado operacional resultante.

Git no se escribe: el estado vivo es el pliegue de ``reconciled``
(``operational_state``); avanzar la fecha en el registro es un PR humano.

Veredicto de la sonda: ``exit_code == 0`` es señal sana; cualquier otro deja el
INC en ``needs_revalidation`` con ``operational=false``. Sin shell aquí: la
capacidad ``execute(command, cwd)`` la inyecta quien arma el reconciliador
(en producción ``workspace_exec`` de ca-mcp sobre el workspace del registro).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional

from common.services.event_store import EventStore, TransitionError

logger = logging.getLogger(__name__)

KIND_EXPIRY = "incident_expiry"
KIND_AUTHORITY = "human_authority"
KIND_RECONCILED = "reconciled"
# Orden de autoridad del schema incident.v1 (authority_ceiling y actions.class).
ACTION_CLASSES = ("read-only", "write-scratch", "write-shared")
EVIDENCE_TAIL = 4000


@dataclass(frozen=True)
class Evidence:
    exit_code: int
    stdout: str
    stderr: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout[-EVIDENCE_TAIL:],
            "stderr": self.stderr[-EVIDENCE_TAIL:],
        }


Executor = Callable[[str, str], Awaitable[Evidence]]
Registry = Callable[[], Awaitable[list[dict[str, Any]]]]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def expiry_identity(incident: dict[str, Any]) -> str:
    return (
        f"{incident['incident_id']}:{incident['learn']['expires_if_not_reverified_by']}"
    )


def is_due(incident: dict[str, Any], now: datetime) -> bool:
    return _parse(incident["learn"]["expires_if_not_reverified_by"]) <= now


def exceeds_ceiling(probe_class: str, ceiling: dict[str, Any]) -> bool:
    return ACTION_CLASSES.index(probe_class) > ACTION_CLASSES.index(
        ceiling["max_action_class_without_human"]
    )


async def rearm_due(
    incidents: Iterable[dict[str, Any]], store: EventStore, now: datetime
) -> int:
    """Apila ``incident_expiry`` por cada INC vencido; devuelve los nuevos."""
    produced = 0
    for incident in incidents:
        probe = (incident.get("learn") or {}).get("executable_probe")
        if not probe or not is_due(incident, now):
            continue
        result = await store.append(
            KIND_EXPIRY,
            expiry_identity(incident),
            {
                "incident_id": incident["incident_id"],
                "probe": probe,
                "ceiling": incident["authority_ceiling"],
            },
        )
        if not result.duplicate:
            produced += 1
            logger.info("INC %s vencido: evento %s", incident["incident_id"], result.id)
    return produced


async def apply_incident_expiry(
    event: dict[str, Any], *, store: EventStore, execute: Optional[Executor]
) -> None:
    """La transición: techo → sonda → evidencia → ``reconciled``."""
    identity, payload = event["identity"], event["payload"]
    incident_id, probe, ceiling = (
        payload["incident_id"],
        payload["probe"],
        payload["ceiling"],
    )
    if execute is None:
        raise TransitionError(f"{incident_id}: sin capacidad de ejecución configurada")
    if exceeds_ceiling(probe["class"], ceiling):
        decision = await store.find(KIND_AUTHORITY, identity)
        if decision is None:
            raise TransitionError(
                f"{incident_id}: sonda {probe['class']} excede el techo "
                f"{ceiling['max_action_class_without_human']}; espera human_authority"
            )
        if decision["payload"].get("decision") != "approve":
            raise RuntimeError(f"{incident_id}: sonda rechazada por humano")
    evidence = await execute(probe["command_or_test"], probe.get("cwd", ""))
    operational = evidence.exit_code == 0
    await store.append(
        KIND_RECONCILED,
        identity,
        {
            "incident_id": incident_id,
            "operational": operational,
            "status": "verified" if operational else "needs_revalidation",
            "evidence": evidence.to_payload(),
        },
    )
    logger.info(
        "INC %s revalidado: %s (exit=%d)",
        incident_id,
        "sano" if operational else "needs_revalidation",
        evidence.exit_code,
    )


async def operational_state(
    store: EventStore, incident: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Pliegue del ``reconciled`` de este vencimiento: el estado vivo, no git."""
    doc = await store.find(KIND_RECONCILED, expiry_identity(incident))
    if doc is None:
        return None
    return {**doc["payload"], "last_verified": doc.get("_ts") or doc.get("created_at")}
