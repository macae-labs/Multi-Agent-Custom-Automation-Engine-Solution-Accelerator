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
async def test_handoff_triage_routes_to_the_specialist_who_answers_and_the_user_is_asked():
    # Topología documentada: triage al frente, especialistas que devuelven al
    # triage, modo por defecto (human-in-loop). Un handoff es texto + tool call
    # en el mismo turno (Sample Interaction). Triage deriva a B; B responde sin
    # delegar y el flujo pide al usuario: el turno termina ahí.
    triage_client = ScriptedClient(
        "T",
        [
            [
                Content.from_text("Derivé a RuffAgent"),
                Content.from_function_call(
                    "c1", "handoff_to_RuffAgent", arguments="{}"
                ),
            ],
        ],
    )
    b_client = ScriptedClient("B", ["Auditoría hecha"])
    triage = Agent(client=triage_client, name="TriageAgent")
    b = Agent(client=b_client, name="RuffAgent")
    workflow, closables = OrchestrationManager.build_pattern_workflow(
        "handoff", [triage, b]
    )
    assert closables == []

    events = await _run(workflow, "Auditar ruff")

    # Enrutamiento según la doc: triage→especialista, especialista→triage.
    assert any("handoff_to_RuffAgent" in t for t in triage_client.tools_seen[0])
    assert any("handoff_to_TriageAgent" in t for t in b_client.tools_seen[0])
    assert not any("handoff_to_RuffAgent" in t for t in b_client.tools_seen[0])
    # B recibió la conversación entera (tarea + lo dicho por el triage).
    assert any("user:Auditar ruff" in t for t in b_client.seen[0])
    speakers = [s for s, _ in _spoken(events)]
    assert speakers.index("TriageAgent") < speakers.index("RuffAgent")
    assert speakers[-1] == "RuffAgent"
    assert any("Auditoría hecha" in t for _, t in _spoken(events))
    # Modo por defecto: B respondió sin delegar → el flujo pide al usuario.
    assert len(b_client.seen) == 1
    assert any(
        isinstance(e, WorkflowEvent) and e.type == "request_info" for e in events
    )


@pytest.mark.asyncio
async def test_handoff_parks_on_the_user_and_the_next_message_resumes_the_same_workflow(
    monkeypatch,
):
    # El ejemplo de Learn entre DOS mensajes del chat: el especialista pregunta,
    # el workflow se aparca en su checkpoint y el siguiente mensaje lo reanuda
    # (paso 1 restaurar, paso 2 responder) en un workflow construido de nuevo.
    from agent_framework import InMemoryCheckpointStorage

    import v4.orchestration.orchestration_manager as om

    storage = InMemoryCheckpointStorage()
    monkeypatch.setattr(om, "get_checkpoint_storage", lambda: storage)

    def _team(triage_script, support_script):
        triage_client = ScriptedClient("T", triage_script)
        support_client = ScriptedClient("S", support_script)
        refund_client = EchoClient("R")
        agents = [
            Agent(client=triage_client, name="TriageAgent"),
            Agent(client=support_client, name="SupportAgent"),
            Agent(client=refund_client, name="RefundAgent"),
        ]
        return agents, triage_client, support_client, refund_client

    parked: list[dict] = []

    async def _park(info):
        parked.append(info)

    agents, _t, support, _r = _team(
        [
            [
                Content.from_function_call(
                    "c1", "handoff_to_SupportAgent", arguments="{}"
                )
            ],
            "derivo a soporte",
        ],
        ["¿Preferís reemplazo o reembolso?"],
    )
    first = [
        e
        async for e in OrchestrationManager().run_pattern(
            "handoff",
            agents,
            [Message(role="user", text="Mi pedido 1234 llegó roto")],
            user_id="u1",
            session_id="s1",
            on_park=_park,
        )
    ]
    assert [s for s, _ in _spoken(first)][-1] == "SupportAgent"
    assert len(parked) == 1 and parked[0]["kind"] == "HandoffAgentUserRequest"

    # Segundo mensaje: workflow NUEVO (otros objetos), mismo checkpoint. La
    # respuesta vuelve al que preguntó (SupportAgent), que con la conversación
    # completa traspasa directo a RefundAgent: malla, sin autoridad central.
    agents2, triage2, _s2, refund2 = _team(
        ["sin uso"],
        [
            [
                Content.from_function_call(
                    "c2", "handoff_to_RefundAgent", arguments="{}"
                )
            ],
            "paso a reembolsos",
        ],
    )
    second = [
        e
        async for e in OrchestrationManager().run_pattern(
            "handoff",
            agents2,
            [],
            user_id="u1",
            session_id="s1",
            on_park=_park,
            resume={
                "checkpoint_id": parked[0]["checkpoint_id"],
                "answer": "Quiero reembolso",
            },
        )
    ]
    speakers = [s for s, _ in _spoken(second)]
    assert "RefundAgent" in speakers
    # Nadie volvió a pasar por el triage: el traspaso lo decidió el especialista.
    assert triage2.seen == []
    # La respuesta del usuario llegó al workflow, con el contexto previo.
    assert any("Quiero reembolso" in t for seen in refund2.seen for t in seen)


