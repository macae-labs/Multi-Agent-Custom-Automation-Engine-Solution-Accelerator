"""Registro y ejecución del incremento 4 con las piezas que ya existen.

- Registro: el backend monta el mismo share que ca-mcp (``MACAE_WORKSPACE_ROOT``)
  y ``workspace_for(user_id, workspace_id)`` es el único resolutor. Los INC se
  leen del clon en disco, ``docs/incidents/*.json``, con ``_resolve`` como
  contención; no hay red para leer un archivo local.
- Ejecución: la misma vía que un agente. ``MCPStreamableHTTPTool`` sobre
  ``MCPConfig.from_env()`` y ``call_tool("workspace_exec", ...)``; la evidencia
  es ``exit_code``/``stdout``/``stderr`` verbatim del payload de la tool
  (``format_success_response``). Un error de la tool es fallo de capacidad.

La referencia es durable y de config (``INCIDENT_REGISTRY_USER_ID`` /
``INCIDENT_REGISTRY_WORKSPACE_ID``), nunca la sesión de un usuario.
"""

import json
import logging
from typing import Any, Optional

from agent_framework import MCPStreamableHTTPTool

from common.config.app_config import config
from v4.common.services.workspace_service import _resolve, workspace_for
from v4.control.incident_revalidation import Evidence, Executor, Registry
from v4.magentic_agents.models.agent_models import MCPConfig

logger = logging.getLogger(__name__)

INCIDENTS_DIR = "docs/incidents"
EXEC_TIMEOUT_SECONDS = 600


class WorkspaceCapability:
    def __init__(
        self,
        *,
        user_id: str,
        workspace_id: str,
        tool: Optional[MCPStreamableHTTPTool] = None,
    ) -> None:
        self.user_id = user_id
        self.workspace_id = workspace_id
        self._tool = tool

    async def _mcp(self) -> MCPStreamableHTTPTool:
        if self._tool is None:
            cfg = MCPConfig.from_env()
            tool = MCPStreamableHTTPTool(
                name=cfg.name, description=cfg.description, url=cfg.url
            )
            await tool.__aenter__()
            self._tool = tool
        return self._tool

    async def aclose(self) -> None:
        if self._tool is not None:
            try:
                await self._tool.close()
            except Exception as ex:  # cierre cruzado de tarea, como en lifecycle
                logger.debug("MCP tool close: %s", ex)
            self._tool = None

    async def registry(self) -> list[dict[str, Any]]:
        ws = workspace_for(self.user_id, self.workspace_id)
        base = _resolve(ws, INCIDENTS_DIR)
        incidents: list[dict[str, Any]] = []
        if not base.is_dir():
            logger.warning("Registro sin %s en %s", INCIDENTS_DIR, ws)
            return incidents
        for path in sorted(base.glob("*.json")):
            try:
                incident = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as ex:
                logger.warning("INC %s ilegible: %s", path.name, ex)
                continue
            if isinstance(incident, dict) and "incident_id" in incident:
                incidents.append(incident)
        return incidents

    async def execute(self, command: str, cwd: str) -> Evidence:
        tool = await self._mcp()
        raw = await tool.call_tool(
            "workspace_exec",
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            command=command,
            path="" if cwd in ("", ".", "/") else cwd,
            timeout=EXEC_TIMEOUT_SECONDS,
        )
        text = (
            raw
            if isinstance(raw, str)
            else "".join(getattr(c, "text", "") or "" for c in raw)
        )
        # format_error_response de ca-mcp es markdown ("##### ❌ Error …"), no
        # JSON: cualquier texto que no sea el JSON de format_success_response es
        # fallo de capacidad, con ese texto como motivo.
        if not text.lstrip().startswith("{"):
            raise RuntimeError(f"workspace_exec: {text[:500]}")
        payload = json.loads(text)
        if payload.get("status") != "success":
            raise RuntimeError(
                f"workspace_exec: {payload.get('message') or text[:500]}"
            )
        details = payload["details"]
        return Evidence(
            exit_code=int(details["exit_code"]),
            stdout=str(details.get("stdout", "")),
            stderr=str(details.get("stderr", "")),
        )


def from_config() -> Optional[WorkspaceCapability]:
    """``None`` si la referencia durable no está configurada: el reconciliador
    entonces sólo reacciona a eventos humanos, y lo dice en el log."""
    user_id = config.INCIDENT_REGISTRY_USER_ID
    workspace_id = config.INCIDENT_REGISTRY_WORKSPACE_ID
    if not (user_id and workspace_id and config.MCP_SERVER_ENDPOINT):
        logger.info(
            "Registro de INC no configurado (INCIDENT_REGISTRY_USER_ID / "
            "INCIDENT_REGISTRY_WORKSPACE_ID / MCP_SERVER_ENDPOINT)"
        )
        return None
    return WorkspaceCapability(user_id=user_id, workspace_id=workspace_id)


def bind(capability: WorkspaceCapability) -> tuple[Registry, Executor]:
    return capability.registry, capability.execute
