"""S3/CloudFront-specific health checker implementation."""
import logging
import os
from typing import Optional, Dict, Any
from datetime import datetime

from observability.provider_health_checker import (
    BaseProviderHealthChecker,
    ProviderHealthMetrics,
)

logger = logging.getLogger(__name__)


class S3HealthChecker(BaseProviderHealthChecker):
    """Health checker for AWS S3."""

    def __init__(
        self,
        provider_id: str = "aws_s3",
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        bucket_name: Optional[str] = None,
    ):
        super().__init__(provider_id, project_id, session_id)
        self.bucket_name = bucket_name
        env_name = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "dev")).lower()
        default_sla_ms = "5000" if env_name in {"dev", "local"} else "500"
        self.latency_sla_ms = float(os.getenv("AWS_S3_SLA_MS", default_sla_ms))

    async def check_health(self) -> ProviderHealthMetrics:
        """Check S3 health: read, write capability, latency SLA, and quota."""
        from adapters.aws_adapter import AWSAdapter
        import time

        adapter = AWSAdapter(
            project_id=self.project_id or "default",
            session_id=self.session_id,
            user_id="health_check",
        )

        async def test_s3_comprehensive():
            # Test read (list objects)
            start = time.perf_counter()
            result = await adapter.execute(
                tool_name="s3_list_objects",
                params={"bucket_name": self.bucket_name or "fibroskin-academic-videos", "prefix": "", "max_keys": 10},
                tool_id="health_check_s3_read",
            )
            read_time_ms = (time.perf_counter() - start) * 1000

            if not result.success:
                raise Exception(f"S3 read failed: {result.error}")

            # Check SLA (should be <500ms)
            if read_time_ms > self.latency_sla_ms:
                logger.warning(
                    "S3 latency SLA violation: %.0fms > %.0fms",
                    read_time_ms,
                    self.latency_sla_ms,
                )

            # Test write capability
            try:
                write_result = await adapter.execute(
                    tool_name="get_signed_url",
                    params={"s3_key": "health_check/test.txt"},
                    tool_id="health_check_s3_write",
                )
                if not write_result.success:
                    logger.warning("S3 write capability may be limited")
            except Exception as e:
                logger.warning(f"S3 write test: {e}")

            # Check quota
            data = result.result or {}
            obj_count = data.get("count", 0)
            if obj_count > 1000000:
                logger.warning(f"S3 quota warning: {obj_count} objects")

            return result.success

        success, duration_ms, error = await self._timed_check(test_s3_comprehensive)

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
                logger.warning(f"Could not get S3 usage metrics: {e}")

        return ProviderHealthMetrics(
            provider_id=self.provider_id,
            is_healthy=is_healthy,
            response_time_ms=duration_ms,
            error_message=error,
            metrics=metrics,
        )

    async def get_usage_metrics(self) -> Dict[str, Any]:
        """Get real S3 bucket usage statistics via AWSAdapter."""
        try:
            from adapters.aws_adapter import AWSAdapter

            adapter = AWSAdapter(
                project_id=self.project_id or "default",
                session_id=self.session_id,
                user_id="health_check",
            )

            # List objects to get bucket stats
            result = await adapter.execute(
                tool_name="s3_list_objects",
                params={"bucket_name": self.bucket_name or "fibroskin-academic-videos", "prefix": "", "max_keys": 1000},
                tool_id="health_check_s3_list",
            )

            if result.success:
                data = result.result
                return {
                    "bucket_name": data.get("bucket", self.bucket_name or "unknown"),
                    "object_count": data.get("count", 0),
                    "objects_listed": len(data.get("objects", [])),
                    "prefix": data.get("prefix", ""),
                    "endpoint": data.get("endpoint_used", "unknown"),
                    "source": data.get("source", "lambda_api"),
                    "last_checked": datetime.utcnow().isoformat(),
                }
            else:
                return {
                    "bucket_name": self.bucket_name or "unknown",
                    "error": result.error or "Failed to list objects",
                    "credentials_required": result.credentials_required is not None,
                    "last_checked": datetime.utcnow().isoformat(),
                }
        except Exception as e:
            logger.error(f"Error getting S3 usage metrics: {e}")
            return {
                "error": str(e),
                "last_checked": datetime.utcnow().isoformat(),
            }

    async def diagnose(self) -> Dict[str, Any]:
        """Run S3-specific diagnostics."""
        base_diag = await super().diagnose()

        # Add S3-specific diagnostics
        base_diag.update({
            "bucket_name": self.bucket_name,
            "cloudfront_enabled": True,  # Based on your setup
        })

        return base_diag
