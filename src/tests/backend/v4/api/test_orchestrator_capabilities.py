"""Las capacidades propias del orquestador, junto a ``compose``.

Lo que el orquestador puede resolver él mismo lo resuelve en la MISMA llamada:
una imagen, una búsqueda viva, un cálculo, los toolboxes del proyecto. Componer
un especialista para eso significa publicar una versión de agente en Foundry y
armar un workflow para terminar llamando la misma tool. ``compose`` queda para
el trabajo que sí necesita especialistas.

La imagen es el caso que no tiene dónde vivir: la tool contesta con los bytes EN
LÍNEA (base64) y nada más — ni archivo de Foundry ni contenedor. Se guardan en el
MISMO ``GeneratedFileStore`` que usa cualquier otro archivo generado y se anuncian
como contenido ``hosted_file``, que es el tipo del framework: de ahí en adelante
manda la rama que ya existe en el manejador SSE (evento ``generated_file``, la
imagen dentro del mensaje, descriptor persistido). Sin carril nuevo y sin shims.
"""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from v4.api import router

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _Stream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def _fake_openai(*replies):
    """Un turno hace varias llamadas (ejecución, síntesis, veredicto). Las
    respuestas se consumen en orden y la última se repite si el turno pide
    más. ``calls`` guarda todas; ``create_kwargs`` la PRIMERA (la oferta)."""
    queue = list(replies)

    class _Fake:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            self.create_kwargs = None
            self.calls: list = []
            _Fake.instances.append(self)

            async def create(**kw):
                self.calls.append(kw)
                if self.create_kwargs is None:
                    self.create_kwargs = kw
                return queue.pop(0) if len(queue) > 1 else queue[0]

            self.responses = SimpleNamespace(create=create)

        async def close(self):
            self.closed = True

    return _Fake


def _client(toolboxes=None, image_deployment="gpt-image-2"):
    c = router._RouterChatClient.__new__(router._RouterChatClient)
    c.agent_name = "Composer"
    c._openai_base_url = "https://account.invalid/openai"
    c._api_version = "2025-03-01-preview"
    c._model = "gpt-5.4-mini"
    c._image_deployment = image_deployment
    c._toolboxes = list(toolboxes or [])
    c._memory_store = None
    c.composition = None
    c._user_id = "u1"
    c._user_access_token = None
    c._workspace_id = None
    # Tools del workspace: sin workspace montado no se conectan (None).
    c._ws_tool = None
    c._ws_tool_lock = asyncio.Lock()
    c._ws_specs = None
    c._ws_names = set()
    c._ws_identity = {}
    c._user_cred = None
    c._bearer = AsyncMock(return_value="tok")
    return c


def _image_item(result, output_format=None):
    return SimpleNamespace(
        type="image_generation_call", result=result, output_format=output_format
    )


def _text(chunk):
    return SimpleNamespace(type="response.output_text.delta", delta=chunk)


def _done(item):
    return SimpleNamespace(type="response.output_item.done", item=item)


async def _collect(client, prompt="hola"):
    return [u async for u in client.invoke(prompt, history=[])]


# ── lo que el orquestador ofrece en su propia llamada ────────────────────────


@pytest.mark.asyncio
async def test_the_orchestrator_offers_its_own_capabilities_and_never_compose():
    fake = _fake_openai(_Stream([]))
    with patch("openai.AsyncOpenAI", fake):
        await _collect(_client())

    tools = fake.instances[-1].create_kwargs["tools"]
    # Sin workspace montado, la web sí se ofrece. ``compose`` nunca: el turno
    # no escala por su cuenta (medido: Plan con aprobación para una pregunta).
    assert [t["type"] for t in tools] == [
        "image_generation",
        "web_search",
        "code_interpreter",
    ]
    assert tools[2]["container"] == {"type": "auto"}
    assert not any(t.get("name") == "compose" for t in tools)


# ── el libro de evidencia es del TURNO ───────────────────────────────────────


def _verdict(goal_met, reason=""):
    return SimpleNamespace(
        output_text=json.dumps({"goal_met": goal_met, "reason": reason})
    )


@pytest.mark.asyncio
async def test_the_evaluator_sees_the_tools_of_earlier_passes():
    # Medido: 20 tools en 16 pasadas y el evaluador vio sólo la última (la
    # síntesis, 0 tools) → "sin respaldo de herramientas" sobre un informe
    # con 20 respaldos → reentrada inútil → "no pude cumplirlo" pegado a un
    # informe cumplido. La evidencia se acumula por turno.
    tool_call = SimpleNamespace(
        type="function_call", name="workspace_exec", call_id="c1", arguments="{}"
    )
    fake = _fake_openai(
        _Stream([_done(tool_call)]),  # pasada 1: sólo ejecuta
        _Stream([_text("dictamen")]),  # pasada 2: sólo redacta
        _verdict(True),  # el evaluador acepta
    )
    c = _client()
    c._ws_names = {"workspace_exec"}
    c._call_workspace_tool = AsyncMock(return_value='{"status": "success"}')
    with patch("openai.AsyncOpenAI", fake):
        updates = await _collect(c)

    assert "".join((x.text or "") for u in updates for x in u.contents) == "dictamen"
    calls = fake.instances[-1].calls
    assert len(calls) == 3, "ejecución, síntesis, veredicto"
    # La salida de la tool volvió por el protocolo, con su call_id…
    assert {
        "type": "function_call_output",
        "call_id": "c1",
        "output": '{"status": "success"}',
    } in [i for i in calls[1]["input"] if isinstance(i, dict)]
    # …y el evaluador juzgó la síntesis CON la evidencia de la pasada anterior.
    assert "workspace_exec" in calls[2]["input"][0]["content"]


