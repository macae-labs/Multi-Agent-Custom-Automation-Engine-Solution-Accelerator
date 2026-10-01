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
from agent_framework import AgentResponseUpdate, Content, WorkflowEvent

from common.services.event_store import EventStore, MemoryContainer, set_event_store
from v4.api import router


@pytest.fixture(autouse=True)
def _ledger_store():
    """El turno escribe su ledger (objetivo, hechos, veredictos) en el store
    de eventos; acá vive en memoria, con el mismo contrato de identidad."""
    store = EventStore(MemoryContainer())
    set_event_store(store)
    yield store
    set_event_store(None)


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
    c._reasoning = {"effort": "medium"}
    c._reasoning_eval = {"effort": "low"}
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
    c._turn_id = "t1"
    c._session_id = "s1"
    c._ledger_unavailable = False
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
async def test_the_orchestrator_offers_compose_and_its_own_capabilities():
    fake = _fake_openai(_Stream([]))
    with patch("openai.AsyncOpenAI", fake):
        await _collect(_client())

    tools = fake.instances[-1].create_kwargs["tools"]
    # La decisión de componer es del dueño: ``compose`` con las cinco
    # semánticas del framework va en la misma oferta que sus capacidades
    # propias. Sin workspace montado, la web sí se ofrece.
    assert tools[0]["name"] == "compose"
    assert tools[0]["parameters"]["properties"]["pattern"]["enum"] == list(
        router._PATTERNS
    )
    assert [t["type"] for t in tools[1:]] == [
        "image_generation",
        "web_search",
        "code_interpreter",
    ]
    assert tools[3]["container"] == {"type": "auto"}


@pytest.mark.asyncio
async def test_a_composed_run_leaves_facts_and_is_judged_by_the_verdict(
    _ledger_store, monkeypatch
):
    # La orquestación elegida corre dentro del turno y bajo la misma ley: lo
    # que cada participante observó son hechos del turno y el veredicto juzga
    # su resultado. Lo que dijeron ya salió por sus eventos: no se repite.
    call = SimpleNamespace(
        type="function_call",
        name="compose",
        arguments=json.dumps(
            {
                "pattern": "sequential",
                "task": "pyproject bajo src/",
                "participants": [
                    {"name": "SrcAgent", "description": "d", "system_message": "s"}
                ],
            }
        ),
    )
    fake = _fake_openai(_Stream([_done(call)]), _verdict(True))
    client = _client()
    result = json.dumps(
        {"status": "success", "details": {"matches": ["src/backend/pyproject.toml"]}}
    )

    async def _run_pattern(pattern, task, participants, history):
        yield WorkflowEvent(
            "output",
            data=AgentResponseUpdate(
                contents=[
                    Content.from_function_call(
                        "c1",
                        "workspace_search_files",
                        arguments='{"query": "pyproject"}',
                    )
                ],
                role="assistant",
            ),
            executor_id="SrcAgent",
        )
        yield WorkflowEvent(
            "output",
            data=AgentResponseUpdate(
                contents=[Content.from_function_result("c1", result=result)],
                role="assistant",
            ),
            executor_id="SrcAgent",
        )
        yield WorkflowEvent(
            "output",
            data=AgentResponseUpdate(
                contents=[Content.from_text("Encontré src/backend/pyproject.toml")],
                role="assistant",
            ),
            executor_id="SrcAgent",
        )

    monkeypatch.setattr(client, "_run_pattern", _run_pattern)
    with patch("openai.AsyncOpenAI", fake):
        updates = await _collect(client, prompt="un especialista que busque pyproject")

    assert [type(u).__name__ for u in updates] == ["WorkflowEvent"] * 3
    calls = fake.instances[-1].calls
    assert len(calls) == 2
    verdict_input = calls[1]["input"][0]["content"]
    assert "sequential con SrcAgent" in verdict_input
    assert "workspace_search_files" in verdict_input
    assert "Encontré src/backend/pyproject.toml" in verdict_input
    assert router._NO_ORCHESTRATION_FACT not in verdict_input
    events = await _ledger_store.history("t1")
    facts = [e["identity"] for e in events if e["kind"] == "fact"]
    assert any(i.startswith("t1:SrcAgent:workspace_search_files:") for i in facts)
    assert any(":workspace_search_files:" not in i for i in facts)
    assert [e["identity"] for e in events if e["kind"] == "verdict"] == ["t1:1"]
    closed = next(e for e in events if e["identity"] == "t1:closed")
    assert closed["payload"]["status"] == "done"


