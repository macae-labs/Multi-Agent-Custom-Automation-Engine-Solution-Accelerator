"""Sonda: un equipo compuesto por el Router LEE el workspace, no lo imagina.

Firma del defecto (prod, rev 0000130, plan c7ab4b24, sesión autonoma-001,
2026-09-19): el Router escaló ``run_plan`` con ``WORKSPACE_ID=...`` y compuso
``RepositoryAuditAgent(mcp=False) ValidationAgent(mcp=False)
DevSecOpsAuditAgent(mcp=False)``. En los cinco minutos del run hubo CERO
llamadas a herramientas de workspace y la "auditoría" describía un
``config.yaml`` y un ``requirements.txt`` con ``pyaudio`` que no existen.

Segunda forma, medida en local el mismo día por este mismo camino: el Router SÍ
concedió ``use_mcp``, pero sólo a ``CloudDeliveryAgent``, y el manager asignó el
paso 1 —"verificar acceso real al workspace... rama, último commit, estructura
de archivos"— a ``RepositoryForensicsAgent``, que quedó con ``coding_tools`` (un
sandbox vacío) y sin workspace. ``team_capabilities.can_see_workspace`` decía
``True`` porque UNO veía. Por eso la afirmación no es "alguien ve" sino "nadie
queda ciego".

La sonda ejerce el camino real —``POST /api/v4/chat/message/stream`` con
``allow_plan=true`` y ``workspace_id``— contra un backend dev en :8010, y afirma
sobre el documento del plan (``waiting_for.team_capabilities``), no sobre el
texto del modelo. No necesita que el plan se ejecute: el defecto ya está fijado
cuando el equipo se compone.

Uso:  uv run --project src/backend python docs/incidents/probes/composed_roster_workspace_probe.py [log] [mensaje]
"""

import asyncio
import json
import pathlib
import re
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src/backend"))

BASE = "http://127.0.0.1:8010/api/v4"
# Identidad por cabecera de principal, como la entrega EasyAuth (dev local). El
# usuario por defecto del backend dev (``auth/sample_user.py``) no tiene clon
# montado; el del registro de incidentes sí.
USER = "00000000-0000-0000-0000-000000000000"
WORKSPACE = "multi-agent-custom-automation-engine-solution-accelerator"
DEFAULT_LOG = "/tmp/backend8010.log"
# Pide el carril de plan explícitamente: el Router es quien compone el roster,
# pero elige carril por su cuenta y con la frase corta suele responder en chat.
DEFAULT_MESSAGE = (
    "Arma un plan multiagente y ejecutalo: auditoria completa del proyecto que "
    "tengo montado en mi workspace. Quiero pasos, agentes especializados y "
    "hallazgos verificados contra el arbol real (rama, ultimo commit, archivos "
    "de configuracion y dependencias que existan de verdad)."
)
FAILED = 0


def check(label, ok, detail=""):
    global FAILED
    FAILED += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}".rstrip())


def stream(message: str, session_id: str) -> list:
    body = json.dumps(
        {
            "session_id": session_id,
            "message": message,
            "workspace_id": WORKSPACE,
            "allow_plan": True,
        }
    ).encode()
    req = urllib.request.Request(
        f"{BASE}/chat/message/stream",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-ms-client-principal-id": USER,
        },
    )
    events = []
    with urllib.request.urlopen(req, timeout=600) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                try:
                    events.append(json.loads(line[5:].strip()))
                except ValueError:
                    pass
    return events


async def parked_plan(plan_id: str, timeout: float = 300.0):
    """El plan se aparca en ``plan_review`` tras componer el equipo."""
    from common.database.database_factory import DatabaseFactory

    store = await DatabaseFactory.get_database(user_id=USER, tenant_id="")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for p in await store.get_all_plans():
            if p.id == plan_id and getattr(p, "waiting_for", None):
                return p.waiting_for
        await asyncio.sleep(5)
    return None


def main():
    log = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG)
    message = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MESSAGE
    session = f"probe-roster-{int(time.time())}"
    mark = log.stat().st_size if log.exists() else 0

    events = stream(message, session)
    tail = log.read_text(errors="replace")[mark:]
    plan = next((e for e in events if e.get("type") == "plan_created"), None)
    lane = re.findall(r"Router decision: function=(\w+)", tail)
    check("el Router escaló a plan", plan is not None, f"carril={lane}")
    if plan is None:
        print(f"\nFALLOS: {FAILED}   sesión={session}")
        return FAILED

    waiting = asyncio.run(parked_plan(plan["plan_id"]))
    check("el plan quedó aparcado con capacidades", bool(waiting), f"{plan['plan_id']}")
    if not waiting:
        print(f"\nFALLOS: {FAILED}   sesión={session}")
        return FAILED

    caps = waiting.get("team_capabilities") or {}
    agents = [a for a in caps.get("agents", []) if a != "ProxyAgent"]
    sighted = set(caps.get("workspace_agents") or [])
    blind = [a for a in agents if a not in sighted]
    check("ningún agente quedó ciego al workspace", blind == [], f"ciegos={blind}")

    # El manager reparte por nombre y descripción: el paso que va a mirar el
    # árbol no puede caerle a un agente sin herramientas.
    steps = (waiting.get("m_plan") or {}).get("steps") or []
    reading = [
        s
        for s in steps
        if re.search(r"workspace|repositor|git|branch|commit|archivo|file", s.get("action", ""), re.I)
    ]
    check("hay un paso que mira el árbol", bool(reading), f"pasos={len(steps)}")
    offenders = [s["agent"] for s in reading if s.get("agent") not in sighted]
    check("ese paso le toca a alguien que ve", offenders == [], f"{offenders}")

    print(f"\n{'OK' if FAILED == 0 else 'FALLOS: ' + str(FAILED)}   sesión={session}")
    print(f"  agentes={agents}\n  ven el workspace={sorted(sighted)}")
    return FAILED


if __name__ == "__main__":
    sys.exit(main())
