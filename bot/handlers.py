"""Handler Telegram: registrasi, video demo, /selesai, /pending, admin."""
from __future__ import annotations

import logging
import mimetypes
import re
import shutil
import tempfile
import time
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from telegram import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, ForceReply,
    InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, ReplyParameters, Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

from .config import COUNTERPART, ROLE_EMOJI, ROLE_LABELS
from .features import norm
from .messages import (
    VIDEO_LABEL, feature_list, features_text, message_link, mention, push_label,
)
from .services import AppServices, announce, get_svc, notify_admins, refresh_report

log = logging.getLogger(__name__)

GH_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
URL_RE = re.compile(r"https?://\S+")
HTML = ParseMode.HTML


# ======================================================================
# Teks
# ======================================================================
def help_text(user: dict | None, is_admin: bool) -> str:
    lines = ["<b>📖 Perintah bot</b>", ""]
    lines += [
        "/pending — push yang masih menunggu video",
        "/selesai — tandai fitur selesai di sheet Plan",
        "/start — status registrasi",
        "/reset — hapus registrasi & daftar ulang",
    ]
    if is_admin:
        lines += ["", "<b>Admin</b>", "/users — daftar developer", "/setgroup — jadikan grup ini tujuan laporan"]
    lines += [
        "", "<b>Cara kirim video demo</b>",
        "Tekan <b>📹 Kirim video</b> di laporan push, lalu reply dengan video.",
        "Video terlalu besar? Reply dengan link Drive/YouTube.",
    ]
    return "\n".join(lines)


def welcome_text(first_name: str) -> str:
    return (
        f"👋 <b>Halo, {escape(first_name)}!</b>\n\n"
        "Aku bot jembatan <b>FE ↔ BE</b> untuk Infruity ERP.\n"
        "Setiap push ke GitHub aku laporkan ke grup, catat ke sheet, "
        "dan bantu simpan video demo ke Drive.\n\n"
        "<b>Langkah 1/2</b> — Kamu pegang bagian apa?"
    )


def role_keyboard(svc: AppServices) -> Kb:
    return Kb([
        [Btn(f"{ROLE_EMOJI[r]} {ROLE_LABELS[r]} · branch {svc.cfg.branch_for_role(r)}",
             callback_data=f"reg:{r}")]
        for r in ("fe", "be")
    ])


def registered_text(user: dict, svc: AppServices) -> str:
    r = user["role"]
    return (
        f"✅ Kamu terdaftar sebagai <b>{ROLE_EMOJI[r]} {ROLE_LABELS[r]}</b>\n"
        f"GitHub: <code>{escape(user['github_login'])}</code> · branch "
        f"<code>{escape(svc.cfg.branch_for_role(r))}</code>\n\n"
        + help_text(user, svc.cfg.is_admin(user["telegram_id"]))
    )


# ======================================================================
# Registrasi
# ======================================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_svc(ctx)
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type != ChatType.PRIVATE:
        url = f"https://t.me/{ctx.bot.username}?start=daftar"
        await msg.reply_html("👋 Registrasi dilakukan lewat chat pribadi dengan bot.",
                             reply_markup=Kb([[Btn("💬 Buka chat bot", url=url)]]))
        return
    row = svc.db.get_user(user.id)
    if row and row["status"] == "active":
        await msg.reply_html(registered_text(row, svc))
        return
    if row and row["status"] == "pending":
        await msg.reply_html("⏳ Registrasi kamu masih <b>menunggu persetujuan admin</b>.\n"
                             "Aku kabari begitu disetujui.")
        return
    ctx.user_data.pop("reg", None)
    await msg.reply_html(welcome_text(user.first_name), reply_markup=role_keyboard(svc))


