"""HTTP server untuk menerima webhook GitHub."""
from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from .github import parse_push, verify_signature
from .pushflow import process_push
from .services import AppServices

log = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()


def build_web_app(svc: AppServices, bot) -> web.Application:
    cfg = svc.cfg

    async def health(_req: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def github(req: web.Request) -> web.Response:
        body = await req.read()
        if not verify_signature(cfg.github_secret, body, req.headers.get("X-Hub-Signature-256")):
            log.warning("Signature webhook tidak valid dari %s", req.remote)
            return web.json_response({"error": "invalid signature"}, status=401)

        event = req.headers.get("X-GitHub-Event", "")
        delivery = req.headers.get("X-GitHub-Delivery", "")
        if event == "ping":
            return web.json_response({"ok": True, "pong": True})
        if event != "push":
            return web.json_response({"ignored": event}, status=202)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.json_response({"error": "bad json"}, status=400)

        evt = parse_push(payload)
        if cfg.repo_full_name and evt.repo and evt.repo != cfg.repo_full_name:
            return web.json_response({"ignored": "repo"}, status=202)
        if delivery and svc.db.delivery_seen(delivery):
            return web.json_response({"ok": True, "duplicate": True})

        async def run():
            try:
                await process_push(svc, bot, evt, delivery)
            except Exception:
                log.exception("Gagal memproses push %s", delivery)

        task = asyncio.create_task(run())
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return web.json_response({"ok": True}, status=202)

    app = web.Application(client_max_size=25 * 1024 * 1024)
    app.router.add_get("/health", health)
    app.router.add_post(cfg.webhook_path, github)
    return app
