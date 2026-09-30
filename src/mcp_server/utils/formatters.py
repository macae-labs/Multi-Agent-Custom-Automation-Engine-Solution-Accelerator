"""
Response formatting utilities for MCP tools.

UN solo envelope, serializable, para éxito y para error:

    {"status": "success" | "error", "action": <str>, "summary": <str>, "details": {...}}

Antes el éxito era JSON y el error markdown ("##### ❌ Error … AGENT SUMMARY: …"):
dos formas para la misma cosa, y cada consumidor —el agente, el backend
(``workspace_capability.execute``) y los tests— tenía que saber cuál era cuál y
adivinar por el primer carácter. Con una sola forma lo dice ``status``.
"""

import json
from typing import Any


def _envelope(
    status: str,
    action: str | None,
    summary: str | None,
    details: dict[str, Any] | None,
) -> str:
    payload: dict[str, Any] = {"status": status}
    if action:
        payload["action"] = action
    if summary:
        payload["summary"] = summary
    if details:
        payload["details"] = details
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def format_mcp_response(
    title: str,
    content: dict[str, Any],
    agent_summary: str,
    additional_instructions: str | None = None,
) -> str:
    """Respuesta genérica: ``title`` es la acción, ``content`` los detalles."""
    details = dict(content)
    if additional_instructions:
        details["instructions"] = additional_instructions
    return _envelope("success", title, agent_summary, details)


def format_error_response(error_message: str, context: str | None = None) -> str:
    """Error con el MISMO envelope que el éxito: ``status: error``, la acción en
    ``action`` (el ``context`` de la tool) y el motivo en ``summary``."""
    return _envelope("error", context, error_message, None)


def format_success_response(action: str, details: dict[str, Any], summary: str | None = None) -> str:
    """Éxito con el payload completo, para que el cliente razone sobre él."""
    # Backwards compatibility: tools that return a single 'result' string
    # (e.g., show_tables, data_provider) keep their existing behavior.
    if isinstance(details, dict) and set(details.keys()) == {"result"}:
        return str(details["result"])
    return _envelope("success", action, summary, details)
