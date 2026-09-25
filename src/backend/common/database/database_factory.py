"""Database factory for creating database instances."""

import logging

from common.config.app_config import config

from .cosmosdb import CosmosDBClient
from .database_base import DatabaseBase


class DatabaseFactory:
    """Factory class for creating database instances."""

    _instance: DatabaseBase | None = None
    _logger = logging.getLogger(__name__)

    @staticmethod
    async def get_database(
        user_id: str = "",
        tenant_id: str = "",
        force_new: bool = False,
    ) -> DatabaseBase:
        """Store del llamador: una conexión por proceso, identidad por llamada.

        La conexión (cliente, base, contenedor) es única y la cierra
        ``close_all``; cada llamada recibe una vista con su ``user_id`` y
        ``tenant_id``. El singleton anterior conservaba la identidad del primer
        llamador del proceso y las consultas "del usuario" de cualquier otro
        usuario leían los datos de aquél (INC-2026-007). ``force_new`` abre una
        conexión propia, fuera del singleton.
        """
        if force_new or DatabaseFactory._instance is None:
            cosmos_db_client = CosmosDBClient(
                endpoint=config.COSMOSDB_ENDPOINT,
                # Allow key-based auth only in dev to avoid accidentally bypassing AAD in prod.
                # Prestada y ASYNC: el cliente es azure.cosmos.aio; una credencial
                # síncrona la envuelve el SDK y get_token bloquea el loop (INC-2026-005).
                credential=config.get_cosmos_credential_async(),
                database_name=config.COSMOSDB_DATABASE,
                container_name=config.COSMOSDB_CONTAINER,
                session_id="",
                user_id=user_id if force_new else "",
                tenant_id=tenant_id if force_new else "",
            )
            await cosmos_db_client.initialize()
            if force_new:
                return cosmos_db_client
            DatabaseFactory._instance = cosmos_db_client
        return DatabaseFactory._instance.for_user(user_id, tenant_id)

    @staticmethod
    async def close_all():
        """Close all database connections."""
        if DatabaseFactory._instance:
            await DatabaseFactory._instance.close()
            DatabaseFactory._instance = None
