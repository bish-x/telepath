# Telepath

> Personal Telegram account assistant with owner-only bot controls and pluggable LLM text polishing.

Telepath runs alongside your personal Telegram account, transcribes incoming and outgoing voice messages and video notes using Telegram's own transcription service, polishes the transcripts with an LLM of your choice, and replies in the original chat — wrapped in a premium emoji. A separate owner-only manager bot exposes a button-based control panel so you can toggle features, manage exceptions, and pick which groups are in scope.

The architecture is feature-based. Voice transcription is the first capability; future features (chat summaries, reminders, etc.) live under `telepath/features/` and reuse the same Telegram clients, storage, and LLM ports.

## Prerequisites

Before you start, gather:

1. **Docker** — Docker Desktop (macOS / Windows) or Docker Engine + Compose v2 (Linux). <https://docs.docker.com/get-docker/>
2. **Telegram API credentials** — sign in at <https://my.telegram.org/apps> and create an app. Copy `App api_id` and `App api_hash`.
3. **A manager bot** — open <https://t.me/BotFather>, send `/newbot`, give it a name. Copy the HTTP API token it prints (looks like `1234567890:AAEhBP_...`).
4. **An LLM API key** — pick one:
   - **OpenAI**: create a key at <https://platform.openai.com/api-keys>.
   - **Anthropic**: create a key at <https://console.anthropic.com/settings/keys>.

## Quickstart

```bash
git clone https://github.com/bish-x/telepath.git
cd telepath
./scripts/setup.sh
```

`setup.sh` is interactive and idempotent. It walks you through:

1. Telegram API credentials (`TG_API_ID`, `TG_API_HASH`).
2. Manager bot token (`TG_MANAGER_BOT_TOKEN`).
3. LLM provider (OpenAI by default; Anthropic also supported out of the box).
4. **Telegram user authorization** — the container will ask you for your phone number (international format, e.g. `+1234567890`), the login code Telegram sends to your other devices, and your 2FA password if you have one. Your numeric user id (`TG_OWNER_ID`) is captured automatically and saved to `.env`.
5. `docker compose up -d` to start the stack.

When it finishes, open Telegram and send `/start` to **your manager bot** (the one you created with @BotFather). Only your account — the one you authorised in step 4 — can use the panel.

Re-run `./scripts/setup.sh` any time to rotate keys or switch the LLM provider. Run `./scripts/auth.sh` if the Telegram session expires or you want to switch accounts.

## Useful commands

```bash
docker compose logs -f telepath     # tail logs
docker compose restart telepath     # restart
docker compose down                 # stop
./scripts/auth.sh                   # re-authorize Telegram (e.g. session revoked)
./scripts/setup.sh                  # rotate keys, switch LLM provider
```

## What it does, in detail

- Runs a Telegram **user client** for account-level access (Telethon-based).
- Runs a separate **owner-only manager bot** with a button panel (aiogram).
- Transcribes incoming and outgoing voice messages and video notes through Telegram's user-only transcription endpoint.
- Polishes the transcript with the configured LLM (Copilot CLI / OpenAI / Anthropic) — punctuation, paragraphs, obvious recognition fixes only; never paraphrases or summarises.
- Replies in the original chat with the polished text, wrapped in your configured premium emoji.
- Marks recognised Russian profanity with strikethrough entities (whole-word matches; substrings inside normal words are left alone).
- Returns the `custom_emoji_id` when the owner sends a premium emoji to the manager bot.

## Runtime safety rules

- Personal chats are processed only after the user client has seen at least 100 visible messages in that dialog (cached in SQLite).
- Groups are processed only when explicitly selected via the manager panel.
- Channels are always skipped.
- Personal-chat exceptions are skipped.
- Voice/video notes longer than 5 minutes are skipped.
- If Telegram returns `MSG_VOICE_TOO_LONG`, `TRANSCRIPTION_FAILED`, or an empty transcript, Telepath skips silently and marks the message processed.
- Processed message IDs are recorded so reconnects don't trigger duplicate replies.
- Voice messages are processed **sequentially through a bounded queue** (default capacity 64). One in-flight transcription/polish/reply at a time, FIFO across all chats — protects against Telegram and LLM rate limits. If the queue fills up under a burst, extra events are dropped with a `voice_dropped_queue_full` warning rather than back-pressuring Telethon.

