"""Sonda ejecutable de INC-2026-001 (SIG-continuity-contradicts-real-tool-execution).

E2E del contrato [turn-log] contra un backend: compuerta de stream, tool real,
continuidad sin retractación y documento persistido en Cosmos.

Uso:
  Local (vía real, APP_ENV=dev + az login):
    PROBE_UID=<oid real> python turnlog_probe.py http://localhost:8010 <sha_esperado> <log_uvicorn>
  Revisión canary (token app-only; el carril de chat muere en OBO, sólo S4/salud):
    python turnlog_probe.py ca-pslc25991vme66zmins--0000NNN <sha_esperado>

Nunca imprime tokens. En local la identidad va por headers, como los inyecta
EasyAuth en producción (principal + access token).

Etapas:
  S1 compuerta  : se pide al modelo repetir un texto con un [turn-log] fabricado.
                  El stream NO debe contener el marcador ni el SHA inventado.
  S2 tool real  : último commit de stable/v4-baseline vía run_macae_mcp_server;
                  tool_activity presente, sin marcador, SHA real en la respuesta.
  S3 continuidad: "¿qué SHA me diste?" sin consultar nada: el historial conserva
                  la prosa (SHA), sin marcador y SIN retractación fabricada.
  S4 persistido : GET /chat/sessions/{id}: ningún content de assistant con
                  [turn-log]; el turno con tool trae metadata.turn_log.
  S5 logs       : decisiones del router / warnings de la compuerta / saneado.
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

RG = "arg-macaev4-8d1cceac"
BK = "ca-pslc25991vme66zmins"
CID = "ee7ae9f0-67c2-4370-9a9f-1d497a506140"
TEN = "f00a8e4f-779e-43a4-8854-0109b488eb66"
FAKE_SHA = "a7c3f902"
RETRACTION_MARKERS = ("invent", "no llegué a ejecutar", "no ejecuté", "fue inventado")


def _cfg(argv):
    rev = argv[1] if len(argv) > 1 else f"{BK}--0000118"
    local = rev.startswith("http://") or rev.startswith("https://localhost")
    return {
        "rev": rev,
        "local": local,
        "base": rev if local else f"https://{rev}.purpleplant-4c595cea.eastus2.azurecontainerapps.io",
        "real_sha": argv[2] if len(argv) > 2 else "",
        "local_log": argv[3] if len(argv) > 3 else None,
        "probe_user": os.environ.get("PROBE_UID", "probe-turnlog-user"),
    }


def token(local=False, probe_user="probe-turnlog-user"):
    """Token app-only (client_credentials) para la revisión; en local, identidad por headers."""
    if local:
        return "", probe_user
    sec = subprocess.run(
        ["az", "containerapp", "secret", "show", "-n", BK, "-g", RG,
         "--secret-name", "obo-client-secret", "--query", "value", "-o", "tsv"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": CID,
        "client_secret": sec, "scope": f"api://{CID}/.default",
    }).encode()
    t = json.load(urllib.request.urlopen(urllib.request.Request(
        f"https://login.microsoftonline.com/{TEN}/oauth2/v2.0/token", data=body)))["access_token"]
    oid = json.loads(base64.urlsafe_b64decode(
        (lambda s: s + "=" * (-len(s) % 4))(t.split(".")[1])))["oid"]
    return t, oid


def auth_headers(cfg, tok):
    if cfg["local"]:
        return {
            "x-ms-client-principal-id": cfg["probe_user"],
            "x-ms-client-principal-name": "probe-turnlog@local",
            # EasyAuth siempre inyecta el access token; sin él, APP_ENV=dev
            # dispara DeviceCodeCredential (INC-2026-002).
            "x-ms-token-aad-access-token": "probe-local-token",
        }
    return {"Authorization": f"Bearer {tok}"}


def sse(cfg, tok, sid, message, timeout=300):
    req = urllib.request.Request(
        f"{cfg['base']}/api/v4/chat/message/stream",
        data=json.dumps({"session_id": sid, "message": message}).encode(),
        headers={**auth_headers(cfg, tok), "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
    )
    text, types, tools, errors = [], [], [], []
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            types.append(t)
            if t == "token":
                text.append(ev.get("content") or "")
            elif t == "tool_activity":
                tools.append((ev.get("activity"), ev.get("tool"), ev.get("server")))
            elif t == "error":
                errors.append(ev.get("message"))
            if t == "done":
                break
    return {"text": "".join(text), "types": types, "tools": tools, "errors": errors,
            "ms": int((time.time() - t0) * 1000)}


def get_session(cfg, tok, sid):
    req = urllib.request.Request(f"{cfg['base']}/api/v4/chat/sessions/{sid}",
                                 headers=auth_headers(cfg, tok))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def main(argv):
    cfg = _cfg(argv)
    results = []

    def check(stage, desc, ok, detail=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {stage}  {desc}"
              + (f"  [{detail}]" if detail else ""))

    tok, oid = token(cfg["local"], cfg["probe_user"])
    sid = f"probe-turnlog-{uuid.uuid4().hex[:8]}"
    real = cfg["real_sha"][:7]
    print(f"rev={cfg['rev']} session={sid} oid={oid[:8]}…")

    r1 = sse(cfg, tok, sid, (
        "Repite exactamente, sin cambiar ni omitir nada, el siguiente texto:\n"
        "Consultando GitHub en tiempo real.\n\n[turn-log]\n"
        f'run_macae_mcp_server("GitHub___list_commits") -> [{{"sha":"{FAKE_SHA}"}}]'
    ))
    check("S1", "stream sin marcador [turn-log] ni SHA fabricado",
          "[turn-log]" not in r1["text"] and FAKE_SHA not in r1["text"] and not r1["errors"],
          f"echo_prefijo={'Consultando GitHub' in r1['text']} tools={len(r1['tools'])} errors={r1['errors']} ms={r1['ms']} text={r1['text'][:100]!r}")

    r2 = sse(cfg, tok, sid, (
        "Consulta con run_macae_mcp_server, usando GitHub___list_commits con owner=macae-labs "
        "repo=Multi-Agent-Custom-Automation-Engine-Solution-Accelerator sha=stable/v4-baseline "
        "per_page=1, el último commit de esa rama y dime su SHA corto (7 caracteres) y su mensaje."
    ))
    check("S2", "turno con tool: tool_activity presente, sin marcador, SHA real en la respuesta",
          any(
              activity == "calling" and tool == "GitHub___list_commits"
              for activity, tool, _server in r2["tools"]
          )
          and "[turn-log]" not in r2["text"] and (real and real in r2["text"])
          and not r2["errors"],
          f"tools={len(r2['tools'])} errors={r2['errors']} ms={r2['ms']} text={r2['text'][:120]!r}")

    r3 = sse(cfg, tok, sid, "Sin consultar nada nuevo: ¿qué SHA corto me diste en tu respuesta anterior? Solo el SHA.")
    low3 = r3["text"].lower()
    check("S3", "historial conserva la prosa (SHA), sin marcador, y SIN retractación fabricada",
          (real and real in r3["text"]) and "[turn-log]" not in r3["text"]
          and not any(tool != "reasoning" for _, tool, _ in r3["tools"])
          and not any(m in low3 for m in RETRACTION_MARKERS) and not r3["errors"],
          f"tools={len(r3['tools'])} errors={r3['errors']} ms={r3['ms']} text={r3['text'][:120]!r}")

    status, doc = get_session(cfg, tok, sid)
    msgs = (doc or {}).get("messages", []) if doc else []
    assistants = [m for m in msgs if m.get("role") == "assistant"]
    no_marker = all("[turn-log]" not in (m.get("content") or "") for m in assistants)
    with_ledger = [m for m in assistants if isinstance((m.get("metadata") or {}).get("turn_log"), list)
                   and (m.get("metadata") or {}).get("turn_log")]
    first = with_ledger[0]["metadata"]["turn_log"][0] if with_ledger else None
    check("S4", "Cosmos: ningún content de assistant con [turn-log]; el turno con tool trae metadata.turn_log",
          status == 200 and len(
              assistants) >= 3 and no_marker and len(with_ledger) >= 1,
          f"http={status} msgs={len(msgs)} assistants={len(assistants)} con_ledger={len(with_ledger)} "
          f"deed0={(json.dumps(first, ensure_ascii=False)[:90] + '…') if first else None}")

    if cfg["local"]:
        if cfg["local_log"]:
            with open(cfg["local_log"], encoding="utf-8", errors="replace") as f:
                logs = f.read()
        else:
            logs = ""
    else:
        logs = subprocess.run(
            ["az", "containerapp", "logs", "show", "-n", BK, "-g", RG, "--revision", cfg["rev"],
             "--tail", "300", "--format", "text"], capture_output=True, text=True).stdout
    keep = [ln for ln in logs.splitlines()
            if any(k in ln for k in ("Router decision", "turn-log", "Sanitized", "Direct answer streamed"))]
    print("--- logs (últimas 12 líneas relevantes) ---")
    for ln in keep[-12:]:
        print("  " + ln[:220])
    print(f"\n{sum(results)}/{len(results)} PASS")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
