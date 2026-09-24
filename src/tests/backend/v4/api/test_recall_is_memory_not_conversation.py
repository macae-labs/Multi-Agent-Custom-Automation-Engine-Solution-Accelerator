"""La memoria larga entra como recuerdo, no como conversación.

Antes, `_recover_session_context` metía los quince resultados del índice con su
rol original: quince turnos de otras sesiones quedaban indistinguibles de lo que
el usuario acababa de escribir, y el Model Router componía el equipo con ellos
(agentes de audio para auditar un repositorio). Misma regla que ya rige para la
evidencia de herramientas: atribuida a ``system``, con procedencia.
"""

import importlib

import pytest

router = importlib.import_module("v4.api.router")


def hit(**kw):
    base = {
        "session_id": "otra-sesion",
        # Entre sesiones se recuerda lo que dijo el USUARIO; lo que dijo el
        # asistente en otra sesión no es un hecho (medido 2026-09-23/24: sus
        # respuestas viejas como recuerdo hacían repetir el mismo error).
        "role": "user",
        "content": "contenido recuperado",
        "timestamp": "2026-05-01T10:00:00Z",
        "session_name": "VoiceLive en iOS",
        "reranker_score": 3.0,
    }
    base.update(kw)
    return base


class FakeSearch:
    def __init__(self, hits):
        self.hits = hits
        self.top_k = None

    async def search_chat_history(self, query, user_id, top_k):
        self.top_k = top_k
        return self.hits


class FakeChat:
    def __init__(self, messages=()):
        self.messages = list(messages)

    async def get_session(self, session_id, user_id):
        return {"messages": self.messages}


@pytest.fixture
def _collaborators_patched(monkeypatch):
    """El servicio de búsqueda se resuelve en el namespace del módulo bajo test."""

    def install(hits):
        search = FakeSearch(hits)

        async def get_search_index_service():
            return search

        import common.services.search_index_service as svc_mod

        monkeypatch.setattr(
            svc_mod, "get_search_index_service", get_search_index_service
        )
        return search

    return install


async def recover(chat, session_id="sesion-actual", message="Audita el repositorio"):
    return await router._recover_session_context(chat, session_id, "u1", message)


@pytest.mark.asyncio
async def test_a_relevant_hit_from_another_session_enters_as_system_recall(
    _collaborators_patched,
):
    _collaborators_patched([hit(content="El TTS falla al primer frame binario")])

    history = await recover(FakeChat())

    assert len(history) == 1
    assert history[0]["role"] == "system"
    assert (
        "[recuerdo de sesión «VoiceLive en iOS», 2026-05-01]" in history[0]["content"]
    )
    assert (
        "el usuario dijo: El TTS falla al primer frame binario"
        in history[0]["content"]
    )


@pytest.mark.asyncio
async def test_every_recalled_hit_is_attributed_no_matter_its_score(
    _collaborators_patched,
):
    """Sin umbral: medido contra el índice real, el reranker puntúa casi igual
    lo pertinente y lo ajeno, así que un piso no separa. Separa la atribución."""
    _collaborators_patched(
        [
            hit(content="pertinente", reranker_score=2.9),
            hit(content="ajeno", reranker_score=1.2, timestamp="2026-05-02T10:00:00Z"),
        ]
    )

    history = await recover(FakeChat())

    assert [m["role"] for m in history] == ["system", "system"]
    assert all("[recuerdo de" in m["content"] for m in history)


@pytest.mark.asyncio
async def test_hits_from_the_current_session_are_left_to_short_memory(
    _collaborators_patched,
):
    """La sesión viva entra completa y en orden; duplicarla la desordena."""
    _collaborators_patched([hit(session_id="sesion-actual", content="turno de hoy")])
    chat = FakeChat([{"role": "user", "content": "turno de hoy"}])

    history = await recover(chat)

    assert history == [{"role": "user", "content": "turno de hoy"}]


@pytest.mark.asyncio
async def test_the_live_session_keeps_its_own_roles(_collaborators_patched):
    _collaborators_patched([])
    chat = FakeChat(
        [
            {"role": "user", "content": "audita el repo"},
            {"role": "assistant", "content": "voy a mirar la rama"},
        ]
    )

    history = await recover(chat)

    assert [m["role"] for m in history] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_the_index_is_asked_for_the_declared_number_of_results(
    _collaborators_patched,
):
    search = _collaborators_patched([])

    await recover(FakeChat())

    assert search.top_k == router.RECALL_TOP_K
