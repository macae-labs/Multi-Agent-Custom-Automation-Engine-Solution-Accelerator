"""Session agent pool. One persistent agent per (tenant_id, user_id, session_id). No eviction."""

import logging
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

_cache: Dict[Tuple[str, str, str], Any] = {}


async def get_or_create(tenant_id: str, user_id: str, session_id: str, factory) -> Any:
    """Get cached agent or create via factory (async callable returning opened agent)."""
    key = (tenant_id, user_id, session_id)
    if key in _cache:
        logger.info("Reusing agent for tenant=%s session=%s", tenant_id[:8], session_id[:12])
        return _cache[key]

    agent = await factory()
    _cache[key] = agent
    logger.info("Created agent for tenant=%s session=%s (pool size: %d)", tenant_id[:8], session_id[:12], len(_cache))
    return agent