## Manager bot panel

Only `TG_OWNER_ID` can open and use the panel. Send `/start` or `/menu` to the manager bot.

```text
Главное меню
├─ Транскрибация
│  ├─ Включить / Выключить
│  ├─ Исключения
│  │  ├─ Заблокировать чат
│  │  └─ Разблокировать чат
│  ├─ Группы
│  │  ├─ ✅ / ○ toggle для каждой группы
│  │  └─ Ввести chat_id
│  └─ Промпт
│     ├─ Изменить промпт
│     └─ Сбросить промпт
├─ Статус
└─ Помощь
```

Slash commands are still supported as a fallback:

```text
/block <chat_id> [title]
/unblock <chat_id>
/list
/allow_group <chat_id> [title]
/deny_group <chat_id>
/groups
/status
```

Send any premium/custom emoji to the manager bot and it replies with the corresponding `custom_emoji_id`.

## LLM providers

Set `LLM_PROVIDER` in `.env` to one of:

| Provider    | Required env vars                          | Notes                                                                              |
|-------------|--------------------------------------------|------------------------------------------------------------------------------------|
| `openai`    | `OPENAI_API_KEY`, `OPENAI_MODEL`           | Works out of the box in Docker. Recommended for first-time setup.                  |
| `anthropic` | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`     | Works out of the box in Docker.                                                    |
| `copilot`   | `COPILOT_COMMAND`, `COPILOT_MODEL`         | Requires `copilot` CLI on the host. **Not bundled in the Docker image.**           |

`OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` let you point at any compatible endpoint (e.g. a Codex-style proxy or an enterprise gateway).

Adding a new provider takes two files:

1. A class under `telepath/llm/` implementing `polish(text, prompt) -> str` (see `openai_api.py`).
2. A branch in `build_polisher()` in `telepath/llm/__init__.py`.

## Running without Docker

If you'd rather run on bare Python (no container), you need Python 3.12+ and either [`uv`](https://docs.astral.sh/uv/) or `pip`:

```bash
# with uv (recommended)
uv venv
uv pip install -e ".[dev]"

# or with stock pip
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# either way:
cp .env.example .env       # fill in the values
telepath-auth              # first-time Telegram authorization
telepath                   # starts both workers
```

Or split the workers across processes: `telepath-user` and `telepath-manager`.

## Project layout

```
telepath/
├── app.py                # entrypoint: runs user client + manager bot together
├── auth.py               # `telepath-auth` CLI: first-time Telethon authorization
├── config.py             # env loading and validation
├── user_client.py        # Telethon-driven user client + event dispatch
├── manager_bot.py        # aiogram-driven owner-only control bot
├── panel.py              # button-based control panel
├── manager.py            # manager service (state changes)
├── runtime.py            # AssistantContext: the narrow ports each feature receives
├── features/             # feature modules — voice_transcription, future capabilities
│   ├── base.py
│   └── voice_transcription.py
├── llm/                  # pluggable text polishers
│   ├── base.py           # TextPolisher Protocol + shared exceptions
│   ├── copilot_cli.py    # `copilot -p ...` subprocess polisher
│   ├── openai_api.py     # OpenAI Chat Completions polisher
│   └── anthropic_api.py  # Anthropic Messages polisher
├── premium_emoji.py
├── profanity.py
├── prompts.py
├── session_paths.py
└── storage.py            # SQLite repository: state, exceptions, groups, processed IDs
```

## Tests

```bash
uv run pytest -q
# or
pytest -q
```

Tests are pure-Python; no Telegram or LLM credentials required (all network calls are mocked through protocol-style ports). The suite enforces 100% coverage on the `telepath/` package.

## Design boundary

The user client owns chat access and transcription. The manager bot only changes configuration. Feature modules receive events and narrow ports, so Telegram clients, storage, and LLM providers can be swapped without rewriting feature behaviour.

## License

MIT. See [LICENSE](LICENSE).
