"""Firestore adapter using official Google Cloud SDK."""
import json
from typing import Any, Dict

from adapters.base_adapter import BaseAdapter


class FirestoreAdapter(BaseAdapter):
    """Minimal Firestore adapter using official SDK."""

    async def _read_firestore_doc(self, db, params: Dict[str, Any]) -> Dict[str, Any]:
        """Read a Firestore document ensuring the path is valid."""
        # Prefer 'full_path', fallback to 'doc_path'
        doc_path = params.get("full_path") or params.get("doc_path", "")
        doc_path = doc_path.strip("/")
        if not doc_path:
            raise ValueError("full_path or doc_path is required")

        # Firestore document path must have even number of segments (collection/doc_id[/subcollection/doc_id...])
        parts = doc_path.split('/')
        if len(parts) % 2 != 0:
            return {
                "success": False,
                "error": f"Invalid document path: '{doc_path}'. A document path must have an even number of segments (collection/doc_id)."
            }

        doc_ref = db.document(doc_path)
        doc = doc_ref.get()
        if not doc.exists:
            return {"exists": False, "data": None}
        return {"exists": True, "data": doc.to_dict(), "id": doc.id}

    def _get_provider_id(self) -> str:
        return "firestore"

    async def _execute_with_credentials(
        self,
        tool_name: str,
        params: Dict[str, Any],
        credentials: Dict[str, str],
    ) -> Any:
        """Execute Firestore operations using official SDK."""
        from google.cloud import firestore

        # Initialize Firestore client with service account
        service_account_json = credentials.get("service_account_json")
        if not service_account_json:
            raise ValueError("service_account_json is required")

        service_account_info = json.loads(service_account_json)
        db = firestore.Client.from_service_account_info(service_account_info)

        # Route to specific operation
        if tool_name == "get_document":
            return await self._get_document(db, params)
        elif tool_name == "create_document":
            return await self._create_document(db, params)
        elif tool_name == "query_documents":
            return await self._query_documents(db, params)
        elif tool_name == "list_collections":
            return await self._list_collections(db, params)
        elif tool_name == "list_documents":
            return await self._list_documents(db, params)
        elif tool_name == "count_documents":
            return await self._count_documents(db, params)
        elif tool_name == "update_document":
            return await self._update_document(db, params)
        elif tool_name == "delete_document":
            return await self._delete_document(db, params)
        elif tool_name == "read_firestore_doc":
            return await self._read_firestore_doc(db, params)
        else:
            raise ValueError(f"Unknown Firestore operation: {tool_name}")

    async def _get_document(self, db, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get a single document by full path."""
        full_path = params.get("full_path")
        if not full_path:
            raise ValueError("full_path is required")

        doc_ref = db.document(full_path)
        doc = doc_ref.get()

        if not doc.exists:
            return {"exists": False, "data": None}

        return {"exists": True, "data": doc.to_dict(), "id": doc.id}

    async def _create_document(self, db, params: Dict[str, Any]) -> Dict[str, str]:
        """Create a new document in Firestore."""
        collection = params.get("collection")
        data = params.get("data")
        document_id = params.get("document_id")

        if not collection or not data:
            raise ValueError("collection and data are required")

        if document_id:
            doc_ref = db.collection(collection).document(document_id)
        else:
            doc_ref = db.collection(collection).document()

        doc_ref.set(data)
        return {"document_id": doc_ref.id, "collection": collection}

    async def _query_documents(self, db, params: Dict[str, Any]) -> list:
        """Query documents from Firestore."""
        collection = params.get("collection")
        limit = params.get("limit", 10)
        where_clauses = params.get("where", [])

        if not collection:
            raise ValueError("collection is required")

        query = db.collection(collection)

        # Apply where clauses if provided
        for clause in where_clauses:
            field = clause.get("field")
            operator = clause.get("operator", "==")
            value = clause.get("value")
            if field and value is not None:
                query = query.where(field, operator, value)

        docs = query.limit(limit).stream()
        return [{"id": doc.id, **doc.to_dict()} for doc in docs]

    async def _update_document(self, db, params: Dict[str, Any]) -> Dict[str, str]:
        """Update an existing document in Firestore."""
        collection = params.get("collection")
        document_id = params.get("document_id")
        data = params.get("data")

        if not collection or not document_id or not data:
            raise ValueError("collection, document_id, and data are required")

        doc_ref = db.collection(collection).document(document_id)
        doc_ref.update(data)
        return {"document_id": document_id, "collection": collection}

    async def _delete_document(self, db, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete a document from Firestore."""
        collection = params.get("collection")
        document_id = params.get("document_id")

        if not collection or not document_id:
            raise ValueError("collection and document_id are required")

        doc_ref = db.collection(collection).document(document_id)
        doc_ref.delete()
        return {"document_id": document_id, "collection": collection, "deleted": True}

    async def _list_collections(self, db, params: Dict[str, Any]) -> list:
        """List all collections in Firestore."""
        collections = db.collections()
        return [{"id": col.id, "collection_id": col.id} for col in collections]

    async def _list_documents(self, db, params: Dict[str, Any]) -> list:
        """List document IDs in a collection."""
        collection = params.get("collection")
        limit = params.get("limit", 100)

        if not collection:
            raise ValueError("collection is required")

        docs = db.collection(collection).limit(limit).stream()
        return [{"id": doc.id, "path": f"{collection}/{doc.id}"} for doc in docs]

    async def _count_documents(self, db, params: Dict[str, Any]) -> Dict[str, int]:
        """Count documents in a collection."""
        collection = params.get("collection")
        if not collection:
            raise ValueError("collection is required")

        docs = db.collection(collection).stream()
        count = sum(1 for _ in docs)
        return {"collection": collection, "count": count}
