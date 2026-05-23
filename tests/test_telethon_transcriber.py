import asyncio
from dataclasses import dataclass

import pytest
from telethon.errors import rpcbaseerrors
from telethon import types

from telepath.features.voice_transcription import VoiceTranscriptionUnavailableError, VoiceTooLongError
from telepath.user_client import TelethonTranscriber


@dataclass
class TranscribeResult:
    text: str
    pending: bool = False
    transcription_id: int = 0


class FakeClient:
    def __init__(self, result, update=None, error=None):
        self.result = result
        self.update = update
        self.error = error
        self.handlers = []
        self.removed = []

    async def get_input_entity(self, chat_id):
        return f"peer:{chat_id}"

    def add_event_handler(self, handler, event):
        self.handlers.append((handler, event))

    def remove_event_handler(self, handler, event):
        self.removed.append((handler, event))

    async def __call__(self, request):
        if self.error:
            raise self.error
        if self.update is not None:
            async def emit_update():
                await asyncio.sleep(0)
                for handler, _ in list(self.handlers):
                    await handler(self.update)

            asyncio.create_task(emit_update())
        return self.result


async def test_transcriber_returns_immediate_text_without_waiting_for_update():
    client = FakeClient(TranscribeResult(text="готово"))
    transcriber = TelethonTranscriber(client, update_timeout_seconds=0.01)

    assert await transcriber.transcribe(chat_id=100, message_id=50) == "готово"
    assert client.removed


async def test_transcriber_waits_for_pending_update_by_transcription_id():
    update = types.UpdateTranscribedAudio(
        peer=types.PeerUser(user_id=7),
        msg_id=50,
        transcription_id=777,
        text="финальный текст",
        pending=False,
    )
    client = FakeClient(TranscribeResult(text="", pending=True, transcription_id=777), update=update)
    transcriber = TelethonTranscriber(client, update_timeout_seconds=0.2)

    assert await transcriber.transcribe(chat_id=100, message_id=50) == "финальный текст"
    assert client.removed


async def test_transcriber_returns_initial_text_when_pending_update_times_out():
    client = FakeClient(TranscribeResult(text="частичный", pending=True, transcription_id=777))
    transcriber = TelethonTranscriber(client, update_timeout_seconds=0.01)

    assert await transcriber.transcribe(chat_id=100, message_id=50) == "частичный"
    assert client.removed


async def test_transcriber_maps_server_too_long_error():
    client = FakeClient(None, error=rpcbaseerrors.BadRequestError(request=None, message="MSG_VOICE_TOO_LONG"))
    transcriber = TelethonTranscriber(client, update_timeout_seconds=0.01)

    with pytest.raises(VoiceTooLongError):
        await transcriber.transcribe(chat_id=100, message_id=50)

    assert client.removed


async def test_transcriber_maps_server_transcription_failed_error():
    client = FakeClient(None, error=rpcbaseerrors.BadRequestError(request=None, message="TRANSCRIPTION_FAILED"))
    transcriber = TelethonTranscriber(client, update_timeout_seconds=0.01)

    with pytest.raises(VoiceTranscriptionUnavailableError):
        await transcriber.transcribe(chat_id=100, message_id=50)

    assert client.removed


async def test_transcriber_reraises_unrelated_bad_request_errors():
    client = FakeClient(None, error=rpcbaseerrors.BadRequestError(request=None, message="SOMETHING_ELSE"))
    transcriber = TelethonTranscriber(client, update_timeout_seconds=0.01)

    with pytest.raises(rpcbaseerrors.BadRequestError):
        await transcriber.transcribe(chat_id=100, message_id=50)


class FakeClientWithEarlyUpdate:
    """Emits an update BEFORE the transcription_id is known, then returns the pending result.

    Exercises the buffered_updates branch + the apply_update no-op early return.
    """

    def __init__(self, *, target_msg_id, target_transcription_id):
        self.result = TranscribeResult(text="", pending=True, transcription_id=target_transcription_id)
        self.handlers = []
        self.removed = []
        self.target_msg_id = target_msg_id
        self.target_transcription_id = target_transcription_id

    async def get_input_entity(self, chat_id):
        return f"peer:{chat_id}"

    def add_event_handler(self, handler, event):
        self.handlers.append((handler, event))

    def remove_event_handler(self, handler, event):
        self.removed.append((handler, event))

    async def __call__(self, request):
        # Emit an unrelated update + the target update BEFORE returning.
        unrelated = types.UpdateTranscribedAudio(
            peer=types.PeerUser(user_id=7),
            msg_id=self.target_msg_id + 1,  # mismatched msg_id → apply_update early return
            transcription_id=self.target_transcription_id,
            text="ignore-me",
            pending=False,
        )
        target = types.UpdateTranscribedAudio(
            peer=types.PeerUser(user_id=7),
            msg_id=self.target_msg_id,
            transcription_id=self.target_transcription_id,
            text="готово",
            pending=False,
        )
        for handler, _ in list(self.handlers):
            await handler(unrelated)
            await handler(target)
        return self.result


async def test_transcriber_buffers_updates_received_before_transcription_id_is_known():
    client = FakeClientWithEarlyUpdate(target_msg_id=50, target_transcription_id=777)
    transcriber = TelethonTranscriber(client, update_timeout_seconds=0.5)

    result = await transcriber.transcribe(chat_id=100, message_id=50)

    assert result == "готово"
    assert client.removed
