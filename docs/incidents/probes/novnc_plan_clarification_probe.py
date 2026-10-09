"""Sonda noVNC del carril de plan: aclaracion del ProxyAgent, recarga y entrada.

Por el frontend REAL (Chromium con cabeza en DISPLAY=:99, Vite :3001 -> backend
:8000). Mide, con la misma tarea del plan 36572ea2 (prod 2026-10-07):

  1. la aclaracion llega por WS como user_clarification_request y que trae
     (largo del texto, si contiene las salidas de los especialistas);
  2. el input del plan esta habilitado para responderla;
  3. tras RECARGAR la pagina con el plan aparcado: se abre el socket del plan,
     se reenvia la aclaracion pendiente y el input queda habilitado o no;
  4. al responder, el flujo sigue y, si cierra, el plan queda completado.

Uso:  DISPLAY=:99 tests/e2e-test/.venv/bin/python docs/incidents/probes/novnc_plan_clarification_probe.py <dir_salida> [frontend_url]
"""

import json
import re
import sys
import time

from playwright.sync_api import sync_playwright

OUT = sys.argv[1]
FRONTEND = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:3001"
T0 = time.monotonic()
TASK = (
    "Necesito decidir si conviene migrar una base de datos on-premise de SQL Server "
    "a la nube. Arma un plan multiagente y ejecutalo: investigar y comparar opciones "
    "de migracion de SQL Server a cloud, estimar costos aproximados, identificar "
    "riesgos tecnicos, proponer un plan de migracion por fases y verificar que el "
    "plan sea coherente con los costos, ajustandolo hasta que todo cuadre. No tengo "
    "aun datos del entorno (tamano de base, version, licencias, SLAs, region, uso): "
    "si algo es ambiguo pregunta antes de seguir. Entrega final en espanol."
)
ANSWER_1 = (
    "Avanza ahora con escenarios pequeno/mediano/grande, opciones Azure y AWS, "
    "RTO 1 hora, RPO 15 minutos, region East US, priorizar costo."
)
ANSWER_2 = "No veo brechas criticas: pasa a la consolidacion final."
ws_frames: list[dict] = []
sockets: list[str] = []


def log(msg):
    line = f"{time.monotonic() - T0:6.1f}s  {msg}"
    print(line, flush=True)
    with open(f"{OUT}/probe.log", "a") as fh:
        fh.write(line + "\n")


def on_websocket(ws):
    sockets.append(ws.url)
    log(f"WS abierto {ws.url}")

    def frame(payload):
        try:
            data = json.loads(payload)
        except Exception:
            return
        kind = data.get("type")
        ws_frames.append({"t": round(time.monotonic() - T0, 1), "type": kind, "data": data.get("data")})
        inner = data.get("data") or {}
        if kind == "user_clarification_request":
            q = str((inner.get("data") or inner).get("question") or inner.get("question") or "")
            log(f"WS user_clarification_request request_id={(inner.get('data') or inner).get('request_id')} question_len={len(q)}")
        elif kind == "agent_message":
            d = inner.get("data") or inner
            log(f"WS agent_message agent={d.get('agent_name')} len={len(str(d.get('content') or ''))}")
        elif kind in ("final_result_message", "plan_approval_request"):
            log(f"WS {kind}")

    ws.on("framereceived", lambda p: frame(p))


def last_clarification():
    for f in reversed(ws_frames):
        if f["type"] == "user_clarification_request":
            inner = f["data"] or {}
            d = inner.get("data") or inner
            return d
    return None


def input_state(pg):
    ta = pg.locator("textarea").first
    try:
        return {"disabled": ta.is_disabled(), "placeholder": ta.get_attribute("placeholder")}
    except Exception as e:
        return {"error": type(e).__name__}


