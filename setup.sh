#!/usr/bin/env bash
# wiki-js-mcp-server — Local setup (Mac/Linux, stdio mode)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "📦 wiki-js-mcp-server setup"
echo ""

# Python version check
if ! command -v python3 &>/dev/null; then
    echo "❌ python3 not found. Install via: brew install python"
    exit 1
fi

PYTHON_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')

if [ "$PYTHON_MAJOR" -lt 3 ] || [ "$PYTHON_MINOR" -lt 11 ]; then
    echo "❌ Python 3.11+ required (found 3.$PYTHON_MINOR)"
    echo "   Install via: brew install python@3.12"
    exit 1
fi

echo "✅ Python $(python3 --version) detected"

# Virtual environment
if [ ! -d "venv" ]; then
    echo "🔧 Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate

echo "📥 Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "✅ Dependencies installed"

# .env setup
if [ ! -f ".env" ]; then
    cp config/example.env .env
    echo ""
    echo "⚠️  .env created from template. You must edit it before use:"
    echo ""
    echo "     WIKIJS_URL=https://your-wiki.example.com"
    echo "     WIKIJS_API_KEY=your_api_key_here"
    echo ""
    echo "     Also set absolute paths for LOG_FILE and WIKIJS_MCP_DB."
    echo "     See README.md for details."
    echo ""
else
    echo "✅ .env already exists"
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit .env with your Wiki.js URL and API key"
echo "  2. Configure Claude Desktop — see README.md"
echo "  3. Restart Claude Desktop and look for the green dot"
