"""La sesión MCP del backend sobrevive al reemplazo del proceso de ca-mcp.

El servidor es el de referencia del protocolo, en OTRO proceso, como ca-mcp
respecto del backend (``_mcp_reference_server.py``). Se mata y se levanta otro
en el mismo puerto: el nuevo no conoce ninguna sesión previa y responde 404 a
la que el cliente guardó, exactamente lo medido en prod 2026-09-30 tras
reiniciar la revisión de ca-mcp. El cliente es el del framework, sin dobles.
"""

import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from agent_framework import MCPStreamableHTTPTool
from agent_framework.exceptions import ToolExecutionException

from v4.common.mcp_tool import ReconnectingMCPTool

SERVER = Path(__file__).with_name("_mcp_reference_server.py")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class McpProcess:
    """Un proceso de ca-mcp: se levanta, atiende, y se mata como un contenedor."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._proc = subprocess.Popen(
            [sys.executable, str(SERVER), str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.pid = self._proc.pid
        deadline = time.monotonic() + 30
        while True:
            with socket.socket() as probe:
                probe.settimeout(0.2)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    return
            if self._proc.poll() is not None or time.monotonic() > deadline:
                self.kill()
                raise RuntimeError("el servidor MCP de prueba no arrancó")
            time.sleep(0.05)

    def kill(self) -> None:
        self._proc.kill()
        self._proc.wait(10)


def _text(out: Any) -> str:
    return out if isinstance(out, str) else "".join(getattr(c, "text", "") or "" for c in out)


@pytest.mark.asyncio
async def test_the_session_is_reopened_when_the_server_process_changes():
    """Tras el reemplazo del proceso, la llamada siguiente la atiende el proceso
    NUEVO (su PID viene en la respuesta): la sesión se reabrió sola, sin
    reiniciar el cliente."""
    port = _free_port()
    first = McpProcess(port)
    second: McpProcess | None = None
    tool = ReconnectingMCPTool(
        name="ca-mcp", url=f"http://127.0.0.1:{port}/mcp", load_prompts=False
    )
    try:
        await tool.__aenter__()
        assert _text(await tool.call_tool("echo", text="antes")) == f"{first.pid}:antes"

        first.kill()
        second = McpProcess(port)

        assert _text(await tool.call_tool("echo", text="después")) == f"{second.pid}:después"
    finally:
        await tool.__aexit__(None, None, None)
        if second is not None:
            second.kill()


@pytest.mark.asyncio
async def test_the_framework_tool_keeps_the_dead_session_which_is_why_the_subclass_exists():
    """La condición es real en el cliente del framework: con la sesión vieja
    contra el proceso nuevo, ``call_tool`` falla y no vuelve a inicializar.
    Si un bump del framework hace pasar esta llamada, este test falla a
    propósito: es la señal de que ``ReconnectingMCPTool`` ya sobra."""
    port = _free_port()
    first = McpProcess(port)
    second: McpProcess | None = None
    tool = MCPStreamableHTTPTool(
        name="ca-mcp", url=f"http://127.0.0.1:{port}/mcp", load_prompts=False
    )
    try:
        await tool.__aenter__()
        assert _text(await tool.call_tool("echo", text="antes")) == f"{first.pid}:antes"

        first.kill()
        second = McpProcess(port)

        with pytest.raises(ToolExecutionException):
            await tool.call_tool("echo", text="después")
    finally:
        await tool.__aexit__(None, None, None)
        if second is not None:
            second.kill()
