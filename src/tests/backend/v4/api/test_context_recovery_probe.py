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

Este archivo corre AISLADO en test.yml (importa v4.api.router real).
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from v4.api.router import (
    _HostedTextContent,
    _HostedUpdate,
    _recover_session_context,
    _RouterChatClient,
    _strip_turn_log_block,
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
