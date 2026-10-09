import asyncio
import hashlib
import hmac
import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:abc")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "s3cret")

from bot.config import Config  # noqa: E402
from bot.db import DB  # noqa: E402
from bot.features import FeatureMapper, group_commits  # noqa: E402
from bot.github import parse_push, verify_signature  # noqa: E402
from bot.messages import render_report, report_keyboard  # noqa: E402
from bot.pushflow import process_push  # noqa: E402
from bot.services import AppServices  # noqa: E402


def payload(branch="frontend", login="noormansetiawan"):
    msgs = [
        "feat(pengadaan): merge form tambah & edit ke index.html (SPA View Switch) & hapus file redundan",
        "feat(nav): replace search button with Kasir / POS in mobile bottom nav across submodules",
        "feat(pengadaan): generate 65 realistic dummy procurement orders with complete item details",
        "fix(products): handle missing update timestamps in list",
        "chore: tidy <script> tags",
    ]
    return {
        "ref": f"refs/heads/{branch}",
        "compare": "https://github.com/infruity/infruity-erp/compare/aaa...bbb",
        "repository": {"full_name": "infruity/infruity-erp"},
        "sender": {"login": login},
        "pusher": {"name": login},
        "head_commit": {"id": "ddfb0daf081121aacff0b53156a9d1e0c3b78999"},
        "commits": [
            {"id": f"{i:07d}abcdef", "message": m, "url": f"https://github.com/c/{i}",
             "distinct": True, "added": [], "removed": [], "modified": ["a.html"]}
            for i, m in enumerate(msgs)
        ],
    }


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return SimpleNamespace(chat_id=chat_id, message_id=len(self.sent) + 100)


class FakeGoogle:
    def __init__(self):
        self.logs, self.notes = [], []

    def append_log(self, values):
        self.logs.append(values)
        return len(self.logs) + 1

    def update_plan_note(self, feature, role, text):
        self.notes.append((feature, role, text))
        return True


@pytest.fixture
def svc(tmp_path):
    cfg = Config.from_env()
    db = DB(tmp_path / "t.db")
    db.set_setting("group_chat_id", "-100123")
    return AppServices(cfg=cfg, db=db, mapper=FeatureMapper(cfg.aliases_file), google=FakeGoogle())


def test_signature():
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_signature("s3cret", body, sig)
    assert not verify_signature("s3cret", body, "sha256=00")
    assert not verify_signature("s3cret", body, None)


def test_aliases_match_sheet():
    plan = json.load(open(os.path.join(os.path.dirname(__file__), "plan_features.json")))
    m = FeatureMapper(Config.from_env().aliases_file)
    assert set(m.features) == set(plan)


def test_mapping_and_grouping(svc):
    evt = parse_push(payload())
    assert evt.branch == "frontend" and evt.pusher_login == "noormansetiawan"
    groups = group_commits(evt.commits, svc.mapper)
    names = [g["feature"] for g in groups]
    assert names == ["Pengadaan", "Navbar", "Produk", "Umum"]
    assert len(groups[0]["commits"]) == 2
    assert svc.mapper.search("pengadaan") == ["Pengadaan"]
    assert "Kasir-POS" in svc.mapper.search("kasir")


def test_role_for_branch(svc):
    assert svc.cfg.role_for_branch("frontend") == "fe"
    assert svc.cfg.role_for_branch("backend") == "be"
    assert svc.cfg.role_for_branch("random") is None


def test_process_push_unregistered_and_registered(svc):
    bot = FakeBot()
    pid = asyncio.run(process_push(svc, bot, parse_push(payload()), "d1"))
    push = svc.db.get_push(pid)
    assert push["video_status"] == "pending"
    assert push["message_id"] and push["chat_id"] == -100123
    assert svc.google.logs[0][5] == "Pengadaan, Navbar, Produk"
    assert ("Pengadaan", "fe") == svc.google.notes[0][:2]
    report = bot.sent[0][1]
    assert "belum terdaftar" in report and "&lt;script&gt;" in report

    # registrasi Noorman + Maulana -> push lama tertaut, laporan mention lawan
    svc.db.upsert_user(1, "noorman", "Achmad Noorman", "noormansetiawan", "fe", "active")
    svc.db.upsert_user(2, "maulana", "Maulana", "maulanairawan", "be", "active")
    assert svc.db.assign_pushes("noormansetiawan", 1) == 1
    push = svc.db.get_push(pid)
    text = render_report(push, svc.pusher_of(push), svc.counterparts_of(push), svc.cfg)
    print("\n" + text)
    assert "@maulana" in text and "Blade" in text and "@noorman" in text
    kb = report_keyboard(push)
    assert kb.inline_keyboard[0][0].callback_data == f"vid:{pid}"
    assert svc.db.pending_pushes_for(1)[0]["id"] == pid


def test_fix_only_push_needs_no_video(svc):
    p = payload(branch="backend", login="maulanairawan")
    p["commits"] = [p["commits"][3]]
    pid = asyncio.run(process_push(svc, FakeBot(), parse_push(p), "d2"))
    push = svc.db.get_push(pid)
    assert push["role"] == "be" and push["video_status"] == "not_needed"
    assert report_keyboard(push).inline_keyboard[0][0].url


def test_release_and_ignored(svc):
    bot = FakeBot()
    pid = asyncio.run(process_push(svc, bot, parse_push(payload(branch="main")), "d3"))
    assert svc.db.get_push(pid)["role"] == "release"
    assert "MAIN diperbarui" in bot.sent[-1][1]
    assert asyncio.run(process_push(svc, bot, parse_push(payload(branch="feature-x")), "d4")) is None


def test_webhook_endpoint(svc):
    from aiohttp.test_utils import TestClient, TestServer
    from bot.webhook import build_web_app

    bot = FakeBot()

    async def go():
        client = TestClient(TestServer(build_web_app(svc, bot)))
        await client.start_server()
        body = json.dumps(payload()).encode()
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        h = {"X-GitHub-Event": "push", "X-GitHub-Delivery": "abc", "X-Hub-Signature-256": sig}
        r1 = await client.post("/github/webhook", data=body, headers=h)
        r_bad = await client.post("/github/webhook", data=body, headers={**h, "X-Hub-Signature-256": "sha256=x"})
        await asyncio.sleep(0.2)
        r_dup = await client.post("/github/webhook", data=body, headers=h)
        dup = await r_dup.json()
        await client.close()
        return r1.status, r_bad.status, dup

    ok, bad, dup = asyncio.run(go())
    assert ok == 202 and bad == 401 and dup.get("duplicate")
    assert len(bot.sent) == 1


def test_application_builds():
    from bot.__main__ import build_application
    from bot.handlers import register_handlers
    app = build_application(Config.from_env())
    register_handlers(app)
    assert app.job_queue is not None
