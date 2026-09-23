"""Identidad por plan en las tres transiciones de la UI, en Chromium real.

Una sola página sirve /session/:id y /plan/:id (PlanPage) en dos modos; el
plan es la identidad de todo lo que el humano responde. Este test mide:

  1. Posición Plan desde el inicio → UNA solicitud de aprobación, la del plan
     recién creado, aprobación 200 a la primera, ningún /resume_plan, agentes
     leyendo el repo real, resultado final.
  2. /session/:id con un plan aparcado → la sesión abre ese plan (la superficie
     que posee el waiting_for durable) y muestra su tarjeta.
  3. Tras el resultado final, un turno de chat en la misma página vuelve al
     carril del Router (/chat/message/stream) sin navegar.

La UI parsea /.auth/me como EasyAuth; aquí se sirve con el oid de prod, así
que cabeceras, WebSocket y el user_id que viaja a ca-mcp son la misma
identidad. Sin relojes en las esperas de estado: la señal es la URL, la
tarjeta, la ranura de clarificación o el resultado final.

Requiere frontend :3001 y backend :8000 (el árbol), y escribe planes bajo el
oid (write-shared: las ids quedan en el log del test para el inventario).

    cd tests/e2e-test
    MACAE_E2E_OID=<oid-de-prod> DISPLAY=:99 .venv/bin/pytest \\
        tests/test_plan_lane_identity.py --headed -m integration -s
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field

import pytest
from playwright.sync_api import Browser, Page

from e2e_constants import URL

APP = (URL or "http://localhost:3001").rstrip("/")
OID = os.getenv("MACAE_E2E_OID", "")
WS_NAME = "multi-agent-custom-automation-engine-solution-accelerator"
PLACEHOLDER = re.compile(
    r"Describe your task|Describe the objective|Type your message|Tell us what", re.I
)
APPROVE = re.compile(r"Approve Task Plan|Aprobar", re.I)
FINAL = re.compile(r"Group Chat Manager|Group_Chat_Manager|Final result|Resultado final", re.I)
TASK = (
    "Validación integral y NO DESTRUCTIVA del proyecto montado en el workspace: "
    "confirma rama y último commit con git de solo lectura, lee "
    "src/backend/pyproject.toml y src/frontend/package.json, ejecuta sólo las "
    "validaciones no destructivas que existan y consolida un reporte con evidencia. "
    "Si falta una herramienta, repórtalo y no instales nada."
)
ANSWER = "No autorizo instalar nada. Reporta exactamente lo que hay y termina."
SCREENSHOTS = os.path.join(os.path.dirname(__file__), "screenshots")

log = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


@dataclass
class Wire:
    """Lo que el navegador realmente envió y recibió."""

    posts: list[dict] = field(default_factory=list)
    responses: list[dict] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)
    plan_id: str = ""
    session_id: str = ""

    def frames_of(self, kind: str) -> list[dict]:
        return [f for f in self.frames if f["type"] == kind]

    def posts_to(self, fragment: str) -> list[dict]:
        return [p for p in self.posts if fragment in p["path"]]


def _wire(page: Page, oid: str) -> Wire:
    wire = Wire()

    def auth_me(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                [
                    {
                        "access_token": "",
                        "expires_on": "",
                        "id_token": "",
                        "provider_name": "aad",
                        "user_id": "e2e@local",
                        "user_claims": [
                            {
                                "typ": "http://schemas.microsoft.com/identity/claims/objectidentifier",
                                "val": oid,
                            },
                            {"typ": "name", "val": "E2E"},
                        ],
                    }
                ]
            ),
        )

    def on_request(req):
        if "/api/" in req.url and req.method == "POST":
            path = "/" + req.url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0]
            wire.posts.append(
                {"path": path, "principal": req.headers.get("x-ms-client-principal-id")}
            )

    def on_response(resp):
        if "/api/" not in resp.url:
            return
        path = "/" + resp.url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0]
        if re.search(r"process_request|resume_plan|plan_approval|user_clarification", path):
            body = resp.text()
            wire.responses.append({"path": path, "status": resp.status, "body": body[:400]})
            log.info("← %s %s %s", resp.status, path, body[:160])
            try:
                data = json.loads(body)
                wire.plan_id = data.get("plan_id") or wire.plan_id
                wire.session_id = data.get("session_id") or wire.session_id
            except ValueError:
                # Keep test flow resilient: some responses may be non-JSON.
                log.debug("Non-JSON API response ignored for %s", path)

    def on_websocket(ws):
        def on_frame(payload):
            try:
                obj = json.loads(payload)
            except (TypeError, ValueError):
                return
            kind = obj.get("type") or obj.get("event") or "?"
            data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
            wire.frames.append({"type": kind, "data": data})
            if kind not in ("agent_message_streaming",):
                log.info("WS ← %s plan=%s", kind, str(data.get("plan_id") or "")[:8])

        ws.on("framereceived", on_frame)

    page.route("**/.auth/me", auth_me)
    page.on("request", on_request)
    page.on("response", on_response)
    page.on("websocket", on_websocket)
    return wire


def _plan_of(frame: dict) -> str:
    data = frame["data"]
    return str(data.get("plan_id") or (data.get("plan") or {}).get("plan_id") or "")


def _shot(page: Page, name: str) -> None:
    os.makedirs(SCREENSHOTS, exist_ok=True)
    page.screenshot(path=os.path.join(SCREENSHOTS, f"plan_lane_identity_{name}.png"), full_page=True)


@pytest.fixture(scope="module")
def identity(browser: Browser):
    """Contexto con la identidad de prod inyectada; una sola sesión de navegador
    para las tres transiciones (la 3 continúa en la página que deja la 1)."""
    if not OID:
        pytest.skip("MACAE_E2E_OID no definido: hace falta el oid de prod de la identidad")
    context = browser.new_context(viewport={"width": 1500, "height": 1000})
    context.set_default_timeout(0)
    page = context.new_page()
    wire = _wire(page, OID)
    page.goto(APP + "/", wait_until="domcontentloaded")
    page.evaluate("(ws) => localStorage.setItem('macae_active_workspace_id', ws)", WS_NAME)
    page.reload(wait_until="domcontentloaded")
    page.get_by_role("textbox").first.wait_for()
    uid = page.evaluate("() => (window.userInfo && window.userInfo.user_id) || null")
    assert uid == OID, f"la UI no tomó la identidad: window.userInfo.user_id={uid}"
    yield page, wire
    context.close()


def _workspace_branch(page: Page) -> str:
    """Rama actual del workspace del test según el backend (GET /workspaces)."""
    listing = page.request.get(
        f"{APP}/api/v4/workspaces", headers={"x-ms-client-principal-id": OID}
    ).json()
    for ws in listing.get("workspaces", []):
        if ws.get("workspace_id") == WS_NAME:
            return str(ws.get("branch") or "")
    return ""


def _lane(page: Page) -> str:
    return (page.get_by_text(re.compile(r"^(Chat|Plan)$")).first.text_content() or "").strip()


def test_plan_lane_one_plan_one_approval_no_resume(identity):
    page, wire = identity
    if _lane(page) != "Plan":
        page.get_by_role("switch").first.click()
    assert _lane(page) == "Plan"
    box = page.get_by_placeholder(PLACEHOLDER).first
    box.fill(TASK)
    box.press("Enter")
    page.wait_for_url(re.compile(r"/plan/"))
    plan_id = page.url.rsplit("/plan/", 1)[-1].split("?")[0]
    log.info("plan creado %s sesión %s", plan_id, wire.session_id)
    _shot(page, "01-plan-created")

    approve = page.get_by_role("button", name=APPROVE)
    approve.first.wait_for()
    _shot(page, "02-plan-review")
    requests = wire.frames_of("plan_approval_request")
    assert [_plan_of(f) for f in requests] == [plan_id], (
        f"solicitudes de aprobación recibidas: {[_plan_of(f)[:8] for f in requests]}; "
        f"esperada una sola, la del plan {plan_id[:8]}"
    )
    assert wire.posts_to("resume_plan") == [], "la página originó orquestación (/resume_plan)"

    with page.expect_response(lambda r: "/plan_approval" in r.url) as got:
        approve.first.click()
    assert got.value.status == 200, got.value.text()

    clarifications = 0
    while True:
        box = page.get_by_placeholder(PLACEHOLDER).first
        if approve.count():
            pytest.fail("segunda tarjeta de aprobación: el plan se re-planificó")
        if page.get_by_text(FINAL).count():
            break
        if box.count() and box.is_enabled():
            clarifications += 1
            _shot(page, f"03-clarification-{clarifications}")
            box.fill(ANSWER)
            with page.expect_response(lambda r: "/user_clarification" in r.url) as got:
                box.press("Enter")
            assert got.value.status == 200, got.value.text()
            page.wait_for_timeout(8000)
        else:
            page.wait_for_timeout(6000)
    _shot(page, "04-final")

    assert wire.posts_to("resume_plan") == []
    assert len(wire.frames_of("plan_approval_request")) == 1
    agent_text = " ".join(str(f["data"].get("content") or "") for f in wire.frames_of("agent_message"))
    # La rama real la dice el propio resolver del backend bajo prueba (en prod
    # el clon del share; en dev, el árbol enlazado): nada de "main" a mano.
    branch = _workspace_branch(page)
    assert branch and branch in agent_text, (
        f"ningún agente reportó la rama real del workspace ({branch!r})"
    )
    log.info(
        "inventario write-shared: user_id=%s session=%s plan=%s clarificaciones=%d",
        OID, wire.session_id, plan_id, clarifications,
    )


def test_after_the_plan_the_same_page_reengages_the_router(identity):
    page, wire = identity
    assert "/plan/" in page.url, "esta transición continúa en la página del plan terminado"
    url_before = page.url
    box = page.get_by_placeholder(PLACEHOLDER).first
    box.wait_for()
    box.fill("¿En qué rama está el workspace? Responde en una línea.")
    with page.expect_response(lambda r: "/chat/message/stream" in r.url) as got:
        box.press("Enter")
    assert got.value.status == 200
    events = [
        json.loads(line[len("data: "):])
        for line in got.value.text().splitlines()
        if line.startswith("data: ")
    ]
    kinds = [e.get("type") for e in events]
    assert "intent" in kinds and "done" in kinds, kinds
    assert page.url == url_before, f"navegó durante el turno de chat: {page.url}"
    _shot(page, "05-post-chat")


def test_a_session_with_a_parked_plan_opens_that_plan(identity):
    page, wire = identity
    sessions = page.request.get(
        f"{APP}/api/v4/chat/sessions", headers={"x-ms-client-principal-id": OID}
    ).json()
    parked = None
    for s in sessions if isinstance(sessions, list) else sessions.get("sessions", []):
        sid = s.get("id") or s.get("session_id")
        if not sid:
            continue
        detail = page.request.get(
            f"{APP}/api/v4/chat/sessions/{sid}", headers={"x-ms-client-principal-id": OID}
        ).json()
        pending = detail.get("pending_plan_review") or detail.get("pending_clarification")
        if pending and pending.get("plan_id"):
            parked = (sid, pending)
            break
    if parked is None:
        pytest.skip("esta identidad no tiene ninguna sesión con plan aparcado")
    sid, pending = parked
    log.info("sesión %s aparcada en plan %s", sid, pending["plan_id"][:8])
    page.goto(f"{APP}/session/{sid}", wait_until="domcontentloaded")
    page.wait_for_url(re.compile(rf"/plan/{re.escape(pending['plan_id'])}"))
    if pending.get("request_id") and (detail.get("pending_plan_review") or {}).get("plan_id") == pending["plan_id"]:
        page.get_by_role("button", name=APPROVE).first.wait_for()
    _shot(page, "06-session-opens-parked-plan")
