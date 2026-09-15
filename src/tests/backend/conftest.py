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
from types import ModuleType
from typing import Any, cast
from unittest.mock import Mock, MagicMock

import pytest


def _stub_module(name: str) -> Any:
    """A ModuleType typed as Any: these stubs exist to be stuffed with fake
    attributes (modules accept arbitrary attributes at runtime). Typing them
    Any confines the relaxation to the stub objects themselves — attribute
    errors on everything else in this file stay fully checked."""
    return cast(Any, ModuleType(name))


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


def _setup_agent_framework_mock():
    """
    Set up mock for agent_framework which is not a pip-installable package.
    This framework is used for Azure AI Agents and needs proper mocking.
    Uses ModuleType with real stub classes for names used in type annotations
    or as base classes, and MagicMock for everything else.
    """
    if 'agent_framework' not in sys.modules:
        # Top-level: agent_framework
        mock_af = _stub_module('agent_framework')

        # Names used as base classes or in Union type hints MUST be real classes
        # to avoid SyntaxError from typing module's forward reference evaluation.
        _class_names = [
            'Agent', 'AgentResponse', 'AgentResponseUpdate', 'AgentRunUpdateEvent',
            'AgentSession', 'AgentThread', 'BaseAgent', 'ChatAgent', 'ChatMessage',
            'ChatOptions', 'Content', 'ExecutorCompletedEvent',
            'FunctionInvocationContext', 'FunctionMiddleware',
            'GroupChatRequestSentEvent', 'GroupChatResponseReceivedEvent',
            'HostedCodeInterpreterTool', 'HostedMCPTool',
            'InMemoryCheckpointStorage', 'MCPStreamableHTTPTool',
            'MagenticBuilder', 'MagenticOrchestratorEvent',
            'MagenticProgressLedger', 'Message', 'Role', 'UsageDetails',
            'WorkflowOutputEvent',
        ]
        for name in _class_names:
            setattr(mock_af, name, type(name, (), {
                '__init__': lambda self, *args, **kwargs: None,
            }))

        # Sub-module: agent_framework._types
        mock_af_types = _stub_module('agent_framework._types')
        mock_af_types.ResponseStream = type('ResponseStream', (), {})
        mock_af._types = mock_af_types
        sys.modules['agent_framework._types'] = mock_af_types

        # Sub-module: agent_framework.azure
        mock_af_azure = _stub_module('agent_framework.azure')
        mock_af_azure.AzureOpenAIChatClient = type('AzureOpenAIChatClient', (), {})
        mock_af_azure.AzureOpenAIResponsesClient = type('AzureOpenAIResponsesClient', (), {})
        mock_af.azure = mock_af_azure

        # Sub-module: agent_framework._workflows._magentic
        mock_af_workflows = _stub_module('agent_framework._workflows')
        mock_af_magentic = _stub_module('agent_framework._workflows._magentic')
        for name in [
            'MagenticContext', 'StandardMagenticManager',
        ]:
            setattr(mock_af_magentic, name, type(name, (), {}))
        for name in [
            'ORCHESTRATOR_FINAL_ANSWER_PROMPT',
            'ORCHESTRATOR_PROGRESS_LEDGER_PROMPT',
            'ORCHESTRATOR_TASK_LEDGER_PLAN_PROMPT',
            'ORCHESTRATOR_TASK_LEDGER_PLAN_UPDATE_PROMPT',
        ]:
            setattr(mock_af_magentic, name, "mock_prompt_string")
        mock_af_workflows._magentic = mock_af_magentic
        mock_af._workflows = mock_af_workflows

        sys.modules['agent_framework'] = mock_af
        sys.modules['agent_framework.azure'] = mock_af_azure
        sys.modules['agent_framework._workflows'] = mock_af_workflows
        sys.modules['agent_framework._workflows._magentic'] = mock_af_magentic

    if 'agent_framework_orchestrations' not in sys.modules:
        mock_af_orch = _stub_module('agent_framework_orchestrations')
        mock_af_orch.MagenticBuilder = type('MagenticBuilder', (), {
            '__init__': lambda self, *args, **kwargs: None,
            'build': lambda self: Mock(),
        })
        sys.modules['agent_framework_orchestrations'] = mock_af_orch

        mock_af_orch_base = _stub_module('agent_framework_orchestrations._base_group_chat_orchestrator')
        for name in ['GroupChatRequestSentEvent', 'GroupChatResponseReceivedEvent']:
            setattr(mock_af_orch_base, name, type(name, (), {}))
        sys.modules['agent_framework_orchestrations._base_group_chat_orchestrator'] = mock_af_orch_base

        mock_af_orch_mag = _stub_module('agent_framework_orchestrations._magentic')
        for name in ['MagenticContext', 'MagenticProgressLedger']:
            setattr(mock_af_orch_mag, name, type(name, (), {}))
        # StandardMagenticManager needs a proper __init__ that accepts args/kwargs
        # because HumanApprovalMagenticManager calls super().__init__(agent, *args, **kwargs)
        setattr(mock_af_orch_mag, 'StandardMagenticManager',
                type('StandardMagenticManager', (), {
                    '__init__': lambda self, *args, **kwargs: None
                }))
        for name in [
            'ORCHESTRATOR_FINAL_ANSWER_PROMPT',
            'ORCHESTRATOR_PROGRESS_LEDGER_PROMPT',
            'ORCHESTRATOR_TASK_LEDGER_PLAN_PROMPT',
            'ORCHESTRATOR_TASK_LEDGER_PLAN_UPDATE_PROMPT',
        ]:
            setattr(mock_af_orch_mag, name, 'mock_prompt_string')
        sys.modules['agent_framework_orchestrations._magentic'] = mock_af_orch_mag

    if 'agent_framework_azure_ai' not in sys.modules:
        mock_af_ai = _stub_module('agent_framework_azure_ai')
        mock_af_ai.AzureAIClient = type('AzureAIClient', (), {})
        mock_af_ai.AzureAIProjectAgentOptions = type('AzureAIProjectAgentOptions', (dict,), {})
        sys.modules['agent_framework_azure_ai'] = mock_af_ai

    if 'agent_framework_openai' not in sys.modules:
        mock_af_openai = _stub_module('agent_framework_openai')
        mock_af_openai.OpenAIChatOptions = type('OpenAIChatOptions', (dict,), {})
        sys.modules['agent_framework_openai'] = mock_af_openai


