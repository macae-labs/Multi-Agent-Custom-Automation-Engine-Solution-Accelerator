"""El front door del carril chat: o4-mini por Responses con la única función
``compose``; el Model Router ya no está en esta capa.

Posición Plan: ``compose`` forzado y ``pattern`` restringido a ``magentic``.
Posición Chat: el modelo responde (texto → ``AgentResponseUpdate``) o compone;
una composición ``magentic`` queda en ``composition`` para que el manejador
cree el Plan, cualquier otro patrón corre dentro del turno (``_run_pattern``)
y sus ``WorkflowEvent`` salen tal cual.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from agent_framework import AgentResponseUpdate, WorkflowEvent
from fastapi import HTTPException

from v4.api import router

ROSTER = [
    {
        "name": "RepoAgent",
        "description": "reads the tree",
        "system_message": "You read repositories.",
        "use_mcp": True,
    }
]


class _Stream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def _fake_openai(reply):
    """Sustituto de AsyncOpenAI: responses.create devuelve `reply` (respuesta
    no stream, o un _Stream). Guarda los kwargs de construcción y de llamada."""

    class _Fake:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            self.create_kwargs = None
            # Un turno hace más de una llamada (compositor + evaluador).
            # calls guarda todas; create_kwargs se queda con la PRIMERA,
            # la del compositor, que es la oferta del turno.
            self.calls: list = []
            _Fake.instances.append(self)

            async def create(**kw):
                self.calls.append(kw)
                if self.create_kwargs is None:
                    self.create_kwargs = kw
                return reply

            self.responses = SimpleNamespace(create=create)

        async def close(self):
            self.closed = True

    return _Fake


def _compose_item(**args):
    return SimpleNamespace(
        type="function_call", name="compose", call_id="c1", arguments=json.dumps(args)
    )


def _client(memory_store=None, toolboxes=None):
    c = router._RouterChatClient.__new__(router._RouterChatClient)
    c.agent_name = "Composer"
    c._openai_base_url = "https://account.invalid/openai"
    c._api_version = "2025-03-01-preview"
    c._model = "o4-mini"
    # Capacidades propias del orquestador: el despliegue de imagen viaja como
    # cabecera y los toolboxes declarados se adjuntan junto a ``compose``.
    c._image_deployment = "gpt-image-2"
    c._toolboxes = list(toolboxes or [])
    c._memory_store = memory_store
    c.composition = None
    c._user_id = "u1"
    c._user_access_token = None
    c._workspace_id = None
    # Tools del workspace: sin workspace montado no se conectan (None).
    c._ws_tool = None
    c._ws_names = set()
    c._ws_identity = {}
    c._user_cred = None
    c._bearer = AsyncMock(return_value="tok")
    return c


async def _collect(client, prompt="hola", **kw):
    return [u async for u in client.invoke(prompt, history=[], **kw)]


@pytest.mark.asyncio
async def test_plan_position_forces_compose_on_o4_mini_responses():
    fake = _fake_openai(
        SimpleNamespace(
            output=[
                _compose_item(
                    pattern="magentic", task="Audit the repo", participants=ROSTER
                )
            ]
        )
    )
    with patch("openai.AsyncOpenAI", fake):
        pattern, task, participants = await _client().compose_plan("audita el repo")

    assert (pattern, task) == ("magentic", "Audit the repo")
    assert [p["name"] for p in participants] == ["RepoAgent"]
    sdk = fake.instances[-1]
    assert sdk.kwargs["base_url"] == "https://account.invalid/openai"
    assert sdk.kwargs["default_query"] == {"api-version": "2025-03-01-preview"}
    assert sdk.closed
    call = sdk.create_kwargs
    assert call["model"] == "o4-mini"
    assert call["tool_choice"] == {"type": "function", "name": "compose"}
    assert call["store"] is False
    assert call["input"] == [{"role": "user", "content": "audita el repo"}]
    (tool,) = call["tools"]
    assert tool["name"] == "compose"
    assert tool["parameters"]["properties"]["pattern"]["enum"] == ["magentic"]
    assert (
        tool["parameters"]["properties"]["participants"] is router._PARTICIPANT_SCHEMA
    )
    assert tool["parameters"]["required"] == ["pattern", "task", "participants"]


@pytest.mark.asyncio
async def test_plan_position_without_a_composition_is_422():
    fake = _fake_openai(
        SimpleNamespace(output=[SimpleNamespace(type="message", content=[])])
    )
    with patch("openai.AsyncOpenAI", fake), pytest.raises(HTTPException) as err:
        await _client().compose_plan("hola")
    assert err.value.status_code == 422


@pytest.mark.asyncio
async def test_malformed_compose_arguments_are_422():
    fake = _fake_openai(
        SimpleNamespace(
            output=[
                SimpleNamespace(type="function_call", name="compose", arguments="{no")
            ]
        )
    )
    with patch("openai.AsyncOpenAI", fake), pytest.raises(HTTPException) as err:
        await _client().compose_plan("audita")
    assert err.value.status_code == 422


@pytest.mark.asyncio
async def test_chat_position_answer_is_a_framework_update():
    fake = _fake_openai(
        _Stream(
            [
                SimpleNamespace(type="response.output_text.delta", delta="Hola, "),
                SimpleNamespace(type="response.output_text.delta", delta="¿qué hay?"),
            ]
        )
    )
    client = _client()
    with patch("openai.AsyncOpenAI", fake):
        updates = await _collect(client)

    assert updates and all(isinstance(u, AgentResponseUpdate) for u in updates)
    assert "".join(c.text for u in updates for c in u.contents) == "Hola, ¿qué hay?"
    assert updates[0].author_name == "Composer"
    assert client.composition is None
    call = fake.instances[-1].create_kwargs
    assert call["tool_choice"] == "auto"
    assert call["stream"] is True and call["store"] is False
    # El turno ofrece las capacidades PROPIAS del orquestador y nunca
    # ``compose``: componer publicaba agentes en Foundry y devolvía el mismo
    # informe tres veces (medido); el Plan se entra desde su carril.
    assert not any(t.get("name") == "compose" for t in call["tools"])
    assert {"image_generation", "web_search", "code_interpreter"} <= {
        t["type"] for t in call["tools"]
    }


@pytest.mark.asyncio
async def test_chat_position_magentic_leaves_the_composition_for_the_handler():
    fake = _fake_openai(
        _Stream(
            [
                SimpleNamespace(
                    type="response.output_item.done",
                    item=_compose_item(
                        pattern="magentic", task="Plan it", participants=ROSTER
                    ),
                )
            ]
        )
    )
    client = _client()
    with patch("openai.AsyncOpenAI", fake):
        updates = await _collect(client, "planifica")

    assert updates == []
    assert client.composition == ("magentic", "Plan it", ROSTER)


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_plan", [True, False])
async def test_the_turn_never_offers_compose_so_it_cannot_create_a_plan(allow_plan):
    # Ni con allow_plan ni sin él: el turno no puede escalar por su cuenta.
    # Medido: escaló una pregunta de lectura a un Plan con compuerta de
    # aprobación, y otra vez a ``sequential`` con tres participantes.
    fake = _fake_openai(_Stream([]))
    with patch("openai.AsyncOpenAI", fake):
        await _collect(_client(), allow_plan=allow_plan)
    tools = fake.instances[-1].create_kwargs["tools"]
    assert not any(t.get("name") == "compose" for t in tools)
    assert not any(
        "pattern" in (t.get("parameters") or {}).get("properties", {}) for t in tools
    )


@pytest.mark.asyncio
async def test_chat_position_other_pattern_runs_in_the_turn_and_yields_workflow_events(
    monkeypatch,
):
    fake = _fake_openai(
        _Stream(
            [
                SimpleNamespace(
                    type="response.output_item.done",
                    item=_compose_item(
                        pattern="concurrent", task="Compare", participants=ROSTER
                    ),
                )
            ]
        )
    )
    client = _client(memory_store=object())
    ran: list = []

    async def _run_pattern(pattern, task, participants, history):
        ran.append((pattern, task, participants, history))
        yield WorkflowEvent("output", data="x", executor_id="RepoAgent")

    monkeypatch.setattr(client, "_run_pattern", _run_pattern)
    with patch("openai.AsyncOpenAI", fake):
        updates = await _collect(client, "compará")

    assert ran == [("concurrent", "Compare", ROSTER, [])]
    assert isinstance(updates[0], WorkflowEvent)
    assert updates[0].executor_id == "RepoAgent"
    assert client.composition == ("concurrent", "Compare", ROSTER)


def test_unknown_pattern_is_refused_by_the_builder():
    from v4.orchestration.orchestration_manager import OrchestrationManager

    with pytest.raises(ValueError):
        OrchestrationManager.build_pattern_workflow("tree", [object()])


@pytest.mark.asyncio
async def test_a_mounted_workspace_is_a_fact_the_composer_receives():
    fake = _fake_openai(_Stream([]))
    without = _client()
    with patch("openai.AsyncOpenAI", fake):
        await _collect(without)
    assert "WORKSPACE" not in fake.instances[-1].create_kwargs["instructions"]

    with_ws = _client()
    with_ws._workspace_id = "my-repo"
    # Con workspace montado el turno conecta al ca-mcp del entorno para
    # declarar sus tools; acá se sustituye (no hay red en el test).
    with_ws._workspace_tools = AsyncMock(return_value=[])
    with patch("openai.AsyncOpenAI", fake):
        await _collect(with_ws)
    call = fake.instances[-1].create_kwargs
    instructions = call["instructions"]
    assert instructions.startswith(router._COMPOSER_INSTRUCTIONS)
    assert "'my-repo'" in instructions
    # Las tools del workspace son SUYAS y el prompt lo dice; la frase vieja
    # "you have no tools yourself" hacía que narrara sin ejecutar (medido).
    assert "workspace_read_file" in instructions
    assert "no tools yourself" not in instructions
    # Y con el repo en disco, buscar en la web no es alternativa a leerlo.
    assert "web_search" not in {t["type"] for t in call["tools"]}
    assert "web_search" in {t["type"] for t in fake.instances[0].create_kwargs["tools"]}
