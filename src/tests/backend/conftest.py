"""
Pytest configuration for backend tests.

This module handles proper test isolation and minimal external module mocking.
"""

import atexit
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest


class TelemetryStub:
    """Endpoint local de Azure Monitor para los tests.

    Acepta la ingestión (POST …/v2.1/track, lista JSON de envelopes) y live
    metrics (POST /QuickPulseService.svc/ping|post, responde no-suscrito) y
    registra cada envelope recibido. Permite afirmar identidad: lo que el SDK
    emitió es lo que llegó. Puerto efímero; se cierra en atexit.
    """

    def __init__(self) -> None:
        self.received: list = []
        self._lock = threading.Lock()
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # silencio: no es salida de test
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                envelopes: list = []
                try:
                    parsed = json.loads(body.decode("utf-8")) if body else []
                    envelopes = parsed if isinstance(parsed, list) else [parsed]
                except ValueError:
                    for line in body.decode("utf-8", "replace").splitlines():
                        try:
                            envelopes.append(json.loads(line))
                        except ValueError:
                            pass
                with stub._lock:
                    stub.received.append(
                        {
                            "path": self.path,
                            "content_type": self.headers.get("Content-Type"),
                            "content_encoding": self.headers.get("Content-Encoding"),
                            "raw_len": len(body),
                            "envelopes": envelopes,
                        }
                    )
                if "QuickPulseService.svc" in self.path:
                    payload = b"{}"
                    self.send_response(200)
                    self.send_header("x-ms-qps-subscribed", "false")
                else:
                    payload = json.dumps(
                        {
                            "itemsReceived": len(envelopes),
                            "itemsAccepted": len(envelopes),
                            "errors": [],
                        }
                    ).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="telemetry-stub", daemon=True
        )
        self._thread.start()
        atexit.register(self.close)

    @property
    def connection_string(self) -> str:
        return (
            "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
            f"IngestionEndpoint=http://127.0.0.1:{self.port}/;"
            f"LiveEndpoint=http://127.0.0.1:{self.port}/"
        )

    def envelopes(self, path_part: str = "track") -> list:
        with self._lock:
            return [
                e
                for r in self.received
                if path_part in r["path"]
                for e in r["envelopes"]
            ]

    def clear(self) -> None:
        with self._lock:
            self.received.clear()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


TELEMETRY_STUB: "TelemetryStub | None" = None


@pytest.fixture
def telemetry_stub() -> TelemetryStub:
    """El stub local de Azure Monitor, vaciado al inicio de cada test."""
    assert TELEMETRY_STUB is not None
    TELEMETRY_STUB.clear()
    return TELEMETRY_STUB


def _setup_environment_variables():
    """Set up required environment variables for testing."""
    env_vars = {
        "AZURE_AI_SUBSCRIPTION_ID": "test-subscription",
        "AZURE_AI_RESOURCE_GROUP": "test-rg",
        "AZURE_AI_PROJECT_NAME": "test-project",
        "AZURE_AI_AGENT_ENDPOINT": "https://test.agent.endpoint.com",
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
        "AZURE_OPENAI_API_KEY": "test-key",
        "AZURE_OPENAI_API_VERSION": "2023-05-15",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "test-deployment",
        "PROJECT_CONNECTION_STRING": "test-connection",
        "AZURE_COSMOS_ENDPOINT": "https://test.cosmos.azure.com",
        "AZURE_COSMOS_KEY": "test-key",
        "AZURE_COSMOS_DATABASE_NAME": "test-db",
        "AZURE_COSMOS_CONTAINER_NAME": "test-container",
        "FRONTEND_SITE_NAME": "http://localhost:3000",
        "AZURE_STORAGE_BLOB_URL": "https://test.blob.core.windows.net",
        "APP_ENV": "dev",
        "AZURE_OPENAI_RAI_DEPLOYMENT_NAME": "test-rai-deployment",
    }
    for key, value in env_vars.items():
        os.environ.setdefault(key, value)
    # Azure Monitor REAL sobre un stub HTTP local: configure_azure_monitor,
    # live metrics y la instrumentación corren de verdad y exportan a un
    # endpoint que responde, sin ningún servicio remoto. Con la clave falsa
    # contra los endpoints reales, exportador y QuickPulse esperaban al
    # servicio al cerrar el intérprete y CI quedó 33 min colgado tras
    # "29 passed" (2026-09-14). Asignación explícita, no setdefault: el .env
    # local traía la cadena REAL y los tests enviaban telemetría a producción.
    global TELEMETRY_STUB
    if TELEMETRY_STUB is None:
        TELEMETRY_STUB = TelemetryStub()
    os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = (
        TELEMETRY_STUB.connection_string
    )
    # Los otros dos canales remotos del exportador, apagados con sus switches:
    # statsbeat (westus-0.in.applicationinsights.azure.com) y el control plane
    # OneSettings (settings.sdk.monitor.azure.com).
    os.environ["APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL"] = "true"
    # Cosmos: ASIGNADO vacío, no setdefault. El .env local trae el endpoint de
    # producción y el reconciliador arranca en el lifespan de la app: con el
    # endpoint heredado intentó crear `work_events` en la cuenta real desde
    # test_app (2026-09-17, Forbidden por política de la cuenta) y dejó un
    # socket abierto que el gate de ResourceWarning señaló. Sin endpoint, los
    # servicios usan sus modos en memoria.
    os.environ["COSMOSDB_ENDPOINT"] = ""
    os.environ["COSMOSDB_KEY"] = ""
    os.environ["APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED"] = "true"


