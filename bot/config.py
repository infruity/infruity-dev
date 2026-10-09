"""Konfigurasi bot, dibaca dari environment / file .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

ROLE_LABELS = {"fe": "Frontend", "be": "Backend", "release": "Release"}
ROLE_EMOJI = {"fe": "🎨", "be": "⚙️", "release": "🚀"}
COUNTERPART = {"fe": "be", "be": "fe"}


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _ints(value: str | None) -> frozenset[int]:
    if not value:
        return frozenset()
    return frozenset(int(v) for v in value.replace(" ", "").split(",") if v)


def _col_index(letter: str) -> int:
    """'A' -> 1, 'D' -> 4, 'AA' -> 27"""
    n = 0
    for ch in letter.strip().upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _plan_cols(value: str) -> tuple[int, int]:
    """'B:C' -> (kolom checkbox selesai, kolom catatan)"""
    done, note = value.split(":")
    return _col_index(done), _col_index(note)


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else BASE_DIR / p


@dataclass(frozen=True)
class Config:
    telegram_token: str
    admin_ids: frozenset[int]
    group_chat_id: int | None
    github_secret: str
    repo_full_name: str
    branch_roles: tuple[tuple[str, str], ...]
    release_branches: frozenset[str]
    spreadsheet_id: str
    plan_sheet: str
    log_sheet: str
    plan_cols: dict = field(hash=False)
    drive_folder_id: str = ""
    google_token_file: Path = BASE_DIR / "secrets/token.json"
    google_sa_file: Path = BASE_DIR / "secrets/service_account.json"
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    webhook_path: str = "/github/webhook"
    db_path: Path = BASE_DIR / "data/bot.db"
    aliases_file: Path = BASE_DIR / "aliases.yaml"
    video_mode: str = "feat"
    reminder_hours: float = 6.0
    telegram_api_base_url: str = ""
    telegram_local_mode: bool = False
    max_video_mb: int = 20
    timezone: str = "Asia/Jakarta"

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN wajib diisi di .env")
        secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
        if not secret:
            raise RuntimeError("GITHUB_WEBHOOK_SECRET wajib diisi di .env")

        branch_roles = []
        for item in os.getenv("BRANCH_ROLES", "frontend:fe,backend:be").split(","):
            item = item.strip()
            if not item:
                continue
            branch, role = item.rsplit(":", 1)
            branch_roles.append((branch.strip(), role.strip()))

        local_mode = _bool(os.getenv("TELEGRAM_LOCAL_MODE"))
        group = os.getenv("TELEGRAM_GROUP_CHAT_ID", "").strip()
        return cls(
            telegram_token=token,
            admin_ids=_ints(os.getenv("TELEGRAM_ADMIN_IDS")),
            group_chat_id=int(group) if group else None,
            github_secret=secret,
            repo_full_name=os.getenv("GITHUB_REPO", "infruity/infruity-erp").lower(),
            branch_roles=tuple(branch_roles),
            release_branches=frozenset(
                b.strip() for b in os.getenv("RELEASE_BRANCHES", "main").split(",") if b.strip()
            ),
            spreadsheet_id=os.getenv("SPREADSHEET_ID", ""),
            plan_sheet=os.getenv("PLAN_SHEET_NAME", "Plan"),
            log_sheet=os.getenv("LOG_SHEET_NAME", "Log Push"),
            plan_cols={
                "fe": _plan_cols(os.getenv("PLAN_COLS_FE", "B:C")),
                "be": _plan_cols(os.getenv("PLAN_COLS_BE", "D:E")),
            },
            drive_folder_id=os.getenv("DRIVE_FOLDER_ID", ""),
            google_token_file=_path(os.getenv("GOOGLE_TOKEN_FILE", "secrets/token.json")),
            google_sa_file=_path(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "secrets/service_account.json")),
            webhook_host=os.getenv("WEBHOOK_HOST", "0.0.0.0"),
            webhook_port=int(os.getenv("WEBHOOK_PORT", "8080")),
            webhook_path=os.getenv("WEBHOOK_PATH", "/github/webhook"),
            db_path=_path(os.getenv("DB_PATH", "data/bot.db")),
            aliases_file=_path(os.getenv("ALIASES_FILE", "aliases.yaml")),
            video_mode=os.getenv("VIDEO_MODE", "feat").lower(),
            reminder_hours=float(os.getenv("REMINDER_HOURS", "6")),
            telegram_api_base_url=os.getenv("TELEGRAM_API_BASE_URL", "").rstrip("/"),
            telegram_local_mode=local_mode,
            max_video_mb=int(os.getenv("MAX_VIDEO_MB", "2000" if local_mode else "20")),
            timezone=os.getenv("TZ_NAME", "Asia/Jakarta"),
        )

    # ---- helper ----
    def is_admin(self, telegram_id: int | None) -> bool:
        return telegram_id is not None and telegram_id in self.admin_ids

    def role_for_branch(self, branch: str) -> str | None:
        for pattern, role in self.branch_roles:
            if pattern.endswith("/*"):
                if branch.startswith(pattern[:-1]):
                    return role
            elif branch == pattern:
                return role
        return None

    def branch_for_role(self, role: str) -> str:
        for pattern, r in self.branch_roles:
            if r == role and not pattern.endswith("/*"):
                return pattern
        return role

    def video_needed(self, commits) -> bool:
        if self.video_mode == "always":
            return True
        if self.video_mode == "never":
            return False
        return any(c.type == "feat" or c.breaking for c in commits)
