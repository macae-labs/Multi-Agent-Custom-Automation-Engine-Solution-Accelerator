"""ProxyAgent real: pregunta con un Content marcado user_input_request=True y deja el
ancla en session.state; con el ancla puesta, un mensaje user es la respuesta y continúa."""

import pytest
from agent_framework import AgentResponse, Message

from v4.magentic_agents.proxy_agent import PENDING_CLARIFICATION_KEY, ProxyAgent


@pytest.mark.asyncio
async def test_first_run_asks_with_marked_content_and_anchors_in_session_state():
    agent = ProxyAgent(user_id="u1", session_id="s1")
    session = agent.create_session()
    response = await agent.run([Message(role="user", text="¿Qué presupuesto tienes?")], session=session)
    assert isinstance(response, AgentResponse)
    [request] = response.user_input_requests
    assert request.user_input_request is True and request.text == "¿Qué presupuesto tienes?"
    assert session.state[PENDING_CLARIFICATION_KEY] == {"id": request.id, "question": request.text}


@pytest.mark.asyncio
async def test_answer_continues_and_clears_the_anchor():
    agent = ProxyAgent(user_id="u1")
    session = agent.create_session()
    await agent.run("¿Qué presupuesto tienes?", session=session)
    second = await agent.run([Message(role="user", text="5000")], session=session)
    assert not second.user_input_requests
    assert "5000" in (second.messages[0].text or "")
    assert PENDING_CLARIFICATION_KEY not in session.state


@pytest.mark.asyncio
async def test_streaming_yields_the_marked_content():
    agent = ProxyAgent()
    session = agent.create_session()
    updates = [u async for u in agent.run("¿Cuál?", stream=True, session=session)]
    assert [c.user_input_request for u in updates for c in u.contents] == [True]


@pytest.mark.asyncio
async def test_each_question_gets_its_own_id():
    agent = ProxyAgent()
    a = (await agent.run("A?", session=agent.create_session())).user_input_requests[0]
    b = (await agent.run("B?", session=agent.create_session())).user_input_requests[0]
    assert a.id != b.id and (a.text, b.text) == ("A?", "B?")


def test_message_normalization():
    agent = ProxyAgent()
    assert [m.text for m in agent._as_messages(["a", "b"])] == ["a", "b"]
    assert agent._as_messages(None) == []
    assert agent._extract_message_text([Message(role="user", text="x"), Message(role="user", text="y")]) == "x y"
