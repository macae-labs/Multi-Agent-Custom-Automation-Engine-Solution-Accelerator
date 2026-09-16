"""
ProxyAgent: Human clarification proxy compliant with agent_framework.

The agent never waits in-process. It emits a ``Content`` marked
``user_input_request=True``; the framework's ``AgentExecutor`` turns that into a
``request_info`` event, the workflow goes idle and the pending request lands in
the checkpoint. The answer comes back through
``workflow.run(checkpoint_id=..., responses={request_id: Content})`` as the
agent's next input (a single ``user`` message).

Deterministic rule (measured in test_user_input_durable_harness.py):

* anchor ``session.state["pending_clarification"] = {"id", "question"}`` is written
  BEFORE the stream ends (``on_checkpoint_save`` serializes the session when the
  superstep closes) and survives checkpoint and ``MagenticResetSignal``;
* if the anchor exists and the incoming messages end with a ``user`` message,
  that message is the human answer → clear the anchor and reply with it;
* otherwise ask, with a fresh ``Content.id``, and store the anchor.

The WebSocket notification (USER_CLARIFICATION_REQUEST) is emitted by the
``request_info`` handler in ``OrchestrationManager``, not here.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, AsyncIterable, Awaitable, Final

from agent_framework import (
    AgentResponse,
    AgentResponseUpdate,
    AgentSession,
    BaseAgent,
    Content,
    Message,
    UsageDetails,
)
from agent_framework._types import ResponseStream

logger = logging.getLogger(__name__)

PENDING_CLARIFICATION_KEY = "pending_clarification"
CLARIFICATION_CONTENT_TYPE: Final = "text"


class ProxyAgent(BaseAgent):
    """
    A human-in-the-loop clarification agent extending agent_framework's BaseAgent.

    This agent mediates human clarification requests rather than using an LLM.
    It follows the agent_framework protocol with run() method (stream=True/False).
    """

    def __init__(
        self,
        user_id: str | None = None,
        session_id: str | None = None,
        name: str = "ProxyAgent",
        description: str = (
            "Clarification agent. Ask this when instructions are unclear or additional "
            "user details are required."
        ),
        **kwargs: Any,
    ):
        super().__init__(name=name, description=description, **kwargs)
        self.user_id = user_id or ""
        self.session_id = session_id or ""

    # ---------------------------
    # AgentProtocol implementation
    # ---------------------------

    def create_session(
        self, *, session_id: str | None = None, **kwargs: Any
    ) -> AgentSession:
        """Create a new session; ``session.state`` carries the clarification anchor."""
        return AgentSession(session_id=session_id, **kwargs)

    def run(
        self,
        messages: str | Message | list[str] | list[Message] | None = None,
        *,
        stream: bool = False,
        session: AgentSession | None = None,
        **kwargs: Any,
    ) -> Awaitable[AgentResponse] | ResponseStream[AgentResponseUpdate, AgentResponse]:
        """
        Run clarification (streaming or non-streaming).

        Must be a regular def (not async def) to match the Agent.run() contract.
        The framework calls agent.run() without await and expects either
        a ResponseStream (stream=True) or an Awaitable (stream=False).
        """
        if stream:
            return ResponseStream(
                self._invoke_stream_internal(messages, session),
                finalizer=lambda updates: AgentResponse.from_updates(updates),
            )

        async def _run_non_streaming() -> AgentResponse:
            updates = [
                update
                async for update in self._invoke_stream_internal(messages, session)
            ]
            return AgentResponse.from_updates(updates)

        return _run_non_streaming()

    async def _invoke_stream_internal(
        self,
        messages: str | Message | list[str] | list[Message] | None,
        session: AgentSession | None,
    ) -> AsyncIterable[AgentResponseUpdate]:
        normalized = self._as_messages(messages)
        state: dict[str, Any] = session.state if session is not None else {}
        pending = state.get(PENDING_CLARIFICATION_KEY)

        if pending and normalized and normalized[-1].role == "user":
            # The human answer arrived (executor _cache after handle_user_input_response).
            state.pop(PENDING_CLARIFICATION_KEY, None)
            answer_text = (
                normalized[-1].text or ""
            ).strip() or "No additional clarification provided."
            logger.info(
                "ProxyAgent: clarification %s answered (%d chars)",
                pending.get("id"),
                len(answer_text),
            )
            response_id = str(uuid.uuid4())
            message_id = str(uuid.uuid4())
            yield AgentResponseUpdate(
                role="assistant",
                contents=[Content.from_text(text=answer_text)],
                author_name=self.name,
                response_id=response_id,
                message_id=message_id,
            )
            question_words = len(str(pending.get("question", "")).split())
            answer_words = len(answer_text.split())
            yield AgentResponseUpdate(
                role="assistant",
                contents=[
                    Content.from_usage(
                        UsageDetails(
                            input_token_count=question_words,
                            output_token_count=answer_words,
                            total_token_count=question_words + answer_words,
                        )
                    )
                ],
                author_name=self.name,
                response_id=response_id,
                message_id=message_id,
            )
            return

        # Ask: the manager's instruction is the question for the human.
        question = self._extract_message_text(normalized)
        content_id = f"clarification-{uuid.uuid4()}"
        # Anchor BEFORE the stream ends so on_checkpoint_save serializes it.
        state[PENDING_CLARIFICATION_KEY] = {"id": content_id, "question": question}
        logger.info(
            "ProxyAgent: requesting clarification %s (session=%s, user=%s)",
            content_id,
            "present" if session else "None",
            self.user_id,
        )
        yield AgentResponseUpdate(
            role="assistant",
            contents=[
                Content(
                    CLARIFICATION_CONTENT_TYPE,
                    text=question,
                    id=content_id,
                    user_input_request=True,
                )
            ],
            author_name=self.name,
            response_id=str(uuid.uuid4()),
            message_id=str(uuid.uuid4()),
        )

    # ---------------------------
    # Helper methods
    # ---------------------------

    @staticmethod
    def _as_messages(
        messages: str | Message | list[str] | list[Message] | None,
    ) -> list[Message]:
        if messages is None:
            return []
        if isinstance(messages, (str, Message)):
            return [
                messages
                if isinstance(messages, Message)
                else Message(role="user", text=messages)
            ]
        return [
            m if isinstance(m, Message) else Message(role="user", text=str(m))
            for m in messages
        ]

    def _extract_message_text(
        self, messages: str | Message | list[str] | list[Message] | None
    ) -> str:
        """Extract text from various message formats."""
        if messages is None:
            return ""
        if isinstance(messages, str):
            return messages
        if isinstance(messages, Message):
            return messages.text or ""
        if isinstance(messages, list):
            if not messages:
                return ""
            first = messages[0]
            if isinstance(first, str):
                return " ".join(str(m) for m in messages)
            if isinstance(first, Message):
                return " ".join(
                    (m.text or "") for m in messages if isinstance(m, Message)
                )
        return str(messages)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def create_proxy_agent(user_id: str | None = None) -> ProxyAgent:
    """Factory for ProxyAgent."""
    return ProxyAgent(user_id=user_id)
