"""Sonda E2E del carril de plan por el camino REAL, en local (:8010 con Router,
Responses y MCP reales): Router → equipo compuesto → plan_review → aprobación →
agentes → (clarificación → respuesta)* → final. Mide, en un solo run, los
síntomas de las raíces abiertas:

  identidad  : team_id del Auto Team, rebuild/cierre de agentes, versiones nuevas en Foundry
  checkpoint : saves fallidos (RequestEntityTooLarge) vs creados; partes en Cosmos
  HITL       : la pregunta de clarificación ¿llega al chat/plan? ¿se responde y reanuda?
  orden      : write-back al chat ANTES de la señal FINAL_RESULT por WS

Corre dos veces con el mismo mensaje para medir identidad (¿mismo team_id?).

Uso:  uv run --project src/backend python docs/incidents/probes/plan_lane_e2e_probe.py <backend_log> [mensaje]
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
USER = "00000000-0000-0000-0000-000000000000"
WORKSPACE = "multi-agent-custom-automation-engine-solution-accelerator"
HEADERS = {"Content-Type": "application/json", "x-ms-client-principal-id": USER}
MESSAGE = (
    "Arma un plan multiagente y ejecutalo: validacion integral y no destructiva "
    "del proyecto montado en mi workspace. Confirma rama y ultimo commit con git, "
    "lee src/backend/pyproject.toml y src/frontend/package.json, lista "
    ".github/workflows e infra, y si algo es ambiguo pregunta antes de seguir."
)
FINDINGS: list[str] = []


def note(label, ok, detail=""):
    FINDINGS.append(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}".rstrip())
    print(FINDINGS[-1], flush=True)


def call(method, path, body=None):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode() if body else None,
        headers=HEADERS, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def stream(message, session_id):
    req = urllib.request.Request(
        f"{BASE}/chat/message/stream",
        data=json.dumps({"session_id": session_id, "message": message,
                         "workspace_id": WORKSPACE, "allow_plan": True}).encode(),
        headers=HEADERS,
    )
    plan_id = None
    with urllib.request.urlopen(req, timeout=600) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                try:
                    ev = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if ev.get("type") == "plan_created":
                    plan_id = ev.get("plan_id")
    return plan_id


async def plan_doc(plan_id):
    from common.database.database_factory import DatabaseFactory
    store = await DatabaseFactory.get_database(user_id=USER, tenant_id="")
    for p in await store.get_all_plans():
        if p.id == plan_id:
            return p
    return None


async def drive(plan_id, log, mark, deadline=900):
    """Aprueba y responde clarificaciones hasta estado terminal; registra cada aparcada."""
    answered, t0, seen = [], time.time(), set()
    while time.time() - t0 < deadline:
        p = await plan_doc(plan_id)
        status = str(getattr(p, "overall_status", "")).split(".")[-1] if p else "?"
        wf = (getattr(p, "waiting_for", None) or {}) if p else {}
        kind, rid = wf.get("kind"), wf.get("request_id")
        if status in ("completed", "failed", "canceled"):
            return status, answered
        if kind and rid and rid not in seen:
            seen.add(rid)
            if kind == "plan_review":
                st, body = call("POST", "/plan_approval",
                                {"m_plan_id": wf.get("m_plan_id"), "plan_id": plan_id,
                                 "decision": "approve", "feedback": "ok"})
                answered.append(("plan_review", st))
                print(f"  aprobado plan_review {rid[:8]} -> http {st}", flush=True)
            elif kind == "clarification":
                q = (wf.get("question") or "").strip()
                note("HITL: la clarificación llega con pregunta", bool(q), f"q={q[:120]!r}")
                # Lo que hace la UI al refrescar: abre un socket nuevo. Sin la
                # pregunta re-enviada, el input queda bloqueado para siempre.
                got = await resent_on_connect(plan_id, rid)
                note("HITL: al reconectar el WS, la pregunta se re-envía", got, f"request_id={rid[:8]}")
                st, body = call("POST", "/user_clarification",
                                {"request_id": rid, "answer": "Procede con lo que ya tienes; no instales nada.", "plan_id": plan_id})
                answered.append(("clarification", st))
                print(f"  respondida clarificación {rid[:8]} -> http {st}", flush=True)
        await asyncio.sleep(6)
    return "timeout", answered


async def resent_on_connect(plan_id, request_id, wait=8.0):
    import websockets
    uri = f"ws://127.0.0.1:8010/api/v4/socket/{plan_id}?user_id={USER}"
    try:
        async with websockets.connect(uri) as ws:
            end = time.time() + wait
            while time.time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
                except asyncio.TimeoutError:
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                data = msg.get("data") or {}
                if msg.get("type") == "user_clarification_request" and data.get("request_id") == request_id:
                    return True
    except Exception as e:  # la sonda informa, no oculta
        print(f"  ws: {type(e).__name__}: {str(e)[:120]}", flush=True)
    return False


def tail(log, mark):
    return log.read_text(errors="replace")[mark:]


async def run_once(label, log):
    mark = log.stat().st_size
    session = f"probe-e2e-{int(time.time())}"
    plan_id = stream(MESSAGE, session)
    note(f"{label}: el Router escaló a plan", bool(plan_id), f"plan={plan_id}")
    if not plan_id:
        return None
    status, answered = await drive(plan_id, log, mark)
    text = tail(log, mark)
    team = re.search(r"Composed team 'Auto Team' \(([0-9a-f-]+)\)", text)
    team_id = team.group(1) if team else None
    rebuilds = len(re.findall(r"Rebuilding orchestration", text))
    new_versions = re.findall(r"CREATED NEW agent version '(\w+)'", text)
    ckpt_fail = len(re.findall(r"Failed to create checkpoint", text))
    ckpt_ok = len(re.findall(r"Created checkpoint:", text))
    parked = re.findall(r"parked on (\w+)", text)
    rounds = "Maximum rounds exceeded" in text
    wb = text.find("Plan result written back")
    ws = text.find("Final result sent via WebSocket")
    print(f"\n=== {label}: plan {plan_id[:8]} terminal={status} aparcadas={parked} respuestas={answered}")
    print(f"    team_id={team_id} rebuilds={rebuilds} versiones_nuevas_foundry={new_versions}")
    print(f"    checkpoints: creados={ckpt_ok} fallidos={ckpt_fail}   rounds_exceeded={rounds}")
    note(f"{label}: ningún checkpoint rechazado por tamaño", ckpt_fail == 0, f"fallidos={ckpt_fail}")
    note(f"{label}: el plan terminó (no timeout)", status in ("completed", "failed"), f"terminal={status}")
    if ws >= 0:
        note(f"{label}: write-back al chat antes de la señal WS", wb >= 0 and wb < ws)
    # ¿la pregunta de clarificación quedó en el chat de la sesión?
    from common.services.chat_cosmos_service import get_chat_cosmos_service
    svc = await get_chat_cosmos_service()
    msgs = ((await svc.get_session(session, USER)) or {}).get("messages", [])
    qs = [m for m in msgs if (m.get("metadata") or {}).get("message_type") == "clarification_question"]
    if "clarification" in parked:
        note(f"{label}: la pregunta quedó persistida en el chat", bool(qs), f"{len(qs)} preguntas")
    return {"plan_id": plan_id, "team_id": team_id, "rebuilds": rebuilds, "versions": new_versions,
            "status": status, "parked": parked}


async def main():
    log = pathlib.Path(sys.argv[1])
    global MESSAGE
    if len(sys.argv) > 2:
        MESSAGE = sys.argv[2]
    first = await run_once("run1", log)
    second = await run_once("run2", log) if first else None
    if first and second:
        note("identidad: el mismo roster reutiliza el team_id", first["team_id"] == second["team_id"],
             f"{(first['team_id'] or '')[:8]} vs {(second['team_id'] or '')[:8]}")
        note("identidad: el segundo turno no reconstruye agentes", second["rebuilds"] == 0, f"rebuilds={second['rebuilds']}")
        note("identidad: sin versiones nuevas en Foundry en el segundo run", not second["versions"], f"{second['versions']}")
    print("\n" + "\n".join(FINDINGS))
    return 0 if all(f.startswith("PASS") for f in FINDINGS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
