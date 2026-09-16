"""Harness de la clarificación al usuario por el camino nativo, medido sin Foundry.

AgentExecutor._run_agent_streaming: si el participante emite un Content con
user_input_request=True, el executor registra la petición por Content.id,
llama ctx.request_info(request, Content) y el workflow queda inactivo con la
petición en el checkpoint. Al reanudar con run(checkpoint_id, responses=
{request_id: Content}), handle_user_input_response reconstruye la entrada del
agente como Message(role="user"|"tool", contents=[…]) y vuelve a invocarlo.

El participante es un BaseAgent que espeja ProxyAgent.run (ResponseStream
sobre un generador de AgentResponseUpdate propios; no hay cliente de modelo).
Un BaseAgent recibe en run() sólo el _cache del executor, no la conversación,
así que el ancla de "¿ya pregunté?" no puede ser el texto ni un contador de la
instancia (la instancia reanudada es nueva): es el id del Content marcado,
guardado en session.state, que el checkpoint serializa y restaura. Se mide
con dos aclaraciones en el mismo plan.
"""

import json
import uuid

import pytest
from agent_framework import (
    Agent,
    AgentResponse,
    AgentResponseUpdate,
    BaseAgent,
    BaseChatClient,
    ChatResponse,
    Content,
    Message,
)
from agent_framework._types import ResponseStream
from agent_framework_orchestrations._magentic import MagenticBuilder, StandardMagenticManager

from common.services.checkpoint_storage import CosmosCheckpointStorage

ANSWER_PREFIX = "RESPUESTA DEL CLARIFIER"


def _ledger(satisfied: bool) -> str:
    return json.dumps({
        "is_request_satisfied": {"reason": "r", "answer": satisfied},
        "is_in_loop": {"reason": "r", "answer": False},
        "is_progress_being_made": {"reason": "r", "answer": True},
        "next_speaker": {"reason": "r", "answer": "Clarifier"},
        "instruction_or_question": {"reason": "r", "answer": "Pide al usuario el dato que falta"},
    })


class ManagerClient(BaseChatClient):
    """Manager guionado: elige a Clarifier hasta contar `rounds` respuestas suyas."""

    def __init__(self, rounds: int) -> None:
        super().__init__()
        self.rounds = rounds

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        assert not stream
        return self._respond(messages)

    async def _respond(self, messages) -> ChatResponse:
        prompt = (messages[-1].text or "").lower()
        answered = sum((m.text or "").count(ANSWER_PREFIX) for m in messages)
        if "is_request_satisfied" in prompt:
            text = _ledger(satisfied=answered >= self.rounds)
        elif "final answer" in prompt:
            text = "FINAL"
        elif "plan" in prompt and "fact" not in prompt:
            text = "- **Clarifier** to ask the user for the missing data"
        else:
            text = "Hechos: faltan datos."
        return ChatResponse(messages=[Message(role="assistant", text=text)])


def _as_messages(messages) -> list[Message]:
    if messages is None:
        return []
    if isinstance(messages, (str, Message)):
        messages = [messages]
    return [m if isinstance(m, Message) else Message(role="user", text=str(m)) for m in messages]


class ProxyLikeAgent(BaseAgent):
    """Espejo de ProxyAgent.run: pregunta con un Content marcado o continúa con la respuesta.

    Ancla determinista: session.state["pending_question"] = id del Content
    marcado que emitió; si existe y llega un mensaje user, es la respuesta.
    """

    def __init__(self, log: list) -> None:
        super().__init__(name="Clarifier")
        self.log = log  # compartido entre instancias: lo que cada run() recibió

    def run(self, messages=None, *, stream=False, session=None, **kwargs):
        if stream:
            return ResponseStream(self._stream(messages, session), finalizer=AgentResponse.from_updates)

        async def _non_stream():
            return AgentResponse.from_updates([u async for u in self._stream(messages, session)])

        return _non_stream()

    async def _stream(self, messages, session):
        msgs = _as_messages(messages)
        state = session.state
        self.log.append({
            "messages": [(m.role, [(c.type, c.id, c.user_input_request, c.text) for c in m.contents]) for m in msgs],
            "state": dict(state),
        })
        pending = state.get("pending_question")
        if pending and msgs and msgs[-1].role == "user":
            state.pop("pending_question")
            yield AgentResponseUpdate(role="assistant", contents=[Content.from_text(f"{ANSWER_PREFIX} {pending}: {msgs[-1].text}")])
            return
        qid = f"q-{uuid.uuid4().hex[:6]}"
        state["pending_question"] = qid
        yield AgentResponseUpdate(role="assistant", contents=[Content("text", text=f"¿Dato para {qid}?", id=qid, user_input_request=True)])