@pytest.mark.asyncio
async def test_handoff_only_the_participants_the_owner_marked_autonomous_continue_alone():
    # El dueño decide por participante (doc: with_autonomous_mode(agents=[...])).
    # El autónomo, si no traspasa, recibe la continuación del framework; el
    # resto devuelve al usuario.
    a_client = ScriptedClient(
        "A",
        [
            [Content.from_function_call("c1", "handoff_to_RuffAgent", arguments="{}")],
            "a",
        ],
    )
    b_client = ScriptedClient(
        "B",
        [
            "primera parte",
            [Content.from_function_call("c2", "handoff_to_SrcAgent", arguments="{}")],
            "b",
        ],
    )
    a = Agent(client=a_client, name="SrcAgent")
    b = Agent(client=b_client, name="RuffAgent")
    workflow, _ = OrchestrationManager.build_pattern_workflow(
        "handoff", [a, b], autonomous={"RuffAgent"}
    )
    await _run(workflow, "Auditar ruff")

    # RuffAgent respondió sin traspasar y, por ser autónomo, el framework lo
    # volvió a invocar con su continuación en vez de pedir al usuario.
    assert len(b_client.seen) >= 2
    assert any("Continue assisting autonomously" in t for t in b_client.seen[1])


@pytest.mark.asyncio
async def test_a_tool_that_needs_approval_parks_and_the_decision_resumes_it(
    monkeypatch,
):
    # Doc: una tool que requiere aprobación emite function_approval_request; la
    # decisión vuelve con to_function_approval_response sobre el checkpoint.
    from agent_framework import FunctionTool, InMemoryCheckpointStorage

    import v4.orchestration.orchestration_manager as om

    storage = InMemoryCheckpointStorage()
    monkeypatch.setattr(om, "get_checkpoint_storage", lambda: storage)
    executed: list[str] = []

    def _publish(branch: str) -> str:
        executed.append(branch)
        return f"publicado {branch}"

    def _team():
        tool = FunctionTool(
            func=_publish,
            name="workspace_publish",
            description="publica",
            approval_mode="always_require",
        )
        client = ScriptedClient(
            "P",
            [
                [
                    Content.from_function_call(
                        "t1", "workspace_publish", arguments='{"branch": "main"}'
                    )
                ],
                "listo, publicado",
            ],
        )
        agent = Agent(client=client, name="PublisherAgent", tools=[tool])
        return [agent]

    parked: list[dict] = []

    async def _park(info):
        parked.append(info)

    _ = [
        e
        async for e in OrchestrationManager().run_pattern(
            "handoff",
            _team(),
            [Message(role="user", text="publicá")],
            user_id="u1",
            session_id="s1",
            on_park=_park,
        )
    ]
    assert parked and parked[0]["kind"] == "function_approval_request"
    assert parked[0]["approval"]["tool"] == "workspace_publish"
    assert executed == []  # nada corre sin la decisión

    _ = [
        e
        async for e in OrchestrationManager().run_pattern(
            "handoff",
            _team(),
            [],
            user_id="u1",
            session_id="s1",
            on_park=_park,
            resume={
                "checkpoint_id": parked[0]["checkpoint_id"],
                "answer": "",
                "decision": "approved",
            },
        )
    ]
    assert executed == ["main"]


