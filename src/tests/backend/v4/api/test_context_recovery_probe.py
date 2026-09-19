"""Probe determinístico del seam persist→recover y de la compuerta [turn-log].

Historia: el ledger de tools de un turno ("[turn-log]\\n<server>.<tool>(args)
-> <resultado>") se persistía DENTRO del content del mensaje assistant y
recover lo reinyectaba verbatim como prosa del asistente. El modelo veía
turnos "suyos" con turn-logs y SHAs, imitaba el patrón sin llamar a ninguna
tool (Router decision function=<none>) y fabricaba commits mezclando SHAs
reales de turnos viejos con inventados. Reproducido en prod 2026-09-13 23:45.

Contrato vigente (router.py, b9817edd):
  * El ledger va a metadata.turn_log, NUNCA a content; content se sanea antes
    de persistir.
  * recover limpia cualquier bloque [turn-log] heredado (Cosmos o Search): el
    modelo nunca ve deeds de tools como prosa propia.
  * En un turno sin tool el router NO emite texto con el marcador: por
    construcción sólo el backend escribe "[turn-log]", así que su aparición en
    texto del modelo es fabricación y se trunca en ese punto, incluso si el
    marcador llega partido entre deltas.
  * Los deeds son registros ESTRUCTURADOS (_make_deed: server, tool, status,
    args/result con longitud real y flag `truncated`), nunca strings
    recortados en silencio; más de _LEDGER_MAX_DEEDS por turno se declara en
    metadata.turn_log_dropped.
  * recover reinyecta la evidencia de ejecución como mensaje `system`
    (_tool_deeds_note), pegado al turno del asistente que la produjo, sin
    marcador, nunca persistido ni indexado. Sin ella el modelo se retracta de
    resultados correctos (en vivo: "ese SHA fue inventado por mí").

Este archivo corre AISLADO en test.yml (importa v4.api.router real).
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from v4.api.router import (
    _ACTIVE_TURNS,
    _DEED_REPLAY_RESULT_CHARS,
    _DEED_RESULT_CAP,
    _HostedTextContent,
    _HostedUpdate,
    _make_deed,
    _recover_session_context,
    _RouterChatClient,
    _strip_turn_log_block,
    _tool_deeds_note,
    abort_chat_turn,
)

PROSE = "Respuesta basada en la ejecución real."
LEDGER = (
    "MacaeMcpServer.workspace_list_entries(...)"
    " -> 20 directories, 37 files in '/' of workspace "
    "'multi-agent-custom-automation-engine-solution-accelerator'."
)
# Formato EXACTO con que se persistía antes (y que puede seguir en Cosmos/Search).
LEGACY_CONTENT = PROSE + "\n\n[turn-log]\n" + LEDGER


def _search_stub(hits):
    stub = MagicMock()
    stub.search_chat_history = AsyncMock(return_value=hits)
    return stub


def _chat_svc(messages):
    svc = MagicMock()
    svc.get_session = AsyncMock(return_value={"messages": messages})
    return svc


# ── _strip_turn_log_block ────────────────────────────────────────────────────


def test_strip_turn_log_block_contract():
    assert _strip_turn_log_block(LEGACY_CONTENT) == PROSE
    assert _strip_turn_log_block(PROSE) == PROSE
    assert _strip_turn_log_block("[turn-log]\n" + LEDGER) == ""
    assert _strip_turn_log_block(None) == ""
    assert _strip_turn_log_block("") == ""


# ── recover ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovered_session_history_keeps_prose_and_drops_turn_log():
    """Cosmos tiene un assistant heredado con [turn-log]: la prosa sobrevive,
    el ledger no entra al historial del modelo."""
    with patch(
        "common.services.search_index_service.get_search_index_service",
        AsyncMock(return_value=_search_stub([])),
    ):
        history = await _recover_session_context(
            _chat_svc(
                [
                    {"role": "user", "content": "valida el workspace"},
                    {"role": "assistant", "content": LEGACY_CONTENT},
                ]
            ),
            "sess-probe",
            "user-probe",
            current_message="¿qué directorios listaste?",
        )

    assistant = [m for m in history if m.get("role") == "assistant"]
    assert assistant, "la respuesta del turno 1 desapareció del historial"
    assert assistant[0]["content"] == PROSE
    joined = "\n".join(m.get("content", "") for m in history)
    assert "[turn-log]" not in joined
    assert "workspace_list_entries" not in joined


@pytest.mark.asyncio
async def test_recovered_search_hits_are_sanitized_too():
    """AI Search devuelve turnos de OTRAS sesiones ya indexados con [turn-log]:
    también se limpian. Es la vía por la que un chat nuevo se contaminaba."""
    hit = {
        "role": "assistant",
        "content": LEGACY_CONTENT,
        "timestamp": "2026-09-13T23:45:00Z",
        # De OTRA sesión: la memoria corta ya trae la actual en orden.
        "session_id": "sess-vieja",
    }
    with patch(
        "common.services.search_index_service.get_search_index_service",
        AsyncMock(return_value=_search_stub([hit])),
    ):
        history = await _recover_session_context(
            _chat_svc([]),
            "sess-new",
            "user-probe",
            current_message="último commit de la rama",
        )

    joined = "\n".join(m.get("content", "") for m in history)
    assert PROSE in joined
    assert "[turn-log]" not in joined
    assert "workspace_list_entries" not in joined


@pytest.mark.asyncio
async def test_recovered_history_replays_tool_deeds_as_system_evidence():
    """El ledger vive en metadata.turn_log. recover lo reinyecta como mensaje
    `system` pegado al turno del asistente que lo produjo: evidencia de que
    hubo ejecución real. Sin ella el modelo se retracta de resultados
    correctos (en vivo: devolvió el SHA real y dijo "ese SHA lo inventé").
    Nunca con el marcador ni en la voz del asistente."""
    deeds = [
        # Registro estructurado (persist actual).
        _make_deed(
            "MacaeMcpServer",
            "GitHub___list_commits",
            '{"sha":"stable/v4-baseline","per_page":1}',
            "success",
            '[{"sha":"01d5ccdb","message":"feat: estado persistente"}]',
        ),
        # Legado: string ya acotado, persistido antes del registro estructurado.
        'MacaeMcpServer.connect_from_registry({"server_name":"tool-box"}) -> connected',
    ]
    with patch(
        "common.services.search_index_service.get_search_index_service",
        AsyncMock(return_value=_search_stub([])),
    ):
        history = await _recover_session_context(
            _chat_svc(
                [
                    {"role": "user", "content": "último commit de stable"},
                    {
                        "role": "assistant",
                        "content": "SHA corto: 01d5ccd",
                        "metadata": {"turn_log": deeds},
                    },
                ]
            ),
            "sess-probe",
            "user-probe",
            current_message="¿qué SHA me diste?",
        )

    assert [m["role"] for m in history] == ["user", "assistant", "system"]
    assert history[1]["content"] == "SHA corto: 01d5ccd"
    note = history[2]["content"]
    assert "GitHub___list_commits @ MacaeMcpServer — estado: success" in note
    assert "01d5ccdb" in note and "feat: estado persistente" in note
    assert "connect_from_registry" in note  # legado se muestra tal cual
    assert "[turn-log]" not in note


def test_make_deed_never_truncates_silently():
    big = "x" * (_DEED_RESULT_CAP + 5)
    deed = _make_deed("S", "t", "{}", "success", big)
    assert deed["result"]["chars"] == _DEED_RESULT_CAP + 5
    assert deed["result"]["truncated"] is True
    assert len(deed["result"]["text"]) == _DEED_RESULT_CAP
    small = _make_deed("S", "t", "{}", "error", "boom")
    assert small["result"] == {"text": "boom", "chars": 4, "truncated": False}
    assert small["status"] == "error"


def test_tool_deeds_note_contract():
    assert _tool_deeds_note(None) == ""
    assert _tool_deeds_note([]) == ""
    assert _tool_deeds_note("not a list") == ""
    # Legado (strings) intacto y sin marcador.
    note = _tool_deeds_note(["a -> 1", "", "b -> 2"])
    assert note.endswith("- a -> 1\n- b -> 2")
    assert "[turn-log]" not in note
    # Replay acotado con el resto DECLARADO, nunca cortado en silencio.
    long_result = "r" * (_DEED_REPLAY_RESULT_CHARS + 250)
    note = _tool_deeds_note([_make_deed("S", "t", "{}", "success", long_result)])
    assert "r" * _DEED_REPLAY_RESULT_CHARS in note
    assert "(… 250 caracteres más en el registro)" in note
    # Ejecuciones más allá del máximo por turno quedan declaradas.
    note = _tool_deeds_note([_make_deed("S", "t", "{}", "success", "ok")], dropped=3)
    assert note.endswith("- (+3 ejecuciones más de este turno sin registro)")


# ── compuerta en el stream del router (turno sin tool) ──────────────────────


class _FakeStream:
    def __init__(self, texts):
        self._texts = list(texts)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._texts:
            raise StopAsyncIteration
        text = self._texts.pop(0)
        delta = SimpleNamespace(content=text, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _fake_openai(texts):
    """Sustituto de openai.AsyncOpenAI: chat.completions.create(stream=True)
    entrega `texts` como deltas de contenido, sin tool_calls (function=<none>)."""

    class _FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(return_value=_FakeStream(texts))
                )
            )

        async def close(self):
            return None

    return _FakeOpenAI


def _client():
    c = _RouterChatClient.__new__(_RouterChatClient)
    c._router_base_url = "https://router.invalid"
    c._router_api_version = "2025-01-01"
    c._router_model = "model-router"
    c._bearer = AsyncMock(return_value="tok")
    return c


async def _direct_text(texts, client=None):
    out: list[str] = []
    with patch("openai.AsyncOpenAI", _fake_openai(texts)):
        async for update in (client or _client()).invoke("¿último commit?", history=[]):
            for content in getattr(update, "contents", []) or []:
                if getattr(content, "type", None) == "text":
                    out.append(getattr(content, "text", "") or "")
    return "".join(out)


@pytest.mark.asyncio
async def test_router_direct_answer_without_marker_flows_complete():
    text = await _direct_text(["Último commit: ", "8358385c ", "en stable."])
    assert text == "Último commit: 8358385c en stable."


@pytest.mark.asyncio
async def test_router_direct_answer_truncated_at_turn_log_marker(caplog):
    """El modelo 'responde' con un turn-log fabricado, con el marcador partido
    entre deltas. Sale la prosa previa; nada desde el marcador en adelante."""
    caplog.set_level(logging.WARNING, logger="v4.api.router")
    text = await _direct_text(
        [
            "Consultando GitHub",
            " en tiempo real.",
            "\n\n[turn",
            '-log]\nrun_macae_mcp_server("GitHub___list_commits")',
            ' -> [{"sha":"a7c3f902"}]',
            "\nOtra línea inventada.",
        ]
    )
    assert text.strip() == "Consultando GitHub en tiempo real."
    assert "[turn-log]" not in text
    assert "a7c3f902" not in text
    assert "inventada" not in text
    assert any("[turn-log]" in r.getMessage() for r in caplog.records), (
        "la compuerta debe dejar rastro en el log del backend"
    )


@pytest.mark.asyncio
async def test_router_answer_that_is_only_a_turn_log_falls_back_to_execution(caplog):
    """La respuesta entera es un turn-log fabricado: no llega NADA de ese texto
    al usuario y, como el router no respondió ni eligió capability, el turno
    cae a la ejecución real (o4-mini + Toolbox) en vez de a la fabricación."""
    caplog.set_level(logging.INFO, logger="v4.api.router")
    client = _client()
    executed: list[tuple] = []

    async def _fake_execute(prompt, history, **kwargs):
        executed.append((prompt, history))
        yield _HostedUpdate([_HostedTextContent("respuesta de ejecución real")])

    client._execute_responses = _fake_execute

    text = await _direct_text(
        ["[turn-log]\n", "MacaeMcpServer.x() -> fabricado"], client=client
    )
    assert text == "respuesta de ejecución real"
    assert "fabricado" not in text
    assert executed == [("¿último commit?", [])]
    assert any("Router produced nothing" in r.getMessage() for r in caplog.records)


# ── abort de turno por identidad ─────────────────────────────────────────────
# El ingress de Container Apps no propaga el cierre del cliente al contenedor
# (medido contra rev 118): el abort viaja por identidad (user_id, turn_id).


def _req(user_id: str) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [
            (b"x-ms-client-principal-id", user_id.encode()),
            (b"x-ms-client-principal-name", b"probe"),
            # EasyAuth siempre inyecta el access token. Sin él, en APP_ENV=dev
            # get_authenticated_user_details lanza DeviceCodeCredential (login
            # interactivo) y espera a un humano: CI quedó 33 min colgado aquí
            # (2026-09-14). Local no lo veía por MACAE_DEV_OBO_TOKEN en .env.
            (b"x-ms-token-aad-access-token", b"test-access-token"),
        ],
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_abort_marks_only_the_active_turn_of_the_same_user():
    _ACTIVE_TURNS.clear()
    _ACTIVE_TURNS[("u1", "t1")] = False
    try:
        assert await abort_chat_turn("t1", _req("u1")) == {
            "turn_id": "t1",
            "aborted": True,
        }
        assert _ACTIVE_TURNS[("u1", "t1")] is True
        # Otro usuario no puede abortar un turno ajeno; un turno inexistente
        # (ya cerrado o nunca abierto) no deja marca alguna.
        assert (await abort_chat_turn("t1", _req("u2")))["aborted"] is False
        assert (await abort_chat_turn("nope", _req("u1")))["aborted"] is False
        assert set(_ACTIVE_TURNS) == {("u1", "t1")}
    finally:
        _ACTIVE_TURNS.clear()