@pytest.mark.asyncio
async def test_composed_tool_results_are_judged_and_unmet_goal_is_reported(
    _ledger_store, monkeypatch
):
    call = SimpleNamespace(
        type="function_call",
        name="compose",
        arguments=json.dumps(
            {
                "pattern": "sequential",
                "task": "investigate",
                "participants": [
                    {"name": "SrcAgent", "description": "d", "system_message": "s"}
                ],
            }
        ),
    )
    fake = _fake_openai(_Stream([_done(call)]), _verdict(False, "faltan datos"))
    client = _client()

    async def _run_pattern(pattern, task, participants, history):
        for content in (
            Content.from_mcp_server_tool_result("m1", output="mcp evidence"),
            Content.from_code_interpreter_tool_result(
                outputs=[Content.from_text("code evidence")]
            ),
        ):
            yield WorkflowEvent(
                "output",
                data=AgentResponseUpdate(contents=[content], role="assistant"),
                executor_id="SrcAgent",
            )

    monkeypatch.setattr(client, "_run_pattern", _run_pattern)
    with patch("openai.AsyncOpenAI", fake):
        updates = await _collect(client)

    assert "Objetivo no cumplido: faltan datos" in str(updates[-1].contents[0].text)
    evidence = fake.instances[-1].calls[1]["input"][0]["content"]
    assert "mcp evidence" in evidence
    assert "code evidence" in evidence
    events = await _ledger_store.history("t1")
    assert len([e for e in events if e["kind"] == "fact"]) == 2
    closed = next(e for e in events if e["identity"] == "t1:closed")
    assert closed["payload"]["status"] == "incomplete"


@pytest.mark.asyncio
async def test_a_direct_answer_tells_the_verdict_that_no_specialists_ran():
    # Si el objetivo pedía especialistas y el orquestador respondió solo, el
    # veredicto tiene que verlo como hecho del turno, no adivinarlo.
    fake = _fake_openai(_Stream([_text("hecho")]), _verdict(True))
    with patch("openai.AsyncOpenAI", fake):
        await _collect(_client(), prompt="dos especialistas en secuencia")
    calls = fake.instances[-1].calls
    assert len(calls) == 2
    assert router._NO_ORCHESTRATION_FACT in calls[1]["input"][0]["content"]


# ── el libro de evidencia es del TURNO ───────────────────────────────────────


def _verdict(goal_met, reason="", corrected=""):
    return SimpleNamespace(
        output_text=json.dumps(
            {
                "goal_met": goal_met,
                "blocked": False,
                "reason": reason,
                "corrected_objective": corrected,
            }
        )
    )


def _exec_output(command, stdout, exit_code=0):
    return json.dumps(
        {
            "status": "success",
            "action": "workspace_exec",
            "summary": f"exit={exit_code} for `{command}`",
            "details": {
                "command": command,
                "cwd": "/",
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": "",
                "truncated": False,
            },
        }
    )


def test_a_fact_is_what_the_tool_observed_not_how_it_was_called():
    """Dos scripts distintos que imprimen lo mismo son el mismo hecho; otra
    salida es otro hecho; una salida que no es el envelope se compara entera."""
    same_a = router._fact_key(_exec_output("ls", "a\nb"))
    same_b = router._fact_key(_exec_output("ls -1 | cat", "a\nb"))
    other = router._fact_key(_exec_output("ls", "a\nc"))
    assert same_a == same_b != other
    assert router._fact_key("texto plano") == router._fact_key("texto plano")
    assert router._fact_key("texto plano") != router._fact_key("otro texto")


