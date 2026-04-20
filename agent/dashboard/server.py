"""aiohttp Application factory + route handlers for the dashboard.

build_app returns an aiohttp.web.Application. The caller is responsible
for running it (either via AppRunner in the agent's asyncio loop or via
aiohttp.web.run_app for tests). Handlers open their own per-request
read-only SQLite connections via the conn_factory passed at build time,
so the agent's writer connection is never shared across the event loop."""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent.dashboard import queries, share

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _make_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _ago(ts: int | None, now: int | None = None) -> str:
    if ts is None:
        return "never"
    delta = (now or int(time.time())) - ts
    if delta < 60:    return f"{delta}s ago"
    if delta < 3600:  return f"{delta // 60}m ago"
    if delta < 86400: return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _flash_from_query(request: web.Request) -> dict | None:
    q = request.query
    if q.get("sent") == "1":
        return {"level": "ok", "message": "✓ share email sent."}
    err = q.get("sent_error")
    if err:
        return {"level": "err", "message": f"✗ share failed: {err}"}
    return None


def _base_context(request: web.Request) -> dict:
    conn = request.app["conn_factory"]()
    try:
        data = queries.landing_data(conn)
    finally:
        conn.close()
    return {
        "kpis": data.kpis,
        "hostname": request.app["hostname"],
        "last_osv_ingest_label": _ago(data.last_osv_ingest),
        "last_walk_label": _ago(data.last_github_walk),
        "flash": _flash_from_query(request),
        "_landing_data": data,  # convenience for handlers that want it
    }


async def index_view(request: web.Request) -> web.Response:
    ctx = _base_context(request)
    data = ctx.pop("_landing_data")
    template = request.app["jinja"].get_template("index.html")
    html = template.render(
        triage=data.triage,
        top_projects=data.top_projects,
        top_advisories=data.top_advisories,
        **ctx,
    )
    return web.Response(text=html, content_type="text/html")


def build_app(
    *,
    conn_factory: Callable[[], sqlite3.Connection],
    share_config: share.ShareConfig,
    hostname: str = "",
) -> web.Application:
    app = web.Application()
    app["conn_factory"] = conn_factory
    app["share_config"] = share_config
    app["hostname"] = hostname or "dashboard"
    app["jinja"] = _make_env()

    app.router.add_get("/", index_view)
    app.router.add_static("/static/", STATIC_DIR, follow_symlinks=False)
    return app
