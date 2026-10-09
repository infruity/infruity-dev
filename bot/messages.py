"""Template pesan Telegram (HTML)."""
from __future__ import annotations

import time
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Kb

from .config import COUNTERPART, ROLE_EMOJI, ROLE_LABELS, Config
from .features import GENERAL

TYPE_EMOJI = {
    "feat": "✨", "fix": "🐛", "refactor": "♻️", "style": "💄", "docs": "📝", "chore": "🔧",
    "perf": "⚡", "test": "🧪", "build": "📦", "ci": "🤖", "merge": "🔀", "revert": "⏪",
}
VIDEO_LABEL = {"pending": "Menunggu", "done": "Selesai", "skipped": "Dilewati", "not_needed": "Tidak diminta"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
MAX_LINES_PER_FEATURE = 6
MAX_LINES_TOTAL = 14


def fmt_time(ts: float, tz: str) -> str:
    dt = datetime.fromtimestamp(ts, ZoneInfo(tz))
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt:%H:%M}"


def iso_time(ts: float, tz: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def mention(user: dict | None, fallback: str = "—") -> str:
    if not user:
        return escape(fallback)
    if user.get("tg_username"):
        return "@" + escape(user["tg_username"])
    return f'<a href="tg://user?id={user["telegram_id"]}">{escape(user.get("full_name") or "dev")}</a>'


def short_name(user: dict | None, fallback: str) -> str:
    if user and user.get("full_name"):
        return escape(user["full_name"])
    return escape(fallback)


def feature_list(groups: list[dict]) -> list[str]:
    return [g["feature"] for g in groups if g["feature"] != GENERAL]


def features_text(groups: list[dict], empty: str = "Umum") -> str:
    feats = feature_list(groups)
    return ", ".join(feats) if feats else empty


def render_commit_lines(groups: list[dict]) -> list[str]:
    lines: list[str] = []
    total = 0
    hidden = 0
    for g in groups:
        icon = "🧭" if g["feature"] == GENERAL else "📦"
        commits = g["commits"]
        if total >= MAX_LINES_TOTAL:
            hidden += len(commits)
            continue
        lines.append(f"{icon} <b>{escape(g['feature'])}</b>")
        for i, c in enumerate(commits):
            if i >= MAX_LINES_PER_FEATURE or total >= MAX_LINES_TOTAL:
                hidden += len(commits) - i
                break
            emoji = TYPE_EMOJI.get(c["type"], "•")
            desc = escape(c["desc"][:120] + ("…" if len(c["desc"]) > 120 else ""))
            link = f'<a href="{escape(c["url"])}">{c["sha"][:7]}</a>' if c.get("url") else c["sha"][:7]
            lines.append(f"   {emoji} {desc} <i>· {link}</i>")
            total += 1
    if hidden:
        lines.append(f"   <i>…dan {hidden} commit lainnya</i>")
    return lines


def handoff_line(push: dict, counterparts: list[dict]) -> str | None:
    role = push["role"]
    if role not in COUNTERPART:
        return None
    who = " ".join(mention(u) for u in counterparts) or f"Tim {ROLE_LABELS[COUNTERPART[role]]}"
    feats = feature_list(push["groups"])
    target = f"<b>{escape(', '.join(feats))}</b>" if feats else "beberapa bagian"
    if role == "fe":
        return f"👀 {who} — tampilan {target} diperbarui. Cek untuk integrasi ke Blade."
    return f"👀 {who} — backend {target} diperbarui. Cek apakah tampilan perlu disesuaikan."


def video_line(push: dict, pusher: dict | None) -> str:
    status = push["video_status"]
    if status == "pending":
        return f"🎬 Video demo: ⏳ menunggu dari {mention(pusher, push.get('github_login') or 'pelaku push')}"
    if status == "done":
        return f'🎬 Video demo: ✅ <a href="{escape(push["video_link"] or "")}">tonton</a>'
    if status == "skipped":
        return "🎬 Video demo: ⏭ dilewati"
    return "🎬 Video demo: — <i>tidak diminta (tanpa commit feat)</i>"


def render_report(push: dict, pusher: dict | None, counterparts: list[dict], cfg: Config) -> str:
    role = push["role"]
    who = short_name(pusher, push.get("github_login") or "?")
    n = push["commit_count"]
    when = fmt_time(push["created_at"], cfg.timezone)

    if role == "release":
        head = f"🚀 <b>{escape(push['branch'].upper())} diperbarui</b> · {who} · {n} commit"
    else:
        head = f"{ROLE_EMOJI[role]} <b>{ROLE_LABELS[role].upper()}</b> · {who} · {n} commit"
    parts = [head, f"<i>branch {escape(push['branch'])} · {when}"
                   + (" · ⚠️ force push" if push.get("forced") else "") + "</i>", ""]
    parts += render_commit_lines(push["groups"])

    extra: list[str] = []
    if role != "release":
        line = handoff_line(push, counterparts)
        if line:
            extra.append(line)
        extra.append(video_line(push, pusher))
    if not pusher:
        extra.append(f"⚠️ GitHub <code>{escape(push.get('github_login') or '?')}</code> belum terdaftar di bot")
    if push.get("sheet_error"):
        extra.append("⚠️ Gagal mencatat ke sheet — cek log server")
    if extra:
        parts += [""] + extra
    return "\n".join(parts)


def report_keyboard(push: dict) -> Kb | None:
    rows = []
    if push["video_status"] == "pending":
        rows.append([
            Btn("📹 Kirim video", callback_data=f"vid:{push['id']}"),
            Btn("⏭ Skip", callback_data=f"skip:{push['id']}"),
        ])
    links = []
    if push.get("compare_url"):
        links.append(Btn("🔗 Lihat perubahan", url=push["compare_url"]))
    if push["video_status"] == "done" and (push.get("video_link") or "").startswith("http"):
        links.append(Btn("🎬 Tonton demo", url=push["video_link"]))
    if links:
        rows.append(links)
    return Kb(rows) if rows else None


def message_link(chat_id: int | None, message_id: int | None) -> str | None:
    if not chat_id or not message_id:
        return None
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{message_id}"
    return None


def push_label(push: dict, cfg: Config) -> str:
    return (f"#{push['id']} · {ROLE_EMOJI.get(push['role'], '')} {features_text(push['groups'])}"
            f" · {fmt_time(push['created_at'], cfg.timezone)}")


def now() -> float:
    return time.time()