async def cb_reg_role(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    role = q.data.split(":", 1)[1]
    ctx.user_data["reg"] = {"role": role, "step": "github"}
    await q.answer()
    await q.edit_message_text(
        f"{ROLE_EMOJI[role]} <b>{ROLE_LABELS[role]}</b> dipilih.\n\n"
        "<b>Langkah 2/2</b> — Kirim <b>GitHub username</b> kamu.\n"
        "<i>Akun yang dipakai untuk push ke repo, mis. <code>noormansetiawan</code></i>",
        parse_mode=HTML,
    )


async def github_lookup(login: str) -> tuple[str, str | None, str]:
    """Return (status, nama, login_kanonik). status: ok | notfound | unknown"""
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"https://api.github.com/users/{login}",
                             headers={"User-Agent": "infruity-devbot"}) as r:
                if r.status == 404:
                    return "notfound", None, login
                if r.status == 200:
                    data = await r.json()
                    return "ok", data.get("name"), data.get("login") or login
    except Exception as e:  # jaringan bermasalah -> tetap lanjut
        log.info("Lookup GitHub gagal: %s", e)
    return "unknown", None, login


async def handle_github_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE, reg: dict) -> None:
    svc = get_svc(ctx)
    msg, user = update.effective_message, update.effective_user
    raw = (msg.text or "").strip()
    m = re.search(r"github\.com/([A-Za-z0-9-]+)", raw)
    login = m.group(1) if m else raw.lstrip("@").strip()
    if not GH_RE.match(login):
        await msg.reply_html("❌ Format username tidak valid.\nContoh: <code>noormansetiawan</code>")
        return
    if svc.db.github_taken(login, user.id):
        await msg.reply_html(f"❌ GitHub <code>{escape(login)}</code> sudah dipakai akun Telegram lain.\n"
                             "Hubungi admin kalau ini keliru.")
        return
    status, name, login = await github_lookup(login)
    if status == "notfound":
        await msg.reply_html(f"❌ Akun GitHub <code>{escape(login)}</code> tidak ditemukan. Cek ejaannya lagi.")
        return
    reg.update(github=login, step="confirm")
    role = reg["role"]
    found = f" ({escape(name)})" if name else ""
    await msg.reply_html(
        "📋 <b>Cek data kamu</b>\n\n"
        f"👤 Telegram: {escape(user.full_name)}" + (f" (@{escape(user.username)})" if user.username else "") + "\n"
        f"🐙 GitHub: <code>{escape(login)}</code>{found}\n"
        f"{ROLE_EMOJI[role]} Role: <b>{ROLE_LABELS[role]}</b> → branch <code>{escape(svc.cfg.branch_for_role(role))}</code>",
        reply_markup=Kb([[Btn("✅ Daftarkan", callback_data="regok"),
                          Btn("✏️ Ubah", callback_data="regedit")]]),
    )


async def cb_reg_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_svc(ctx)
    q, user = update.callback_query, update.effective_user
    reg = ctx.user_data.get("reg")
    if q.data == "regedit":
        ctx.user_data.pop("reg", None)
        await q.answer()
        await q.edit_message_text(welcome_text(user.first_name), parse_mode=HTML, reply_markup=role_keyboard(svc))
        return
    if not reg or reg.get("step") != "confirm":
        await q.answer("Sesi registrasi habis. Ketik /start lagi.", show_alert=True)
        return

    admin = svc.cfg.is_admin(user.id)
    role, login = reg["role"], reg["github"]
    svc.db.upsert_user(user.id, user.username, user.full_name, login, role, "active" if admin else "pending")
    ctx.user_data.pop("reg", None)
    await q.answer()

    if admin:
        linked = svc.db.assign_pushes(login, user.id)
        row = svc.db.get_user(user.id)
        await q.edit_message_text("🎉 <b>Terdaftar!</b>" + (f" {linked} push lama ditautkan." if linked else "")
                                  + "\n\n" + registered_text(row, svc), parse_mode=HTML)
        await announce(ctx.bot, svc, f"👋 {mention(row)} bergabung sebagai {ROLE_EMOJI[role]} <b>{ROLE_LABELS[role]}</b>")
        return

    await q.edit_message_text("📨 <b>Permintaan terkirim.</b>\nMenunggu persetujuan admin — aku kabari di sini.",
                              parse_mode=HTML)
    await notify_admins(
        ctx.bot, svc,
        "🔔 <b>Permintaan registrasi</b>\n\n"
        f"👤 {escape(user.full_name)}" + (f" (@{escape(user.username)})" if user.username else "") + "\n"
        f"🐙 GitHub: <code>{escape(login)}</code>\n"
        f"{ROLE_EMOJI[role]} {ROLE_LABELS[role]} → branch <code>{escape(svc.cfg.branch_for_role(role))}</code>",
        reply_markup=Kb([[Btn("✅ Setujui", callback_data=f"appr:{user.id}"),
                          Btn("❌ Tolak", callback_data=f"rej:{user.id}")]]),
    )


