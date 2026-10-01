"""Tool MCP cuya sesión sigue la vida del SERVIDOR, no la del proceso cliente.

Una sesión streamable-http vive en la memoria del proceso de ca-mcp. Cuando ese
proceso se reemplaza (deploy, reinicio de revisión, escalado), el
``Mcp-Session-Id`` que el cliente guarda deja de existir y el servidor responde
404 a todo request que lo lleve: es la norma del protocolo, y la misma norma
obliga al cliente a abrir una sesión nueva con otro ``initialize``.

El cliente del framework no lo hace. ``MCPTool.call_tool`` sólo reconecta ante
``ClosedResourceError``; el ``McpError("Session terminated")`` que el
transporte fabrica a partir del 404 lo relanza como ``ToolExecutionException``
y la sesión muerta se queda para toda la vida del proceso. Medido en prod
2026-09-30: backend arrancado 10:22:56, proceso nuevo de ca-mcp 10:23:33 y,
desde 10:23:41, cada ``POST /mcp`` del backend → 404 hasta reiniciar el
backend. Reproducido en localhost con ``WorkspaceCapability`` (la clase del
reconciliador): tras reemplazar el proceso del MCP, ``Session terminated`` en
cada llamada. Lo sufren los sitios de larga vida: el reconciliador y las tools
de los agentes de equipo; el chat crea su cliente por request.

Aquí la sesión se verifica ANTES de cada llamada con el mismo ``ping`` que el
framework ya manda antes de cada página de ``list_tools``, y se reabre si el
servidor ya no la reconoce. No se interpreta ningún error después del hecho.
"""

import logging
from typing import Any

from agent_framework import Content, MCPStreamableHTTPTool

logger = logging.getLogger(__name__)


class ReconnectingMCPTool(MCPStreamableHTTPTool):
    """``MCPStreamableHTTPTool`` que reabre la sesión cuando el servidor cambió."""

    async def _ensure_session(self) -> None:
        session = self.session
        if session is not None:
            try:
                await session.send_ping()
                return
            except Exception as ex:
                logger.info(
                    "Sesión MCP de %s no reconocida por el servidor (%s): se reabre",
                    self.name,
                    type(ex).__name__,
                )
        await self.connect(reset=True)

    async def call_tool(self, tool_name: str, **kwargs: Any) -> str | list[Content]:
        await self._ensure_session()
        return await super().call_tool(tool_name, **kwargs)