# Set up environment and minimal mocks before any test imports
_setup_environment_variables()


@pytest.fixture(autouse=True)
def _no_interactive_credential_under_pytest(monkeypatch):
    """INV-NO-INTERACTIVE-CREDENTIAL-UNDER-PYTEST (INC-2026-002): bajo pytest,
    _dev_acquire_user_token falla con nombre en vez de abrir DeviceCodeCredential
    y esperar a un humano. El test debe traer el token como lo inyecta EasyAuth
    (x-ms-token-aad-access-token). Se parchea en el módulo real que usa el
    router (auth.auth_utils); ya no existe un segundo nombre para ese archivo."""
    import importlib

    auth_utils = importlib.import_module("auth.auth_utils")

    def _fail(*_args, **_kwargs):
        raise AssertionError(
            "INV-NO-INTERACTIVE-CREDENTIAL-UNDER-PYTEST: la petición del test no "
            "trae access token y la cadena de auth intentó DeviceCodeCredential. "
            "Añade x-ms-token-aad-access-token (o Authorization) al scope."
        )

    monkeypatch.setattr(auth_utils, "_dev_acquire_user_token", _fail)


@pytest.fixture
def mock_azure_services():
    """Fixture to provide common Azure service mocks."""
    return {
        "cosmos_client": Mock(),
        "openai_client": Mock(),
        "ai_project_client": Mock(),
        "credential": Mock(),
    }


class FakeCosmosContainer:
    """Doble en memoria de la superficie de ContainerProxy (aio) que usan los
    servicios sobre Cosmos, con la semántica del SDK que importa al plano de
    control: ``create_item`` con id repetido → CosmosResourceExistsError (409);
    ``replace_item``/``upsert_item`` con ``etag`` y ``MatchConditions.IfNotModified``
    sobre un etag viejo → CosmosAccessConditionFailedError (412); ``read_item``
    inexistente → CosmosResourceNotFoundError (404); cada escritura cambia
    ``_etag``. La partición es la ruta ``partition_path`` (por defecto
    ``workflow_name``, la del contenedor de checkpoints)."""

    def __init__(self, partition_path: str = "workflow_name") -> None:
        self.docs: dict = {}
        self.partition_path = partition_path
        self._version = 0

    def _stamp(self, body):
        from azure.cosmos import exceptions

        # El servicio real rechaza items de más de 2 MB (413); sin esto el
        # doble aceptaría lo que producción rechaza (RequestEntityTooLarge).
        if len(json.dumps(body).encode("utf-8")) > 2 * 1024 * 1024:
            raise exceptions.CosmosHttpResponseError(
                status_code=413, message='{"Errors":["Request size is too large"]}'
            )
        self._version += 1
        doc = dict(body)
        doc["_etag"] = f'"{self._version}"'
        doc["_ts"] = self._version
        self.docs[doc["id"]] = doc
        return dict(doc)

    def _check_etag(self, item_id, etag, match_condition):
        from azure.core import MatchConditions
        from azure.cosmos import exceptions

        if match_condition is None or etag is None:
            return
        current = self.docs.get(item_id, {}).get("_etag")
        if match_condition == MatchConditions.IfNotModified and current != etag:
            raise exceptions.CosmosAccessConditionFailedError(
                status_code=412, message="etag mismatch"
            )

    async def create_item(self, body, **kwargs):
        from azure.cosmos import exceptions

        if body["id"] in self.docs:
            raise exceptions.CosmosResourceExistsError(
                status_code=409, message="conflict"
            )
        return self._stamp(body)

    async def upsert_item(self, body, *, etag=None, match_condition=None, **kwargs):
        self._check_etag(body["id"], etag, match_condition)
        return self._stamp(body)

    async def replace_item(
        self, item, body, *, etag=None, match_condition=None, **kwargs
    ):
        from azure.cosmos import exceptions

        item_id = item if isinstance(item, str) else item["id"]
        if item_id not in self.docs:
            raise exceptions.CosmosResourceNotFoundError(
                status_code=404, message="not found"
            )
        self._check_etag(item_id, etag, match_condition)
        return self._stamp(body)

    async def read_item(self, item, partition_key, **kwargs):
        from azure.cosmos import exceptions

        doc = self.docs.get(item)
        if doc is None or doc.get(self.partition_path) != partition_key:
            raise exceptions.CosmosResourceNotFoundError(
                status_code=404, message="not found"
            )
        return dict(doc)

    async def delete_item(self, item, partition_key, **kwargs):
        assert self.docs[item][self.partition_path] == partition_key
        del self.docs[item]

    def query_items(self, query, parameters=None, partition_key=None, **kwargs):
        params = {p["name"]: p["value"] for p in parameters or []}

        async def _gen():
            for doc in list(self.docs.values()):
                if (
                    partition_key is not None
                    and doc.get(self.partition_path) != partition_key
                ):
                    continue
                if any(
                    k != "@" + self.partition_path and doc.get(k[1:]) != v
                    for k, v in params.items()
                    if k.startswith("@")
                ):
                    continue
                yield dict(doc)

        return _gen()


@pytest.fixture
def fake_cosmos_container():
    return FakeCosmosContainer()


@pytest.fixture
def fake_cosmos_container_factory():
    """Contenedores falsos con otra ruta de partición (eventos, lease)."""
    return FakeCosmosContainer
