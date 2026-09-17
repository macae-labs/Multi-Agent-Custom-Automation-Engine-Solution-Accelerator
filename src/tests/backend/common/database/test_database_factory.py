"""DatabaseFactory: una conexión por proceso, identidad por llamada (INC-2026-007)."""

from unittest.mock import AsyncMock, Mock

import pytest

from common.database import cosmosdb as cosmosdb_mod
from common.database import database_factory as factory_mod
from common.database.cosmosdb import CosmosDBClient
from common.database.database_factory import DatabaseFactory


async def _no_items():
    for item in ():
        yield item


@pytest.fixture(autouse=True)
def _collaborators_patched(monkeypatch):
    """Cliente Cosmos sin red y config fija, en el namespace de cada módulo."""
    cosmos_client_cls = Mock(name="CosmosClient")
    cosmos_client_cls.return_value.close = AsyncMock()
    monkeypatch.setattr(cosmosdb_mod, "CosmosClient", cosmos_client_cls)
    monkeypatch.setattr(
        factory_mod,
        "config",
        Mock(
            COSMOSDB_ENDPOINT="https://test.documents.azure.com:443/",
            COSMOSDB_DATABASE="test_db",
            COSMOSDB_CONTAINER="test_container",
            get_cosmos_credential_async=Mock(return_value="credential"),
        ),
    )
    monkeypatch.setattr(DatabaseFactory, "_instance", None)
    return cosmos_client_cls


@pytest.mark.asyncio
async def test_first_call_opens_one_connection_scoped_to_the_caller(
    _collaborators_patched,
):
    store = await DatabaseFactory.get_database(user_id="user1", tenant_id="t1")

    _collaborators_patched.assert_called_once()
    assert isinstance(store, CosmosDBClient)
    assert (store.user_id, store.tenant_id) == ("user1", "t1")
    # El singleton no lleva identidad: nadie consulta "del usuario" sin scope.
    assert DatabaseFactory._instance is not None
    assert DatabaseFactory._instance.user_id == ""


@pytest.mark.asyncio
async def test_second_user_shares_the_connection_and_keeps_its_own_identity(
    _collaborators_patched,
):
    """Firma de INC-2026-007: el segundo usuario recibía el store del primero."""
    first = await DatabaseFactory.get_database(user_id="user1")
    second = await DatabaseFactory.get_database(user_id="user2")

    _collaborators_patched.assert_called_once()
    assert first.container is second.container
    assert first.user_id == "user1"
    assert second.user_id == "user2"


@pytest.mark.asyncio
async def test_user_queries_carry_the_callers_identity(_collaborators_patched):
    """get_all_plans filtra por el user_id de la vista, no del singleton."""
    await DatabaseFactory.get_database(user_id="user1")
    second = await DatabaseFactory.get_database(user_id="user2")
    second.container.query_items = Mock(return_value=_no_items())

    await second.get_all_plans()

    parameters = second.container.query_items.call_args.kwargs["parameters"]
    assert {"name": "@user_id", "value": "user2"} in parameters


@pytest.mark.asyncio
async def test_force_new_opens_an_independent_connection(_collaborators_patched):
    # Una instancia distinta por construcción, para distinguir las conexiones.
    _collaborators_patched.side_effect = [Mock(name="shared"), Mock(name="own")]
    shared = await DatabaseFactory.get_database(user_id="user1")
    own = await DatabaseFactory.get_database(user_id="user1", force_new=True)

    assert _collaborators_patched.call_count == 2
    assert own is not DatabaseFactory._instance
    assert own.client is not shared.client
    assert own.user_id == "user1"


@pytest.mark.asyncio
async def test_close_all_closes_the_shared_connection_once(_collaborators_patched):
    await DatabaseFactory.get_database(user_id="user1")
    await DatabaseFactory.get_database(user_id="user2")

    await DatabaseFactory.close_all()

    _collaborators_patched.return_value.close.assert_awaited_once()
    assert DatabaseFactory._instance is None


@pytest.mark.asyncio
async def test_initialize_error_propagates_and_leaves_no_singleton(
    _collaborators_patched,
):
    _collaborators_patched.side_effect = RuntimeError("Initialization failed")

    with pytest.raises(RuntimeError, match="Initialization failed"):
        await DatabaseFactory.get_database(user_id="user1")

    assert DatabaseFactory._instance is None
