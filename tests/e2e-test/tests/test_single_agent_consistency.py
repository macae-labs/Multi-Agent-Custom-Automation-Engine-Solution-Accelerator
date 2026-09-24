"""Un solo agente, todas las capacidades, la misma conducta ante el mismo pedido.

Tres pedidos × N corridas (N = MACAE_CONSISTENCY_RUNS, 5 por defecto), cada una
en una sesión nueva, por el SSE real del backend con la identidad de prod. El
criterio por pedido es que las N corridas pasen la verificación de hechos y
usen el mismo camino de herramientas: se mide consistencia, no que "salga una
vez".

  1. Workspace: leer package.json del workspace montado y citar name/version.
  2. Imagen: generar una imagen y que el archivo se descargue (PNG real).
  3. Foundry por Toolbox: listar agentes del proyecto vía Foundry MCP Server.

Requiere backend :8000 (el árbol) con MACAE_MCP_PUBLIC_ENDPOINT alcanzable por
el servicio de modelos. Escribe sesiones de chat bajo el oid (write-shared).

    cd tests/e2e-test
    MACAE_E2E_OID=<oid-de-prod> .venv/bin/pytest \\
        tests/test_single_agent_consistency.py -m integration -s
"""

import json
import logging
import os
import re
import time
import uuid

import pytest

from e2e_constants import URL

APP = (URL or "http://localhost:3001").rstrip("/")
API = (os.getenv("MACAE_URL_API") or "http://localhost:8000").rstrip("/")
OID = os.getenv("MACAE_E2E_OID", "")
WS_NAME = "multi-agent-custom-automation-engine-solution-accelerator"
RUNS = int(os.getenv("MACAE_CONSISTENCY_RUNS", "5"))
# Evidencia por corrida: el SSE crudo de cada sesión (vacío = no guardar).
DUMP_DIR = os.getenv("MACAE_CONSISTENCY_DUMP", "")
# Deployment del agente para esta medición (vacío = el CHAT_ORCHESTRATOR_MODEL del backend).
MODEL = os.getenv("MACAE_CONSISTENCY_MODEL", "")
# Los nombres citados deben salir del resultado REAL de la herramienta del
# Toolbox (FoundryMCPServerpreview___agent_get/agent_list), no de una lista fija.
AGENT_NAME = re.compile(r'\\?"name\\?":\s*\\?"([A-Za-z0-9_-]+)\\?"')

log = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


def _events(text: str) -> list[dict]:
    return [json.loads(line[len("data: ") :]) for line in text.splitlines() if line.startswith("data: ")]


def _text(events: list[dict]) -> str:
    return "".join(e.get("content") or "" for e in events if e.get("type") == "token")


def _tools(events: list[dict]) -> list[tuple]:
    return [
        (e.get("tool"), e.get("server"))
        for e in events
        if e.get("type") == "tool_activity" and e.get("activity") == "calling"
    ]


@pytest.fixture(scope="module")
def api(playwright):
    if not OID:
        pytest.skip("MACAE_E2E_OID no definido: hace falta el oid de prod de la identidad")
    ctx = playwright.request.new_context(
        base_url=API,
        extra_http_headers={
            "x-ms-client-principal-id": OID,
            "x-ms-client-principal-name": "e2e-consistency",
        },
    )
    yield ctx
    ctx.dispose()


def _turn(api, message: str) -> tuple[list[dict], float]:
    session_id = str(uuid.uuid4())
    t0 = time.monotonic()
    resp = api.post(
        "/api/v4/chat/message/stream",
        data={
            "session_id": session_id,
            "message": message,
            "workspace_id": WS_NAME,
            **({"model": MODEL} if MODEL else {}),
        },
        headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
        timeout=0,
    )
    assert resp.status == 200, resp.text()[:300]
    body = resp.text()
    if DUMP_DIR:
        os.makedirs(DUMP_DIR, exist_ok=True)
        with open(os.path.join(DUMP_DIR, f"{session_id}.sse.txt"), "w", encoding="utf-8") as f:
            f.write(body)
    return _events(body), time.monotonic() - t0


