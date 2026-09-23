"""El carril chat con el composer, en Chromium real contra el árbol.

o4-mini por Responses decide en la misma llamada: responde, o compone una
orquestación del framework con participantes construidos por la fábrica. Este
test mide en la posición Chat:

  1. Una pregunta que no necesita herramientas la contesta el composer:
     SSE con tokens de un solo emisor, sin actividad de herramientas, sin plan.
  2. Un pedido sobre el workspace montado se compone: habla al menos un
     participante distinto del composer, hay actividad real de herramientas y
     la respuesta trae hechos del árbol que sólo salen de leerlo (el nombre y
     la versión del package.json del frontend y el requires-python del
     pyproject del backend, leídos aquí del propio árbol).

Misma identidad inyectada que test_plan_lane_identity (oid de prod vía
/.auth/me, workspace activo en localStorage). Requiere frontend :3001 y
backend :8000 (el árbol). El patrón elegido y los participantes quedan en el
log del test y en el del backend ("Composer: pattern=...").

    cd tests/e2e-test
    MACAE_E2E_OID=<oid-de-prod> DISPLAY=:99 .venv/bin/pytest \\
        tests/test_chat_lane_composer.py --headed -m integration -s
"""

import json
import logging
import os
import re

import pytest
from playwright.sync_api import Browser, Page

import tests.test_plan_lane_identity as plan_lane

log = logging.getLogger(__name__)
pytestmark = pytest.mark.integration

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PLAIN = (
    "¿Qué diferencia hay entre un handoff y un group chat en Microsoft Agent "
    "Framework? Respondé en dos líneas."
)
COMPOSED = (
    "Usá el workspace montado, sin responder de memoria: leé "
    "src/frontend/package.json y decime su `name` y su `version`; y leé "
    "src/backend/pyproject.toml y decime el valor exacto de `requires-python`. "
    "Citá los valores tal cual están en los archivos."
)
_SPEAKERS: dict[str, str] = {}


def _tree_facts() -> dict[str, str]:
    with open(os.path.join(ROOT, "src", "frontend", "package.json"), encoding="utf-8") as f:
        pkg = json.load(f)
    with open(os.path.join(ROOT, "src", "backend", "pyproject.toml"), encoding="utf-8") as f:
        m = re.search(r'^requires-python\s*=\s*"([^"]+)"', f.read(), re.M)
    assert m, "pyproject sin requires-python"
    return {"name": pkg["name"], "version": pkg["version"], "requires_python": m.group(1)}


@pytest.fixture(scope="module")
def identity(browser: Browser):
    if not plan_lane.OID:
        pytest.skip("MACAE_E2E_OID no definido: hace falta el oid de prod de la identidad")
    context = browser.new_context(viewport={"width": 1500, "height": 1000})
    context.set_default_timeout(0)
    page = context.new_page()
    wire = plan_lane._wire(page, plan_lane.OID)
    page.goto(plan_lane.APP + "/", wait_until="domcontentloaded")
    page.evaluate(
        "(ws) => localStorage.setItem('macae_active_workspace_id', ws)", plan_lane.WS_NAME
    )
    page.reload(wait_until="domcontentloaded")
    page.get_by_role("textbox").first.wait_for()
    uid = page.evaluate("() => (window.userInfo && window.userInfo.user_id) || null")
    assert uid == plan_lane.OID, f"la UI no tomó la identidad: window.userInfo.user_id={uid}"
    if plan_lane._lane(page) != "Chat":
        page.get_by_role("switch").first.click()
    assert plan_lane._lane(page) == "Chat"
    yield page, wire
    context.close()


def _chat_turn(page: Page, text: str) -> list[dict]:
    """Envía un turno en la posición Chat y devuelve los eventos SSE del stream."""
    # El selector de workspace borra la clave al montar si el listado local no
    # trae el slug (en dev el repo vive en el share de prod, no en ~/.macae; en
    # prod el listado sí lo incluye). El turno lleva el workspace que el humano
    # tiene seleccionado, así que se reafirma la selección antes de enviar.
    page.evaluate(
        "(ws) => localStorage.setItem('macae_active_workspace_id', ws)", plan_lane.WS_NAME
    )
    box = page.get_by_placeholder(plan_lane.PLACEHOLDER).first
    box.wait_for()
    box.fill(text)
    with page.expect_response(lambda r: "/chat/message/stream" in r.url) as got:
        box.press("Enter")
    assert got.value.status == 200, got.value.text()
    events = [
        json.loads(line[len("data: ") :])
        for line in got.value.text().splitlines()
        if line.startswith("data: ")
    ]
    kinds = [e.get("type") for e in events]
    log.info(
        "SSE kinds=%s speakers=%s tools=%s",
        sorted(set(kinds)),
        [e.get("agent") for e in events if e.get("type") == "agent"],
        [
            (e.get("activity"), e.get("tool"))
            for e in events
            if e.get("type") == "tool_activity"
        ][:12],
    )
    return events


def _text(events: list[dict]) -> str:
    return "".join(e.get("content") or "" for e in events if e.get("type") == "token")


def test_a_plain_question_is_answered_by_the_composer(identity):
    page, _wire = identity
    events = _chat_turn(page, PLAIN)
    kinds = [e.get("type") for e in events]
    assert "done" in kinds and "plan_created" not in kinds
    assert "tool_activity" not in kinds, (
        "una pregunta sin herramientas no puede disparar actividad de herramientas"
    )
    speakers = {e.get("agent") for e in events if e.get("type") == "agent"}
    assert len(speakers) == 1, f"habló más de un emisor: {speakers}"
    answer = _text(events)
    assert answer.strip(), "el composer no respondió"
    assert {e.get("agent") for e in events if e.get("type") == "token"} == speakers
    _SPEAKERS["composer"] = next(iter(speakers))
    plan_lane._shot(page, "chat-01-plain")
    log.info("respuesta directa de %s: %s", _SPEAKERS["composer"], answer[:240])


def test_a_workspace_request_is_composed_and_run_in_the_turn(identity):
    page, _wire = identity
    facts = _tree_facts()
    events = _chat_turn(page, COMPOSED)
    kinds = [e.get("type") for e in events]
    assert "done" in kinds
    assert "plan_created" not in kinds, (
        "el composer eligió el Plan formal (magentic); este pedido corre en el turno"
    )
    speakers = [e.get("agent") for e in events if e.get("type") == "agent"]
    participants = [s for s in speakers if s != _SPEAKERS.get("composer")]
    answer = _text(events)
    log.info("hablaron %s; respuesta: %s", speakers, answer[:400])
    assert participants, f"ningún participante compuesto habló: {speakers}"
    assert "tool_activity" in kinds, "sin actividad de herramientas no hubo lectura real"
    for key, value in facts.items():
        assert value in answer, f"la respuesta no trae {key}={value!r} del árbol"
    plan_lane._shot(page, "chat-02-composed")
    log.info("participantes que hablaron: %s", participants)
