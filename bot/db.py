"""Penyimpanan SQLite: user terdaftar, push, dan setting."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id  INTEGER PRIMARY KEY,
    tg_username  TEXT,
    full_name    TEXT,
    github_login TEXT,
    role         TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_users_github ON users(lower(github_login));

CREATE TABLE IF NOT EXISTS pushes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id       TEXT UNIQUE,
    branch            TEXT NOT NULL,
    role              TEXT NOT NULL,
    github_login      TEXT,
    telegram_id       INTEGER,
    head_sha          TEXT,
    compare_url       TEXT,
    groups_json       TEXT NOT NULL,
    commit_count      INTEGER NOT NULL,
    forced            INTEGER NOT NULL DEFAULT 0,
    needs_video       INTEGER NOT NULL DEFAULT 0,
    video_status      TEXT NOT NULL DEFAULT 'not_needed',
    video_link        TEXT,
    chat_id           INTEGER,
    message_id        INTEGER,
    prompt_message_id INTEGER,
    log_row           INTEGER,
    sheet_error       INTEGER NOT NULL DEFAULT 0,
    reminded          INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pushes_msg ON pushes(chat_id, message_id);
CREATE INDEX IF NOT EXISTS ix_pushes_pending ON pushes(video_status, telegram_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _d(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    if "groups_json" in d:
        d["groups"] = json.loads(d["groups_json"])
    return d


class DB:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self.lock:
            return _d(self.conn.execute(sql, params).fetchone())

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [_d(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---------- settings ----------
    def get_setting(self, key: str) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---------- users ----------
    def upsert_user(self, telegram_id: int, tg_username: str | None, full_name: str,
                    github_login: str, role: str, status: str) -> None:
        self._exec(
            """INSERT INTO users(telegram_id,tg_username,full_name,github_login,role,status,created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(telegram_id) DO UPDATE SET tg_username=excluded.tg_username,
                 full_name=excluded.full_name, github_login=excluded.github_login,
                 role=excluded.role, status=excluded.status""",
            (telegram_id, tg_username, full_name, github_login, role, status, time.time()),
        )

    def get_user(self, telegram_id: int) -> dict | None:
        return self._one("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))

    def get_user_by_github(self, login: str) -> dict | None:
        return self._one(
            "SELECT * FROM users WHERE lower(github_login)=lower(?) AND status='active'", (login,)
        )

    def github_taken(self, login: str, exclude_telegram_id: int) -> bool:
        return self._one(
            "SELECT 1 FROM users WHERE lower(github_login)=lower(?) AND telegram_id<>? AND status<>'rejected'",
            (login, exclude_telegram_id),
        ) is not None

    def set_user_status(self, telegram_id: int, status: str) -> None:
        self._exec("UPDATE users SET status=? WHERE telegram_id=?", (status, telegram_id))

    def delete_user(self, telegram_id: int) -> None:
        self._exec("DELETE FROM users WHERE telegram_id=?", (telegram_id,))

    def list_users(self) -> list[dict]:
        return self._all("SELECT * FROM users ORDER BY role, created_at")

    def active_by_role(self, role: str) -> list[dict]:
        return self._all("SELECT * FROM users WHERE role=? AND status='active'", (role,))

    # ---------- pushes ----------
    def delivery_seen(self, delivery_id: str) -> bool:
        return self._one("SELECT 1 FROM pushes WHERE delivery_id=?", (delivery_id,)) is not None

    def insert_push(self, **f) -> int:
        f.setdefault("created_at", time.time())
        f["groups_json"] = json.dumps(f.pop("groups"), ensure_ascii=False)
        cols = ",".join(f)
        marks = ",".join("?" for _ in f)
        cur = self._exec(f"INSERT INTO pushes({cols}) VALUES({marks})", tuple(f.values()))
        return int(cur.lastrowid)

    def update_push(self, push_id: int, **f) -> None:
        if not f:
            return
        sets = ",".join(f"{k}=?" for k in f)
        self._exec(f"UPDATE pushes SET {sets} WHERE id=?", (*f.values(), push_id))

    def get_push(self, push_id: int) -> dict | None:
        return self._one("SELECT * FROM pushes WHERE id=?", (push_id,))

    def get_push_by_message(self, chat_id: int, message_id: int) -> dict | None:
        return self._one(
            "SELECT * FROM pushes WHERE chat_id=? AND (message_id=? OR prompt_message_id=?)",
            (chat_id, message_id, message_id),
        )

    def pending_pushes_for(self, telegram_id: int) -> list[dict]:
        return self._all(
            "SELECT * FROM pushes WHERE video_status='pending' AND telegram_id=? ORDER BY created_at DESC",
            (telegram_id,),
        )

    def all_pending(self) -> list[dict]:
        return self._all("SELECT * FROM pushes WHERE video_status='pending' ORDER BY created_at DESC")

    def due_reminders(self, cutoff_ts: float) -> list[dict]:
        return self._all(
            """SELECT * FROM pushes WHERE video_status='pending' AND reminded=0
               AND message_id IS NOT NULL AND created_at < ?""",
            (cutoff_ts,),
        )

    def assign_pushes(self, github_login: str, telegram_id: int) -> int:
        cur = self._exec(
            "UPDATE pushes SET telegram_id=? WHERE telegram_id IS NULL AND lower(github_login)=lower(?)",
            (telegram_id, github_login),
        )
        return cur.rowcount

    def recent_features_for(self, telegram_id: int, since_ts: float) -> list[str]:
        rows = self._all(
            "SELECT groups_json FROM pushes WHERE telegram_id=? AND created_at>=? ORDER BY created_at DESC",
            (telegram_id, since_ts),
        )
        seen: list[str] = []
        for r in rows:
            for g in r["groups"]:
                if g["feature"] != "Umum" and g["feature"] not in seen:
                    seen.append(g["feature"])
        return seen
