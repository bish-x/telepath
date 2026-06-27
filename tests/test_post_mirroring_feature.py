from dataclasses import dataclass

from telepath.features.post_mirroring import (
    POST_MIRROR_FEATURE,
    PostMirrorEvent,
    PostMirrorFeature,
    PostMirrorSendResult,
    PostMirrorSourceSettings,
)


@dataclass
class FakeState:
    enabled: bool = True
    target_chat_id: int | None = -100900
    source_settings: PostMirrorSourceSettings | None = None

    def __post_init__(self):
        self.processed = set()
        self.marked = []

    def is_post_mirroring_enabled(self):
        return self.enabled

    def get_post_mirror_target_chat_id(self):
        return self.target_chat_id

    def get_post_mirror_source_settings(self, chat_id):
        return self.source_settings

    def is_processed(self, chat_id, message_id, feature):
        return (chat_id, message_id, feature) in self.processed

    def mark_processed(self, chat_id, message_id, feature):
        self.marked.append((chat_id, message_id, feature))
        self.processed.add((chat_id, message_id, feature))
        return True

    def upsert_post_mirror_source(self, source_chat_id, title=None, kind="channel"):
        if self.source_settings is None:
            self.source_settings = PostMirrorSourceSettings(title=title, kind=kind)
            return
        self.source_settings = PostMirrorSourceSettings(
            enabled=self.source_settings.enabled,
            target_thread_id=self.source_settings.target_thread_id,
            title=title or self.source_settings.title,
            kind=kind,
        )

    def set_post_mirror_source_topic(self, source_chat_id, target_thread_id):
        assert self.source_settings is not None
        self.source_settings = PostMirrorSourceSettings(
            enabled=self.source_settings.enabled,
            target_thread_id=target_thread_id,
            title=self.source_settings.title,
            kind=self.source_settings.kind,
        )


class ClaimingFakeState(FakeState):
    def __post_init__(self):
        super().__post_init__()
        self.claimed = set()
        self.released = []

    def try_claim_processing(self, chat_id, message_ids, feature):
        keys = {(chat_id, message_id, feature) for message_id in message_ids}
        if self.processed & keys or self.claimed & keys:
            return False
        self.claimed.update(keys)
        return True

    def mark_many_processed(self, chat_id, message_ids, feature):
        for message_id in message_ids:
            self.mark_processed(chat_id, message_id, feature)
        self.release_processing_claims(chat_id, message_ids, feature)

    def release_processing_claims(self, chat_id, message_ids, feature):
        keys = {(chat_id, message_id, feature) for message_id in message_ids}
        self.claimed.difference_update(keys)
        self.released.append(tuple(message_ids))


class FakeSender:
    def __init__(self):
        self.calls = []

    async def copy_post(self, event, *, target_chat_id, target_thread_id):
        self.calls.append((event, target_chat_id, target_thread_id))
        return PostMirrorSendResult(message_count=len(event.message_ids), media_count=1)


class FakeTopicManager:
    def __init__(self):
        self.created = []

    async def create_topic(self, target_chat_id, title):
        self.created.append((target_chat_id, title))
        return 77


class SlowSender(FakeSender):
    def __init__(self):
        super().__init__()
        self.started = False

    async def copy_post(self, event, *, target_chat_id, target_thread_id):
        self.started = True
        import asyncio

        await asyncio.sleep(0)
        return await super().copy_post(event, target_chat_id=target_chat_id, target_thread_id=target_thread_id)


@dataclass
class FakeContext:
    post_mirror_settings: FakeState
    post_mirror_sender: FakeSender
    processed: FakeState
    post_mirror_topic_manager: FakeTopicManager | None = None


def _event(*, message_ids=(10,), is_channel=True, is_group=False):
    return PostMirrorEvent(
        chat_id=-100123,
        message_id=message_ids[0],
        message_ids=tuple(message_ids),
        is_channel=is_channel,
        is_group=is_group,
        grouped_id=777 if len(message_ids) > 1 else None,
        messages=tuple(object() for _ in message_ids),
    )


async def test_post_mirror_feature_copies_enabled_channel_to_configured_topic():
    state = FakeState(
        source_settings=PostMirrorSourceSettings(
            enabled=True,
            target_thread_id=42,
            title="Source",
            kind="channel",
        )
    )
    sender = FakeSender()
    feature = PostMirrorFeature()

    result = await feature.handle(_event(), FakeContext(state, sender, state))

    assert result == "mirrored"
    assert sender.calls[0][1:] == (-100900, 42)
    assert state.marked == [(-100123, 10, POST_MIRROR_FEATURE)]


