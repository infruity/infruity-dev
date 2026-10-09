"""Uji mode polling dengan server GitHub API tiruan."""
import asyncio
import dataclasses

from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.poller import GitHubPoller
from tests.test_core import FakeBot, svc  # noqa: F401

REPO = "infruity/infruity-erp"


def fake_github(state):
    async def branches(req):
        etag = f'"{state["backend"]}{state["frontend"]}"'
        state["calls"].append("branches")
        if req.headers.get("If-None-Match") == etag:
            return web.Response(status=304)
        return web.json_response(
            [{"name": "backend", "commit": {"sha": state["backend"]}},
             {"name": "frontend", "commit": {"sha": state["frontend"]}},
             {"name": "random", "commit": {"sha": "zzz"}}],
            headers={"ETag": etag})

    async def compare(req):
        spec = req.match_info["spec"]
        state["calls"].append("compare:" + spec)
        return web.json_response({
            "status": "ahead",
            "html_url": f"https://github.com/{REPO}/compare/{spec}",
            "commits": [
                {"sha": "b2" * 20, "html_url": "https://github.com/c/b2",
                 "commit": {"message": "feat(produk): tes notifikasi bot telegram"},
                 "author": None},  # email tidak tertaut ke akun GitHub
            ],
        })

    app = web.Application()
    app.router.add_get(f"/repos/{REPO}/branches", branches)
    app.router.add_get(f"/repos/{REPO}/compare/{{spec}}", compare)
    return app


def test_poll_detects_new_push_and_maps_by_branch(svc):  # noqa: F811
    svc.db.upsert_user(1, "noorman", "Achmad Noorman", "noormansetiawan", "fe", "active")
    svc.db.upsert_user(2, "maulana", "Maulana", "maulanairawan", "be", "active")
    state = {"backend": "a1" * 20, "frontend": "f1" * 20, "calls": []}
    bot = FakeBot()

    async def go():
        server = TestServer(fake_github(state))
        await server.start_server()
        svc.cfg = dataclasses.replace(svc.cfg, github_api_url=str(server.make_url("")).rstrip("/"))
        poller = GitHubPoller(svc, bot)
        await poller.tick()                       # run pertama: hanya mencatat posisi
        assert bot.sent == []
        await poller.tick()                       # tidak berubah -> 304
        assert state["calls"][-1] == "branches" and not any(c.startswith("compare") for c in state["calls"])
        state["backend"] = "b2" * 20              # Maulana push ke backend
        await poller.tick()
        await poller.tick()                       # tidak dobel
        await poller.close()
        await server.close()

    asyncio.run(go())
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "BACKEND" in text and "Maulana" in text and "Produk" in text and "@noorman" in text
    push = svc.db.get_push(1)
    assert push["telegram_id"] == 2 and push["video_status"] == "pending"
    assert push["compare_url"].endswith("a1" * 20 + "..." + "b2" * 20)
    assert sum(c.startswith("compare") for c in state["calls"]) == 1
