"""
Test configuration for MCP server tests.
"""

import gc
import pytest
import sys
from pathlib import Path

# UNA sola identidad de import: los tests importan el producto como el
# producto se importa a sí mismo (`from core.factory import …`,
# `from services… import …`). Tener además la raíz del repo en sys.path
# permitía `from src.mcp_server.core.factory import …` y cargaba el MISMO
# archivo dos veces como dos módulos: dos enums Domain, dos MCPToolFactory,
# `isinstance` falso y `Domain.HR != Domain.HR` (la firma de INC-2026-004,
# repetida en el MCP). Nadie lo vio porque esta suite no corría en ningún gate.
repo_root = Path(__file__).resolve().parents[3]
mcp_server_path = repo_root / "src" / "mcp_server"
# Al FRENTE y sin condición: `python -m pytest` ya trae el cwd en sys.path[0]
# y otros conftests insertan delante; si esta ruta queda detrás, otra identidad
# gana el import. El orden es parte del contrato.
sys.path = [entry for entry in sys.path if entry != str(mcp_server_path)]
sys.path.insert(0, str(mcp_server_path))


@pytest.fixture
def mcp_factory():
    """Factory fixture for tests."""
    from core.factory import MCPToolFactory

    return MCPToolFactory()


@pytest.fixture
def hr_service():
    """HR service fixture."""
    from services.hr_service import HRService

    return HRService()


@pytest.fixture
def tech_support_service():
    """Tech support service fixture."""
    from services.tech_support_service import TechSupportService

    return TechSupportService()


@pytest.fixture
def general_service():
    """General service fixture."""
    from services.general_service import GeneralService

    return GeneralService()


@pytest.fixture
def mock_mcp_server():
    """Mock MCP server for testing."""

    class MockMCP:
        def __init__(self):
            self.tools = []
            self.resources = []

        def resource(self, *args, **kwargs):
            # Los recursos no son tools: se registran aparte para que
            # tool_count se contraste sólo contra @mcp.tool.
            def decorator(func):
                self.resources.append({"func": func, "args": args, "kwargs": kwargs})
                return func

            return decorator

        def tool(self, tags=None):
            def decorator(func):
                self.tools.append({"func": func, "tags": tags or []})
                return func

            return decorator

    return MockMCP()


@pytest.fixture(autouse=True)
def _collect_after_each_test():
    """Un recurso sin cerrar tiene que fallar en el test que lo abrió.

    pytest.ini convierte ResourceWarning en error, pero el aviso salta cuando el
    recolector pasa, que puede ser módulos más tarde: la falla caía en un test
    al azar (medido: en tres corridas, tres tests distintos de
    test_tool_arguments). Recolectar al cerrar cada test ata la culpa a quien la
    tiene.
    """
    yield
    gc.collect()
