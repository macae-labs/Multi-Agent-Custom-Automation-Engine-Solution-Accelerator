"""
Test configuration for agent tests.
"""

import sys
from pathlib import Path

import pytest

# Get the root directory of the project
root_dir = Path(__file__).parent

# `src` NO va en sys.path. Con `src` ahí, cada directorio de src/ con
# __init__.py se vuelve importable como paquete (`backend.*`, `mcp_server`) y el
# producto queda cargado DOS veces bajo dos nombres: es la firma de
# INC-2026-004, y lo que hacía que `import mcp_server` resolviera al paquete
# src/mcp_server en vez del módulo mcp_server.py. Nadie importa vía `src`
# (medido: cero `from backend.`/`from agents.`); common y v4 viven bajo
# src/backend, que sí está en el path.

# Add src/backend to path for relative imports within backend
backend_path = root_dir / "src" / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))


@pytest.fixture
def agent_env_vars():
    """Common environment variables for agent testing."""
    return {
        "BING_CONNECTION_NAME": "test_bing_connection",
        "MCP_SERVER_ENDPOINT": "http://test-mcp-server",
        "MCP_SERVER_NAME": "test_mcp_server",
        "MCP_SERVER_DESCRIPTION": "Test MCP server",
        "TENANT_ID": "test_tenant_id",
        "CLIENT_ID": "test_client_id",
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com/",
        "AZURE_OPENAI_API_KEY": "test_key",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "test_deployment",
    }
