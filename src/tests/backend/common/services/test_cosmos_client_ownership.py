"""Propiedad del cliente Cosmos: quien lo adquiere lo cierra ante cualquier fallo.

Los tres servicios crean un ``CosmosClient`` y luego resuelven base y contenedor.
Si el guard empieza en el ``read`` y falla antes, la sesión aiohttp queda abierta
y el proceso termina con "Unclosed client session". Aquí se afirma el cierre en
el fallo TEMPRANO (resolver la base), no sólo en el contenedor ausente.
"""

from unittest.mock import AsyncMock, Mock

import pytest
from azure.cosmos import exceptions

import common.services.checkpoint_storage as ckpt_mod
import common.services.event_store as store_mod
import v4.common.services.mcp_connections_service as mcp_mod


def cosmos_client_that_fails_resolving_the_database():
    client = Mock(name="CosmosClient")
    client.close = AsyncMock()
    client.get_database_client.side_effect = RuntimeError("endpoint resolution failed")
    return Mock(return_value=client), client


def cosmos_client_with_a_missing_container():
    client = Mock(name="CosmosClient")
    client.close = AsyncMock()
    container = Mock()
    container.read = AsyncMock(
        side_effect=exceptions.CosmosResourceNotFoundError(status_code=404)
    )
    client.get_database_client.return_value.get_container_client.return_value = (
        container
    )
    return Mock(return_value=client), client


@pytest.fixture
def cosmos_config(monkeypatch):
    cfg = Mock(
        COSMOSDB_ENDPOINT="https://acct.documents.azure.com:443/",
        COSMOSDB_DATABASE="macae",
        WORK_EVENTS_CONTAINER="work_events_dev",
        get_cosmos_credential_async=Mock(return_value="cred"),
    )
    for mod in (store_mod, ckpt_mod, mcp_mod):
        monkeypatch.setattr(mod, "config", cfg, raising=False)
    return cfg


@pytest.mark.asyncio
async def test_event_store_closes_the_client_when_the_database_cannot_be_resolved(
    cosmos_config, monkeypatch
):
    cls, client = cosmos_client_that_fails_resolving_the_database()
    monkeypatch.setattr(store_mod, "CosmosClient", cls)

    with pytest.raises(RuntimeError, match="endpoint resolution failed"):
        await store_mod.EventStore()._ensure_initialized()

    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_event_store_closes_the_client_when_the_container_is_missing(
    cosmos_config, monkeypatch
):
    cls, client = cosmos_client_with_a_missing_container()
    monkeypatch.setattr(store_mod, "CosmosClient", cls)

    with pytest.raises(RuntimeError, match="work_events_dev"):
        await store_mod.EventStore()._ensure_initialized()

    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_checkpoint_storage_closes_the_client_when_the_database_fails(
    cosmos_config, monkeypatch
):
    cls, client = cosmos_client_that_fails_resolving_the_database()
    monkeypatch.setattr(ckpt_mod, "CosmosClient", cls)

    with pytest.raises(RuntimeError, match="endpoint resolution failed"):
        await ckpt_mod.CosmosCheckpointStorage()._ensure_initialized()

    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_connections_closes_the_client_when_the_database_fails(
    cosmos_config, monkeypatch
):
    cls, client = cosmos_client_that_fails_resolving_the_database()
    monkeypatch.setattr(mcp_mod, "CosmosClient", cls)
    service = mcp_mod.MCPConnectionsService()

    with pytest.raises(RuntimeError, match="endpoint resolution failed"):
        await service._ensure_initialized()

    client.close.assert_awaited_once()
