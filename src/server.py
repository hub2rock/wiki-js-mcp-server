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
from sqlalchemy import Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv()

UTC = ZoneInfo("UTC")
_MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB — protection against huge files

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    model_config = ConfigDict(env_file=".env", extra="ignore")

    WIKIJS_URL: str = Field(default="http://localhost:3000")
    WIKIJS_API_KEY: str = Field(default="")
    WIKIJS_GRAPHQL_ENDPOINT: str = Field(default="/graphql")

    # Transport
    MCP_TRANSPORT: str = Field(default="stdio")  # stdio | http
    HTTP_HOST: str = Field(default="0.0.0.0")
    HTTP_PORT: int = Field(default=8000)

    # Local DB (file→page mappings)
    WIKIJS_MCP_DB: str = Field(default="./wikijs_mappings.db")

    # Misc
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FILE: str = Field(default="wikijs_mcp.log")
    DEFAULT_SPACE_NAME: str = Field(default="Documentation")

    @property
    def graphql_url(self) -> str:
        return f"{self.WIKIJS_URL.rstrip('/')}{self.WIKIJS_GRAPHQL_ENDPOINT}"

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.WIKIJS_API_KEY}",
            "Content-Type": "application/json",
        }

    def validate_config(self) -> None:
        if not self.WIKIJS_URL:
            raise ValueError("WIKIJS_URL must be set.")
        if not self.WIKIJS_API_KEY:
            raise ValueError("WIKIJS_API_KEY must be set.")


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


class FileMapping(Base):
    __tablename__ = "file_mappings"
    id = Column(Integer, primary_key=True)
    file_path = Column(String, unique=True, nullable=False)
    page_id = Column(Integer, nullable=False)
    relationship_type = Column(String, nullable=False)
    last_updated = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    file_hash = Column(String)
    repository_root = Column(String, default="")
    space_name = Column(String, default="")


engine = create_engine(f"sqlite:///{settings.WIKIJS_MCP_DB}", connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
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
# GraphQL Client
# ---------------------------------------------------------------------------

class WikiJSClient:
    """Async Wiki.js GraphQL client with retry logic."""

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0, headers=settings.headers)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
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


# ---------------------------------------------------------------------------
# FastMCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("wiki-js-mcp-server")


# ── Connection & Status ──────────────────────────────────────────────────────

@mcp.tool()
async def wikijs_connection_status() -> str:
    """Check Wiki.js connection and authentication status."""
    try:
        async with WikiJSClient() as c:
            await c.query("query { pages { list(limit: 1) { id } } }")
        return json.dumps({
            "connected": True,
            "authenticated": True,
            "api_url": settings.WIKIJS_URL,
            "graphql_url": settings.graphql_url,
            "status": "healthy",
        })
    except Exception as e:
        return json.dumps({
            "connected": False,
            "error": str(e),
            "api_url": settings.WIKIJS_URL,
            "status": "connection_failed",
        })


# ── Page CRUD ────────────────────────────────────────────────────────────────

