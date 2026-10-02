"""La fuente de señales vivas: alertas de Azure Monitor → ``{"id","rule","dimensions"}``.

Las alertas reales traen una sola dimensión (``_ResourceId``); las dimensiones
que las firmas declaran se obtienen ejecutando la consulta que la alerta trae.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from common.services.event_store import EventStore, MemoryContainer
from v4.control.alert_source import (
    AzureMonitorAlertSource,
    expand_rows,
    rule_name,
    signal_identity,
)
from v4.control.incident_revalidation import candidates, match_alerts

RULE = (
    "/subscriptions/s/resourceGroups/rg/providers/microsoft.insights/"
    "scheduledqueryrules/macae-api-5xx"
)
RESOURCE = (
    "/subscriptions/s/resourcegroups/rg/providers/microsoft.insights/components/appi"
)
LINK = "https://api.loganalytics.io/v1/workspaces/w/query?query=AppRequests&timespan=x"


class _Credential:
    def __init__(self):
        self.scopes = []

    async def get_token(self, *scopes):
        self.scopes.append(scopes[0])
        return SimpleNamespace(token="tok")


def _alert(name="a1", link=LINK, rule=RULE):
    condition = {
        "dimensions": [{"name": "_ResourceId", "value": RESOURCE}],
        "linkToFilteredSearchResultsAPI": link,
    }
    return {
        # Como lo publica Alerts Management: ``name`` es el nombre de la regla y
        # la instancia (GUID) va al final de ``id``.
        "name": "macae-api-5xx",
        "id": f"/subscriptions/s/resourcegroups/rg/providers/microsoft.insights/components/appi/providers/Microsoft.AlertsManagement/alerts/{name}",
        "properties": {
            "essentials": {
                "alertRule": rule,
                "monitorCondition": "Resolved",
                "startDateTime": "2026-10-02T02:38:48Z",
            },
            "context": {"context": {"condition": {"allOf": [condition]}}},
        },
    }


def _table(rows):
    return {
        "tables": [
            {
                "name": "PrimaryResult",
                "columns": [
                    {"name": "TimeGenerated"},
                    {"name": "_ResourceId"},
                    {"name": "Name"},
                    {"name": "ResultCode"},
                    {"name": "AggregatedValue"},
                ],
                "rows": rows,
            }
        ]
    }


def _source(pages, table=None, fail_logs=False):
    pages = list(pages)
    cred = _Credential()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        if "AlertsManagement" in str(request.url):
            assert request.url.params.get("includeContext") == "true" or pages[0].get(
                "paged"
            )
            return httpx.Response(200, json=pages.pop(0))
        if fail_logs:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json=table or _table([]))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AzureMonitorAlertSource("s", "rg", cred, time_range="1h", http=http), cred


@pytest.mark.asyncio
async def test_a_fired_alert_expands_into_one_signal_per_row_with_the_real_dimensions():
    rows = [
        ["2026-10-02T02:33:13Z", RESOURCE, "GET /api/v4/chat/sessions", "500", 3],
        [
            "2026-10-02T02:33:13Z",
            RESOURCE,
            "DELETE /api/v4/workspaces/{workspace_id}",
            "500",
            1,
        ],
    ]
    source, cred = _source([{"value": [_alert()]}], _table(rows))
    signals = await source()
    again, _ = _source([{"value": [_alert()]}], _table(list(reversed(rows))))

    assert [s["rule"] for s in signals] == ["macae-api-5xx", "macae-api-5xx"]
    assert signals[0]["dimensions"] == {
        "_ResourceId": RESOURCE,
        "Name": "GET /api/v4/chat/sessions",
        "ResultCode": "500",
    }
    assert len({s["id"] for s in signals}) == 2
    # La identidad va por contenido: otro orden de filas, mismas señales.
    assert {s["id"] for s in await again()} == {s["id"] for s in signals}
    assert cred.scopes == [
        "https://management.azure.com/.default",
        "https://api.loganalytics.io/.default",
    ]


@pytest.mark.asyncio
async def test_an_alert_without_search_link_keeps_its_own_dimensions():
    source, _ = _source([{"value": [_alert(link=None)]}])
    (signal,) = await source()
    assert signal["id"] == signal_identity("a1", {"_ResourceId": RESOURCE})
    assert signal["dimensions"] == {"_ResourceId": RESOURCE}


@pytest.mark.asyncio
async def test_pagination_follows_the_next_link():
    pages = [
        {
            "value": [_alert("a1", link=None)],
            "nextLink": "https://management.azure.com/subscriptions/s/providers/Microsoft.AlertsManagement/alerts?skipToken=1",
            "paged": True,
        },
        {"value": [_alert("a2", link=None)], "paged": True},
    ]
    source, _ = _source(pages)
    assert [s["id"] for s in await source()] == [
        signal_identity("a1", {"_ResourceId": RESOURCE}),
        signal_identity("a2", {"_ResourceId": RESOURCE}),
    ]


@pytest.mark.asyncio
async def test_unavailable_azure_yields_no_signals_and_never_raises():
    def handler(request):
        return httpx.Response(403, json={"error": {"code": "AuthorizationFailed"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = AzureMonitorAlertSource("s", "rg", _Credential(), http=http)
    assert await source() == []


@pytest.mark.asyncio
async def test_an_alert_whose_query_fails_is_skipped_not_fatal():
    source, _ = _source(
        [{"value": [_alert("a1"), _alert("a2", link=None)]}], fail_logs=True
    )
    assert [s["id"] for s in await source()] == [
        signal_identity("a2", {"_ResourceId": RESOURCE})
    ]


@pytest.mark.asyncio
async def test_expanded_signals_match_an_incident_signature_and_originate_work():
    incident = {
        "incident_id": "INC-2026-011",
        "signature": {
            "alert_match": [
                {
                    "rule": "macae-api-5xx",
                    "dimensions": {
                        "Name": "GET /api/v4/chat/sessions",
                        "ResultCode": "500",
                    },
                }
            ]
        },
        "authority_ceiling": {"max_action_class_without_human": "read-only"},
        "learn": {"executable_probe": {"class": "read-only", "command_or_test": "x"}},
    }
    rows = [["t", RESOURCE, "GET /api/v4/chat/sessions", "500", 1]]
    source, _ = _source([{"value": [_alert()]}], _table(rows))
    signals = await source()
    assert candidates(signals[0]["rule"], signals[0]["dimensions"], [incident])

    store = EventStore(MemoryContainer())
    assert await match_alerts(signals, [incident], store) == 1
    # Releer la misma alerta en el barrido siguiente no origina trabajo nuevo.
    assert await match_alerts(signals, [incident], store) == 0
    (event,) = await store.pending()
    assert event["kind"] == "incident_detected"
    assert event["payload"]["dimensions"]["Name"] == "GET /api/v4/chat/sessions"


def test_a_signal_identity_is_a_legal_cosmos_id():
    # El id del work_event es ``kind:identity``; Cosmos rechaza / \\ ? # (medido
    # en el loop vivo con el separador anterior, "#").
    identity = signal_identity(
        "112224fa-1341-1752-d67f-5f6fd3f90004", {"Name": "GET /x", "ResultCode": "500"}
    )
    assert not set(identity) & set("/\\?#"), identity
    assert identity.startswith("112224fa-1341-1752-d67f-5f6fd3f90004.")


def test_an_unsupported_time_range_falls_back_to_one_hour():
    # Alerts Management acepta sólo 1h/1d/7d/30d; otro valor es un 400 (medido).
    source = AzureMonitorAlertSource("s", "rg", _Credential(), time_range="2h")
    assert source._time_range == "1h"


def test_rule_name_and_rows_are_what_the_signature_sees():
    assert rule_name(RULE) == "macae-api-5xx"
    assert expand_rows(_table([["t", RESOURCE, "GET /x", "500", 2]])["tables"][0]) == [
        {"_ResourceId": RESOURCE, "Name": "GET /x", "ResultCode": "500"}
    ]
    assert json.dumps(expand_rows({"columns": [], "rows": []})) == "[]"


def test_without_subscription_and_group_there_is_no_source():
    assert AzureMonitorAlertSource.from_config(SimpleNamespace()) is None
    cfg = SimpleNamespace(
        ALERTS_SUBSCRIPTION_ID="s",
        ALERTS_RESOURCE_GROUP="rg",
        ALERTS_TIME_RANGE="2h",
        get_shared_async_credential=lambda: _Credential(),
    )
    assert isinstance(AzureMonitorAlertSource.from_config(cfg), AzureMonitorAlertSource)
