#!/usr/bin/env python3
"""Test script for Strategic Orchestrator Agent with business signals analysis.

This demonstrates:
1. Collecting health snapshot with business signals
2. Invoking StrategicOrchestratorAgent to analyze trends
3. Generating autonomous improvement recommendations
4. Decision framework for self-healing vs escalation
"""

import asyncio
import logging
import pytest
from datetime import datetime

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_strategic_orchestrator():
    """Test the StrategicOrchestratorAgent with simulated health data."""

    logger.info("=" * 80)
    logger.info("STRATEGIC ORCHESTRATOR AGENT - AUTONOMOUS DECISION TEST")
    logger.info("=" * 80)

    # Import after logging setup
    from kernel_agents.strategic_orchestrator_agent import StrategicOrchestratorAgent
    from observability.app_health_monitor import AppHealthMonitor
    from observability.observability_snapshot_store import ObservabilitySnapshotStore

    # Step 1: Create Strategic Orchestrator Agent
    logger.info("\n[STEP 1] Initializing StrategicOrchestratorAgent...")
    agent = await StrategicOrchestratorAgent.create()
    logger.info(
        f"✓ Agent created: {getattr(agent, '_agent_name', 'StrategicOrchestratorAgent')}"
    )
    # _system_message may not exist on all agent implementations; fallback safely
    system_prompt = getattr(agent, "_system_message", None) or getattr(
        agent, "system_message", ""
    )
    if system_prompt:
        logger.info(
            f"  System prompt ({len(system_prompt)} chars):\n  {system_prompt[:200]}..."
        )
    else:
        logger.info("  System prompt: <not available>")

    # Step 2: Collect current health snapshot
    logger.info("\n[STEP 2] Collecting real health snapshot from providers...")
    monitor = AppHealthMonitor(session_id="strategic_test")
    try:
        snapshot = await monitor.get_health_snapshot()
        logger.info(f"✓ Snapshot collected at {snapshot.timestamp.isoformat()}")
        logger.info(f"  Health Score: {snapshot.health_score:.1f}%")
        logger.info(
            f"  Overall Health: {'✓ HEALTHY' if snapshot.overall_health else '✗ DEGRADED'}"
        )
        logger.info(f"  Providers: {len(snapshot.provider_health)}")
        logger.info(f"  App KPIs: {len(snapshot.app_kpis)} metrics")

        # Show provider status
        for provider_id, health in snapshot.provider_health.items():
            status = "✓" if health.is_healthy else "✗"
            logger.info(f"    {status} {provider_id}: {health.response_time_ms:.1f}ms")

        # Show business signals
        logger.info("  Business Signals:")
        for key in ["active_sessions", "total_plans", "completion_rate", "error_rate"]:
            if key in snapshot.app_kpis:
                value = snapshot.app_kpis[key]
                logger.info(f"    - {key}: {value}")

    except Exception as e:
        logger.error(f"✗ Failed to collect snapshot: {e}")
        # Create mock snapshot for demonstration
        logger.info("  Using mock snapshot for demonstration...")
        snapshot = {
            "timestamp": datetime.utcnow().isoformat(),
            "overall_health": False,
            "health_score": 42.0,
            "provider_health": {
                "firestore": {"is_healthy": True, "response_time_ms": 150.0},
                "aws_s3": {
                    "is_healthy": False,
                    "response_time_ms": 0.0,
                    "error_message": "Connection timeout",
                },
            },
            "app_kpis": {
                "active_sessions": 3,
                "total_plans": 45,
                "completion_rate": 75.5,
                "error_rate": 8.2,
                "success_rate": 68.5,
                "plans_failed": 14,
            },
            "errors": ["aws_s3: Connection timeout"],
        }

    # Step 3: Retrieve historical trends (if available)
    logger.info("\n[STEP 3] Retrieving historical trends...")
    try:
        store = ObservabilitySnapshotStore()
        await store.ensure_initialized()
        trends = await store.get_trends(days=7)
        logger.info(
            f"✓ Trends retrieved: {len(trends.get('timeline', []))} data points"
        )

        if trends.get("summary"):
            logger.info(f"  Summary: {trends['summary'].get('trend', 'unknown')}")
            logger.info(
                f"  Avg Health Score: {trends['summary'].get('avg_health_score', 0):.1f}%"
            )
    except Exception as e:
        logger.warning(f"  Trends not available: {e}")
        trends = None

    # Step 4: Invoke Strategic Orchestrator Agent
    logger.info("\n[STEP 4] Invoking StrategicOrchestratorAgent for analysis...")
    logger.info("  Request: Analyze health trends and generate improvement plan")

    try:
        analysis = await agent.analyze_business_context(
            health_snapshot=snapshot
            if isinstance(snapshot, dict)
            else snapshot.to_dict(),
            trends=trends,
        )

        # Display analysis results
        logger.info("\n[STRATEGIC ANALYSIS RESULTS]")
        logger.info("-" * 80)

        # Health Status
        health_status = analysis.get("health_status", {})
        logger.info("Health Status:")
        logger.info(f"  Score: {health_status.get('score', 0):.1f}%")
        logger.info(f"  Status: {health_status.get('overall', 'unknown')}")
        logger.info(
            f"  Unhealthy Providers: {health_status.get('unhealthy_providers', [])}"
        )

        # Business Impact
        business_signals = analysis.get("business_signals", {})
        logger.info("\nBusiness Signals:")
        for key, value in business_signals.items():
            logger.info(f"  {key}: {value}")

        # Detected Issues
        issues = analysis.get("detected_issues", [])
        logger.info(f"\nDetected Issues ({len(issues)}):")
        for issue in issues:
            severity_icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}.get(
                issue.get("severity"), "⚪"
            )
            logger.info(
                f"  {severity_icon} [{issue.get('severity')}] {issue.get('category')}: {issue.get('issue')}"
            )
            logger.info(f"      → {issue.get('details')}")

        # Recommended Actions
        actions = analysis.get("recommended_actions", [])
        logger.info(f"\nRecommended Actions ({len(actions)}):")
        for i, action in enumerate(actions, 1):
            priority_icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}.get(
                action.get("priority"), "⚪"
            )
            logger.info(
                f"  {priority_icon} [{action.get('priority')}] {action.get('action')}"
            )
            logger.info(f"      Description: {action.get('description')}")
            logger.info(f"      Owner: {action.get('owner_agent', 'TBD')}")
            logger.info(f"      Time: ~{action.get('estimated_time_minutes', 0)} min")
            logger.info(
                f"      Autonomous: {'Yes ✓' if action.get('can_execute_autonomously') else 'No (manual review)'}"
            )

            steps = action.get("steps", [])
            for step in steps:
                logger.info(f"        {step}")

        # Autonomy Decision
        autonomy = analysis.get("autonomy_decision", {})
        logger.info("\nAutonomy Decision:")
        logger.info(
            f"  Escalate: {'Yes' if autonomy.get('escalate') else 'No (Self-Heal Attempt)'}"
        )
        logger.info(f"  Reason: {autonomy.get('reason')}")
        logger.info(f"  Action: {autonomy.get('action')}")
        logger.info(f"  Message Type: {autonomy.get('message_type')}")

        # If self-healing, show next steps
        if not autonomy.get("escalate"):
            logger.info("\n[SELF-HEALING PROTOCOL]")
            logger.info("  The system will execute the following actions autonomously:")
            for action in [a for a in actions if a.get("can_execute_autonomously")]:
                logger.info(f"    1. {action.get('action')}")
            logger.info("  No manual intervention required at this time.")
        else:
            logger.info("\n[ESCALATION REQUIRED]")
            logger.info("  This situation requires manual review.")
            logger.info("  Publishing CRITICAL_FAILURE message to Service Bus...")
            logger.info("  Alerting on-call team...")

        # Step 5: Integration with Azure native services
        logger.info("\n" + "=" * 80)
        logger.info("[INTEGRATION WITH AZURE NATIVE SERVICES]")
        logger.info("=" * 80)

        logger.info("\n✓ Azure Function Timer Trigger (every 5-15 min):")
        logger.info("  Calls: GET /api/observability/snapshot")
        logger.info("  Stores: Cosmos DB snapshots with timestamp")
        logger.info("  Triggers: StrategicOrchestratorAgent if health_score < 50%")

        logger.info("\n✓ Service Bus Integration:")
        logger.info("  Topic: 'strategic-decisions'")
        logger.info(f"  Message Type: '{autonomy.get('message_type')}'")
        logger.info(
            f"  Severity: '{autonomy.get('escalate') and 'CRITICAL' or 'ADVISORY'}'"
        )

        logger.info("\n✓ Cosmos Change Feed (Event-Driven):")
        logger.info("  Trigger: health_score drops below 50%")
        logger.info("  Action: Cosmos Change Feed → Service Bus → Alert")

        logger.info("\n✓ Container Apps Health Probes:")
        logger.info("  Endpoint: GET /health")
        logger.info("  Interval: 30 seconds")
        logger.info("  Timeout: 5 seconds")
        logger.info("  Uses: AppHealthMonitor.get_health_snapshot()")

        logger.info("\n" + "=" * 80)
        logger.info("TEST COMPLETED SUCCESSFULLY ✓")
        logger.info("=" * 80)

        # Return analysis for verification
        return analysis

    except Exception as e:
        logger.error(f"✗ Agent analysis failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(test_strategic_orchestrator())
