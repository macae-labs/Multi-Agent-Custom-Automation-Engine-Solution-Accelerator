"""Señales vivas para el reconciliador: alertas disparadas en Azure Monitor.

Cierra el corte reactivo del circuito (telemetría → detección → ``incident_detected``):
el reconciliador llama a esta fuente en cada barrido y empareja lo que devuelve con
``signature.alert_match`` de las INC (``incident_revalidation.match_alerts``).

Hechos medidos 2026-10-02 que fijan el diseño:

* Las alertas se leen por sondeo con la credencial del proceso (identidad
  administrada en prod, ``az login`` en dev) desde Alerts Management. No hay
  webhook: el backend está detrás de EasyAuth y Azure Monitor no puede
  autenticarse ahí; el grupo de acciones ``ag-macae`` no tiene receptores.
* Las reglas de consulta programada se crearon sin dimensiones de división, así
  que la alerta trae UNA dimensión (``_ResourceId``). Las dimensiones reales que
  las firmas declaran (``Name``, ``ResultCode``, ``ExceptionType``…) sólo existen
  en la consulta que la propia alerta trae en ``linkToFilteredSearchResultsAPI``;
  se ejecuta contra Log Analytics y cada fila es una señal.
* Las reglas se resuelven solas a los 5 min y el barrido es cada 300 s: se
  listan las alertas del último ``time_range`` sin filtrar por estado; la
  identidad de la señal (alerta + dimensiones) hace idempotente el emparejamiento.

Contrato de salida (``AlertSource``): ``{"id", "rule", "dimensions"}`` por señal.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ARM = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
LOGS_SCOPE = "https://api.loganalytics.io/.default"
API_VERSION = "2019-05-05-preview"
#: Los únicos rangos que Alerts Management acepta (otro valor → 400, medido).
TIME_RANGES = ("1h", "1d", "7d", "30d")
#: Columnas de la consulta de la alerta que no son dimensiones de la señal.
_NOT_DIMENSIONS = frozenset({"TimeGenerated", "AggregatedValue"})


def rule_name(alert_rule_id: str) -> str:
    """El nombre corto de la regla (último segmento del id ARM): es lo que las
    firmas declaran en ``alert_match[].rule``."""
    return alert_rule_id.rstrip("/").rsplit("/", 1)[-1]


def signal_identity(alert_id: str, dimensions: dict[str, str]) -> str:
    """Identidad estable de una señal: la alerta más SUS dimensiones.

    Una alerta expandida en varias filas es varias señales; el orden de las
    filas entre dos lecturas no puede cambiar la identidad (volvería a originar
    trabajo por la misma causa), así que entra el contenido, no la posición.

    La identidad termina en el ``id`` del ``work_event`` en Cosmos, que prohíbe
    ``/``, ``\\``, ``?`` y ``#`` (medido 2026-10-02 en el loop vivo: "Id contains
    illegal chars" al escribir el ``incident_detected``); el separador es ``.``.
    """
    if not dimensions:
        return alert_id
    digest = hashlib.sha1(
        "|".join(f"{k}={v}" for k, v in sorted(dimensions.items())).encode("utf-8")
    ).hexdigest()[:10]
    return f"{alert_id}.{digest}"


def expand_rows(table: dict[str, Any]) -> list[dict[str, str]]:
    """Filas de la consulta de la alerta → una dimensión por columna."""
    columns = [str(c.get("name") or "") for c in table.get("columns") or []]
    rows: list[dict[str, str]] = []
    for row in table.get("rows") or []:
        dims = {
            name: str(value)
            for name, value in zip(columns, row, strict=False)
            if name and name not in _NOT_DIMENSIONS and value is not None
        }
        if dims:
            rows.append(dims)
    return rows


def _condition(alert: dict[str, Any]) -> dict[str, Any]:
    """La condición evaluada (``allOf[0]``) tal como la publica Alerts
    Management: ``properties.context.context.condition`` en las de Log."""
    props = alert.get("properties") or {}
    ctx = props.get("context") or {}
    inner = ctx.get("context") or ctx
    all_of = (inner.get("condition") or {}).get("allOf") or []
    return dict(all_of[0]) if all_of else {}


class AzureMonitorAlertSource:
    """``AlertSource`` sobre Alerts Management + Log Analytics."""

    def __init__(
        self,
        subscription_id: str,
        resource_group: str,
        credential: Any,
        *,
        time_range: str = "1h",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._subscription_id = subscription_id
        self._resource_group = resource_group
        self._credential = credential
        if time_range not in TIME_RANGES:
            logger.warning(
                "ALERTS_TIME_RANGE %r no es uno de %s; se usa 1h",
                time_range,
                TIME_RANGES,
            )
            time_range = "1h"
        self._time_range = time_range
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=30.0)

    @classmethod
    def from_config(cls, config: Any) -> AzureMonitorAlertSource | None:
        """La fuente del proceso, o ``None`` si no hay suscripción y grupo:
        entonces el loop sólo origina trabajo por vencimiento de INC."""
        subscription = str(getattr(config, "ALERTS_SUBSCRIPTION_ID", "") or "")
        group = str(getattr(config, "ALERTS_RESOURCE_GROUP", "") or "")
        if not subscription or not group:
            logger.info(
                "Sin ALERTS_SUBSCRIPTION_ID/ALERTS_RESOURCE_GROUP: el reconciliador "
                "no recibe alertas (sólo vencimientos)"
            )
            return None
        return cls(
            subscription,
            group,
            config.get_shared_async_credential(),
            time_range=str(getattr(config, "ALERTS_TIME_RANGE", "") or "1h"),
        )

    async def __call__(self) -> list[dict[str, Any]]:
        try:
            alerts = await self._alerts()
        except Exception as e:  # la fuente nunca tumba el barrido
            logger.warning("Alertas no disponibles (%s): %s", type(e).__name__, e)
            return []
        signals: list[dict[str, Any]] = []
        seen: set[str] = set()
        for alert in alerts:
            try:
                for signal in await self._signals(alert):
                    if signal["id"] in seen:
                        continue
                    seen.add(signal["id"])
                    signals.append(signal)
            except Exception as e:
                logger.warning(
                    "Alerta %s no expandible (%s): %s",
                    alert.get("name"),
                    type(e).__name__,
                    e,
                )
        if signals:
            logger.info(
                "Señales vivas: %d (alertas %d, últimas %s)",
                len(signals),
                len(alerts),
                self._time_range,
            )
        return signals

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    async def _headers(self, scope: str) -> dict[str, str]:
        token = await self._credential.get_token(scope)
        return {"Authorization": f"Bearer {token.token}"}

    async def _alerts(self) -> list[dict[str, Any]]:
        url: str | None = (
            f"{ARM}/subscriptions/{self._subscription_id}"
            "/providers/Microsoft.AlertsManagement/alerts"
        )
        params: dict[str, str] | None = {
            "api-version": API_VERSION,
            "targetResourceGroup": self._resource_group,
            "timeRange": self._time_range,
            "includeContext": "true",
        }
        headers = await self._headers(ARM_SCOPE)
        found: list[dict[str, Any]] = []
        while url:
            response = await self._http.get(url, params=params, headers=headers)
            response.raise_for_status()
            body = response.json()
            found.extend(body.get("value") or [])
            url, params = body.get("nextLink"), None
        return found

    async def _signals(self, alert: dict[str, Any]) -> list[dict[str, Any]]:
        essentials = (alert.get("properties") or {}).get("essentials") or {}
        rule = rule_name(str(essentials.get("alertRule") or ""))
        # En la lista, ``name`` es el NOMBRE DE LA REGLA (medido 2026-10-02) y la
        # instancia de la alerta es el GUID al final de ``id``. Con el nombre,
        # toda nueva aparición de la misma regla y dimensiones colapsaría en la
        # primera y nunca volvería a originar trabajo.
        alert_id = str(alert.get("id") or "").rstrip("/").rsplit("/", 1)[-1] or str(
            alert.get("name") or ""
        )
        if not rule or not alert_id:
            return []
        condition = _condition(alert)
        base = {
            str(d["name"]): str(d.get("value"))
            for d in condition.get("dimensions") or []
            if d.get("name")
        }
        link = condition.get("linkToFilteredSearchResultsAPI")
        rows = await self._rows(str(link)) if link else []
        dimension_sets = [{**base, **row} for row in rows] or [base]
        return [
            {
                "id": signal_identity(alert_id, dims),
                "rule": rule,
                "dimensions": dims,
                "fired": essentials.get("startDateTime"),
                "state": essentials.get("monitorCondition"),
            }
            for dims in dimension_sets
        ]

    async def _rows(self, link: str) -> list[dict[str, str]]:
        response = await self._http.get(link, headers=await self._headers(LOGS_SCOPE))
        response.raise_for_status()
        tables = response.json().get("tables") or []
        return expand_rows(tables[0]) if tables else []
