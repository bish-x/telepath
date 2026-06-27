from telepath.features.post_mirroring import PostMirrorSourceSettings
from telepath.storage import SQLiteAssistantRepository


def test_post_mirror_target_group_and_source_topic_are_persistent(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert repo.is_post_mirroring_enabled() is False
    assert repo.get_post_mirror_target_chat_id() is None
    repo.set_post_mirroring_enabled(True)
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100123, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100123, 42)
    repo.set_post_mirror_source_enabled(-100123, True)

    settings = repo.get_post_mirror_source_settings(-100123)
    assert settings == PostMirrorSourceSettings(
        enabled=True,
        target_thread_id=42,
        title="Source Channel",
        kind="channel",
    )
    assert repo.is_post_mirroring_enabled() is True
    assert repo.get_post_mirror_target_chat_id() == -100900
    assert repo.list_post_mirror_sources() == [
        {
            "source_chat_id": -100123,
            "title": "Source Channel",
            "kind": "channel",
            "enabled": True,
            "target_thread_id": 42,
        }
    ]


def test_post_mirror_source_requires_valid_kind_and_positive_topic(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    repo.upsert_post_mirror_source(-100123, "Source", "private")
    assert repo.get_post_mirror_source_settings(-100123).kind == "chat"

    try:
        repo.set_post_mirror_source_topic(-100123, 0)
    except ValueError as error:
        assert "topic" in str(error)
    else:
        raise AssertionError("expected topic validation error")


def test_post_mirror_folder_enables_channel_and_group_members_lazily_without_topics(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_reaction_folder(2, "Mirror folder", position=0)
    repo.replace_reaction_folder_members(
        2,
        [
            {"chat_id": -100123, "title": "News", "kind": "channel"},
            {"chat_id": -100456, "title": "Community", "kind": "group"},
            {"chat_id": 777, "title": "Alice", "kind": "private"},
        ],
    )

    assert repo.list_post_mirror_folders() == [
        {
            "folder_id": 2,
            "title": "Mirror folder",
            "enabled": False,
            "source_count": 2,
            "configured_count": 0,
            "history_processed_count": 0,
        }
    ]
    assert repo.list_post_mirror_folder_sources(2) == [
        {"chat_id": -100456, "title": "Community", "kind": "group"},
        {"chat_id": -100123, "title": "News", "kind": "channel"},
    ]

    repo.set_post_mirror_folder_enabled(2, True)

    assert repo.get_post_mirror_source_settings(-100123) == PostMirrorSourceSettings(
        enabled=True,
        target_thread_id=None,
        title="News",
        kind="channel",
    )
    assert repo.get_post_mirror_source_settings(-100456) == PostMirrorSourceSettings(
        enabled=True,
        target_thread_id=None,
        title="Community",
        kind="group",
    )
    assert repo.get_post_mirror_source_settings(777) is None
    assert repo.list_post_mirror_sources() == []
    repo.mark_processed(-100123, 1, "post_mirroring")
    assert repo.list_post_mirror_folders()[0]["history_processed_count"] == 1


def test_post_mirror_target_group_change_disables_folder_settings(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_reaction_folder(2, "Mirror folder", position=0)
    repo.set_post_mirror_folder_enabled(2, True)
    repo.upsert_post_mirror_source(-100123, "News", "channel")
    repo.set_post_mirror_source_topic(-100123, 42)

    repo.set_post_mirror_target_chat_id(-100901)

    assert repo.list_post_mirror_folders()[0]["enabled"] is False
    assert repo.get_post_mirror_source_settings(-100123).target_thread_id is None


def test_post_mirror_processing_claims_prevent_duplicate_workers(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert repo.try_claim_processing(-100123, (10, 11), "post_mirroring")
    assert not repo.try_claim_processing(-100123, (11, 12), "post_mirroring")
    assert not repo.is_processed(-100123, 10, "post_mirroring")

    repo.mark_many_processed(-100123, (10, 11), "post_mirroring")

    assert repo.is_processed(-100123, 10, "post_mirroring")
    assert repo.is_processed(-100123, 11, "post_mirroring")
    assert not repo.try_claim_processing(-100123, (10,), "post_mirroring")
    assert repo.try_claim_processing(-100123, (12,), "post_mirroring")


def test_post_mirror_processing_claims_are_released_on_failure(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")

    assert repo.try_claim_processing(-100123, (10,), "post_mirroring")
    repo.release_processing_claims(-100123, (10,), "post_mirroring")

    assert repo.try_claim_processing(-100123, (10,), "post_mirroring")


def test_post_mirror_target_group_change_disables_sources_and_clears_group_scoped_topics(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirror_target_chat_id(-100900)
    repo.upsert_post_mirror_source(-100123, "Source Channel", "channel")
    repo.set_post_mirror_source_topic(-100123, 77)
    repo.set_post_mirror_source_enabled(-100123, True)

    repo.set_post_mirror_target_chat_id(-100901)

    settings = repo.get_post_mirror_source_settings(-100123)
    assert settings.enabled is False
    assert settings.target_thread_id is None


def test_post_mirror_outbox_persists_and_deduplicates_delivery_jobs(tmp_path):
    db_path = tmp_path / "assistant.sqlite3"
    repo = SQLiteAssistantRepository(db_path)

    assert repo.enqueue_post_mirror_delivery(
        source_chat_id=-100123,
        message_ids=(10, 11),
        is_channel=True,
        is_group=False,
        grouped_id=777,
        target_chat_id=-100900,
        target_thread_id=42,
        origin="history",
        ready_at=1000,
    ) is True
    assert repo.enqueue_post_mirror_delivery(
        source_chat_id=-100123,
        message_ids=(10, 11),
        is_channel=True,
        is_group=False,
        grouped_id=777,
        target_chat_id=-100900,
        target_thread_id=42,
        origin="history",
        ready_at=1000,
    ) is False

    reopened = SQLiteAssistantRepository(db_path)
    jobs = reopened.list_ready_post_mirror_deliveries(now=1000, limit=10)

    assert len(jobs) == 1
    assert jobs[0].source_chat_id == -100123
    assert jobs[0].message_ids == (10, 11)
    assert jobs[0].is_channel is True
    assert jobs[0].is_group is False
    assert jobs[0].grouped_id == 777
    assert jobs[0].target_chat_id == -100900
    assert jobs[0].target_thread_id == 42
    assert jobs[0].origin == "history"


def test_post_mirror_outbox_marks_sent_and_defers_failed_jobs(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.enqueue_post_mirror_delivery(
        source_chat_id=-100123,
        message_ids=(10,),
        is_channel=True,
        is_group=False,
        grouped_id=None,
        target_chat_id=-100900,
        target_thread_id=42,
        origin="realtime",
        ready_at=1000,
    )
    job = repo.list_ready_post_mirror_deliveries(now=1000, limit=1)[0]

    repo.defer_post_mirror_delivery(job.id, delay_seconds=60, error="offline", now=1000)

    assert repo.list_ready_post_mirror_deliveries(now=1059, limit=1) == []
    deferred = repo.list_ready_post_mirror_deliveries(now=1060, limit=1)[0]
    assert deferred.attempts == 1
    assert deferred.last_error == "offline"

    repo.mark_post_mirror_delivery_sent(deferred.id, now=1061)

    assert repo.list_ready_post_mirror_deliveries(now=2000, limit=10) == []


def test_post_mirror_target_group_change_cancels_pending_outbox_jobs(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.set_post_mirror_target_chat_id(-100900)
    repo.enqueue_post_mirror_delivery(
        source_chat_id=-100123,
        message_ids=(10,),
        is_channel=True,
        is_group=False,
        grouped_id=None,
        target_chat_id=-100900,
        target_thread_id=42,
        origin="realtime",
        ready_at=1000,
    )

    repo.set_post_mirror_target_chat_id(-100901)

    assert repo.list_ready_post_mirror_deliveries(now=1000, limit=10) == []
