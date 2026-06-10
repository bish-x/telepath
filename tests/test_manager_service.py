from telepath.manager import ManagerService
from telepath.storage import SQLiteAssistantRepository


def _service(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    return ManagerService(owner_id=10, blacklist=repo), repo


def test_manager_rejects_non_owner_commands(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    service = ManagerService(owner_id=10, blacklist=repo)

    response = service.handle_command(user_id=11, text="/block 123 Alice")

    assert response == "Доступ запрещен."
    assert not repo.is_blocked(123)


def test_manager_allows_owner_to_manage_blacklist(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    service = ManagerService(owner_id=10, blacklist=repo)

    assert service.handle_command(user_id=10, text="/block 123 Alice") == "Чат 123 добавлен в исключения."
    assert service.handle_command(user_id=10, text="/block -456 Work Group") == "Чат -456 добавлен в исключения."

    assert repo.is_blocked(123)
    assert repo.is_blocked(-456)
    assert service.handle_command(user_id=10, text="/list") == "Исключения:\n- -456: Work Group\n- 123: Alice"

    assert service.handle_command(user_id=10, text="/unblock 123") == "Чат 123 убран из исключений."
    assert not repo.is_blocked(123)


def test_manager_allows_owner_to_manage_group_whitelist(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    service = ManagerService(owner_id=10, blacklist=repo)

    assert service.handle_command(user_id=10, text="/allow_group -100123 Team") == "Группа -100123 включена."
    assert repo.is_group_allowed(-100123)
    assert service.handle_command(user_id=10, text="/groups") == "Включенные группы:\n- -100123: Team"

    assert service.handle_command(user_id=10, text="/deny_group -100123") == "Группа -100123 выключена."
    assert not repo.is_group_allowed(-100123)


def test_manager_reports_invalid_commands(tmp_path):
    repo = SQLiteAssistantRepository(tmp_path / "assistant.sqlite3")
    service = ManagerService(owner_id=10, blacklist=repo)

    assert "Формат:" in service.handle_command(user_id=10, text="/block")
    assert "Неизвестная команда" in service.handle_command(user_id=10, text="/wat")


def test_manager_help_on_empty_input(tmp_path):
    service, _ = _service(tmp_path)
    assert "Команды:" in service.handle_command(user_id=10, text="   ")


def test_manager_help_on_start_command(tmp_path):
    service, _ = _service(tmp_path)
    assert "Команды:" in service.handle_command(user_id=10, text="/start")
    assert "Команды:" in service.handle_command(user_id=10, text="/help")


def test_manager_status_command(tmp_path):
    service, _ = _service(tmp_path)
    assert service.handle_command(user_id=10, text="/status") == "Панель управления работает."


def test_manager_block_with_non_int_chat_id(tmp_path):
    service, _ = _service(tmp_path)
    assert service.handle_command(user_id=10, text="/block abc Alice").startswith("Формат:")


def test_manager_unblock_argument_validation(tmp_path):
    service, _ = _service(tmp_path)
    assert service.handle_command(user_id=10, text="/unblock").startswith("Формат:")
    assert service.handle_command(user_id=10, text="/unblock abc").startswith("Формат:")
    assert service.handle_command(user_id=10, text="/unblock 1 extra").startswith("Формат:")


def test_manager_allow_group_argument_validation(tmp_path):
    service, _ = _service(tmp_path)
    assert service.handle_command(user_id=10, text="/allow_group").startswith("Формат:")
    assert service.handle_command(user_id=10, text="/allow_group not_a_number").startswith("Формат:")


def test_manager_deny_group_argument_validation(tmp_path):
    service, _ = _service(tmp_path)
    assert service.handle_command(user_id=10, text="/deny_group").startswith("Формат:")
    assert service.handle_command(user_id=10, text="/deny_group abc").startswith("Формат:")
    assert service.handle_command(user_id=10, text="/deny_group 1 extra").startswith("Формат:")


def test_manager_groups_when_empty(tmp_path):
    service, _ = _service(tmp_path)
    assert service.handle_command(user_id=10, text="/groups") == "Включенные группы: нет"