@mcp.tool()
async def wikijs_create_page(
    title: str,
    content: str,
    path: str = "",
    description: str = "",
    tags: List[str] = None,
    parent_id: int = None,
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
    """
    try:
        async with WikiJSClient() as c:
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
                "locale": "en",
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
        return json.dumps({"error": str(e)})


@mcp.tool()
async def wikijs_get_page(page_id: int = None, path: str = None, locale: str = "en") -> str:
    """
    Get a Wiki.js page by ID or path.

    Args:
        page_id: Page ID (use either page_id OR path)
        path: Page path e.g. 'infra/proxmox' (use either page_id OR path)
        locale: Locale (default: en)
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
                data = await c.query(q, {"path": path, "locale": locale})
                pg = data.get("pages", {}).get("singleByPath")

            if not pg:
                return json.dumps({"error": "Page not found"})

            return json.dumps({
                "pageId": pg["id"],
                "title": pg["title"],
                "path": pg["path"],
                "content": pg["content"],
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
        return json.dumps({"error": str(e)})


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
        return json.dumps({"error": str(e)})


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
                        mapping = db.query(FileMapping).filter(FileMapping.page_id == page_id).first()
                        if mapping:
                            db.delete(mapping)
                            result["file_mapping_removed"] = True
                return json.dumps(result)
            return json.dumps({"error": rr.get("message", "Unknown error")})

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def wikijs_move_page(
    page_id: int,
    destination_path: str,
    destination_locale: str = "en",
) -> str:
    """
    Move a Wiki.js page to a new path.

    Args:
        page_id: ID of the page to move
        destination_path: New path (e.g. 'infra/archive/old-page')
        destination_locale: New locale (default: en)
    """
    try:
        current_raw = await wikijs_get_page(page_id=page_id)
        current = json.loads(current_raw)
        if "error" in current:
            return current_raw

        async with WikiJSClient() as c:
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
        return json.dumps({"error": str(e)})


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
                data = await c.query(
                    """query($query:String!){pages{search(query:$query,path:"",locale:"en"){
                        results{id title description path locale} totalHits
                    }}}""",
                    {"query": query},
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
        return json.dumps({"error": str(e)})


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
        return json.dumps({"error": str(e)})


@mcp.tool()
async def wikijs_get_tree(
    parent_path: str = "",
    mode: str = "ALL",
    locale: str = "en",
    parent_id: int = None,
) -> str:
    """
    Get the Wiki.js page tree structure.

    Args:
        parent_path: Root path to start from (empty = full tree)
        mode: ALL | FOLDERS | PAGES (default: ALL)
        locale: Locale filter (default: en)
        parent_id: Parent page ID (optional)
    """
    try:
        async with WikiJSClient() as c:
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
        return json.dumps({"error": str(e)})


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
        return json.dumps({"error": str(e)})


@mcp.tool()
async def wikijs_create_nested_page(
    title: str,
    content: str,
    parent_path: str,
    create_parent_if_missing: bool = True,
) -> str:
    """
    Create a page nested under a given path, creating parent pages if needed.

    Args:
        title: New page title
        content: Markdown content
        parent_path: Full parent path (e.g. 'infra/proxmox')
        create_parent_if_missing: Auto-create missing parent pages
    """
    try:
        parent_raw = await wikijs_get_page(path=parent_path)
        parent = json.loads(parent_raw)

        if "error" in parent:
            if not create_parent_if_missing:
                return json.dumps({"error": f"Parent '{parent_path}' not found"})

            parts = parent_path.split("/")
            current = ""
            for part in parts:
                current = f"{current}/{part}".lstrip("/")
                check_raw = await wikijs_get_page(path=current)
                check = json.loads(check_raw)
                if "error" in check:
                    part_title = part.replace("-", " ").title()
                    cr = json.loads(await wikijs_create_page(
                        title=part_title,
                        content=f"# {part_title}\n\n*Auto-created section.*",
                        path=current,
                    ))
                    if "error" in cr:
                        return json.dumps({"error": f"Failed to create parent '{current}': {cr['error']}"})

        full_path = f"{parent_path}/{slugify(title)}"
        result_raw = await wikijs_create_page(title=title, content=content, path=full_path)
        result = json.loads(result_raw)
        if "error" not in result:
            result["parent_path"] = parent_path
            result["full_path"] = full_path
        return json.dumps(result)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def wikijs_create_repo_structure(
    repo_name: str,
    description: str = None,
    sections: List[str] = None,
) -> str:
    """
    Create a complete documentation structure for a repository/project.

    Args:
        repo_name: Repository name (becomes root page)
        description: Short description of the project
        sections: Section names to create (default: Overview, Architecture, API, Development, Deployment)
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

        root_raw = await wikijs_create_page(title=repo_name, content=root_content, path=root_path)
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
            sec_raw = await wikijs_create_page(title=section, content=sec_content, path=sec_path)
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
        return json.dumps({"error": str(e)})


@mcp.tool()
async def wikijs_create_documentation_hierarchy(
    project_name: str,
    file_mappings: List[Dict[str, str]],
    auto_organize: bool = True,
) -> str:
    """
    Build a full documentation hierarchy for a project from a list of files.

    Args:
        project_name: Project name (root page)
        file_mappings: List of {"file_path": "src/foo.py"} dicts
        auto_organize: Auto-categorize files into components/api/utils/etc.
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
        repo_raw = await wikijs_create_repo_structure(project_name, sections=active_sections)
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
                )
                ov = json.loads(ov_raw)
                if "error" not in ov:
                    created_pages.append(ov)
                    with get_db() as db:
                        mapping = FileMapping(
                            file_path=fp,
                            page_id=ov["pageId"],
                            relationship_type="documents",
                            file_hash=get_file_hash(fp),
                            repository_root=find_repo_root(fp) or "",
                        )
                        db.merge(mapping)

        return json.dumps({
            "project": project_name,
            "root": repo,
            "pages_created": len(created_pages),
            "auto_organized": auto_organize,
            "status": "completed",
        })

    except Exception as e:
        return json.dumps({"error": str(e)})


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
        return json.dumps({"error": str(e)})


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
        return json.dumps({"error": str(e)})


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
        return json.dumps({"error": str(e)})


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
            mapping = db.query(FileMapping).filter(FileMapping.file_path == file_path).first()
            if mapping:
                mapping.page_id = page_id
                mapping.relationship_type = relationship
                mapping.file_hash = fh
                mapping.last_updated = datetime.datetime.now(UTC)
            else:
                db.add(FileMapping(
                    file_path=file_path, page_id=page_id,
                    relationship_type=relationship, file_hash=fh, repository_root=repo or "",
                ))
        return json.dumps({"linked": True, "file_path": file_path, "page_id": page_id, "relationship": relationship})

    except Exception as e:
        return json.dumps({"error": str(e)})


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
            mapping = db.query(FileMapping).filter(FileMapping.file_path == file_path).first()
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
            mapping = db.query(FileMapping).filter(FileMapping.file_path == file_path).first()
            if mapping:
                mapping.file_hash = get_file_hash(file_path)
                mapping.last_updated = datetime.datetime.now(UTC)

        return json.dumps({"synced": True, "file_path": file_path, "page_id": page_id, "change_summary": change_summary})

    except Exception as e:
        return json.dumps({"error": str(e)})


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
        return json.dumps({"error": str(e)})


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
                    mapping = db.query(FileMapping).filter(FileMapping.file_path == fp).first()
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
        return json.dumps({"error": str(e)})


@mcp.tool()
async def wikijs_cleanup_orphaned_mappings() -> str:
    """Remove local file→page mappings whose Wiki.js page no longer exists."""
    try:
        with get_db() as db:
            mappings = db.query(FileMapping).all()
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
        return json.dumps({"error": str(e)})


@mcp.tool()
async def wikijs_repository_context() -> str:
    """Show current repository context and file→page mappings."""
    try:
        repo_root = find_repo_root()
        with get_db() as db:
            mappings = db.query(FileMapping).filter(FileMapping.repository_root == repo_root).all()
            result = {
                "repository_root": repo_root,
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
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_http() -> None:
    """Run as HTTP server (Docker/remote)."""
    settings.validate_config()
    logger.info(f"wiki-js-mcp-server HTTP mode — {settings.HTTP_HOST}:{settings.HTTP_PORT}")
    app = mcp.streamable_http_app()
    config = uvicorn.Config(app=app, host=settings.HTTP_HOST, port=settings.HTTP_PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def run_stdio() -> None:
    """Run as stdio server (Claude Desktop local)."""
    settings.validate_config()
    logger.info("wiki-js-mcp-server stdio mode")
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
