"""Semánticas del framework con agentes REALES y el workflow REAL que arma
``build_pattern_workflow`` — sin red y sin simular los builders.

Un cliente de chat que devuelve lo que recibió deja ver qué contexto llega a
cada participante. Eso es la semántica (quién recibe qué, en qué orden, qué se
agrega); los tests con builders simulados sólo prueban el cableado.
"""

import json

import pytest
from agent_framework import (
    Agent,
    AgentResponse,
    AgentResponseUpdate,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
    ResponseStream,
    WorkflowEvent,
)

from v4.orchestration.orchestration_manager import OrchestrationManager


def _role(m):
    role = getattr(m, "role", "")
    return str(getattr(role, "value", role)).lower()


class EchoClient(BaseChatClient):
    """Responde describiendo exactamente lo que recibió."""

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label
        self.seen: list[list[str]] = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        texts = [f"{_role(m)}:{m.text}" for m in messages if m.text]
        self.seen.append(texts)
        reply = f"{self.label} respondió a: {messages[-1].text}"
        if stream:

            async def _stream():
                yield ChatResponseUpdate(
                    role="assistant", contents=[{"type": "text", "text": reply}]
                )

            # El contrato del framework: un ResponseStream con finalizador que
            # convierte los updates en la ChatResponse final.
            return ResponseStream(
                _stream(),
                finalizer=lambda updates: ChatResponse(
                    messages=[Message(role="assistant", text=reply)]
                ),
            )

        async def _response():
            return ChatResponse(messages=[Message(role="assistant", text=reply)])

        return _response()


async def _run(workflow, task: str):
    events = []
    async for event in workflow.run([Message(role="user", text=task)], stream=True):
        events.append(event)
    return events


def _spoken(events):
    """(executor, texto) de las salidas de agentes, en orden."""
    out = []
    for e in events:
        if not isinstance(e, WorkflowEvent) or e.type != "output":
            continue
        data = e.data
        if isinstance(data, AgentResponse):
            text = "".join(m.text or "" for m in data.messages)
        elif isinstance(data, AgentResponseUpdate):
            text = "".join(getattr(c, "text", "") or "" for c in data.contents)
        else:
            continue
        if text:
            out.append((e.executor_id, text))
    return out


@pytest.mark.asyncio
async def test_sequential_each_stage_gets_its_predecessor_output_and_its_own_step():
    a_client, b_client = EchoClient("A"), EchoClient("B")
    a = Agent(client=a_client, name="SrcAgent")
    b = Agent(client=b_client, name="RuffAgent")
    workflow, closables = OrchestrationManager.build_pattern_workflow(
        "sequential", [a, b], steps={"RuffAgent": "correr ruff en cada directorio"}
    )
    assert closables == []

    events = await _run(workflow, "Auditar ruff del workspace")

    # El primero recibe la tarea.
    assert a_client.seen[0] == ["user:Auditar ruff del workspace"]
    # El segundo recibe la salida del primero y SU paso, no la tarea original:
    # la etapa es posición más instrucción propia.
    assert b_client.seen[0] == [
        "assistant:A respondió a: Auditar ruff del workspace",
        "user:correr ruff en cada directorio",
    ]
    speakers = [s for s, _ in _spoken(events)]
    assert speakers.index("SrcAgent") < speakers.index("RuffAgent")
    assert any("B respondió a: correr ruff" in t for _, t in _spoken(events))


@pytest.mark.asyncio
async def test_sequential_without_a_step_is_last_agent_only():
    a_client, b_client = EchoClient("A"), EchoClient("B")
    a = Agent(client=a_client, name="SrcAgent")
    b = Agent(client=b_client, name="RuffAgent")
    workflow, _ = OrchestrationManager.build_pattern_workflow("sequential", [a, b])

    await _run(workflow, "Tarea")

    assert b_client.seen[0] == ["assistant:A respondió a: Tarea"]


@pytest.mark.asyncio
async def test_concurrent_every_participant_gets_the_same_task_and_the_aggregate_has_all():
    a_client, b_client = EchoClient("A"), EchoClient("B")
    a = Agent(client=a_client, name="LensA")
    b = Agent(client=b_client, name="LensB")
    workflow, closables = OrchestrationManager.build_pattern_workflow(
        "concurrent", [a, b]
    )
    assert closables == []

    events = await _run(workflow, "Evaluá esta propuesta")

    # Fan-out del MISMO input: nadie ve al otro.
    assert a_client.seen == [["user:Evaluá esta propuesta"]]
    assert b_client.seen == [["user:Evaluá esta propuesta"]]
    # El agregador por defecto reúne las respuestas de todos en una lista.
    aggregated = [
        e.data
        for e in events
        if isinstance(e, WorkflowEvent)
        and e.type == "output"
        and isinstance(e.data, list)
    ]
    assert aggregated, "sin salida agregada"
    texts = "\n".join(m.text or "" for m in aggregated[-1] if hasattr(m, "text"))
    assert "A respondió a: Evaluá esta propuesta" in texts
    assert "B respondió a: Evaluá esta propuesta" in texts


