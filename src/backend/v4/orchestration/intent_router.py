"""
IntentRouter — Classifies user messages into intent categories.

Uses LLM-based classification (AzureOpenAIChatClient) for accurate intent
detection across languages and phrasings.  The LLM is the *sole* decision
maker; there are no hardcoded keyword/regex fallbacks.

Routes messages to:
  - "task"           → Full plan workflow (existing process_request flow)
  - "conversational" → Direct agent response without plan creation
  - "mcp_query"      → MCP Inspector / bridge query via TechnicalSupportAgent
"""

import logging
from enum import Enum
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    TASK = "task"
    CONVERSATIONAL = "conversational"
    MCP_QUERY = "mcp_query"


class IntentResult(BaseModel):
    intent: Intent
    confidence: float
    reasoning: str


_SYSTEM_PROMPT = """You are an intent classifier for a multi-agent automation system.

The system has three lanes:
1. task — Internal business workflows (onboard employee, generate press release, configure laptop, set up Office 365, etc.) handled by planning agents.
2. mcp_query — Anything involving external services, MCP tools/servers, filesystem operations, directory browsing, connecting to servers, asking about capabilities/tools, or any action on a third-party platform (GitHub, Slack, YouTube, Google Drive, etc.) handled by the MCP Inspector agent.
3. conversational — Pure greetings, farewells, or questions about the assistant itself with NO actionable request.

CRITICAL — Session continuity rule:
When PREVIOUS_INTENT is provided it tells you which lane is currently active.
Short replies such as confirmations, denials, follow-ups, or clarifications
(in any language) are CONTINUATIONS of the active lane, NOT new conversations.
Examples: "Si", "Yes", "Ok", "Hazlo", "Dale", "No", "Cancel", "Go ahead",
"Exactly", "That one", "Please", "Do it", "Why?", "How?", "Show me", etc.
— all of these STAY in the PREVIOUS_INTENT lane.

Only classify as a DIFFERENT lane when the user clearly introduces an
unrelated topic (e.g. switching from filesystem operations to asking about
HR onboarding).

Respond with ONLY one word: task, conversational, or mcp_query."""


class IntentRouter:
    """
    LLM-based intent classifier with keyword fallback.

    Uses AzureOpenAIChatClient.get_response() for accurate classification
    across languages. Falls back to keyword heuristics on failure.
    """

    @staticmethod
    async def classify_async(
        message: str,
        previous_intent: Optional[str] = None,
    ) -> IntentResult:
        """Classify using LLM as the sole decision maker.

        Args:
            message: The user message to classify.
            previous_intent: The intent of the last assistant message in this
                session.  Passed as structured context so the LLM can
                maintain session lane continuity.
        """
        if not message or not message.strip():
            return IntentResult(
                intent=Intent.CONVERSATIONAL,
                confidence=1.0,
                reasoning="Empty message",
            )

        try:
            from azure.identity.aio import DefaultAzureCredential

            from agent_framework import ChatOptions, Message
            from agent_framework.azure import AzureOpenAIChatClient
            from common.config.app_config import config

            client = AzureOpenAIChatClient(
                endpoint=config.AZURE_OPENAI_ENDPOINT,
                deployment_name=config.AZURE_OPENAI_DEPLOYMENT_NAME,
                credential=DefaultAzureCredential(),
            )

            # Build the user payload with session context
            user_text = message.strip()
            if previous_intent:
                user_text = (
                    f"PREVIOUS_INTENT: {previous_intent}\nUSER_MESSAGE: {user_text}"
                )

            messages = [
                Message(role="system", text=_SYSTEM_PROMPT),
                Message(role="user", text=user_text),
            ]
            options = ChatOptions(max_tokens=5, temperature=0.0)

            response = await client.get_response(messages, options=options)
            raw = (response.text or "").strip().lower()

            if "mcp" in raw:
                return IntentResult(
                    intent=Intent.MCP_QUERY,
                    confidence=0.95,
                    reasoning=f"LLM: mcp_query (raw={raw})",
                )
            if "conversational" in raw:
                return IntentResult(
                    intent=Intent.CONVERSATIONAL,
                    confidence=0.95,
                    reasoning=f"LLM: conversational (raw={raw})",
                )
            if "task" in raw:
                return IntentResult(
                    intent=Intent.TASK,
                    confidence=0.95,
                    reasoning=f"LLM: task (raw={raw})",
                )

            # LLM returned unexpected output — use previous_intent if available
            logger.warning(
                "IntentRouter LLM returned unexpected: '%s', falling back", raw
            )

        except Exception as e:
            logger.warning("IntentRouter LLM call failed (%s), falling back", e)

        # ── Fallback: preserve session lane, no keyword heuristics ────
        if previous_intent:
            try:
                kept = Intent(previous_intent)
                logger.info(
                    "IntentRouter fallback: preserving previous_intent '%s'",
                    previous_intent,
                )
                return IntentResult(
                    intent=kept,
                    confidence=0.7,
                    reasoning=f"fallback: preserved previous_intent ({previous_intent})",
                )
            except ValueError:
                pass

        return IntentResult(
            intent=Intent.CONVERSATIONAL,
            confidence=0.5,
            reasoning="fallback: no previous_intent, defaulting to conversational",
        )
