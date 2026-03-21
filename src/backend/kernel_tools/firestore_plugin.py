import logging
from typing import Annotated, Optional
from semantic_kernel.functions import kernel_function
from adapters.firestore_adapter import FirestoreAdapter
from adapters.base_adapter import BaseAdapter


class FirestorePlugin:
    """Generic Firestore plugin using adapter for credential resolution."""

    def __init__(
        self,
        project_id: str,
        collection_root: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        self.project_id = project_id
        self.collection_root = collection_root
        self.session_id = session_id
        self.user_id = user_id
        self._adapter = FirestoreAdapter(
            project_id=project_id, session_id=session_id, user_id=user_id
        )

    @kernel_function(
        name="read_firestore_doc",
        description="Read a document from Firestore using its path (e.g., 'users/123')",
    )
    async def read_document(
        self,
        doc_path: Annotated[str, "Full document path from the collection root"],
    ) -> str:
        """Read document from Firestore using adapter."""
        try:
            # CAMBIO: La llave debe ser 'full_path' para que el adapter la reconozca
            result = await self._adapter.execute(
                tool_name="get_document",
                params={"full_path": doc_path.strip("/")},
                tool_id="read_firestore_doc",
            )
            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"
            doc_data = result.result
            if not doc_data.get("exists"):
                return f"Document '{doc_path}' not found."
            return str(doc_data.get("data"))
        except Exception as e:
            logging.error(f"Firestore read failed: {e}")
            return f"Error: {str(e)}"

    @kernel_function(
        name="write_firestore_doc",
        description="Write document to Firestore collection. Use format: collection/document_id or just collection/ for auto-ID.",
    )
    async def write_document(
        self,
        doc_path: Annotated[
            str,
            "Document path as collection/document_id (e.g., 'users/user123') or just 'users/' for auto-ID or 'users/123/logs' for subcollection auto-ID",
        ],
        data: Annotated[str, "JSON data as string"],
    ) -> str:
        """Write document to Firestore using adapter. Supports auto-ID if doc_path is a collection or subcollection path (odd number of parts)."""
        try:
            import json

            parts = [p for p in doc_path.strip("/").split("/") if p]
            if not parts:
                return f"Error: doc_path must specify at least a collection, got: {doc_path}"

            # Odd number of parts: treat as collection or subcollection, use auto-ID
            # Even number of parts: treat last as document_id
            if len(parts) % 2 == 1:
                collection = parts[0]
                if len(parts) == 1:
                    subpath = None
                else:
                    subpath = "/".join(parts[1:])
                document_id = None
            else:
                collection = parts[0]
                if len(parts) == 2:
                    document_id = parts[1]
                    subpath = None
                else:
                    document_id = parts[-1]
                    subpath = "/".join(parts[1:-1])

            # Compose Firestore path for adapter
            params = {"collection": collection, "data": json.loads(data)}
            if document_id is not None:
                params["document_id"] = document_id
            if subpath:
                params["subpath"] = subpath

            result = await self._adapter.execute(
                tool_name="create_document",
                params=params,
                tool_id="write_firestore_doc",
            )

            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"

            doc_id = result.result.get("document_id")
            return f"SUCCESS: Document '{doc_id}' written to collection '{collection}' with data: {data}"

        except Exception as e:
            logging.error(f"Firestore write failed: {e}")
            return f"Error: {str(e)}"

    @kernel_function(
        name="list_firestore_collections", description="List all Firestore collections"
    )
    async def list_collections(self) -> str:
        """List all collections in Firestore."""
        try:
            result = await self._adapter.execute(
                tool_name="list_collections",
                params={},
                tool_id="list_firestore_collections",
            )

            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"

            collections = result.result
            return str([c["id"] for c in collections])

        except Exception as e:
            logging.error(f"Firestore list failed: {e}")
            return f"Error: {str(e)}"

    @kernel_function(
        name="list_firestore_documents",
        description="List documents in a Firestore collection",
    )
    async def list_documents(
        self,
        collection: Annotated[str, "Collection name (root level, no slashes)"],
        limit: Annotated[int, "Maximum documents to return"] = 25,
    ) -> str:
        """List documents in a collection using adapter query."""
        try:
            result = await self._adapter.execute(
                tool_name="query_documents",
                params={"collection": collection, "limit": limit},
                tool_id="list_firestore_documents",
            )

            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"

            docs = result.result or []
            return str(docs)
        except Exception as e:
            logging.error(f"Firestore list_documents failed: {e}")
            return f"Error: {str(e)}"

    @kernel_function(
        name="count_firestore_docs",
        description="Count documents in a Firestore collection",
    )
    async def count_documents(
        self,
        collection: Annotated[str, "Collection name (root level, no slashes)"],
    ) -> str:
        """Count documents in a collection."""
        try:
            # Use collection name directly, don't prepend collection_root
            result = await self._adapter.execute(
                tool_name="count_documents",
                params={"collection": collection},
                tool_id="count_firestore_docs",
            )

            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"

            return f"Count: {result.result['count']}"

        except Exception as e:
            logging.error(f"Firestore count failed: {e}")
            return f"Error: {str(e)}"

    @kernel_function(
        name="query_firestore_docs",
        description="Query documents from a Firestore collection with optional where clauses and limit. 'where' should be a JSON string list of clauses: [{field, operator, value}].",
    )
    async def query_documents(
        self,
        collection: Annotated[str, "Collection name (root level, no slashes)"],
        where: Annotated[
            str,
            "JSON string list of where clauses: [{field, operator, value}]. Optional.",
        ],
        limit: Annotated[
            int, "Maximum number of documents to return. Optional, default 10."
        ] = 10,
    ) -> str:
        """Query documents in a collection with optional where clauses and limit."""
        try:
            import json

            where_clauses = []
            if where:
                try:
                    where_clauses = json.loads(where)
                except Exception as e:
                    return f"Error: Invalid 'where' JSON: {e}"
            params = {"collection": collection, "limit": limit, "where": where_clauses}
            result = await self._adapter.execute(
                tool_name="query_documents",
                params=params,
                tool_id="query_firestore_docs",
            )
            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"
            return str(result.result)
        except Exception as e:
            logging.error(f"Firestore query failed: {e}")
            return f"Error: {str(e)}"

    @kernel_function(
        name="update_firestore_doc",
        description="Update an existing document in Firestore. Requires collection/document_id as doc_path and data as JSON string.",
    )
    async def update_document(
        self,
        doc_path: Annotated[
            str, "Document path as collection/document_id (e.g., 'users/user123')"
        ],
        data: Annotated[str, "JSON data as string for fields to update"],
    ) -> str:
        """Update an existing document in Firestore using adapter."""
        try:
            import json

            parts = [p for p in doc_path.strip("/").split("/") if p]
            if len(parts) != 2:
                return f"Error: doc_path must be in the format 'collection/document_id', got: {doc_path}"
            collection, document_id = parts
            params = {
                "collection": collection,
                "document_id": document_id,
                "data": json.loads(data),
            }
            result = await self._adapter.execute(
                tool_name="update_document",
                params=params,
                tool_id="update_firestore_doc",
            )
            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"
            return f"SUCCESS: Document '{document_id}' in collection '{collection}' updated with data: {data}"
        except Exception as e:
            logging.error(f"Firestore update failed: {e}")
            return f"Error: {str(e)}"

    @kernel_function(
        name="delete_firestore_doc",
        description="Delete a document from Firestore. Requires collection/document_id as doc_path.",
    )
    async def delete_document(
        self,
        doc_path: Annotated[
            str, "Document path as collection/document_id (e.g., 'users/user123')"
        ],
    ) -> str:
        """Delete a document from Firestore using adapter."""
        try:
            parts = [p for p in doc_path.strip("/").split("/") if p]
            if len(parts) != 2:
                return f"Error: doc_path must be in the format 'collection/document_id', got: {doc_path}"
            collection, document_id = parts
            params = {"collection": collection, "document_id": document_id}
            result = await self._adapter.execute(
                tool_name="delete_document",
                params=params,
                tool_id="delete_firestore_doc",
            )
            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"
            return f"SUCCESS: Document '{document_id}' in collection '{collection}' deleted."
        except Exception as e:
            logging.error(f"Firestore delete failed: {e}")
            return f"Error: {str(e)}"

    @kernel_function(
        name="list_firestore_subcollections",
        description="List subcollections of a Firestore document or collection. Accepts a document path (e.g., 'users/123') or a collection name (e.g., 'users') to discover subcollections from the first available document.",
    )
    async def list_subcollections(
        self,
        doc_path: Annotated[str, "Document path (e.g., 'users/123' or 'courses/abc')"],
    ) -> str:
        """List subcollections under a document using adapter."""
        try:
            result = await self._adapter.execute(
                tool_name="list_subcollections",
                params={"doc_path": doc_path.strip("/")},
                tool_id="list_firestore_subcollections",
            )
            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"
            subcols = result.result
            if not subcols:
                return f"No subcollections found under '{doc_path}'"
            return str([s["id"] for s in subcols])
        except Exception as e:
            logging.error(f"Firestore list_subcollections failed: {e}")
            return f"Error: {str(e)}"

    @kernel_function(
        name="list_documents_at_path",
        description="List documents at any Firestore path including subcollections (e.g., 'users/123/orders')",
    )
    async def list_documents_at_path(
        self,
        collection_path: Annotated[
            str, "Full collection path (e.g., 'users' or 'users/123/orders')"
        ],
        limit: Annotated[int, "Maximum documents to return"] = 25,
    ) -> str:
        """List documents at any collection path (root or subcollection)."""
        try:
            result = await self._adapter.execute(
                tool_name="list_documents_at_path",
                params={"collection_path": collection_path.strip("/"), "limit": limit},
                tool_id="list_documents_at_path",
            )
            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"
            docs = result.result
            if not docs:
                return f"No documents found at '{collection_path}'"
            return str(docs)
        except Exception as e:
            logging.error(f"Firestore list_documents_at_path failed: {e}")
            return f"Error: {str(e)}"