async def cb_approval(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_svc(ctx)
    q, admin = update.callback_query, update.effective_user
    if not svc.cfg.is_admin(admin.id):
        await q.answer("Hanya admin.", show_alert=True)
        return
    action, tid = q.data.split(":")
    target = svc.db.get_user(int(tid))
    if not target or target["status"] != "pending":
        await q.answer("Sudah diproses.")
        await q.edit_message_reply_markup(None)
        return
    await q.answer()
    original = q.message.text_html or ""
    if action == "appr":
        svc.db.set_user_status(target["telegram_id"], "active")
        linked = svc.db.assign_pushes(target["github_login"], target["telegram_id"])
        target = svc.db.get_user(target["telegram_id"])
        await q.edit_message_text(original + f"\n\n✅ Disetujui oleh {escape(admin.first_name)}", parse_mode=HTML)
        try:
            await ctx.bot.send_message(
                target["telegram_id"],
                "🎉 <b>Registrasi disetujui!</b>" + (f" {linked} push lama ditautkan." if linked else "")
                + "\n\n" + registered_text(target, svc), parse_mode=HTML)
        except TelegramError:
            pass
        r = target["role"]
        await announce(ctx.bot, svc, f"👋 {mention(target)} bergabung sebagai {ROLE_EMOJI[r]} <b>{ROLE_LABELS[r]}</b>")
    else:
        svc.db.delete_user(target["telegram_id"])
        await q.edit_message_text(original + f"\n\n❌ Ditolak oleh {escape(admin.first_name)}", parse_mode=HTML)
        try:
            await ctx.bot.send_message(target["telegram_id"], "❌ Registrasi ditolak admin. Ketik /start untuk mencoba lagi.")
        except TelegramError:
            pass


# ======================================================================
# Video demo
# ======================================================================
def can_submit(svc: AppServices, push: dict, uid: int) -> bool:
    return push.get("telegram_id") == uid or svc.cfg.is_admin(uid)


async def _push_from_callback(update: Update, ctx) -> tuple[AppServices, dict | None]:
    svc = get_svc(ctx)
    q = update.callback_query
    push = svc.db.get_push(int(q.data.split(":")[1]))
    if not push:
        await q.answer("Push tidak ditemukan.", show_alert=True)
        return svc, None
    if push["video_status"] != "pending":
        await q.answer(f"Video sudah: {VIDEO_LABEL[push['video_status']]}.", show_alert=True)
        return svc, None
    if not can_submit(svc, push, update.effective_user.id):
        owner = svc.pusher_of(push)
        who = (owner or {}).get("full_name") or push.get("github_login") or "pelaku push"
        await q.answer(f"Tombol ini untuk {who} 🙂", show_alert=True)
        return svc, None
    return svc, push


async def cb_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc, push = await _push_from_callback(update, ctx)
    if not push:
        return
    q, user = update.callback_query, update.effective_user
    u = svc.db.get_user(user.id) or {"telegram_id": user.id, "tg_username": user.username, "full_name": user.full_name}
    sent = await q.message.reply_html(
        f"📹 {mention(u)} kirim video demo untuk push <code>{push['head_sha'][:7]}</code> · "
        f"<b>{escape(features_text(push['groups']))}</b>\n\n"
        f"<b>Reply pesan ini</b> dengan video (maks {svc.cfg.max_video_mb} MB).\n"
        "<i>Terlalu besar? Reply dengan link Drive/YouTube.</i>",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Lampirkan video demo…"),
    )
    if q.message.chat_id == push.get("chat_id"):
        svc.db.update_push(push["id"], prompt_message_id=sent.message_id)
    ctx.user_data["await_push"] = push["id"]
    await q.answer("Silakan kirim videonya 👇")


async def cb_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc, push = await _push_from_callback(update, ctx)
    if not push:
        return
    svc.db.update_push(push["id"], video_status="skipped")
    await update.callback_query.answer("Dilewati ⏭")
    await _after_video_change(ctx, svc, push, "skipped", "")


async def _after_video_change(ctx, svc: AppServices, push: dict, status: str, link: str) -> None:
    if push.get("log_row"):
        try:
            await svc.gcall("update_log_video", push["log_row"], VIDEO_LABEL[status], link)
        except Exception:
            log.exception("Gagal update status video di sheet")
    await refresh_report(ctx.bot, svc, push["id"])
    if push.get("prompt_message_id"):
        try:
            await ctx.bot.delete_message(push["chat_id"], push["prompt_message_id"])
        except TelegramError:
            pass
    if ctx.user_data.get("await_push") == push["id"]:
        ctx.user_data.pop("await_push", None)


def resolve_push(svc: AppServices, ctx, msg, uid: int, private: bool) -> tuple[dict | None, list[dict]]:
    if msg.reply_to_message:
        p = svc.db.get_push_by_message(msg.chat_id, msg.reply_to_message.message_id)
        if p:
            return p, []
    aw = ctx.user_data.get("await_push")
    if aw:
        p = svc.db.get_push(aw)
        if p and p["video_status"] == "pending":
            return p, []
    if not private:
        return None, []
    pending = svc.db.pending_pushes_for(uid)
    if len(pending) == 1:
        return pending[0], []
    return None, pending


async def on_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_svc(ctx)
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    media = msg.video or msg.document
    if media is None:
        return
    private = chat.type == ChatType.PRIVATE
    push, candidates = resolve_push(svc, ctx, msg, user.id, private)

    if push is None:
        if candidates:
            ctx.user_data["orphan"] = {"file_id": media.file_id, "size": media.file_size,
                                       "mime": media.mime_type, "name": getattr(media, "file_name", None)}
            kb = [[Btn(push_label(p, svc.cfg)[:60], callback_data=f"pick:{p['id']}")] for p in candidates[:8]]
            await msg.reply_html("🤔 Video ini untuk push yang mana?", reply_markup=Kb(kb))
        elif private:
            await msg.reply_html("Tidak ada push yang sedang menunggu video 🙂")
        return

    if not can_submit(svc, push, user.id):
        owner = svc.pusher_of(push)
        await msg.reply_html(f"🙅 Video untuk push ini ditunggu dari {mention(owner, push.get('github_login') or '?')}.")
        return
    if push["video_status"] != "pending":
        await msg.reply_html(f"ℹ️ Video untuk push ini sudah: <b>{VIDEO_LABEL[push['video_status']]}</b>.")
        return
    await save_video_file(ctx, svc, push, media.file_id, media.file_size, media.mime_type,
                          getattr(media, "file_name", None), msg)


async def cb_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc, push = await _push_from_callback(update, ctx)
    if not push:
        return
    q = update.callback_query
    orphan = ctx.user_data.pop("orphan", None)
    if not orphan:
        await q.answer("Videonya sudah tidak ada, kirim ulang ya.", show_alert=True)
        return
    await q.answer()
    await q.edit_message_text(f"👌 Untuk push <b>{escape(push_label(push, svc.cfg))}</b>", parse_mode=HTML)
    await save_video_file(ctx, svc, push, orphan["file_id"], orphan["size"], orphan["mime"], orphan["name"], q.message)


def _video_filename(push: dict, svc: AppServices, ext: str) -> str:
    dt = datetime.fromtimestamp(push["created_at"], ZoneInfo(svc.cfg.timezone))
    feats = feature_list(push["groups"])
    slug = norm(feats[0]) if feats else "umum"
    return f"{dt:%Y-%m-%d_%H%M}_{push['branch']}_{slug}_{(push['head_sha'] or '')[:7]}{ext}"


async def save_video_file(ctx, svc: AppServices, push: dict, file_id: str, size: int | None,
                          mime: str | None, name: str | None, reply_to) -> None:
    limit = svc.cfg.max_video_mb
    if size and size > limit * 1024 * 1024:
        await reply_to.reply_html(
            f"⚠️ Video <b>{size / 1024 / 1024:.1f} MB</b> melebihi batas <b>{limit} MB</b>.\n"
            "Kompres dulu (turunkan resolusi/durasi), atau reply laporan dengan <b>link Drive/YouTube</b>.")
        return
    if svc.google is None:
        await reply_to.reply_html("⚠️ Google Drive belum dikonfigurasi di server. Kirim link video saja.")
        return

    status = await reply_to.reply_html("⏳ Mengunggah video ke Drive…")
    ext = (Path(name).suffix if name else "") or mimetypes.guess_extension(mime or "") or ".mp4"
    tmpdir = Path(tempfile.mkdtemp(prefix="vid_"))
    try:
        tg_file = await ctx.bot.get_file(file_id)
        local = tmpdir / f"video{ext}"
        await tg_file.download_to_drive(custom_path=local)
        link = await svc.gcall("upload_video", local, _video_filename(push, svc, ext),
                               push["branch"], mime or "video/mp4")
    except BadRequest as e:
        text = ("⚠️ File terlalu besar untuk diunduh bot (batas Telegram 20 MB).\n"
                "Kompres dulu atau kirim link Drive/YouTube.") if "too big" in str(e).lower() \
            else f"❌ Gagal mengambil video: {escape(str(e))}"
        await status.edit_text(text, parse_mode=HTML)
        return
    except Exception:
        log.exception("Upload video gagal (push #%s)", push["id"])
        await status.edit_text("❌ Gagal mengunggah ke Drive. Coba lagi, atau kirim link videonya.")
        return
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    svc.db.update_push(push["id"], video_status="done", video_link=link)
    await _after_video_change(ctx, svc, push, "done", link)
    await status.edit_text(f'✅ Video tersimpan · <a href="{escape(link)}">buka di Drive</a>',
                           parse_mode=HTML, disable_web_page_preview=True)


async def maybe_video_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Reply laporan/prompt dengan link -> dicatat sebagai video demo."""
    svc = get_svc(ctx)
    msg, user = update.effective_message, update.effective_user
    m = URL_RE.search(msg.text or "")
    if not m:
        return False
    push = None
    if msg.reply_to_message:
        rid = msg.reply_to_message.message_id
        push = svc.db.get_push_by_message(msg.chat_id, rid)
        # Reply ke laporan (bukan prompt) hanya dihitung jika dari pelaku push,
        # supaya diskusi/link biasa dari orang lain tidak dianggap video.
        if push and push.get("prompt_message_id") != rid and push.get("telegram_id") != user.id:
            return False
    if push is None and ctx.user_data.get("await_push"):
        push = svc.db.get_push(ctx.user_data["await_push"])
    if not push or push["video_status"] != "pending" or not can_submit(svc, push, user.id):
        return False
    link = m.group(0)
    svc.db.update_push(push["id"], video_status="done", video_link=link)
    await _after_video_change(ctx, svc, push, "done", link)
    await msg.reply_html("✅ Link video tercatat.")
    return True


# ======================================================================
# Teks bebas
# ======================================================================
async def on_private_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    reg = ctx.user_data.get("reg")
    if reg and reg.get("step") == "github":
        await handle_github_input(update, ctx, reg)
        return
    if await maybe_video_link(update, ctx):
        return
    await update.effective_message.reply_html("Ketik /help untuk melihat perintah.")


async def on_group_reply_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await maybe_video_link(update, ctx)


# ======================================================================
# /selesai
# ======================================================================
def _active_user(svc: AppServices, uid: int) -> dict | None:
    u = svc.db.get_user(uid)
    return u if u and u["status"] == "active" else None


async def cmd_selesai(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_svc(ctx)
    msg = update.effective_message
    user = _active_user(svc, update.effective_user.id)
    if not user:
        await msg.reply_html("Kamu belum terdaftar. Ketik /start di chat pribadi bot.")
        return
    role = user["role"]
    query = " ".join(ctx.args or []).strip()
    if not query:
        recent = svc.db.recent_features_for(user["telegram_id"], time.time() - 14 * 86400)
        if not recent:
            await msg.reply_html("Ketik nama fiturnya, mis. <code>/selesai pengadaan</code>")
            return
        kb = [[Btn(f, callback_data=f"done:{svc.mapper.token(f)}")] for f in recent[:8]]
        await msg.reply_html(
            f"🏁 Fitur mana yang sudah <b>selesai</b> di sisi {ROLE_EMOJI[role]} {ROLE_LABELS[role]}?\n"
            "<i>Atau ketik /selesai &lt;nama fitur&gt;</i>", reply_markup=Kb(kb))
        return
    matches = svc.mapper.search(query)
    if not matches:
        await msg.reply_html(f"❌ Fitur <b>{escape(query)}</b> tidak ditemukan di daftar Plan.")
        return
    if len(matches) == 1:
        text, kb = _confirm_done(svc, matches[0], role)
        await msg.reply_html(text, reply_markup=kb)
        return
    kb = [[Btn(f, callback_data=f"done:{svc.mapper.token(f)}")] for f in matches]
    await msg.reply_html("Maksudnya yang mana?", reply_markup=Kb(kb))


def _confirm_done(svc: AppServices, feature: str, role: str):
    text = f"Tandai <b>{escape(feature)}</b> selesai di sisi {ROLE_EMOJI[role]} <b>{ROLE_LABELS[role]}</b>?"
    kb = Kb([[Btn("✅ Ya, selesai", callback_data=f"dok:{svc.mapper.token(feature)}"),
              Btn("Batal", callback_data="cancel")]])
    return text, kb


async def cb_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_svc(ctx)
    q = update.callback_query
    user = _active_user(svc, update.effective_user.id)
    if not user:
        await q.answer("Kamu belum terdaftar.", show_alert=True)
        return
    action, token = q.data.split(":")
    feature = svc.mapper.by_token(token)
    if not feature:
        await q.answer("Fitur tidak dikenal.", show_alert=True)
        return
    role = user["role"]
    if action == "done":
        text, kb = _confirm_done(svc, feature, role)
        await q.answer()
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
        return

    await q.answer("Menyimpan…")
    try:
        ok = await svc.gcall("set_plan_done", feature, role, True)
    except Exception:
        log.exception("Gagal centang Plan")
        ok = False
    if not ok:
        await q.edit_message_text(f"❌ Gagal mencentang <b>{escape(feature)}</b> di sheet Plan.", parse_mode=HTML)
        return
    await q.edit_message_text(f"✅ <b>{escape(feature)}</b> ditandai selesai ({ROLE_LABELS[role]}).", parse_mode=HTML)

    others = svc.db.active_by_role(COUNTERPART[role])
    who = " ".join(mention(u) for u in others) or f"Tim {ROLE_LABELS[COUNTERPART[role]]}"
    nxt = "HTML siap dikonversi ke Blade." if role == "fe" else "Backend siap, cek di tampilan."
    text = f"🏁 <b>{escape(feature)}</b> selesai di sisi {ROLE_EMOJI[role]} {ROLE_LABELS[role]} oleh {mention(user)}\n👉 {who} — {nxt}"
    if q.message.chat_id != svc.group_chat_id():
        await announce(ctx.bot, svc, text)
    else:
        await q.message.reply_html(text)


async def cb_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Dibatalkan.")


# ======================================================================
# /pending, admin, lain-lain
# ======================================================================
async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_svc(ctx)
    uid = update.effective_user.id
    msg = update.effective_message
    admin = svc.cfg.is_admin(uid)
    if not _active_user(svc, uid) and not admin:
        await msg.reply_html("Kamu belum terdaftar. Ketik /start di chat pribadi bot.")
        return
    rows = svc.db.all_pending() if admin else svc.db.pending_pushes_for(uid)
    if not rows:
        await msg.reply_html("🎉 Tidak ada push yang menunggu video.")
        return
    lines = [f"⏳ <b>{len(rows)} push menunggu video</b>", ""]
    kb = []
    for p in rows[:10]:
        link = message_link(p.get("chat_id"), p.get("message_id"))
        label = escape(push_label(p, svc.cfg))
        owner = f" · {escape(p.get('github_login') or '')}" if admin else ""
        lines.append(f'• <a href="{link}">{label}</a>{owner}' if link else f"• {label}{owner}")
        kb.append(Btn(f"📹 #{p['id']}", callback_data=f"vid:{p['id']}"))
    await msg.reply_html("\n".join(lines), disable_web_page_preview=True,
                         reply_markup=Kb([kb[i:i + 3] for i in range(0, len(kb), 3)]))


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_svc(ctx)
    if not svc.cfg.is_admin(update.effective_user.id):
        return
    users = svc.db.list_users()
    if not users:
        await update.effective_message.reply_html("Belum ada developer terdaftar.")
        return
    icon = {"active": "✅", "pending": "⏳", "rejected": "❌"}
    lines = ["<b>👥 Developer</b>", ""] + [
        f"{icon.get(u['status'], '•')} {ROLE_EMOJI.get(u['role'], '')} {mention(u)} · "
        f"<code>{escape(u['github_login'] or '-')}</code>"
        for u in users
    ]
    await update.effective_message.reply_html("\n".join(lines))


async def cmd_setgroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_svc(ctx)
    chat, msg = update.effective_chat, update.effective_message
    if not svc.cfg.is_admin(update.effective_user.id):
        await msg.reply_html("Hanya admin yang bisa mengatur grup.")
        return
    if chat.type == ChatType.PRIVATE:
        await msg.reply_html("Jalankan perintah ini <b>di dalam grup</b> tujuan laporan.")
        return
    svc.db.set_setting("group_chat_id", str(chat.id))
    await msg.reply_html(f"✅ Laporan push akan dikirim ke <b>{escape(chat.title or 'grup ini')}</b>.")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_svc(ctx)
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    svc.db.delete_user(update.effective_user.id)
    ctx.user_data.clear()
    await update.effective_message.reply_html("🗑 Registrasi dihapus. Ketik /start untuk daftar ulang.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_svc(ctx)
    uid = update.effective_user.id
    await update.effective_message.reply_html(help_text(svc.db.get_user(uid), svc.cfg.is_admin(uid)))


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u, c = update.effective_user, update.effective_chat
    await update.effective_message.reply_html(
        f"🆔 User ID kamu: <code>{u.id}</code>\n💬 Chat ID ini: <code>{c.id}</code>")


async def reminder_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc = get_svc(ctx)
    cutoff = time.time() - svc.cfg.reminder_hours * 3600
    for p in svc.db.due_reminders(cutoff):
        owner = svc.pusher_of(p)
        try:
            await ctx.bot.send_message(
                p["chat_id"],
                f"⏰ {mention(owner, p.get('github_login') or 'dev')}, video demo untuk push "
                f"<code>{(p['head_sha'] or '')[:7]}</code> · <b>{escape(features_text(p['groups']))}</b> masih ditunggu.",
                parse_mode=HTML,
                reply_parameters=ReplyParameters(message_id=p["message_id"], allow_sending_without_reply=True),
                reply_markup=Kb([[Btn("📹 Kirim video", callback_data=f"vid:{p['id']}"),
                                  Btn("⏭ Skip", callback_data=f"skip:{p['id']}")]]),
            )
        except TelegramError as e:
            log.warning("Reminder gagal: %s", e)
        svc.db.update_push(p["id"], reminded=1)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Error saat memproses update", exc_info=ctx.error)


async def set_commands(app: Application) -> None:
    private = [
        BotCommand("start", "Registrasi / status"),
        BotCommand("pending", "Push yang menunggu video"),
        BotCommand("selesai", "Tandai fitur selesai"),
        BotCommand("help", "Bantuan"),
        BotCommand("reset", "Daftar ulang"),
    ]
    group = [
        BotCommand("pending", "Push yang menunggu video"),
        BotCommand("selesai", "Tandai fitur selesai"),
        BotCommand("help", "Bantuan"),
    ]
    await app.bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
    await app.bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats())


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("selesai", cmd_selesai))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("setgroup", cmd_setgroup))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("id", cmd_id))

    app.add_handler(CallbackQueryHandler(cb_reg_role, pattern=r"^reg:(fe|be)$"))
    app.add_handler(CallbackQueryHandler(cb_reg_confirm, pattern=r"^reg(ok|edit)$"))
    app.add_handler(CallbackQueryHandler(cb_approval, pattern=r"^(appr|rej):\d+$"))
    app.add_handler(CallbackQueryHandler(cb_video, pattern=r"^vid:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_skip, pattern=r"^skip:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_pick, pattern=r"^pick:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_done, pattern=r"^(done|dok):[0-9a-f]+$"))
    app.add_handler(CallbackQueryHandler(cb_cancel, pattern=r"^cancel$"))

    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_private_text))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & filters.REPLY & ~filters.COMMAND,
                                   on_group_reply_text))
    app.add_error_handler(on_error)
