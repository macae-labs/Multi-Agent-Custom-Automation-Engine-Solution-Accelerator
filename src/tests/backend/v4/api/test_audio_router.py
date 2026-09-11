import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from azure.ai.voicelive.models import ServerEventType
from backend.v4.api import audio_router


class _WebSocket:
    def __init__(self, *messages):
        self.messages = iter(messages)
        self.sent_text: list[str] = []

    async def accept(self):
        pass

    async def receive(self):
        try:
            return next(self.messages)
        except StopIteration:
            await asyncio.Future()
            raise RuntimeError("Unreachable: receive() resumed after waiting forever")

    async def send_text(self, message):
        self.sent_text.append(message)

    async def send_bytes(self, _message):
        pass


class _VoiceLive:
    def __init__(self, events=()):
        self.events = events
        self.session = SimpleNamespace(update=AsyncMock())
        self.response = SimpleNamespace(create=AsyncMock(), cancel=AsyncMock())
        self.input_audio_buffer = SimpleNamespace(append=AsyncMock())
        self.conversation = SimpleNamespace(item=SimpleNamespace(delete=AsyncMock()))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def __aiter__(self):
        async def events():
            for event in self.events:
                yield event

        return events()


@pytest.mark.asyncio
async def test_audio_stream_speak_requests_audio_response():
    websocket = _WebSocket(
        {"text": json.dumps({"type": "speak", "text": "Hello"})},
    )
    voice_live = _VoiceLive()

    with (
        patch.object(audio_router.config, "get_shared_async_credential"),
        patch.object(audio_router, "vl_connect", return_value=voice_live),
    ):
        await audio_router.audio_stream(websocket)

    voice_live.response.create.assert_awaited_once()
    request = voice_live.response.create.await_args.kwargs["response"]
    assert request["modalities"] == ["audio"]
    assert "Hello" in request["instructions"]


@pytest.mark.asyncio
async def test_audio_stream_forwards_user_transcript():
    websocket = _WebSocket()
    voice_live = _VoiceLive(
        [
            SimpleNamespace(
                type=ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
                transcript="What time is it?",
            )
        ]
    )

    with (
        patch.object(audio_router.config, "get_shared_async_credential"),
        patch.object(audio_router, "vl_connect", return_value=voice_live),
    ):
        await audio_router.audio_stream(websocket)

    assert websocket.sent_text == [
        json.dumps({"type": "user_transcript", "text": "What time is it?"})
    ]


class _DisconnectingWebSocket(_WebSocket):
    """Réplica del contrato Starlette: receive() DEVUELVE el mensaje de
    disconnect (no lanza), y cualquier receive() posterior lanza RuntimeError."""

    def __init__(self, *messages):
        super().__init__(*messages, {"type": "websocket.disconnect", "code": 1001})
        self._disconnected = False

    async def receive(self):
        if self._disconnected:
            raise RuntimeError(
                'Cannot call "receive" once a disconnect message has been received.'
            )
        msg = await super().receive()
        if msg.get("type") == "websocket.disconnect":
            self._disconnected = True
        return msg


@pytest.mark.asyncio
async def test_audio_stream_stops_reading_after_disconnect(caplog):
    """PROBE: el cliente cierra el WS (iOS) → _browser_to_vl debe cortar en el
    mensaje de disconnect y NUNCA volver a llamar receive(). Sin el break, este
    test registra el RuntimeError visto en prod."""
    websocket = _DisconnectingWebSocket()
    voice_live = _VoiceLive()

    with (
        patch.object(audio_router.config, "get_shared_async_credential"),
        patch.object(audio_router, "vl_connect", return_value=voice_live),
    ):
        await audio_router.audio_stream(websocket)

    assert 'Cannot call "receive"' not in caplog.text
    assert "_browser_to_vl" not in caplog.text


def _run(websocket, voice_live, **kwargs):
    async def go():
        with (
            patch.object(audio_router.config, "get_shared_async_credential"),
            patch.object(audio_router, "vl_connect", return_value=voice_live),
        ):
            await audio_router.audio_stream(websocket, **kwargs)

    return go()


@pytest.mark.asyncio
async def test_lane_create_carries_turn_and_lane_metadata():
    """Cada response.create lleva metadata {turn_id, lane}: Voice Live la
    devuelve en response.created/done (verificado en vivo) y es la etiqueta
    autoritativa de la respuesta, no el estado compartido lane_ctx."""
    websocket = _WebSocket(
        {
            "text": json.dumps(
                {"type": "say", "turn_id": 7, "lane": "say", "text": "Hi"}
            )
        },
    )
    voice_live = _VoiceLive()
    await _run(websocket, voice_live)

    request = voice_live.response.create.await_args.kwargs["response"]
    assert request["metadata"] == {"turn_id": "7", "lane": "say"}


