#!/usr/bin/env bash
# Opens the AgentVoice dashboard, starting the server first if it isn't running.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/.venv/bin/agentvoice"
ENV_FILE="$ROOT/.env"
PORT="$(grep -E '^PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 || true)"
PORT="${PORT:-8080}"
URL="http://localhost:$PORT"

if ! curl -fsS "$URL/api/health" >/dev/null 2>&1; then
    if systemctl --user is-enabled agentvoice.service >/dev/null 2>&1; then
        systemctl --user start agentvoice.service
    else
        mkdir -p "$HOME/.local/state"
        nohup "$BIN" --env "$ENV_FILE" serve >"$HOME/.local/state/agentvoice.log" 2>&1 &
    fi
    for _ in $(seq 1 30); do
        curl -fsS "$URL/api/health" >/dev/null 2>&1 && break
        sleep 0.5
    done
fi

TOKEN="$("$BIN" --env "$ENV_FILE" token)"
xdg-open "$URL/#token=$TOKEN" >/dev/null 2>&1 &
