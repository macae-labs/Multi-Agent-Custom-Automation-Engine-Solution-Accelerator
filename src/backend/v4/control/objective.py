"""La ley del dueño del objetivo, compartida por el chat y por el carril de plan.

Un objetivo tiene un dueño que ejecuta, observa, juzga y continúa hasta un
estado terminal. Hasta aquí había dos dueños con dos leyes: el turno del chat
(``_RouterChatClient``: objetivo, ledger, veredicto) y el manager Magentic del
plan (prompts propios de hechos, plan, progreso y respuesta final). Este
módulo es la ley única que ambos aplican:

- **Veredicto**: un juicio ESTRUCTURADO de la ejecución contra el objetivo
  (``goal_met``, ``blocked``, ``reason``, ``corrected_objective``). Nunca un
  prefijo en prosa. Si la evidencia contradice una premisa del objetivo, el
  veredicto devuelve el objetivo reescrito con el hecho medido y el dueño
  sigue con ése (medido 2026-09-30: una tarea con un puerto deliberadamente
  distinto del real, validación del usuario de la reacción del bucle).
- **Hecho**: la identidad de un dato observado es LO OBSERVADO, no cómo se
  obtuvo. Dos llamadas distintas que devuelven lo mismo son el mismo hecho;
  volver a obtenerlo no es progreso.
- **Ledger**: objetivo, hechos y veredictos se registran como ``work_event``
  por identidad (``objective``/``fact``/``verdict``), fuera del request. Un
  hecho repetido es un duplicado: la señal durable de "sin progreso".
"""

import hashlib
import json
import logging
from typing import Any

from common.services.event_store import get_event_store

logger = logging.getLogger(__name__)

VERDICT_INSTRUCTIONS = (
    "Verificá una ejecución YA OCURRIDA contra su objetivo. "
    "``goal_met`` es verdadero sólo si el objetivo quedó "
    "cumplido Y la evidencia lo respalda. Una capacidad que "
    "falló, una afirmación sin evidencia que la sostenga o un "
    "objetivo a medias son falso. ``reason`` explica en una "
    "línea qué falta o qué falló. ``blocked`` es verdadero "
    "SÓLO si eso no puede obtenerse con las capacidades "
    "disponibles en este entorno y la respuesta lo nombra como "
    "limitación; ofrecer continuar no es una limitación. "
    "``corrected_objective``: si la evidencia contradice una "
    "premisa del objetivo (un puerto, ruta, nombre o valor que "
    "la evidencia muestra distinto), es el objetivo reescrito "
    "con el hecho medido; si el objetivo se sostiene, cadena "
    "vacía. Juzgá contra el contrato, no contra el código de "
    "estado: una respuesta de error a un request inválido (id "
    "inexistente, cuerpo vacío) es comportamiento correcto, no "
    "una falla."
)

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal_met": {"type": "boolean"},
        "blocked": {"type": "boolean"},
        "reason": {"type": "string"},
        "corrected_objective": {"type": "string"},
    },
    "required": ["goal_met", "blocked", "reason", "corrected_objective"],
    "additionalProperties": False,
}


def verdict_input(objective: str, evidence: list[str], answer: str) -> str:
    """El material que juzga el veredicto: objetivo, evidencia y respuesta."""
    return (
        f"OBJETIVO:\n{objective}\n\n"
        "EVIDENCIA DE EJECUCIÓN:\n"
        + ("\n".join(evidence) or "(ninguna capacidad usada)")
        + f"\n\nRESPUESTA DADA:\n{answer or '(vacía)'}"
    )


def parse_verdict(text: str) -> tuple[str, str, str] | None:
    """``(kind, reason, corrected_objective)`` o ``None`` si no hay contrato.

    ``kind`` es ``done``, ``blocked`` o ``retry``. Sin el JSON del contrato no
    hay veredicto: no se adivina por el texto.
    """
    try:
        judged = json.loads(text)
        met = judged["goal_met"]
        reason = str(judged["reason"])[:300]
        blocked = judged.get("blocked") is True
        corrected = str(judged.get("corrected_objective") or "").strip()
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if met is True:
        return ("done", reason, "")
    return (
        "blocked" if blocked else "retry",
        reason or "objetivo no cumplido",
        corrected,
    )


def fact_key(output: str) -> str:
    """Identidad del DATO que devolvió una capacidad, no de cómo se la llamó.

    Para el envelope de ca-mcp cuenta lo observado (``stdout``/``stderr``/
    ``exit_code`` de una ejecución, o ``details``); el comando y el resumen
    describen la llamada. Cualquier otro texto se compara entero.
    """
    try:
        payload = json.loads(output)
    except (ValueError, TypeError):
        payload = None
    observed: Any = output
    if isinstance(payload, dict) and isinstance(payload.get("details"), dict):
        details = payload["details"]
        if "stdout" in details:
            observed = {k: details.get(k) for k in ("stdout", "stderr", "exit_code")}
        else:
            observed = details
    raw = (
        observed
        if isinstance(observed, str)
        else json.dumps(observed, sort_keys=True, ensure_ascii=False)
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


class Ledger:
    """El historial de un dueño de objetivo, por identidad, fuera del request.

    ``record`` devuelve ``True`` si el evento ya existía (duplicado), ``False``
    si es nuevo y ``None`` si el store no está disponible (dev sin Cosmos): el
    dueño sigue, el ledger es evidencia y no una dependencia; se avisa una vez.
    """

    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id
        self._unavailable = False

    async def record(
        self, kind: str, identity: str, payload: dict[str, Any]
    ) -> bool | None:
        try:
            result = await get_event_store().append(kind, identity, payload)
        except Exception as ex:
            if not self._unavailable:
                self._unavailable = True
                logger.warning(
                    "Ledger de %s no disponible (%s): %s",
                    self.owner_id,
                    type(ex).__name__,
                    ex,
                )
            return None
        return result.duplicate

    async def opened(self, payload: dict[str, Any]) -> bool | None:
        return await self.record("objective", self.owner_id, payload)

    async def fact(
        self, source: str, output: str, payload: dict[str, Any]
    ) -> tuple[str, bool | None]:
        """Registra un hecho; devuelve su identidad y si ya existía."""
        identity = f"{self.owner_id}:{source}:{fact_key(output)}"
        return identity, await self.record("fact", identity, payload)

    async def verdict(
        self, lap: int, kind: str, reason: str, corrected: str, new_facts: int
    ) -> bool | None:
        return await self.record(
            "verdict",
            f"{self.owner_id}:{lap}",
            {
                "kind": kind,
                "reason": reason,
                "corrected_objective": corrected,
                "new_facts": new_facts,
            },
        )

    async def closed(self, status: str, laps: int, facts: int) -> bool | None:
        return await self.record(
            "objective",
            f"{self.owner_id}:closed",
            {"status": status, "laps": laps, "facts": facts},
        )
