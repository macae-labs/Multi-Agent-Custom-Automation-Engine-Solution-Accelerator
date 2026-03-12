# cosmos_memory_kernel.py

import asyncio
import logging
import uuid
import json
import datetime
from typing import Any, Dict, List, Optional, Type, Tuple, cast
import numpy as np

from azure.cosmos.partition_key import PartitionKey
from semantic_kernel.memory.memory_record import MemoryRecord
from semantic_kernel.memory.memory_store_base import MemoryStoreBase
from semantic_kernel.contents import ChatMessageContent, ChatHistory, AuthorRole

# Import the AppConfig instance
from app_config import config
from models.messages_kernel import BaseDataModel, Plan, Session, Step, AgentMessage
from models.project_profile import ProjectProfile

# Type alias for Cosmos DB query parameters
_CosmosParams = List[Dict[str, Any]]


# Add custom JSON encoder class for datetime objects
class DateTimeEncoder(json.JSONEncoder):
    """Custom JSON encoder for handling datetime objects."""

    def default(self, o: Any) -> Any:
        if isinstance(o, datetime.datetime):
            return o.isoformat()
        return super().default(o)


def _clone_record_without_embedding(record: MemoryRecord) -> MemoryRecord:
    """SK 1.x stores is_reference/external_source_name/key as private attrs.
    Use getattr to access them safely across SK versions."""
    return MemoryRecord(
        is_reference=getattr(record, "_is_reference", False),
        external_source_name=getattr(record, "_external_source_name", None),
        id=record.id,
        description=record.description,
        text=record.text,
        additional_metadata=record.additional_metadata,
        embedding=None,
        key=getattr(record, "_key", None),
    )


