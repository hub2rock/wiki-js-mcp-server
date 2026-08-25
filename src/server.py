#!/usr/bin/env python3
"""
wiki-js-mcp-server — Wiki.js MCP Server
Merger of talosdeus/wiki-js-mcp + jaalbin24/wikijs-mcp-server
Supports stdio (Claude Desktop local) and HTTP/SSE (Docker remote)
"""

import asyncio
import datetime
import fnmatch
import hashlib
import json
import logging
import os
import ast
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import Field, ConfigDict
from pydantic_settings import BaseSettings
from slugify import slugify
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from tenacity import before_sleep_log, retry, stop_after_attempt, wait_exponential

# PyJWT — only needed for MCP_AUTH_MODE=oauth (multi-user HTTP mode).
try:
    import jwt
    from jwt import PyJWKClient

    _JWT_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    _JWT_AVAILABLE = False

# Fernet — optional encryption-at-rest for stored per-user Wiki.js keys.
try:
    from cryptography.fernet import Fernet, InvalidToken

    _FERNET_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    _FERNET_AVAILABLE = False

load_dotenv()

__version__ = "2.0.0"

UTC = ZoneInfo("UTC")
_MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB — protection against huge files

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    model_config = ConfigDict(env_file=".env", extra="ignore")

    WIKIJS_URL: str = Field(default="http://localhost:3000")
    # Single-user fallback key. Used when MCP_AUTH_MODE=none (stdio / personal use).
    # In oauth mode each user registers their own key instead — see UserKey.
    WIKIJS_API_KEY: str = Field(default="")
    WIKIJS_GRAPHQL_ENDPOINT: str = Field(default="/graphql")

    # Transport
    MCP_TRANSPORT: str = Field(default="stdio")  # stdio | http
    HTTP_HOST: str = Field(default="0.0.0.0")
    HTTP_PORT: int = Field(default=8000)

    # ── Auth (multi-user connector mode) ────────────────────────────────────
    # none  → single shared WIKIJS_API_KEY (stdio, personal use)
    # oauth → OAuth 2.1 resource server; every caller brings their own
    #         Wiki.js API key, so Wiki.js permissions apply per user.
    MCP_AUTH_MODE: str = Field(default="none")

    # Public base URL this server is reachable at (no trailing slash).
    # Required in oauth mode: it is the `resource` identifier advertised in the
    # RFC 9728 protected-resource metadata that MCP clients discover.
    MCP_PUBLIC_URL: str = Field(default="")

    # OAuth issuer (Authentik application URL, e.g.
    # https://auth.example.com/application/o/wikijs-mcp/). JWKS is discovered
    # from its OIDC configuration unless OAUTH_JWKS_URL is set explicitly.
    OAUTH_ISSUER: str = Field(default="")
    OAUTH_JWKS_URL: str = Field(default="")
    OAUTH_AUDIENCE: str = Field(default="")
    OAUTH_ALGORITHMS: str = Field(default="RS256")
    # Space- or comma-separated scopes a token must carry. Empty = no check.
    OAUTH_REQUIRED_SCOPES: str = Field(default="")
    OAUTH_SUPPORTED_SCOPES: str = Field(default="openid profile email")

    # Fernet key (44-char urlsafe base64) encrypting stored per-user Wiki.js
    # keys at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    MCP_ENCRYPTION_KEY: str = Field(default="")

    # Local DB (file→page mappings)
    WIKIJS_MCP_DB: str = Field(default="./wikijs_mappings.db")

    # Content
    # Default content locale for created pages. Empty = auto-detect from the
    # Wiki.js instance's own default locale (queried once, cached).
    WIKIJS_DEFAULT_LOCALE: str = Field(default="")

    # Misc
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FILE: str = Field(default="wikijs_mcp.log")
    DEFAULT_SPACE_NAME: str = Field(default="Documentation")

    @property
    def graphql_url(self) -> str:
        return f"{self.WIKIJS_URL.rstrip('/')}{self.WIKIJS_GRAPHQL_ENDPOINT}"

    def headers_for(self, api_key: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    @property
    def oauth_enabled(self) -> bool:
        return self.MCP_AUTH_MODE.strip().lower() == "oauth"

    @property
    def algorithms(self) -> List[str]:
        return [a for a in self.OAUTH_ALGORITHMS.replace(",", " ").split() if a]

    @property
    def required_scopes(self) -> List[str]:
        return [s for s in self.OAUTH_REQUIRED_SCOPES.replace(",", " ").split() if s]

    @property
    def supported_scopes(self) -> List[str]:
        return [s for s in self.OAUTH_SUPPORTED_SCOPES.replace(",", " ").split() if s]

    @property
    def resource_identifier(self) -> str:
        """The canonical `resource` URI advertised to MCP clients (RFC 9728)."""
        return f"{self.MCP_PUBLIC_URL.rstrip('/')}/mcp"

    def validate_config(self) -> None:
        if not self.WIKIJS_URL:
            raise ValueError("WIKIJS_URL must be set.")

        if self.oauth_enabled:
            if not _JWT_AVAILABLE:
                raise ValueError(
                    "MCP_AUTH_MODE=oauth requires PyJWT with crypto extras. "
                    "Install it with: pip install 'pyjwt[crypto]'"
                )
            if not self.OAUTH_ISSUER:
                raise ValueError("OAUTH_ISSUER must be set when MCP_AUTH_MODE=oauth.")
            if not self.MCP_PUBLIC_URL:
                raise ValueError("MCP_PUBLIC_URL must be set when MCP_AUTH_MODE=oauth.")
            if self.MCP_ENCRYPTION_KEY and not _FERNET_AVAILABLE:
                raise ValueError(
                    "MCP_ENCRYPTION_KEY is set but the 'cryptography' package is missing."
                )
            if not self.MCP_ENCRYPTION_KEY:
                logger.warning(
                    "MCP_ENCRYPTION_KEY is not set — per-user Wiki.js API keys will be "
                    "stored in plaintext in %s. Set it to encrypt them at rest.",
                    self.WIKIJS_MCP_DB,
                )
        elif not self.WIKIJS_API_KEY:
            raise ValueError(
                "WIKIJS_API_KEY must be set (or set MCP_AUTH_MODE=oauth for per-user keys)."
            )


settings = Settings()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(settings.LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("wiki-js-mcp")

# ---------------------------------------------------------------------------
# Database (SQLite — file→page mappings)
# ---------------------------------------------------------------------------

Base = declarative_base()

# Owner value used when the server runs without OAuth (single-user stdio mode).
_SINGLE_USER_OWNER = "__local__"


class FileMapping(Base):
    __tablename__ = "file_mappings"
    __table_args__ = (UniqueConstraint("owner_sub", "file_path", name="uq_owner_file"),)

    id = Column(Integer, primary_key=True)
    # OAuth subject of the user who owns this mapping. Mappings are private to
    # their owner so two users can map the same path independently.
    owner_sub = Column(String, nullable=False, default=_SINGLE_USER_OWNER, index=True)
    file_path = Column(String, nullable=False)
    page_id = Column(Integer, nullable=False)
    relationship_type = Column(String, nullable=False)
    last_updated = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    file_hash = Column(String)
    repository_root = Column(String, default="")
    space_name = Column(String, default="")


class UserKey(Base):
    """A single user's Wiki.js API key, keyed by their OAuth subject.

    This is what makes the server multi-tenant: every Wiki.js call is made with
    the calling user's own key, so Wiki.js enforces that user's own permissions.
    """

    __tablename__ = "user_keys"
    id = Column(Integer, primary_key=True)
    owner_sub = Column(String, unique=True, nullable=False, index=True)
    # Encrypted when MCP_ENCRYPTION_KEY is configured (see is_encrypted).
    wikijs_api_key = Column(Text, nullable=False)
    is_encrypted = Column(Boolean, nullable=False, default=False)
    label = Column(String, default="")
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    last_used = Column(DateTime)


engine = create_engine(f"sqlite:///{settings.WIKIJS_MCP_DB}", connect_args={"check_same_thread": False})


def _legacy_file_mappings_schema(conn) -> bool:
    """True if file_mappings still carries the pre-multi-user UNIQUE(file_path)."""
    if not conn.execute(text("PRAGMA table_info(file_mappings)")).fetchall():
        return False  # table does not exist yet
    for idx in conn.execute(text("PRAGMA index_list(file_mappings)")).fetchall():
        name, is_unique = idx[1], idx[2]
        if not is_unique:
            continue
        cols = [r[2] for r in conn.execute(text(f'PRAGMA index_info("{name}")')).fetchall()]
        if cols == ["file_path"]:
            return True
    return False


def _init_db() -> None:
    """Create tables, migrating a pre-2.0 single-user database if present.

    Pre-2.0 file_mappings had UNIQUE(file_path) and no owner_sub. SQLite cannot
    drop a constraint in place, so the table is renamed, recreated by
    create_all(), and its rows copied back under the single-user owner.
    """
    rebuild = False
    with engine.connect() as conn:
        if _legacy_file_mappings_schema(conn):
            logger.info("Migrating file_mappings to the multi-user schema…")
            conn.execute(text("ALTER TABLE file_mappings RENAME TO file_mappings_legacy"))
            conn.commit()
            rebuild = True

    Base.metadata.create_all(engine)

    with engine.connect() as conn:
        if rebuild:
            conn.execute(
                text(
                    "INSERT INTO file_mappings "
                    "(owner_sub, file_path, page_id, relationship_type, last_updated, "
                    " file_hash, repository_root, space_name) "
                    "SELECT :owner, file_path, page_id, relationship_type, last_updated, "
                    "       file_hash, repository_root, space_name "
                    "FROM file_mappings_legacy"
                ),
                {"owner": _SINGLE_USER_OWNER},
            )
            conn.execute(text("DROP TABLE file_mappings_legacy"))
            conn.commit()
            logger.info("file_mappings migration complete.")
        else:
            # Non-legacy database that predates owner_sub (additive upgrade only).
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(file_mappings)")).fetchall()}
            if cols and "owner_sub" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE file_mappings ADD COLUMN owner_sub VARCHAR "
                        f"NOT NULL DEFAULT '{_SINGLE_USER_OWNER}'"
                    )
                )
                conn.commit()


