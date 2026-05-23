from telepath.panel import ControlPanelService
from telepath.storage import SQLiteAssistantRepository


def _status(panel, user_id=10):
    return panel.handle_action(user_id=user_id, action="status")


def button_texts(view):
    return [button.text for row in view.keyboard for button in row]


def assert_no_home_button(view):
    assert "Домой" not in button_texts(view)


def assert_bottom_back_only(view):
    assert view.keyboard[-1] == [type(view.keyboard[-1][0])("Назад", "back")]


def test_control_panel_is_owner_only(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    view = panel.main(user_id=11)

    assert view.text == "Access denied."
    assert view.keyboard == []


def test_control_panel_main_and_transcription_toggle(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    main = panel.main(user_id=10)
    assert "Главное меню" in main.text
    assert "Транскрибация" in button_texts(main)
    assert_no_home_button(main)

    transcription = panel.handle_action(user_id=10, action="transcription")
    assert "Транскрибация: включена" in transcription.text
    assert "Личные чаты" in transcription.text
    assert "Исключения" in button_texts(transcription)
    assert "Группы" in button_texts(transcription)
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


def test_control_panel_blacklist_add_and_remove_flow(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    panel = ControlPanelService(owner_id=10, state=repo)

    add_prompt = panel.handle_action(user_id=10, action="transcription.blacklist.add")
    assert add_prompt.input_state == "block_chat"
    assert "chat_id" in add_prompt.text
    assert_bottom_back_only(add_prompt)

    added = panel.handle_text(user_id=10, state="block_chat", text="100 Alice")
    assert repo.is_blocked(100)
    assert "Blocked chat 100" in added.text
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
    assert "Unblocked chat 100" in removed.text
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
    assert "Copilot" not in view.text  # legacy hardcoded line is gone
    assert_bottom_back_only(view)


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

    assert view.text == "Access denied."


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

    assert "Как пользоваться" in view.text
    assert_bottom_back_only(view)


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

    assert view.text == "Access denied."


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

    assert "Removed group -100123" in view.text
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
    assert view.text == "Access denied."


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
