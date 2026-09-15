"""
Pytest configuration for backend tests.

This module handles proper test isolation and minimal external module mocking.
"""

import atexit
import json
import os
import sys
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
            return [e for r in self.received if path_part in r["path"] for e in r["envelopes"]]

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
        'AZURE_AI_SUBSCRIPTION_ID': 'test-subscription',
        'AZURE_AI_RESOURCE_GROUP': 'test-rg',
        'AZURE_AI_PROJECT_NAME': 'test-project',
        'AZURE_AI_AGENT_ENDPOINT': 'https://test.agent.endpoint.com',
        'AZURE_OPENAI_ENDPOINT': 'https://test.openai.azure.com/',
        'AZURE_OPENAI_API_KEY': 'test-key',
        'AZURE_OPENAI_API_VERSION': '2023-05-15',
        'AZURE_OPENAI_DEPLOYMENT_NAME': 'test-deployment',
        'PROJECT_CONNECTION_STRING': 'test-connection',
        'AZURE_COSMOS_ENDPOINT': 'https://test.cosmos.azure.com',
        'AZURE_COSMOS_KEY': 'test-key',
        'AZURE_COSMOS_DATABASE_NAME': 'test-db',
        'AZURE_COSMOS_CONTAINER_NAME': 'test-container',
        'FRONTEND_SITE_NAME': 'http://localhost:3000',
        'AZURE_STORAGE_BLOB_URL': 'https://test.blob.core.windows.net',
        'APP_ENV': 'dev',
        'AZURE_OPENAI_RAI_DEPLOYMENT_NAME': 'test-rai-deployment',
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
    os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = TELEMETRY_STUB.connection_string
    # Los otros dos canales remotos del exportador, apagados con sus switches:
    # statsbeat (westus-0.in.applicationinsights.azure.com) y el control plane
    # OneSettings (settings.sdk.monitor.azure.com).
    os.environ["APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL"] = "true"
    os.environ["APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED"] = "true"


# Set up environment and minimal mocks before any test imports
_setup_environment_variables()


@pytest.fixture(autouse=True)
def _no_interactive_credential_under_pytest(monkeypatch):
    """INV-NO-INTERACTIVE-CREDENTIAL-UNDER-PYTEST (INC-2026-002): bajo pytest,
    _dev_acquire_user_token falla con nombre en vez de abrir DeviceCodeCredential
    y esperar a un humano. El test debe traer el token como lo inyecta EasyAuth
    (x-ms-token-aad-access-token). Se parchea en ambos nombres de módulo con los
    que se importa el mismo archivo (router: auth.auth_utils; tests: auth.auth_utils)."""
    import importlib

    importlib.import_module("auth.auth_utils")  # el módulo real del router, cargado

    def _fail(*_args, **_kwargs):
        raise AssertionError(
            "INV-NO-INTERACTIVE-CREDENTIAL-UNDER-PYTEST: la petición del test no "
            "trae access token y la cadena de auth intentó DeviceCodeCredential. "
            "Añade x-ms-token-aad-access-token (o Authorization) al scope."
        )

    for name in ("auth.auth_utils", "auth.auth_utils"):
        if name in sys.modules:
            monkeypatch.setattr(sys.modules[name], "_dev_acquire_user_token", _fail)

# Pre-import the middleware chain (self_heal -> tool_errors -> event_utils)
# while the CLEAN stub above is in sys.modules. Several test modules stomp
# sys.modules['agent_framework'] with their own partial Mocks at import time;
# if this chain first loads under one of those (suite order), subclassing
# FunctionMiddleware hits a Mock instance and the import dies (surfacing as
# "module ... has no attribute 'foundry_agent'"). Importing it here caches the
# real modules so later stomps can't re-execute them.
try:
    import v4.magentic_agents.common.self_heal_middleware  # noqa: F401
except Exception:
    # Never fail collection over an optional warm-up import.
    pass
try:
    import azure.ai.voicelive  # noqa: F401
    import azure.ai.voicelive.aio  # noqa: F401
    import azure.ai.voicelive.models  # noqa: F401
    import v4.api.audio_router  # noqa: F401
except Exception:
    pass


@pytest.fixture
def mock_azure_services():
    """Fixture to provide common Azure service mocks."""
    return {
        'cosmos_client': Mock(),
        'openai_client': Mock(),
        'ai_project_client': Mock(),
        'credential': Mock(),
    }
