#!/usr/bin/env sh
# Start Copilot (macOS, Linux, WSL). Everything else is set up on the web page that opens.
set -e
cd "$(dirname "$0")"
if ! command -v uv >/dev/null 2>&1; then
  echo "Can cai uv truoc: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi
uv sync --quiet
exec uv run actoroffice serve "$@"
