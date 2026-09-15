"""Sonda ejecutable del abort de turno por identidad (turn_id) — carril de voz /
continuación (ver memoria: el ingress de Container Apps NO propaga el cierre
del cliente al contenedor; INC pendiente de registro).

Uso: python abort_turn_probe.py [http://localhost:8010]

  A) turno con turn_id; tras el primer token: POST /chat/turns/{turn_id}/abort
     + cierre TCP real (SHUT_RDWR) → el generador para y NO se persiste nada
     (ni user ni assistant: la sesión no existe, 404).
  B) control: turno normal → user + assistant persistidos, en ese orden.

Señal esperada en el log del backend:
  "Chat turn <id> marked aborted by client"
  "Chat turn <id> aborted by client: stopping generation"
  "Chat turn <id> aborted by client: nothing persisted"
"""

import http.client
import json
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8010"
HOST, PORT = BASE.split("://", 1)[1].split(":")[0], int(BASE.rsplit(":", 1)[1])
H = {
    "x-ms-client-principal-id": "probe-abort-user",
    "x-ms-client-principal-name": "probe",
    # EasyAuth siempre inyecta el access token (INC-2026-002).
    "x-ms-token-aad-access-token": "probe-local-token",
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
}


def session(sid):
    req = urllib.request.Request(f"{BASE}/api/v4/chat/sessions/{sid}", headers=H)
    try:
        return [m["role"] for m in json.load(urllib.request.urlopen(req, timeout=60)).get("messages", [])]
    except urllib.error.HTTPError as e:
        return f"http {e.code}"


def stream(sid, turn_id, abort_after_first_token):
    c = http.client.HTTPConnection(HOST, PORT, timeout=120)
    body = {"session_id": sid, "message": "Escribe un párrafo de 120 palabras sobre el mar, sin herramientas.",
            "turn_id": turn_id}
    c.request("POST", "/api/v4/chat/message/stream", body=json.dumps(body), headers=H)
    r = c.getresponse()
    buf = b""
    while True:
        chunk = r.read1(512)
        if not chunk:
            break
        buf += chunk
        if abort_after_first_token and (b'"type": "token"' in buf or b'"type":"token"' in buf):
            ab = urllib.request.Request(f"{BASE}/api/v4/chat/turns/{turn_id}/abort", data=b"", method="POST", headers=H)
            res = json.load(urllib.request.urlopen(ab, timeout=30))
            # Cierre TCP REAL: sock.close() no basta mientras la respuesta mantiene su makefile().
            c.sock.shutdown(socket.SHUT_RDWR)
            c.sock.close()
            r.close()
            c.close()
            return res
    return {"completed": True}


def main():
    ok = True
    sid_a = f"probe-abort-A-{uuid.uuid4().hex[:6]}"
    tid = str(uuid.uuid4())
    res = stream(sid_a, tid, True)
    print("A) abort endpoint →", res)
    time.sleep(30)
    sa = session(sid_a)
    a_ok = res.get("aborted") is True and (sa == "http 404" or sa == [])
    ok &= a_ok
    print("A) 30s después, sesión:", sa, "→", "PASS: nada persistido" if a_ok else "FAIL")
    sid_b = f"probe-abort-B-{uuid.uuid4().hex[:6]}"
    res = stream(sid_b, str(uuid.uuid4()), False)
    print("B) control →", res)
    time.sleep(3)
    roles = session(sid_b)
    b_ok = roles == ["user", "assistant"]
    ok &= b_ok
    print("B) sesión:", roles, "→", "PASS" if b_ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