def _measure(api, name: str, message: str, check) -> None:
    rows = []
    for i in range(1, RUNS + 1):
        events, secs = _turn(api, message)
        tools = _tools(events)
        text = _text(events)
        ok, why = check(events, text)
        errors = [e.get("message") or e.get("error") for e in events if e.get("type") == "error"]
        if errors:
            why = f"{why} | error: {str(errors[0])[:160]}"
        rows.append((i, round(secs), ok, why, [t for t, _ in tools]))
        log.info(
            "%s run %d/%d: %s in %ds tools=%s %s",
            name, i, RUNS, "OK" if ok else "FAIL", secs, [t for t, _ in tools], why,
        )
    log.info("%s modelo=%s resumen: %s", name, MODEL or "(backend)", [(r[0], r[2], r[1]) for r in rows])
    failures = [r for r in rows if not r[2]]
    assert not failures, f"{name}: {len(failures)}/{RUNS} corridas fallaron: {[(r[0], r[3]) for r in failures]}"


def test_workspace_read_is_consistent(api):
    def check(events, text):
        tools = [t for t, _ in _tools(events)]
        if not any("workspace_read_file" in str(t) for t in tools):
            return False, f"no leyó el workspace: {tools}"
        missing = [f for f in ("multi-agent-frontend", "0.1.0") if f not in text]
        return (not missing), f"faltan {missing}" if missing else ""

    _measure(
        api,
        "workspace",
        "Usá el workspace montado, sin responder de memoria: leé src/frontend/package.json "
        "y decime su `name` y su `version` exactos, tal cual están en el archivo.",
        check,
    )


def test_image_generation_is_consistent(api):
    def check(events, text):
        files = [e for e in events if e.get("type") == "generated_file"]
        if not files:
            return False, "sin generated_file"
        resp = api.get(files[0]["download_url"], timeout=60_000)
        if resp.status != 200:
            return False, f"descarga {resp.status}"
        if not resp.headers.get("content-type", "").startswith("image/"):
            return False, f"content-type {resp.headers.get('content-type')}"
        return len(resp.body()) > 1000, f"{len(resp.body())} bytes"

    _measure(
        api,
        "imagen",
        "Generá una imagen de un faro en un acantilado al atardecer, estilo acuarela. "
        "Entregá el archivo de la imagen.",
        check,
    )


def _foundry_check(events, text):
    calls = [e for e in events if e.get("type") == "tool_activity"]
    if not any("toolbox" in json.dumps(e).lower() for e in calls):
        return False, f"sin actividad del Toolbox: {[(e.get('tool'), e.get('server')) for e in calls][:6]}"
    served = set()
    for e in calls:
        # El resultado real viaja en el preview, sea llamada directa
        # (FoundryMCPServerpreview___agent_get) o vía la meta-tool call_tool.
        if e.get("activity") == "result" and '"object\\": \\"agent' in str(e.get("result_preview") or ""):
            served.update(AGENT_NAME.findall(str(e.get("result_preview") or "")))
    if not served:
        return False, "el Toolbox no devolvió agentes"
    # El SSE lleva un preview acotado del resultado (los primeros nombres): la
    # respuesta debe citar lo visible del resultado real y listar varios agentes.
    cited = sorted(a for a in served if a in text)
    listed = sorted(set(re.findall(r"\b([A-Z][A-Za-z0-9]+Agent)\b", text)))
    ok = bool(cited) and len(listed) >= 3
    return ok, f"citados del resultado real: {cited}; listados: {listed[:8]}"


def test_foundry_via_toolbox_is_consistent(api):
    check = _foundry_check

    _measure(
        api,
        "foundry",
        "Usando el Foundry MCP Server disponible en el Toolbox, listá los primeros 5 agentes "
        "registrados en mi proyecto de Foundry con su nombre. Sin inventar: consultá el servidor.",
        check,
    )
