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

import contextvars
import logging
from collections.abc import Callable
from typing import Any

from agent_framework import Content, MCPStreamableHTTPTool

logger = logging.getLogger(__name__)

#: Quien quiera ver cada ejecución real de una tool MCP (nombre, argumentos,
#: resultado), sin depender de lo que el stream del workflow muestre. Medido
#: 2026-10-04: una tool aprobada se ejecuta al reanudar pero el framework no
#: emite su resultado en el stream; el hecho se registra aquí, donde ocurre.
TOOL_OBSERVER: contextvars.ContextVar[Callable[[str, dict, Any], None] | None] = (
    contextvars.ContextVar("mcp_tool_observer", default=None)
)


class ReconnectingMCPTool(MCPStreamableHTTPTool):
    """``MCPStreamableHTTPTool`` que reabre la sesión cuando el servidor cambió.

    ``approval_from_annotations=True``: la aprobación humana de cada tool sale
    del contrato del propio servidor (anotación MCP ``readOnlyHint``): la de
    solo lectura corre sin pedir; cualquier otra pide aprobación
    (``function_approval_request`` del framework). Sin listas en el cliente.
    """

    def __init__(
        self, *args: Any, approval_from_annotations: bool = False, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._approval_from_annotations = approval_from_annotations

    async def load_tools(self) -> None:
        if self._approval_from_annotations and self.session is not None:
            from mcp import types

            read_only: list[str] = []
            requires: list[str] = []
            params: types.PaginatedRequestParams | None = None
            while True:
                page = await self.session.list_tools(params=params)
                for tool in page.tools:
                    hint = getattr(tool.annotations, "readOnlyHint", None)
                    (read_only if hint is True else requires).append(tool.name)
                if not page.nextCursor:
                    break
                params = types.PaginatedRequestParams(cursor=page.nextCursor)
            self.approval_mode = {
                "never_require_approval": read_only,
                "always_require_approval": requires,
            }
        await super().load_tools()

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
        result = await super().call_tool(tool_name, **kwargs)
        observer = TOOL_OBSERVER.get()
        if observer is not None:
            try:
                observer(tool_name, dict(kwargs), result)
            except Exception as ex:  # observar nunca rompe la ejecución
                logger.debug("Observador de tools falló: %s", ex)
        return result