async def test_post_mirror_feature_marks_every_album_message_after_copy():
    state = FakeState(
        source_settings=PostMirrorSourceSettings(
            enabled=True,
            target_thread_id=42,
            title="Source",
            kind="channel",
        )
    )
    sender = FakeSender()

    result = await PostMirrorFeature().handle(_event(message_ids=(10, 11, 12)), FakeContext(state, sender, state))

    assert result == "mirrored"
    assert [item[1] for item in state.marked] == [10, 11, 12]
    assert sender.calls[0][0].grouped_id == 777


async def test_post_mirror_feature_skips_when_target_group_or_topic_missing():
    no_group = FakeState(
        target_chat_id=None,
        source_settings=PostMirrorSourceSettings(enabled=True, target_thread_id=42),
    )
    no_topic = FakeState(
        target_chat_id=-100900,
        source_settings=PostMirrorSourceSettings(enabled=True, target_thread_id=None),
    )
    sender = FakeSender()

    assert await PostMirrorFeature().handle(_event(), FakeContext(no_group, sender, no_group)) == "skipped_no_target_group"
    assert await PostMirrorFeature().handle(_event(), FakeContext(no_topic, sender, no_topic)) == "skipped_no_topic"
    assert sender.calls == []


async def test_post_mirror_feature_creates_missing_topic_lazily_before_copy():
    state = FakeState(
        target_chat_id=-100900,
        source_settings=PostMirrorSourceSettings(
            enabled=True,
            target_thread_id=None,
            title="Source Channel",
            kind="channel",
        ),
    )
    sender = FakeSender()
    topic_manager = FakeTopicManager()

    result = await PostMirrorFeature().handle(
        _event(),
        FakeContext(state, sender, state, topic_manager),
    )

    assert result == "mirrored"
    assert topic_manager.created == [(-100900, "Source Channel")]
    assert state.source_settings.target_thread_id == 77
    assert sender.calls[0][1:] == (-100900, 77)


async def test_post_mirror_feature_can_defer_missing_topic_to_outbox_sender():
    state = FakeState(
        target_chat_id=-100900,
        source_settings=PostMirrorSourceSettings(
            enabled=True,
            target_thread_id=None,
            title="Source Channel",
            kind="channel",
        ),
    )
    sender = FakeSender()

    class FailingTopicManager:
        async def create_topic(self, target_chat_id, title):
            raise AssertionError("topic creation must be deferred to delivery worker")

    context = FakeContext(state, sender, state, FailingTopicManager())
    context.post_mirror_defer_missing_topics = True

    result = await PostMirrorFeature().handle(_event(), context)

    assert result == "mirrored"
    assert sender.calls[0][1:] == (-100900, None)
    assert state.source_settings.target_thread_id is None
    assert state.marked == [(-100123, 10, POST_MIRROR_FEATURE)]


async def test_post_mirror_feature_skips_when_source_is_target_group_to_prevent_loops():
    state = FakeState(
        target_chat_id=-100123,
        source_settings=PostMirrorSourceSettings(enabled=True, target_thread_id=42),
    )
    sender = FakeSender()

    result = await PostMirrorFeature().handle(_event(is_channel=False, is_group=True), FakeContext(state, sender, state))

    assert result == "skipped_target_group"
    assert sender.calls == []


async def test_post_mirror_feature_skips_already_processed_album():
    state = FakeState(
        source_settings=PostMirrorSourceSettings(enabled=True, target_thread_id=42),
    )
    for message_id in (10, 11):
        state.processed.add((-100123, message_id, POST_MIRROR_FEATURE))
    sender = FakeSender()

    result = await PostMirrorFeature().handle(_event(message_ids=(10, 11)), FakeContext(state, sender, state))

    assert result == "skipped_processed"
    assert sender.calls == []


async def test_post_mirror_feature_skips_partially_processed_album_to_avoid_duplicates():
    state = FakeState(
        source_settings=PostMirrorSourceSettings(enabled=True, target_thread_id=42),
    )
    state.processed.add((-100123, 10, POST_MIRROR_FEATURE))
    sender = FakeSender()

    result = await PostMirrorFeature().handle(_event(message_ids=(10, 11)), FakeContext(state, sender, state))

    assert result == "skipped_processed"
    assert sender.calls == []
    assert state.marked == []


async def test_post_mirror_feature_claims_messages_before_copy_to_prevent_realtime_history_race():
    state = ClaimingFakeState(
        source_settings=PostMirrorSourceSettings(enabled=True, target_thread_id=42),
    )
    sender = SlowSender()
    context = FakeContext(state, sender, state)

    import asyncio

    first, second = await asyncio.gather(
        PostMirrorFeature().handle(_event(message_ids=(10, 11)), context),
        PostMirrorFeature().handle(_event(message_ids=(10, 11)), context),
    )

    assert sorted([first, second]) == ["mirrored", "skipped_processing"]
    assert len(sender.calls) == 1
    assert {item[1] for item in state.marked} == {10, 11}
    assert state.claimed == set()
