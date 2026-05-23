from __future__ import annotations

import asyncio

from telepath.config import Settings, load_settings
from telepath.panel import PanelView, ControlPanelService
from telepath.premium_emoji import extract_premium_emoji_ids, format_premium_emoji_reply
from telepath.storage import SQLiteAssistantRepository


def _active_llm_model(settings: Settings) -> str | None:
    if settings.llm_provider == "openai":
        return settings.openai_model
    if settings.llm_provider == "anthropic":
        return settings.anthropic_model
    if settings.llm_provider == "copilot":
        return settings.copilot_model
    return None


def _panel_markup(view: PanelView):
    if not view.keyboard:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=button.text, callback_data=f"panel:{button.action}") for button in row]
            for row in view.keyboard
        ]
    )


class PanelNavigation:
    def __init__(self):
        self._current: dict[int, str] = {}
        self._history: dict[int, list[str]] = {}

    def reset(self, *, user_id: int) -> None:
        self._current[user_id] = "main"
        self._history[user_id] = []

    def visit(self, *, user_id: int, action: str) -> None:
        current = self._current.get(user_id, "main")
        if action != current:
            self._history.setdefault(user_id, []).append(current)
        self._current[user_id] = action

    def back(self, *, user_id: int) -> str:
        history = self._history.setdefault(user_id, [])
        action = history.pop() if history else "main"
        self._current[user_id] = action
        return action


async def run_manager_bot(settings: Settings) -> None:  # pragma: no cover - integration only
    from aiogram import Bot, Dispatcher, F, types

    bot = Bot(token=settings.manager_bot_token)
    dispatcher = Dispatcher()
    state = SQLiteAssistantRepository(settings.database_path)
    panel = ControlPanelService(
        owner_id=settings.owner_id,
        state=state,
        llm_provider=settings.llm_provider,
        llm_model=_active_llm_model(settings),
    )
    pending_input: dict[int, str] = {}
    navigation = PanelNavigation()

    def remember_input(user_id: int, view: PanelView) -> None:
        if view.input_state:
            pending_input[user_id] = view.input_state
        else:
            pending_input.pop(user_id, None)

    def render_action(user_id: int, action: str) -> PanelView:
        if action == "main":
            return panel.main(user_id=user_id)
        return panel.handle_action(user_id=user_id, action=action)

    @dispatcher.callback_query(F.data.startswith("panel:"))
    async def handle_panel_callback(callback: types.CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        requested_action = (callback.data or "").removeprefix("panel:")
        if requested_action == "back":
            view = render_action(user_id, navigation.back(user_id=user_id))
        else:
            view = panel.handle_action(user_id=user_id, action=requested_action)
            navigation.visit(user_id=user_id, action=view.action)
        remember_input(user_id, view)
        if callback.message:
            await callback.message.edit_text(view.text, reply_markup=_panel_markup(view))
        await callback.answer()

    @dispatcher.message()
    async def handle_message(message: types.Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        text = message.text or ""
        if text.startswith("/start") or text.startswith("/menu"):
            view = panel.main(user_id=user_id)
            navigation.reset(user_id=user_id)
        else:
            premium_emojis = extract_premium_emoji_ids(message)
            if user_id == settings.owner_id and premium_emojis:
                await message.answer(format_premium_emoji_reply(premium_emojis))
                return
            view = panel.handle_text(user_id=user_id, state=pending_input.get(user_id), text=text)
            navigation.visit(user_id=user_id, action=view.action)
        remember_input(user_id, view)
        await message.answer(view.text, reply_markup=_panel_markup(view))

    await dispatcher.start_polling(bot)


def main() -> None:  # pragma: no cover - integration only
    asyncio.run(run_manager_bot(load_settings()))


if __name__ == "__main__":
    main()
