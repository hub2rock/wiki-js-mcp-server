# Wiki.js MCP Server

> A comprehensive **Model Context Protocol (MCP) server** for Wiki.js — 23 tools, dual transport (stdio + HTTP/SSE), built for production self-hosted infrastructure.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/FastMCP-3.x-green)](https://gofastmcp.com)
[![Wiki.js](https://img.shields.io/badge/Wiki.js-2.x-blueviolet)](https://js.wiki/)
[![Docker](https://img.shields.io/badge/Docker-hub2rock%2Fwiki--js--mcp--server-blue?logo=docker)](https://hub.docker.com/r/hub2rock/wiki-js-mcp-server)

Connect **Claude Desktop** or any MCP-compatible client directly to your Wiki.js instance. Read pages, create structured documentation, sync code changes, manage hierarchies — all through natural language.

---

## 🎯 What This Does

Once connected, you can ask Claude to:

- 🔍 **Search** your wiki for any topic
- 📖 **Read** any page by path or ID
- ✏️ **Create & update** documentation
- 🗂️ **Organize** pages by moving them around
- 🏗️ **Scaffold** full repo documentation structures
- 🔗 **Sync** source code changes to linked wiki pages
- 🗑️ **Clean up** outdated pages and hierarchies

---

## 🚀 Quick Start

### Prerequisites

- Python **3.11+** (3.12 recommended)
- A running [Wiki.js](https://js.wiki/) instance (v2.x)
- A Wiki.js **API key** with Full Access (see [Getting an API key](#-getting-a-wikijs-api-key))

### 1. Clone & setup

```bash
git clone https://github.com/hub2rock/wiki-js-mcp-server.git
cd wiki-js-mcp-server
chmod +x setup.sh start.sh
./setup.sh
```

The setup script creates a Python virtual environment and installs all dependencies.

### 2. Configure

Edit `.env` at the project root:

```env
WIKIJS_URL=https://your-wiki.example.com
WIKIJS_API_KEY=your_api_key_here
```

> ⚠️ **Important for Claude Desktop (stdio mode):** use **absolute paths** for `WIKIJS_MCP_DB` and `LOG_FILE`. Claude Desktop launches the process from `/`, so relative paths will fail.

```env
WIKIJS_MCP_DB=/absolute/path/to/wiki-js-mcp-server/wikijs_mappings.db
LOG_FILE=/absolute/path/to/wiki-js-mcp-server/wikijs_mcp.log
```

### 3. Configure Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "wikijs": {
      "command": "/absolute/path/to/wiki-js-mcp-server/venv/bin/python",
      "args": ["/absolute/path/to/wiki-js-mcp-server/src/server.py", "--stdio"]
    }
  }
}
```

Restart Claude Desktop → **Settings → Developer** → you should see `wikijs` with a 🟢 green dot and 23 tools listed.

### 4. Verify the connection

Ask Claude: *"Check my Wiki.js connection status"* or *"List all pages in my wiki"*.

---

## 📊 MCP Tools (23 Total)

### 🔧 Connection

| Tool | Description |
|------|-------------|
| `wikijs_connection_status` | Check connection & authentication health |

### 📝 Core Page Management

| Tool | Description |
|------|-------------|
| `wikijs_create_page` | Create a new page with optional path or parent |
| `wikijs_get_page` | Retrieve a page by ID or path |
| `wikijs_update_page` | Update content, title, description, or tags |
| `wikijs_delete_page` | Delete a page by ID or path |
| `wikijs_move_page` | Move a page to a new path or locale |
| `wikijs_search_pages` | Full-text search with fallback to list filter |
| `wikijs_list_pages` | List all pages with metadata |
| `wikijs_get_tree` | Get the full page tree structure |

### 🏗️ Hierarchical Documentation

| Tool | Description |
|------|-------------|
| `wikijs_get_page_children` | List direct children of a page |
| `wikijs_create_nested_page` | Create a page under a path, auto-creates parents |
| `wikijs_create_repo_structure` | Scaffold a complete repository documentation structure |
| `wikijs_create_documentation_hierarchy` | Auto-organize project files into categorized docs |

### 🗂️ Spaces & Organization

| Tool | Description |
|------|-------------|
| `wikijs_list_spaces` | List top-level documentation spaces |
| `wikijs_create_space` | Create a new top-level space |

### 🗑️ Deletion & Cleanup

| Tool | Description |
|------|-------------|
| `wikijs_batch_delete_pages` | Delete multiple pages by IDs, paths, or glob pattern |
| `wikijs_delete_hierarchy` | Delete an entire page hierarchy |

### 🔗 File↔Page Sync (Code/Doc Integration)

| Tool | Description |
|------|-------------|
| `wikijs_link_file_to_page` | Persist a link between a source file and a wiki page |
| `wikijs_sync_file_docs` | Append a change note to a file's linked wiki page |
| `wikijs_generate_file_overview` | Auto-generate documentation for a Python source file |
| `wikijs_bulk_update_project_docs` | Batch sync multiple changed files to their wiki pages |
| `wikijs_cleanup_orphaned_mappings` | Remove mappings to deleted wiki pages |
| `wikijs_repository_context` | Show current repo context and active mappings |

---

## 🐳 Docker Deployment (Remote HTTP Mode)

The Docker image is published on Docker Hub — no build required.

### 1. Deploy on your server

```bash
mkdir wiki-js-mcp && cd wiki-js-mcp
curl -O https://raw.githubusercontent.com/hub2rock/wiki-js-mcp-server/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/hub2rock/wiki-js-mcp-server/main/config/example.env
cp example.env .env
nano .env
docker compose up -d
docker compose logs -f
```

`.env` for Docker mode:

```env
WIKIJS_URL=http://your-wiki-internal-ip:8090
WIKIJS_API_KEY=your_api_key_here
MCP_TRANSPORT=http
HTTP_HOST=0.0.0.0
HTTP_PORT=8000
WIKIJS_MCP_DB=/app/data/wikijs_mappings.db
LOG_FILE=/app/data/wikijs_mcp.log
```

### 2. Nginx reverse proxy

```nginx
server {
    listen 443 ssl;
    server_name mcp-wiki.your-domain.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    auth_basic "Wiki MCP";
    auth_basic_user_file /etc/nginx/.htpasswd-mcp;

    location / {
        proxy_pass http://your-server:8000;
        proxy_http_version 1.1;

        # Required for SSE / streaming
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;
        proxy_read_timeout 3600s;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

```bash
htpasswd -c /etc/nginx/.htpasswd-mcp your-user
nginx -t && systemctl reload nginx
```

### 3. Claude Desktop config (HTTP mode)

```json
{
  "mcpServers": {
    "wikijs": {
      "type": "http",
      "url": "https://mcp-wiki.your-domain.com/mcp",
      "headers": {
        "Authorization": "Basic <base64(user:password)>"
      }
    }
  }
}
```

Generate the base64 value:
```bash
echo -n "user:password" | base64
```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WIKIJS_URL` | `http://localhost:3000` | Wiki.js base URL |
| `WIKIJS_API_KEY` | — | API key (Full Access) |
| `WIKIJS_GRAPHQL_ENDPOINT` | `/graphql` | GraphQL endpoint path |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `HTTP_HOST` | `0.0.0.0` | HTTP bind address |
| `HTTP_PORT` | `8000` | HTTP listen port |
| `WIKIJS_MCP_DB` | `./wikijs_mappings.db` | SQLite DB for file↔page mappings |
| `LOG_FILE` | `./wikijs_mcp.log` | Log file path (**use absolute path in stdio mode**) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `DEFAULT_SPACE_NAME` | `Documentation` | Default space name for new structures |

---

## 🔑 Getting a Wiki.js API Key

1. Log into your Wiki.js instance as an administrator
2. Navigate to **Administration → API Access**
3. Enable the API if not already enabled
4. Click **New Key**
5. Give it a name (e.g. `mcp-server`) and set permissions to **Full Access**
6. Copy the key immediately — it won't be shown again

---

## 🔍 Troubleshooting

### Claude Desktop: `Operation not permitted`
The shell script can't be executed due to macOS Gatekeeper. Use the Python binary directly instead:
```json
"command": "/path/to/venv/bin/python",
"args": ["/path/to/src/server.py", "--stdio"]
```

### `Read-only file system` error on startup
You're using relative paths for `LOG_FILE` or `WIKIJS_MCP_DB`. Claude Desktop launches from `/`. Use absolute paths in `.env`.

### `ModuleNotFoundError: No module named 'slugify'`
The venv may have been created with the wrong Python version. Recreate it:
```bash
rm -rf venv
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -c "from slugify import slugify; print('OK')"
```

### Connection or authentication errors
- Verify your `WIKIJS_URL` has no trailing slash
- Ensure the API is enabled in Wiki.js Administration
- Check that the API key has Full Access permissions
- Run `wikijs_connection_status` from Claude to get a detailed status

---

## 📁 Project Structure

```
wiki-js-mcp-server/
├── src/
│   └── server.py              # MCP server — all 23 tools
├── config/
│   └── example.env            # Configuration template
├── Dockerfile                 # python:3.12-slim, non-root user
├── docker-compose.yml         # Pulls hub2rock/wiki-js-mcp-server:latest
├── setup.sh                   # Local setup script (Mac/Linux)
├── start.sh                   # stdio launcher for Claude Desktop
├── requirements.txt           # Python dependencies
├── pyproject.toml             # Package metadata
└── LICENSE                    # MIT
```

---

## 🏢 Example Workflows

### Documentation-first development

Before writing any code, ask Claude to check existing patterns:
```
Search my wiki for authentication patterns before I implement the login feature.
```

### Auto-scaffold a new project

```
Create a complete documentation structure for my project "infra-2rock"
with sections: Overview, Architecture, Networking, Security, Runbooks.
```

### Sync a code change to docs

```
I just refactored the Zabbix monitoring module. Sync the change to its wiki page
with this summary: "Migrated alert thresholds to external config file".
```

### Clean up after a project ends

```
Delete the entire hierarchy under "old-project" including its root page.
```

---

## 🛠️ Technical Stack

- **[FastMCP 3.x](https://gofastmcp.com)** — Python MCP SDK
- **[httpx](https://www.python-httpx.org/)** — Async HTTP client for GraphQL
- **[SQLAlchemy](https://www.sqlalchemy.org/)** — SQLite ORM for file↔page mappings
- **[Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)** — Environment configuration
- **[tenacity](https://tenacity.readthedocs.io/)** — Retry logic with exponential backoff
- **[uvicorn](https://www.uvicorn.org/)** — ASGI server for HTTP mode

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -m 'feat: add my feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request

---

## 🙏 Credits

Built on top of:
- **[talosdeus/wiki-js-mcp](https://github.com/talosdeus/wiki-js-mcp)** — hierarchical documentation tools, file↔page sync, SQLite mapping DB
- **[jaalbin24/wikijs-mcp-server](https://github.com/jaalbin24/wikijs-mcp-server)** — `move`, `list`, `tree` tools, HTTP/SSE transport architecture, Docker setup

---

## 📄 License

[MIT](LICENSE) — free to use, modify, and redistribute with attribution.
