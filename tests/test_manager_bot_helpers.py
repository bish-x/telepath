from pathlib import Path

from telepath.config import Settings
from telepath.manager_bot import _active_llm_model, _panel_markup
from telepath.panel import PanelButton, PanelView


def _settings(**overrides) -> Settings:
    base = dict(
        telegram_api_id=1,
        telegram_api_hash="h",
        telegram_session="s",
        manager_bot_token="t",
        owner_id=1,
        database_path=Path("data/x.sqlite3"),
    )
    base.update(overrides)
    return Settings(**base)


def test_active_llm_model_for_each_provider():
    assert _active_llm_model(_settings(llm_provider="openai", openai_model="gpt-x")) == "gpt-x"
    assert (
        _active_llm_model(_settings(llm_provider="anthropic", anthropic_model="claude-x"))
        == "claude-x"
    )
    assert _active_llm_model(_settings(llm_provider="copilot", copilot_model="cop-x")) == "cop-x"


def test_active_llm_model_returns_none_for_unknown_provider():
    assert _active_llm_model(_settings(llm_provider="unknown")) is None


def test_panel_markup_returns_none_when_keyboard_empty():
    view = PanelView(text="hi", keyboard=[])

    assert _panel_markup(view) is None


def test_panel_markup_builds_inline_keyboard_with_panel_prefix():
    view = PanelView(
        text="hi",
        keyboard=[
            [PanelButton("A", "main"), PanelButton("B", "status")],
            [PanelButton("Back", "back")],
        ],
    )

    markup = _panel_markup(view)

    assert markup is not None
    rows = markup.inline_keyboard
    assert [(btn.text, btn.callback_data) for btn in rows[0]] == [
        ("A", "panel:main"),
        ("B", "panel:status"),
    ]
    assert [(btn.text, btn.callback_data) for btn in rows[1]] == [("Back", "panel:back")]