def wait_for(pred, limit, label):
    t = time.monotonic()
    while time.monotonic() - t < limit:
        if pred():
            return True
        pg.wait_for_timeout(2_000)
    log(f"TIMEOUT {label} ({limit}s)")
    return False


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=["--window-size=1400,900"])
    pg = browser.new_page(viewport={"width": 1400, "height": 900})
    pg.on("websocket", on_websocket)
    pg.on("pageerror", lambda e: log(f"PAGEERROR {str(e)[:160]}"))
    pg.goto(FRONTEND, wait_until="domcontentloaded", timeout=120000)
    box = pg.get_by_placeholder("Tell us what needs planning", exact=False)
    box.wait_for(timeout=240000)
    ws_item = (
        pg.locator("[role=listitem], li, button, div")
        .filter(has_text="Multi-Agent-Custom")
        .filter(has_not_text="Multi-Agent Chat")
        .last
    )
    try:
        ws_item.click(timeout=60000)
    except Exception as e:
        log(f"workspace no clicable: {type(e).__name__}")
    time.sleep(2)
    active_ws = pg.evaluate("() => localStorage.getItem('macae_active_workspace_id')")
    log(f"workspace activo: {active_ws}")
    box.click()
    box.fill(TASK)
    box.press("Enter")
    log("tarea enviada desde Home")

    ok = wait_for(lambda: "/plan/" in pg.url, 300, "navegacion a /plan/")
    log(f"url: {pg.url}")
    plan_id = pg.url.rsplit("/plan/", 1)[-1] if ok else ""
    pg.screenshot(path=f"{OUT}/01_plan_page.png")

    approve = pg.get_by_role("button", name="Aprobar")
    if wait_for(lambda: approve.count() > 0, 240, "boton Aprobar"):
        log("Aprobar visible; click")
        approve.first.click()
    pg.screenshot(path=f"{OUT}/02_after_approve.png")

    # 1. primera aclaracion
    if wait_for(lambda: last_clarification() is not None, 400, "primera aclaracion"):
        c = last_clarification()
        q = str(c.get("question") or "")
        log(f"ACLARACION 1 len={len(q)} inicio={q[:140]!r}")
        time.sleep(3)
        log(f"input tras aclaracion 1: {input_state(pg)}")
        pg.screenshot(path=f"{OUT}/03_clarification_1.png")
        ta = pg.locator("textarea").first
        ta.click()
        ta.fill(ANSWER_1)
        ta.press("Enter")
        log("respuesta 1 enviada por el input del plan")
        n_before = len([f for f in ws_frames if f["type"] == "user_clarification_request"])
        # 2. segunda aclaracion (o final)
        def second_or_final():
            n = len([f for f in ws_frames if f["type"] == "user_clarification_request"])
            fin = any(f["type"] == "final_result_message" for f in ws_frames)
            return n > n_before or fin
        wait_for(second_or_final, 900, "segunda aclaracion o final")
        agents = [f for f in ws_frames if f["type"] == "agent_message"]
        log(f"agent_message recibidos: {[(f['data'].get('data') or f['data']).get('agent_name') for f in agents]}")
        c2 = last_clarification()
        if c2 and len([f for f in ws_frames if f["type"] == "user_clarification_request"]) > n_before:
            q2 = str(c2.get("question") or "")
            names = re.findall(r"(Azure SQL Managed Instance|Fase 0|Costos mensuales|Checklist)", q2)
            log(f"ACLARACION 2 len={len(q2)} contiene_salidas_de_especialistas={bool(names)} marcadores={sorted(set(names))} fin={q2[-160:]!r}")
            pg.screenshot(path=f"{OUT}/04_clarification_2.png")
            log(f"input antes de recargar: {input_state(pg)}")
            # 3. recarga con el plan aparcado
            frames_before = len(ws_frames)
            socks_before = len(sockets)
            pg.reload(wait_until="domcontentloaded")
            log("pagina recargada")
            time.sleep(12)
            new_socks = sockets[socks_before:]
            re_sent = [f for f in ws_frames[frames_before:] if f["type"] == "user_clarification_request"]
            log(f"tras recarga: sockets nuevos={new_socks} reenvio_aclaracion={len(re_sent)} input={input_state(pg)}")
            body = pg.evaluate("() => document.body.innerText")
            log(f"texto visible tras recarga: {len(body)} chars; contiene 'Revisa'={'Revisa' in body}")
            pg.screenshot(path=f"{OUT}/05_after_reload.png")
            st = input_state(pg)
            if not st.get("disabled"):
                ta = pg.locator("textarea").first
                ta.click()
                ta.fill(ANSWER_2)
                ta.press("Enter")
                log("respuesta 2 enviada tras recarga")
                wait_for(lambda: any(f["type"] == "final_result_message" for f in ws_frames), 600, "final tras respuesta 2")
            else:
                log("INPUT BLOQUEADO tras recarga: no se puede responder la aclaracion 2")
    else:
        log("sin aclaracion: el manager no llamo al ProxyAgent en esta corrida")
    time.sleep(5)
    log(f"url final: {pg.url}")
    pg.screenshot(path=f"{OUT}/06_final.png")
    with open(f"{OUT}/ws_frames.json", "w") as fh:
        json.dump(ws_frames, fh, ensure_ascii=False, default=str)
    browser.close()
