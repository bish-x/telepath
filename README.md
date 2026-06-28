# Telepath

Personal Telegram account assistant with an owner-only control bot.

Telepath runs next to your own Telegram account. It can transcribe voice
messages, polish the transcript with an LLM, export chat history on demand, and
automatically react to selected channel posts. A separate manager bot gives you
a button-based control panel, so you can change settings from Telegram instead
of editing config files.

Telepath is not a hosted service. You run it yourself, keep the SQLite state
locally, and choose which LLM provider, chats, groups, and channels are in
scope.

## What You Get

- **Voice transcription** for incoming and outgoing voice messages and video
  notes, using Telegram's user-account transcription endpoint.
- **LLM text polishing** through OpenAI, Anthropic, or GitHub Copilot CLI:
  punctuation, paragraphs, and obvious recognition fixes without rewriting the
  message.
- **Owner-only manager bot** for feature toggles, group allowlists, chat
  export, channel autolike settings, diagnostics, and prompt editing.
- **Chat history export** from Telegram into `.txt` files, launched from the
  manager panel with presets and safety confirmations for large exports.
- **Channel autolike** from your Telegram account with per-channel settings,
  folder defaults, randomized reactions, Telegram Premium-aware reaction count,
  fallback handling, and delayed sending.
- **Post mirroring into forum topics**: selected channels or groups are copied
  into topics of one owner-selected forum group, including media and albums,
  without relying on Telegram forward metadata. Source posts are first persisted
  into a local SQLite outbox; topic delivery is attempted only while another
  Telegram client session for the owner account is recently active.
- **Local state** in SQLite: processed messages, allowlists, channel reaction
  settings, post-mirroring rules, cached Telegram folders, available reactions,
  and chat metadata.

## How It Works

Telepath starts two workers in the same process:

1. **User client**: a Telethon client logged in as your Telegram account. It can
   read messages, request Telegram voice transcriptions, export chat history,
   inspect channel reaction settings, and send reactions.
2. **Manager bot**: an aiogram bot that only accepts commands and callbacks from
   `TG_OWNER_ID`.

The user client owns Telegram account access. The manager bot only changes
configuration and starts explicit owner-requested actions. Feature modules
receive narrow ports for storage, LLM calls, replies, and reaction sending, so
the core behavior stays testable without live Telegram credentials.

## Quickstart

### 1. Clone and run setup

```bash
git clone https://github.com/bish-x/telepath.git
cd telepath
./scripts/setup.sh
```

`setup.sh` is interactive and idempotent. It asks for:

