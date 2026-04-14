"""Chat session persistence to Cosmos DB.

Extracted from: microsoft/customer-chatbot-solution-accelerator (cosmos_service.py)
Adapted for MACAE: uses existing config.COSMOSDB_ENDPOINT + DefaultAzureCredential.

Container: chat_sessions (pk=/user_id)
Pattern: sessions with embedded messages (denormalized, same as customer-chatbot).
"""

import datetime
import logging
import uuid
from typing import Any, Dict, List, Optional

from azure.cosmos.aio import CosmosClient, DatabaseProxy
from azure.cosmos import exceptions
from azure.cosmos.aio import ContainerProxy

from common.config.app_config import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic-free models (dict-based to stay lightweight)
# ---------------------------------------------------------------------------

CHAT_CONTAINER_NAME = "chat_sessions"
PARTITION_KEY_PATH = "/user_id"


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ChatCosmosService:
    """CRUD for chat sessions stored in Cosmos DB."""

    def __init__(self) -> None:
        self._client: Optional[CosmosClient] = None
        self._database: Optional[DatabaseProxy] = None
        self._container: Optional[ContainerProxy] = None
        self._initialized = False

    async def initialize(self) -> None:
        """Lazy-init: create client + ensure container exists."""
        if self._initialized:
            return

        endpoint = config.COSMOSDB_ENDPOINT
        credential = config.get_azure_credentials()
        db_name = config.COSMOSDB_DATABASE

        if not endpoint or not db_name:
            logger.warning(
                "ChatCosmosService: COSMOSDB_ENDPOINT or COSMOSDB_DATABASE not set — "
                "chat persistence DISABLED."
            )
            return

        try:
            self._client = CosmosClient(url=endpoint, credential=credential)
            self._database = self._client.get_database_client(db_name)

            # Container must already exist (created via Azure CLI / Bicep).
            # RBAC data-plane credentials cannot create containers.
            self._container = self._database.get_container_client(CHAT_CONTAINER_NAME)

            # Verify the container is reachable with a lightweight read
            await self._container.read()

            self._initialized = True
            logger.info(
                "ChatCosmosService initialized (container=%s)", CHAT_CONTAINER_NAME
            )

        except Exception as e:
            logger.error("ChatCosmosService init failed: %s", e)
            self._container = None
            self._initialized = False
            # Service stays disabled — callers will get empty results

    async def _ensure(self) -> bool:
        """Ensure initialized. Returns False if disabled."""
        if not self._initialized:
            await self.initialize()
        return self._initialized and self._container is not None

    # ── Session CRUD ─────────────────────────────────────────────

    async def create_session(
        self,
        user_id: str,
        session_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new chat session."""
        if not await self._ensure():
            return self._fallback_session(user_id, session_name)

        session_id = _new_id()
        now = _utc_now_iso()

        doc = {
            "id": session_id,
            "user_id": user_id,
            "session_name": session_name or f"Chat {now[:10]}",
            "is_active": True,
            "messages": [],
            "message_count": 0,
            "created_at": now,
            "updated_at": now,
            "last_message_at": None,
        }

        try:
            await self._container.upsert_item(doc)
            logger.info("Created chat session %s for user %s", session_id, user_id)
            return doc
        except Exception as e:
            logger.error("Error creating session: %s", e)
            return self._fallback_session(user_id, session_name)

    async def get_session(
        self,
        session_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get a session by ID."""
        if not await self._ensure():
            return None

        try:
            item = await self._container.read_item(
                item=session_id,
                partition_key=user_id,
            )
            return item
        except exceptions.CosmosResourceNotFoundError:
            return None
        except Exception as e:
            logger.error("Error reading session %s: %s", session_id, e)
            return None

    async def get_sessions_by_user(
        self,
        user_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Get all sessions for a user, most recent first."""
        if not await self._ensure():
            return []

        try:
            query = (
                "SELECT c.id, c.user_id, c.session_name, c.message_count, "
                "c.created_at, c.updated_at, c.last_message_at, c.is_active "
                "FROM c WHERE c.user_id = @user_id "
                "ORDER BY c.updated_at DESC "
                "OFFSET 0 LIMIT @limit"
            )
            params = [
                {"name": "@user_id", "value": user_id},
                {"name": "@limit", "value": limit},
            ]

            items = []
            async for item in self._container.query_items(
                query=query,
                parameters=params,
                partition_key=user_id,
            ):
                items.append(item)
            return items

        except Exception as e:
            logger.error("Error listing sessions for user %s: %s", user_id, e)
            return []

    async def delete_session(
        self,
        session_id: str,
        user_id: str,
    ) -> bool:
        """Delete a chat session."""
        if not await self._ensure():
            return False

        try:
            await self._container.delete_item(
                item=session_id,
                partition_key=user_id,
            )
            logger.info("Deleted chat session %s", session_id)
            return True
        except exceptions.CosmosResourceNotFoundError:
            return False
        except Exception as e:
            logger.error("Error deleting session %s: %s", session_id, e)
            return False

    # ── Messages ─────────────────────────────────────────────────

    async def add_message(
        self,
        session_id: str,
        user_id: str,
        content: str,
        role: str = "user",
        metadata: Optional[Dict] = None,
    ) -> Optional[Dict[str, Any]]:
        """Append a message to session's embedded messages array.

        Pattern: read → append → upsert (same as customer-chatbot).
        """
        if not await self._ensure():
            return None

        try:
            session = await self.get_session(session_id, user_id)
            if not session:
                # Create session with the REQUESTED session_id, not a new UUID
                now = _utc_now_iso()
                session = {
                    "id": session_id,
                    "user_id": user_id,
                    "session_name": f"Chat {now[:10]}",
                    "is_active": True,
                    "messages": [],
                    "message_count": 0,
                    "created_at": now,
                    "updated_at": now,
                    "last_message_at": None,
                }

            message = {
                "id": _new_id(),
                "content": content,
                "role": role,
                "timestamp": _utc_now_iso(),
                "metadata": metadata or {},
            }

            session.setdefault("messages", []).append(message)
            session["message_count"] = len(session["messages"])
            session["last_message_at"] = message["timestamp"]
            session["updated_at"] = message["timestamp"]

            # Update session name from first user message
            if role == "user" and session["message_count"] == 1:
                session["session_name"] = content[:60]

            await self._container.upsert_item(session)

            # Push to AI Search index (fire-and-forget for real-time RAG)
            try:
                import asyncio
                from common.services.search_index_service import (
                    get_search_index_service,
                )

                search_svc = await get_search_index_service()
                asyncio.create_task(
                    search_svc.index_chat_message(
                        message_id=message["id"],
                        session_id=session_id,
                        user_id=user_id,
                        role=role,
                        content=content,
                        intent=(metadata or {}).get("intent", ""),
                        timestamp=message["timestamp"],
                        session_name=session.get("session_name", ""),
                    )
                )
            except Exception as search_err:
                logger.debug("AI Search indexing skipped: %s", search_err)

            return message

        except Exception as e:
            logger.error("Error adding message to session %s: %s", session_id, e)
            return None

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _fallback_session(
        user_id: str,
        session_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return an in-memory-only session when Cosmos is unavailable."""
        now = _utc_now_iso()
        return {
            "id": _new_id(),
            "user_id": user_id,
            "session_name": session_name or f"Chat {now[:10]}",
            "is_active": True,
            "messages": [],
            "message_count": 0,
            "created_at": now,
            "updated_at": now,
            "last_message_at": None,
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[ChatCosmosService] = None


async def get_chat_cosmos_service() -> ChatCosmosService:
    """Get the singleton ChatCosmosService (lazy init)."""
    global _instance
    if _instance is None:
        _instance = ChatCosmosService()
        await _instance.initialize()
    return _instance