@pytest.mark.asyncio
async def test_the_turn_stops_when_a_pass_adds_no_new_fact(_ledger_store):
    """Medido: tres vueltas más reescribiendo el mismo listado. Volver a
    obtener el MISMO dato con otra llamada no es progreso: el turno termina
    tras el segundo veredicto en vez de girar."""
    call_1 = SimpleNamespace(
        type="function_call",
        name="workspace_exec",
        call_id="c1",
        arguments='{"command":"ls"}',
    )
    call_2 = SimpleNamespace(
        type="function_call",
        name="workspace_exec",
        call_id="c2",
        arguments='{"command":"ls -1 | cat"}',
    )
    fake = _fake_openai(
        _Stream([_done(call_1), _text("informe 1")]),
        _verdict(False, "falta"),
        _Stream([_done(call_2), _text("informe 2")]),
        _verdict(False, "sigue faltando"),
    )
    c = _client()
    c._ws_names = {"workspace_exec"}
    c._call_workspace_tool = AsyncMock(
        side_effect=[_exec_output("ls", "a\nb"), _exec_output("ls -1 | cat", "a\nb")]
    )
    with patch("openai.AsyncOpenAI", fake):
        updates = await _collect(c)

    calls = fake.instances[-1].calls
    assert len(calls) == 4, "pasada, veredicto, pasada sin dato nuevo, veredicto: fin"
    assert "".join((x.text or "") for u in updates for x in u.contents) == "informe 2"
    # El dueño del objetivo dejó su historial fuera del request: un objetivo,
    # UN hecho (el segundo era el mismo dato: duplicado por identidad), dos
    # veredictos y el cierre con el motivo.
    ledger = await _ledger_store.history("t1")
    kinds = [(e["kind"], e["identity"]) for e in ledger]
    assert kinds[0] == ("objective", "t1")
    assert [k for k, _ in kinds].count("fact") == 1
    assert [k for k, _ in kinds].count("verdict") == 2
    closed = next(e for e in ledger if e["identity"] == "t1:closed")
    assert closed["payload"] == {"status": "no_progress", "laps": 2, "facts": 1}


@pytest.mark.asyncio
async def test_a_false_premise_is_corrected_and_the_turn_continues_with_the_corrected_objective(
    _ledger_store,
):
    """Validación deliberada del usuario (2026-09-30): la tarea nombra el puerto
    9124 sabiendo que el backend escucha en 8000, para observar la reacción del
    bucle. La reacción correcta es corregir la premisa con el hecho medido y
    continuar: la vuelta siguiente y su veredicto juzgan el objetivo vigente, y
    la verificación llega como nota del sistema, nunca como un mensaje del
    usuario al que el modelo le conteste."""
    call_1 = SimpleNamespace(
        type="function_call",
        name="workspace_exec",
        call_id="c1",
        arguments='{"command":"ss -ltn"}',
    )
    fake = _fake_openai(
        _Stream([_done(call_1), _text("escucha en 8000")]),
        _verdict(False, "dice 9124", corrected="Validá el backend en el puerto 8000"),
        _Stream([_text("validado en 8000")]),
        _verdict(True),
    )
    c = _client()
    c._ws_names = {"workspace_exec"}
    c._call_workspace_tool = AsyncMock(return_value=_exec_output("ss -ltn", ":8000"))
    with patch("openai.AsyncOpenAI", fake):
        updates = await _collect(c, prompt="Validá el backend en el puerto 9124")

    calls = fake.instances[-1].calls
    assert len(calls) == 4
    second_verdict_input = calls[3]["input"][0]["content"]
    assert "OBJETIVO:\nValidá el backend en el puerto 8000" in second_verdict_input
    notes = [
        i
        for i in calls[2]["input"]
        if isinstance(i, dict) and "Verificación" in str(i.get("content", ""))
    ]
    assert notes and all(n["role"] == "developer" for n in notes)
    assert "puerto 8000" in notes[0]["content"]
    assert "".join((x.text or "") for u in updates for x in u.contents).endswith(
        "validado en 8000"
    )
    ledger = await _ledger_store.history("t1")
    first_verdict = next(e for e in ledger if e["identity"] == "t1:1")
    assert (
        first_verdict["payload"]["corrected_objective"]
        == "Validá el backend en el puerto 8000"
    )
    assert (
        next(e for e in ledger if e["identity"] == "t1:closed")["payload"]["status"]
        == "done"
    )