def _setup_azure_monitor_mock():
    """Azure Monitor NO se neutraliza: azure-monitor-opentelemetry está en
    uv.lock y configure_azure_monitor corre de verdad contra TelemetryStub
    (ver _setup_environment_variables). Un no-op aquí convertía la telemetría
    en adorno y dejaba sin cubrir la inicialización real."""
    return None


def _patch_azure_ai_projects_models():
    """
    Patch azure.ai.projects.models to add names that may be missing
    in older SDK versions (e.g. PromptAgentDefinition).
    """
    try:
        import azure.ai.projects.models as models_mod
        missing_names = [
            'PromptAgentDefinition',
            'AzureAISearchAgentTool',
            'AzureAISearchToolResource',
            'AISearchIndexResource',
        ]
        for name in missing_names:
            if not hasattr(models_mod, name):
                setattr(models_mod, name, MagicMock())
    except ImportError:
        # azure-ai-projects not installed at all — create full mock
        sys.modules['azure.ai.projects'] = MagicMock()
        sys.modules['azure.ai.projects.models'] = MagicMock()


# Set up environment and minimal mocks before any test imports
_setup_environment_variables()


@pytest.fixture(autouse=True)
def _no_interactive_credential_under_pytest(monkeypatch):
    """INV-NO-INTERACTIVE-CREDENTIAL-UNDER-PYTEST (INC-2026-002).

    Con APP_ENV=dev, get_authenticated_user_details llama
    _dev_acquire_user_token cuando la petición no trae access token, y sin
    MACAE_DEV_OBO_TOKEN eso abre DeviceCodeCredential: un login interactivo que
    espera a un humano. En CI quedó 33 min colgado. Aquí se convierte en un
    fallo inmediato con nombre: el test debe llevar el token como lo inyecta
    EasyAuth (x-ms-token-aad-access-token)."""
    import importlib
    import types
    from pathlib import Path

    def _fail(*_args, **_kwargs):
        raise AssertionError(
            "INV-NO-INTERACTIVE-CREDENTIAL-UNDER-PYTEST: la petición del test no "
            "trae access token y la cadena de auth intentó DeviceCodeCredential. "
            "Añade x-ms-token-aad-access-token (o Authorization) al scope."
        )

    backend_auth_pkg = (Path(__file__).resolve().parents[2] / "backend" / "auth" / "__init__.py")

    def _is_backend_auth_pkg(mod) -> bool:
        # Identidad del paquete: el `auth` del backend es el que vive en
        # src/backend/auth. En el lote completo `sys.modules['auth']` puede ser
        # OTRA cosa: el paquete de tests src/tests/backend/auth (pytest en modo
        # importlib lo registra con ese nombre porque src/tests/backend no es
        # paquete) o un Mock dejado por test_router.py. En ambos casos no hay
        # módulo real que proteger y se omite POR IDENTIDAD, sin except
        # genérico: un ImportError real del backend se propaga y se ve.
        f = getattr(mod, "__file__", None)
        return bool(f) and Path(f).resolve() == backend_auth_pkg

    targets = []
    auth_pkg = sys.modules.get("auth")
    if auth_pkg is None or _is_backend_auth_pkg(auth_pkg):
        targets.append(sys.modules.get("auth.auth_utils") or importlib.import_module("auth.auth_utils"))
    # Tests que importan por paquete (backend.auth.auth_utils): otro objeto
    # módulo; se parchea si ya está cargado.
    mod2 = sys.modules.get("backend.auth.auth_utils")
    if isinstance(mod2, types.ModuleType):
        targets.append(mod2)
    for mod in targets:
        if isinstance(getattr(mod, "_dev_acquire_user_token", None), types.FunctionType):
            monkeypatch.setattr(mod, "_dev_acquire_user_token", _fail)
_setup_agent_framework_mock()
_setup_azure_monitor_mock()

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
_patch_azure_ai_projects_models()


@pytest.fixture
def mock_azure_services():
    """Fixture to provide common Azure service mocks."""
    return {
        'cosmos_client': Mock(),
        'openai_client': Mock(),
        'ai_project_client': Mock(),
        'credential': Mock(),
    }
