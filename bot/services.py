"""Objek bersama + helper yang dipakai handler Telegram dan webhook."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from .config import COUNTERPART, Config
from .db import DB
from .features import FeatureMapper
from .google_services import GoogleServices
from .messages import render_report, report_keyboard

log = logging.getLogger(__name__)


@dataclass
class AppServices:
    cfg: Config
    db: DB
    mapper: FeatureMapper
    google: GoogleServices | None

    def group_chat_id(self) -> int | None:
        v = self.db.get_setting("group_chat_id")
        return int(v) if v else self.cfg.group_chat_id

    def pusher_of(self, push: dict) -> dict | None:
        if push.get("telegram_id"):
            u = self.db.get_user(push["telegram_id"])
            if u and u["status"] == "active":
                return u
        if push.get("github_login"):
            return self.db.get_user_by_github(push["github_login"])
        return None

    def counterparts_of(self, push: dict) -> list[dict]:
        other = COUNTERPART.get(push["role"])
        return self.db.active_by_role(other) if other else []

    async def gcall(self, fn_name: str, *args, **kwargs):
        """Panggil method GoogleServices di thread terpisah. Return None jika Google nonaktif."""
        if self.google is None:
            return None
        return await asyncio.to_thread(getattr(self.google, fn_name), *args, **kwargs)


def get_svc(ctx) -> AppServices:
    return ctx.application.bot_data["svc"]


async def refresh_report(bot, svc: AppServices, push_id: int) -> None:
    push = svc.db.get_push(push_id)
    if not push or not push.get("message_id"):
        return
    text = render_report(push, svc.pusher_of(push), svc.counterparts_of(push), svc.cfg)
    try:
        await bot.edit_message_text(
            chat_id=push["chat_id"], message_id=push["message_id"], text=text,
            parse_mode=ParseMode.HTML, reply_markup=report_keyboard(push),
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Gagal edit laporan #%s: %s", push_id, e)


async def notify_admins(bot, svc: AppServices, text: str, reply_markup=None) -> None:
    for admin_id in svc.cfg.admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML,
                                   reply_markup=reply_markup, disable_web_page_preview=True)
        except TelegramError as e:
            log.warning("Gagal DM admin %s: %s (admin harus /start bot dulu)", admin_id, e)


async def announce(bot, svc: AppServices, text: str, reply_markup=None) -> None:
    chat_id = svc.group_chat_id()
    if not chat_id:
        return
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                               reply_markup=reply_markup, disable_web_page_preview=True)
    except TelegramError as e:
        log.warning("Gagal kirim ke grup: %s", e)
