#!/usr/bin/env python3
"""
Setup AI Search pipeline: vector + semantic search for MACAE.

Usage:
    cd src/backend && .venv/bin/python ../../scripts/setup_search_pipeline.py

Prereqs (already done):
    ✅ text-embedding-3-small deployed in Azure OpenAI
    ✅ AI Search service running (basic tier, semanticSearch: "free")
    ✅ Cosmos DB with chat_sessions container

Steps executed:
    1. Upgrade 8 existing indices → add content_vector + vectorSearch + semantic config
    2. Create chat-history-index for message-level RAG
    3. Backfill embeddings for existing 27 documents
    4. Backfill all chat messages from Cosmos DB → chat-history-index
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import aiohttp
from azure.identity.aio import DefaultAzureCredential as DefaultAzureCredentialAsync

# ── Load env ─────────────────────────────────────────────────────
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(SCRIPT_DIR, "..", "src", "backend")
load_dotenv(os.path.join(BACKEND_DIR, ".env"), override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("setup_search")

# ── Config ───────────────────────────────────────────────────────

SEARCH_ENDPOINT = os.getenv("AZURE_AI_SEARCH_ENDPOINT", "").rstrip("/")
OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
EMBEDDING_DEPLOYMENT = os.getenv(
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"
)
EMBEDDING_DIMS = 1536
COSMOS_ENDPOINT = os.getenv("COSMOSDB_ENDPOINT", "")
COSMOS_DATABASE = os.getenv("COSMOSDB_DATABASE", "macae")

SEARCH_API = "2024-07-01"
OPENAI_API = "2024-10-21"

EXISTING_INDICES = [
    "contract-compliance-doc-index",
    "contract-risk-doc-index",
    "contract-summary-doc-index",
    "macae-retail-customer-index",
    "macae-retail-order-index",
    "macae-rfp-compliance-index",
    "macae-rfp-risk-index",
    "macae-rfp-summary-index",
]

# ── Schema templates ─────────────────────────────────────────────

VECTOR_SEARCH_CONFIG = {
    "algorithms": [
        {
            "name": "hnsw-algo",
            "kind": "hnsw",
            "hnswParameters": {
                "m": 4,
                "efConstruction": 400,
                "efSearch": 500,
                "metric": "cosine",
            },
        }
    ],
    "profiles": [
        {
            "name": "vector-profile",
            "algorithm": "hnsw-algo",
        }
    ],
}

CONTENT_VECTOR_FIELD = {
    "name": "content_vector",
    "type": "Collection(Edm.Single)",
    "searchable": True,
    "retrievable": False,
    "stored": True,
    "dimensions": EMBEDDING_DIMS,
    "vectorSearchProfile": "vector-profile",
}

DOC_SEMANTIC_CONFIG = {
    "configurations": [
        {
            "name": "default",
            "prioritizedFields": {
                "titleField": {"fieldName": "title"},
                "prioritizedContentFields": [{"fieldName": "content"}],
                "prioritizedKeywordsFields": [],
            },
        }
    ],
}

CHAT_SEMANTIC_CONFIG = {
    "configurations": [
        {
            "name": "chat-semantic-config",
            "prioritizedFields": {
                "prioritizedContentFields": [{"fieldName": "content"}],
                "prioritizedKeywordsFields": [],
            },
        }
    ],
}

CHAT_HISTORY_INDEX_SCHEMA = {
    "name": "chat-history-index",
    "fields": [
        {
            "name": "id",
            "type": "Edm.String",
            "key": True,
            "searchable": False,
            "filterable": True,
            "retrievable": True,
        },
        {
            "name": "session_id",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "retrievable": True,
        },
        {
            "name": "user_id",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "retrievable": True,
        },
        {
            "name": "role",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "retrievable": True,
        },
        {
            "name": "content",
            "type": "Edm.String",
            "searchable": True,
            "filterable": False,
            "retrievable": True,
        },
        {
            "name": "content_vector",
            "type": "Collection(Edm.Single)",
            "searchable": True,
            "retrievable": False,
            "stored": True,
            "dimensions": EMBEDDING_DIMS,
            "vectorSearchProfile": "vector-profile",
        },
        {
            "name": "intent",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "retrievable": True,
        },
        {
            "name": "timestamp",
            "type": "Edm.DateTimeOffset",
            "searchable": False,
            "filterable": True,
            "sortable": True,
            "retrievable": True,
        },
        {
            "name": "session_name",
            "type": "Edm.String",
            "searchable": True,
            "filterable": False,
            "retrievable": True,
        },
    ],
    "vectorSearch": VECTOR_SEARCH_CONFIG,
    "semantic": CHAT_SEMANTIC_CONFIG,
}


# ── Azure API client ─────────────────────────────────────────────


class AzureClients:
    """Thin wrapper for Azure REST API calls."""

    def __init__(self):
        self.credential = DefaultAzureCredentialAsync()
        self._session: Optional[aiohttp.ClientSession] = None

    async def init_session(self):
        self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session:
            await self._session.close()
        await self.credential.close()

    async def _search_headers(self) -> dict:
        token = await self.credential.get_token("https://search.azure.com/.default")
        return {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }

    async def _openai_headers(self) -> dict:
        token = await self.credential.get_token(
            "https://cognitiveservices.azure.com/.default"
        )
        return {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }

    # ── Search index operations ──────────────────────────────────

    async def get_index(self, name: str) -> Optional[dict]:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/indexes/{name}?api-version={SEARCH_API}"
        async with self._session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

    async def put_index(self, name: str, schema: dict) -> bool:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/indexes/{name}?api-version={SEARCH_API}"
        async with self._session.put(url, headers=headers, json=schema) as resp:
            if resp.status in (200, 201, 204):
                return True
            body = await resp.text()
            logger.error(
                "PUT index %s failed: HTTP %s — %s", name, resp.status, body[:500]
            )
            return False

    async def create_index(self, schema: dict) -> bool:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/indexes?api-version={SEARCH_API}"
        async with self._session.post(url, headers=headers, json=schema) as resp:
            if resp.status in (200, 201):
                return True
            body = await resp.text()
            logger.error("Create index failed: HTTP %s — %s", resp.status, body[:500])
            return False

    async def search_docs(
        self, index_name: str, search: str = "*", top: int = 1000
    ) -> list:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/indexes/{index_name}/docs/search?api-version={SEARCH_API}"
        body = {"search": search, "top": top, "select": "*"}
        async with self._session.post(url, headers=headers, json=body) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("value", [])
            return []

    async def upload_docs(self, index_name: str, docs: list) -> bool:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/indexes/{index_name}/docs/index?api-version={SEARCH_API}"
        async with self._session.post(
            url, headers=headers, json={"value": docs}
        ) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                failed = [r for r in data.get("value", []) if not r.get("status")]
                if failed:
                    logger.warning("  Some docs failed: %s", failed[:3])
                return True
            body = await resp.text()
            logger.error(
                "Upload to %s failed: HTTP %s — %s",
                index_name,
                resp.status,
                body[:500],
            )
            return False

    # ── Embedding generation ─────────────────────────────────────

    async def generate_embeddings(
        self, texts: List[str]
    ) -> Optional[List[List[float]]]:
        """Batch embedding generation (chunks of 16 to avoid token limits)."""
        if not texts:
            return []

        all_embeddings: List[List[float]] = []
        url = (
            f"{OPENAI_ENDPOINT}/openai/deployments/{EMBEDDING_DEPLOYMENT}"
            f"/embeddings?api-version={OPENAI_API}"
        )

        for i in range(0, len(texts), 16):
            batch = [t[:8000] for t in texts[i : i + 16]]
            headers = await self._openai_headers()
            async with self._session.post(
                url, headers=headers, json={"input": batch}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    all_embeddings.extend([d["embedding"] for d in data["data"]])
                else:
                    body = await resp.text()
                    logger.error(
                        "Embedding error (batch %d): HTTP %s — %s",
                        i,
                        resp.status,
                        body[:300],
                    )
                    return None
            if i + 16 < len(texts):
                await asyncio.sleep(0.5)

        return all_embeddings


# ═══════════════════════════════════════════════════════════════════
# Step 1: Upgrade existing indices
# ═══════════════════════════════════════════════════════════════════


async def upgrade_existing_indices(clients: AzureClients) -> int:
    """Add content_vector + vectorSearch + semantic config to existing indices."""
    upgraded = 0
    for idx_name in EXISTING_INDICES:
        logger.info("  Checking index: %s", idx_name)
        schema = await clients.get_index(idx_name)
        if not schema:
            logger.warning("    Index %s not found, skipping", idx_name)
            continue

        field_names = [f["name"] for f in schema.get("fields", [])]
        if "content_vector" in field_names:
            logger.info("    Already has content_vector ✅")
            upgraded += 1
            continue

        # Add vector field, vector search config, semantic config
        schema["fields"].append(CONTENT_VECTOR_FIELD)
        schema["vectorSearch"] = VECTOR_SEARCH_CONFIG
        schema["semantic"] = DOC_SEMANTIC_CONFIG

        # Remove OData metadata (can't send in PUT)
        for key in ["@odata.context", "@odata.etag"]:
            schema.pop(key, None)

        if await clients.put_index(idx_name, schema):
            logger.info("    ✅ Upgraded %s", idx_name)
            upgraded += 1
        else:
            logger.error("    ❌ Failed to upgrade %s", idx_name)

    return upgraded


# ═══════════════════════════════════════════════════════════════════
# Step 2: Create chat-history-index
# ═══════════════════════════════════════════════════════════════════


async def create_chat_history_index(clients: AzureClients) -> bool:
    """Create chat-history-index for message-level RAG."""
    existing = await clients.get_index("chat-history-index")
    if existing:
        logger.info("  chat-history-index already exists ✅")
        return True

    if await clients.create_index(CHAT_HISTORY_INDEX_SCHEMA):
        logger.info("  ✅ Created chat-history-index")
        return True

    logger.error("  ❌ Failed to create chat-history-index")
    return False


# ═══════════════════════════════════════════════════════════════════
# Step 3: Backfill embeddings for existing docs
# ═══════════════════════════════════════════════════════════════════


async def backfill_existing_docs(clients: AzureClients) -> int:
    """Generate and store embeddings for all existing documents."""
    total = 0
    for idx_name in EXISTING_INDICES:
        logger.info("  Backfilling: %s", idx_name)
        docs = await clients.search_docs(idx_name)
        if not docs:
            logger.info("    No documents found")
            continue

        # Check if first doc already has vector
        if docs[0].get("content_vector"):
            logger.info("    Already has vectors ✅ (%d docs)", len(docs))
            total += len(docs)
            continue

        contents = [d.get("content", "") or d.get("title", "") for d in docs]
        logger.info("    Generating embeddings for %d docs...", len(contents))
        embeddings = await clients.generate_embeddings(contents)
        if embeddings is None:
            logger.error("    Failed to generate embeddings")
            continue

        merge_docs = []
        for doc, vector in zip(docs, embeddings):
            merge_docs.append(
                {
                    "@search.action": "mergeOrUpload",
                    "id": doc["id"],
                    "content": doc.get("content", ""),
                    "title": doc.get("title", ""),
                    "content_vector": vector,
                }
            )

        if await clients.upload_docs(idx_name, merge_docs):
            logger.info("    ✅ %d docs embedded", len(merge_docs))
            total += len(merge_docs)
        else:
            logger.error("    ❌ Upload failed")

    return total


# ═══════════════════════════════════════════════════════════════════
# Step 4: Backfill chat history from Cosmos → chat-history-index
# ═══════════════════════════════════════════════════════════════════


async def backfill_chat_history(clients: AzureClients) -> int:
    """Index all existing chat messages from Cosmos DB."""
    from azure.cosmos.aio import CosmosClient

    if not COSMOS_ENDPOINT:
        logger.warning("  COSMOSDB_ENDPOINT not set, skipping")
        return 0

    cosmos = CosmosClient(url=COSMOS_ENDPOINT, credential=clients.credential)
    db = cosmos.get_database_client(COSMOS_DATABASE)
    container = db.get_container_client("chat_sessions")

    # Collect all messages from sessions
    all_messages: List[Dict[str, Any]] = []
    session_count = 0

    async for session in container.query_items(
        "SELECT c.id, c.user_id, c.session_name, c.messages FROM c "
        "WHERE ARRAY_LENGTH(c.messages) > 0"
    ):
        session_count += 1
        session_id = session["id"]
        user_id = session.get("user_id", "")
        session_name = session.get("session_name", "")

        for msg in session.get("messages", []):
            content = msg.get("content", "")
            if not content or len(content.strip()) < 3:
                continue
            all_messages.append(
                {
                    "message_id": msg.get("id", f"{session_id}-{len(all_messages)}"),
                    "session_id": session_id,
                    "user_id": user_id,
                    "role": msg.get("role", "user"),
                    "content": content,
                    "intent": (msg.get("metadata") or {}).get("intent", ""),
                    "timestamp": msg.get("timestamp", ""),
                    "session_name": session_name,
                }
            )

    logger.info(
        "  Found %d messages across %d sessions", len(all_messages), session_count
    )

    if not all_messages:
        await cosmos.close()
        return 0

    # Generate embeddings
    contents = [m["content"] for m in all_messages]
    logger.info("  Generating embeddings for %d messages...", len(contents))
    embeddings = await clients.generate_embeddings(contents)
    if embeddings is None:
        logger.error("  Failed to generate embeddings")
        await cosmos.close()
        return 0

    # Build search docs
    search_docs: List[Dict[str, Any]] = []
    for msg, vector in zip(all_messages, embeddings):
        ts = msg["timestamp"]
        # Ensure valid ISO 8601 format for DateTimeOffset
        if ts and not ts.endswith("Z") and "+" not in ts:
            ts = ts + "Z"
        search_docs.append(
            {
                "@search.action": "mergeOrUpload",
                "id": msg["message_id"],
                "session_id": msg["session_id"],
                "user_id": msg["user_id"],
                "role": msg["role"],
                "content": msg["content"],
                "content_vector": vector,
                "intent": msg["intent"],
                "timestamp": ts if ts else None,
                "session_name": msg["session_name"],
            }
        )

    # Upload in batches of 100
    indexed = 0
    for i in range(0, len(search_docs), 100):
        batch = search_docs[i : i + 100]
        if await clients.upload_docs("chat-history-index", batch):
            indexed += len(batch)
            logger.info("    Indexed batch %d–%d", i + 1, i + len(batch))
        else:
            logger.error("    Failed batch %d–%d", i + 1, i + len(batch))
        await asyncio.sleep(0.3)

    await cosmos.close()
    logger.info("  ✅ Backfilled %d/%d messages", indexed, len(search_docs))
    return indexed


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════


async def main():
    logger.info("=" * 60)
    logger.info("MACAE AI Search Pipeline Setup")
    logger.info("=" * 60)
    logger.info("Search:    %s", SEARCH_ENDPOINT)
    logger.info("OpenAI:    %s", OPENAI_ENDPOINT)
    logger.info("Embedding: %s (%d dims)", EMBEDDING_DEPLOYMENT, EMBEDDING_DIMS)
    logger.info("Cosmos:    %s", COSMOS_ENDPOINT[:60] if COSMOS_ENDPOINT else "NOT SET")
    logger.info("")

    if not SEARCH_ENDPOINT:
        logger.error("AZURE_AI_SEARCH_ENDPOINT not set. Aborting.")
        sys.exit(1)
    if not OPENAI_ENDPOINT:
        logger.error("AZURE_OPENAI_ENDPOINT not set. Aborting.")
        sys.exit(1)

    clients = AzureClients()
    await clients.init_session()

    try:
        # Step 1
        logger.info("── Step 1: Upgrade existing indices ──────────────────")
        upgraded = await upgrade_existing_indices(clients)
        logger.info("Result: %d/%d indices upgraded\n", upgraded, len(EXISTING_INDICES))

        # Step 2
        logger.info("── Step 2: Create chat-history-index ─────────────────")
        created = await create_chat_history_index(clients)
        logger.info("")

        # Step 3
        logger.info("── Step 3: Backfill embeddings for existing docs ─────")
        docs_count = await backfill_existing_docs(clients)
        logger.info("Result: %d documents embedded\n", docs_count)

        # Step 4
        logger.info("── Step 4: Backfill chat history from Cosmos DB ──────")
        msgs_count = await backfill_chat_history(clients)
        logger.info("Result: %d messages indexed\n", msgs_count)

        # Summary
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("=" * 60)
        logger.info("  Indices upgraded:     %d/%d", upgraded, len(EXISTING_INDICES))
        logger.info("  Chat index created:   %s", "✅" if created else "❌")
        logger.info("  Doc embeddings:       %d", docs_count)
        logger.info("  Chat msgs indexed:    %d", msgs_count)
        logger.info(
            "  Total indices:        %d",
            len(EXISTING_INDICES) + (1 if created else 0),
        )
        logger.info("=" * 60)
        logger.info("")
        logger.info("Next steps:")
        logger.info(
            "  1. Add AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small to .env"
        )
        logger.info(
            "  2. Restart backend — new messages auto-indexed via SearchIndexService"
        )
        logger.info(
            "  3. Both MCP and conversational agents now use hybrid search for context"
        )

    finally:
        await clients.close()


if __name__ == "__main__":
    asyncio.run(main())
