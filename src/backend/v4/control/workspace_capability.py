"""Registro y ejecución del incremento 4 con las piezas que ya existen.

- Registro: el backend monta el mismo share que ca-mcp (``MACAE_WORKSPACE_ROOT``)
  y ``workspace_for(user_id, workspace_id)`` es el único resolutor. Los INC se
  leen del clon en disco, ``docs/incidents/*.json``, con ``_resolve`` como
  contención; no hay red para leer un archivo local.
- Ejecución: la misma vía que un agente. ``MCPStreamableHTTPTool`` sobre
  ``MCPConfig.from_env()`` y ``call_tool("workspace_exec", ...)``; la evidencia
  es ``exit_code``/``stdout``/``stderr`` verbatim del payload de la tool
  (``format_success_response``). Un error de la tool es fallo de capacidad.

El registro se descubre por CONTENIDO, no por configuración: es el workspace
que contiene ``docs/incidents``. Nada que declarar, nada que marcar, nada que
poner en variables de entorno: se clona el repo en un workspace desde la UI y
el reconciliador lo encuentra.
"""

import json
import logging
import subprocess
from typing import Any, Optional

from agent_framework import MCPStreamableHTTPTool

from v4.common.services.workspace_service import (
    REGISTRY_WORKSPACE_ID,
    WORKSPACE_ROOT,
    _resolve,
    workspace_for,
)
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
        #: Commit del registro leído en el último ``registry()``.
        self.source = ""
        #: Sólo el workspace propio del reconciliador se adelanta.
        self.owned = workspace_id == REGISTRY_WORKSPACE_ID
        #: Una condición estable se registra al cambiar, no en cada vuelta.
        self._said_foreign = False

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

    def _head(self, ws: Any) -> str:
        """Commit del clon. El backend monta el mismo share, así que es git local."""
        try:
            done = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, timeout=15
            )
        except (OSError, subprocess.SubprocessError) as ex:
            logger.warning("HEAD del registro ilegible: %s", ex)
            return ""
        return done.stdout.decode().strip() if done.returncode == 0 else ""

    async def _fast_forward(self) -> None:  # noqa: D401
        """Adelanta el registro antes de leerlo: un clon que nadie sincroniza es
        una foto que se pudre y cada merge lo deja más atrás.

        Sólo en el workspace propio (``incident-registry``): el del usuario en
        Monaco y el de los agentes se leen tal cual, nunca se les hace merge por
        debajo. Si el árbol divergió, git falla, se registra el motivo y se sigue
        con lo que hay; el commit queda en la evidencia, así que un registro
        viejo se delata en vez de mentir en silencio."""
        if not self.owned:
            if not self._said_foreign:
                self._said_foreign = True
                logger.info(
                    "Registro en un workspace ajeno (%s): se lee tal cual, sin "
                    "adelantar",
                    self.workspace_id,
                )
            return
        try:
            evidence = await self.execute(
                "git fetch --quiet origin && git merge --ff-only --quiet @{u}", ""
            )
        except Exception as ex:  # la capacidad no está disponible: se lee lo que hay
            logger.warning("Registro sin adelantar (%s): %s", type(ex).__name__, ex)
            return
        if evidence.exit_code != 0:
            logger.warning(
                "Registro sin adelantar (exit %d): %s",
                evidence.exit_code,
                (evidence.stderr or evidence.stdout).strip()[-200:],
            )

    async def registry(self) -> list[dict[str, Any]]:
        await self._fast_forward()
        ws = workspace_for(self.user_id, self.workspace_id)
        self.source = self._head(ws)
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
            source=self.source,
        )


def discover() -> Optional[WorkspaceCapability]:
    """El workspace que contiene ``docs/incidents``. ``None`` si no hay ninguno
    o si hay varios: con varios no se adivina, se nombran en el log."""
    if not WORKSPACE_ROOT.is_dir():
        logger.info("Registro de INC: no existe la raíz %s", WORKSPACE_ROOT)
        return None
    found: list[tuple[str, str]] = []
    for user_dir in sorted(p for p in WORKSPACE_ROOT.iterdir() if p.is_dir()):
        for ws in sorted(user_dir.iterdir()):
            if (ws.is_dir() or ws.is_symlink()) and any(
                (ws / INCIDENTS_DIR).glob("*.json")
            ):
                found.append((user_dir.name, ws.name))
    # El workspace propio gana sin ambigüedad: es el único que el reconciliador
    # posee y adelanta. Los demás sólo cuentan si no existe.
    owned = [f for f in found if f[1] == REGISTRY_WORKSPACE_ID]
    if len(owned) == 1:
        user_id, workspace_id = owned[0]
        logger.info("Registro de INC (propio): %s/%s", user_id, workspace_id)
        return WorkspaceCapability(user_id=user_id, workspace_id=workspace_id)
    if not found:
        logger.info("Registro de INC: ningún workspace contiene %s", INCIDENTS_DIR)
        return None
    if len(found) > 1:
        logger.warning(
            "Registro de INC ambiguo (%d): %s. El reconciliador no origina "
            "trabajo hasta que quede uno.",
            len(found),
            ", ".join(f"{u}/{w}" for u, w in found),
        )
        return None
    user_id, workspace_id = found[0]
    logger.info("Registro de INC: %s/%s", user_id, workspace_id)
    return WorkspaceCapability(user_id=user_id, workspace_id=workspace_id)


def bind(capability: WorkspaceCapability) -> tuple[Registry, Executor]:
    return capability.registry, capability.execute
