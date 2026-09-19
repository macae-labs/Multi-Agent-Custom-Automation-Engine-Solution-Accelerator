"""Sonda de canary del HITL durable (incrementos 2 y 3) contra una revisión.

Con el token app-only (client_credentials) por la URL de la revisión, como las
sondas de INC-001/003. Dos corridas:
  A) plan → aparcado en plan_review → approve → aparcado en clarification
     (ProxyAgent) → user_clarification → terminal. Evidencia en Cosmos:
     waiting_for por etapa, work_events aplicados, checkpoints del linaje
     purgados.
  B) plan → plan_review → revise(feedback) → nuevo plan_review con otro
     request_id → approve → terminal.
Escribe planes/sesiones de prueba bajo el principal de la app (write-shared):
las ids salen al final para el inventario de INC-2026-003.

Uso (desde src/backend):
  revisión: uv run python ../../docs/incidents/probes/hitl_canary_probe.py 0000125 [team_id]
  local:    PROBE_UID=<oid> uv run python ../../docs/incidents/probes/hitl_canary_probe.py http://localhost:8010 [team_id]
  usuario real contra la revisión (device code interactivo): HITL_USER_DEVICE_CODE=1 uv run python ../../docs/incidents/probes/hitl_canary_probe.py 0000125
"""

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import turnlog_probe as tp  # noqa: E402  (token app-only, mismas constantes)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "src", "backend", ".env"))
from azure.cosmos.aio import CosmosClient  # noqa: E402

TASK = (
    "Antes de hacer cualquier otra cosa, el ProxyAgent debe preguntarme qué trimestre "
    "fiscal quiero analizar. Con mi respuesta, responde únicamente 'Listo para <trimestre>' "
    "y termina el plan sin más pasos."
)
TERMINAL = {"completed", "failed", "canceled"}


