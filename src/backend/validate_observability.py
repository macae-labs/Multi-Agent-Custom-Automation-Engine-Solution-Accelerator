#!/usr/bin/env python3
"""Quick validation script for observability components."""

import sys
import asyncio
from app_config import config

async def main():
    """Validate all observability components."""
    print("=" * 60)
    print("OBSERVABILITY COMPONENTS VALIDATION")
    print("=" * 60)
    
    # Test 1: Import context injector
    print("\n1. Testing ContextInjector import...")
    try:
        from observability.context_injector import (
            HealthAwareContextInjector,
            AgentHealthDecisionHelper,
        )
        print("   ✅ ContextInjector imported successfully")
    except Exception as e:
        print(f"   ❌ Failed to import ContextInjector: {e}")
        return False
    
    # Test 2: Import snapshot store
    print("\n2. Testing ObservabilitySnapshotStore import...")
    try:
        from observability.observability_snapshot_store import ObservabilitySnapshotStore
        print("   ✅ ObservabilitySnapshotStore imported successfully")
    except Exception as e:
        print(f"   ❌ Failed to import ObservabilitySnapshotStore: {e}")
        return False
    
    # Test 3: Test health context injection
    print("\n3. Testing health context injection...")
    try:
        mock_snapshot = {
            "timestamp": "2026-03-02T10:30:00Z",
            "overall_health": True,
            "health_score": 95.0,
            "provider_health": {
                "firestore": {
                    "is_healthy": True,
                    "response_time_ms": 45.0,
                },
                "aws_s3": {
                    "is_healthy": True,
                    "response_time_ms": 50.0,
                }
            },
            "app_kpis": {
                "active_sessions": 5,
                "total_sessions": 10,
                "error_rate": 0.5,
            },
            "errors": []
        }
        base_msg = "You are a helpful tech support agent."
        enhanced = HealthAwareContextInjector.inject_health_snapshot(
            base_msg, mock_snapshot
        )
        if "Current Application Health Status" in enhanced:
            print("   ✅ Health context injected successfully")
            print(f"      Enhanced message length: {len(enhanced)} chars")
        else:
            print("   ❌ Health context not found in enhanced message")
            return False
    except Exception as e:
        print(f"   ❌ Failed to inject health context: {e}")
        return False
    # Test 4: Test decision helper
    print("\n4. Testing AgentHealthDecisionHelper...")
    try:
        should_proceed, reason = AgentHealthDecisionHelper.should_attempt_operation(
            mock_snapshot,
            ["firestore", "aws_s3"],
            "test_operation"
        )
        if should_proceed:
            print(f"   ✅ Decision helper working: {reason}")
        else:
            print(f"   ⚠️  Decision: {reason}")
        retry_strat = AgentHealthDecisionHelper.get_retry_strategy(mock_snapshot)
        print(f"   ✅ Retry strategy: {retry_strat['max_retries']} max retries")
    except Exception as e:
        print(f"   ❌ Failed decision helper test: {e}")
        return False
    # Test 5: Test trends aggregation
    print("\n5. Testing trends aggregation...")
    try:
        store = ObservabilitySnapshotStore()

        # Seed one snapshot so trends has real data points to aggregate.
        await store.persist_snapshot(
            {
                "timestamp": mock_snapshot["timestamp"],
                "overall_health": mock_snapshot["overall_health"],
                "health_score": mock_snapshot["health_score"],
                "provider_health": mock_snapshot["provider_health"],
                "app_kpis": mock_snapshot["app_kpis"],
                "errors": mock_snapshot["errors"],
            },
            project_id="test-project",
        )

        trends = await asyncio.wait_for(
            store.get_trends(days=1, project_id="test-project"),
            timeout=20.0,
        )

        if "timeline" in trends and "summary" in trends:
            print("   ✅ Trends aggregation working")
            print(f"      Timeline points: {len(trends['timeline'])}")
            print(f"      Summary: {trends['summary'].get('trend', 'unknown')}")
        else:
            print("   ❌ Trends missing required keys")
            return False
    except asyncio.TimeoutError:
        print("   ❌ Failed trends test: timed out after 20s (Cosmos query too slow)")
        return False
    except Exception as e:
        print(f"   ❌ Failed trends test: {e}")
        return False
    # Test 6: Test app_health_monitor (basic)
    print("\n6. Testing AppHealthMonitor basics...")
    try:
        from observability.app_health_monitor import AppHealthMonitor, AppHealthSnapshot
        _ = AppHealthMonitor  # force symbol usage for static analysis
        _ = AppHealthSnapshot
        print("   ✅ AppHealthMonitor imported successfully")
        print("   ✅ AppHealthSnapshot dataclass available")
    except Exception as e:
        print(f"   ❌ Failed AppHealthMonitor import: {e}")
        return False
    print("\n" + "=" * 60)
    print("✅ ALL OBSERVABILITY COMPONENTS VALIDATED SUCCESSFULLY!")
    print("=" * 60)
    return True

if __name__ == "__main__":
    try:
        success = asyncio.run(main())
        sys.exit(0 if success else 1)
    finally:
        # Close cached async SDK clients to avoid aiohttp leak warnings in one-off scripts.
        asyncio.run(config.close())
