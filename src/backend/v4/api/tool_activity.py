"""Etiquetas de actividad de tools para la UI y la narración de voz.

Módulo puro (solo ``json``): se importa desde ``router.py`` y desde los tests
SIN arrastrar el router completo. Importar ``router`` en un test unitario lo
exponía a los mocks de ``sys.modules`` que otros tests dejan instalados y
obligaba a aislarlo en su propia sesión de pytest en el workflow.
"""

from __future__ import annotations

import json
from typing import Any


def describe_tool_call(
    tool_name: str, server_name: str, arguments: Any
) -> tuple[str, str]:
    """Nombre REAL de la tool y del servidor para narrar y mostrar.

    ``call_external_tool`` es un envoltorio: la tool y el servidor reales van
    en sus argumentos (``target_tool`` / ``server_name``) y, en el Toolbox
    (``call_tool``), un nivel más adentro (``arguments.name``). Sin esto la
    voz decía "Consultando call external tool en MacaeMcpServer" cuando la
    llamada real era ``GitHub___list_commits`` en ``tool-box``.
    """
    if tool_name != "call_external_tool":
        return tool_name, server_name
    args = arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return tool_name, server_name
    if not isinstance(args, dict):
        return tool_name, server_name
    tool = str(args.get("target_tool") or tool_name)
    server = str(args.get("server_name") or server_name)
    inner = args.get("arguments")
    if tool == "call_tool" and isinstance(inner, dict) and inner.get("name"):
        tool = str(inner["name"])
    return tool, server