def _workflow(storage, log: list, rounds: int = 1):
    manager = StandardMagenticManager(agent=Agent(client=ManagerClient(rounds), name="MagenticManager"), max_round_count=6)
    return MagenticBuilder(participants=[ProxyLikeAgent(log)], manager=manager, checkpoint_storage=storage).build()


@pytest.fixture
def storage(fake_cosmos_container):
    return CosmosCheckpointStorage(container=fake_cosmos_container)


async def _run_and_collect(coro_stream):
    asks = []
    async for event in coro_stream:
        if event.type == "request_info":
            asks.append(event)
    return asks


@pytest.mark.asyncio
async def test_question_is_marked_content_and_the_anchor_is_in_session_state(storage):
    log = []
    workflow = _workflow(storage, log)
    asks = await _run_and_collect(workflow.run("Completa el formulario", stream=True))
    assert len(asks) == 1
    ask = asks[0]
    assert isinstance(ask.data, Content) and ask.data.user_input_request is True and ask.data.id.startswith("q-")
    assert ask.request_id != ask.data.id  # id del evento (workflow) ≠ id del Content (agente)

    latest = await storage.get_latest(workflow_name=workflow.name)
    assert latest is not None and list(latest.pending_request_info_events) == [ask.request_id]
    assert latest.pending_request_info_events[ask.request_id].data.id == ask.data.id
    # El primer run() recibe sólo la instrucción del manager y un estado vacío.
    assert log[0]["messages"][-1][0] == "user" and log[0]["state"] == {}


@pytest.mark.asyncio
async def test_answer_resumes_new_instance_with_user_role_and_restored_state(storage):
    log = []
    workflow = _workflow(storage, log)
    ask = (await _run_and_collect(workflow.run("Completa el formulario", stream=True)))[0]
    latest = await storage.get_latest(workflow_name=workflow.name)

    log2: list = []
    result = await _workflow(storage, log2).run(
        checkpoint_id=latest.checkpoint_id,
        checkpoint_storage=storage,
        responses={ask.request_id: Content.from_text("42")},
    )
    # La instancia nueva recibe SÓLO la respuesta (el _cache del executor), con rol user,
    # y el estado de sesión restaurado desde el checkpoint con el id de la pregunta pendiente.
    first = log2[0]
    assert first["messages"] == [("user", [("text", None, None, "42")])]
    assert first["state"] == {"pending_question": ask.data.id}
    texts = [m.text for out in result.get_outputs() for m in (out if isinstance(out, list) else [out]) if isinstance(m, Message)]
    assert texts and texts[-1] == "FINAL", texts
    assert not result.get_request_info_events()


@pytest.mark.asyncio
async def test_two_clarifications_in_one_plan_resume_by_their_own_id(storage):
    log: list = []
    workflow = _workflow(storage, log, rounds=2)
    first_ask = (await _run_and_collect(workflow.run("Completa el formulario", stream=True)))[0]
    cp1 = await storage.get_latest(workflow_name=workflow.name)

    # Primera respuesta: el plan sigue (el manager quiere dos rondas) y aparece la segunda pregunta.
    resumed = _workflow(storage, log, rounds=2)
    second_asks = await _run_and_collect(resumed.run(
        checkpoint_id=cp1.checkpoint_id, checkpoint_storage=storage,
        responses={first_ask.request_id: Content.from_text("A1")}, stream=True,
    ))
    assert len(second_asks) == 1 and second_asks[0].data.id != first_ask.data.id
    cp2 = await storage.get_latest(workflow_name=resumed.name)
    assert list(cp2.pending_request_info_events) == [second_asks[0].request_id]

    result = await _workflow(storage, log, rounds=2).run(
        checkpoint_id=cp2.checkpoint_id, checkpoint_storage=storage,
        responses={second_asks[0].request_id: Content.from_text("A2")},
    )
    texts = [m.text for out in result.get_outputs() for m in (out if isinstance(out, list) else [out]) if isinstance(m, Message)]
    assert texts and texts[-1] == "FINAL", texts
    answers = [e for e in log if e["messages"] and e["messages"][-1][1][0][3] in ("A1", "A2")]
    assert [e["state"]["pending_question"] for e in answers] == [first_ask.data.id, second_asks[0].data.id]


@pytest.mark.asyncio
async def test_function_result_answer_arrives_with_tool_role(storage):
    log: list = []
    workflow = _workflow(storage, log)
    ask = (await _run_and_collect(workflow.run("Completa el formulario", stream=True)))[0]
    latest = await storage.get_latest(workflow_name=workflow.name)
    log2: list = []
    await _workflow(storage, log2).run(
        checkpoint_id=latest.checkpoint_id, checkpoint_storage=storage,
        responses={ask.request_id: Content.from_function_result(call_id=ask.data.id, result="42")},
    )
    assert log2[0]["messages"][0][0] == "tool"