_init_db()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def get_db():
    """Context manager for database sessions — always properly closed."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Per-request identity (multi-user)
# ---------------------------------------------------------------------------

# Set by the auth middleware for the duration of one HTTP request. Every tool
# resolves the calling user's Wiki.js key from this, so a single server process
# serves many users without ever mixing their credentials.
_current_sub: ContextVar[Optional[str]] = ContextVar("current_sub", default=None)
_current_claims: ContextVar[Optional[Dict[str, Any]]] = ContextVar("current_claims", default=None)


class AuthError(Exception):
    """Raised when the caller has no usable Wiki.js credential."""


def current_owner() -> str:
    """Identity that owns file mappings for the current call."""
    if not settings.oauth_enabled:
        return _SINGLE_USER_OWNER
    sub = _current_sub.get()
    if not sub:
        raise AuthError("No authenticated user in the current request context.")
    return sub


def _fernet() -> Optional["Fernet"]:
    if not settings.MCP_ENCRYPTION_KEY or not _FERNET_AVAILABLE:
        return None
    try:
        return Fernet(settings.MCP_ENCRYPTION_KEY.encode())
    except Exception as e:
        raise ValueError(f"MCP_ENCRYPTION_KEY is not a valid Fernet key: {e}")


def store_user_key(owner_sub: str, api_key: str, label: str = "") -> None:
    """Persist (and optionally encrypt) one user's Wiki.js API key."""
    f = _fernet()
    stored, encrypted = (f.encrypt(api_key.encode()).decode(), True) if f else (api_key, False)
    with get_db() as db:
        row = db.query(UserKey).filter(UserKey.owner_sub == owner_sub).first()
        if row:
            row.wikijs_api_key = stored
            row.is_encrypted = encrypted
            row.label = label
        else:
            db.add(UserKey(owner_sub=owner_sub, wikijs_api_key=stored, is_encrypted=encrypted, label=label))


def load_user_key(owner_sub: str) -> Optional[str]:
    """Return one user's decrypted Wiki.js API key, or None if unregistered."""
    with get_db() as db:
        row = db.query(UserKey).filter(UserKey.owner_sub == owner_sub).first()
        if not row:
            return None
        stored, encrypted = row.wikijs_api_key, row.is_encrypted
        row.last_used = datetime.datetime.now(UTC)

    if not encrypted:
        return stored
    f = _fernet()
    if not f:
        raise AuthError(
            "Your stored Wiki.js key is encrypted but MCP_ENCRYPTION_KEY is not configured "
            "on the server. Restore the key or re-register with wikijs_register_my_key."
        )
    try:
        return f.decrypt(stored.encode()).decode()
    except InvalidToken:
        raise AuthError(
            "Your stored Wiki.js key could not be decrypted (MCP_ENCRYPTION_KEY changed). "
            "Re-register it with wikijs_register_my_key."
        )


def resolve_api_key() -> str:
    """The Wiki.js API key to use for the current call."""
    if not settings.oauth_enabled:
        return settings.WIKIJS_API_KEY

    key = load_user_key(current_owner())
    if not key:
        raise AuthError(
            "No Wiki.js API key registered for your account. Create one in Wiki.js under "
            "Administration → API Access, then call wikijs_register_my_key with it. "
            "Your key determines which pages you can read and edit."
        )
    return key


def owned_mappings(db):
    """FileMapping query restricted to the calling user's own mappings."""
    return db.query(FileMapping).filter(FileMapping.owner_sub == current_owner())


# ---------------------------------------------------------------------------
# GraphQL Client
# ---------------------------------------------------------------------------

