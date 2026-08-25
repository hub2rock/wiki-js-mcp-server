# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP (Model Context Protocol) server that exposes a Wiki.js instance to MCP clients (Claude Desktop, etc.) as ~24 tools. The entire server — settings, GraphQL client, SQLite models, all tools, and both transport entrypoints — lives in a single file: `src/server.py` (~1400 lines). There are no other Python modules.

## Commands

```bash
# First-time local setup (creates venv, installs deps, copies config/example.env → .env)
./setup.sh

# Run locally in stdio mode (what Claude Desktop spawns)
./start.sh                        # wrapper: activates venv, runs server --stdio
python src/server.py --stdio      # direct
python src/server.py --http       # HTTP/SSE mode (used by Docker)

# Transport is chosen by CLI flag first, then MCP_TRANSPORT env var (default: stdio)

# Docker (HTTP mode) — pulls hub2rock/wiki-js-mcp-server:latest, no local build
docker compose up -d
docker compose logs -f

# Build the image locally
docker build -t wiki-js-mcp-server .
```

There is **no test suite, linter, or formatter configured** — do not assume `pytest`/`ruff`/etc. exist. Verify changes by running the server and exercising tools against a live Wiki.js instance. `wikijs_connection_status` is the fastest smoke test.

CI (`.github/workflows/docker-publish.yml`) only builds and pushes the Docker image on push to `main`; it runs no tests.

## Configuration

