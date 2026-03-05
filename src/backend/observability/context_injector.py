"""Context Injector - Inject health snapshots into agent context.

This module provides:
1. Injecting latest health snapshot into agent system context
2. Enabling agents to make decisions based on app health status
3. Providing observability data to all agent types without manual wiring
"""
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class HealthAwareContextInjector:
    """Injects health context into agent initialization."""

    @staticmethod
    def inject_health_snapshot(
        agent_system_context: str,
        health_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Inject health status into agent's system context.

        This enriches the agent's understanding of app health before
        it starts making decisions.

        Args:
            agent_system_context: Original system context string
            health_snapshot: Health snapshot dict (from AppHealthMonitor)

        Returns:
            Enhanced system context with health information
        """
        if not health_snapshot:
            return agent_system_context

        try:
            health_section = HealthAwareContextInjector._build_health_section(
                health_snapshot
            )

            # Inject health context right after system prompt but before task description
            enhanced_context = (
                f"{agent_system_context}\n\n"
                f"## Current Application Health Status\n"
                f"{health_section}"
            )

            return enhanced_context

        except Exception as e:
            logger.error(f"Error injecting health context: {e}")
            return agent_system_context

    @staticmethod
    def _build_health_section(snapshot: Dict[str, Any]) -> str:
        """Build human-readable health section for context."""
        try:
            timestamp = snapshot.get("timestamp", "unknown")
            overall_health = snapshot.get("overall_health", False)
            health_score = snapshot.get("health_score", 0)
            provider_health = snapshot.get("provider_health", {})
            app_kpis = snapshot.get("app_kpis", {})
            errors = snapshot.get("errors", [])

            # Build status indicator
            status_icon = "✅" if overall_health else "⚠️"
            health_status = (
                "HEALTHY" if overall_health else "DEGRADED"
            )

            section = f"""
**Status**: {status_icon} {health_status}
**Score**: {health_score:.1f}/100.0
**Last Updated**: {timestamp}

### Provider Health:
"""
            for provider_id, health in provider_health.items():
                provider_dict = (
                    health.to_dict() if hasattr(health, "to_dict") else health
                )
                provider_status = (
                    "✅ OK" if provider_dict.get("is_healthy") else "❌ DOWN"
                )
                response_time = provider_dict.get("response_time_ms", 0)
                section += (
                    f"- **{provider_id}**: {provider_status} "
                    f"(response: {response_time:.0f}ms)\n"
                )

            # Add KPIs if available
            if app_kpis and not app_kpis.get("error"):
                section += "\n### Application Metrics:\n"
                if "total_sessions" in app_kpis:
                    section += (
                        f"- **Active Sessions**: "
                        f"{app_kpis['active_sessions']}/{app_kpis['total_sessions']}\n"
                    )
                if "plans_completed" in app_kpis:
                    section += (
                        f"- **Plans**: "
                        f"{app_kpis['plans_completed']} completed, "
                        f"{app_kpis['plans_failed']} failed\n"
                    )
                if "error_rate" in app_kpis:
                    section += (
                        f"- **Error Rate**: {app_kpis['error_rate']:.1f}%\n"
                    )
                if "completion_rate" in app_kpis:
                    section += (
                        f"- **Completion Rate**: {app_kpis['completion_rate']:.1f}%\n"
                    )

            # Add alerts if any errors
            if errors:
                section += "\n### ⚠️ Active Issues:\n"
                for error in errors[:3]:  # Show top 3
                    section += f"- {error}\n"
                if len(errors) > 3:
                    section += f"- ... and {len(errors) - 3} more issues\n"

            section += (
                "\n**Note**: Consider this information when making decisions. "
                "If services are degraded, prefer robust retry strategies."
            )

            return section

        except Exception as e:
            logger.error(f"Error building health section: {e}")
            return "⚠️ Health status unavailable"

    @staticmethod
    def create_health_aware_system_prompt(
        base_role: str,
        health_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create a system prompt that includes health awareness.

        Args:
            base_role: Agent's base role description
            health_snapshot: Optional health snapshot

        Returns:
            Complete system prompt with health context
        """
        base_prompt = f"{base_role}\n"

        if health_snapshot:
            health_section = (
                HealthAwareContextInjector._build_health_section(health_snapshot)
            )
            return (
                f"{base_prompt}\n"
                f"## Current System Health\n"
                f"{health_section}"
            )

        return base_prompt


class AgentHealthDecisionHelper:
    """Helper for agents to make health-aware decisions."""

    @staticmethod
    def should_attempt_operation(
        health_snapshot: Dict[str, Any],
        required_providers: list,
        operation_name: str,
    ) -> tuple[bool, str]:
        """Determine if operation should proceed based on health.

        Args:
            health_snapshot: Current health snapshot
            required_providers: List of provider_ids needed for operation
            operation_name: Name of operation (for logging)

        Returns:
            Tuple of (should_proceed: bool, reason: str)
        """
        try:
            if not health_snapshot:
                return True, "No health data available, proceeding"

            health_score = health_snapshot.get("health_score", 50)
            provider_health = health_snapshot.get("provider_health", {})

            # Check required providers
            missing_providers = []
            for provider_id in required_providers:
                provider_info = provider_health.get(provider_id)
                if not provider_info:
                    missing_providers.append(provider_id)
                    continue

                provider_dict = (
                    provider_info.to_dict()
                    if hasattr(provider_info, "to_dict")
                    else provider_info
                )
                if not provider_dict.get("is_healthy"):
                    missing_providers.append(provider_id)

            if missing_providers:
                return (
                    False,
                    f"{operation_name} blocked: "
                    f"providers unavailable: {', '.join(missing_providers)}",
                )

            # Check overall score
            if health_score < 50:
                return (
                    False,
                    f"{operation_name} blocked: overall health critical ({health_score:.0f}%)",
                )

            if health_score < 75:
                return (
                    True,
                    f"{operation_name} proceeding with caution: "
                    f"health degraded ({health_score:.0f}%)",
                )

            return True, f"{operation_name} healthy, proceeding normally"

        except Exception as e:
            logger.error(f"Error checking operation health: {e}")
            return True, "Could not evaluate health, proceeding"

    @staticmethod
    def get_retry_strategy(
        health_snapshot: Dict[str, Any],
        default_max_retries: int = 3,
    ) -> Dict[str, Any]:
        """Get recommended retry strategy based on health.

        Args:
            health_snapshot: Current health snapshot
            default_max_retries: Default number of retries

        Returns:
            Retry strategy configuration
        """
        if not health_snapshot:
            return {
                "max_retries": default_max_retries,
                "initial_backoff_ms": 1000,
                "max_backoff_ms": 30000,
            }

        health_score = health_snapshot.get("health_score", 100)

        # Scale retry strategy based on health
        if health_score > 90:
            # System healthy, minimal retries needed
            return {
                "max_retries": 2,
                "initial_backoff_ms": 500,
                "max_backoff_ms": 10000,
            }
        elif health_score > 75:
            # System good, normal retries
            return {
                "max_retries": default_max_retries,
                "initial_backoff_ms": 1000,
                "max_backoff_ms": 30000,
            }
        elif health_score > 50:
            # System degraded, more aggressive retries
            return {
                "max_retries": 5,
                "initial_backoff_ms": 2000,
                "max_backoff_ms": 60000,
            }
        else:
            # System critical, maximum retries with longer backoff
            return {
                "max_retries": 7,
                "initial_backoff_ms": 5000,
                "max_backoff_ms": 120000,
            }
