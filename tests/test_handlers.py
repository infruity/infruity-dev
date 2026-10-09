"""Uji alur handler dengan objek Telegram tiruan (tanpa jaringan)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import handlers as H
from bot.github import parse_push
from bot.pushflow import process_push
from tests.test_core import FakeBot, payload, svc  # noqa: F401  (fixture)

ADMIN = 999


def make_ctx(svc, user_data=None, args=None):
    app = SimpleNamespace(bot_data={"svc": svc})
    bot = MagicMock()
    bot.username = "infruity_dev_bot"
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    return SimpleNamespace(application=app, bot=bot, user_data=user_data if user_data is not None else {},
                           args=args or [])


def make_msg(chat_id, text=None, reply_to=None):
    m = MagicMock()
    m.chat_id = chat_id
    m.text = text
    m.reply_to_message = SimpleNamespace(message_id=reply_to) if reply_to else None
    m.reply_html = AsyncMock(return_value=SimpleNamespace(message_id=555, edit_text=AsyncMock()))
    return m


def make_update(uid, chat_type="private", chat_id=None, msg=None, data=None, username="noorman", name="Achmad Noorman"):
    user = SimpleNamespace(id=uid, username=username, full_name=name, first_name=name.split()[0])
    chat = SimpleNamespace(id=chat_id or uid, type=chat_type, title="Infruity Dev")
    q = None
    if data is not None:
        q = MagicMock()
        q.data = data
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        q.edit_message_reply_markup = AsyncMock()
        q.message = make_msg(chat.id)
        q.message.text_html = "🔔 req"
    return SimpleNamespace(effective_user=user, effective_chat=chat, effective_message=msg,
                           callback_query=q)


@pytest.fixture
def s(svc):  # noqa: F811
    object.__setattr__(svc.cfg, "admin_ids", frozenset({ADMIN}))
    return svc


def test_registration_needs_admin_approval(s, monkeypatch):
    monkeypatch.setattr(H, "github_lookup", AsyncMock(return_value=("ok", "Achmad Noorman", "noormansetiawan")))
    ud = {}
    ctx = make_ctx(s, ud)
    asyncio.run(H.cmd_start(make_update(1, msg=make_msg(1, "/start")), ctx))
    asyncio.run(H.cb_reg_role(make_update(1, data="reg:fe"), ctx))
    assert ud["reg"] == {"role": "fe", "step": "github"}
    m = make_msg(1, "https://github.com/noormansetiawan")
    asyncio.run(H.on_private_text(make_update(1, msg=m), ctx))
    assert ud["reg"]["github"] == "noormansetiawan"
    assert "Cek data kamu" in m.reply_html.call_args.args[0]

    asyncio.run(H.cb_reg_confirm(make_update(1, data="regok"), ctx))
    assert s.db.get_user(1)["status"] == "pending"
    # admin dapat DM berisi tombol approve
    sent = ctx.bot.send_message.call_args
    assert sent.args[0] == ADMIN and "Permintaan registrasi" in sent.args[1]

    # non-admin tidak bisa approve
    upd = make_update(2, data="appr:1")
    asyncio.run(H.cb_approval(upd, make_ctx(s)))
    assert s.db.get_user(1)["status"] == "pending"
    asyncio.run(H.cb_approval(make_update(ADMIN, data="appr:1", name="Maulana Irawan"), make_ctx(s)))
    assert s.db.get_user(1)["status"] == "active"


def _registered_push(s):
    s.db.upsert_user(1, "noorman", "Achmad Noorman", "noormansetiawan", "fe", "active")
    s.db.upsert_user(ADMIN, "maulana", "Maulana", "maulanairawan", "be", "active")
    pid = asyncio.run(process_push(s, FakeBot(), parse_push(payload()), "dx"))
    return s.db.get_push(pid)


def test_video_button_permissions_and_prompt(s):
    push = _registered_push(s)
    # orang lain (bukan pelaku push / admin) ditolak
    upd = make_update(3, chat_type="supergroup", chat_id=-100123, data=f"vid:{push['id']}", username="x", name="X Y")
    asyncio.run(H.cb_video(upd, make_ctx(s)))
    assert "Tombol ini untuk" in upd.callback_query.answer.call_args.args[0]
    # Noorman -> prompt ForceReply, prompt id tersimpan
    ud = {}
    upd = make_update(1, chat_type="supergroup", chat_id=-100123, data=f"vid:{push['id']}")
    asyncio.run(H.cb_video(upd, make_ctx(s, ud)))
    assert ud["await_push"] == push["id"]
    assert s.db.get_push(push["id"])["prompt_message_id"] == 555


def test_link_reply_marks_video_done(s):
    push = _registered_push(s)
    # admin membalas laporan dengan link biasa -> BUKAN video
    m = make_msg(-100123, "cek ini https://example.com", reply_to=push["message_id"])
    upd = make_update(ADMIN, chat_type="supergroup", chat_id=-100123, msg=m, username="maulana", name="Maulana I")
    assert asyncio.run(H.maybe_video_link(upd, make_ctx(s))) is False
    # pelaku push membalas dengan link -> tercatat
    m = make_msg(-100123, "https://drive.google.com/file/d/xyz/view", reply_to=push["message_id"])
    ctx = make_ctx(s)
    assert asyncio.run(H.maybe_video_link(make_update(1, chat_type="supergroup", chat_id=-100123, msg=m), ctx))
    p = s.db.get_push(push["id"])
    assert p["video_status"] == "done" and "drive.google.com" in p["video_link"]
    ctx.bot.edit_message_text.assert_awaited()  # laporan di-update


def test_too_big_video_rejected(s):
    push = _registered_push(s)
    m = make_msg(-100123, reply_to=push["message_id"])
    m.video = SimpleNamespace(file_id="F", file_size=50 * 1024 * 1024, mime_type="video/mp4", file_name="a.mp4")
    m.document = None
    asyncio.run(H.on_video(make_update(1, chat_type="supergroup", chat_id=-100123, msg=m), make_ctx(s)))
    assert "melebihi batas" in m.reply_html.call_args.args[0]


def test_selesai_flow(s):
    _registered_push(s)
    s.google.set_plan_done = lambda f, r, d: True
    m = make_msg(1, "/selesai")
    asyncio.run(H.cmd_selesai(make_update(1, msg=m), make_ctx(s, args=["pengadaan"])))
    kb = m.reply_html.call_args.kwargs["reply_markup"]
    data = kb.inline_keyboard[0][0].callback_data
    assert data.startswith("dok:")
    upd = make_update(1, data=data)
    ctx = make_ctx(s)
    asyncio.run(H.cb_done(upd, ctx))
    assert "ditandai selesai" in upd.callback_query.edit_message_text.call_args.args[0]
    group_msg = ctx.bot.send_message.call_args.args[1]
    assert "Pengadaan" in group_msg and "@maulana" in group_msg
