"""App Health Monitor - Orchestrates health checks across all providers.

This module provides centralized health monitoring that automatically discovers
and checks all providers registered in ToolRegistry. It's designed to scale
automatically as new providers are added.
"""
import asyncio
import logging
import os
from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import dataclass, field

from tool_registry import ToolRegistry
from observability.provider_health_checker import ProviderHealthChecker, ProviderHealthMetrics
from observability.firestore_health_checker import FirestoreHealthChecker
from observability.s3_health_checker import S3HealthChecker

logger = logging.getLogger(__name__)


@dataclass
class AppHealthSnapshot:
    """Complete health snapshot of the application."""

    timestamp: datetime
    overall_health: bool
    health_score: float  # 0.0 to 100.0
    provider_health: Dict[str, ProviderHealthMetrics] = field(default_factory=dict)
    app_kpis: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "overall_health": self.overall_health,
            "health_score": self.health_score,
            "provider_health": {
                k: v.to_dict() for k, v in self.provider_health.items()
            },
            "app_kpis": self.app_kpis,
            "errors": self.errors,
        }


class AppHealthMonitor:
    """Centralized health monitoring system.

    Architecture:
    1. Auto-discovers providers from ToolRegistry
    2. Instantiates appropriate health checker for each provider
    3. Runs health checks in parallel
    4. Calculates aggregate health score
    5. Stores snapshots for trend analysis (future)

    Design principles:
    - Zero configuration: Discovers providers automatically
    - Extensible: New providers get health checks without code changes
    - Non-blocking: All checks run with timeouts
    - Informative: Returns actionable metrics
    """

    # Registry of health checker classes by provider_id
    _health_checker_registry: Dict[str, type] = {
        "firestore": FirestoreHealthChecker,
        "aws_s3": S3HealthChecker,
        # Auto-register new checkers here or via registration method
    }

    def __init__(
        self,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        enabled_providers: Optional[List[str]] = None,
    ):
        """Initialize health monitor.

        Args:
            project_id: Optional project to scope checks to
            session_id: Optional session for context
            enabled_providers: Optional list of provider_ids to check.
                              If None, checks all registered providers.
        """
        self.project_id = project_id
        self.session_id = session_id
        self.enabled_providers = enabled_providers

    @classmethod
    def register_health_checker(
        cls,
        provider_id: str,
        checker_class: type,
    ) -> None:
        """Register a health checker for a provider.

        This allows plugins to register custom health checkers.

        Args:
            provider_id: Provider identifier (e.g., 'salesforce')
            checker_class: Class that extends ProviderHealthChecker
        """
        if not issubclass(checker_class, ProviderHealthChecker):
            raise TypeError(
                f"checker_class must extend ProviderHealthChecker, "
                f"got {checker_class}"
            )

        cls._health_checker_registry[provider_id] = checker_class
        logger.info(f"Registered health checker for provider: {provider_id}")

    def _get_active_providers(self) -> List[str]:
        """Get list of providers to check.

        Returns:
            List of provider_ids to health check
        """
        if self.enabled_providers:
            return self.enabled_providers

        # Auto-discover from ToolRegistry
        all_providers = ToolRegistry.get_all_providers()
        return [p.provider_id for p in all_providers if p.provider_id in self._health_checker_registry]

    def _create_health_checker(
        self,
        provider_id: str,
    ) -> Optional[ProviderHealthChecker]:
        """Create a health checker instance for a provider.

        Args:
            provider_id: Provider to create checker for

        Returns:
            ProviderHealthChecker instance or None if not registered
        """
        checker_class = self._health_checker_registry.get(provider_id)
        if not checker_class:
            logger.warning(
                f"No health checker registered for provider: {provider_id}"
            )
            return None

        try:
            return checker_class(
                provider_id=provider_id,
                project_id=self.project_id,
                session_id=self.session_id,
            )
        except Exception as e:
            logger.error(
                f"Failed to create health checker for {provider_id}: {e}"
            )
            return None

    async def collect_provider_health(
        self,
        provider_id: str,
        timeout_seconds: float = 35.0,
    ) -> Optional[ProviderHealthMetrics]:
        """Collect health metrics for a single provider.

        Args:
            provider_id: Provider to check

        Returns:
            ProviderHealthMetrics or None if check failed
        """
        checker = self._create_health_checker(provider_id)
        if not checker:
            return None

        try:
            # Run health check with extended timeout to allow retries
            # _timed_check() does 3 retries with 10s timeout each = max 30s + backoff
            health = await asyncio.wait_for(checker.check_health(), timeout=timeout_seconds)
            return health
        except asyncio.TimeoutError:
            logger.error(f"Health check timeout for provider: {provider_id}")
            return ProviderHealthMetrics(
                provider_id=provider_id,
                is_healthy=False,
                response_time_ms=35000.0,
                error_message="Health check timeout after retries",
            )
        except Exception as e:
            logger.exception(f"Health check failed for {provider_id}: {e}")
            return ProviderHealthMetrics(
                provider_id=provider_id,
                is_healthy=False,
                response_time_ms=0.0,
                error_message=str(e),
            )

    async def collect_all_provider_health(
        self,
        timeout_seconds: float = 35.0,
    ) -> Dict[str, ProviderHealthMetrics]:
        """Collect health metrics from all active providers in parallel.

        Returns:
            Dict mapping provider_id to ProviderHealthMetrics
        """
        active_providers = self._get_active_providers()

        if not active_providers:
            logger.warning("No active providers to health check")
            return {}

        # Run all health checks in parallel
        tasks = [
            self.collect_provider_health(provider_id, timeout_seconds=timeout_seconds)
            for provider_id in active_providers
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Build result dict, filtering out None/exceptions
        health_map = {}
        for provider_id, result in zip(active_providers, results):
            if isinstance(result, Exception):
                logger.error(f"Exception checking {provider_id}: {result}")
                health_map[provider_id] = ProviderHealthMetrics(
                    provider_id=provider_id,
                    is_healthy=False,
                    response_time_ms=0.0,
                    error_message=str(result),
                )
            elif result:
                health_map[provider_id] = result

        return health_map

    def _calculate_health_score(
        self,
        provider_health: Dict[str, ProviderHealthMetrics],
    ) -> float:
        """Calculate aggregate health score from provider health.

        Args:
            provider_health: Map of provider health metrics

        Returns:
            Health score from 0.0 to 100.0
        """
        if not provider_health:
            return 100.0  # No providers = no problems

        healthy_count = sum(
            1 for h in provider_health.values() if h.is_healthy
        )
        total_count = len(provider_health)

        return (healthy_count / total_count) * 100.0

    async def collect_app_kpis(self) -> Dict[str, Any]:
        """Collect application-level KPIs from Cosmos DB.

        Returns:
            Dict of KPI metrics from real data
        """
        from context.cosmos_memory_kernel import CosmosMemoryContext

        try:
            # Initialize Cosmos context
            cosmos = CosmosMemoryContext(
                session_id=self.session_id or "health_check",
                user_id="system"
            )

            # Initialize Cosmos before querying
            await cosmos.ensure_initialized()

            # Get real metrics from Cosmos
            all_plans = await cosmos.get_all_plans()
            all_sessions = await cosmos.get_all_sessions()

            # Calculate operational metrics
            total_plans = len(all_plans)
            in_progress_plans = sum(
                1 for p in all_plans
                if getattr(p, 'overall_status', None)
                and str(getattr(p.overall_status, 'value', p.overall_status)).lower() == "in_progress"
            )
            completed_plans = sum(
                1 for p in all_plans
                if getattr(p, 'overall_status', None)
                and str(getattr(p.overall_status, 'value', p.overall_status)).lower() == "completed"
            )
            failed_plans = sum(
                1 for p in all_plans
                if getattr(p, 'overall_status', None)
                and str(getattr(p.overall_status, 'value', p.overall_status)).lower() == "failed"
            )

            # Get steps metrics
            all_steps = []
            for plan in all_plans:
                try:
                    steps = await cosmos.get_steps_by_plan(plan.id)
                    all_steps.extend(steps)
                except Exception as step_e:
                    logger.debug(f"Could not get steps for plan {plan.id}: {step_e}")

            total_steps = len(all_steps)
            completed_steps = sum(
                1 for s in all_steps
                if s.status and str(getattr(s.status, 'value', s.status)).lower() == "completed"
            )
            failed_steps = sum(
                1 for s in all_steps
                if s.status and str(getattr(s.status, 'value', s.status)).lower() == "failed"
            )

            # Business metrics for trending
            from datetime import timedelta
            active_sessions = sum(
                1 for s in all_sessions
                if (hasattr(s, 'timestamp') and s.timestamp
                    and (datetime.utcnow() - s.timestamp < timedelta(hours=1)))
            ) if all_sessions else len(all_sessions)

            completion_rate = (completed_plans / total_plans * 100) if total_plans > 0 else 0.0
            success_rate = (completed_plans / (completed_plans + failed_plans) * 100) if (completed_plans + failed_plans) > 0 else 0.0

            return {
                # Operational metrics
                "total_sessions": len(all_sessions),
                "active_sessions": active_sessions,
                "total_plans": total_plans,
                "plans_in_progress": in_progress_plans,
                "plans_completed": completed_plans,
                "plans_failed": failed_plans,
                "total_steps": total_steps,
                "steps_completed": completed_steps,
                "steps_failed": failed_steps,

                # Business metrics (for trending analysis)
                "error_rate": (failed_steps / total_steps * 100) if total_steps > 0 else 0.0,
                "completion_rate": completion_rate,
                "success_rate": success_rate,
                "avg_steps_per_plan": (total_steps / total_plans) if total_plans > 0 else 0.0,
                "engagement_ratio": (active_sessions / len(all_sessions) * 100) if all_sessions else 0.0,

                "last_snapshot": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            logger.error(f"Failed to collect app KPIs from Cosmos: {e}")
            return {
                "error": str(e),
                "last_snapshot": datetime.utcnow().isoformat(),
            }

    async def get_health_snapshot(self, mode: str = "observability") -> AppHealthSnapshot:
        """Get complete health snapshot of the application.

        This is the main entry point for health monitoring.

        Returns:
            AppHealthSnapshot with all health data
        """
        is_prompt_mode = mode == "prompt"
        logger.info("Collecting health snapshot (mode=%s)...", mode)

        prompt_timeout = float(os.getenv("HEALTH_PROMPT_PROVIDER_TIMEOUT_SECONDS", "8"))
        observability_timeout = float(
            os.getenv("HEALTH_OBSERVABILITY_PROVIDER_TIMEOUT_SECONDS", "35")
        )
        timeout_seconds = prompt_timeout if is_prompt_mode else observability_timeout

        # Collect provider health in parallel
        provider_health = await self.collect_all_provider_health(
            timeout_seconds=timeout_seconds
        )

        # Collect app KPIs
        app_kpis: Dict[str, Any] = {}
        if not is_prompt_mode:
            try:
                app_kpis = await self.collect_app_kpis()
            except Exception as e:
                logger.error(f"Failed to collect app KPIs: {e}")
                app_kpis = {"error": str(e)}

        # Calculate health score
        health_score = self._calculate_health_score(provider_health)
        overall_health = health_score >= 75.0  # 75% threshold

        # Collect errors
        errors = [
            f"{p_id}: {health.error_message}"
            for p_id, health in provider_health.items()
            if not health.is_healthy and health.error_message
        ]

        snapshot = AppHealthSnapshot(
            timestamp=datetime.utcnow(),
            overall_health=overall_health,
            health_score=health_score,
            provider_health=provider_health,
            app_kpis=app_kpis,
            errors=errors,
        )

        logger.info(
            f"Health snapshot collected: "
            f"score={health_score:.1f}%, "
            f"providers={len(provider_health)}, "
            f"errors={len(errors)}"
        )

        return snapshot
