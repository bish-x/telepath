from pathlib import Path

from telepath.session_paths import ensure_session_parent


def test_ensure_session_parent_creates_parent_for_nested_sqlite_session(tmp_path):
    session_name = tmp_path / "data" / "telepath"

    ensure_session_parent(str(session_name))

    assert (tmp_path / "data").is_dir()


def test_ensure_session_parent_ignores_string_session():
    ensure_session_parent("1A" * 180)
