#!/usr/bin/env bash
# Telepath one-shot setup. Idempotent: safe to re-run.
#
# What it does:
#   1. Verifies docker + docker compose v2 are present.
#   2. Walks you through .env (Telegram + LLM provider).
#   3. Builds the docker image.
#   4. Runs interactive Telegram auth inside the container and captures TG_OWNER_ID.
#   5. Brings the stack up with `docker compose up -d`.
#
# Re-run any time. Existing .env values are kept unless you ask to overwrite them.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/.env.example"
DATA_DIR="$REPO_ROOT/data"

# --- pretty output ---------------------------------------------------------
if [ -t 1 ]; then
    C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_RESET=$'\033[0m'
else
    C_BOLD=""; C_DIM=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }
ok()   { printf '  %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
err()  { printf '  %s✗%s %s\n' "$C_RED" "$C_RESET" "$*" 1>&2; }
die()  { err "$*"; exit 1; }

# --- prereq check ----------------------------------------------------------
step "[1/5] Checking prerequisites"

command -v docker >/dev/null 2>&1 || die "docker not found. Install Docker Desktop or Docker Engine and retry."
ok "docker"

if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
    warn "Using legacy docker-compose v1. v2 (docker compose) is preferred."
else
    die "docker compose not found. Install Docker Desktop (bundled v2) or the compose plugin."
fi
ok "$DC"

if ! docker info >/dev/null 2>&1; then
    die "docker daemon is not reachable. Start Docker and re-run."
fi
ok "docker daemon reachable"

# --- helpers ---------------------------------------------------------------
# Read a value from .env if present; print empty string otherwise.
env_get() {
    local key="$1"
    [ -f "$ENV_FILE" ] || { printf ''; return; }
    awk -F= -v k="$key" '$1==k {sub(/^[^=]+=/,""); print; exit}' "$ENV_FILE"
}

# Upsert key=value in .env (preserves other keys; creates file if absent).
env_set() {
    local key="$1" value="$2"
    local tmp
    tmp="$(mktemp "$ENV_FILE.XXXXXX")"
    if [ -f "$ENV_FILE" ]; then
        awk -F= -v k="$key" -v v="$value" '
            BEGIN { written=0 }
            $1==k { print k "=" v; written=1; next }
            { print }
            END { if (!written) print k "=" v }
        ' "$ENV_FILE" >"$tmp"
    else
        printf '%s=%s\n' "$key" "$value" >"$tmp"
    fi
    mv "$tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

ask() {
    # ask <prompt> <default>  — writes user input to REPLY
    local prompt="$1" default="${2:-}"
    if [ -n "$default" ]; then
        printf '  %s [%s]: ' "$prompt" "$default"
    else
        printf '  %s: ' "$prompt"
    fi
    IFS= read -r REPLY || REPLY=""
    if [ -z "$REPLY" ] && [ -n "$default" ]; then
        REPLY="$default"
    fi
}

ask_secret() {
    local prompt="$1"
    printf '  %s: ' "$prompt"
    stty -echo 2>/dev/null || true
    IFS= read -r REPLY || REPLY=""
    stty echo 2>/dev/null || true
    printf '\n'
}

# --- step 2: telegram creds ------------------------------------------------
step "[2/5] Telegram credentials"
say "  ${C_DIM}Get TG_API_ID and TG_API_HASH at https://my.telegram.org/apps.${C_RESET}"
say "  ${C_DIM}Create a manager bot at @BotFather and copy its token.${C_RESET}"

cur_api_id="$(env_get TG_API_ID)"
cur_api_hash="$(env_get TG_API_HASH)"
cur_bot_token="$(env_get TG_MANAGER_BOT_TOKEN)"

while :; do
    ask "TG_API_ID" "$cur_api_id"
    case "$REPLY" in
        ''|0|*[!0-9]*) err "TG_API_ID must be a positive integer.";;
        *) TG_API_ID="$REPLY"; break;;
    esac
done

while :; do
    ask "TG_API_HASH" "$cur_api_hash"
    if printf '%s' "$REPLY" | grep -qE '^[a-fA-F0-9]{32}$'; then
        TG_API_HASH="$REPLY"
        break
    fi
    err "TG_API_HASH must be 32 hex characters (as shown on my.telegram.org/apps)."