class WikiJSClient:
    """Async Wiki.js GraphQL client with retry logic.

    The API key is resolved per instance — in oauth mode that is the calling
    user's own key, so Wiki.js applies their permissions to every operation.
    """

    def __init__(self, api_key: str = None):
        key = api_key if api_key is not None else resolve_api_key()
        self.client = httpx.AsyncClient(timeout=120.0, headers=settings.headers_for(key))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.client.aclose()

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=8), reraise=True, before_sleep=before_sleep_log(logger, logging.WARNING))
    async def query(self, gql: str, variables: Dict = None) -> Dict:
        payload: Dict[str, Any] = {"query": gql}
        if variables:
            payload["variables"] = variables
        try:
            resp = await self.client.post(settings.graphql_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                msg = "; ".join(e.get("message", str(e)) for e in data["errors"])
                raise Exception(f"GraphQL error: {msg}")
            return data.get("data", {})
        except httpx.HTTPStatusError as e:
            raise Exception(f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except httpx.RequestError as e:
            raise Exception(f"Connection error: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_file_hash(file_path: str) -> str:
    try:
        with open(file_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (FileNotFoundError, OSError):
        return ""


def find_repo_root(start: str = None) -> Optional[str]:
    path = Path(start or os.getcwd()).resolve()
    for p in [path] + list(path.parents):
        if (p / ".git").exists() or (p / ".wikijs_mcp").exists():
            return str(p)
    return str(path)


def extract_code_structure(file_path: str) -> Dict[str, Any]:
    """Extract classes, functions, and imports from a Python file via AST."""
    try:
        size = os.path.getsize(file_path)
        if size > _MAX_FILE_SIZE_BYTES:
            return {"error": f"File too large ({size // 1024} KB) — max {_MAX_FILE_SIZE_BYTES // 1024} KB"}
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        tree = ast.parse(content)
        result: Dict[str, Any] = {"classes": [], "functions": [], "imports": []}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                result["classes"].append({"name": node.name, "line": node.lineno, "docstring": ast.get_docstring(node)})
            elif isinstance(node, ast.FunctionDef):
                result["functions"].append({"name": node.name, "line": node.lineno, "docstring": ast.get_docstring(node)})
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    result["imports"].append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    result["imports"].append(f"{mod}.{alias.name}")
        return result
    except Exception as e:
        logger.error(f"AST parse error for {file_path}: {e}")
        return {"classes": [], "functions": [], "imports": []}


# Cached Wiki.js default content locale (resolved once from the instance).
_default_locale_cache: Optional[str] = None


async def resolve_default_locale(c: "WikiJSClient", override: Optional[str] = None) -> str:
    """Resolve the content locale to use for a page operation.

    Precedence: explicit ``override`` → ``WIKIJS_DEFAULT_LOCALE`` setting →
    the Wiki.js instance's own default locale (queried once and cached) → ``en``.

    This avoids hard-coding ``en``, which would otherwise create/read pages in
    the wrong locale on wikis whose default locale is not English.
    """
    if override:
        return override
    if settings.WIKIJS_DEFAULT_LOCALE:
        return settings.WIKIJS_DEFAULT_LOCALE
    global _default_locale_cache
    if _default_locale_cache:
        return _default_locale_cache
    try:
        data = await c.query("query{localization{config{locale}}}")
        loc = data.get("localization", {}).get("config", {}).get("locale")
        _default_locale_cache = loc or "en"
        logger.info(f"Resolved Wiki.js default locale: {_default_locale_cache}")
    except Exception as e:
        logger.warning(f"Could not resolve Wiki.js default locale, falling back to 'en': {e}")
        _default_locale_cache = "en"
    return _default_locale_cache


# ---------------------------------------------------------------------------
# OAuth 2.1 resource server (multi-user connector mode)
# ---------------------------------------------------------------------------

_jwks_client: Optional["PyJWKClient"] = None
_jwks_lock = asyncio.Lock()


async def _discover_jwks_url() -> str:
    """Resolve the issuer's JWKS URL, via OIDC discovery when not configured."""
    if settings.OAUTH_JWKS_URL:
        return settings.OAUTH_JWKS_URL

    disco = f"{settings.OAUTH_ISSUER.rstrip('/')}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=30.0) as c:
        resp = await c.get(disco)
        resp.raise_for_status()
        jwks_uri = resp.json().get("jwks_uri")
    if not jwks_uri:
        raise ValueError(f"No jwks_uri in OIDC discovery document at {disco}")
    logger.info("Discovered JWKS URL: %s", jwks_uri)
    return jwks_uri


async def _get_jwks_client() -> "PyJWKClient":
    global _jwks_client
    if _jwks_client is not None:
        return _jwks_client
    async with _jwks_lock:
        if _jwks_client is None:  # re-check inside the lock
            _jwks_client = PyJWKClient(await _discover_jwks_url(), cache_keys=True)
    return _jwks_client


async def validate_bearer_token(token: str) -> Dict[str, Any]:
    """Verify a bearer token's signature, issuer, audience and scopes.

    Raises PermissionError for a valid token lacking required scopes, and
    ValueError for anything that makes the token unusable.
    """
    client = await _get_jwks_client()
    # PyJWKClient does blocking I/O when a key is not cached yet.
    signing_key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)

    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=settings.algorithms,
        audience=settings.OAUTH_AUDIENCE or None,
        issuer=settings.OAUTH_ISSUER or None,
        options={"verify_aud": bool(settings.OAUTH_AUDIENCE)},
        leeway=60,
    )

    required = settings.required_scopes
    if required:
        raw = claims.get("scope") or claims.get("scp") or ""
        granted = set(raw.split()) if isinstance(raw, str) else set(raw)
        missing = [s for s in required if s not in granted]
        if missing:
            raise PermissionError(f"Token is missing required scope(s): {', '.join(missing)}")

    return claims


def _resource_metadata_url() -> str:
    return f"{settings.MCP_PUBLIC_URL.rstrip('/')}/.well-known/oauth-protected-resource"


def _auth_challenge(detail: str, status: int = 401) -> JSONResponse:
    """401/403 carrying the RFC 9728 pointer clients use to find the auth server."""
    err = "invalid_token" if status == 401 else "insufficient_scope"
    return JSONResponse(
        {"error": err, "error_description": detail},
        status_code=status,
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="wiki-js-mcp-server", error="{err}", '
                f'error_description="{detail}", '
                f'resource_metadata="{_resource_metadata_url()}"'
            )
        },
    )


async def protected_resource_metadata(request) -> JSONResponse:
    """RFC 9728 metadata — tells MCP clients which authorization server to use."""
    return JSONResponse(
        {
            "resource": settings.resource_identifier,
            "authorization_servers": [settings.OAUTH_ISSUER.rstrip("/")],
            "scopes_supported": settings.supported_scopes,
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://github.com/2rock-Inc/wiki-js-mcp-server",
        }
    )


