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

Un mismo registro puede ser alcanzable bajo VARIAS identidades (el mismo clon
enlazado desde varios ``user_id``). Eso sigue siendo UN registro: los
candidatos se agrupan por la ruta física que resuelven, y entre los alias de
uno se elige la identidad de trabajo — ``INCIDENT_REGISTRY_USER_ID`` desempata
si está declarada. La ambigüedad que detiene al reconciliador es la de verdad:
dos registros DISTINTOS.
"""

import json
import logging
import os
import subprocess
from typing import Any

from agent_framework import MCPStreamableHTTPTool

from v4.common.services.workspace_service import (
    _SAFE_ID,
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
        tool: MCPStreamableHTTPTool | None = None,
        owned: bool = False,
    ) -> None:
        self.user_id = user_id
        self.workspace_id = workspace_id
        self._tool = tool
        #: Commit del registro leído en el último ``registry()``.
        self.source = ""
        #: Se adelanta el registro que ``discover()`` ELIGIÓ, y sólo ése.
        #: Antes esto se decidía por el NOMBRE del workspace
        #: (``incident-registry``): como nadie lo bautiza así —un usuario crea
        #: el workspace con un nombre y una URL, no conoce el concepto de
        #: workspace_id— el adelanto no corría nunca y el clon se podría
        #: (medido en prod: clon en un commit anterior a ``docs/incidents``).
        #: El nombre sigue sirviendo, pero sólo como desempate en ``discover``.
        #: Adelantar es ``--ff-only``: no reescribe ni descarta nada; ante
        #: divergencia falla, se registra y el registro se lee como está.
        self.owned = owned
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
            # Mismo pre-flight que ``_git``: sobre el share el dueño del árbol no
            # es el uid del proceso y git aborta por "dubious ownership", con lo
            # que ``source`` quedaba vacío y la evidencia no decía de qué árbol
            # salió el veredicto.
            done = subprocess.run(
                ["git", "-c", f"safe.directory={ws}", "rev-parse", "HEAD"],
                cwd=ws,
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as ex:
            logger.warning("HEAD del registro ilegible: %s", ex)
            return ""
        return done.stdout.decode().strip() if done.returncode == 0 else ""

    async def _fast_forward(self) -> None:  # noqa: D401
        """Adelanta el registro antes de leerlo: un clon que nadie sincroniza es
        una foto que se pudre y cada merge lo deja más atrás.

        Sólo en el registro que ``discover()`` eligió; cualquier otro workspace
        se lee tal cual. ``--ff-only`` es la garantía: no reescribe historia ni
        descarta commits, y si el árbol divergió git falla, se registra el motivo
        y se sigue con lo que hay; el commit queda en la evidencia, así que un
        registro viejo se delata en vez de mentir en silencio."""
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


def _pick_identity(aliases: list[tuple[str, str]]) -> tuple[str, str]:
    """De varios alias del MISMO registro, la identidad con la que se trabaja.

    El reconciliador no tiene identidad propia: usa la que sale de aquí para
    llamar ``workspace_exec`` en ca-mcp, así que el ``user_id`` elegido tiene
    que ser uno bajo el que el workspace es alcanzable. Todos los alias lo son.
    ``INCIDENT_REGISTRY_USER_ID`` desempata cuando está declarado; si no, el
    primero en orden, que es estable entre arranques.
    """
    declared = (os.environ.get("INCIDENT_REGISTRY_USER_ID") or "").strip()
    if declared:
        for alias in aliases:
            if alias[0] == declared:
                return alias
        if len(aliases) > 1:
            logger.info(
                "INCIDENT_REGISTRY_USER_ID=%s no está entre los alias del "
                "registro (%s); se toma el primero.",
                declared,
                ", ".join(u for u, _ in aliases),
            )
    return aliases[0]


def discover() -> WorkspaceCapability | None:
    """El workspace que contiene ``docs/incidents``. ``None`` si no hay ninguno
    o si hay varios: con varios no se adivina, se nombran en el log."""
    if not WORKSPACE_ROOT.is_dir():
        logger.info("Registro de INC: no existe la raíz %s", WORKSPACE_ROOT)
        return None
    # Candidatos por CONTENIDO, agrupados por la ruta FÍSICA que resuelven: el
    # mismo registro alcanzable bajo varias identidades (un clon montado y
    # enlazado desde varios user_id) es UN registro, no varios. Comparar
    # nombres lo contaba como ambigüedad y el reconciliador no originaba
    # trabajo nunca (medido 2026-09-24: tres alias del mismo directorio).
    by_path: dict[str, list[tuple[str, str]]] = {}
    for user_dir in sorted(
        p for p in WORKSPACE_ROOT.iterdir() if p.is_dir() and _SAFE_ID.match(p.name)
    ):
        for ws in sorted(user_dir.iterdir()):
            # Sólo identidades y workspaces con nombre VÁLIDO. Un borrado que no
            # pudo terminar deja un resto ``.deleting-<id>-<hex>`` con el árbol
            # entero dentro; sin esta cerca contaba como un segundo registro,
            # ``discover`` se declaraba ambiguo y el reconciliador dejaba de
            # originar trabajo por basura que el usuario ya no ve.
            if not _SAFE_ID.match(ws.name):
                continue
            if (ws.is_dir() or ws.is_symlink()) and any(
                (ws / INCIDENTS_DIR).glob("*.json")
            ):
                by_path.setdefault(str(ws.resolve()), []).append(
                    (user_dir.name, ws.name)
                )
    found: list[tuple[str, str]] = [_pick_identity(a) for a in by_path.values()]
    # El workspace con el nombre canónico gana el DESEMPATE entre varios
    # registros; no es lo que define que el reconciliador lo adelante (eso lo
    # define haber sido elegido acá). Los demás sólo cuentan si no existe.
    canonical = [f for f in found if f[1] == REGISTRY_WORKSPACE_ID]
    if len(canonical) == 1:
        user_id, workspace_id = canonical[0]
        logger.info("Registro de INC (propio): %s/%s", user_id, workspace_id)
        return WorkspaceCapability(
            user_id=user_id, workspace_id=workspace_id, owned=True
        )
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
    return WorkspaceCapability(user_id=user_id, workspace_id=workspace_id, owned=True)


def bind(capability: WorkspaceCapability) -> tuple[Registry, Executor]:
    return capability.registry, capability.execute


class RegistryProvider:
    """``discover`` + ``bind`` reintentables, dueños de la capacidad que crean.

    El registro es un clon en el share: existe cuando alguien lo crea desde la
    UI, que puede ser mucho después de este arranque. Descubrirlo una sola vez
    dejaba al reconciliador sin registro NI ejecutor hasta el próximo reinicio
    (medido en prod: la revisión viva arrancó con "ningún workspace contiene
    docs/incidents" y quedó así). Devuelve el par la primera vez que vincula y
    ``None`` mientras no haya registro o cuando ya está vinculado.
    """

    def __init__(self) -> None:
        self._capability: WorkspaceCapability | None = None

    def __call__(self) -> tuple[Registry, Executor] | None:
        if self._capability is not None:
            return None
        capability = discover()
        if capability is None:
            return None
        self._capability = capability
        return bind(capability)

    async def aclose(self) -> None:
        if self._capability is not None:
            await self._capability.aclose()
            self._capability = None