done

while :; do
    ask "TG_MANAGER_BOT_TOKEN" "$cur_bot_token"
    if [ -n "$REPLY" ] && printf '%s' "$REPLY" | grep -qE '^[0-9]+:[A-Za-z0-9_-]+$'; then
        TG_MANAGER_BOT_TOKEN="$REPLY"
        break
    fi
    err "TG_MANAGER_BOT_TOKEN must look like 1234567:AAAA... (digits, colon, opaque token)."
done

# --- step 3: LLM provider --------------------------------------------------
step "[3/5] LLM provider"
cur_provider="$(env_get LLM_PROVIDER)"
[ -n "$cur_provider" ] || cur_provider="openai"

say "  1) openai     — recommended. Needs OPENAI_API_KEY."
say "  2) anthropic  — Claude. Needs ANTHROPIC_API_KEY."
say "  3) copilot    — GitHub Copilot CLI. Only for host installs (not this Docker image)."

case "$cur_provider" in
    openai) default_choice="1";;
    anthropic) default_choice="2";;
    copilot) default_choice="3";;
    *) default_choice="1";;
esac

while :; do
    ask "Provider [1/2/3]" "$default_choice"
    case "$REPLY" in
        1) LLM_PROVIDER="openai"; break;;
        2) LLM_PROVIDER="anthropic"; break;;
        3) LLM_PROVIDER="copilot"; break;;
        *) err "Pick 1, 2, or 3.";;
    esac
done

OPENAI_API_KEY=""
OPENAI_MODEL=""
ANTHROPIC_API_KEY=""
ANTHROPIC_MODEL=""

case "$LLM_PROVIDER" in
    openai)
        cur_key="$(env_get OPENAI_API_KEY)"
        cur_model="$(env_get OPENAI_MODEL)"
        if [ -n "$cur_key" ]; then
            ask "Reuse existing OPENAI_API_KEY (Y/n)" "y"
            case "$REPLY" in y|Y) OPENAI_API_KEY="$cur_key";; esac
        fi
        if [ -z "$OPENAI_API_KEY" ]; then
            while :; do
                ask_secret "OPENAI_API_KEY"
                [ -n "$REPLY" ] && { OPENAI_API_KEY="$REPLY"; break; }
                err "OPENAI_API_KEY is required for provider=openai."
            done
        fi
        ask "OPENAI_MODEL" "${cur_model:-gpt-4o-mini}"
        OPENAI_MODEL="$REPLY"
        ;;
    anthropic)
        cur_key="$(env_get ANTHROPIC_API_KEY)"
        cur_model="$(env_get ANTHROPIC_MODEL)"
        if [ -n "$cur_key" ]; then
            ask "Reuse existing ANTHROPIC_API_KEY (Y/n)" "y"
            case "$REPLY" in y|Y) ANTHROPIC_API_KEY="$cur_key";; esac
        fi
        if [ -z "$ANTHROPIC_API_KEY" ]; then
            while :; do
                ask_secret "ANTHROPIC_API_KEY"
                [ -n "$REPLY" ] && { ANTHROPIC_API_KEY="$REPLY"; break; }
                err "ANTHROPIC_API_KEY is required for provider=anthropic."
            done
        fi
        ask "ANTHROPIC_MODEL" "${cur_model:-claude-haiku-4-5-20251001}"
        ANTHROPIC_MODEL="$REPLY"
        ;;
    copilot)
        warn "Copilot CLI is NOT bundled in the Docker image."
        warn "Either install \`copilot\` on the host and run telepath without Docker,"
        warn "or extend Dockerfile to install \`@github/copilot\` plus an auth volume."
        ;;
esac

# --- step 4: write .env ----------------------------------------------------
step "[4/5] Writing .env"

if [ -f "$ENV_FILE" ]; then
    cp -p "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
    ok "Backed up existing .env"
fi

# Seed .env with example defaults the first time, then upsert our values.
if [ ! -f "$ENV_FILE" ] && [ -f "$ENV_EXAMPLE" ]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
fi

