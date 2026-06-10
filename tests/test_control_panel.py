from telepath.panel import ControlPanelService
from telepath.storage import SQLiteAssistantRepository


def _status(panel, user_id=10):
    return panel.handle_action(user_id=user_id, action="status")


def button_texts(view):
    return [button.text for row in view.keyboard for button in row]


def button_actions(view):
    return [button.action for row in view.keyboard for button in row]


def button_by_text(view, text):
    return next(button for row in view.keyboard for button in row if button.text == text)


def button_by_action(view, action):
    return next(button for row in view.keyboard for button in row if button.action == action)


def assert_no_home_button(view):
    assert "Домой" not in button_texts(view)


def assert_bottom_back_only(view):
    assert view.keyboard[-1] == [type(view.keyboard[-1][0])("Назад", "back")]


def test_control_panel_is_owner_only(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.main(user_id=11)

    assert view.text == "Доступ запрещен."
    assert view.keyboard == []


def test_control_panel_main_and_transcription_toggle(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    main = panel.main(user_id=10)
    assert "Главное меню" in main.text
    assert "Транскрибация" in button_texts(main)
    assert "Автолайк ТГК" in button_texts(main)
    assert "Экспорт чатов" not in button_texts(main)
    assert_no_home_button(main)

    transcription = panel.handle_action(user_id=10, action="transcription")
    assert "Транскрибация: включена" in transcription.text
    assert "Личные чаты" in transcription.text
    assert "Исключения" in button_texts(transcription)
    assert "Группы" in button_texts(transcription)
    assert "Чаты" in button_texts(transcription)
    assert "Настройки" in button_texts(transcription)
    assert "Выключить" in button_texts(transcription)
    assert "Смайлы: выкл" in transcription.text
    assert "Включить смайлы" in button_texts(transcription)
    assert_no_home_button(transcription)
    assert_bottom_back_only(transcription)

    disabled = panel.handle_action(user_id=10, action="transcription.toggle")
    assert "Транскрибация: выключена" in disabled.text
    assert not repo.is_feature_enabled("voice_transcription")
    assert "Включить" in button_texts(disabled)
    assert_bottom_back_only(disabled)


def test_control_panel_main_shows_chat_export_when_enabled(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo, chat_export_enabled=True)

    main = panel.main(user_id=10)

    assert "Экспорт чатов" in button_texts(main)


def test_control_panel_transcription_decoration_toggle(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    enabled = panel.handle_action(user_id=10, action="transcription.decoration.toggle")

    assert repo.is_transcription_decoration_enabled()
    assert "Смайлы: вкл" in enabled.text
    assert "Выключить смайлы" in button_texts(enabled)
    assert_bottom_back_only(enabled)

    disabled = panel.handle_action(user_id=10, action="transcription.decoration.toggle")

    assert not repo.is_transcription_decoration_enabled()
    assert "Смайлы: выкл" in disabled.text
    assert "Включить смайлы" in button_texts(disabled)
    assert_bottom_back_only(disabled)


def test_control_panel_transcription_chat_list_toggles_private_chat(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(100, "Alice", "private", last_seen_at=20)
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    panel = ControlPanelService(owner_id=10, state=repo)

    chats = panel.handle_action(user_id=10, action="transcription.chats")

    assert "Чаты" in chats.text
    assert "❌ [ЛС] Alice" in button_texts(chats)
    assert "News" not in chats.text
    assert_bottom_back_only(chats)

    enabled = panel.handle_action(user_id=10, action="transcription.chats.toggle:100:0")

    assert repo.is_private_chat_transcription_enabled(100)
    assert "Включено: Alice" in enabled.text
    assert "✅ [ЛС] Alice" in button_texts(enabled)


def test_control_panel_transcription_chat_toggle_can_disable_auto_allowed_private_chat(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(100, "Alice", "private", last_seen_at=20)
    repo.save_private_chat_message_gate(100, 250, True)
    panel = ControlPanelService(owner_id=10, state=repo)

    chats = panel.handle_action(user_id=10, action="transcription.chats")
    assert "✅ [ЛС] Alice" in button_texts(chats)

    disabled = panel.handle_action(user_id=10, action="transcription.chats.toggle:100:0")

    assert repo.get_private_chat_transcription_override(100) is False
    assert "Выключено: Alice" in disabled.text
    assert "❌ [ЛС] Alice" in button_texts(disabled)


def test_control_panel_transcription_chats_enabled_filter(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(100, "Alice", "private", last_seen_at=30)
    repo.upsert_known_chat(200, "Bob", "private", last_seen_at=20)
    repo.upsert_known_chat(-100123, "Team", "group", last_seen_at=10)
    repo.set_private_chat_transcription(200, "Bob", True)
    repo.allow_group(-100123, "Team")
    panel = ControlPanelService(owner_id=10, state=repo)

    enabled_only = panel.handle_action(user_id=10, action="transcription.chats.enabled")

    assert "Фильтр: только включенные" in enabled_only.text
    texts = button_texts(enabled_only)
    assert "✅ [ЛС] Bob" in texts
    assert "✅ [ГР] Team" in texts
    assert "❌ [ЛС] Alice" not in texts
    assert button_by_text(enabled_only, "Все чаты").action == "transcription.chats"
    assert_bottom_back_only(enabled_only)


def test_control_panel_transcription_chats_search_flow_preserves_context(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(100, "Alice", "private", last_seen_at=30)
    repo.upsert_known_chat(200, "Bob", "private", last_seen_at=20)
    repo.upsert_known_chat(-100123, "Team", "group", last_seen_at=10)
    panel = ControlPanelService(owner_id=10, state=repo)

    prompt = panel.handle_action(user_id=10, action="transcription.chats.search")

    assert prompt.input_state == "transcription_chat_search"
    assert "часть названия" in prompt.text
    assert button_by_text(prompt, "К списку чатов").action == "transcription.chats"

    empty = panel.handle_text(user_id=10, state="transcription_chat_search", text="   ")

    assert empty.input_state == "transcription_chat_search"
    assert "Поиск не должен быть пустым" in empty.text

    results = panel.handle_text(user_id=10, state="transcription_chat_search", text="bo")

    assert "Поиск: bo" in results.text
    assert "Найдено: 1" in results.text
    texts = button_texts(results)
    assert "❌ [ЛС] Bob" in texts
    assert "❌ [ЛС] Alice" not in texts
    assert button_by_text(results, "Найти заново").action == "transcription.chats.search"
    assert button_by_text(results, "Сбросить поиск").action == "transcription.chats.search.clear"

    toggled = panel.handle_action(user_id=10, action=button_by_text(results, "❌ [ЛС] Bob").action)

    assert "Поиск: bo" in toggled.text
    assert "Включено: Bob" in toggled.text
    assert "✅ [ЛС] Bob" in button_texts(toggled)


def test_control_panel_transcription_numeric_settings(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    settings = panel.handle_action(user_id=10, action="transcription.settings")
    assert "Лимит личного чата: 100 сообщений" in settings.text
    assert "ГС от: 0 сек" in settings.text
    assert "Лимит сообщений" in button_texts(settings)
    assert "Минимум ГС" in button_texts(settings)

    limit_prompt = panel.handle_action(user_id=10, action="transcription.settings.private_limit")
    assert limit_prompt.input_state == "private_min_messages"
    limit_saved = panel.handle_text(user_id=10, state="private_min_messages", text="250")
    assert repo.get_private_chat_min_messages() == 250
    assert "Лимит личного чата: 250 сообщений" in limit_saved.text

    duration_prompt = panel.handle_action(user_id=10, action="transcription.settings.voice_min_duration")
    assert duration_prompt.input_state == "voice_min_duration"
    duration_saved = panel.handle_text(user_id=10, state="voice_min_duration", text="12")
    assert repo.get_voice_min_duration_seconds() == 12
    assert "ГС от: 12 сек" in duration_saved.text


def test_control_panel_transcription_why_screen_is_repairable(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    transcription = panel.handle_action(user_id=10, action="transcription")
    assert button_by_text(transcription, "Почему не работает").action == "transcription.why"

    why = panel.handle_action(user_id=10, action="transcription.why")

    assert "Почему чат не обрабатывается" in why.text
    assert "Личные чаты" in why.text
    assert "Группы" in why.text
    assert "Каналы не транскрибируются" in why.text
    assert "Исключения" in why.text
    assert "ГС" in why.text
    assert button_by_text(why, "Чаты").action == "transcription.chats"
    assert button_by_text(why, "Группы").action == "transcription.groups"
    assert button_by_text(why, "Исключения").action == "transcription.blacklist"
    assert_bottom_back_only(why)


def test_control_panel_blacklist_add_and_remove_flow(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    add_prompt = panel.handle_action(user_id=10, action="transcription.blacklist.add")
    assert add_prompt.input_state == "block_chat"
    assert "chat_id" in add_prompt.text
    assert_bottom_back_only(add_prompt)

    added = panel.handle_text(user_id=10, state="block_chat", text="100 Alice")
    assert repo.is_blocked(100)
    assert "Чат 100 добавлен в исключения" in added.text
    assert_bottom_back_only(added)

    blacklist = panel.handle_action(user_id=10, action="transcription.blacklist")
    assert "100: Alice" in blacklist.text
    assert "Исключения" in blacklist.text
    assert_no_home_button(blacklist)
    assert_bottom_back_only(blacklist)

    remove_prompt = panel.handle_action(user_id=10, action="transcription.blacklist.remove")
    assert remove_prompt.input_state == "unblock_chat"
    assert_bottom_back_only(remove_prompt)

    removed = panel.handle_text(user_id=10, state="unblock_chat", text="100")
    assert not repo.is_blocked(100)
    assert "Чат 100 убран из исключений" in removed.text
    assert_bottom_back_only(removed)


def test_control_panel_group_whitelist_add_and_remove_flow(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_group(-100123, "Team")
    repo.upsert_known_group(-100456, "Archive")
    panel = ControlPanelService(owner_id=10, state=repo)

    groups = panel.handle_action(user_id=10, action="transcription.groups")
    assert "Группы" in groups.text
    assert "Выбрано: 0" in groups.text
    assert "○ Team" in button_texts(groups)
    assert "○ Archive" in button_texts(groups)
    assert "Удалить группу" not in button_texts(groups)
    assert_no_home_button(groups)
    assert_bottom_back_only(groups)

    added = panel.handle_action(user_id=10, action="transcription.groups.toggle:-100123:0")
    assert repo.is_group_allowed(-100123)
    assert "Включено: Team" in added.text
    assert "✅ Team" in button_texts(added)
    assert_bottom_back_only(added)

    removed = panel.handle_action(user_id=10, action="transcription.groups.toggle:-100123:0")
    assert not repo.is_group_allowed(-100123)
    assert "Выключено: Team" in removed.text
    assert "○ Team" in button_texts(removed)
    assert_bottom_back_only(removed)


def test_control_panel_group_picker_merges_manual_allowed_groups(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.allow_group(-100999, "Manual")
    panel = ControlPanelService(owner_id=10, state=repo)

    groups = panel.handle_action(user_id=10, action="transcription.groups")

    assert "✅ Manual" in button_texts(groups)
    assert "Выбрано: 1" in groups.text
    assert_bottom_back_only(groups)


def test_control_panel_group_picker_keeps_recent_group_order(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_group(-1001, "Recent", last_seen_at=30)
    repo.upsert_known_group(-1002, "Older", last_seen_at=20)
    repo.upsert_known_group(-1003, "Oldest", last_seen_at=10)
    panel = ControlPanelService(owner_id=10, state=repo)

    groups = panel.handle_action(user_id=10, action="transcription.groups")
    toggles = [text for text in button_texts(groups) if text.startswith("○ ")]

    assert toggles[:3] == ["○ Recent", "○ Older", "○ Oldest"]


def test_control_panel_group_picker_truncates_long_group_titles(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_group(-1001, "Очень длинное название группы которое не должно раздувать кнопку")
    panel = ControlPanelService(owner_id=10, state=repo)

    groups = panel.handle_action(user_id=10, action="transcription.groups")
    toggle = next(text for text in button_texts(groups) if text.startswith("○ "))

    assert toggle == "○ Очень длинное название группы которое..."


def test_control_panel_group_picker_keeps_manual_add_as_fallback(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    add_prompt = panel.handle_action(user_id=10, action="transcription.groups.add")
    assert add_prompt.input_state == "allow_group"
    assert "chat_id группы" in add_prompt.text

    added = panel.handle_text(user_id=10, state="allow_group", text="-100123 Team")

    assert repo.is_group_allowed(-100123)
    assert repo.list_known_groups() == [{"chat_id": -100123, "title": "Team"}]
    assert "Включено: Team" in added.text
    assert "✅ Team" in button_texts(added)
    assert_bottom_back_only(added)


def test_control_panel_groups_enabled_filter(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_group(-100123, "Team")
    repo.upsert_known_group(-100456, "Archive")
    repo.allow_group(-100123, "Team")
    panel = ControlPanelService(owner_id=10, state=repo)

    enabled_only = panel.handle_action(user_id=10, action="transcription.groups.enabled")

    assert "Фильтр: только выбранные" in enabled_only.text
    texts = button_texts(enabled_only)
    assert "✅ Team" in texts
    assert "○ Archive" not in texts
    assert button_by_text(enabled_only, "Все группы").action == "transcription.groups"
    assert_bottom_back_only(enabled_only)

    toggled = panel.handle_action(user_id=10, action=button_by_text(enabled_only, "✅ Team").action)

    assert "Фильтр: только выбранные" in toggled.text
    assert "Пока нет выбранных групп" in toggled.text


def test_control_panel_groups_search_flow_preserves_context(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_group(-100123, "Team")
    repo.upsert_known_group(-100456, "Archive")
    panel = ControlPanelService(owner_id=10, state=repo)

    prompt = panel.handle_action(user_id=10, action="transcription.groups.search")

    assert prompt.input_state == "transcription_group_search"
    assert "часть названия" in prompt.text
    assert button_by_text(prompt, "К списку групп").action == "transcription.groups"

    results = panel.handle_text(user_id=10, state="transcription_group_search", text="tea")

    assert "Поиск: tea" in results.text
    assert "Найдено: 1" in results.text
    texts = button_texts(results)
    assert "○ Team" in texts
    assert "○ Archive" not in texts
    assert button_by_text(results, "Найти заново").action == "transcription.groups.search"
    assert button_by_text(results, "Сбросить поиск").action == "transcription.groups.search.clear"

    toggled = panel.handle_action(user_id=10, action=button_by_text(results, "○ Team").action)

    assert "Поиск: tea" in toggled.text
    assert "Включено: Team" in toggled.text
    assert "✅ Team" in button_texts(toggled)


def test_control_panel_autolike_channel_list_and_defaults(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    panel = ControlPanelService(owner_id=10, state=repo)

    channels = panel.handle_action(user_id=10, action="reactions")
    assert "Автолайк ТГК" in channels.text
    assert "Автолайк: включен" in channels.text
    assert "Глобальный фильтр: хорошие" in channels.text
    assert "Пауза: 240-900 сек" in channels.text
    assert "❌ News · хорошие · смесь" in button_texts(channels)
    assert "Выключить автолайк" in button_texts(channels)
    assert "Пауза" in button_texts(channels)

    detail = panel.handle_action(user_id=10, action="reactions.channel:-100123:0")
    assert "Канал: News" in detail.text
    assert "Реакций на пост: 3" in detail.text
    assert "Выбор: случайно" in detail.text
    assert "Приоритет: смесь" in detail.text
    assert "3 реакции" in button_texts(detail)
    assert "Доступные реакции: еще не проверялись" in detail.text
    assert button_by_text(detail, "Хорошие").style == "primary"
    assert button_by_text(detail, "Плохие").style is None
    assert button_by_text(detail, "Все").style is None
    assert button_by_text(detail, "Обновить доступные").action == "rcr:-100123:0"

    blocked = panel.handle_action(user_id=10, action="reactions.channel.max3:-100123:0")
    assert "Реакций на пост: 3" in blocked.text
    assert repo.get_reaction_channel_settings(-100123).max_reactions == 3

    repo.set_account_premium(True)
    enabled = panel.handle_action(user_id=10, action="reactions.channel.max3:-100123:0")
    assert "Реакций на пост: 3" in enabled.text
    assert repo.get_reaction_channel_settings(-100123).max_reactions == 3

    global_negative = panel.handle_action(user_id=10, action="reactions.global.mode:negative")
    assert repo.get_reaction_global_mode() == "negative"
    assert "Глобальный фильтр: плохие" in global_negative.text
    assert button_by_action(global_negative, "reactions.global.mode:negative").style == "primary"
    assert button_by_action(global_negative, "reactions.global.mode:positive").style is None

    premium_source = panel.handle_action(user_id=10, action="reactions.channel.source:-100123:premium:0")
    assert repo.get_reaction_channel_settings(-100123).reaction_source == "premium"
    assert "Приоритет: премиум" in premium_source.text
    assert button_by_text(premium_source, "Премиум").style == "primary"

    negative = panel.handle_action(user_id=10, action="reactions.channel.mode:-100123:negative:0")
    assert button_by_text(negative, "Хорошие").style is None
    assert button_by_text(negative, "Плохие").style == "primary"
    assert button_by_text(negative, "Все").style is None


def test_control_panel_autolike_global_toggle(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    disabled = panel.handle_action(user_id=10, action="reactions.toggle")

    assert not repo.is_reaction_autolike_enabled()
    assert "Автолайк: выключен" in disabled.text
    assert "Включить автолайк" in button_texts(disabled)


def test_control_panel_autolike_folder_settings_flow(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_reaction_folder(2, "AI feeds", position=0)
    repo.replace_reaction_folder_members(
        2,
        [{"chat_id": -100123, "title": "News", "kind": "channel"}],
    )
    panel = ControlPanelService(owner_id=10, state=repo)

    root = panel.handle_action(user_id=10, action="reactions")

    assert "Папки: 0/1" in root.text
    assert button_by_text(root, "Папки").action == "reactions.folders"

    folders = panel.handle_action(user_id=10, action="reactions.folders")

    assert "Папки автолайка" in folders.text
    assert "Включено: 0/1" in folders.text
    assert "○ AI feeds · 1 канал" in button_texts(folders)
    assert button_by_text(folders, "Обновить папки").action == "rfr"
    assert_bottom_back_only(folders)

    detail = panel.handle_action(user_id=10, action="reactions.folder:2")

    assert "Папка: AI feeds" in detail.text
    assert "Каналов: 1" in detail.text
    assert "Автолайк: выключен" in detail.text
    assert button_by_text(detail, "Включить папку").action == "reactions.folder.toggle:2"

    enabled = panel.handle_action(user_id=10, action="reactions.folder.toggle:2")

    assert repo.get_reaction_folder_settings(2).enabled
    assert "Автолайк: включен" in enabled.text
    assert button_by_text(enabled, "Хорошие").style == "primary"

    negative = panel.handle_action(user_id=10, action="reactions.folder.mode:2:negative")

    assert repo.get_reaction_folder_settings(2).mode == "negative"
    assert button_by_text(negative, "Плохие").style == "primary"
    assert button_by_text(negative, "Хорошие").style is None

    premium = panel.handle_action(user_id=10, action="reactions.folder.source:2:premium")

    assert repo.get_reaction_folder_settings(2).reaction_source == "premium"
    assert button_by_text(premium, "Премиум").style == "primary"


def test_control_panel_autolike_channel_list_shows_folder_inheritance_without_creating_override(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    repo.upsert_reaction_folder(2, "AI feeds", position=0)
    repo.replace_reaction_folder_members(
        2,
        [{"chat_id": -100123, "title": "News", "kind": "channel"}],
    )
    repo.set_reaction_folder_enabled(2, True)
    panel = ControlPanelService(owner_id=10, state=repo)

    channels = panel.handle_action(user_id=10, action="reactions")

    assert "Включено каналов: 1" in channels.text
    assert "📁 News · хорошие · смесь" in button_texts(channels)

    detail = panel.handle_action(user_id=10, action="reactions.channel:-100123:0")

    assert repo.get_reaction_channel_settings(-100123) is None
    assert "Источник настройки: папка" in detail.text
    assert "Папка: AI feeds" in detail.text
    assert button_by_text(detail, "Настроить вручную").action == "reactions.channel.customize:-100123:0"
    assert button_by_text(detail, "Выключить для канала").action == "reactions.channel.toggle:-100123:0"

    customized = panel.handle_action(user_id=10, action="reactions.channel.customize:-100123:0")

    assert repo.get_reaction_channel_settings(-100123).enabled
    assert "Источник настройки: канал" in customized.text


def test_control_panel_autolike_channel_enabled_filter(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    repo.upsert_known_chat(-100456, "Archive", "channel", last_seen_at=20)
    repo.set_reaction_channel_enabled(-100123, True)
    panel = ControlPanelService(owner_id=10, state=repo)

    enabled_only = panel.handle_action(user_id=10, action="reactions.enabled")

    assert "Фильтр каналов: включенные" in enabled_only.text
    texts = button_texts(enabled_only)
    assert "✅ News · хорошие · смесь" in texts
    assert "❌ Archive" not in texts
    assert button_by_text(enabled_only, "Все каналы").action == "reactions.filter.enabled:any"
    assert_bottom_back_only(enabled_only)


def test_control_panel_autolike_channel_search_combines_enabled_mode_and_source_filters(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100111, "Bad enabled premium", "channel", last_seen_at=40)
    repo.upsert_known_chat(-100222, "Bad disabled premium", "channel", last_seen_at=30)
    repo.upsert_known_chat(-100333, "Good enabled premium", "channel", last_seen_at=20)
    repo.upsert_known_chat(-100444, "Bad enabled standard", "channel", last_seen_at=10)
    repo.set_account_premium(True)
    repo.set_reaction_channel_enabled(-100111, True)
    repo.set_reaction_channel_mode(-100111, "negative")
    repo.set_reaction_channel_source(-100111, "premium")
    repo.set_reaction_channel_enabled(-100222, False)
    repo.set_reaction_channel_mode(-100222, "negative")
    repo.set_reaction_channel_source(-100222, "premium")
    repo.set_reaction_channel_enabled(-100333, True)
    repo.set_reaction_channel_mode(-100333, "positive")
    repo.set_reaction_channel_source(-100333, "premium")
    repo.set_reaction_channel_enabled(-100444, True)
    repo.set_reaction_channel_mode(-100444, "negative")
    repo.set_reaction_channel_source(-100444, "standard")
    panel = ControlPanelService(owner_id=10, state=repo)

    enabled = panel.handle_action(user_id=10, action="reactions.filter.enabled:on")
    negative = panel.handle_action(user_id=10, action="reactions.filter.mode:negative")
    premium = panel.handle_action(user_id=10, action="reactions.filter.source:premium")
    prompt = panel.handle_action(user_id=10, action="reactions.search")
    results = panel.handle_text(user_id=10, state=prompt.input_state, text="bad")

    assert "Фильтр каналов: включенные" in results.text
    assert "Тип реакций: плохие" in results.text
    assert "Приоритет: премиум" in results.text
    assert "Поиск: bad" in results.text
    assert "Найдено: 1" in results.text
    texts = button_texts(results)
    assert "✅ Bad enabled premium · плохие · премиум" in texts
    assert "Bad disabled premium" not in "\n".join(texts)
    assert "Good enabled premium" not in "\n".join(texts)
    assert "Bad enabled standard" not in "\n".join(texts)
    assert button_by_text(enabled, "Включенные").style == "primary"
    assert button_by_text(negative, "Плохие").style == "primary"
    assert button_by_text(premium, "Премиум").style == "primary"

    cleared_search = panel.handle_action(user_id=10, action="reactions.search.clear")
    assert "Поиск:" not in cleared_search.text
    assert "Фильтр каналов: включенные" in cleared_search.text
    assert "Тип реакций: плохие" in cleared_search.text
    assert "Приоритет: премиум" in cleared_search.text


def test_control_panel_autolike_channel_list_uses_last_enabled_effective_source(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    repo.upsert_reaction_folder(2, "AI feeds", position=0)
    repo.replace_reaction_folder_members(
        2,
        [{"chat_id": -100123, "title": "News", "kind": "channel"}],
    )
    repo.upsert_reaction_channel(-100123, "News")
    repo.set_reaction_channel_mode(-100123, "negative")
    repo.set_reaction_channel_source(-100123, "standard")
    repo.set_reaction_channel_enabled(-100123, True)
    repo.set_reaction_folder_mode(2, "all")
    repo.set_reaction_folder_source(2, "premium")
    repo.set_reaction_folder_enabled(2, True)
    panel = ControlPanelService(owner_id=10, state=repo)

    channels = panel.handle_action(user_id=10, action="reactions")

    assert "📁 News · все · премиум" in button_texts(channels)
    assert "✅ News · плохие · обычные" not in button_texts(channels)

    premium = panel.handle_action(user_id=10, action="reactions.filter.source:premium")
    standard = panel.handle_action(user_id=10, action="reactions.filter.source:standard")

    assert "📁 News · все · премиум" in button_texts(premium)
    assert "News" not in "\n".join(button_texts(standard))


def test_control_panel_autolike_channel_search_flow(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    repo.upsert_known_chat(-100456, "Archive", "channel", last_seen_at=20)
    panel = ControlPanelService(owner_id=10, state=repo)

    prompt = panel.handle_action(user_id=10, action="reactions.search")

    assert prompt.input_state == "reaction_channel_search"
    assert "часть названия" in prompt.text
    assert button_by_text(prompt, "К списку каналов").action == "reactions"

    results = panel.handle_text(user_id=10, state="reaction_channel_search", text="new")

    assert "Поиск: new" in results.text
    assert "Найдено: 1" in results.text
    texts = button_texts(results)
    assert "❌ News · хорошие · смесь" in texts
    assert "❌ Archive" not in texts
    assert button_by_text(results, "Найти заново").action == "reactions.search"
    assert button_by_text(results, "Сбросить поиск").action == "reactions.search.clear"


def test_control_panel_autolike_delay_range_setting(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    prompt = panel.handle_action(user_id=10, action="reactions.delay")
    assert prompt.input_state == "reaction_delay"
    assert "диапазон задержки" in prompt.text

    updated = panel.handle_text(user_id=10, state="reaction_delay", text="45-120")

    assert repo.get_reaction_delay_range_seconds() == (45, 120)
    assert "Пауза: 45-120 сек" in updated.text


def test_control_panel_autolike_history_screen_and_channel_action(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    repo.upsert_reaction_channel(-100123, "News")
    repo.set_reaction_channel_enabled(-100123, True)
    panel = ControlPanelService(owner_id=10, state=repo)

    root = panel.handle_action(user_id=10, action="reactions")
    assert button_by_text(root, "История").action == "reactions.history"

    history = panel.handle_action(user_id=10, action="reactions.history")
    assert "История автолайка" in history.text
    assert "Каналов для массового запуска: 1" in history.text
    assert "Массовый запуск берет только включенные realtime-каналы" in history.text
    assert "Выключенный канал можно обработать из карточки канала" in history.text
    assert "Лимит считается по новым пригодным постам" in history.text
    assert "8-15 сек" in history.text
    assert button_by_text(history, "1000").action == "rhb:all:1000"
    assert button_by_text(history, "2000").action == "rhb:all:2000"
    assert button_by_text(history, "5000").action == "rhb:all:5000"
    assert button_by_text(history, "Все посты").action == "rhb:all:all"
    assert button_by_text(history, "К автолайку").action == "reactions"
    assert all(len(f"panel:{action}".encode()) <= 64 for action in button_actions(history))

    detail = panel.handle_action(user_id=10, action="reactions.channel:-100123:0")
    assert button_by_text(detail, "История").action == "reactions.channel.history:-100123:0"

    channel_history = panel.handle_action(user_id=10, action="reactions.channel.history:-100123:0")
    assert "История канала: News" in channel_history.text
    assert "Фильтр: хорошие" in channel_history.text
    assert "Приоритет: смесь" in channel_history.text
    assert "Выбор: случайно" in channel_history.text
    assert "Реакций на пост: 3" in channel_history.text
    assert "Лимит считается по новым пригодным постам" in channel_history.text
    assert button_by_text(channel_history, "1000").action == "rhb:ch:-100123:1000:0"
    assert button_by_text(channel_history, "2000").action == "rhb:ch:-100123:2000:0"
    assert button_by_text(channel_history, "5000").action == "rhb:ch:-100123:5000:0"
    assert button_by_text(channel_history, "Все посты").action == "rhb:ch:-100123:all:0"
    assert button_by_text(channel_history, "К настройкам").action == "reactions.channel:-100123:0"
    assert all(len(f"panel:{action}".encode()) <= 64 for action in button_actions(channel_history))


def test_control_panel_autolike_history_allows_disabled_channel_with_settings(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    repo.upsert_reaction_channel(-100123, "News")
    repo.set_reaction_channel_enabled(-100123, False)
    panel = ControlPanelService(owner_id=10, state=repo)

    channel_history = panel.handle_action(user_id=10, action="reactions.channel.history:-100123:0")

    assert "История канала: News" in channel_history.text
    assert "Автолайк: выключен" in channel_history.text
    assert button_by_text(channel_history, "1000").action == "rhb:ch:-100123:1000:0"
    assert button_by_text(channel_history, "Все посты").action == "rhb:ch:-100123:all:0"


def test_control_panel_autolike_channel_filters_and_custom_reactions(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    panel = ControlPanelService(owner_id=10, state=repo)

    toggled = panel.handle_action(user_id=10, action="reactions.channel.toggle:-100123:0")
    assert repo.get_reaction_channel_settings(-100123).enabled
    assert "✅ News · хорошие · смесь" in button_texts(toggled)

    negative = panel.handle_action(user_id=10, action="reactions.channel.mode:-100123:negative:0")
    assert repo.get_reaction_channel_settings(-100123).mode == "negative"
    assert "Фильтр: плохие" in negative.text

    emoji_list = panel.handle_action(user_id=10, action="reactions.channel.emojis:-100123:0")
    assert "✅ 👍" in button_texts(emoji_list)
    assert button_by_text(emoji_list, "✅ 👍").style == "primary"

    disabled = panel.handle_action(user_id=10, action="reactions.channel.emoji:-100123:🔥:0")
    settings = repo.get_reaction_channel_settings(-100123)
    assert settings.mode == "negative"
    assert settings.selected_emojis == ()
    assert settings.disabled_emojis == ("🔥",)
    assert "❌ 🔥" in button_texts(disabled)
    assert button_by_text(disabled, "❌ 🔥").style is None


def test_control_panel_autolike_channel_random_strategy_and_reaction_categories(tmp_path):
    from telepath.features.channel_reactions import ReactionCandidate

    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    repo.replace_reaction_channel_available_reactions(
        -100123,
        [
            ReactionCandidate(kind="emoji", emoji="🔥", value="ignored", category="positive"),
            ReactionCandidate(kind="custom", emoji="1234567890123456789", value="ignored", category="neutral"),
        ],
    )
    panel = ControlPanelService(owner_id=10, state=repo)

    detail = panel.handle_action(user_id=10, action="reactions.channel:-100123:0")
    assert "Выбор: случайно" in detail.text
    assert "Случайно" in button_texts(detail)

    randomized = panel.handle_action(user_id=10, action="reactions.channel.strategy:-100123:random:0")
    assert repo.get_reaction_channel_settings(-100123).selection_strategy == "random"
    assert "Выбор: случайно" in randomized.text

    emoji_list = panel.handle_action(user_id=10, action="reactions.channel.emojis:-100123:0")
    texts = button_texts(emoji_list)
    assert "Кат: ?" in texts
    assert "Кат: +" in texts
    assert button_by_text(emoji_list, "Кат: ?").style == "primary"
    assert button_by_text(emoji_list, "Кат: +").style == "success"
    assert button_by_text(emoji_list, "✅ ⭐ 12345678…").icon_custom_emoji_id == "1234567890123456789"

    recategorized = panel.handle_action(user_id=10, action="rcc:-100123:1234567890123456789:0")
    assert repo.get_reaction_channel_settings(-100123).emoji_categories["1234567890123456789"] == "positive"
    assert "Кат: +" in button_texts(recategorized)


def test_control_panel_autolike_uses_observed_channel_reaction_picker(tmp_path):
    from telepath.features.channel_reactions import ReactionCandidate

    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-1001234567890, "News", "channel", last_seen_at=30)
    repo.replace_reaction_channel_available_reactions(
        -1001234567890,
        [
            ReactionCandidate(kind="emoji", emoji="👎", value="ignored", category="negative"),
            ReactionCandidate(kind="custom", emoji="1234567890123456789", value="ignored", category="positive"),
        ],
    )
    panel = ControlPanelService(owner_id=10, state=repo)

    detail = panel.handle_action(user_id=10, action="reactions.channel:-1001234567890:0")
    assert "Доступных реакций: 2" in detail.text

    emoji_list = panel.handle_action(user_id=10, action="reactions.channel.emojis:-1001234567890:0")

    texts = button_texts(emoji_list)
    assert "✅ 👎" in texts
    assert "✅ ⭐ 12345678…" in texts
    assert "Кат: -" in texts
    assert "Кат: +" in texts
    assert button_by_text(emoji_list, "Кат: -").style == "danger"
    assert button_by_text(emoji_list, "Кат: +").style == "success"
    assert button_by_text(emoji_list, "✅ ⭐ 12345678…").icon_custom_emoji_id == "1234567890123456789"
    assert "✅ 👍" not in texts
    assert all(len(f"panel:{action}".encode()) <= 64 for action in button_actions(emoji_list))


def test_control_panel_autolike_reaction_picker_does_not_show_default_reactions_when_checked_empty(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.upsert_known_chat(-100123, "News", "channel", last_seen_at=30)
    repo.upsert_reaction_channel(-100123, "News")
    repo.replace_reaction_channel_available_reactions(-100123, [])
    panel = ControlPanelService(owner_id=10, state=repo)

    detail = panel.handle_action(user_id=10, action="reactions.channel:-100123:0")
    emoji_list = panel.handle_action(user_id=10, action="reactions.channel.emojis:-100123:0")

    assert "Доступных реакций: 0" in detail.text
    assert "Доступные реакции не найдены" in emoji_list.text
    assert "✅ 👍" not in button_texts(emoji_list)
    assert "Обновить доступные" in button_texts(emoji_list)


def test_control_panel_prompt_view_edit_and_reset(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    prompt_view = panel.handle_action(user_id=10, action="transcription.prompt")
    assert "Текущий промпт" in prompt_view.text
    assert "Изменить промпт" in button_texts(prompt_view)
    assert_no_home_button(prompt_view)
    assert_bottom_back_only(prompt_view)

    edit = panel.handle_action(user_id=10, action="transcription.prompt.edit")
    assert edit.input_state == "text_polish_prompt"
    assert_bottom_back_only(edit)

    updated = panel.handle_text(user_id=10, state="text_polish_prompt", text="Расставь запятые, смысл не меняй.")
    assert "Промпт обновлен" in updated.text
    assert repo.get_text_polish_prompt() == "Расставь запятые, смысл не меняй."
    assert_bottom_back_only(updated)

    reset = panel.handle_action(user_id=10, action="transcription.prompt.reset")
    assert "Промпт сброшен" in reset.text
    assert "Не делай summary" in repo.get_text_polish_prompt()
    assert_bottom_back_only(reset)


def test_control_panel_status_shows_openai_provider_and_model(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(
        owner_id=10,
        state=repo,
        llm_provider="openai",
        llm_model="gpt-4o-mini",
    )

    view = _status(panel)

    assert "LLM: OpenAI (gpt-4o-mini)" in view.text
    assert "Экспорт чатов: недоступен" in view.text
    assert "Copilot" not in view.text  # legacy hardcoded line is gone
    assert button_by_text(view, "Обновить").action == "status.refresh"
    assert "Назад" in button_texts(view)


def test_control_panel_status_has_refresh_and_section_navigation(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.block_chat(100, "Alice")
    repo.allow_group(-100123, "Team")
    repo.upsert_reaction_channel(-100456, "News")
    repo.set_reaction_channel_enabled(-100456, True)
    repo.set_account_premium(True)
    panel = ControlPanelService(owner_id=10, state=repo, chat_export_enabled=True)

    view = _status(panel)

    assert "Статус" in view.text
    assert "Транскрибация: включена" in view.text
    assert "Manager bot: работает" in view.text
    assert "User client: неизвестно" in view.text
    assert "Очередь ГС: неизвестно" in view.text
    assert "Последняя ошибка: нет данных" in view.text
    assert "Исключения: 1" in view.text
    assert "Группы: 1" in view.text
    assert "Автолайк ТГК: 1 каналов" in view.text
    assert "Premium: да" in view.text
    assert "Экспорт чатов: доступен" in view.text
    assert button_by_text(view, "Обновить").action == "status.refresh"
    assert button_by_text(view, "Транскрибация").action == "transcription"
    assert button_by_text(view, "Автолайк ТГК").action == "reactions"
    assert button_by_text(view, "Экспорт чатов").action == "export.chats"
    assert button_by_text(view, "Помощь").action == "help"
    assert "Назад" in button_texts(view)
    assert all(len(f"panel:{action}".encode()) <= 64 for action in button_actions(view))

    refreshed = panel.handle_action(user_id=10, action="status.refresh")

    assert refreshed.action == "status"
    assert "Premium: да" in refreshed.text


def test_control_panel_status_shows_chat_export_when_enabled(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo, chat_export_enabled=True)

    view = _status(panel)

    assert "Экспорт чатов: доступен" in view.text


def test_control_panel_status_shows_anthropic_provider_and_model(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(
        owner_id=10,
        state=repo,
        llm_provider="anthropic",
        llm_model="claude-haiku-4-5-20251001",
    )

    view = _status(panel)

    assert "LLM: Anthropic (claude-haiku-4-5-20251001)" in view.text


def test_control_panel_status_shows_copilot_when_default(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = _status(panel)

    assert "LLM: GitHub Copilot CLI" in view.text


def test_control_panel_status_omits_model_when_unset(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo, llm_provider="openai", llm_model=None)

    view = _status(panel)

    assert "LLM: OpenAI\n" in view.text + "\n"  # no trailing parens, no None
    assert "(None)" not in view.text


def test_control_panel_status_renders_unknown_provider_label_as_value(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo, llm_provider="custom")

    view = _status(panel)

    assert "LLM: custom" in view.text


def test_control_panel_status_denied_to_non_owner(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo, llm_provider="openai", llm_model="m")

    view = _status(panel, user_id=11)

    assert view.text == "Доступ запрещен."


# --- handle_action coverage edges ------------------------------------------


def test_handle_action_main_alias_returns_main(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    for action in ("main", "home", "back"):
        view = panel.handle_action(user_id=10, action=action)
        assert "Главное меню" in view.text


def test_handle_action_help_returns_help_view(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_action(user_id=10, action="help")

    assert "Помощь" in view.text
    assert "Транскрибация обрабатывает голосовые" in view.text
    assert "Группы включаются только явно" in view.text
    assert "Каналы не транскрибируются" in view.text
    assert "1." not in view.text
    assert button_by_text(view, "Транскрибация").action == "transcription"
    assert button_by_text(view, "Чаты").action == "transcription.chats"
    assert button_by_text(view, "Группы").action == "transcription.groups"
    assert button_by_text(view, "Автолайк ТГК").action == "reactions"
    assert button_by_text(view, "Статус").action == "status"
    assert "Назад" in button_texts(view)
    assert all(len(f"panel:{action}".encode()) <= 64 for action in button_actions(view))


def test_handle_action_unknown_returns_fallback_message(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_action(user_id=10, action="nope")

    assert "Неизвестное действие" in view.text


def test_handle_action_groups_remove_returns_groups_view(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_action(user_id=10, action="transcription.groups.remove")

    assert "Группы" in view.text


def test_handle_action_denied_for_non_owner(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_action(user_id=11, action="help")

    assert view.text == "Доступ запрещен."


def test_help_mentions_chat_export_when_enabled(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo, chat_export_enabled=True)

    view = panel.handle_action(user_id=10, action="help")

    assert "Экспорт чатов" in view.text
    assert button_by_text(view, "Экспорт чатов").action == "export.chats"


# --- pagination -------------------------------------------------------------


def test_groups_view_paginates_when_many_groups(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    for i in range(20):
        repo.upsert_known_group(-1000 - i, f"Group {i}")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_action(user_id=10, action="transcription.groups.page:1")
    # Buttons for groups on page 1 + pagination row + add + back
    labels = button_texts(view)
    assert any("2/" in label for label in labels)  # page indicator
    assert "‹" in labels and "›" in labels


def test_parse_page_returns_zero_on_invalid(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    # malformed page action → ValueError caught → defaults to first page
    view = panel.handle_action(user_id=10, action="transcription.groups.page:abc")
    assert "Группы" in view.text


def test_groups_view_shows_empty_state_when_no_groups(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_action(user_id=10, action="transcription.groups")

    assert "Пока нет найденных групп" in view.text


# --- blacklist empty state --------------------------------------------------


def test_blacklist_view_shows_empty_message(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_action(user_id=10, action="transcription.blacklist")

    assert "Исключения пусты" in view.text


# --- _toggle_group safety branches ------------------------------------------


def test_toggle_group_with_malformed_action_returns_groups(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_action(user_id=10, action="transcription.groups.toggle:123")
    assert "Группы" in view.text


def test_toggle_group_with_non_int_chat_id_returns_groups(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_action(user_id=10, action="transcription.groups.toggle:foo:bar")
    assert "Группы" in view.text


def test_toggle_unknown_group_uses_chat_id_as_title(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_action(user_id=10, action="transcription.groups.toggle:-999:0")
    assert "Включено: -999" in view.text


# --- handle_text edges ------------------------------------------------------


def test_handle_text_disallow_group_route(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    repo.allow_group(-100123, "Team")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_text(user_id=10, state="disallow_group", text=" -100123 ")

    assert "Группа -100123 выключена" in view.text
    assert not repo.is_group_allowed(-100123)


def test_handle_text_slash_command_routes_to_command_service(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_text(user_id=10, state=None, text="/list")

    assert isinstance(view.text, str)
    assert_bottom_back_only(view)


def test_handle_text_falls_back_to_main_when_no_state_and_no_command(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_text(user_id=10, state=None, text="hello world")
    assert "Главное меню" in view.text


def test_handle_text_denied_for_non_owner(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_text(user_id=99, state="block_chat", text="123")
    assert view.text == "Доступ запрещен."


# --- block_chat / unblock_chat / allow_group / disallow_group input errors -


def test_block_chat_empty_text_returns_retry(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_text(user_id=10, state="block_chat", text="   ")
    assert "chat_id" in view.text
    assert "Повторить" in button_texts(view)


def test_block_chat_non_int_chat_id_returns_retry(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_text(user_id=10, state="block_chat", text="not_a_number")
    assert "числом" in view.text


def test_unblock_chat_non_int_returns_retry(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_text(user_id=10, state="unblock_chat", text="garbage")
    assert "числом" in view.text


def test_allow_group_empty_text_returns_retry(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_text(user_id=10, state="allow_group", text="")
    assert "chat_id группы" in view.text


def test_allow_group_non_int_returns_retry(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_text(user_id=10, state="allow_group", text="abc Team")
    assert "числом" in view.text


def test_disallow_group_non_int_returns_retry(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_text(user_id=10, state="disallow_group", text="abc")
    assert "числом" in view.text


def test_prompt_text_too_short_returns_retry(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.handle_text(user_id=10, state="text_polish_prompt", text="too short")
    assert "слишком короткий" in view.text
