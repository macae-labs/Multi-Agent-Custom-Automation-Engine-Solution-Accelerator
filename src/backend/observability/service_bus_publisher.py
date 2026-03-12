"""Service Bus publisher for strategic autonomy decisions.

This module is intentionally thin:
- No business logic duplication
- Best-effort publish with graceful degradation if Service Bus is not configured
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ServiceBusPublisher:
    """Publish strategic decision events to Azure Service Bus."""

    def __init__(
        self,
        connection_string: Optional[str] = None,
        topic_name: Optional[str] = None,
    ) -> None:
        # Support both canonical and legacy env var names.
        resolved_connection_string = (
            connection_string
            or os.getenv("AZURE_SERVICE_BUS_CONNECTION_STRING", "")
            or os.getenv("AZURE_SERVICEBUS_CONNECTION_STRING", "")
        )
        resolved_topic_name = (
            topic_name
            or os.getenv("AZURE_SERVICE_BUS_TOPIC", "")
            or os.getenv("AZURE_SERVICEBUS_TOPIC", "")
            or "strategic-decisions"
        )
        self.connection_string = (resolved_connection_string).strip()
        self.topic_name = resolved_topic_name.strip()

    def is_configured(self) -> bool:
        return bool(self.connection_string and self.topic_name)

    async def publish_decision(
        self,
        message_type: str,
        health_snapshot: Dict[str, Any],
        analysis: Dict[str, Any],
    ) -> bool:
        """Publish strategic decision message.

        Returns:
            True on successful publish, False if skipped/failed.
        """
        if not self.is_configured():
            logger.warning(
                "Service Bus publisher not configured "
                "(AZURE_SERVICE_BUS_CONNECTION_STRING|AZURE_SERVICEBUS_CONNECTION_STRING "
                "/ AZURE_SERVICE_BUS_TOPIC|AZURE_SERVICEBUS_TOPIC)"
            )
            return False

        payload = {
            "type": message_type,
            "timestamp": datetime.utcnow().isoformat(),
            "health_score": health_snapshot.get("health_score"),
            "overall_health": health_snapshot.get("overall_health"),
            "provider_health": health_snapshot.get("provider_health", {}),
            "detected_issues": analysis.get("detected_issues", []),
            "recommended_actions": analysis.get("recommended_actions", []),
            "autonomy_decision": analysis.get("autonomy_decision", {}),
        }

        # Lazy import: keep runtime resilient even if azure-servicebus package is absent.
        try:
            from azure.servicebus.aio import ServiceBusClient
            from azure.servicebus import ServiceBusMessage
        except Exception as exc:
            logger.error(
                "azure-servicebus not available. Install azure-servicebus to enable publish. Error: %s",
                exc,
            )
            return False

        try:
            client = ServiceBusClient.from_connection_string(self.connection_string)
            async with client:
                sender = client.get_topic_sender(topic_name=self.topic_name)
                async with sender:
                    msg = ServiceBusMessage(
                        body=json.dumps(payload),
                        content_type="application/json",
                        subject=message_type,
                    )
                    await sender.send_messages(msg)
            logger.info(
                "Strategic event published to Service Bus topic '%s' (%s)",
                self.topic_name,
                message_type,
            )
            return True
        except Exception as exc:
            logger.exception(
                "Failed to publish strategic decision to Service Bus: %s", exc
            )
            return False


_publisher_singleton: Optional[ServiceBusPublisher] = None


def get_service_bus_publisher() -> ServiceBusPublisher:
    """Singleton accessor for ServiceBusPublisher."""
    global _publisher_singleton
    if _publisher_singleton is None:
        _publisher_singleton = ServiceBusPublisher()
    return _publisher_singleton