@pytest.mark.asyncio
async def test_a_composed_chat_turn_that_ends_without_parking_purges_its_checkpoints(
    monkeypatch,
):
    # Un turno compuesto construye con storage durable para poder aparcarse, pero
    # su ``workflow_name`` nunca entra en ``plan.workflow_names`` (el único
    # recorrido de purga de planes). Si termina SIN aparcar, sus checkpoints son
    # huérfanos que ningún plan recoge: ``run_pattern`` los borra al cerrar.
    from agent_framework import InMemoryCheckpointStorage

    import v4.orchestration.orchestration_manager as om

    storage = InMemoryCheckpointStorage()
    monkeypatch.setattr(om, "get_checkpoint_storage", lambda: storage)

    agents = [
        Agent(client=EchoClient("A"), name="SrcAgent"),
        Agent(client=EchoClient("B"), name="RuffAgent"),
    ]

    async def _park(info):  # nunca se llama: el turno no pregunta al usuario
        raise AssertionError("un turno sin request_info no debe aparcar")

    _ = [
        e
        async for e in OrchestrationManager().run_pattern(
            "sequential",
            agents,
            [Message(role="user", text="Auditar ruff")],
            user_id="u1",
            session_id="s1",
            on_park=_park,
        )
    ]
    # El turno terminó sin aparcar: nada que reanudar, ningún checkpoint queda.
    assert storage._checkpoints == {}


@pytest.mark.asyncio
async def test_a_parked_chat_turn_retains_its_checkpoint_and_a_resume_purges_the_spent_one(
    monkeypatch,
):
    # Lo contrario del anterior: un turno que SÍ aparca conserva su checkpoint
    # (es la identidad que el usuario puede reanudar). Al reanudarlo, el linaje
    # del que se reanudó queda consumido y se purga, mientras el nuevo aparcado
    # se conserva — la durabilidad sigue a la identidad vigente, no acumula.
    from agent_framework import InMemoryCheckpointStorage

    import v4.orchestration.orchestration_manager as om

    storage = InMemoryCheckpointStorage()
    monkeypatch.setattr(om, "get_checkpoint_storage", lambda: storage)

    parked: list[dict] = []

    async def _park(info):
        parked.append(info)

    def _team(triage_script, support_script):
        return [
            Agent(client=ScriptedClient("T", triage_script), name="TriageAgent"),
            Agent(client=ScriptedClient("S", support_script), name="SupportAgent"),
            Agent(client=EchoClient("R"), name="RefundAgent"),
        ]

    first_team = _team(
        [
            [
                Content.from_function_call(
                    "c1", "handoff_to_SupportAgent", arguments="{}"
                )
            ],
            "derivo a soporte",
        ],
        ["¿Preferís reemplazo o reembolso?"],
    )
    _ = [
        e
        async for e in OrchestrationManager().run_pattern(
            "handoff",
            first_team,
            [Message(role="user", text="Mi pedido 1234 llegó roto")],
            user_id="u1",
            session_id="s1",
            on_park=_park,
        )
    ]
    # Aparcado: el checkpoint que conserva la solicitud sigue en el storage.
    assert len(parked) == 1
    assert any(
        cp.checkpoint_id == parked[0]["checkpoint_id"]
        for cp in storage._checkpoints.values()
    )

    second_team = _team(
        ["sin uso"],
        [
            [
                Content.from_function_call(
                    "c2", "handoff_to_RefundAgent", arguments="{}"
                )
            ],
            "paso a reembolsos",
        ],
    )
    _ = [
        e
        async for e in OrchestrationManager().run_pattern(
            "handoff",
            second_team,
            [],
            user_id="u1",
            session_id="s1",
            on_park=_park,
            resume={
                "checkpoint_id": parked[0]["checkpoint_id"],
                "answer": "Quiero reembolso",
            },
        )
    ]
    # La reanudación volvió a aparcar (handoff devuelve al usuario): el linaje
    # del que se reanudó ya está consumido y se purgó; sólo queda el nuevo.
    assert len(parked) == 2
    present = {cp.checkpoint_id for cp in storage._checkpoints.values()}
    assert parked[0]["checkpoint_id"] not in present
    assert parked[1]["checkpoint_id"] in present