@pytest.mark.asyncio
async def test_response_events_are_labelled_from_metadata_and_items_forgotten():
    """response.created/done → transcript_start/end etiquetados con la metadata
    de ESA respuesta y su response_id; al terminar (completed o cancelled) sus
    items de salida se borran de la conversación para que no contaminen la
    siguiente respuesta (un say tras un speak cancelado repetía el speak)."""
    response = SimpleNamespace(
        id="resp_1",
        metadata={"turn_id": "12", "lane": "ack"},
        status="ResponseStatus.CANCELLED",
        output=[SimpleNamespace(id="item_a"), SimpleNamespace(id="item_b")],
    )
    websocket = _WebSocket()
    voice_live = _VoiceLive(
        [
            SimpleNamespace(type=ServerEventType.RESPONSE_CREATED, response=response),
            SimpleNamespace(type=ServerEventType.RESPONSE_DONE, response=response),
        ]
    )
    await _run(websocket, voice_live)

    start, end = (json.loads(m) for m in websocket.sent_text)
    assert start == {
        "type": "transcript_start",
        "turn_id": 12,
        "lane": "ack",
        "response_id": "resp_1",
    }
    assert end["type"] == "transcript_end"
    assert (end["turn_id"], end["lane"], end["response_id"]) == (12, "ack", "resp_1")
    assert "CANCELLED" in end["status"]
    deleted = [
        c.kwargs["item_id"] for c in voice_live.conversation.item.delete.await_args_list
    ]
    assert deleted == ["item_a", "item_b"]


@pytest.mark.asyncio
async def test_audio_chunk_carries_the_frame_response_id():
    """Vía b64 (la de prod): cada audio_chunk lleva el response_id REAL del
    delta. Tras cancel(A)+create(B) el server sigue vaciando deltas de A; el
    cliente los descarta por este id (ack/say/speak comparten turno)."""
    websocket = _WebSocket()
    voice_live = _VoiceLive(
        [
            SimpleNamespace(
                type=ServerEventType.RESPONSE_AUDIO_DELTA,
                delta=b"\x00\x01",
                response_id="resp_A",
            )
        ]
    )
    await _run(websocket, voice_live, audio="b64")

    chunk = json.loads(websocket.sent_text[0])
    assert chunk["type"] == "audio_chunk"
    assert chunk["response_id"] == "resp_A"
    assert chunk["data"]


class _SlowCreateVoiceLive(_VoiceLive):
    """Voice Live cuyo RESPONSE_CREATED tarda 300 ms en llegar tras el create
    (el server real tarda 150-200 ms) y que responde a cancel() con un
    RESPONSE_DONE cancelled, como el real."""

    def __init__(self):
        super().__init__()
        self._q: asyncio.Queue = asyncio.Queue()
        self._n = 0
        self.response = SimpleNamespace(
            create=AsyncMock(side_effect=self._create),
            cancel=AsyncMock(side_effect=self._cancel),
        )

    async def _create(self, response):
        self._n += 1
        rid = f"resp_{self._n}"
        meta = response.get("metadata", {})
        self._active = SimpleNamespace(
            id=rid, metadata=meta, status="ResponseStatus.COMPLETED", output=[]
        )

        async def later():
            await asyncio.sleep(0.3)
            await self._q.put(
                SimpleNamespace(
                    type=ServerEventType.RESPONSE_CREATED, response=self._active
                )
            )

        asyncio.get_running_loop().create_task(later())

    async def _cancel(self, **_kw):
        done = SimpleNamespace(
            id=self._active.id,
            metadata=self._active.metadata,
            status="ResponseStatus.CANCELLED",
            output=[],
        )
        await self._q.put(
            SimpleNamespace(type=ServerEventType.RESPONSE_DONE, response=done)
        )

    def __aiter__(self):
        async def events():
            while True:
                yield await self._q.get()

        return events()


@pytest.mark.asyncio
async def test_back_to_back_lanes_serialize_on_response_created():
    """Dos say seguidos: el segundo debe CANCELAR al primero y crearse después,
    aunque el RESPONSE_CREATED del primero tarde más que un 'respiro' fijo.
    Sin esperar el created, el segundo veía el carril libre, creaba sin
    cancelar y una respuesta se perdía en silencio (3 say → 2 en local)."""
    websocket = _DisconnectingWebSocket(
        {
            "text": json.dumps(
                {"type": "say", "turn_id": 3, "lane": "say", "text": "uno"}
            )
        },
        {
            "text": json.dumps(
                {"type": "say", "turn_id": 3, "lane": "say", "text": "dos"}
            )
        },
    )
    voice_live = _SlowCreateVoiceLive()

    await asyncio.wait_for(_run(websocket, voice_live), timeout=10)

    assert voice_live.response.create.await_count == 2
    voice_live.response.cancel.assert_awaited_once()
    starts = [
        json.loads(m)
        for m in websocket.sent_text
        if json.loads(m)["type"] == "transcript_start"
    ]
    assert [s["response_id"] for s in starts] == ["resp_1", "resp_2"]
    assert all((s["turn_id"], s["lane"]) == (3, "say") for s in starts)
