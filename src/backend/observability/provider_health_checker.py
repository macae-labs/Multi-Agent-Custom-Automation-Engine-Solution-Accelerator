"""Base interface for provider-specific health checks.

This module defines the abstract interface that all provider health checkers
must implement. New providers added to ToolRegistry can automatically have
health checkers registered without modifying core health check logic.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


@dataclass
class ProviderHealthMetrics:
    """Standard health metrics for a provider."""

    provider_id: str
    is_healthy: bool
    response_time_ms: float
    status_code: Optional[int] = None
    error_message: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "provider_id": self.provider_id,
            "is_healthy": self.is_healthy,
            "response_time_ms": self.response_time_ms,
            "status_code": self.status_code,
            "error_message": self.error_message,
            "metrics": self.metrics,
            "timestamp": self.timestamp.isoformat(),
        }


class ProviderHealthChecker(ABC):
    """Abstract base class for provider health checks.

    Each provider (Firestore, S3, Salesforce, etc.) should implement this
    interface to provide health status, metrics, and diagnostics.

    Design principles:
    1. Non-blocking: Health checks should timeout quickly
    2. Informative: Return actionable metrics beyond just "up/down"
    3. Credential-aware: Handle missing credentials gracefully
    4. Scalable: Support checking specific resources vs entire provider
    """

    def __init__(
        self,
        provider_id: str,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ):
        self.provider_id = provider_id
        self.project_id = project_id
        self.session_id = session_id

    @abstractmethod
    async def check_health(self) -> ProviderHealthMetrics:
        """Perform health check for this provider.

        Returns:
            ProviderHealthMetrics with health status and relevant metrics

        Example metrics to include:
        - Firestore: doc_count, collection_count, recent_writes
        - S3: bucket_size_mb, object_count, recent_uploads
        - API: rate_limit_remaining, requests_per_minute
        """
        pass

    @abstractmethod
    async def check_credentials(self) -> bool:
        """Verify that credentials are configured and valid.

        Returns:
            True if credentials exist and work, False otherwise
        """
        pass

    async def get_usage_metrics(self) -> Dict[str, Any]:
        """Get usage/activity metrics for this provider.

        Optional method for providers that track usage.
        Returns empty dict by default.

        Returns:
            Dict with usage metrics like requests_24h, bandwidth_used, etc.
        """
        return {}

    async def diagnose(self) -> Dict[str, Any]:
        """Run diagnostic checks and return detailed status.

        Optional method for deep diagnostics. Returns basic info by default.

        Returns:
            Dict with diagnostic information
        """
        health = await self.check_health()
        has_creds = await self.check_credentials()

        return {
            "provider_id": self.provider_id,
            "health": health.to_dict(),
            "credentials_configured": has_creds,
            "project_id": self.project_id,
        }


class BaseProviderHealthChecker(ProviderHealthChecker):
    """Base implementation with common utility methods.

    Subclasses can override specific methods while inheriting utilities.
    """

    async def _timed_check(self, check_fn) -> tuple[bool, float, Optional[str]]:
        """Helper to time a check function with retry/backoff logic.

        Args:
            check_fn: Async function to execute

        Returns:
            Tuple of (success, duration_ms, error_message)
        """
        import time
        import asyncio

        max_retries = 1
        timeout_seconds = 0.5

        for attempt in range(max_retries):
            start = time.perf_counter()
            try:
                await asyncio.wait_for(check_fn(), timeout=timeout_seconds)
                duration_ms = (time.perf_counter() - start) * 1000
                return True, duration_ms, None
            except asyncio.TimeoutError:
                duration_ms = (time.perf_counter() - start) * 1000
                if attempt < max_retries - 1:
                    backoff_time = 2**attempt
                    logger.warning(
                        f"Health check timeout for {self.provider_id} (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {backoff_time}s..."
                    )
                    await asyncio.sleep(backoff_time)
                else:
                    error_msg = "Health check timeout after retries"
                    logger.error(
                        f"{self.provider_id}: {error_msg} (total: {duration_ms:.0f}ms)"
                    )
                    return False, duration_ms, error_msg
            except Exception as e:
                duration_ms = (time.perf_counter() - start) * 1000
                logger.error(f"Health check failed for {self.provider_id}: {e}")
                return False, duration_ms, str(e)

        return False, 0.0, "Unknown error"

    async def check_credentials(self) -> bool:
        """Default implementation checks with credential_resolver."""
        try:
            from credential_resolver import credential_resolver

            creds = await credential_resolver.resolve_credentials(
                project_id=self.project_id or "default",
                provider_id=self.provider_id,
            )
            return creds is not None and len(creds) > 0
        except Exception as e:
            logger.warning(f"Credential check failed for {self.provider_id}: {e}")
            return False
