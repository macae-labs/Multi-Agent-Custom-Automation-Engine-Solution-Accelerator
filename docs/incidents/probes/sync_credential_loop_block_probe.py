"""Sonda de INC-2026-005 (SIG-sync-credential-wrapped-in-aio-client-blocks-loop).

(1) Mecanismo: azure.cosmos.aio.CosmosClient acepta una credencial SÍNCRONA y
    la envuelve; su get_token corre en el hilo del event loop, así que cada
    renovación de token (IMDS en prod, az login en dev) congela todas las
    corrutinas en vuelo. Se mide con un ticker concurrente y dos credenciales
    falsas de la misma latencia: una síncrona (time.sleep) y una async
    (asyncio.sleep). Sin red: endpoint 127.0.0.1:1, el fallo posterior es el
    esperado. Señal de la firma: hueco síncrono ≈ latencia; sano: ≈ tick.
(2) Producto: ningún archivo de src/backend que construya un CosmosClient aio
    puede resolver su credencial con get_azure_credentials() ni con
    get_azure_credential_async(); la forma es config.get_cosmos_credential_async().

Uso (desde src/backend): uv run python ../../docs/incidents/probes/sync_credential_loop_block_probe.py
Salida: dos huecos en ms y la lista de sitios ofensores; exit 0 sólo si el
mecanismo se reproduce como se describe Y no hay sitios ofensores.
"""

import asyncio
import pathlib
import re
import sys
import time

from azure.core.credentials import AccessToken
from azure.cosmos.aio import CosmosClient

LATENCY_S = 0.8


class SyncSlowCredential:
    def get_token(self, *scopes, **kwargs):
        time.sleep(LATENCY_S)
        return AccessToken("t", int(time.time()) + 3600)


class AsyncSlowCredential:
    async def get_token(self, *scopes, **kwargs):
        await asyncio.sleep(LATENCY_S)
        return AccessToken("t", int(time.time()) + 3600)

    async def close(self):
        return None


async def max_loop_gap(credential) -> float:
    gaps, stop = [], False

    async def ticker():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    client = CosmosClient(url="https://127.0.0.1:1/", credential=credential)
    try:
        await asyncio.wait_for(client.get_database_client("db").read(), timeout=5)
    except Exception:
        pass  # sin servicio detrás: sólo interesa lo que pasó ANTES de enviar
    finally:
        await client.close()
    stop = True
    await task
    return max(gaps)


def offending_sites(backend_root: pathlib.Path) -> list[str]:
    bad = []
    for path in backend_root.rglob("*.py"):
        if ".venv" in path.parts or "tests" in path.parts or path.name == "app_config.py":
            continue  # app_config.py DEFINE el resolutor; se auditan los sitios de llamada
        text = path.read_text(encoding="utf-8", errors="replace")
        if "azure.cosmos.aio" not in text:
            continue
        for m in re.finditer(r"\bconfig\.get_azure_credentials?(_async)?\(", text):
            line = text.count("\n", 0, m.start()) + 1
            bad.append(f"{path.relative_to(backend_root)}:{line}: {m.group(0)}")
    return bad


def main() -> int:
    sync_gap = asyncio.run(max_loop_gap(SyncSlowCredential()))
    async_gap = asyncio.run(max_loop_gap(AsyncSlowCredential()))
    print(f"credencial síncrona: hueco máximo del loop = {sync_gap * 1000:.0f} ms (latencia {LATENCY_S * 1000:.0f} ms)")
    print(f"credencial async   : hueco máximo del loop = {async_gap * 1000:.0f} ms")
    root = pathlib.Path(__file__).resolve().parents[3] / "src" / "backend"
    bad = offending_sites(root)
    print("sitios que resuelven credencial propia para un CosmosClient aio:", bad or "ninguno")
    mechanism_ok = sync_gap >= LATENCY_S * 0.9 and async_gap < 0.1
    return 0 if mechanism_ok and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
