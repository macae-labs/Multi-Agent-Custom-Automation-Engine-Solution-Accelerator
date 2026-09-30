"""El módulo del servidor es importable y coherente consigo mismo.

Estos tests estuvieron "pasando" con ``return False``: pytest ignora el valor
devuelto, así que nunca pudieron fallar. Además importaban ``mcp_server`` como
PAQUETE (``src/mcp_server/__init__.py``, alcanzable porque el conftest de la
raíz ponía ``src`` en sys.path) en vez del módulo ``mcp_server.py``, por lo que
``mcp`` no existía. Ahora afirman, importan el módulo real y abren el cliente
como contexto para no dejar nada sin cerrar.
"""

import pytest
from fastmcp import Client

import mcp_server


def test_the_server_module_exposes_a_configured_server():
    assert mcp_server.mcp is not None
    services = mcp_server.factory.get_all_services()
    summary = mcp_server.factory.get_tool_summary()
    assert summary["total_services"] == len(services) > 0
    assert summary["total_tools"] == sum(s.tool_count for s in services.values())


@pytest.mark.asyncio
async def test_a_client_sees_exactly_the_declared_tools():
    """Lo declarado (tool_count) y lo servido (list_tools) tienen que coincidir."""
    async with Client(mcp_server.mcp) as client:
        served = {t.name for t in await client.list_tools()}

    assert len(served) == mcp_server.factory.get_tool_summary()["total_tools"]
    assert {"workspace_exec", "workspace_publish", "call_external_tool"} <= served