class ScriptedClient(FunctionInvocationLayer, BaseChatClient):
    """Devuelve, en orden, las respuestas guionadas (texto o contenidos).

    Con la capa de invocación de funciones del framework: si el guion devuelve
    una llamada a función (p. ej. ``handoff_to_X``), el agente la ejecuta de
    verdad y vuelve a llamar al cliente con el resultado, como con un modelo.
    """

    def __init__(self, label: str, script: list) -> None:
        super().__init__()
        self.label = label
        self.script = list(script)
        self.seen: list[list[str]] = []
        self.tools_seen: list[list[str]] = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.seen.append([f"{_role(m)}:{m.text}" for m in messages if m.text])
        self.tools_seen.append(
            [
                str(
                    getattr(t, "name", None)
                    or (t.get("name") if isinstance(t, dict) else t)
                )
                for t in (options.get("tools") or [])
            ]
        )
        step = self.script.pop(0) if self.script else f"{self.label}: sin guion"
        contents = step if isinstance(step, list) else [Content.from_text(step)]
        message = Message(role="assistant", contents=contents)
        if stream:

            async def _stream():
                yield ChatResponseUpdate(role="assistant", contents=list(contents))

            return ResponseStream(
                _stream(), finalizer=lambda updates: ChatResponse(messages=[message])
            )

        async def _response():
            return ChatResponse(messages=[message])

        return _response()


def _decision(next_speaker=None, terminate=False, final=None):
    return json.dumps(
        {
            "terminate": terminate,
            "reason": "guion",
            "next_speaker": next_speaker,
            "final_message": final,
        }
    )


@pytest.mark.asyncio
async def test_group_chat_the_orchestrator_picks_who_speaks_and_everyone_sees_the_whole_history():
    a_client, b_client = EchoClient("A"), EchoClient("B")
    a = Agent(client=a_client, name="LensA")
    b = Agent(client=b_client, name="LensB")
    orchestrator_client = ScriptedClient(
        "O",
        [
            _decision(next_speaker="LensB"),
            _decision(next_speaker="LensA"),
            _decision(terminate=True, final="Cerrado por el orquestador"),
        ],
    )
    orchestrator = Agent(client=orchestrator_client, name="GroupChatOrchestrator")
    workflow, closables = OrchestrationManager.build_pattern_workflow(
        "group_chat", [a, b], orchestrator=orchestrator
    )
    # El orquestador inyectado no se cierra desde acá: no lo creó el constructor.
    assert closables == []

    events = await _run(workflow, "Debatan la propuesta")

    # Quién habla lo decide el orquestador, en el orden que eligió.
    assert [s for s, _ in _spoken(events)] == ["LensB", "LensA"]
    # Sincronización de contexto: el segundo en hablar ve la tarea Y lo que dijo
    # el primero (historial completo), no sólo su turno.
    assert b_client.seen[0] == ["user:Debatan la propuesta"]
    assert a_client.seen[0] == [
        "user:Debatan la propuesta",
        "assistant:B respondió a: Debatan la propuesta",
    ]
    # El orquestador decidió tres veces: B, A, terminar.
    assert len(orchestrator_client.seen) == 3
    final = [
        e.data
        for e in events
        if isinstance(e, WorkflowEvent)
        and e.type == "output"
        and isinstance(e.data, list)
    ]
    assert final and any(
        "Cerrado por el orquestador" in (m.text or "")
        for m in final[-1]
        if hasattr(m, "text")
    )


@pytest.mark.asyncio
async def test_handoff_the_first_agent_hands_the_conversation_to_the_second_then_asks_the_user():
    # A arranca, llama a la tool de handoff hacia B; el framework la intercepta,
    # B responde, y sin otro handoff el control vuelve al usuario (request_info).
    a_client = ScriptedClient(
        "A",
        [
            [Content.from_function_call("c1", "handoff_to_RuffAgent", arguments="{}")],
            "A entregó a RuffAgent",
        ],
    )
    b_client = EchoClient("B")
    a = Agent(client=a_client, name="SrcAgent")
    b = Agent(client=b_client, name="RuffAgent")
    workflow, closables = OrchestrationManager.build_pattern_workflow("handoff", [a, b])
    assert closables == []

    events = await _run(workflow, "Auditar ruff")

    # A recibió la tarea y la tool de handoff hacia B estaba en su oferta.
    assert a_client.seen[0] == ["user:Auditar ruff"]
    assert any("handoff_to_RuffAgent" in t for t in a_client.tools_seen[0])
    # B habló después de A, con la conversación de A.
    speakers = [s for s, _ in _spoken(events)]
    assert "RuffAgent" in speakers
    assert any("user:Auditar ruff" in t for t in b_client.seen[0])
    # Sin otro handoff, el framework pide al usuario: el turno del chat termina ahí
    # (run_pattern sin plan_id) y el carril de plan aparca.
    assert any(
        isinstance(e, WorkflowEvent) and e.type == "request_info" for e in events
    )
