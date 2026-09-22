"""
Configuration settings for the Magentic Employee Onboarding system.
Handles Azure OpenAI, MCP, and environment setup (agent_framework version).
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from agent_framework import ChatOptions
from agent_framework.azure import AzureOpenAIChatClient
from fastapi import WebSocket

from common.config.app_config import config
from common.models.messages_af import TeamConfiguration
from v4.models.messages import WebsocketMessageType

logger = logging.getLogger(__name__)


class AzureConfig:
    """Azure OpenAI and authentication configuration (agent_framework)."""

    def __init__(self):
        self.endpoint = config.AZURE_OPENAI_ENDPOINT
        self.reasoning_model = config.REASONING_MODEL_NAME
        self.standard_model = config.AZURE_OPENAI_DEPLOYMENT_NAME
        # self.bing_connection_name = config.AZURE_BING_CONNECTION_NAME

        # Acquire credential (assumes app_config wrapper returns a DefaultAzureCredential or similar)
        self.credential = config.get_azure_credentials()

    def ad_token_provider(self) -> str:
        """Return a bearer token string for Azure Cognitive Services scope."""
        token = self.credential.get_token(config.AZURE_COGNITIVE_SERVICES)
        return token.token

    async def create_chat_completion_service(
        self, use_reasoning_model: bool = False
    ) -> AzureOpenAIChatClient:
        """
        Create an AzureOpenAIChatClient (agent_framework) for the selected model.
        Matches former AzureChatCompletion usage.
        """
        model_name = (
            self.reasoning_model if use_reasoning_model else self.standard_model
        )
        return AzureOpenAIChatClient(
            endpoint=self.endpoint,
            deployment_name=model_name,
            credential=self.ad_token_provider,  # function returning token string
        )

    def create_execution_settings(self) -> ChatOptions:
        """
        Create ChatOptions analogous to previous OpenAIChatPromptExecutionSettings.
        """
        return ChatOptions(
            max_tokens=4000,
            temperature=0.3,
        )


class MCPConfig:
    """MCP server configuration."""

    def __init__(self):
        self.url = config.MCP_SERVER_ENDPOINT
        self.name = config.MCP_SERVER_NAME
        self.description = config.MCP_SERVER_DESCRIPTION
        logger.info(f"🔧 MCP Config initialized - URL: {self.url}, Name: {self.name}")

    def get_headers(self, token: str):
        """Get MCP headers with authentication token."""
        headers = (
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            if token
            else {}
        )
        logger.debug(f"📋 MCP Headers created: {headers}")
        return headers


class OrchestrationConfig:
    """Configuration for orchestration settings (agent_framework workflow storage)."""

    def __init__(self):
        # Previously Dict[str, MagenticOrchestration]; now generic workflow objects from MagenticBuilder.build()
        self.orchestrations: Dict[str, Any] = {}  # user_id -> workflow instance
        self.managers: Dict[
            str, Any
        ] = {}  # user_id -> HumanApprovalMagenticManager (same lifecycle as its workflow)
        self.agent_wrappers: Dict[
            str, List[Any]
        ] = {}  # user_id -> list of lifecycle-managed agent wrappers (for proper close)
        self.sockets: Dict[str, WebSocket] = {}  # user_id -> WebSocket
        self.max_rounds: int = 20  # Maximum replanning rounds

        # No in-process waits for humans. Plan review and clarification are
        # native request_info events: the workflow goes idle, the pending
        # request lives in the checkpoint, ``Plan.waiting_for`` points at it and
        # the answer resumes the workflow (OrchestrationManager.resume_orchestration).

        # Default timeout (seconds) for machine waiting operations
        self.default_timeout: float = 1800.0

        # Sessions with an orchestration run currently in flight (created /
        # awaiting approval / executing). Makes resume_plan idempotent: a plan
        # just created by process_request is not re-triggered into a duplicate run.
        self.active_runs: set[str] = set()

    def mark_run_active(self, session_id: str) -> None:
        """Mark a session as having an orchestration run in flight."""
        if session_id:
            self.active_runs.add(session_id)

    def clear_run_active(self, session_id: str) -> None:
        """Clear the in-flight flag when a run finishes or fails."""
        self.active_runs.discard(session_id)

    def is_run_active(self, session_id: str) -> bool:
        """True if an orchestration run is already in flight for this session."""
        return bool(session_id) and session_id in self.active_runs

    def get_current_orchestration(self, user_id: str) -> Any:
        """Get existing orchestration workflow instance for user_id."""
        return self.orchestrations.get(user_id, None)


class ConnectionConfig:
    """WebSocket connections, addressed by plan (``process_id``).

    Every message names the plan it belongs to; nothing is routed by user and
    nothing is buffered in memory. What a plan waits from the human lives in
    its ``waiting_for`` (Cosmos) and the socket endpoint re-sends it when that
    plan's socket connects; agent output and the final result are persisted
    before any signal. A message for a plan without a socket is dropped here,
    never queued for "the user's next socket": that queue and the user→process
    map delivered another plan's approval request to a freshly opened plan
    page (2026-09-22: 988b2933 flushed onto 7804a8f4's socket; a1e2fd6f onto
    6b204c5e's, whose approve then answered 404).
    """

    def __init__(self):
        self.connections: Dict[str, WebSocket] = {}

    def add_connection(
        self, process_id: str, connection: WebSocket, user_id: Optional[str] = None
    ):
        """Register the socket of a plan, replacing a previous one for the same plan."""
        process_id = str(process_id)
        previous = self.connections.get(process_id)
        if previous is not None and previous is not connection:
            try:
                asyncio.create_task(previous.close())
            except Exception as e:
                logger.error(
                    "Error closing existing connection for process %s: %s",
                    process_id,
                    e,
                )
        self.connections[process_id] = connection
        logger.info(
            "WebSocket connection added for process: %s (user: %s)",
            process_id,
            user_id,
        )

    def remove_connection(self, process_id: str):
        """Forget the socket of a plan."""
        self.connections.pop(str(process_id), None)

    def get_connection(self, process_id: str):
        """Fetch a connection by process_id."""
        return self.connections.get(process_id)

    async def close_connection(self, process_id: str):
        """Close and remove a connection by process_id."""
        connection = self.get_connection(process_id)
        if connection:
            try:
                await connection.close()
                logger.info("Connection closed for process ID: %s", process_id)
            except Exception as e:
                logger.error("Error closing connection for %s: %s", process_id, e)
        else:
            logger.warning("No connection found for process ID: %s", process_id)

        self.remove_connection(process_id)
        logger.info("Connection removed for process ID: %s", process_id)

    async def send_status_update_async(
        self,
        message: Any,
        user_id: str,
        message_type: WebsocketMessageType = WebsocketMessageType.SYSTEM_MESSAGE,
        *,
        process_id: Optional[str] = None,
    ):
        """Send a message to the socket of the plan it belongs to.

        ``process_id`` is the plan's identity. A message without it has no
        destination and is not delivered anywhere, never to "the user's latest
        socket". Without a socket for that plan the message is dropped: what
        matters is durable and the plan page reloads it."""
        if not process_id:
            logger.warning(
                "WS message %s without process_id (user %s): not delivered",
                message_type,
                user_id,
            )
            return
        process_id = str(process_id)
        connection = self.get_connection(process_id)
        if connection is None:
            logger.debug(
                "No socket for process %s (user %s): %s not delivered live",
                process_id,
                user_id,
                message_type,
            )
            return

        try:
            if hasattr(message, "to_dict"):
                message_data = message.to_dict()
            elif hasattr(message, "data") and hasattr(message, "type"):
                message_data = message.data
            elif isinstance(message, dict):
                message_data = message
            else:
                message_data = str(message)
        except Exception as e:
            logger.error("Error processing message data: %s", e)
            message_data = str(message)

        payload = {"type": message_type, "data": message_data}
        try:
            await connection.send_text(json.dumps(payload, default=str))
            logger.debug("Message sent to process %s (user %s)", process_id, user_id)
        except Exception as e:
            logger.error("Failed to send message to process %s: %s", process_id, e)
            self.remove_connection(process_id)

    def send_status_update(self, message: str, process_id: str):
        """Sync helper to send a message by process_id."""
        process_id = str(process_id)
        connection = self.get_connection(process_id)
        if connection:
            try:
                asyncio.create_task(connection.send_text(message))
            except Exception as e:
                logger.error("Failed to send message to process %s: %s", process_id, e)
        else:
            logger.warning("No connection found for process ID: %s", process_id)


class TeamConfig:
    """Team configuration for agents."""

    def __init__(self):
        self.teams: Dict[str, TeamConfiguration] = {}

    def set_current_team(self, user_id: str, team_configuration: TeamConfiguration):
        """Store current team configuration for user."""
        self.teams[user_id] = team_configuration

    def get_current_team(self, user_id: str) -> Optional[TeamConfiguration]:
        """Retrieve current team configuration for user."""
        return self.teams.get(user_id, None)


# Global config instances (names unchanged)
azure_config = AzureConfig()
mcp_config = MCPConfig()
orchestration_config = OrchestrationConfig()
connection_config = ConnectionConfig()
team_config = TeamConfig()
