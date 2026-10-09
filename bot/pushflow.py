"""Pemrosesan event push: catat ke DB & sheet, lalu laporkan ke Telegram."""
from __future__ import annotations

import logging
from html import escape

from telegram.constants import ParseMode

from .config import ROLE_LABELS
from .features import group_commits, mapped_features
from .github import PushEvent
from .messages import (
    VIDEO_LABEL, fmt_time, iso_time, now, render_report, report_keyboard,
)
from .services import AppServices, notify_admins

log = logging.getLogger(__name__)


def _summary(commits) -> str:
    return "\n".join(c.headline for c in commits)


async def process_push(svc: AppServices, bot, evt: PushEvent, delivery_id: str) -> int | None:
    cfg, db = svc.cfg, svc.db
    if evt.deleted or not evt.branch or not evt.commits:
        log.info("Lewati push %s (deleted/tag/kosong)", evt.ref)
        return None

    if evt.branch in cfg.release_branches:
        role = "release"
    else:
        role = cfg.role_for_branch(evt.branch)
    if role is None:
        log.info("Branch %s tidak dipantau", evt.branch)
        return None

    commits = [c for c in evt.commits if c.distinct] or evt.commits
    groups = group_commits(commits, svc.mapper)
    user = db.get_user_by_github(evt.pusher_login)
    needs_video = role != "release" and cfg.video_needed(commits)
    created = now()

    push_id = db.insert_push(
        delivery_id=delivery_id or None,
        branch=evt.branch,
        role=role,
        github_login=evt.pusher_login,
        telegram_id=user["telegram_id"] if user else None,
        head_sha=evt.head_sha,
        compare_url=evt.compare_url,
        groups=groups,
        commit_count=len(commits),
        forced=int(evt.forced),
        needs_video=int(needs_video),
        video_status="pending" if needs_video else "not_needed",
        created_at=created,
    )

    # ---- Google Sheet ----
    if svc.google is not None:
        try:
            row = await svc.gcall("append_log", [
                iso_time(created, cfg.timezone),
                (user or {}).get("full_name") or evt.pusher_name,
                ROLE_LABELS.get(role, role),
                evt.branch,
                evt.pusher_login,
                ", ".join(mapped_features(groups)) or "Umum",
                len(commits),
                _summary(commits),
                evt.head_sha[:7],
                evt.compare_url,
                VIDEO_LABEL["pending" if needs_video else "not_needed"],
                "",
                push_id,
            ])
            db.update_push(push_id, log_row=row)
            if role in cfg.plan_cols:
                stamp = fmt_time(created, cfg.timezone)
                for g in groups:
                    if g["feature"] == "Umum":
                        continue
                    descs = "; ".join(c["desc"] for c in g["commits"])[:300]
                    await svc.gcall("update_plan_note", g["feature"], role, f"{stamp}: {descs}")
        except Exception:
            log.exception("Gagal menulis ke Google Sheet (push #%s)", push_id)
            db.update_push(push_id, sheet_error=1)

    # ---- Telegram ----
    push = db.get_push(push_id)
    text = render_report(push, user, svc.counterparts_of(push), cfg)
    chat_id = svc.group_chat_id()
    if chat_id is None:
        await notify_admins(bot, svc, "ℹ️ Grup belum diset. Tambahkan bot ke grup lalu ketik /setgroup di sana.\n\n" + text)
        return push_id

    msg = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                 reply_markup=report_keyboard(push), disable_web_page_preview=True)
    db.update_push(push_id, chat_id=msg.chat_id, message_id=msg.message_id)

    if not user:
        await notify_admins(
            bot, svc,
            f"⚠️ Push dari GitHub <code>{escape(evt.pusher_login)}</code> ke <b>{escape(evt.branch)}</b> "
            "belum tertaut ke akun Telegram.\nMinta orangnya /start bot dan daftar dengan username itu — "
            "push lamanya akan otomatis tertaut.",
        )
    return push_id
