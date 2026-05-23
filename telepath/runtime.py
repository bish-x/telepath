from __future__ import annotations

from dataclasses import dataclass

from telepath.features.voice_transcription import (
    BlacklistPort,
    GroupWhitelistPort,
    PrivateChatGatePort,
    ProcessedMessagesPort,
    ReplyPort,
    SettingsPort,
    TextPolisherPort,
    TranscriberPort,
)


@dataclass
class AssistantContext:
    blacklist: BlacklistPort
    group_whitelist: GroupWhitelistPort
    transcriber: TranscriberPort
    polisher: TextPolisherPort
    replies: ReplyPort
    settings: SettingsPort
    processed: ProcessedMessagesPort
    private_chat_gate: PrivateChatGatePort
