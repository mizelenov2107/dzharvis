#!/bin/bash
# Поднимает сервер «Джарвис» (для launchd при входе в систему).
# Homebrew при обновлениях уносит python3, поэтому берём первый живой интерпретатор.
KOREN="$(cd "$(dirname "$0")" && pwd)"
for p in "$KOREN/.venv/bin/python" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$p" ] && exec "$p" "$KOREN/server.py"
done
echo "нет python3" >&2; exit 1
