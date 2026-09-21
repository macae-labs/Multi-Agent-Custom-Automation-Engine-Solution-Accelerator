"""
Configuration settings for the Magentic Employee Onboarding system.
Handles Azure OpenAI, MCP, and environment setup (agent_framework version).
"""

import asyncio
import json
import logging
import time
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
        self.max_rounds: int = 8  # Maximum replanning rounds

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
    """Connection manager for WebSocket connections.

    Includes a short-lived pending buffer to handle the race condition where
    orchestration emits status updates before the frontend WebSocket connects.
    Messages are buffered per user_id and flushed on add_connection.
    """

    # Buffer config — kept conservative to avoid memory bloat
    PENDING_TTL_SECONDS = 60  # drop messages older than this
    PENDING_MAX_PER_USER = 100  # cap per-user buffer to prevent runaway

    def __init__(self):
        self.connections: Dict[str, WebSocket] = {}
        self.user_to_process: Dict[str, str] = {}
        # Race-condition buffer: messages emitted while WS isn't connected yet.
        # Schema: user_id -> list[(timestamp_float, message, message_type)]
        self.pending_messages: Dict[str, list] = {}

    def _prune_pending(self, user_id: str) -> None:
        """Drop expired buffered messages for a single user (TTL-based)."""
        bucket = self.pending_messages.get(user_id)
        if not bucket:
            return
        now = time.time()
        fresh = [
            (ts, m, mt) for ts, m, mt in bucket if now - ts < self.PENDING_TTL_SECONDS
        ]
        if fresh:
            self.pending_messages[user_id] = fresh
        else:
            self.pending_messages.pop(user_id, None)

    def add_connection(
        self, process_id: str, connection: WebSocket, user_id: Optional[str] = None
    ):
        """Add or replace a connection for a process/user."""
        if process_id in self.connections:
            try:
                asyncio.create_task(self.connections[process_id].close())
            except Exception as e:
                logger.error(
                    "Error closing existing connection for process %s: %s",
                    process_id,
                    e,
                )

        self.connections[process_id] = connection

        if user_id:
            user_id = str(user_id)
            old_process_id = self.user_to_process.get(user_id)
            if old_process_id and old_process_id != process_id:
                old_conn = self.connections.get(old_process_id)
                if old_conn:
                    try:
                        asyncio.create_task(old_conn.close())
                        del self.connections[old_process_id]
                        logger.info(
                            "Closed old connection %s for user %s",
                            old_process_id,
                            user_id,
                        )
                    except Exception as e:
                        logger.error(
                            "Error closing old connection for user %s: %s", user_id, e
                        )

            self.user_to_process[user_id] = process_id
            logger.info(
                "WebSocket connection added for process: %s (user: %s)",
                process_id,
                user_id,
            )

            # Flush any pending messages buffered before WS connected
            # (race-condition fix: orchestration emits before frontend connects)
            self._prune_pending(user_id)
            pending = self.pending_messages.pop(user_id, [])
            if pending:
                logger.info(
                    "Flushing %d pending message(s) for user %s on WS connect",
                    len(pending),
                    user_id,
                )
                for _ts, msg, mtype in pending:
                    asyncio.create_task(
                        self.send_status_update_async(msg, user_id, mtype)
                    )
        else:
            logger.info("WebSocket connection added for process: %s", process_id)

    def remove_connection(self, process_id: str):
        """Remove a connection and associated user mapping."""
        process_id = str(process_id)
        self.connections.pop(process_id, None)
        for user_id, mapped in list(self.user_to_process.items()):
            if mapped == process_id:
                del self.user_to_process[user_id]
                logger.debug("Removed user mapping: %s -> %s", user_id, process_id)
                break

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
    ):
        """Send a status update to a user via its mapped process connection."""
        if not user_id:
            logger.warning("No user_id provided for WebSocket message")
            return

        process_id = self.user_to_process.get(user_id)
        if not process_id:
            # WS not connected yet — buffer instead of dropping.
            # Will be flushed on next add_connection for this user_id.
            self._prune_pending(user_id)
            bucket = self.pending_messages.setdefault(user_id, [])
            if len(bucket) >= self.PENDING_MAX_PER_USER:
                bucket.pop(0)  # drop oldest, keep window size
            bucket.append((time.time(), message, message_type))
            logger.debug(
                "Buffered WS message for user %s (no active WS yet, %d pending)",
                user_id,
                len(bucket),
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
        connection = self.get_connection(process_id)
        if connection:
            try:
                await connection.send_text(json.dumps(payload, default=str))
                logger.debug(
                    "Message sent to user %s via process %s", user_id, process_id
                )
            except Exception as e:
                logger.error("Failed to send message to user %s: %s", user_id, e)
                self.remove_connection(process_id)
        else:
            logger.warning(
                "No connection found for process ID: %s (user: %s)", process_id, user_id
            )
            self.user_to_process.pop(user_id, None)

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
