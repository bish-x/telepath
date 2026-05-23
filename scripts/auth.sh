#!/usr/bin/env bash
# Re-authorize the Telegram user session inside the Telepath container.
# Use this if the session expired, was revoked, or you want to switch accounts.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env"

if [ ! -f "$ENV_FILE" ]; then
    printf 'No .env found at %s. Run ./scripts/setup.sh first.\n' "$ENV_FILE" 1>&2
    exit 1
fi

if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    printf 'docker compose not found. Install Docker Desktop or the compose plugin.\n' 1>&2
    exit 1
fi

env_get() {
    awk -F= -v k="$1" '$1==k {sub(/^[^=]+=/,""); print; exit}' "$ENV_FILE"
}

env_set() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp "$ENV_FILE.XXXXXX")"
    awk -F= -v k="$key" -v v="$value" '
        BEGIN { written=0 }
        $1==k { print k "=" v; written=1; next }
        { print }
        END { if (!written) print k "=" v }
    ' "$ENV_FILE" >"$tmp"
    mv "$tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

session_name="$(env_get TG_SESSION)"
[ -n "$session_name" ] || session_name="data/telepath"
session_path="$REPO_ROOT/${session_name}.session"
owner_id_file="$REPO_ROOT/$(dirname "$session_name")/.owner_id"

if [ -f "$session_path" ]; then
    printf 'Removing existing session file %s\n' "$session_path"
    rm -f "$session_path" "${session_path}-journal"
fi
rm -f "$owner_id_file"

# Stop running container so it does not race for the session DB.
$DC down --remove-orphans >/dev/null 2>&1 || true

# Run interactively so Telethon's phone/code/2FA prompts appear in the user's
# terminal without pipe buffering. `telepath-auth` writes the resolved owner id
# to .owner_id in the session directory; we read it from the host.
if ! $DC run --rm telepath telepath-auth; then
    printf 'Authorization failed.\n' 1>&2
    exit 1
fi

owner_id=""
if [ -f "$owner_id_file" ]; then
    owner_id="$(tr -d '[:space:]' <"$owner_id_file")"
fi

if printf '%s' "$owner_id" | grep -qE '^[1-9][0-9]*$'; then
    env_set TG_OWNER_ID "$owner_id"
    printf 'TG_OWNER_ID=%s saved to .env\n' "$owner_id"
else
    printf 'WARN: could not read a valid TG_OWNER_ID from %s.\n' "$owner_id_file" 1>&2
    printf 'WARN: .env still has the previous value — manager bot may reject the new account.\n' 1>&2
    printf 'WARN: open Telegram and ask @userinfobot for your numeric id, then set it in .env.\n' 1>&2
fi

$DC up -d
printf 'Telepath restarted.\n'
