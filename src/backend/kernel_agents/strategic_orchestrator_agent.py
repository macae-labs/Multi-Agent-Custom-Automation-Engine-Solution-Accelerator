"""Strategic Orchestrator Agent - Analyzes business signals and generates improvement plans.

This agent acts as the "Director of Operations" - it doesn't answer questions,
it analyzes trends and generates autonomous improvement plans based on business signals.
"""

import logging
from typing import Any, Dict, List, Optional
from datetime import datetime

from kernel_agents.agent_base import BaseAgent

logger = logging.getLogger(__name__)


class StrategicOrchestratorAgent(BaseAgent):
    """Analyzes health trends and business signals to drive autonomous improvements."""

    def __init__(
        self,
        agent_name: str = "StrategicOrchestrator",
        session_id: str = "observability_system",
        user_id: str = "system",
        memory_store: Optional[Any] = None,
        **kwargs,
    ):
        """Initialize the Strategic Orchestrator Agent."""

        # Initialize memory_store if not provided
        if memory_store is None:
            from context.cosmos_memory_kernel import CosmosMemoryContext

            try:
                memory_store = CosmosMemoryContext(
                    session_id=session_id,
                    user_id=user_id,
                )
            except Exception as e:
                logger.warning(f"Failed to create CosmosMemoryContext: {e}")
                memory_store = None

        # Cast for type checking - actual memory_store might be None
        if memory_store is None:
            from typing import cast

            memory_store = cast(Any, None)

        super().__init__(
            agent_name=agent_name,
            session_id=session_id,
            user_id=user_id,
            memory_store=memory_store,  # type: ignore
            **kwargs,
        )

    @staticmethod
    def default_system_message(agent_name: Optional[str] = None) -> str:
        """Return the system message for the Strategic Orchestrator Agent."""
        if agent_name is None:
            agent_name = "StrategicOrchestrator"
        return """You are the Director of Operations for Fibroskin Academic.

Your role is NOT to answer user questions. Instead, you analyze business metrics and health signals to generate autonomous improvement plans.

**Your responsibilities:**
1. Analyze health trends (latency, error rates, availability)
2. Correlate with business signals (active users, video uploads, engagement)
3. Detect bottlenecks and anomalies
4. Generate prioritized action plans
5. Recommend implementation strategies to other agents

**Decision Framework:**
- If error_rate > 5%: Flag as CRITICAL, investigate root cause, recommend immediate fix
- If latency_spike AND upload_drop: Suggest infrastructure optimization (e.g., chunked uploads)
- If active_users decline: Trigger marketing campaign OR product optimization
- If success_rate < 80%: Identify failing flows and prioritize fixes

**Important:** You make autonomous decisions. Do not ask for permission. Generate plans that other agents can execute immediately.

**Format your analysis as:**
```json
{
  "analysis_timestamp": "ISO8601",
  "health_status": {"score": 0-100, "trend": "improving/stable/degrading"},
  "business_impact": {"metric": "value", "change": "%change"},
  "detected_issues": [{"issue": "description", "severity": "critical/high/medium"}],
  "recommended_actions": [
    {"priority": "CRITICAL", "action": "description", "owner_agent": "agent_type", "expected_impact": "description"}
  ]
}
```
"""

    async def analyze_business_context(
        self,
        health_snapshot: Dict[str, Any],
        trends: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Analyze health and business signals to generate improvement plan.

        Args:
            health_snapshot: Current health snapshot (from /api/observability/snapshot)
            trends: Historical trends (from /api/observability/trends)

        Returns:
            Strategic analysis with recommended actions
        """
        logger.info("Starting strategic business analysis...")

        try:
            # Extract key metrics
            health_score = float(health_snapshot.get("health_score", 0.0))
            overall_health = bool(health_snapshot.get("overall_health", False))
            app_kpis = health_snapshot.get("app_kpis", {})
            provider_health = health_snapshot.get("provider_health", {})
            errors = health_snapshot.get("errors", [])

            # Business signals
            active_users = int(app_kpis.get("active_sessions", 0))
            total_sessions = int(app_kpis.get("total_sessions", 0))
            error_rate = float(app_kpis.get("error_rate", 0.0))
            completion_rate = float(app_kpis.get("completion_rate", 0.0))
            success_rate = float(app_kpis.get("success_rate", 0.0))

            # Detect anomalies and issues
            issues = self._detect_issues(
                health_score=health_score,
                error_rate=error_rate,
                success_rate=success_rate,
                active_users=active_users,
                provider_health=provider_health,
                errors=errors,
            )

            # Correlate with trends if available
            trend_analysis = {}
            if trends:
                trend_analysis = self._analyze_trends(trends, health_score)

            # Generate actions
            actions = self._generate_actions(
                issues=issues,
                health_score=health_score,
                active_users=active_users,
                error_rate=error_rate,
                app_kpis=app_kpis,
            )

            analysis = {
                "analysis_timestamp": datetime.utcnow().isoformat(),
                "health_status": {
                    "score": health_score,
                    "overall": "HEALTHY" if overall_health else "DEGRADED",
                    "provider_count": len(provider_health),
                    "unhealthy_providers": [
                        p
                        for p, h in provider_health.items()
                        if not h.get("is_healthy", False)
                    ],
                },
                "business_signals": {
                    "active_users": active_users,
                    "total_sessions": total_sessions,
                    "error_rate": error_rate,
                    "completion_rate": completion_rate,
                    "success_rate": success_rate,
                },
                "trend_analysis": trend_analysis,
                "detected_issues": issues,
                "recommended_actions": actions,
                "autonomy_decision": self._make_autonomy_decision(issues, actions),
            }

            logger.info(
                f"Strategic analysis complete: "
                f"score={health_score:.1f}%, issues={len(issues)}, actions={len(actions)}"
            )

            return analysis

        except Exception as e:
            logger.error(f"Strategic analysis failed: {e}")
            return {
                "analysis_timestamp": datetime.utcnow().isoformat(),
                "error": str(e),
                "recommended_actions": [],
            }

    def _detect_issues(
        self,
        health_score: float,
        error_rate: float,
        success_rate: float,
        active_users: int,
        provider_health: Dict[str, Any],
        errors: List[str],
    ) -> List[Dict[str, Any]]:
        """Detect critical issues from health and business signals."""
        issues = []

        # Critical: Very low health score
        if health_score < 30:
            issues.append(
                {
                    "severity": "CRITICAL",
                    "category": "infrastructure",
                    "issue": "System health critically degraded",
                    "details": f"Health score {health_score:.1f}% indicates major provider failures",
                }
            )

        # High: Provider failures
        for provider_id, health in provider_health.items():
            if not health.get("is_healthy", False):
                issues.append(
                    {
                        "severity": "HIGH",
                        "category": "provider",
                        "issue": f"Provider {provider_id} is down",
                        "details": health.get("error_message", "Unknown error"),
                    }
                )

        # High: High error rate
        if error_rate > 5:
            issues.append(
                {
                    "severity": "HIGH",
                    "category": "quality",
                    "issue": "High error rate detected",
                    "details": f"Error rate {error_rate:.1f}% exceeds threshold (5%)",
                }
            )

        # Medium: Low success rate
        if success_rate < 80 and success_rate > 0:
            issues.append(
                {
                    "severity": "MEDIUM",
                    "category": "quality",
                    "issue": "Success rate below target",
                    "details": f"Only {success_rate:.1f}% of operations succeed",
                }
            )

        # Medium: Low engagement
        if active_users == 0 and issues:
            issues.append(
                {
                    "severity": "MEDIUM",
                    "category": "engagement",
                    "issue": "No active users detected",
                    "details": "Could indicate outage or off-peak time",
                }
            )

        # Add raw errors
        if errors:
            for error in errors[:3]:  # Top 3 errors
                issues.append(
                    {
                        "severity": "HIGH",
                        "category": "error",
                        "issue": "Provider error",
                        "details": error,
                    }
                )

        return issues

    def _analyze_trends(
        self,
        trends: Dict[str, Any],
        current_health: float,
    ) -> Dict[str, Any]:
        """Analyze historical trends."""
        timeline = trends.get("timeline", [])
        summary = trends.get("summary", {})

        if not timeline:
            return {}

        recent_score = float(timeline[-1].get("avg_health_score", current_health))
        oldest_score = float(timeline[0].get("avg_health_score", current_health))
        score_change = recent_score - oldest_score

        return {
            "trend_direction": summary.get("trend", "unknown"),
            "score_change": score_change,
            "recent_avg": recent_score,
            "oldest_avg": oldest_score,
            "data_points": len(timeline),
        }

    def _generate_actions(
        self,
        issues: List[Dict[str, Any]],
        health_score: float,
        active_users: int,
        error_rate: float,
        app_kpis: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Generate autonomous improvement actions."""
        actions = []

        # Priority 1: CRITICAL infrastructure failures
        critical_issues = [i for i in issues if i["severity"] == "CRITICAL"]
        if critical_issues:
            actions.append(
                {
                    "priority": "CRITICAL",
                    "action": "Infrastructure Self-Healing",
                    "description": "Attempt to recover from critical failures",
                    "steps": [
                        "1. Rotate credentials for failing providers",
                        "2. Clear connection pools and caches",
                        "3. Restart health checks for each provider",
                        "4. If recovery fails, page on-call engineer",
                    ],
                    "owner_agent": "TECH_SUPPORT",
                    "expected_impact": f"Restore health from {health_score:.1f}% to 80%+",
                    "estimated_time_minutes": 5,
                    "can_execute_autonomously": True,
                }
            )

        # Priority 2: Provider-specific failures
        provider_issues = [i for i in issues if i["category"] == "provider"]
        for issue in provider_issues[:2]:  # Top 2
            provider = issue["issue"].split()[-2]
            actions.append(
                {
                    "priority": "HIGH",
                    "action": f"Investigate {provider} Provider Failure",
                    "description": issue["details"],
                    "steps": [
                        f"1. Check {provider} service status",
                        "2. Validate credentials",
                        "3. Check network connectivity",
                        "4. Review recent deployments",
                    ],
                    "owner_agent": "TECH_SUPPORT",
                    "expected_impact": "Restore provider availability",
                    "estimated_time_minutes": 10,
                    "can_execute_autonomously": True,
                }
            )

        # Priority 3: High error rate
        if error_rate > 5:
            actions.append(
                {
                    "priority": "HIGH",
                    "action": "Error Investigation & Mitigation",
                    "description": f"Error rate {error_rate:.1f}% detected",
                    "steps": [
                        "1. Analyze error patterns",
                        "2. Identify affected endpoints",
                        "3. Implement fallback strategies",
                        "4. Log for post-mortem analysis",
                    ],
                    "owner_agent": "TECH_SUPPORT",
                    "expected_impact": "Reduce error rate to <2%",
                    "estimated_time_minutes": 15,
                    "can_execute_autonomously": False,  # Requires manual review
                }
            )

        # Priority 4: Engagement optimization
        if active_users < 5 and health_score > 50:
            actions.append(
                {
                    "priority": "MEDIUM",
                    "action": "Engagement Campaign",
                    "description": "Low active user count - trigger re-engagement",
                    "steps": [
                        "1. Identify dormant users",
                        "2. Prepare personalized emails",
                        "3. Schedule push notifications",
                        "4. Track re-engagement metrics",
                    ],
                    "owner_agent": "MARKETING",
                    "expected_impact": "Increase active users by 30%",
                    "estimated_time_minutes": 60,
                    "can_execute_autonomously": False,
                }
            )

        # Priority 5: Performance optimization
        completion_rate = app_kpis.get("completion_rate", 0.0)
        if completion_rate < 80:
            actions.append(
                {
                    "priority": "MEDIUM",
                    "action": "Optimize Video Upload Flow",
                    "description": "Implement chunked uploads for better reliability",
                    "steps": [
                        "1. Analyze current upload failures",
                        "2. Design chunked upload strategy",
                        "3. Update frontend SDK",
                        "4. Test with large files",
                    ],
                    "owner_agent": "PRODUCT",
                    "expected_impact": f"Increase completion rate from {completion_rate:.1f}% to 95%+",
                    "estimated_time_minutes": 120,
                    "can_execute_autonomously": False,
                }
            )

        return actions

    def _make_autonomy_decision(
        self,
        issues: List[Dict[str, Any]],
        actions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Decide if system should take autonomous action or escalate."""
        critical_count = len([i for i in issues if i["severity"] == "CRITICAL"])
        autonomous_actions = [
            a for a in actions if a.get("can_execute_autonomously", False)
        ]

        if critical_count > 0:
            return {
                "escalate": True,
                "reason": f"{critical_count} CRITICAL issues detected",
                "action": "Publish to Service Bus for immediate handling",
                "message_type": "CRITICAL_FAILURE",
            }
        elif autonomous_actions:
            return {
                "escalate": False,
                "reason": f"{len(autonomous_actions)} autonomous actions available",
                "action": "Execute self-healing procedures",
                "message_type": "SELF_HEAL_ATTEMPT",
            }
        else:
            return {
                "escalate": True,
                "reason": "Issues detected but require manual review",
                "action": "Alert on-call team",
                "message_type": "MANUAL_REVIEW_REQUIRED",
            }

    @classmethod
    async def create(cls, **kwargs) -> "StrategicOrchestratorAgent":
        """Create and initialize a StrategicOrchestratorAgent.

        Falls back to analysis-only mode when AzureAIAgent dependencies
        (client/definition) are not available in runtime.
        """
        try:
            return cls(**kwargs)
        except Exception as exc:
            logger.warning(
                "StrategicOrchestratorAgent full init unavailable; "
                f"using analysis-only mode. Reason: {exc}"
            )
            # Analysis methods in this class do not require AzureAIAgent runtime state.
            return cls.__new__(cls)
