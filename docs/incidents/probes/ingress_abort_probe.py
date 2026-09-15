"""¿Propaga el ingress de Container Apps el cierre del cliente al contenedor?

Medido 2026-09-14 contra la rev 118: NO (2/2). Con token app-only el turno de
chat falla en _bearer (~1s) y persiste un assistant vacío; si se corta el
socket apenas llegan las cabeceras y aun así aparece el assistant, el cierre
no llegó al generador. En uvicorn directo el mismo corte SÍ cancela.

Uso: python ingress_abort_probe.py [ca-pslc25991vme66zmins--0000NNN]
Requiere `az login` con acceso al secreto obo-client-secret del Container App.
"""

import http.client
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from turnlog_probe import BK, token  # noqa: E402

REV = sys.argv[1] if len(sys.argv) > 1 else f"{BK}--0000118"
HOST = f"{REV}.purpleplant-4c595cea.eastus2.azurecontainerapps.io"


def main():
    tok, _oid = token()
    propagated = 0
    for i in range(2):
        sid = f"probe-ingress-abort-{int(time.time())}-{i}"
        c = http.client.HTTPSConnection(HOST, timeout=60, context=ssl.create_default_context())
        c.request("POST", "/api/v4/chat/message/stream",
                  body=json.dumps({"session_id": sid, "message": "hola"}),
                  headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
                           "Accept": "text/event-stream"})
        r = c.getresponse()
        t0 = time.time()
        c.sock.shutdown(socket.SHUT_RDWR)
        c.sock.close()
        r.close()
        c.close()
        print(f"#{i + 1} status={r.status} → socket cortado {int((time.time() - t0) * 1000)} ms tras las cabeceras")
        time.sleep(20)
        req = urllib.request.Request(f"https://{HOST}/api/v4/chat/sessions/{sid}",
                                     headers={"Authorization": f"Bearer {tok}"})
        try:
            msgs = json.load(urllib.request.urlopen(req, timeout=60)).get("messages", [])
            has_assistant = any(m["role"] == "assistant" for m in msgs)
            propagated += 0 if has_assistant else 1
            print(f"   20s después: {[m['role'] for m in msgs]} → "
                  + ("ASSISTANT PERSISTIDO: el cierre NO llegó al generador" if has_assistant
                     else "sin assistant: el cierre SÍ cortó el generador"))
        except urllib.error.HTTPError as e:
            print("   sesión:", e.code)
    print(f"\ncierres propagados: {propagated}/2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