All config comes from environment / `.env` via `Settings(BaseSettings)` at the top of `server.py`. `config/example.env` is the template. Key vars: `WIKIJS_URL`, `WIKIJS_API_KEY` (Full Access), `MCP_TRANSPORT`, `WIKIJS_MCP_DB`, `LOG_FILE`. Multi-user connector mode adds `MCP_AUTH_MODE`, `MCP_PUBLIC_URL`, `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, and `MCP_ENCRYPTION_KEY`.

Note `model_config = ConfigDict(extra="ignore")`: a misspelled variable is silently dropped and the default applies — the classic symptom is the server querying `localhost:3000` because someone set `WIKIJS_API_URL` instead of `WIKIJS_URL`.

**Critical for stdio mode:** Claude Desktop launches the process from `/`, so `WIKIJS_MCP_DB` and `LOG_FILE` MUST be absolute paths or the server fails on startup (read-only filesystem / can't create SQLite DB).

## Architecture

Three layers, all in `server.py`:

1. **`WikiJSClient`** — async httpx client used as `async with WikiJSClient() as c:`. Its `.query(gql, variables)` method is the single chokepoint for all Wiki.js access. It wraps httpx with `@retry` (tenacity, 2 attempts, exponential backoff, `reraise=True` so the real exception surfaces instead of `RetryError`), a 120s timeout, raises `GraphQL error: ...` when the response contains an `errors` array, and normalizes HTTP/connection errors. Every tool talks to Wiki.js through this method — never call httpx directly in a tool.

   **The API key is per-call, not global.** `WikiJSClient()` resolves its key through `resolve_api_key()`: in single-user mode that is `settings.WIKIJS_API_KEY`; in oauth mode it is the calling user's own key, looked up from `UserKey` by the `sub` in `_current_sub`. Never reintroduce a module-level `settings.headers` — that would make every user share one credential. Pass `WikiJSClient(api_key=...)` only to validate a key before storing it.

2. **SQLite DB** — two SQLAlchemy models, created/migrated on import via `_init_db()`:
   - `FileMapping` — links local source files to wiki pages. Scoped per user by `owner_sub` with `UNIQUE(owner_sub, file_path)`; **always query it through `owned_mappings(db)`**, never `db.query(FileMapping)` directly, or one user's mappings leak into another's. Powers the "File↔Page Sync" tool group.
   - `UserKey` — one Wiki.js API key per OAuth subject, Fernet-encrypted when `MCP_ENCRYPTION_KEY` is set. Access via `store_user_key()` / `load_user_key()`.

   `_init_db()` migrates pre-2.0 databases (which had `UNIQUE(file_path)` and no `owner_sub`) by rebuilding the table and assigning existing rows to `_SINGLE_USER_OWNER`. It is idempotent. Use `get_db()` for sessions. This DB is the only local state; Wiki.js itself is the source of truth for page content.

3. **Tools** — each is an `async def` decorated with `@mcp.tool()` on the module-level `mcp = FastMCP("wiki-js-mcp-server")`. Tools return **strings** (usually `json.dumps(...)`), not objects. They are grouped in the file: connection, core page CRUD, hierarchy/scaffolding, spaces, deletion/cleanup, and file↔page sync.

Entrypoint: `main()` reads the transport (CLI `--http`/`--stdio` overrides `MCP_TRANSPORT`), then runs `run_http()` or `run_stdio()` (`mcp.run_stdio_async()`). Both call `settings.validate_config()` first.

`run_http()` serves `build_http_app()`, which wraps `mcp.http_app()` in a parent Starlette app adding `/healthz`, the RFC 9728 discovery routes, and `OAuthResourceServerMiddleware`. Two things there are load-bearing:
- The wrapper **must** re-run FastMCP's lifespan (`mcp_app.router.lifespan_context`) or the MCP endpoint 500s on first use — its session manager starts there.
- The middleware is **pure ASGI, not `BaseHTTPMiddleware`**, so the identity ContextVars are set in the same task that runs the MCP app. `BaseHTTPMiddleware` runs the downstream app in a child task, which makes context propagation subtle.

### Auth modes

`MCP_AUTH_MODE=none` (default) keeps the pre-2.0 behaviour: one shared `WIKIJS_API_KEY`, no identity. `MCP_AUTH_MODE=oauth` turns the server into an OAuth 2.1 resource server for multi-user connector deployments: bearer tokens are verified against the issuer's JWKS (discovered from its OIDC config and cached), and `_current_sub` / `_current_claims` carry the caller's identity for the request.

When adding a tool that touches Wiki.js or file mappings, it needs no auth code — `WikiJSClient()` and `owned_mappings()` already resolve the caller. Tools that are meaningless without an identity should call `current_owner()`, which raises `AuthError` in that case. Anything cwd-dependent (`find_repo_root()`) is meaningless in remote multi-user mode — see how `wikijs_repository_context` branches on `settings.oauth_enabled`.

### Conventions to preserve when adding/editing tools

- Page paths are always derived with `slugify(title)` — reuse it so paths stay consistent with existing hierarchy logic.
- Large pages are a real constraint: Wiki.js pages with inline base64 images can blow past the MCP response size limit. `wikijs_get_page` supports `include_content=False` and `max_content_chars` (default 800000, truncates with a warning); `wikijs_get_page_metadata` returns size info only. Preserve these escape hatches for any content-returning tool.
- `extract_code_structure()` uses Python `ast` and refuses files over `_MAX_FILE_SIZE_BYTES` (5 MB) — it's Python-only, used by `wikijs_generate_file_overview`.
- All Wiki.js reads/writes are GraphQL against `settings.graphql_url` (`WIKIJS_URL` + `WIKIJS_GRAPHQL_ENDPOINT`). Target is Wiki.js **2.x** schema.
- **Locale is never hard-coded.** Any tool that reads/creates/moves a page must resolve its locale via `await resolve_default_locale(c, locale)` (precedence: explicit arg → `WIKIJS_DEFAULT_LOCALE` → the instance's `localization.config.locale`, queried once and cached in `_default_locale_cache` → `en`). Tools take `locale: str = None` and pass it through; a hard-coded `"en"` would create/read pages in the wrong locale on non-English wikis. `wikijs_update_page` is the exception — it reuses the fetched page's own locale.

## Notes / known inconsistencies

- Tool count and FastMCP version drift across docs: README says 24 tools / FastMCP 3.x, `pyproject.toml` says 23 tools / `fastmcp>=2.0.0`, recent commits pin behavior to FastMCP 3.2.4 (e.g. `http_app` replaced the old `streamable_http_app`). Trust the actual `@mcp.tool()` decorators in `server.py` for the real tool set, and the installed FastMCP for API surface.
- `wikijs_mappings.db` and `wikijs_mcp.log` in the repo root are runtime artifacts, not source.