@pytest.mark.asyncio
async def test_workspace_tool_discovery_strips_identity_and_caches_metadata():
    class _Fn:
        name = "workspace_read_file"
        description = "read a file"

        def parameters(self):
            return {
                "properties": {
                    "user_id": {"type": "string"},
                    "workspace_id": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["user_id", "workspace_id", "path"],
            }

    class _Tool:
        enters = 0

        def __init__(self, **_kwargs):
            self.functions = [_Fn()]

        async def __aenter__(self):
            type(self).enters += 1
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    c = _client()
    c._workspace_id = "my-repo"
    cfg = SimpleNamespace(name="ws", description="workspace", url="https://mcp.invalid")
    with (
        patch("v4.api.router.MCPStreamableHTTPTool", _Tool),
        patch("v4.magentic_agents.models.agent_models.MCPConfig.from_env", return_value=cfg),
    ):
        first = await c._workspace_tools()
        second = await c._workspace_tools()

    assert _Tool.enters == 1
    assert first == second
    assert first == [
        {
            "type": "function",
            "name": "workspace_read_file",
            "description": "read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        }
    ]
    assert c._ws_identity == {"workspace_read_file": ("user_id", "workspace_id")}
    assert c._ws_names == {"workspace_read_file"}


@pytest.mark.asyncio
async def test_workspace_tool_results_are_serialized_when_not_text_chunks():
    class _Tool:
        async def call_tool(self, name, **kwargs):
            assert name == "workspace_read_file"
            assert kwargs == {"path": "README.md", "user_id": "u1", "workspace_id": "repo"}
            return {"status": "ok", "path": "README.md"}

    c = _client()
    c._workspace_id = "repo"
    c._ws_tool = _Tool()
    c._ws_identity = {"workspace_read_file": ("user_id", "workspace_id")}

    out = await c._call_workspace_tool("workspace_read_file", '{"path":"README.md"}')

    assert json.loads(out) == {"status": "ok", "path": "README.md"}


@pytest.mark.asyncio
async def test_a_capability_only_success_emits_a_brief_final_text():
    class _Store:
        async def save(self, file_id, filename, data):
            return True

    fake = _fake_openai(
        _Stream([_done(_image_item(base64.b64encode(PNG).decode()))]),
        _verdict(True),
    )
    with (
        patch("openai.AsyncOpenAI", fake),
        patch(
            "v4.common.services.generated_file_store.GeneratedFileStore.get_instance",
            return_value=_Store(),
        ),
    ):
        updates = await _collect(_client())

    assert "".join((x.text or "") for u in updates for x in u.contents) == "Listo."


@pytest.mark.asyncio
async def test_every_declared_toolbox_is_attached_with_the_preview_gate():
    fake = _fake_openai(_Stream([]))
    boxes = [("Toolbox", "https://p.invalid/toolboxes/Toolbox/mcp?api-version=v1")]
    with patch("openai.AsyncOpenAI", fake):
        await _collect(_client(toolboxes=boxes))

    (mcp,) = [
        t for t in fake.instances[-1].create_kwargs["tools"] if t["type"] == "mcp"
    ]
    assert mcp["server_label"] == "Toolbox"
    assert mcp["server_url"] == boxes[0][1]
    assert mcp["require_approval"] == "never"
    # El endpoint está detrás de una compuerta de preview: sin la cabecera
    # contesta 401 por válido que sea el token.
    assert mcp["headers"]["Foundry-Features"] == "Toolboxes=V1Preview"
    assert mcp["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_without_declared_toolboxes_none_is_attached():
    fake = _fake_openai(_Stream([]))
    with patch("openai.AsyncOpenAI", fake):
        await _collect(_client(toolboxes=[]))
    assert not [
        t for t in fake.instances[-1].create_kwargs["tools"] if t["type"] == "mcp"
    ]


@pytest.mark.asyncio
async def test_azure_reads_the_image_deployment_from_a_request_header():
    # Sin esta cabecera la llamada ENTERA contesta 400, incluso en turnos que
    # nunca iban a generar una imagen.
    fake = _fake_openai(_Stream([]))
    with patch("openai.AsyncOpenAI", fake):
        await _collect(_client(image_deployment="gpt-image-2"))
    headers = fake.instances[-1].kwargs["default_headers"]
    assert headers["x-ms-oai-image-generation-deployment"] == "gpt-image-2"


# ── la imagen entra por el canal que ya existe ───────────────────────────────


@pytest.mark.asyncio
async def test_a_generated_image_is_stored_and_announced_as_a_hosted_file():
    saved: list = []

    class _Store:
        async def save(self, file_id, filename, data):
            saved.append((file_id, filename, data))
            return True

    fake = _fake_openai(_Stream([_done(_image_item(base64.b64encode(PNG).decode()))]))
    with (
        patch("openai.AsyncOpenAI", fake),
        patch(
            "v4.common.services.generated_file_store.GeneratedFileStore.get_instance",
            return_value=_Store(),
        ),
    ):
        updates = await _collect(_client())

    # Los bytes quedaron guardados ANTES de anunciarse el enlace: no hay carrera
    # que pueda dejar la descarga en 404.
    assert len(saved) == 1
    file_id, filename, data = saved[0]
    assert data == PNG
    assert filename == f"{file_id}.png"

    (content,) = [c for u in updates for c in u.contents]
    assert content.type == "hosted_file"
    assert content.file_id == file_id
    # La rama del manejador SSE lee el nombre de additional_properties.
    assert content.additional_properties["filename"] == filename
    assert content.media_type == "image/png"


@pytest.mark.asyncio
async def test_the_format_the_tool_reports_is_the_one_stored():
    saved: list = []

    class _Store:
        async def save(self, file_id, filename, data):
            saved.append(filename)
            return True

    item = _image_item(base64.b64encode(PNG).decode(), output_format="jpeg")
    fake = _fake_openai(_Stream([_done(item)]))
    with (
        patch("openai.AsyncOpenAI", fake),
        patch(
            "v4.common.services.generated_file_store.GeneratedFileStore.get_instance",
            return_value=_Store(),
        ),
    ):
        updates = await _collect(_client())

    assert saved[0].endswith(".jpeg")
    (content,) = [c for u in updates for c in u.contents]
    assert content.media_type == "image/jpeg"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["", None, "!!!no-es-base64!!!"])
async def test_an_image_call_without_usable_bytes_stores_nothing_and_does_not_raise(
    payload,
):
    class _Store:
        async def save(self, *a, **kw):  # pragma: no cover - no debe llamarse
            raise AssertionError("no hay bytes que guardar")

    fake = _fake_openai(_Stream([_done(_image_item(payload))]))
    with (
        patch("openai.AsyncOpenAI", fake),
        patch(
            "v4.common.services.generated_file_store.GeneratedFileStore.get_instance",
            return_value=_Store(),
        ),
    ):
        assert await _collect(_client()) == []


@pytest.mark.asyncio
async def test_composing_still_works_with_the_capabilities_attached():
    # Agregar capacidades no desplaza a ``compose``: sigue siendo la vía del
    # trabajo que necesita especialistas.
    call = SimpleNamespace(
        type="function_call",
        name="compose",
        arguments=json.dumps(
            {
                "pattern": "magentic",
                "task": "Auditar el repo",
                "participants": [
                    {"name": "RepoAgent", "description": "d", "system_message": "s"}
                ],
            }
        ),
    )
    fake = _fake_openai(_Stream([_done(call)]))
    client = _client()
    with patch("openai.AsyncOpenAI", fake):
        await _collect(client)
    assert client.composition is not None
    pattern, task, participants = client.composition
    assert (pattern, task) == ("magentic", "Auditar el repo")
    assert [p["name"] for p in participants] == ["RepoAgent"]


# ── el modelo del turno ──────────────────────────────────────────────────────


def _build(monkeypatch, *, model=None, toolboxes="", configured="o4-mini"):
    from common.config.app_config import config

    monkeypatch.setattr(
        config, "AZURE_AI_PROJECT_ENDPOINT", "https://acc.invalid/api/projects/p", False
    )
    monkeypatch.setattr(config, "CHAT_ORCHESTRATOR_MODEL", configured, False)
    monkeypatch.setattr(config, "CHAT_TOOLBOXES", toolboxes, False)
    monkeypatch.setattr(config, "_get_optional", lambda name, default=None: default)
    return router._RouterChatClient("Composer", model=model)


def test_the_caller_names_the_engine_for_this_turn(monkeypatch):
    # Las fortalezas medidas difieren por tipo de trabajo; el runtime puede
    # nombrar el motor del turno en vez de dejarlo clavado en configuración.
    assert _build(monkeypatch, model="gpt-5.1-codex-mini")._model == (
        "gpt-5.1-codex-mini"
    )


def test_without_a_named_engine_the_configured_one_is_used(monkeypatch):
    assert _build(monkeypatch, configured="gpt-5.4-mini")._model == "gpt-5.4-mini"


def test_a_declared_toolbox_version_pins_the_url(monkeypatch):
    client = _build(monkeypatch, toolboxes="Toolbox:3, Otra , ,")
    assert client._toolboxes == [
        (
            "Toolbox",
            "https://acc.invalid/api/projects/p/toolboxes/Toolbox/versions/3/mcp?api-version=v1",
        ),
        (
            "Otra",
            "https://acc.invalid/api/projects/p/toolboxes/Otra/mcp?api-version=v1",
        ),
    ]


def test_no_declared_toolboxes_means_no_attachments(monkeypatch):
    assert _build(monkeypatch, toolboxes="")._toolboxes == []
