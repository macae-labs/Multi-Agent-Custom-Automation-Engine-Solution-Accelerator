"""El carril chat es UN agente con todas las capacidades, en cada turno.

Sin router ni composer: ``invoke`` ejecuta siempre ``_execute_responses`` con
el/los Toolbox de Foundry, MacaeMcpServer (workspace + registry, con la
identidad del usuario), el code interpreter, web_search e image_generation
adjuntos a la vez; el modelo decide dentro de su propio bucle. Lo que vuelve
son los shims que el manejador SSE ya renderiza. Medido el 2026-09-23: el
mismo pedido dio cuatro conductas distintas cuando un router/composer elegía
por adelantado qué herramienta llevaba el turno.
"""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from v4.api import router


class _Stream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def _fake_openai(events):
    class _Fake:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            self.create_kwargs = None
            _Fake.instances.append(self)

            async def create(**kw):
                self.create_kwargs = kw
                return _Stream(events)

            self.responses = SimpleNamespace(create=create)

        async def close(self):
            self.closed = True

    return _Fake


def _client(token=None, workspace="my-repo"):
    c = router._RouterChatClient.__new__(router._RouterChatClient)
    c.agent_name = "Assistant"
    c._openai_base_url = "https://acct/openai"
    c._api_version = "2025-03-01-preview"
    c._model = "o4-mini"
    c._user_id = "u1"
    c._user_access_token = token
    c._workspace_id = workspace
    c._user_cred = None
    c._memory_store = None
    c._toolboxes = [
        ("Toolbox", "https://acct/api/projects/p/toolboxes/Toolbox/mcp?api-version=v1")
    ]
    c._macae_mcp_url = "https://ca-mcp.example/mcp"
    c._image_deployment = "gpt-image-2"
    c._bearer = AsyncMock(return_value="tok")
    return c


async def _run(client, prompt="hola", history=None):
    return [u async for u in client.invoke(prompt, history=history or [])]


def _delta(text):
    return SimpleNamespace(type="response.output_text.delta", delta=text)


@pytest.mark.asyncio
async def test_every_chat_turn_attaches_every_capability_at_once():
    fake = _fake_openai([_delta("hola")])
    client = _client(token="user-jwt")
    with patch("openai.AsyncOpenAI", fake):
        await _run(client, "¿qué hay?", history=[{"role": "user", "content": "antes"}])

    sdk = fake.instances[-1]
    assert sdk.kwargs["default_headers"] == {
        "x-ms-oai-image-generation-deployment": "gpt-image-2"
    }
    call = sdk.create_kwargs
    assert (
        call["model"] == "o4-mini" and call["stream"] is True and call["store"] is False
    )
    assert "tool_choice" not in call
    assert call["input"] == [
        {"role": "user", "content": "antes"},
        {"role": "user", "content": "¿qué hay?"},
    ]
    kinds = [t.get("server_label") or t["type"] for t in call["tools"]]
    assert kinds == [
        "Toolbox",
        "MacaeMcpServer",
        "code_interpreter",
        "web_search",
        "image_generation",
    ]
    toolbox = call["tools"][0]
    assert toolbox["server_url"].endswith("/toolboxes/Toolbox/mcp?api-version=v1")
    assert toolbox["headers"] == {
        "Authorization": "Bearer tok",
        "Foundry-Features": "Toolboxes=V1Preview",
    }
    macae = call["tools"][1]
    assert macae["headers"] == {
        "x-ms-client-principal-id": "u1",
        "Authorization": "Bearer user-jwt",
    }
    assert macae["require_approval"] == "never"


@pytest.mark.asyncio
async def test_the_instructions_carry_identity_workspace_and_toolbox_contract(
    monkeypatch,
):
    from common.config.app_config import config

    monkeypatch.setattr(
        config, "AZURE_AI_PROJECT_ENDPOINT", "https://acct/api/projects/p"
    )
    fake = _fake_openai([])
    with patch("openai.AsyncOpenAI", fake):
        await _run(_client())
    instructions = fake.instances[-1].create_kwargs["instructions"]
    assert "user_id='u1'" in instructions
    assert "workspace 'my-repo'" in instructions and "workspace_exec" in instructions
    assert "EXACTLY the identifier tool_search returned" in instructions
    assert "projectEndpoint='https://acct/api/projects/p'" in instructions

    fake = _fake_openai([])
    with patch("openai.AsyncOpenAI", fake):
        await _run(_client(workspace=None))
    assert "WORKSPACE:" not in fake.instances[-1].create_kwargs["instructions"]


@pytest.mark.asyncio
async def test_no_user_token_means_no_bearer_toward_macae():
    fake = _fake_openai([])
    with patch("openai.AsyncOpenAI", fake):
        await _run(_client(token=None))
    macae = fake.instances[-1].create_kwargs["tools"][1]
    assert macae["headers"] == {"x-ms-client-principal-id": "u1"}


@pytest.mark.asyncio
async def test_text_streams_as_content_shims_the_handler_renders():
    fake = _fake_openai([_delta("Hola, "), _delta("¿qué necesitás?")])
    with patch("openai.AsyncOpenAI", fake):
        updates = await _run(_client())
    texts = [c.text for u in updates for c in u.contents if c.type == "text"]
    assert "".join(texts) == "Hola, ¿qué necesitás?"
    assert fake.instances[-1].closed


@pytest.mark.asyncio
async def test_a_generated_image_is_persisted_and_surfaced_as_a_generated_file(
    monkeypatch,
):
    saved: dict = {}

    class _Store:
        async def save(self, file_id, filename, data):
            saved.update(file_id=file_id, filename=filename, size=len(data))
            return True

    monkeypatch.setattr(
        router.GeneratedFileStore, "get_instance", classmethod(lambda cls: _Store())
    )
    png = b"\x89PNG" + b"\x00" * 32
    done = SimpleNamespace(
        type="response.output_item.done",
        item=SimpleNamespace(
            type="image_generation_call",
            id="ig_1",
            result=base64.b64encode(png).decode(),
        ),
    )
    fake = _fake_openai([done, _delta("Listo.")])
    with patch("openai.AsyncOpenAI", fake):
        updates = await _run(_client(), "generá un faro")

    files = [c for u in updates for c in u.contents if c.type == "hosted_file"]
    assert len(files) == 1 and files[0].file_id.startswith("img_")
    assert saved == {
        "file_id": files[0].file_id,
        "filename": files[0].file_id + ".png",
        "size": len(png),
    }
    assert files[0].additional_properties == {"filename": files[0].name}
