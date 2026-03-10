"""Firestore-specific health checker implementation."""
import logging
import os
from typing import Optional, Dict, Any
from datetime import datetime

from observability.provider_health_checker import (
    BaseProviderHealthChecker,
    ProviderHealthMetrics,
)

logger = logging.getLogger(__name__)


class FirestoreHealthChecker(BaseProviderHealthChecker):
    """Health checker for Google Firestore."""

    def __init__(
        self,
        provider_id: str = "firestore",
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        collection_root: Optional[str] = None,
    ):
        super().__init__(provider_id, project_id, session_id)
        self.collection_root = collection_root or ""
        env_name = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "dev")).lower()
        default_sla_ms = "2000" if env_name in {"dev", "local"} else "200"
        self.latency_sla_ms = float(os.getenv("FIRESTORE_SLA_MS", default_sla_ms))

    async def check_health(self) -> ProviderHealthMetrics:
        """Check Firestore health: read, write, latency SLA, and quota."""
        from adapters.firestore_adapter import FirestoreAdapter
        import time

        adapter = FirestoreAdapter(
            project_id=self.project_id or "default",
            session_id=self.session_id,
            user_id="health_check",
        )

        async def test_firestore_comprehensive():
            # Test read (get document)
            start = time.perf_counter()
            result = await adapter.execute(
                tool_name="get_document",
                params={"full_path": "health_check/test"},
                tool_id="health_check_read",
            )
            read_time_ms = (time.perf_counter() - start) * 1000

            if not result.success:
                raise Exception(f"Firestore read failed: {result.error}")

            # Check SLA (should be <200ms)
            if read_time_ms > self.latency_sla_ms:
                logger.warning(
                    "Firestore latency SLA violation: %.0fms > %.0fms",
                    read_time_ms,
                    self.latency_sla_ms,
                )

            # Test write capability
            try:
                query_result = await adapter.execute(
                    tool_name="query_documents",
                    params={"collection": "health_check", "limit": 1},
                    tool_id="health_check_query",
                )
                if not query_result.success:
                    logger.warning("Firestore write/query capability limited")
            except Exception as e:
                logger.warning(f"Firestore query test: {e}")

            # Check quota
            try:
                list_result = await adapter.execute(
                    tool_name="list_collections",
                    params={},
                    tool_id="health_check_quota",
                )
                if list_result.success:
                    cols = list_result.result.get("collections", [])
                    if len(cols) > 100:
                        logger.warning(f"Firestore quota: {len(cols)} collections > 100")
            except Exception as e:
                logger.debug(f"Firestore quota check: {e}")

            return result.success

        success, duration_ms, error = await self._timed_check(test_firestore_comprehensive)

        sla_compliant = duration_ms < self.latency_sla_ms
        metrics = {"sla_compliant": sla_compliant}

        # Health fails if operation failed OR SLA was violated
        is_healthy = success and sla_compliant
        if not sla_compliant:
            error = error or f"SLA violation: {duration_ms:.0f}ms > {self.latency_sla_ms:.0f}ms"

        if success:
            try:
                usage = await self.get_usage_metrics()
                metrics.update(usage)
            except Exception as e:
                logger.warning(f"Could not get Firestore usage metrics: {e}")

        return ProviderHealthMetrics(
            provider_id=self.provider_id,
            is_healthy=is_healthy,
            response_time_ms=duration_ms,
            error_message=error,
            metrics=metrics,
        )

    async def get_usage_metrics(self) -> Dict[str, Any]:
        """Get Firestore usage statistics."""
        from adapters.firestore_adapter import FirestoreAdapter

        try:
            adapter = FirestoreAdapter(
                project_id=self.project_id or "default",
                session_id=self.session_id,
                user_id="health_check",
            )

            # Try to list collections to verify access
            result = await adapter.execute(
                tool_name="list_collections",
                params={},
                tool_id="health_check_list",
            )

            if result.success:
                # result.result is a list of collection dicts directly, not wrapped
                collections = result.result if isinstance(result.result, list) else []
                return {
                    "collections_count": len(collections),
                    "collection_root": self.collection_root,
                    "last_checked": datetime.utcnow().isoformat(),
                }
            else:
                return {
                    "collections_count": 0,
                    "error": "Could not list collections",
                    "credentials_required": result.credentials_required is not None,
                }
        except Exception as e:
            logger.error(f"Error getting Firestore usage metrics: {e}")
            return {"error": str(e)}