@pytest.mark.asyncio
async def test_the_ledger_of_a_turn_is_read_by_its_owner_only(
    _ledger_store, monkeypatch
):
    await _ledger_store.append("objective", "t9", {"objective": "x", "user_id": "u1"})
    await _ledger_store.append(
        "fact", "t9:workspace_exec:abc", {"tool": "workspace_exec"}
    )
    await _ledger_store.append("objective", "t9:closed", {"status": "done"})
    await _ledger_store.append(
        "objective", "t8", {"objective": "otro", "user_id": "u1"}
    )

    monkeypatch.setattr(router, "_extract_auth", lambda request: ("u1", ""))
    body = await router.chat_turn_ledger("t9", request=None)
    assert [e["identity"] for e in body["events"]] == [
        "t9",
        "t9:workspace_exec:abc",
        "t9:closed",
    ]

    monkeypatch.setattr(router, "_extract_auth", lambda request: ("intruso", ""))
    with pytest.raises(router.HTTPException) as err:
        await router.chat_turn_ledger("t9", request=None)
    assert err.value.status_code == 404


@pytest.mark.asyncio
async def test_a_plan_id_resolves_to_the_ledger_of_its_magentic_plan(
    _ledger_store, monkeypatch
):
    """La UI conoce el ``plan_id``; el dueño del ledger es el id propio del
    MPlan. Se resuelve por el documento del plan (``m_plan``), no adivinando."""
    await _ledger_store.append(
        "objective", "plan:m1", {"objective": "auditar", "user_id": "u1"}
    )
    await _ledger_store.append("verdict", "plan:m1:1", {"kind": "done"})
    plan = SimpleNamespace(m_plan={"id": "m1"})
    store = SimpleNamespace(get_plan=AsyncMock(return_value=plan))
    monkeypatch.setattr(
        router.DatabaseFactory, "get_database", AsyncMock(return_value=store)
    )
    monkeypatch.setattr(router, "_extract_auth", lambda request: ("u1", ""))

    body = await router.chat_turn_ledger("p-ui", request=None)

    assert body["owner"] == "plan:m1"
    assert [e["identity"] for e in body["events"]] == ["plan:m1", "plan:m1:1"]
    store.get_plan.assert_awaited_once_with("p-ui")


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
    # Contrato efectivo de inferencia en CADA llamada: el loop streamed lleva
    # el reasoning del orquestador y el veredicto el del evaluador; ninguna
    # lleva temperature ni top_p (el control que cambia el comportamiento es
    # reasoning.effort: medido low → 0 tokens de razonamiento, medium → 25).
    assert calls[0]["reasoning"] == {"effort": "medium"} and calls[0]["stream"] is True
    assert calls[2]["reasoning"] == {"effort": "low"}
    assert all("temperature" not in c and "top_p" not in c for c in calls)
    assert calls[0]["model"] == calls[2]["model"] == "gpt-5.4-mini"


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
        patch("v4.api.router.ReconnectingMCPTool", _Tool),
        patch(
            "v4.magentic_agents.models.agent_models.MCPConfig.from_env",
            return_value=cfg,
        ),
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
            assert kwargs == {
                "path": "README.md",
                "user_id": "u1",
                "workspace_id": "repo",
            }
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


def _build(monkeypatch, *, model=None, toolboxes="", configured="gpt-5.4-mini"):
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
