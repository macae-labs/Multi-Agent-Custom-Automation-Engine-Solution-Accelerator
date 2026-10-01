"""Un proceso de ca-mcp para los tests: servidor MCP de referencia (``mcp.server``,
el mismo paquete que corre dentro de FastMCP) con sus sesiones en SU memoria.

Se ejecuta como proceso aparte, igual que ca-mcp respecto del backend: matarlo
y levantar otro en el mismo puerto es exactamente el reemplazo de proceso que
se midió en prod 2026-09-30, y su memoria no es la del test. La tool ``echo``
devuelve ``<pid>:<text>``: qué proceso atendió cada llamada queda en la
respuesta misma.
"""

import contextlib
import os
import sys
from typing import Any

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount


def build() -> Starlette:
    server: Server = Server("ca-mcp-de-prueba")

    @server.list_tools()
    async def _tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="echo",
                description="devuelve <pid>:<text>",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]

    @server.call_tool()
    async def _call(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        return [types.TextContent(type="text", text=f"{os.getpid()}:{arguments['text']}")]

    manager = StreamableHTTPSessionManager(app=server, json_response=True)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        async with manager.run():
            yield

    return Starlette(routes=[Mount("/", app=manager.handle_request)], lifespan=lifespan)


if __name__ == "__main__":
    uvicorn.run(build(), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