class OAuthResourceServerMiddleware:
    """Pure-ASGI bearer-token gate.

    Implemented as raw ASGI rather than BaseHTTPMiddleware so the identity
    ContextVars are set in the same task that runs the MCP app — with
    BaseHTTPMiddleware the downstream app runs in a child task and context
    propagation becomes subtle.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.oauth_enabled:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        # Discovery must stay reachable to unauthenticated clients — that is how
        # they learn where to authenticate.
        if path.startswith("/.well-known/") or path == "/healthz":
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return await _auth_challenge("Missing bearer token.")(scope, receive, send)

        try:
            claims = await validate_bearer_token(auth[7:].strip())
        except PermissionError as e:
            return await _auth_challenge(str(e), status=403)(scope, receive, send)
        except Exception as e:
            logger.warning("Rejected bearer token: %s", e)
            return await _auth_challenge("Invalid or expired token.")(scope, receive, send)

        sub = claims.get("sub")
        if not sub:
            return await _auth_challenge("Token has no 'sub' claim.")(scope, receive, send)

        t_sub = _current_sub.set(sub)
        t_claims = _current_claims.set(claims)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_sub.reset(t_sub)
            _current_claims.reset(t_claims)


# ---------------------------------------------------------------------------
# FastMCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("wiki-js-mcp-server")


# ── Connection & Status ──────────────────────────────────────────────────────

@mcp.tool()
async def wikijs_connection_status() -> str:
    """Check Wiki.js connection and authentication status for the current user."""
    base: Dict[str, Any] = {
        "api_url": settings.WIKIJS_URL,
        "graphql_url": settings.graphql_url,
        "server_version": __version__,
        "auth_mode": "oauth" if settings.oauth_enabled else "single_user",
    }

    if settings.oauth_enabled:
        try:
            base["user"] = _describe_current_user()
            base["key_registered"] = load_user_key(current_owner()) is not None
        except AuthError as e:
            return json.dumps({**base, "connected": False, "error": str(e), "status": "not_authenticated"})
        if not base["key_registered"]:
            return json.dumps({
                **base,
                "connected": False,
                "status": "key_not_registered",
                "next_step": "Call wikijs_register_my_key with your personal Wiki.js API key.",
            })

    try:
        async with WikiJSClient() as c:
            await c.query("query { pages { list(limit: 1) { id } } }")
        return json.dumps({**base, "connected": True, "authenticated": True, "status": "healthy"})
    except Exception as e:
        return json.dumps({**base, "connected": False, "error": str(e), "status": "connection_failed"})


def _describe_current_user() -> Dict[str, Any]:
    """Non-sensitive identity summary from the validated token."""
    claims = _current_claims.get() or {}
    return {
        "sub": current_owner(),
        "email": claims.get("email"),
        "name": claims.get("name") or claims.get("preferred_username"),
    }


@mcp.tool()
async def wikijs_whoami() -> str:
    """Show who the server thinks you are and whether your Wiki.js key is registered."""
    if not settings.oauth_enabled:
        return json.dumps({
            "auth_mode": "single_user",
            "note": "This server uses one shared WIKIJS_API_KEY; there is no per-user identity.",
        })
    try:
        owner = current_owner()
    except AuthError as e:
        return json.dumps({"auth_mode": "oauth", "authenticated": False, "error": str(e)})

    with get_db() as db:
        row = db.query(UserKey).filter(UserKey.owner_sub == owner).first()
        registered = row is not None
        info = {
            "label": row.label,
            "encrypted_at_rest": row.is_encrypted,
            "registered_at": row.created_at.isoformat() if row.created_at else None,
            "last_used": row.last_used.isoformat() if row.last_used else None,
        } if row else None

    return json.dumps({
        "auth_mode": "oauth",
        "authenticated": True,
        "user": _describe_current_user(),
        "key_registered": registered,
        "key": info,
    })


@mcp.tool()
async def wikijs_register_my_key(api_key: str, label: str = "") -> str:
    """
    Register YOUR personal Wiki.js API key with this server (one-time setup).

    Every wiki operation you request is then performed with this key, so you see
    and change exactly what your own Wiki.js account is allowed to. The key is
    stored server-side against your authenticated identity, encrypted at rest
    when the server is configured with an encryption key.

    Create a key in Wiki.js under Administration → API Access.

    Args:
        api_key: Your personal Wiki.js API key
        label: Optional note to recognise this key later (e.g. 'laptop')
    """
    if not settings.oauth_enabled:
        return json.dumps({
            "error": "This server runs in single-user mode and uses the shared WIKIJS_API_KEY. "
                     "Per-user keys require MCP_AUTH_MODE=oauth.",
        })

    api_key = (api_key or "").strip()
    if not api_key:
        return json.dumps({"error": "api_key must not be empty."})

    try:
        owner = current_owner()
    except AuthError as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise

    # Verify the key actually works before storing it — a bad key stored now
    # would fail confusingly on every later call.
    try:
        async with WikiJSClient(api_key=api_key) as c:
            await c.query("query { pages { list(limit: 1) { id } } }")
    except Exception as e:
        return json.dumps({
            "registered": False,
            "error": f"Wiki.js rejected this key: {e}",
            "hint": "Check the key is valid and has not expired in Administration → API Access.",
        })

    store_user_key(owner, api_key, label)
    logger.info("Registered Wiki.js key for user %s", owner)
    return json.dumps({
        "registered": True,
        "user": _describe_current_user(),
        "label": label,
        "encrypted_at_rest": bool(settings.MCP_ENCRYPTION_KEY),
        "status": "verified_and_stored",
    })


@mcp.tool()
async def wikijs_forget_my_key() -> str:
    """Delete your stored Wiki.js API key from this server."""
    if not settings.oauth_enabled:
        return json.dumps({"error": "This server runs in single-user mode; there is no stored per-user key."})

    try:
        owner = current_owner()
    except AuthError as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise

    with get_db() as db:
        row = db.query(UserKey).filter(UserKey.owner_sub == owner).first()
        if not row:
            return json.dumps({"removed": False, "status": "no_key_registered"})
        db.delete(row)

    logger.info("Removed Wiki.js key for user %s", owner)
    return json.dumps({"removed": True, "status": "deleted"})


# ── Page CRUD ────────────────────────────────────────────────────────────────

@mcp.tool()
async def wikijs_create_page(
    title: str,
    content: str,
    path: str = "",
    description: str = "",
    tags: List[str] = None,
    parent_id: int = None,
    locale: str = None,
) -> str:
    """
    Create a new Wiki.js page.

    Args:
        title: Page title
        content: Markdown content
        path: Explicit path (e.g. 'infra/proxmox/setup'). Auto-generated from title if empty.
        description: Short description (optional)
        tags: List of tags (optional)
        parent_id: Parent page ID — path will be prefixed with parent path (optional)
        locale: Content locale (optional). Defaults to the wiki's default locale.
    """
    try:
        async with WikiJSClient() as c:
            loc = await resolve_default_locale(c, locale)
            if not path:
                if parent_id:
                    pr = await c.query(
                        "query($id:Int!){pages{single(id:$id){path}}}",
                        {"id": parent_id},
                    )
                    parent_path = pr.get("pages", {}).get("single", {}).get("path", "")
                    path = f"{parent_path}/{slugify(title)}" if parent_path else slugify(title)
                else:
                    path = slugify(title)

            mutation = """
            mutation($content:String!,$description:String!,$editor:String!,$isPublished:Boolean!,
                     $isPrivate:Boolean!,$locale:String!,$path:String!,$tags:[String]!,$title:String!){
                pages{
                    create(content:$content,description:$description,editor:$editor,
                           isPublished:$isPublished,isPrivate:$isPrivate,locale:$locale,
                           path:$path,tags:$tags,title:$title){
                        responseResult{succeeded errorCode message}
                        page{id path title}
                    }
                }
            }"""

            data = await c.query(mutation, {
                "content": content,
                "description": description,
                "editor": "markdown",
                "isPublished": True,
                "isPrivate": False,
                "locale": loc,
                "path": path,
                "tags": tags or [],
                "title": title,
            })

            res = data.get("pages", {}).get("create", {})
            rr = res.get("responseResult", {})
            if rr.get("succeeded"):
                pg = res.get("page", {})
                return json.dumps({"pageId": pg["id"], "path": pg["path"], "title": pg["title"], "status": "created"})
            return json.dumps({"error": rr.get("message", "Unknown error")})

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_get_page(page_id: int = None, path: str = None, locale: str = None, include_content: bool = True, max_content_chars: int = 800000) -> str:
    """
    Get a Wiki.js page by ID or path.

    Args:
        page_id: Page ID (use either page_id OR path)
        path: Page path e.g. 'infra/proxmox' (use either page_id OR path)
        locale: Locale (default: the wiki's default locale)
        include_content: Include page content (default: True). Set False for metadata only.
        max_content_chars: Truncate content at this many chars (default: 800000 ~800KB).
    """
    try:
        if not page_id and not path:
            return json.dumps({"error": "Provide page_id or path"})

        async with WikiJSClient() as c:
            if page_id:
                q = """query($id:Int!){pages{single(id:$id){
                    id path title content description isPublished locale
                    createdAt updatedAt editor authorName tags{tag}
                }}}"""
                data = await c.query(q, {"id": page_id})
                pg = data.get("pages", {}).get("single")
            else:
                q = """query($path:String!,$locale:String!){pages{singleByPath(path:$path,locale:$locale){
                    id path title content description isPublished locale
                    createdAt updatedAt editor authorName tags{tag}
                }}}"""
                loc = await resolve_default_locale(c, locale)
                data = await c.query(q, {"path": path, "locale": loc})
                pg = data.get("pages", {}).get("singleByPath")

            if not pg:
                return json.dumps({"error": "Page not found"})

            page_content = pg["content"] if include_content else ""
            content_size = len(page_content.encode("utf-8"))
            truncated = False
            if include_content and content_size > max_content_chars:
                page_content = page_content[:max_content_chars] + "\n\n[... contenu tronqué — page trop grande pour le MCP]"
                truncated = True

            return json.dumps({
                "pageId": pg["id"],
                "title": pg["title"],
                "path": pg["path"],
                "content": page_content,
                "content_size_bytes": content_size,
                "content_truncated": truncated,
                "description": pg.get("description", ""),
                "isPublished": pg.get("isPublished"),
                "locale": pg.get("locale"),
                "editor": pg.get("editor"),
                "author": pg.get("authorName"),
                "createdAt": pg.get("createdAt"),
                "updatedAt": pg.get("updatedAt"),
                "tags": [t["tag"] for t in pg.get("tags", [])],
            })

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise




@mcp.tool()
async def wikijs_get_page_metadata(page_id: int = None, path: str = None, locale: str = None) -> str:
    """
    Get page metadata only — no content. Fast and always within size limits.
    Use this when wikijs_get_page fails due to page size (large images, etc.)

    Args:
        page_id: Page ID (use either page_id OR path)
        path: Page path (use either page_id OR path)
        locale: Locale (default: the wiki's default locale)
    """
    try:
        if not page_id and not path:
            return json.dumps({"error": "Provide page_id or path"})

        async with WikiJSClient() as c:
            if page_id:
                q = """query($id:Int!){pages{single(id:$id){
                    id path title description isPublished locale
                    createdAt updatedAt editor authorName tags{tag}
                    content
                }}}"""
                data = await c.query(q, {"id": page_id})
                pg = data.get("pages", {}).get("single")
            else:
                q = """query($path:String!,$locale:String!){pages{singleByPath(path:$path,locale:$locale){
                    id path title description isPublished locale
                    createdAt updatedAt editor authorName tags{tag}
                    content
                }}}"""
                loc = await resolve_default_locale(c, locale)
                data = await c.query(q, {"path": path, "locale": loc})
                pg = data.get("pages", {}).get("singleByPath")

            if not pg:
                return json.dumps({"error": "Page not found"})

            raw_content = pg.get("content", "")
            content_bytes = len(raw_content.encode("utf-8"))

            return json.dumps({
                "pageId": pg["id"],
                "title": pg["title"],
                "path": pg["path"],
                "description": pg.get("description", ""),
                "isPublished": pg.get("isPublished"),
                "locale": pg.get("locale"),
                "editor": pg.get("editor"),
                "author": pg.get("authorName"),
                "createdAt": pg.get("createdAt"),
                "updatedAt": pg.get("updatedAt"),
                "tags": [t["tag"] for t in pg.get("tags", [])],
                "content_size_bytes": content_bytes,
                "content_size_kb": round(content_bytes / 1024, 1),
                "has_large_content": content_bytes > 500_000,
                "tip": "Use wikijs_get_page with include_content=False to get metadata only, or include_content=True with max_content_chars to truncate." if content_bytes > 500_000 else None,
            })

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise

@mcp.tool()
async def wikijs_update_page(
    page_id: int,
    content: str = None,
    title: str = None,
    description: str = None,
    tags: List[str] = None,
) -> str:
    """
    Update an existing Wiki.js page. Only provided fields are changed.

    Args:
        page_id: ID of the page to update
        content: New markdown content (optional)
        title: New title (optional)
        description: New description (optional)
        tags: New tags list (optional)
    """
    try:
        current_raw = await wikijs_get_page(page_id=page_id)
        current = json.loads(current_raw)
        if "error" in current:
            return current_raw

        async with WikiJSClient() as c:
            mutation = """
            mutation($id:Int!,$content:String,$description:String,$editor:String,
                     $isPrivate:Boolean,$isPublished:Boolean,$locale:String,
                     $path:String,$tags:[String],$title:String){
                pages{
                    update(id:$id,content:$content,description:$description,editor:$editor,
                           isPrivate:$isPrivate,isPublished:$isPublished,locale:$locale,
                           path:$path,tags:$tags,title:$title){
                        responseResult{succeeded errorCode message}
                        page{id path title updatedAt}
                    }
                }
            }"""

            data = await c.query(mutation, {
                "id": page_id,
                "content": content if content is not None else current["content"],
                "title": title if title is not None else current["title"],
                "description": description if description is not None else current.get("description", ""),
                "editor": current.get("editor", "markdown"),
                "isPrivate": False,
                "isPublished": current.get("isPublished", True),
                "locale": current.get("locale", "en"),
                "path": current["path"],
                "tags": tags if tags is not None else current.get("tags", []),
            })

            res = data.get("pages", {}).get("update", {})
            rr = res.get("responseResult", {})
            if rr.get("succeeded"):
                pg = res.get("page", {})
                return json.dumps({"pageId": page_id, "status": "updated", "title": pg["title"], "updatedAt": pg["updatedAt"]})
            return json.dumps({"error": rr.get("message", "Unknown error")})

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_delete_page(
    page_id: int = None,
    path: str = None,
    remove_file_mapping: bool = True,
) -> str:
    """
    Delete a Wiki.js page by ID or path.

    Args:
        page_id: Page ID (use either page_id OR path)
        path: Page path (use either page_id OR path)
        remove_file_mapping: Remove local file→page mapping if it exists
    """
    try:
        if not page_id and not path:
            return json.dumps({"error": "Provide page_id or path"})

        if not page_id and path:
            pg_raw = await wikijs_get_page(path=path)
            pg = json.loads(pg_raw)
            if "error" in pg:
                return pg_raw
            page_id = pg["pageId"]

        async with WikiJSClient() as c:
            data = await c.query(
                "mutation($id:Int!){pages{delete(id:$id){responseResult{succeeded message}}}}",
                {"id": page_id},
            )
            rr = data.get("pages", {}).get("delete", {}).get("responseResult", {})
            if rr.get("succeeded"):
                result: Dict[str, Any] = {"deleted": True, "pageId": page_id, "status": "deleted"}
                if remove_file_mapping:
                    with get_db() as db:
                        mapping = owned_mappings(db).filter(FileMapping.page_id == page_id).first()
                        if mapping:
                            db.delete(mapping)
                            result["file_mapping_removed"] = True
                return json.dumps(result)
            return json.dumps({"error": rr.get("message", "Unknown error")})

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_move_page(
    page_id: int,
    destination_path: str,
    destination_locale: str = None,
) -> str:
    """
    Move a Wiki.js page to a new path.

    Args:
        page_id: ID of the page to move
        destination_path: New path (e.g. 'infra/archive/old-page')
        destination_locale: New locale (default: the wiki's default locale)
    """
    try:
        current_raw = await wikijs_get_page(page_id=page_id)
        current = json.loads(current_raw)
        if "error" in current:
            return current_raw

        async with WikiJSClient() as c:
            destination_locale = await resolve_default_locale(c, destination_locale)
            data = await c.query(
                """mutation($id:Int!,$destinationPath:String!,$destinationLocale:String!){
                    pages{move(id:$id,destinationPath:$destinationPath,destinationLocale:$destinationLocale){
                        responseResult{succeeded errorCode message}
                    }}
                }""",
                {"id": page_id, "destinationPath": destination_path, "destinationLocale": destination_locale},
            )
            rr = data.get("pages", {}).get("move", {}).get("responseResult", {})
            if rr.get("succeeded"):
                return json.dumps({
                    "moved": True,
                    "pageId": page_id,
                    "title": current["title"],
                    "from": current["path"],
                    "to": destination_path,
                    "locale": destination_locale,
                })
            return json.dumps({"error": rr.get("message", "Unknown error")})

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


# ── Search & List ────────────────────────────────────────────────────────────

@mcp.tool()
async def wikijs_search_pages(query: str, limit: int = 20) -> str:
    """
    Full-text search across all Wiki.js pages.

    Args:
        query: Search terms
        limit: Max results (default: 20, max: 100)
    """
    limit = min(limit, 100)
    try:
        async with WikiJSClient() as c:
            try:
                loc = await resolve_default_locale(c)
                data = await c.query(
                    """query($query:String!,$locale:String!){pages{search(query:$query,path:"",locale:$locale){
                        results{id title description path locale} totalHits
                    }}}""",
                    {"query": query, "locale": loc},
                )
                results = data.get("pages", {}).get("search", {}).get("results", [])[:limit]
                total = data.get("pages", {}).get("search", {}).get("totalHits", len(results))
            except Exception:
                # Fallback: client-side filter on list
                all_raw = await wikijs_list_pages(limit=1000)
                all_data = json.loads(all_raw)
                q = query.lower()
                results = [
                    p for p in all_data.get("pages", [])
                    if q in p.get("title", "").lower()
                    or q in p.get("description", "").lower()
                    or q in p.get("path", "").lower()
                ][:limit]
                total = len(results)

            return json.dumps({"results": results, "total": total, "query": query})

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_list_pages(limit: int = 50) -> str:
    """
    List all Wiki.js pages.

    Args:
        limit: Maximum number of pages to return (default: 50, max: 5000)
    """
    limit = min(limit, 5000)
    try:
        async with WikiJSClient() as c:
            data = await c.query(
                """query($limit:Int!){pages{list(limit:$limit){
                    id path title description updatedAt createdAt locale isPublished
                }}}""",
                {"limit": limit},
            )
            pages = data.get("pages", {}).get("list", [])
            return json.dumps({"pages": pages, "count": len(pages)})

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_get_tree(
    parent_path: str = "",
    mode: str = "ALL",
    locale: str = None,
    parent_id: int = None,
) -> str:
    """
    Get the Wiki.js page tree structure.

    Args:
        parent_path: Root path to start from (empty = full tree)
        mode: ALL | FOLDERS | PAGES (default: ALL)
        locale: Locale filter (default: the wiki's default locale)
        parent_id: Parent page ID (optional)
    """
    try:
        async with WikiJSClient() as c:
            locale = await resolve_default_locale(c, locale)
            data = await c.query(
                """query($path:String,$parent:Int,$mode:PageTreeMode!,$locale:String!,$includeAncestors:Boolean){
                    pages{tree(path:$path,parent:$parent,mode:$mode,locale:$locale,includeAncestors:$includeAncestors){
                        id path depth title isPrivate isFolder parent pageId locale
                    }}
                }""",
                {
                    "path": parent_path or None,
                    "parent": parent_id,
                    "mode": mode,
                    "locale": locale,
                    "includeAncestors": False,
                },
            )
            tree = data.get("pages", {}).get("tree", [])
            return json.dumps({"tree": tree, "count": len(tree), "root": parent_path or "/"})

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


# ── Hierarchical tools ───────────────────────────────────────────────────────

@mcp.tool()
async def wikijs_get_page_children(page_id: int = None, path: str = None) -> str:
    """
    List direct child pages of a given page.

    Args:
        page_id: Parent page ID (use either page_id OR path)
        path: Parent page path (use either page_id OR path)
    """
    try:
        if not page_id and not path:
            return json.dumps({"error": "Provide page_id or path"})

        parent_raw = await wikijs_get_page(page_id=page_id, path=path)
        parent = json.loads(parent_raw)
        if "error" in parent:
            return parent_raw

        parent_path = parent["path"]

        async with WikiJSClient() as c:
            data = await c.query(
                "query{pages{list(limit:5000){id title path description isPublished updatedAt}}}",
            )
            all_pages = data.get("pages", {}).get("list", [])

        children = [
            {
                "pageId": p["id"],
                "title": p["title"],
                "path": p["path"],
                "description": p.get("description", ""),
                "updatedAt": p.get("updatedAt"),
                "isPublished": p.get("isPublished"),
            }
            for p in all_pages
            if p["path"].startswith(f"{parent_path}/")
            and "/" not in p["path"][len(parent_path) + 1:]
        ]

        return json.dumps({
            "parent": {"pageId": parent["pageId"], "title": parent["title"], "path": parent_path},
            "children": children,
            "total": len(children),
        })

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_create_nested_page(
    title: str,
    content: str,
    parent_path: str,
    create_parent_if_missing: bool = True,
    locale: str = None,
) -> str:
    """
    Create a page nested under a given path, creating parent pages if needed.

    Args:
        title: New page title
        content: Markdown content
        parent_path: Full parent path (e.g. 'infra/proxmox')
        create_parent_if_missing: Auto-create missing parent pages
        locale: Content locale (optional). Defaults to the wiki's default locale.
    """
    try:
        parent_raw = await wikijs_get_page(path=parent_path, locale=locale)
        parent = json.loads(parent_raw)

        if "error" in parent:
            if not create_parent_if_missing:
                return json.dumps({"error": f"Parent '{parent_path}' not found"})

            parts = parent_path.split("/")
            current = ""
            for part in parts:
                current = f"{current}/{part}".lstrip("/")
                check_raw = await wikijs_get_page(path=current, locale=locale)
                check = json.loads(check_raw)
                if "error" in check:
                    part_title = part.replace("-", " ").title()
                    cr = json.loads(await wikijs_create_page(
                        title=part_title,
                        content=f"# {part_title}\n\n*Auto-created section.*",
                        path=current,
                        locale=locale,
                    ))
                    if "error" in cr:
                        return json.dumps({"error": f"Failed to create parent '{current}': {cr['error']}"})

        full_path = f"{parent_path}/{slugify(title)}"
        result_raw = await wikijs_create_page(title=title, content=content, path=full_path, locale=locale)
        result = json.loads(result_raw)
        if "error" not in result:
            result["parent_path"] = parent_path
            result["full_path"] = full_path
        return json.dumps(result)

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_create_repo_structure(
    repo_name: str,
    description: str = None,
    sections: List[str] = None,
    locale: str = None,
) -> str:
    """
    Create a complete documentation structure for a repository/project.

    Args:
        repo_name: Repository name (becomes root page)
        description: Short description of the project
        sections: Section names to create (default: Overview, Architecture, API, Development, Deployment)
        locale: Content locale (optional). Defaults to the wiki's default locale.
    """
    try:
        if not sections:
            sections = ["Overview", "Architecture", "API Reference", "Development", "Deployment"]

        root_path = slugify(repo_name)
        toc = "\n".join(f"- [{s}]({root_path}/{slugify(s)})" for s in sections)
        root_content = (
            f"# {repo_name}\n\n{description or ''}\n\n"
            f"## Sections\n\n{toc}\n\n"
            f"---\n*Generated by wiki-js-mcp-server*"
        )

        root_raw = await wikijs_create_page(title=repo_name, content=root_content, path=root_path, locale=locale)
        root = json.loads(root_raw)
        if "error" in root:
            return root_raw

        created = [root]
        for section in sections:
            sec_path = f"{root_path}/{slugify(section)}"
            sec_content = (
                f"# {section}\n\n"
                f"*Documentation for {repo_name} — {section} section.*\n\n"
                f"---\n[← Back to {repo_name}]({root_path})"
            )
            sec_raw = await wikijs_create_page(title=section, content=sec_content, path=sec_path, locale=locale)
            sec = json.loads(sec_raw)
            if "error" not in sec:
                created.append(sec)

        return json.dumps({
            "repo": repo_name,
            "root_page_id": root["pageId"],
            "root_path": root_path,
            "sections": sections,
            "pages_created": len(created),
            "pages": created,
            "status": "created",
        })

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_create_documentation_hierarchy(
    project_name: str,
    file_mappings: List[Dict[str, str]],
    auto_organize: bool = True,
    locale: str = None,
) -> str:
    """
    Build a full documentation hierarchy for a project from a list of files.

    Args:
        project_name: Project name (root page)
        file_mappings: List of {"file_path": "src/foo.py"} dicts
        auto_organize: Auto-categorize files into components/api/utils/etc.
        locale: Content locale (optional). Defaults to the wiki's default locale.
    """
    try:
        buckets: Dict[str, List] = {
            "components": [], "api": [], "utils": [], "services": [],
            "models": [], "tests": [], "config": [], "other": [],
        }

        if auto_organize:
            for m in file_mappings:
                fp = m["file_path"].lower()
                if "component" in fp:
                    buckets["components"].append(m)
                elif "api" in fp or "endpoint" in fp or "route" in fp:
                    buckets["api"].append(m)
                elif "util" in fp or "helper" in fp:
                    buckets["utils"].append(m)
                elif "service" in fp:
                    buckets["services"].append(m)
                elif "model" in fp or "type" in fp or "schema" in fp:
                    buckets["models"].append(m)
                elif "test" in fp:
                    buckets["tests"].append(m)
                elif "config" in fp or ".env" in fp:
                    buckets["config"].append(m)
                else:
                    buckets["other"].append(m)
        else:
            buckets["other"] = file_mappings

        active_sections = [k.title() for k, v in buckets.items() if v]
        repo_raw = await wikijs_create_repo_structure(project_name, sections=active_sections, locale=locale)
        repo = json.loads(repo_raw)
        if "error" in repo:
            return repo_raw

        created_pages: List[Dict] = []

        for bucket, files in buckets.items():
            for fm in files:
                fp = fm["file_path"]
                page_title = os.path.basename(fp)
                parent = f"{slugify(project_name)}/{bucket}"
                ov_raw = await wikijs_create_nested_page(
                    title=page_title,
                    content=f"# {page_title}\n\n**File:** `{fp}`\n\n*Auto-generated.*",
                    parent_path=parent,
                    locale=locale,
                )
                ov = json.loads(ov_raw)
                if "error" not in ov:
                    created_pages.append(ov)
                    with get_db() as db:
                        existing = owned_mappings(db).filter(FileMapping.file_path == fp).first()
                        if existing:
                            existing.page_id = ov["pageId"]
                            existing.relationship_type = "documents"
                            existing.file_hash = get_file_hash(fp)
                            existing.repository_root = find_repo_root(fp) or ""
                            existing.last_updated = datetime.datetime.now(UTC)
                        else:
                            db.add(FileMapping(
                                owner_sub=current_owner(),
                                file_path=fp,
                                page_id=ov["pageId"],
                                relationship_type="documents",
                                file_hash=get_file_hash(fp),
                                repository_root=find_repo_root(fp) or "",
                            ))

        return json.dumps({
            "project": project_name,
            "root": repo,
            "pages_created": len(created_pages),
            "auto_organized": auto_organize,
            "status": "completed",
        })

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


# ── Spaces ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def wikijs_list_spaces() -> str:
    """List all top-level 'spaces' (root path segments) in the wiki."""
    try:
        async with WikiJSClient() as c:
            data = await c.query("query{pages{list(limit:5000){id path}}}")
        pages = data.get("pages", {}).get("list", [])
        spaces: Dict[str, Any] = {}
        for p in pages:
            top = p["path"].split("/")[0] or "root"
            if top not in spaces:
                spaces[top] = {"slug": top, "name": top.replace("-", " ").title(), "pageCount": 0}
            spaces[top]["pageCount"] += 1
        return json.dumps({"spaces": list(spaces.values()), "total": len(spaces)})

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_create_space(name: str, description: str = None) -> str:
    """
    Create a new top-level space (root page).

    Args:
        name: Space name
        description: Space description (optional)
    """
    content = (
        f"# {name}\n\n"
        f"{description or 'Main page for ' + name + '.'}\n\n"
        f"---\n*Created by wiki-js-mcp-server*"
    )
    result_raw = await wikijs_create_page(title=name, content=content)
    result = json.loads(result_raw)
    if "error" not in result:
        result["type"] = "space"
    return json.dumps(result)


# ── Batch Delete ─────────────────────────────────────────────────────────────

@mcp.tool()
async def wikijs_batch_delete_pages(
    page_ids: List[int] = None,
    page_paths: List[str] = None,
    path_pattern: str = None,
    confirm_deletion: bool = False,
) -> str:
    """
    Delete multiple pages at once.

    Args:
        page_ids: List of page IDs to delete
        page_paths: List of page paths to delete
        path_pattern: Glob pattern (e.g. 'archive/*') — capped at 100 matches for safety
        confirm_deletion: MUST be True to actually delete (safety guard)
    """
    if not confirm_deletion:
        return json.dumps({"error": "Set confirm_deletion=True to proceed.", "safety": True})

    to_delete: List[Dict] = []
    try:
        if page_ids:
            for pid in page_ids:
                raw = await wikijs_get_page(page_id=pid)
                pg = json.loads(raw)
                if "error" not in pg:
                    to_delete.append(pg)

        if page_paths:
            for pp in page_paths:
                raw = await wikijs_get_page(path=pp)
                pg = json.loads(raw)
                if "error" not in pg:
                    to_delete.append(pg)

        if path_pattern:
            all_raw = await wikijs_list_pages(limit=5000)
            all_pg = json.loads(all_raw).get("pages", [])
            matches = [
                {"pageId": pg["id"], "path": pg["path"], "title": pg["title"]}
                for pg in all_pg
                if fnmatch.fnmatch(pg["path"], path_pattern)
            ]
            if len(matches) > 100:
                return json.dumps({
                    "error": f"Pattern matches {len(matches)} pages — max 100 per batch. Refine your pattern.",
                    "matched_count": len(matches),
                })
            to_delete.extend(matches)

        # Deduplicate
        seen: Dict[int, Dict] = {}
        for pg in to_delete:
            seen[pg["pageId"]] = pg
        to_delete = list(seen.values())

        deleted, failed = [], []
        for pg in to_delete:
            raw = await wikijs_delete_page(page_id=pg["pageId"])
            res = json.loads(raw)
            if "error" not in res:
                deleted.append({"pageId": pg["pageId"], "path": pg["path"], "title": pg["title"]})
            else:
                failed.append({"pageId": pg["pageId"], "error": res["error"]})

        return json.dumps({
            "deleted": len(deleted),
            "failed": len(failed),
            "deleted_pages": deleted,
            "failed_pages": failed,
        })

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_delete_hierarchy(
    root_path: str,
    delete_mode: str = "children_only",
    confirm_deletion: bool = False,
) -> str:
    """
    Delete an entire page hierarchy.

    Args:
        root_path: Root path of the hierarchy (e.g. 'infra/old-project')
        delete_mode: children_only | include_root | root_only
        confirm_deletion: MUST be True to actually delete (safety guard)
    """
    if not confirm_deletion:
        return json.dumps({"error": "Set confirm_deletion=True to proceed.", "safety": True})

    try:
        all_raw = await wikijs_list_pages(limit=5000)
        all_pages = json.loads(all_raw).get("pages", [])

        root_page = next((p for p in all_pages if p["path"] == root_path), None)
        children = [p for p in all_pages if p["path"].startswith(f"{root_path}/")]

        match delete_mode:
            case "children_only":
                targets = children
            case "include_root":
                targets = children + ([root_page] if root_page else [])
            case "root_only":
                targets = [root_page] if root_page else []
            case _:
                return json.dumps({"error": "delete_mode must be: children_only | include_root | root_only"})

        targets.sort(key=lambda p: p["path"].count("/"), reverse=True)
        ids = [p["id"] for p in targets]
        return await wikijs_batch_delete_pages(page_ids=ids, confirm_deletion=True)

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


# ── File↔Page mapping ────────────────────────────────────────────────────────

@mcp.tool()
async def wikijs_link_file_to_page(
    file_path: str,
    page_id: int,
    relationship: str = "documents",
) -> str:
    """
    Persist a link between a local file and a Wiki.js page in the local DB.

    Args:
        file_path: Absolute or relative path to the source file
        page_id: Wiki.js page ID
        relationship: Relationship type (documents | references | etc.)
    """
    try:
        fh = get_file_hash(file_path)
        repo = find_repo_root(file_path)
        with get_db() as db:
            mapping = owned_mappings(db).filter(FileMapping.file_path == file_path).first()
            if mapping:
                mapping.page_id = page_id
                mapping.relationship_type = relationship
                mapping.file_hash = fh
                mapping.last_updated = datetime.datetime.now(UTC)
            else:
                db.add(FileMapping(
                    owner_sub=current_owner(),
                    file_path=file_path, page_id=page_id,
                    relationship_type=relationship, file_hash=fh, repository_root=repo or "",
                ))
        return json.dumps({"linked": True, "file_path": file_path, "page_id": page_id, "relationship": relationship})

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_sync_file_docs(
    file_path: str,
    change_summary: str,
    snippet: str = None,
) -> str:
    """
    Append a change note to the Wiki.js page linked to a file.

    Args:
        file_path: Path to the changed file
        change_summary: Human-readable summary of the change
        snippet: Optional code snippet illustrating the change
    """
    try:
        with get_db() as db:
            mapping = owned_mappings(db).filter(FileMapping.file_path == file_path).first()
            if not mapping:
                return json.dumps({"error": f"No page mapping for {file_path}. Use wikijs_link_file_to_page first."})
            page_id = mapping.page_id

        pg_raw = await wikijs_get_page(page_id=page_id)
        pg = json.loads(pg_raw)
        if "error" in pg:
            return pg_raw

        now = datetime.datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        addition = f"\n\n## Change — {now}\n\n{change_summary}\n"
        if snippet:
            addition += f"\n```\n{snippet}\n```\n"

        new_content = pg["content"] + addition
        update_raw = await wikijs_update_page(page_id=page_id, content=new_content)
        update = json.loads(update_raw)
        if "error" in update:
            return update_raw

        with get_db() as db:
            mapping = owned_mappings(db).filter(FileMapping.file_path == file_path).first()
            if mapping:
                mapping.file_hash = get_file_hash(file_path)
                mapping.last_updated = datetime.datetime.now(UTC)

        return json.dumps({"synced": True, "file_path": file_path, "page_id": page_id, "change_summary": change_summary})

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_generate_file_overview(
    file_path: str,
    target_page_id: int = None,
) -> str:
    """
    Auto-generate a documentation page for a Python source file.

    Args:
        file_path: Path to the source file to document
        target_page_id: Update existing page instead of creating a new one (optional)
    """
    try:
        if not os.path.exists(file_path):
            return json.dumps({"error": f"File not found: {file_path}"})

        struct = extract_code_structure(file_path)
        if "error" in struct:
            return json.dumps({"error": struct["error"]})

        now = datetime.datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        parts = [
            f"# {os.path.basename(file_path)}\n",
            f"**Path:** `{file_path}`  \n**Generated:** {now}\n",
        ]
        if struct["imports"]:
            parts.append("\n## Dependencies\n")
            parts += [f"- `{i}`" for i in struct["imports"]]
        if struct["classes"]:
            parts.append("\n## Classes\n")
            for cls in struct["classes"]:
                parts.append(f"### {cls['name']} *(line {cls['line']})*")
                if cls["docstring"]:
                    parts.append(cls["docstring"])
        if struct["functions"]:
            parts.append("\n## Functions\n")
            for fn in struct["functions"]:
                parts.append(f"### {fn['name']}() *(line {fn['line']})*")
                if fn["docstring"]:
                    parts.append(fn["docstring"])

        content = "\n".join(parts)

        if target_page_id:
            raw = await wikijs_update_page(page_id=target_page_id, content=content)
            result = json.loads(raw)
            result["action"] = "updated"
        else:
            title = f"{os.path.basename(file_path)} — Documentation"
            raw = await wikijs_create_page(title=title, content=content)
            result = json.loads(raw)
            if "error" not in result:
                result["action"] = "created"
                await wikijs_link_file_to_page(file_path, result["pageId"], "documents")

        return json.dumps(result)

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_bulk_update_project_docs(
    summary: str,
    affected_files: List[str],
    context: str = "",
    auto_create_missing: bool = True,
) -> str:
    """
    Batch-sync documentation for multiple changed files.

    Args:
        summary: Overall change description
        affected_files: List of file paths that changed
        context: Additional context (optional)
        auto_create_missing: Create pages for unmapped files
    """
    try:
        updated, created, errors = [], [], []

        for fp in affected_files:
            try:
                with get_db() as db:
                    mapping = owned_mappings(db).filter(FileMapping.file_path == fp).first()
                    has_mapping = mapping is not None
                    page_id = mapping.page_id if mapping else None

                if has_mapping:
                    raw = await wikijs_sync_file_docs(fp, f"Bulk update: {summary}", context or None)
                    res = json.loads(raw)
                    if "error" not in res:
                        updated.append({"file": fp, "page_id": page_id})
                    else:
                        errors.append({"file": fp, "error": res["error"]})
                elif auto_create_missing:
                    raw = await wikijs_generate_file_overview(fp)
                    res = json.loads(raw)
                    if "error" not in res and "pageId" in res:
                        created.append({"file": fp, "page_id": res["pageId"]})
                    else:
                        errors.append({"file": fp, "error": res.get("error", "creation failed")})
            except Exception as e:
                errors.append({"file": fp, "error": str(e)})

        return json.dumps({
            "updated": len(updated),
            "created": len(created),
            "errors": len(errors),
            "details": {"updated": updated, "created": created, "errors": errors},
        })

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_cleanup_orphaned_mappings() -> str:
    """Remove local file→page mappings whose Wiki.js page no longer exists."""
    try:
        with get_db() as db:
            mappings = owned_mappings(db).all()
            orphaned, valid = [], []

            for m in mappings:
                raw = await wikijs_get_page(page_id=m.page_id)
                res = json.loads(raw)
                if "error" in res:
                    orphaned.append({"file": m.file_path, "page_id": m.page_id})
                    db.delete(m)
                else:
                    valid.append({"file": m.file_path, "page_id": m.page_id})

        return json.dumps({
            "total": len(mappings),
            "valid": len(valid),
            "orphaned_removed": len(orphaned),
            "orphaned": orphaned,
        })

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


@mcp.tool()
async def wikijs_repository_context() -> str:
    """Show your file→page mappings, scoped to the current repository when local."""
    try:
        with get_db() as db:
            q = owned_mappings(db)
            if settings.oauth_enabled:
                # Remote multi-user mode: the server's working directory has no
                # relation to the caller's machine, so scoping by it would hide
                # every mapping. Return all of the caller's own mappings.
                repo_root = None
            else:
                repo_root = find_repo_root()
                q = q.filter(FileMapping.repository_root == repo_root)
            mappings = q.all()
            result = {
                "repository_root": repo_root,
                "scope": "all_my_mappings" if repo_root is None else "current_repository",
                "space_name": settings.DEFAULT_SPACE_NAME,
                "mapped_files": len(mappings),
                "mappings": [
                    {
                        "file": m.file_path,
                        "page_id": m.page_id,
                        "relationship": m.relationship_type,
                        "last_updated": m.last_updated.isoformat() if m.last_updated else None,
                    }
                    for m in mappings[:20]
                ],
            }
        return json.dumps(result)

    except Exception as e:
        logger.error("Tool error: %s", e, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_http_app():
    """Wrap the FastMCP app with discovery endpoints and the auth gate."""
    mcp_app = mcp.http_app()

    routes = [
        Route("/healthz", lambda request: JSONResponse({"status": "ok", "version": __version__})),
    ]
    if settings.oauth_enabled:
        # Both spellings are registered: clients may probe the bare path or the
        # resource-suffixed one derived from the MCP endpoint path.
        routes += [
            Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
            Route("/.well-known/oauth-protected-resource/mcp", protected_resource_metadata),
        ]
    routes.append(Mount("/", app=mcp_app))

    app = Starlette(
        routes=routes,
        # FastMCP's session manager lives in its own lifespan; the wrapper must
        # run it or the MCP endpoint returns 500 on first use.
        lifespan=lambda _: mcp_app.router.lifespan_context(mcp_app),
    )
    return OAuthResourceServerMiddleware(app)


async def run_http() -> None:
    """Run as HTTP server (Docker/remote)."""
    settings.validate_config()
    mode = "oauth multi-user" if settings.oauth_enabled else "single-user"
    logger.info(
        f"wiki-js-mcp-server v{__version__} HTTP mode ({mode}) — "
        f"{settings.HTTP_HOST}:{settings.HTTP_PORT}"
    )
    if settings.oauth_enabled:
        logger.info("OAuth issuer: %s", settings.OAUTH_ISSUER)
        logger.info("Protected resource: %s", settings.resource_identifier)

    config = uvicorn.Config(
        app=build_http_app(), host=settings.HTTP_HOST, port=settings.HTTP_PORT, log_level="info"
    )
    server = uvicorn.Server(config)
    await server.serve()


async def run_stdio() -> None:
    """Run as stdio server (Claude Desktop local)."""
    settings.validate_config()
    logger.info(f"wiki-js-mcp-server v{__version__} stdio mode")
    await mcp.run_stdio_async()


def main() -> None:
    import sys
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    if len(sys.argv) > 1:
        if sys.argv[1] == "--http":
            transport = "http"
        elif sys.argv[1] == "--stdio":
            transport = "stdio"

    if transport == "http":
        asyncio.run(run_http())
    else:
        asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
