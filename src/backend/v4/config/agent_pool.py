"""Session agent pool. One persistent agent per (user_id, session_id). No eviction."""

import logging
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

_cache: Dict[Tuple[str, str], Any] = {}


async def get_or_create(user_id: str, session_id: str, factory) -> Any:
    """Get cached agent or create via factory (async callable returning opened agent)."""
    key = (user_id, session_id)
    if key in _cache:
        logger.info("Reusing agent for session %s", session_id[:12])
        return _cache[key]

    agent = await factory()
    _cache[key] = agent
    logger.info("Created agent for session %s (pool size: %d)", session_id[:12], len(_cache))
    return agent
