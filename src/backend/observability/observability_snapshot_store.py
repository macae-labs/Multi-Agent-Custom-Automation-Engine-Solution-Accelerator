"""Observability Snapshot Store - Persist and query health snapshots in Cosmos DB.

This module handles:
1. Persisting health snapshots to Cosmos DB for trend analysis
2. Querying snapshots by time range (24h, 7d, 30d)
3. Calculating aggregated metrics and trends
4. No manual binning - uses Cosmos SQL aggregation with bucket calculation
"""
import logging
import math
import uuid
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from context.cosmos_memory_kernel import CosmosMemoryContext

logger = logging.getLogger(__name__)


class ObservabilitySnapshotStore:
    """Store and retrieve health snapshots from Cosmos DB."""

    SESSION_ID = "observability_system"
    USER_ID = "system"

    def __init__(self) -> None:
        """Initialize snapshot store."""
        self.cosmos_context: Optional[CosmosMemoryContext] = None

    async def ensure_initialized(self) -> None:
        """Initialize Cosmos connection if needed."""
        if self.cosmos_context is None:
            self.cosmos_context = CosmosMemoryContext(
                session_id=self.SESSION_ID,
                user_id=self.USER_ID,
            )
            await self.cosmos_context.ensure_initialized()

    async def persist_snapshot(
        self,
        snapshot_dict: Dict[str, Any],
        project_id: Optional[str] = None,
    ) -> bool:
        """Persist a health snapshot to Cosmos DB."""
        await self.ensure_initialized()

        try:
            if self.cosmos_context is None:
                raise RuntimeError("Cosmos context was not initialized")

            now = datetime.utcnow()
            # Create snapshot document
            doc = {
                "id": f"snapshot_{uuid.uuid4().hex}",
                "type": "health_snapshot",
                "data_type": "health_snapshot",
                "session_id": self.SESSION_ID,
                "user_id": self.USER_ID,
                "project_id": project_id or "global",
                "timestamp": snapshot_dict.get("timestamp"),
                "health_score": snapshot_dict.get("health_score", 0.0),
                "overall_health": snapshot_dict.get("overall_health", False),
                "provider_health": snapshot_dict.get("provider_health", {}),
                "app_kpis": snapshot_dict.get("app_kpis", {}),
                "errors": snapshot_dict.get("errors", []),
                "created_at": now.isoformat(),
            }

            persisted_id = await self.cosmos_context.upsert_async("observability", doc)
            if not persisted_id:
                logger.error("Failed to persist snapshot document in Cosmos")
                return False

            logger.info(
                f"Snapshot persisted for project {project_id}: "
                f"score={doc['health_score']}, "
                f"health={doc['overall_health']}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to persist snapshot: {e}")
            return False

    async def get_trends(
        self,
        days: int = 1,
        project_id: Optional[str] = None,
        granularity_minutes: int = 60,
    ) -> Dict[str, Any]:
        """Get health trends for the specified time period.

        Uses Cosmos SQL aggregation with bucketing:
        - For 24h: hourly buckets (24 data points)
        - For 7d: 4-hour buckets (42 data points)
        - For 30d: daily buckets (30 data points)

        Args:
            days: Number of days to look back (1, 7, or 30)
            project_id: Optional project ID
            granularity_minutes: Bucket size in minutes (default 60 for hourly)

        Returns:
            Dict with:
            - timeline: List of time buckets with aggregated metrics
            - summary: Overall trend analysis
            - anomalies: Detected issues
        """
        await self.ensure_initialized()

        try:
            if self.cosmos_context is None:
                raise RuntimeError("Cosmos context was not initialized")

            # Calculate time window
            now = datetime.utcnow()
            start_time = now - timedelta(days=days)
            start_ts = int(start_time.timestamp())

            # Determine granularity based on days
            if days == 1:
                granularity_minutes = 60  # Hourly
            elif days == 7:
                granularity_minutes = 240  # 4-hourly
            elif days == 30:
                granularity_minutes = 1440  # Daily
            else:
                granularity_minutes = max(60, (days * 24 * 60) // 30)

            bucket_seconds = granularity_minutes * 60

            # Prefer native Cosmos aggregation when supported by the account/client.
            query = f"""
                SELECT
                    FLOOR(c._ts / {bucket_seconds}) * {bucket_seconds} AS bucket,
                    COUNT(1) AS sample_count,
                    AVG(c.health_score) AS avg_health_score,
                    MIN(c.health_score) AS min_health_score,
                    MAX(c.health_score) AS max_health_score,
                    AVG(IIF(IS_NUMBER(c.app_kpis.error_rate), c.app_kpis.error_rate, 0)) AS avg_error_rate,
                    AVG(IIF(IS_NUMBER(c.app_kpis.completion_rate), c.app_kpis.completion_rate, 0)) AS avg_completion_rate,
                    AVG(IIF(IS_ARRAY(c.errors), ARRAY_LENGTH(c.errors), 0)) AS avg_error_count
                FROM c
                WHERE c.type = @type
                    AND c.session_id = @session_id
                    AND c.project_id = @project_id
                    AND c._ts >= @start_ts
                GROUP BY FLOOR(c._ts / {bucket_seconds})
            """

            parameters = [
                {"name": "@type", "value": "health_snapshot"},
                {"name": "@session_id", "value": "observability_system"},
                {"name": "@project_id", "value": project_id or "global"},
                {"name": "@start_ts", "value": start_ts},
            ]

            # Cosmos SQL query comment (for reference)
            # bucket_seconds = granularity_minutes * 60
            # SELECT
            #     FLOOR(c._ts / {bucket_seconds}) * {bucket_seconds} as bucket,
            #     COUNT(1) as count,
            #     AVG(c.health_score) as avg_health_score,
            #     MIN(c.health_score) as min_health_score,
            #     MAX(c.health_score) as max_health_score,
            #     SUM(c.app_kpis.error_rate ?? 0) / COUNT(1) as avg_error_rate,
            #     SUM(c.app_kpis.completion_rate ?? 0) / COUNT(1) as avg_completion_rate,
            #     SUM(ARRAY_LENGTH(c.errors) ?? 0) / COUNT(1) as avg_error_count
            # FROM c
            # WHERE c.type = 'health_snapshot'
            #     AND c.project_id = @project_id
            #     AND c._ts >= @start_ts
            # GROUP BY FLOOR(c._ts / {bucket_seconds})
            # ORDER BY bucket DESC
            #
            # WHERE params:
            #   @project_id = project_id or "global"
            #   @start_ts = int(start_time.timestamp())

            logger.info(
                f"Querying trends for {days}d, project={project_id}, "
                f"granularity={granularity_minutes}min"
            )

            container = self.cosmos_context._container
            if container is None:
                raise RuntimeError("Cosmos container is not initialized")

            # Fast existence check to avoid expensive aggregate queries on empty ranges.
            precheck_query = """
                SELECT TOP 1 c.id
                FROM c
                WHERE c.type = @type
                    AND c.session_id = @session_id
                    AND c.project_id = @project_id
                    AND c._ts >= @start_ts
            """
            precheck_items = container.query_items(
                query=precheck_query,
                parameters=parameters,
                partition_key="observability_system",
            )
            has_data = False
            async for _ in precheck_items:
                has_data = True
                break
            if not has_data:
                return {
                    "query_params": {
                        "days": days,
                        "project_id": project_id or "global",
                        "granularity_minutes": granularity_minutes,
                        "time_window": {
                            "start": start_time.isoformat(),
                            "end": now.isoformat(),
                        },
                    },
                    "timeline": [],
                    "summary": self._calculate_summary([]),
                    "anomalies": [],
                }

            timeline: List[Dict[str, Any]] = []
            try:
                items = container.query_items(
                    query=query,
                    parameters=parameters,
                    partition_key=self.SESSION_ID,
                )
                async for item in items:
                    bucket = int(item.get("bucket", 0) or 0)
                    timeline.append(
                        {
                            "bucket": bucket,
                            "bucket_timestamp": datetime.utcfromtimestamp(bucket).isoformat(),
                            "avg_health_score": float(item.get("avg_health_score", 0.0) or 0.0),
                            "min_health_score": float(item.get("min_health_score", 0.0) or 0.0),
                            "max_health_score": float(item.get("max_health_score", 0.0) or 0.0),
                            "avg_error_rate": float(item.get("avg_error_rate", 0.0) or 0.0),
                            "avg_completion_rate": float(item.get("avg_completion_rate", 0.0) or 0.0),
                            "avg_error_count": float(item.get("avg_error_count", 0.0) or 0.0),
                            "sample_count": int(item.get("sample_count", 0) or 0),
                        }
                    )
            except Exception as native_exc:
                logger.warning(
                    "Native Cosmos trend aggregation unsupported; "
                    "falling back to Python aggregation. Reason: %s",
                    native_exc,
                )
                timeline = await self._get_trends_python_fallback(
                    container=container,
                    project_id=project_id or "global",
                    start_ts=start_ts,
                    bucket_seconds=bucket_seconds,
                )

            timeline.sort(key=lambda x: x["bucket"])
            timeline = timeline[-200:]

            return {
                "query_params": {
                    "days": days,
                    "project_id": project_id or "global",
                    "granularity_minutes": granularity_minutes,
                    "time_window": {
                        "start": start_time.isoformat(),
                        "end": now.isoformat(),
                    },
                },
                "timeline": timeline,
                "summary": self._calculate_summary(timeline),
                "anomalies": self._detect_anomalies(timeline),
            }

        except Exception as e:
            logger.error(f"Failed to get trends: {e}")
            raise

    async def _get_trends_python_fallback(
        self,
        container: Any,
        project_id: str,
        start_ts: int,
        bucket_seconds: int,
    ) -> List[Dict[str, Any]]:
        """Fallback aggregation when Cosmos SQL GROUP BY/aggregate isn't supported."""
        raw_query = """
            SELECT
                c._ts,
                c.health_score,
                c.app_kpis.error_rate AS error_rate,
                c.app_kpis.completion_rate AS completion_rate,
                c.errors
            FROM c
            WHERE c.type = @type
                AND c.session_id = @session_id
                AND c.project_id = @project_id
                AND c._ts >= @start_ts
        """
        params = [
            {"name": "@type", "value": "health_snapshot"},
            {"name": "@session_id", "value": self.SESSION_ID},
            {"name": "@project_id", "value": project_id},
            {"name": "@start_ts", "value": start_ts},
        ]
        items = container.query_items(
            query=raw_query,
            parameters=params,
            partition_key=self.SESSION_ID,
        )

        buckets: Dict[int, Dict[str, Any]] = {}
        async for item in items:
            ts = int(item.get("_ts", 0) or 0)
            bucket = (ts // bucket_seconds) * bucket_seconds

            health = float(item.get("health_score", 0.0) or 0.0)
            err_rate = float(item.get("error_rate", 0.0) or 0.0)
            completion = float(item.get("completion_rate", 0.0) or 0.0)
            errors = item.get("errors") if isinstance(item.get("errors"), list) else []

            agg = buckets.setdefault(
                bucket,
                {
                    "bucket": bucket,
                    "count": 0,
                    "health_sum": 0.0,
                    "health_min": health,
                    "health_max": health,
                    "error_rate_sum": 0.0,
                    "completion_rate_sum": 0.0,
                    "error_count_sum": 0.0,
                },
            )

            agg["count"] += 1
            agg["health_sum"] += health
            agg["health_min"] = min(agg["health_min"], health)
            agg["health_max"] = max(agg["health_max"], health)
            agg["error_rate_sum"] += err_rate
            agg["completion_rate_sum"] += completion
            agg["error_count_sum"] += len(errors)

        timeline: List[Dict[str, Any]] = []
        for bucket in sorted(buckets):
            agg = buckets[bucket]
            count = max(int(agg["count"]), 1)
            timeline.append(
                {
                    "bucket": bucket,
                    "bucket_timestamp": datetime.utcfromtimestamp(bucket).isoformat(),
                    "avg_health_score": agg["health_sum"] / count,
                    "min_health_score": agg["health_min"],
                    "max_health_score": agg["health_max"],
                    "avg_error_rate": agg["error_rate_sum"] / count,
                    "avg_completion_rate": agg["completion_rate_sum"] / count,
                    "avg_error_count": agg["error_count_sum"] / count,
                    "sample_count": count,
                }
            )

        return timeline

    def _calculate_summary(self, timeline: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate overall trend summary."""
        if not timeline:
            return {
                "avg_health_score": 0.0,
                "min_health_score": 0.0,
                "max_health_score": 0.0,
                "avg_error_rate": 0.0,
                "trend": "stable",
            }

        health_scores = [t["avg_health_score"] for t in timeline]
        error_rates = [t.get("avg_error_rate", 0) for t in timeline]

        return {
            "avg_health_score": sum(health_scores) / len(health_scores),
            "min_health_score": min(health_scores),
            "max_health_score": max(health_scores),
            "avg_error_rate": sum(error_rates) / len(error_rates),
            "trend": self._calculate_trend(health_scores),
        }

    def _calculate_trend(self, values: List[float]) -> str:
        """Determine trend direction."""
        if len(values) < 2:
            return "stable"

        # Compare first half vs second half
        mid = len(values) // 2
        first_half_avg = sum(values[:mid]) / mid if mid > 0 else values[0]
        second_half_avg = sum(values[mid:]) / (len(values) - mid)

        diff = second_half_avg - first_half_avg
        if diff > 2:
            return "improving"
        elif diff < -2:
            return "degrading"
        else:
            return "stable"

    def _detect_anomalies(self, timeline: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Detect anomalies in timeline."""
        anomalies = []

        if not timeline or len(timeline) < 3:
            return anomalies

        health_scores = [t["avg_health_score"] for t in timeline]
        avg = sum(health_scores) / len(health_scores)
        stddev = (sum((x - avg) ** 2 for x in health_scores) / len(health_scores)) ** 0.5

        if math.isclose(stddev, 0.0):
            return anomalies

        # Flag points more than 2 stddevs from mean
        for i, datapoint in enumerate(timeline):
            score = datapoint["avg_health_score"]
            if abs(score - avg) > 2 * stddev:
                anomalies.append({
                    "timestamp": datapoint["bucket_timestamp"],
                    "health_score": score,
                    "deviation": abs(score - avg) / stddev if stddev > 0 else 0,
                    "severity": "high" if abs(score - avg) > 3 * stddev else "medium",
                })

        return anomalies