class Api:
    """Revisión: token app-only. Local (http://...): identidad real por cabeceras, como
    las inyecta EasyAuth (PROBE_UID = oid del usuario; el backend en dev corre con az login)."""

    def __init__(self, rev: str):
        self.local = rev.startswith("http://") or rev.startswith("https://localhost")
        self.base = rev if self.local else f"https://{tp.BK}--{rev}.purpleplant-4c595cea.eastus2.azurecontainerapps.io"
        if self.local:
            self.tok, self.oid = "", os.environ["PROBE_UID"]
        elif os.environ.get("HITL_USER_DEVICE_CODE") == "1":
            # Identidad REAL contra la revisión: token de usuario para la audiencia del
            # backend por device code, con la misma función que usa el backend en dev.
            # Nunca se imprime ni se guarda; vive en este proceso.
            # Audiencia = el backend (la única que EasyAuth acepta); el ámbito de
            # auth_utils es el del servidor MCP y no sirve aquí. Sin caché en disco.
            # EasyAuth del backend (medido 2026-09-17): issuer
            # login.microsoftonline.com/{tp.TEN}/v2.0 y audiencia tp.CID, con
            # unauthenticatedClientAction=AllowAnonymous: un token de otro tenant
            # entra SIN principal y el backend responde 500. Por eso el tenant es el
            # mismo del token app-only (tp.TEN) y los claims se verifican ANTES de llamar.
            import base64

            from azure.identity import DeviceCodeCredential

            # AADSTS90009 (medido): la app pide un token para sí misma, así que el
            # recurso va por GUID, no por api://; user_impersonation es el scope expuesto.
            scope = os.environ.get("HITL_USER_SCOPE", f"{tp.CID}/user_impersonation")
            credential = DeviceCodeCredential(client_id=tp.CID, tenant_id=tp.TEN)
            self.tok = credential.get_token(scope).token
            seg = self.tok.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
            # Contrato del token delegado (tenant de tokens, no el de recursos ni common):
            # tid == tp.TEN, iss == issuer v2 de ese tenant, aud == tp.CID y
            # scp con user_impersonation (un app-only trae roles, no scp).
            expected = {
                "tid": tp.TEN,
                "iss": f"https://login.microsoftonline.com/{tp.TEN}/v2.0",
                "aud": tp.CID,
            }
            wrong = {k: claims.get(k) for k, v in expected.items() if claims.get(k) != v}
            if "user_impersonation" not in claims.get("scp", "").split():
                wrong["scp"] = claims.get("scp")
            if wrong:
                raise RuntimeError(f"token fuera del contrato de EasyAuth: {wrong} (esperado {expected} + scp user_impersonation)")
            self.oid = claims["oid"]
            print(f"identidad real: tid=ok iss=ok aud=ok scp=ok oid={self.oid[:8]}…")
        else:
            self.tok, self.oid = tp.token(False)

    def headers(self):
        if self.local:
            return {"x-ms-client-principal-id": self.oid, "x-ms-client-principal-name": "probe-hitl@local",
                    "x-ms-token-aad-access-token": "probe-local-token"}
        return {"Authorization": f"Bearer {self.tok}"}

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={**self.headers(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read().decode()
                try:
                    return r.status, (json.loads(raw) if raw else None)
                except json.JSONDecodeError:
                    return r.status, raw  # texto plano (p. ej. /healthz → "OK")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:300]


class Cosmos:
    def __init__(self, local: bool = False):
        # El contenedor de eventos es el del ENTORNO BAJO PRUEBA: validando una
        # revisión desplegada es el de producción, aunque el .env local apunte
        # al de desarrollo. Leerlo del .env daba FAIL con los eventos en None.
        self.events_container = (
            os.environ.get("WORK_EVENTS_CONTAINER", "work_events") if local else "work_events"
        )
        self.client = CosmosClient(url=os.environ["COSMOSDB_ENDPOINT"], credential=os.environ["COSMOSDB_KEY"])
        self.db = self.client.get_database_client(os.environ["COSMOSDB_DATABASE"])
        self.memory = self.db.get_container_client(os.environ["COSMOSDB_CONTAINER"])

    async def plan(self, plan_id: str):
        items = self.memory.query_items(query="SELECT * FROM c WHERE c.plan_id=@p AND c.data_type='plan'",
                                        parameters=[{"name": "@p", "value": plan_id}])
        async for doc in items:
            return doc
        return None

    async def event(self, kind: str, identity: str):
        c = self.db.get_container_client(self.events_container)
        items = c.query_items(query="SELECT c.id, c.status, c.error, c.payload FROM c WHERE c.id=@i",
                              parameters=[{"name": "@i", "value": f"{kind}:{identity}"}])
        async for doc in items:
            return doc
        return None

    async def checkpoints(self, workflow_names):
        c = self.db.get_container_client("workflow_checkpoints")
        total = 0
        for name in workflow_names:
            items = c.query_items(query="SELECT VALUE COUNT(1) FROM c WHERE c.workflow_name=@w",
                                  parameters=[{"name": "@w", "value": name}], partition_key=name)
            async for n in items:
                total += n
        return total

    async def close(self):
        await self.client.close()


async def wait_for(cos: Cosmos, plan_id: str, predicate, label: str, timeout=420):
    t0 = time.time()
    while time.time() - t0 < timeout:
        doc = await cos.plan(plan_id)
        if doc is not None and predicate(doc):
            print(f"    {label}: {int(time.time() - t0)}s")
            return doc
        await asyncio.sleep(5)
    doc = await cos.plan(plan_id)
    raise TimeoutError(f"{label}: no ocurrió en {timeout}s (status={doc and doc.get('overall_status')}, waiting_for={doc and doc.get('waiting_for')})")


def parked(kind):
    return lambda d: (d.get("waiting_for") or {}).get("kind") == kind


def parked_other_than(kind, request_id):
    return lambda d: ((d.get("waiting_for") or {}).get("kind") == kind
                      and (d.get("waiting_for") or {}).get("request_id") != request_id)


def terminal(d):
    return d.get("overall_status") in TERMINAL


async def run(rev: str, team_id: str | None):
    api = Api(rev)
    cos, results, created = Cosmos(local=api.local), [], []

    def check(stage, ok, detail=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {stage}  {detail}")

    try:
        st, body = api.call("GET", "/healthz")
        check("healthz", st == 200, f"http={st}")
        st, teams = api.call("GET", "/api/v4/team_configs")
        check("team_configs", st == 200 and isinstance(teams, list), f"http={st} {teams if not isinstance(teams, list) else str(len(teams)) + ' equipos'}")
        if not isinstance(teams, list):
            raise RuntimeError(f"team_configs {st}: {teams}")
        with_proxy = [t for t in teams if any(a.get("name") == "ProxyAgent" for a in t.get("agents", []))]
        check("equipo con ProxyAgent", bool(team_id or with_proxy), f"{len(with_proxy)} de {len(teams)}")
        team_id = team_id or with_proxy[0]["team_id"]
        if os.environ.get("HITL_SMOKE") == "1":
            print("smoke: llamadas iniciales OK; sin corridas")
            return 0 if all(results) else 1
        st, body = api.call("POST", "/api/v4/select_team", {"team_id": team_id})
        check("select_team", st == 200, f"team={team_id[:8]}… http={st} {'' if st == 200 else body}")
        st, body = api.call("GET", "/api/v4/init_team?team_switched=true")
        check("init_team", st == 200, f"http={st} {'' if st == 200 else body}")

        async def new_plan(label):
            session_id = f"probe-hitl-{uuid.uuid4().hex[:8]}"
            st, plan_id = api.call("POST", "/api/v4/process_request", {"session_id": session_id, "description": TASK})
            if st != 200:
                raise RuntimeError(f"process_request {st}: {plan_id}")
            plan_id = plan_id["plan_id"] if isinstance(plan_id, dict) else plan_id
            check(f"{label} process_request", isinstance(plan_id, str) and len(plan_id) > 8, f"plan={plan_id[:8]}…")
            created.append((session_id, plan_id))
            return session_id, plan_id

        async def answer_until_terminal(label, plan_id, first_rid=None):
            """Responde clarificaciones en bucle hasta el terminal: con modelo real el
            número de preguntas no es determinista (medido en la 126: dos seguidas)."""
            answers = ["El tercer trimestre de 2026", "Listo para el tercer trimestre de 2026"]
            rids, last = [], first_rid
            for i in range(6):
                doc = await wait_for(cos, plan_id, lambda d, last=last: terminal(d) or parked_other_than("clarification", last)(d), f"{label} clarification o terminal")
                if terminal(doc):
                    return doc, rids
                wf2 = doc["waiting_for"]
                check(f"{label} pregunta del ProxyAgent #{i + 1}", bool(wf2.get("question")), f"pregunta={str(wf2.get('question'))[:70]!r}")
                if label == "A":
                    st, body = api.call("POST", "/api/v4/user_clarification", {"request_id": wf2["request_id"], "answer": answers[min(i, 1)], "plan_id": plan_id})
                else:
                    st, body = api.call("POST", "/api/v4/events", {"kind": "clarification", "request_id": wf2["request_id"], "payload": {"answer": answers[min(i, 1)]}})
                check(f"{label} clarification #{i + 1} {'user_clarification' if label == 'A' else '/events'}", st == 200, f"http={st} {body}")
                rids.append(wf2["request_id"])
                last = wf2["request_id"]
            raise RuntimeError(f"{label}: más de 6 clarificaciones seguidas")

        # ── A: approve → clarificaciones → terminal (o continuar un plan ya aparcado: RESUME_PLAN_A)
        plan_id = os.environ.get("RESUME_PLAN_A") or (await new_plan("A"))[1]
        doc = await wait_for(cos, plan_id, lambda d: parked("plan_review")(d) or parked("clarification")(d) or terminal(d), "A aparcado o terminal")
        rid_review = None
        if parked("plan_review")(doc):
            wf = doc["waiting_for"]
            check("A waiting_for.plan_review", bool(wf.get("request_id") and wf.get("checkpoint_id") and wf.get("m_plan_id")),
                  f"request_id={wf.get('request_id', '')[:8]}… names={doc.get('workflow_names')}")
            st, body = api.call("POST", "/api/v4/plan_approval", {"m_plan_id": wf["m_plan_id"], "plan_id": plan_id, "decision": "approve", "feedback": "ok"})
            check("A plan_approval approve", st == 200, f"http={st} {body}")
            rid_review = wf["request_id"]
        doc, rids_a = await answer_until_terminal("A", plan_id)
        check("A terminal completed", doc.get("overall_status") == "completed", f"status={doc.get('overall_status')} waiting_for={doc.get('waiting_for')}")
        events = ([await cos.event("plan_review", rid_review)] if rid_review else []) + [await cos.event("clarification", r) for r in rids_a]
        check("A eventos aplicados", bool(events) and all(e and e["status"] == "applied" for e in events),
              f"{[(e and e['status']) for e in events]}")
        check("A sin token en los eventos", all("user_access_token" not in json.dumps(e.get("payload", {})) for e in events if e))
        names = doc.get("workflow_names") or []
        # Cuántos segmentos haya depende de si la orquestación se reutilizó: no
        # es invariante. Lo que sí lo es: ninguno conserva checkpoints al terminar.
        check("A checkpoints del linaje purgados", bool(names) and await cos.checkpoints(names) == 0, f"segmentos={len(names)}")

        # ── B: revise → nuevo plan_review → approve → terminal
        _, plan_b = await new_plan("B")
        doc = await wait_for(cos, plan_b, parked("plan_review"), "B aparcado en plan_review")
        wfb = doc["waiting_for"]
        # B entra por /api/v4/events, el camino durable puro (A usa las rutas de la UI).
        st, body = api.call("POST", "/api/v4/events", {"kind": "plan_review", "request_id": wfb["request_id"], "payload": {"decision": "revise", "feedback": "Hazlo en un solo paso: pregunta y termina."}})
        check("B /events revise", st == 200 and body.get("status") == "recorded", f"http={st} {body}")
        doc = await wait_for(cos, plan_b, parked_other_than("plan_review", wfb["request_id"]), "B nuevo plan_review tras replan")
        wfb2 = doc["waiting_for"]
        check("B petición nueva", wfb2["request_id"] != wfb["request_id"] and wfb2.get("m_plan_id") != wfb.get("m_plan_id"), f"is_stalled={wfb2.get('is_stalled')}")
        st, body = api.call("POST", "/api/v4/events", {"kind": "plan_review", "request_id": wfb2["request_id"], "payload": {"decision": "approve"}})
        check("B /events approve del plan revisado", st == 200 and body.get("status") == "recorded", f"http={st} {body}")
        doc, _ = await answer_until_terminal("B", plan_b)
        check("B terminal completed", doc.get("overall_status") == "completed", f"status={doc.get('overall_status')}")
        evr = await cos.event("plan_review", wfb["request_id"])
        check("B evento revise aplicado", bool(evr and evr["status"] == "applied"), f"{evr and evr['status']}")
        _names_b = doc.get("workflow_names") or []
        check("B checkpoints purgados", bool(_names_b) and await cos.checkpoints(_names_b) == 0, f"segmentos={len(_names_b)}")
    except Exception as e:
        check("excepción", False, f"{type(e).__name__}: {e}")
    finally:
        await cos.close()
    print(f"\n{sum(results)}/{len(results)} PASS")
    print("inventario (write-shared, para INC-2026-003):", json.dumps({"user_id": api.oid, "sessions": [s for s, _ in created], "plans": [p for _, p in created]}))
    return 0 if all(results) else 1


if __name__ == "__main__":
    rev = sys.argv[1] if len(sys.argv) > 1 else "0000125"
    sys.exit(asyncio.run(run(rev, sys.argv[2] if len(sys.argv) > 2 else None)))