class CosmosMemoryContext(MemoryStoreBase):
    """A buffered chat completion context that saves messages and data models to Cosmos DB."""

    MODEL_CLASS_MAPPING = {
        "session": Session,
        "plan": Plan,
        "step": Step,
        "agent_message": AgentMessage,
        "project_profile": ProjectProfile,
        # Messages are handled separately
    }

    def __init__(
        self,
        session_id: str,
        user_id: str,
        cosmos_container: Optional[str] = None,
        cosmos_endpoint: Optional[str] = None,
        cosmos_database: Optional[str] = None,
        buffer_size: int = 100,
        initial_messages: Optional[List[ChatMessageContent]] = None,
    ) -> None:
        self._buffer_size = buffer_size
        self._messages = initial_messages or []

        # Use values from AppConfig instance if not provided
        self._cosmos_container = cosmos_container or config.COSMOSDB_CONTAINER
        self._cosmos_endpoint = cosmos_endpoint or config.COSMOSDB_ENDPOINT
        self._cosmos_database = cosmos_database or config.COSMOSDB_DATABASE

        self._database = None
        self._container = None
        self.session_id = session_id
        self.user_id = user_id
        self._initialized = asyncio.Event()
        # Skip auto-initialize in constructor to avoid requiring a running event loop
        self._initialized.set()

    async def initialize(self):
        """Initialize the memory context using CosmosDB.
        Uses the singleton database client from AppConfig to prevent
        creating multiple CosmosClient instances and causing aiohttp session leaks.
        """
        try:
            if not self._database:
                # Use the singleton database client from config instead of creating new client
                # This prevents aiohttp session leaks from multiple CosmosClient instances
                self._database = config.get_cosmos_database_client()

            # Set up CosmosDB container
            self._container = await self._database.create_container_if_not_exists(
                id=self._cosmos_container,
                partition_key=PartitionKey(path="/session_id"),
            )
        except Exception as e:
            logging.error(
                f"Failed to initialize CosmosDB container: {e}. Continuing without CosmosDB for testing."
            )
            # Do not raise to prevent test failures
            self._container = None

        self._initialized.set()

    # Helper method for awaiting initialization
    async def ensure_initialized(self):
        """Ensure that the container is initialized."""
        if not self._initialized.is_set():
            # If the initialization hasn't been done, do it now
            await self.initialize()

        # If after initialization the container is still None, that means initialization failed
        if self._container is None:
            # Re-attempt initialization once in case the previous attempt failed
            try:
                await self.initialize()
            except Exception as e:
                logging.error(f"Re-initialization attempt failed: {e}")

            # If still not initialized, raise error
            if self._container is None:
                raise RuntimeError(
                    "CosmosDB container is not available. Initialization failed."
                )

    async def add_item(self, item: BaseDataModel) -> None:
        """Add a data model item to Cosmos DB."""
        await self.ensure_initialized()
        assert self._container is not None
        try:
            # Convert the model to a dict
            document = item.model_dump()

            # Handle datetime objects by converting them to ISO format strings
            for key, value in list(document.items()):
                if isinstance(value, datetime.datetime):
                    document[key] = value.isoformat()

            # Now create the item with the serialized datetime values
            await self._container.create_item(body=document)
            logging.info(f"Item added to Cosmos DB - {document['id']}")
        except Exception as e:
            logging.exception(f"Failed to add item to Cosmos DB: {e}")
            raise  # Propagate the error instead of silently failing

    async def update_item(self, item: BaseDataModel) -> None:
        """Update an existing item in Cosmos DB."""
        await self.ensure_initialized()
        assert self._container is not None
        try:
            # Convert the model to a dict
            document = item.model_dump()

            # Handle datetime objects by converting them to ISO format strings
            for key, value in list(document.items()):
                if isinstance(value, datetime.datetime):
                    document[key] = value.isoformat()

            # Now upsert the item with the serialized datetime values
            await self._container.upsert_item(body=document)
        except Exception as e:
            logging.exception(f"Failed to update item in Cosmos DB: {e}")
            raise  # Propagate the error instead of silently failing

    async def get_item_by_id(
        self, item_id: str, partition_key: str, model_class: Type[BaseDataModel]
    ) -> Optional[BaseDataModel]:
        """Retrieve an item by its ID and partition key."""
        await self.ensure_initialized()
        assert self._container is not None
        try:
            item = await self._container.read_item(
                item=item_id, partition_key=partition_key
            )
            return model_class.model_validate(item)
        except Exception as e:
            logging.exception(f"Failed to retrieve item from Cosmos DB: {e}")
            return None

    async def query_items(
        self,
        query: str,
        parameters: List[Dict[str, Any]],
        model_class: Type[BaseDataModel],
    ) -> List[BaseDataModel]:
        """Query items from Cosmos DB and return a list of model instances."""
        await self.ensure_initialized()
        assert self._container is not None

        try:
            items = self._container.query_items(
                query=query, parameters=list(parameters)
            )
            result_list = []
            async for item in items:
                item["ts"] = item["_ts"]
                result_list.append(model_class.model_validate(item))
            return result_list
        except Exception as e:
            logging.exception(f"Failed to query items from Cosmos DB: {e}")
            return []

    async def add_session(self, session: Session) -> None:
        """Add a session to Cosmos DB."""
        await self.add_item(session)

    async def get_session(self, session_id: str) -> Optional[Session]:
        """Retrieve a session by session_id."""
        query = "SELECT * FROM c WHERE c.id=@id AND c.data_type=@data_type"
        parameters = [
            {"name": "@id", "value": session_id},
            {"name": "@data_type", "value": "session"},
        ]
        sessions = await self.query_items(query, parameters, Session)
        return cast(Optional[Session], sessions[0] if sessions else None)

    async def get_all_sessions(self) -> List[Session]:
        """Retrieve all sessions."""
        query = "SELECT * FROM c WHERE c.data_type=@data_type"
        parameters = [
            {"name": "@data_type", "value": "session"},
        ]
        sessions = await self.query_items(query, parameters, Session)
        return cast(List[Session], sessions)

    async def add_plan(self, plan: Plan) -> None:
        """Add a plan to Cosmos DB."""
        await self.add_item(plan)

    async def update_plan(self, plan: Plan) -> None:
        """Update an existing plan in Cosmos DB."""
        await self.update_item(plan)

    async def get_plan_by_session(self, session_id: str) -> Optional[Plan]:
        """Retrieve a plan associated with a session."""
        query = "SELECT * FROM c WHERE c.session_id=@session_id AND c.user_id=@user_id AND c.data_type=@data_type"
        parameters = [
            {"name": "@session_id", "value": session_id},
            {"name": "@data_type", "value": "plan"},
            {"name": "@user_id", "value": self.user_id},
        ]
        plans = await self.query_items(query, parameters, Plan)
        return cast(Optional[Plan], plans[0] if plans else None)

    async def get_plan_by_plan_id(self, plan_id: str) -> Optional[Plan]:
        """Retrieve a plan associated with a session."""
        query = "SELECT * FROM c WHERE c.id=@id AND c.user_id=@user_id AND c.data_type=@data_type"
        parameters = [
            {"name": "@id", "value": plan_id},
            {"name": "@data_type", "value": "plan"},
            {"name": "@user_id", "value": self.user_id},
        ]
        plans = await self.query_items(query, parameters, Plan)
        return cast(Optional[Plan], plans[0] if plans else None)

    async def get_thread_by_session(self, session_id: str) -> Optional[Plan]:
        """Retrieve a thread (stored as Plan shape) associated with a session."""
        query = "SELECT * FROM c WHERE c.session_id=@session_id AND c.user_id=@user_id AND c.data_type=@data_type"
        parameters = [
            {"name": "@session_id", "value": session_id},
            {"name": "@data_type", "value": "thread"},
            {"name": "@user_id", "value": self.user_id},
        ]
        threads = await self.query_items(query, parameters, Plan)
        return cast(Optional[Plan], threads[0] if threads else None)

    async def get_plan(self, plan_id: str) -> Optional[Plan]:
        """Retrieve a plan by its ID.

        Args:
            plan_id: The ID of the plan to retrieve

        Returns:
            The Plan object or None if not found
        """
        # Use the session_id as the partition key since that's how we're partitioning our data
        return cast(
            Optional[Plan],
            await self.get_item_by_id(
                plan_id, partition_key=self.session_id, model_class=Plan
            ),
        )

    async def get_all_plans(self) -> List[Plan]:
        """Retrieve all plans."""
        query = "SELECT * FROM c WHERE c.user_id=@user_id AND c.data_type=@data_type ORDER BY c._ts DESC"
        parameters = [
            {"name": "@data_type", "value": "plan"},
            {"name": "@user_id", "value": self.user_id},
        ]
        plans = await self.query_items(query, parameters, Plan)
        return cast(List[Plan], plans)

    async def add_step(self, step: Step) -> None:
        """Add a step to Cosmos DB."""
        await self.add_item(step)

    async def update_step(self, step: Step) -> None:
        """Update an existing step in Cosmos DB."""
        await self.update_item(step)

    async def get_steps_by_plan(self, plan_id: str) -> List[Step]:
        """Retrieve all steps associated with a plan."""
        query = "SELECT * FROM c WHERE c.plan_id=@plan_id AND c.user_id=@user_id AND c.data_type=@data_type"
        parameters = [
            {"name": "@plan_id", "value": plan_id},
            {"name": "@data_type", "value": "step"},
            {"name": "@user_id", "value": self.user_id},
        ]
        steps = await self.query_items(query, parameters, Step)
        return cast(List[Step], steps)

    async def get_steps_for_plan(
        self, plan_id: str, session_id: Optional[str] = None
    ) -> List[Step]:
        """Retrieve all steps associated with a plan.

        Args:
            plan_id: The ID of the plan to retrieve steps for
            session_id: Optional session ID if known

        Returns:
            List of Step objects
        """
        return await self.get_steps_by_plan(plan_id)

    async def get_step(self, step_id: str, session_id: str) -> Optional[Step]:
        return cast(
            Optional[Step],
            await self.get_item_by_id(
                step_id, partition_key=session_id, model_class=Step
            ),
        )

    async def add_agent_message(self, message: AgentMessage) -> None:
        """Add an agent message to Cosmos DB.

        Args:
            message: The AgentMessage to add
        """
        await self.add_item(message)

    async def get_agent_messages_by_session(
        self, session_id: str
    ) -> List[AgentMessage]:
        """Retrieve agent messages for a specific session.

        Args:
            session_id: The session ID to get messages for

        Returns:
            List of AgentMessage objects
        """
        query = "SELECT * FROM c WHERE c.session_id=@session_id AND c.data_type=@data_type ORDER BY c._ts ASC"
        parameters = [
            {"name": "@session_id", "value": session_id},
            {"name": "@data_type", "value": "agent_message"},
        ]
        messages = await self.query_items(query, parameters, AgentMessage)
        return cast(List[AgentMessage], messages)

    async def add_message(self, message: ChatMessageContent) -> None:
        """Add a message to the memory and save to Cosmos DB."""
        await self.ensure_initialized()
        assert self._container is not None

        try:
            self._messages.append(message)
            while len(self._messages) > self._buffer_size:
                self._messages.pop(0)

            message_dict = {
                "id": str(uuid.uuid4()),
                "session_id": self.session_id,
                "user_id": self.user_id,
                "data_type": "message",
                "content": {
                    "role": message.role.value,
                    "content": message.content,
                    "metadata": message.metadata,
                },
                "source": message.metadata.get("source", ""),
            }
            await self._container.create_item(body=message_dict)
        except Exception as e:
            logging.exception(f"Failed to add message to Cosmos DB: {e}")
            raise  # Propagate the error instead of silently failing

    async def get_messages(self) -> List[ChatMessageContent]:
        """Get recent messages for the session."""
        await self.ensure_initialized()
        assert self._container is not None

        try:
            query = """
                SELECT * FROM c
                WHERE c.session_id=@session_id AND c.data_type=@data_type
                ORDER BY c._ts ASC
                OFFSET 0 LIMIT @limit
            """
            parameters = [
                {"name": "@session_id", "value": self.session_id},
                {"name": "@data_type", "value": "message"},
                {"name": "@limit", "value": self._buffer_size},
            ]
            items = self._container.query_items(
                query=query,
                parameters=list(parameters),
            )
            messages = []
            async for item in items:
                content = item.get("content", {})
                role = content.get("role", "user")
                chat_role = AuthorRole.ASSISTANT
                if role == "user":
                    chat_role = AuthorRole.USER
                elif role == "system":
                    chat_role = AuthorRole.SYSTEM
                elif role == "tool":  # Equivalent to FunctionExecutionResultMessage
                    chat_role = AuthorRole.TOOL

                message = ChatMessageContent(
                    role=chat_role,
                    content=content.get("content", ""),
                    metadata=content.get("metadata", {}),
                )
                messages.append(message)
            return messages
        except Exception as e:
            logging.exception(f"Failed to load messages from Cosmos DB: {e}")
            return []

    def get_chat_history(self) -> ChatHistory:
        """Convert the buffered messages to a ChatHistory object."""
        history = ChatHistory()
        for message in self._messages:
            history.add_message(message)
        return history

    async def save_chat_history(self, history: ChatHistory) -> None:
        """Save a ChatHistory object to the store."""
        for message in history.messages:
            await self.add_message(message)

    async def get_data_by_type(self, data_type: str) -> List[BaseDataModel]:
        """Query the Cosmos DB for documents with the matching data_type, session_id and user_id."""
        await self.ensure_initialized()
        if self._container is None:
            return []

        model_class = self.MODEL_CLASS_MAPPING.get(data_type, BaseDataModel)
        try:
            query = "SELECT * FROM c WHERE c.session_id=@session_id AND c.user_id=@user_id AND c.data_type=@data_type ORDER BY c._ts ASC"
            parameters = [
                {"name": "@session_id", "value": self.session_id},
                {"name": "@data_type", "value": data_type},
                {"name": "@user_id", "value": self.user_id},
            ]
            return await self.query_items(query, parameters, model_class)
        except Exception as e:
            logging.exception(f"Failed to query data by type from Cosmos DB: {e}")
            return []

    async def get_data_by_type_and_session_id(
        self, data_type: str, session_id: str
    ) -> List[BaseDataModel]:
        """Query the Cosmos DB for documents with the matching data_type, session_id and user_id."""
        await self.ensure_initialized()
        if self._container is None:
            return []

        model_class = self.MODEL_CLASS_MAPPING.get(data_type, BaseDataModel)
        try:
            query = "SELECT * FROM c WHERE c.session_id=@session_id AND c.user_id=@user_id AND c.data_type=@data_type ORDER BY c._ts ASC"
            parameters = [
                {"name": "@session_id", "value": session_id},
                {"name": "@data_type", "value": data_type},
                {"name": "@user_id", "value": self.user_id},
            ]
            return await self.query_items(query, parameters, model_class)
        except Exception as e:
            logging.exception(f"Failed to query data by type from Cosmos DB: {e}")
            return []

    async def get_latest_data_by_type_for_user(
        self, data_type: str
    ) -> Optional[BaseDataModel]:
        """Return latest item by data_type for current user across all sessions."""
        await self.ensure_initialized()
        if self._container is None:
            return None

        model_class = self.MODEL_CLASS_MAPPING.get(data_type, BaseDataModel)
        try:
            query = (
                "SELECT TOP 1 * FROM c "
                "WHERE c.user_id=@user_id AND c.data_type=@data_type "
                "ORDER BY c._ts DESC"
            )
            parameters = [
                {"name": "@user_id", "value": self.user_id},
                {"name": "@data_type", "value": data_type},
            ]
            items = await self.query_items(query, parameters, model_class)
            return items[0] if items else None
        except Exception as e:
            logging.exception(
                f"Failed to query latest data by type from Cosmos DB: {e}"
            )
            return None

    async def delete_item(self, item_id: str, partition_key: str) -> None:
        """Delete an item from Cosmos DB."""
        await self.ensure_initialized()
        assert self._container is not None
        try:
            await self._container.delete_item(item=item_id, partition_key=partition_key)
        except Exception as e:
            logging.exception(f"Failed to delete item from Cosmos DB: {e}")

    async def delete_items_by_query(
        self, query: str, parameters: List[Dict[str, Any]]
    ) -> None:
        """Delete items matching the query."""
        await self.ensure_initialized()
        assert self._container is not None
        try:
            items = self._container.query_items(
                query=query, parameters=list(parameters)
            )
            async for item in items:
                item_id = item["id"]
                partition_key = item.get("session_id", None)
                await self._container.delete_item(
                    item=item_id, partition_key=partition_key
                )
        except Exception as e:
            logging.exception(f"Failed to delete items from Cosmos DB: {e}")

    async def delete_all_messages(self, data_type) -> None:
        """Delete all messages of a specific type from Cosmos DB."""
        query = "SELECT c.id, c.session_id FROM c WHERE c.data_type=@data_type AND c.user_id=@user_id"
        parameters = [
            {"name": "@data_type", "value": data_type},
            {"name": "@user_id", "value": self.user_id},
        ]
        await self.delete_items_by_query(query, parameters)

    async def delete_all_items(self, data_type) -> None:
        """Delete all items of a specific type from Cosmos DB."""
        await self.delete_all_messages(data_type)

    async def get_all_messages(self) -> List[Dict[str, Any]]:
        """Retrieve all messages from Cosmos DB."""
        await self.ensure_initialized()
        if self._container is None:
            return []
        assert self._container is not None

        try:
            messages_list = []
            query = "SELECT * FROM c WHERE c.user_id=@user_id OFFSET 0 LIMIT @limit"
            parameters = [
                {"name": "@user_id", "value": self.user_id},
                {"name": "@limit", "value": 100},
            ]
            items = self._container.query_items(
                query=query, parameters=list(parameters)
            )
            async for item in items:
                messages_list.append(item)
            return messages_list
        except Exception as e:
            logging.exception(f"Failed to get messages from Cosmos DB: {e}")
            return []

    async def get_all_items(self) -> List[Dict[str, Any]]:
        """Retrieve all items from Cosmos DB."""
        return await self.get_all_messages()

    async def close(self) -> None:  # type: ignore[override]
        """Close the Cosmos DB client."""
        return

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def __del__(self) -> None:
        pass  # async close handled via __aexit__

    async def create_collection(self, collection_name: str) -> None:
        """Create a new collection. For CosmosDB, we don't need to create new collections
        as everything is stored in the same container with type identifiers."""
        await self.ensure_initialized()
        pass

    async def get_collections(self) -> List[str]:
        """Get all collections."""
        await self.ensure_initialized()

        assert self._container is not None
        try:
            query = """
                SELECT DISTINCT c.collection
                FROM c
                WHERE c.data_type = 'memory' AND c.session_id = @session_id
            """
            parameters: List[Dict[str, Any]] = [
                {"name": "@session_id", "value": self.session_id}
            ]
            items = self._container.query_items(
                query=query, parameters=list(parameters)
            )
            collections = []
            async for item in items:
                if "collection" in item and item["collection"] not in collections:
                    collections.append(item["collection"])
            return collections
        except Exception as e:
            logging.exception(f"Failed to get collections from Cosmos DB: {e}")
            return []

    async def does_collection_exist(self, collection_name: str) -> bool:
        """Check if a collection exists."""
        collections = await self.get_collections()
        return collection_name in collections

    async def delete_collection(self, collection_name: str) -> None:
        """Delete a collection."""
        await self.ensure_initialized()
        assert self._container is not None
        try:
            query = """
                SELECT c.id, c.session_id
                FROM c
                WHERE c.collection = @collection AND c.data_type = 'memory' AND c.session_id = @session_id
            """
            parameters: _CosmosParams = [
                {"name": "@collection", "value": collection_name},
                {"name": "@session_id", "value": self.session_id},
            ]

            items = self._container.query_items(
                query=query, parameters=list(parameters)
            )
            async for item in items:
                await self._container.delete_item(
                    item=item["id"], partition_key=item["session_id"]
                )
        except Exception as e:
            logging.exception(f"Failed to delete collection from Cosmos DB: {e}")

    async def upsert_memory_record(self, collection: str, record: MemoryRecord) -> str:
        """Store a memory record.
        Note: SK 1.32.2 changed MemoryRecord to use private attributes (_key, _external_source_name, _is_reference).
        We use getattr to access these safely and store them in our Cosmos document for later retrieval.
        """
        # Access attributes safely - SK 1.32.2 uses private attrs, but we need the values for storage
        # Use getattr with fallback to handle both old and new SK versions
        key = getattr(record, "key", None) or getattr(record, "_key", None) or ""
        external_source_name = getattr(record, "external_source_name", None) or getattr(
            record, "_external_source_name", None
        )
        is_reference = getattr(record, "is_reference", None) or getattr(
            record, "_is_reference", False
        )
        memory_dict = {
            "id": record.id or str(uuid.uuid4()),
            "session_id": self.session_id,
            "user_id": self.user_id,
            "data_type": "memory",
            "collection": collection,
            "text": record.text,
            "description": record.description,
            "external_source_name": external_source_name,
            "additional_metadata": record.additional_metadata,
            "embedding": (
                record.embedding.tolist() if record.embedding is not None else None
            ),
            "key": key,
            "is_reference": is_reference,  # Store for correct reconstruction
        }

        assert self._container is not None
        await self._container.upsert_item(body=memory_dict)
        return memory_dict["id"]

    async def get_memory_record(
        self, collection: str, key: str, with_embedding: bool = False
    ) -> Optional[MemoryRecord]:
        """Retrieve a memory record."""
        query = """
            SELECT * FROM c
            WHERE c.collection=@collection AND c.key=@key AND c.session_id=@session_id AND c.data_type=@data_type
        """
        parameters: _CosmosParams = [
            {"name": "@collection", "value": collection},
            {"name": "@key", "value": key},
            {"name": "@session_id", "value": self.session_id},
            {"name": "@data_type", "value": "memory"},
        ]

        assert self._container is not None
        items = self._container.query_items(query=query, parameters=list(parameters))
        async for item in items:
            is_ref = item.get("is_reference", False)
            return MemoryRecord(
                is_reference=is_ref,
                external_source_name=item.get("external_source_name"),
                id=item["id"],
                description=item.get("description"),
                text=item.get("text"),
                additional_metadata=item.get("additional_metadata"),
                embedding=(
                    np.array(item["embedding"])
                    if with_embedding and item.get("embedding")
                    else None
                ),
                key=item.get("key"),
            )
        return None

    async def remove_memory_record(self, collection: str, key: str) -> None:
        """Remove a memory record."""
        query = """
            SELECT c.id FROM c
            WHERE c.collection=@collection AND c.key=@key AND c.session_id=@session_id AND c.data_type=@data_type
        """
        parameters: _CosmosParams = [
            {"name": "@collection", "value": collection},
            {"name": "@key", "value": key},
            {"name": "@session_id", "value": self.session_id},
            {"name": "@data_type", "value": "memory"},
        ]

        assert self._container is not None
        items = self._container.query_items(query=query, parameters=list(parameters))
        async for item in items:
            await self._container.delete_item(
                item=item["id"], partition_key=self.session_id
            )

    async def upsert_async(self, collection_name: str, record: Dict[str, Any]) -> str:
        """Helper method to insert documents directly."""
        await self.ensure_initialized()
        assert self._container is not None
        try:
            if "session_id" not in record:
                record["session_id"] = self.session_id

            if "id" not in record:
                record["id"] = str(uuid.uuid4())

            await self._container.upsert_item(body=record)
            return record["id"]
        except Exception as e:
            logging.exception(f"Failed to upsert item to Cosmos DB: {e}")
            return ""

    async def get_memory_records(
        self, collection: str, limit: int = 1000, with_embeddings: bool = False
    ) -> List[MemoryRecord]:
        """Get memory records from a collection."""
        await self.ensure_initialized()

        try:
            query = """
                SELECT *
                FROM c
                WHERE c.collection = @collection
                AND c.data_type = 'memory'
                AND c.session_id = @session_id
                ORDER BY c._ts DESC
                OFFSET 0 LIMIT @limit
            """
            parameters = [
                {"name": "@collection", "value": collection},
                {"name": "@session_id", "value": self.session_id},
                {"name": "@limit", "value": limit},
            ]

            assert self._container is not None
            items = self._container.query_items(
                query=query, parameters=list(parameters)
            )
            records: List[MemoryRecord] = []
            async for item in items:
                embedding = None
                if with_embeddings and "embedding" in item and item["embedding"]:
                    embedding = np.array(item["embedding"])

                # SK 1.32.2 MemoryRecord constructor requires is_reference as first positional arg
                # Read is_reference from document, default to False for local records
                is_ref = item.get("is_reference", False)
                record = MemoryRecord(
                    is_reference=is_ref,
                    external_source_name=item.get("external_source_name"),
                    id=item["id"],
                    description=item.get("description"),
                    text=item.get("text"),
                    additional_metadata=item.get("additional_metadata"),
                    embedding=embedding,
                    key=item.get("key"),
                )
                records.append(record)
            return records
        except Exception as e:
            logging.exception(f"Failed to get memory records from Cosmos DB: {e}")
            return []

    async def upsert(self, collection_name: str, record: MemoryRecord) -> str:
        """Upsert a memory record into the store."""
        return await self.upsert_memory_record(collection_name, record)

    async def upsert_batch(
        self, collection_name: str, records: List[MemoryRecord]
    ) -> List[str]:
        """Upsert a batch of memory records into the store."""
        result_ids = []
        for record in records:
            record_id = await self.upsert_memory_record(collection_name, record)
            result_ids.append(record_id)
        return result_ids

    async def get(
        self, collection_name: str, key: str, with_embedding: bool = False
    ) -> MemoryRecord:
        """Get a memory record from the store."""
        record = await self.get_memory_record(collection_name, key, with_embedding)
        if record is None:
            raise KeyError(
                f"Memory record not found: collection={collection_name}, key={key}"
            )
        return record

    async def get_batch(
        self, collection_name: str, keys: List[str], with_embeddings: bool = False
    ) -> List[MemoryRecord]:
        """Get a batch of memory records from the store."""
        results = []
        for key in keys:
            record = await self.get_memory_record(collection_name, key, with_embeddings)
            if record:
                results.append(record)
        return results

    async def remove(self, collection_name: str, key: str) -> None:
        """Remove a memory record from the store."""
        await self.remove_memory_record(collection_name, key)

    async def remove_batch(self, collection_name: str, keys: List[str]) -> None:
        """Remove a batch of memory records from the store."""
        for key in keys:
            await self.remove_memory_record(collection_name, key)

    async def get_nearest_match(
        self,
        collection_name: str,
        embedding: np.ndarray,
        min_relevance_score: float = 0.0,
        with_embedding: bool = False,
    ) -> Tuple[MemoryRecord, float]:
        """Get the nearest match to the given embedding."""
        matches = await self.get_nearest_matches(
            collection_name, embedding, 1, min_relevance_score, with_embedding
        )
        if not matches:
            # Return a dummy record to satisfy the interface - callers should check score
            dummy = MemoryRecord(
                is_reference=False,
                external_source_name=None,
                id="",
                description=None,
                text=None,
                additional_metadata=None,
                embedding=None,
            )
            return (dummy, 0.0)
        return matches[0]

    async def get_nearest_matches(
        self,
        collection_name: str,
        embedding: np.ndarray,
        limit: int = 1,
        min_relevance_score: float = 0.0,
        with_embeddings: bool = False,
    ) -> List[Tuple[MemoryRecord, float]]:
        """Get the nearest matches to the given embedding."""
        await self.ensure_initialized()

        try:
            records = await self.get_memory_records(
                collection_name, limit=100, with_embeddings=True
            )

            results = []
            for record in records:
                if record.embedding is not None:
                    similarity = np.dot(embedding, record.embedding) / (
                        np.linalg.norm(embedding) * np.linalg.norm(record.embedding)
                    )

                    if similarity >= min_relevance_score:
                        if not with_embeddings:
                            record = _clone_record_without_embedding(record)
                        results.append((record, float(similarity)))

            results.sort(key=lambda x: x[1], reverse=True)
            return results[:limit]
        except Exception as e:
            logging.exception(f"Failed to get nearest matches from Cosmos DB: {e}")
            return []