- Telegram API credentials from <https://my.telegram.org/apps>
- a manager bot token from [@BotFather](https://t.me/BotFather)
- your LLM provider and API key
- Telegram user authorization for the account Telepath will assist

When setup finishes, it starts Docker Compose. Open Telegram, send `/start` to
your manager bot, and use the panel from there.

### 2. Useful commands

```bash
docker compose logs -f telepath     # follow logs
docker compose restart telepath     # restart the assistant
docker compose down                 # stop everything
./scripts/setup.sh                  # rotate keys or change provider
./scripts/auth.sh                   # re-authorize the Telegram user session
```

## Configuration

Copy `.env.example` to `.env` manually, or let `./scripts/setup.sh` fill it.

Required values:

| Variable | Purpose |
| --- | --- |
| `TG_API_ID`, `TG_API_HASH` | Telegram API app credentials for the user client. |
| `TG_MANAGER_BOT_TOKEN` | BotFather token for the owner-only manager bot. |
| `TG_OWNER_ID` | Your numeric Telegram user id. `setup.sh` fills it during auth. |
| `TG_SESSION` | Telethon session path without the `.session` suffix. |
| `TG_ASSISTANT_DB` | SQLite database path. |
| `LLM_PROVIDER` | `openai`, `anthropic`, or `copilot`. |

Provider-specific values:

| Provider | Required variables | Notes |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY`, `OPENAI_MODEL` | Works in Docker out of the box. |
| Anthropic | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Works in Docker out of the box. |
| Copilot CLI | `COPILOT_COMMAND`, `COPILOT_MODEL` | Requires the `copilot` CLI on the host; it is not bundled in the Docker image. |

`OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` can point to compatible gateways.

## Manager Bot

Only `TG_OWNER_ID` can use the manager bot. Routine navigation answers callback
queries silently; visible Telegram toasts are reserved for meaningful
long-running actions such as starting a chat export or a channel history job.

Main areas:

- **Transcription**
  - global on/off switch
  - personal chat and group scopes
  - minimum voice duration
  - maximum voice duration
  - private-chat message threshold
  - prompt editing
  - diagnostics explaining why a message was skipped
- **Chat export**
  - searchable chat picker
  - preset export limits
  - manual limit entry
  - confirmation before heavy exports
  - `.txt` result sent back by the bot
- **Channel autolike**
  - global kill-switch
  - per-channel settings
  - Telegram folder defaults
  - delayed reactions
  - history backfill queue
  - available reaction refresh
  - ordinary, premium/custom, or mixed reaction sources
- **Status and help**
  - current runtime settings
  - quick command reference

Slash commands are still available as a fallback:

```text
/block <chat_id> [title]
/unblock <chat_id>
/list
/allow_group <chat_id> [title]
/deny_group <chat_id>
/groups
/status
```

Send a premium/custom emoji to the manager bot and Telepath replies with its
`custom_emoji_id`, which is useful when configuring custom reaction buttons.

## Voice Transcription

Telepath is conservative by default:

- personal chats are processed only after the configured visible-message
  threshold, unless explicitly enabled in the manager panel
- groups are processed only when explicitly selected
- channels are skipped by voice transcription
- personal-chat exceptions are skipped
- voice/video notes longer than the configured maximum duration are skipped
- voice/video notes shorter than the configured minimum duration are skipped
- Telegram transcription failures and empty transcripts are skipped silently and
  marked processed
- voice messages go through a bounded FIFO queue so Telegram and LLM providers
  are not hit in parallel bursts

The LLM polishing step is intentionally narrow: it fixes transcript readability
without summarizing, paraphrasing, or changing the meaning.

## Channel Autolike

Channel autolike reacts from your Telegram account, not from the manager bot.
It is designed for controlled channels and explicit owner configuration.

Core behavior:

- disabled globally by the kill-switch when needed
- per-channel settings override folder defaults
- folders can act as defaults for channels without manual settings
- reaction delay is configurable and defaults to 240-900 seconds
- delayed jobs read the latest message reactions at send time, not only when
  the post first appears
- non-Premium accounts are capped at one automatic reaction
- Premium accounts can use up to three reactions
- paid Telegram Star reactions are ignored and never automated
- already processed history posts are retried when owner reactions were removed
  or only partially applied

Reaction selection:

- source: ordinary reactions, premium/custom emoji reactions, or mixed
- mode: positive, negative, all, or manually selected
- strategy: priority order or random
- random mode avoids repeating the exact same set on every post when possible
- visible reactions already present on a post are used as a fallback when
  Telegram rejects unseen reactions
- custom/premium emoji reactions can be categorized manually per channel because
  Telegram does not expose their semantic meaning

History processing:

- available from the manager panel for one channel or all enabled channels
- batch sizes: 1000, 2000, 5000, or full history
- albums are handled as one logical post to avoid duplicate reactions
- jobs are queued so repeated clicks do not run overlapping history scans
  - completion is sent as a separate bot message with metrics
- **Posts to topics**
  - one target Telegram group with topics enabled
  - per-source channel/group topic mapping
  - topic creation from the manager panel when adding a source
  - realtime copy with no artificial delay
  - history backfill for old posts
  - albums copied as one logical post
  - idempotency so realtime and history do not duplicate copied posts

## Chat Export

The manager bot can export readable chat history as a `.txt` document or a
media `.zip` archive. Exports are owner-triggered, not automatic.

The picker supports search and paging. Large exports require explicit
confirmation so a mistaken button press does not start a long Telegram history
scan.

Text exports are sent by the manager bot as small `.txt` documents. Media
archives are created and uploaded by the Telegram user account into the private
chat with the manager bot, so large parts do not go through cloud Bot API upload
limits. Non-Premium accounts use archive parts up to about 1.5 GB; Premium
accounts use parts up to about 3.5 GB.

Media export is streamed through temporary files: downloaded source media is
packed into the current zip part and removed, each completed zip part is sent,
then that zip file is removed from the server. While one part is uploading, the
next part can continue forming.

## Posts To Topics

Telepath can copy posts from selected Telegram channels or groups into topics
inside one configured Telegram forum group. Each source gets its own topic. If a
source is added from the manager panel and no topic is configured yet, Telepath
creates the topic through the Telegram user account, stores the topic root
message id, and enables that source.

New posts are accepted into the mirror outbox in realtime without the autolike
delay. During gated delivery, protected-source cases are handled by copying
content as new messages: Telepath downloads media to a temporary directory,
sends the text/media/album to the configured topic, then removes the temporary
files. History backfill uses the same outbox and copy path, so it can safely
coexist with realtime mirroring.

Delivery is gated to avoid the assistant making the owner account look online:
realtime and history workers accept posts into the local SQLite outbox, while a
separate sender drains that outbox only when a non-current Telegram
authorization for the same account was active recently. The current Telepath
session never counts as owner activity. Missing topics are created by that
sender after the gate opens, and queued deliveries are paced by
`POST_MIRROR_DELIVERY_DELAY_MIN_SECONDS` /
`POST_MIRROR_DELIVERY_DELAY_MAX_SECONDS` instead of being burst-sent. Each
drain normally aims to clear ready posts within about two minutes; larger
backlogs can use up to `POST_MIRROR_ONLINE_DELIVERY_WINDOW_SECONDS` so delivery
keeps spacing between posts without stretching the online session indefinitely.
The Telepath session is marked offline again after each drain attempt.

## Running Without Docker

Use Python 3.12+.

```bash
# with uv
uv venv
uv pip install -e ".[dev]"

# or with stock Python
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
telepath-auth
telepath
```

You can also split the workers:

```bash
telepath-user
telepath-manager
```

## Project Layout

```text
telepath/
├── app.py                         # starts user client and manager bot
├── auth.py                        # Telegram user authorization CLI
├── chat_export.py                 # text export of Telegram chat history
├── config.py                      # environment loading and validation
├── manager_bot.py                 # aiogram owner-only bot
├── manager.py                     # state-changing manager service
├── panel.py                       # manager panel views and actions
├── runtime.py                     # narrow runtime ports for features
├── storage.py                     # SQLite repository
├── user_client.py                 # Telethon user client and queues
├── features/
│   ├── channel_reactions.py       # channel autolike domain logic
│   └── voice_transcription.py     # voice transcription feature
├── llm/
│   ├── anthropic_api.py
│   ├── copilot_cli.py
│   └── openai_api.py
├── premium_emoji.py
├── profanity.py
├── prompts.py
└── session_paths.py
```

## Tests

```bash
pytest -q
```

The test suite is pure Python. Telegram, Bot API, and LLM calls are mocked
through narrow ports, so tests do not require real credentials.

Coverage settings live in `pyproject.toml`:

```bash
coverage run -m pytest
coverage report
```

## Security And Privacy Notes

- Keep `.env`, `.session`, and SQLite database files private.
- The public repository ignores runtime state and session files.
- The setup script writes credentials only to your local `.env`.
- Telepath stores operational state in SQLite; it does not send chat history to
  a backend service.
- Voice polishing sends transcript text to the LLM provider you configure.
- Chat exports are explicit owner actions.
- Telegram account automation can violate expectations if used carelessly. Keep
  scopes narrow and use the manager panel kill-switches when testing.

## Design Boundary

Telepath intentionally separates account access from configuration:

- Telethon user client: reads Telegram data and performs account actions.
- Manager bot: owner-only control surface.
- Feature modules: pure behavior around narrow ports.
- SQLite repository: local state and idempotency.

That boundary keeps the system testable and makes it easier to add future
features without coupling them directly to Telegram clients or LLM SDKs.

## License

MIT. See [LICENSE](LICENSE).