env_set TG_API_ID "$TG_API_ID"
env_set TG_API_HASH "$TG_API_HASH"
env_set TG_MANAGER_BOT_TOKEN "$TG_MANAGER_BOT_TOKEN"
# Seed default paths if absent (.env.example seeding above usually covers this).
[ -n "$(env_get TG_SESSION)" ] || env_set TG_SESSION "data/telepath"
[ -n "$(env_get TG_ASSISTANT_DB)" ] || env_set TG_ASSISTANT_DB "data/assistant.sqlite3"
env_set LLM_PROVIDER "$LLM_PROVIDER"

case "$LLM_PROVIDER" in
    openai)
        env_set OPENAI_API_KEY "$OPENAI_API_KEY"
        [ -n "$OPENAI_MODEL" ] && env_set OPENAI_MODEL "$OPENAI_MODEL"
        ;;
    anthropic)
        env_set ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY"
        [ -n "$ANTHROPIC_MODEL" ] && env_set ANTHROPIC_MODEL "$ANTHROPIC_MODEL"
        ;;
esac

# TG_OWNER_ID may be filled later by the auth step. Reserve a placeholder so
# docker compose's env_file does not error on missing vars during build.
[ -n "$(env_get TG_OWNER_ID)" ] || env_set TG_OWNER_ID "0"

ok ".env written ($(stat -f '%Sp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE"))"

# --- step 5: build + auth + up --------------------------------------------
step "[5/5] Building image"
mkdir -p "$DATA_DIR"
$DC build

# Auth: skip if a Telegram session already exists AND TG_OWNER_ID is set.
session_name="$(env_get TG_SESSION)"
session_path="$REPO_ROOT/${session_name}.session"
current_owner="$(env_get TG_OWNER_ID)"

# `telepath-auth` writes the resolved id to <session_dir>/.owner_id; that lives
# inside the bind-mounted ./data directory, so the host script can read it
# without parsing the interactive auth stdout.
owner_id_file="$REPO_ROOT/$(dirname "$session_name")/.owner_id"

if [ -f "$session_path" ] && [ -n "$current_owner" ] && [ "$current_owner" != "0" ]; then
    ok "Existing Telegram session found at ${session_name}.session — skipping auth"
    ok "TG_OWNER_ID=$current_owner"
else
    step "Authorizing Telegram user account"
    say "  You'll be asked for:"
    say "    • phone number in international format (e.g. +1234567890)"
    say "    • login code from Telegram"
    say "    • optional 2FA password"
    printf '  Press Enter when ready. '
    IFS= read -r _ || true

    # Stale id file from a previous attempt would mislead the post-check below.
    rm -f "$owner_id_file"

    # Run interactively — output goes straight to the user's terminal, so the
    # Telethon prompts (phone / code / 2FA) appear without any pipe buffering.
    if ! $DC run --rm telepath telepath-auth; then
        die "Telegram authorization failed. Fix the issue and re-run ./scripts/setup.sh"
    fi

    if [ -f "$owner_id_file" ]; then
        owner_id="$(tr -d '[:space:]' <"$owner_id_file")"
    else
        owner_id=""
    fi

    if [ -z "$owner_id" ] || ! printf '%s' "$owner_id" | grep -qE '^[1-9][0-9]*$'; then
        warn "Could not read TG_OWNER_ID from $owner_id_file."
        while :; do
            ask "Paste your Telegram numeric user ID" ""
            case "$REPLY" in
                ''|0|*[!0-9]*) err "Must be a positive integer.";;
                *) owner_id="$REPLY"; break;;
            esac
        done
    fi
    env_set TG_OWNER_ID "$owner_id"
    ok "TG_OWNER_ID=$owner_id saved to .env"
fi

step "Starting Telepath"
$DC up -d
ok "Container started"

cat <<EOF

${C_GREEN}${C_BOLD}Done.${C_RESET} Open Telegram and send /start to your manager bot.

Useful commands:
  Tail logs:   $DC logs -f telepath
  Stop:        $DC down
  Restart:     $DC restart telepath
  Re-auth:     ./scripts/auth.sh
  Re-run setup (rotate keys, switch LLM): ./scripts/setup.sh

EOF
