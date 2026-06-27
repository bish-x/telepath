from __future__ import annotations

from dataclasses import dataclass

from telepath.features.channel_reactions import ReactionSenderPort, ReactionSettingsPort
from telepath.features.post_mirroring import (
    PostMirrorSenderPort,
    PostMirrorSettingsPort,
    PostMirrorTopicManagerPort,
)
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
    reaction_settings: ReactionSettingsPort
    reaction_sender: ReactionSenderPort
    post_mirror_settings: PostMirrorSettingsPort
    post_mirror_sender: PostMirrorSenderPort
    post_mirror_topic_manager: PostMirrorTopicManagerPort | None = None
    post_mirror_defer_missing_topics: bool = False
