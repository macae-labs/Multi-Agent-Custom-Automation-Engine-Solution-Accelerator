"""Test script for observability module - Quick validation."""

import asyncio
import logging
import pytest

logging.basicConfig(level=logging.INFO)


@pytest.mark.asyncio
async def test_health_monitor():
    """Test the health monitoring system with real runtime configuration."""
    from observability.app_health_monitor import AppHealthMonitor
    from app_config import config
    from credential_resolver import credential_resolver

    print("\n" + "=" * 60)
    print("TESTING APP HEALTH MONITOR")
    print("=" * 60)

    try:
        # Create monitor with default project_id (uses existing secrets)
        monitor = AppHealthMonitor(
            project_id="default",
            session_id="test-session",
        )

        print("\n💡 Test Configuration:")
        print("   - project_id: default (uses project-default-firestore secret)")
        print("   - session_id: test-session")
        print("   - Timeout strategy: 35s outer (allows 3x10s retries + backoff)")
        print("   - S3 bucket: fibroskin-academic-videos (from runtime config)")

        print("\n1️⃣  Testing registered health checkers...")
        from observability.app_health_monitor import AppHealthMonitor

        registered = AppHealthMonitor._health_checker_registry
        print(f"   Registered checkers: {list(registered.keys())}")

        print("\n2️⃣  Testing provider discovery...")
        active_providers = monitor._get_active_providers()
        print(f"   Active providers to check: {active_providers}")

        print("\n3️⃣  Testing individual provider health check...")
        if "firestore" in active_providers:
            print("   Checking Firestore health...")
            firestore_health = await monitor.collect_provider_health("firestore")
            if firestore_health:
                print(
                    f"   ✓ Firestore: healthy={firestore_health.is_healthy}, "
                    f"time={firestore_health.response_time_ms:.1f}ms"
                )
                if firestore_health.metrics:
                    print(f"     Metrics: {firestore_health.metrics}")
            else:
                print("   ✗ Firestore health check returned None")

        if "aws_s3" in active_providers:
            print("   Checking S3 health...")
            s3_health = await monitor.collect_provider_health("aws_s3")
            if s3_health:
                print(
                    f"   ✓ S3: healthy={s3_health.is_healthy}, "
                    f"time={s3_health.response_time_ms:.1f}ms"
                )
                if s3_health.metrics:
                    print(f"     Metrics: {s3_health.metrics}")
            else:
                print("   ✗ S3 health check returned None")

        print("\n4️⃣  Testing full health snapshot...")
        snapshot = await monitor.get_health_snapshot()

        print("\n📊 HEALTH SNAPSHOT RESULTS:")
        print(f"   Timestamp: {snapshot.timestamp}")
        print(
            f"   Overall Health: {'🟢 HEALTHY' if snapshot.overall_health else '🔴 UNHEALTHY'}"
        )
        print(f"   Health Score: {snapshot.health_score:.1f}%")
        print(f"   Providers Checked: {len(snapshot.provider_health)}")

        if snapshot.provider_health:
            print("\n   Provider Details:")
            for provider_id, health in snapshot.provider_health.items():
                status = "🟢" if health.is_healthy else "🔴"
                print(f"     {status} {provider_id}: {health.response_time_ms:.1f}ms")
                if health.error_message:
                    print(f"        Error: {health.error_message}")

        if snapshot.errors:
            print("\n   ⚠️  Errors:")
            for error in snapshot.errors:
                print(f"     - {error}")

        print("\n   App KPIs:")
        for key, value in snapshot.app_kpis.items():
            print(f"     - {key}: {value}")

        print("\n" + "=" * 60)
        print("✅ TEST COMPLETED")
        print("=" * 60)

        return snapshot
    finally:
        await credential_resolver.close()
        await config.close()


if __name__ == "__main__":
    asyncio.run(test_health_monitor())
