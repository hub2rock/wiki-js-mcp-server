#!/usr/bin/env bash
# wiki-2rock-mcp — Start (stdio mode, Claude Desktop)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    echo "❌ venv not found. Run ./setup.sh first." >&2
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "❌ .env not found. Run ./setup.sh first." >&2
    exit 1
fi

source venv/bin/activate
exec python src/server.py --stdio
