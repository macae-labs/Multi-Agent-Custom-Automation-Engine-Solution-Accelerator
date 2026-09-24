"""Posición Plan: ``compose_plan`` en o4-mini/Responses con ``compose`` forzado y
``pattern`` restringido a ``magentic`` (el humano pidió un plan)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from v4.api import router

ROSTER = [
    {
        "name": "RepoAgent",
        "description": "reads the tree",
        "system_message": "You read repositories.",
        "use_mcp": True,
    }
]


class _Stream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def _fake_openai(reply):
    """Sustituto de AsyncOpenAI: responses.create devuelve `reply` (respuesta
    no stream, o un _Stream). Guarda los kwargs de construcción y de llamada."""

    class _Fake:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            self.create_kwargs = None
            _Fake.instances.append(self)

            async def create(**kw):
                self.create_kwargs = kw
                return reply

            self.responses = SimpleNamespace(create=create)

        async def close(self):
            self.closed = True

    return _Fake


def _compose_item(**args):
    return SimpleNamespace(
        type="function_call", name="compose", call_id="c1", arguments=json.dumps(args)
    )


def _client(memory_store=None):
    c = router._RouterChatClient.__new__(router._RouterChatClient)
    c.agent_name = "Composer"
    c._openai_base_url = "https://account.invalid/openai"
    c._api_version = "2025-03-01-preview"
    c._model = "o4-mini"
    c._memory_store = memory_store
    c._user_id = "u1"
    c._user_access_token = None
    c._workspace_id = None
    c._user_cred = None
    c._bearer = AsyncMock(return_value="tok")
    return c
