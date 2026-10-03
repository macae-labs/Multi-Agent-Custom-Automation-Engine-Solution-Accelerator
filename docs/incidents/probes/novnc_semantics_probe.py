"""Sonda noVNC: un turno REAL en posición Chat por la UI y lo que dejó.

Uso:
    DISPLAY=:99 tests/e2e-test/.venv/bin/python docs/incidents/probes/novnc_semantics_probe.py \
        "<tarea>" <captura.png> <user_id> [<frontend_url>] [<backend_url>]

Registra: el POST del turno (turn_id), los eventos SSE (quién habla, tool por
agente, approval_request), lo que muestra la UI al estabilizarse y el ledger
del turno por ``GET /api/v4/chat/turns/{turn_id}/ledger`` (objetivo, hecho
``compose`` con el patrón elegido, hechos por participante, veredicto, cierre).
Vive en el repo, no en un scratchpad: es evidencia reproducible (INC-2026-009,
semánticas del framework 2026-10-03).
"""

import json
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

T0 = time.monotonic()


def log(message: str) -> None:
    print(f"{time.monotonic() - T0:6.1f}s  {message}", flush=True)


TASK, SHOT, UID = sys.argv[1], sys.argv[2], sys.argv[3]
FRONTEND = sys.argv[4] if len(sys.argv) > 4 else "http://localhost:3001"
BACKEND = sys.argv[5] if len(sys.argv) > 5 else "http://127.0.0.1:8000"
LS = '() => localStorage.getItem("macae_active_workspace_id")'
turn: dict = {}
sse: list = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=["--window-size=1400,860"])
    pg = browser.new_page(viewport={"width": 1400, "height": 860})

    def on_req(r):
        if "/chat/message/stream" in r.url:
            try:
                body = json.loads(r.post_data or "{}")
            except Exception:
                body = {}
            turn["id"] = body.get("turn_id")
            log(f"POST chat turn_id={turn['id']} workspace={body.get('workspace_id')}")

    def on_resp(r):
        if "/chat/message/stream" in r.url:
            sse.append(r)

    pg.on("request", on_req)
    pg.on("response", on_resp)
    pg.on("pageerror", lambda e: log(f"PAGEERROR {str(e)[:160]}"))
    pg.goto(FRONTEND, wait_until="domcontentloaded", timeout=120000)
    pg.get_by_placeholder("Describe your task", exact=False).wait_for(timeout=240000)
    ws_item = (
        pg.locator("[role=listitem], li, button, div")
        .filter(has_text="Multi-Agent-Custom")
        .filter(has_not_text="Multi-Agent Chat")
        .last
    )
    for attempt in range(3):
        try:
            ws_item.click(timeout=240000)
            break
        except Exception as e:
            log(f"intento {attempt + 1}: selector de workspace no clicable ({type(e).__name__})")
            try:
                pg.get_by_placeholder("Describe your task", exact=False).wait_for(timeout=240000)
            except Exception:
                pass
    time.sleep(3)
    log(f"workspace activo: {pg.evaluate(LS)}")
    box = pg.get_by_placeholder("Describe your task", exact=False)
    box.click()
    box.fill(TASK)
    box.press("Enter")
    t_send = time.monotonic()
    last, stable = "", None
    while time.monotonic() - t_send < 600:
        time.sleep(3)
        try:
            body = pg.evaluate("() => document.body.innerText")
        except Exception:
            continue
        if body != last:
            last, stable = body, time.monotonic()
        elif stable and time.monotonic() - stable > 45:
            log(f"UI estable ({time.monotonic() - t_send:.0f}s tras enviar)")
            break
    pg.screenshot(path=SHOT)
    i = last.find("Multi-Agent Chat")
    print("----- UI (desde el chat) -----")
    print(last[i : i + 3500] if i >= 0 else last[-3500:])
    print("-----")
    events = []
    for r in sse:
        try:
            txt = r.text()
        except Exception as e:
            log(f"SSE no legible: {e}")
            continue
        for line in txt.splitlines():
            if line.startswith("data:"):
                try:
                    events.append(json.loads(line[5:].strip()))
                except Exception:
                    pass
    browser.close()

with open(SHOT + ".sse.json", "w") as f:
    json.dump(events, f)
kinds: dict = {}
speakers: list = []
for e in events:
    kinds[e.get("type")] = kinds.get(e.get("type"), 0) + 1
    if e.get("type") == "agent" and e.get("agent") not in speakers:
        speakers.append(e.get("agent"))
print(f"== SSE: {len(events)} eventos; tipos={kinds}")
print(f"== hablantes en orden: {speakers}")
for e in events:
    if e.get("type") == "tool_activity":
        print(
            f"   tool_activity agent={e.get('agent')} {e.get('activity')} {e.get('tool')} "
            f"args={str(e.get('args') or '')[:110]}"
        )
    if e.get("type") in ("approval_request", "plan_created", "error"):
        print(f"   {e}")
tid = turn.get("id")
if tid:
    req = urllib.request.Request(
        f"{BACKEND}/api/v4/chat/turns/{tid}/ledger", headers={"x-ms-client-principal-id": UID}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        print(f"== ledger del turno {tid}: {len(d['events'])} eventos ==")
        keys = (
            "objective", "tool", "failed", "kind", "reason", "corrected_objective",
            "new_facts", "status", "laps", "facts", "output", "pattern", "participants", "agent",
        )
        for e in d["events"]:
            pl = e["payload"]
            short = {k: (v[:110] if isinstance(v, str) else v) for k, v in pl.items() if k in keys}
            print(f"  {e['kind']:9s} {e['identity'][:78]:78s} {short}")
    except Exception as ex:
        print(f"ledger: {ex}")
