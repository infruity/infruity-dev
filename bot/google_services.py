"""Akses Google Sheets (log + Plan) dan Google Drive (video demo).

Semua method bersifat blocking -> panggil lewat asyncio.to_thread().
Satu lock dipakai karena klien Google (httplib2/requests) tidak thread-safe.
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import gspread
from gspread.utils import rowcol_to_a1
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import Config
from .features import norm

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

LOG_HEADER = [
    "Waktu", "Developer", "Role", "Branch", "GitHub", "Fitur", "Jumlah Commit",
    "Ringkasan Commit", "Head SHA", "Link Compare", "Status Video", "Link Video", "Push ID",
]
COL_VIDEO_STATUS = LOG_HEADER.index("Status Video") + 1
BOT_NOTE_PREFIX = "🤖"


def load_credentials(cfg: Config):
    if cfg.google_token_file.exists():
        creds = Credentials.from_authorized_user_file(str(cfg.google_token_file), SCOPES)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
            cfg.google_token_file.write_text(creds.to_json())
        return creds
    if cfg.google_sa_file.exists():
        return service_account.Credentials.from_service_account_file(str(cfg.google_sa_file), scopes=SCOPES)
    return None


class GoogleServices:
    def __init__(self, cfg: Config, creds):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.gc = gspread.authorize(creds)
        self.drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._sh = None
        self._folders: dict[str, str] = {}

    @classmethod
    def from_config(cls, cfg: Config) -> "GoogleServices | None":
        creds = load_credentials(cfg)
        if creds is None:
            log.warning("Kredensial Google tidak ditemukan — fitur Sheet & Drive nonaktif.")
            return None
        return cls(cfg, creds)

    # ---------------- Sheets ----------------
    @property
    def sh(self):
        if self._sh is None:
            self._sh = self.gc.open_by_key(self.cfg.spreadsheet_id)
        return self._sh

    def _log_ws(self):
        try:
            return self.sh.worksheet(self.cfg.log_sheet)
        except gspread.WorksheetNotFound:
            ws = self.sh.add_worksheet(self.cfg.log_sheet, rows=1000, cols=len(LOG_HEADER))
            ws.update(range_name="A1", values=[LOG_HEADER])
            ws.freeze(rows=1)
            ws.format("A1:M1", {"textFormat": {"bold": True}})
            return ws

    def append_log(self, values: list) -> int | None:
        with self.lock:
            ws = self._log_ws()
            res = ws.append_row(values, value_input_option="RAW", table_range="A1")
            rng = (res or {}).get("updates", {}).get("updatedRange", "")
            m = re.search(r"![A-Z]+(\d+)", rng)
            return int(m.group(1)) if m else None

    def update_log_video(self, row: int, status: str, link: str = "") -> None:
        with self.lock:
            ws = self._log_ws()
            start = rowcol_to_a1(row, COL_VIDEO_STATUS)
            end = rowcol_to_a1(row, COL_VIDEO_STATUS + 1)
            ws.update(range_name=f"{start}:{end}", values=[[status, link]], value_input_option="RAW")

    def _plan_row(self, ws, feature: str) -> int | None:
        target = norm(feature)
        for idx, cell in enumerate(ws.col_values(1), start=1):
            if norm(cell or "") == target:
                return idx
        return None

    def update_plan_note(self, feature: str, role: str, text: str) -> bool:
        """Tulis/ganti satu baris catatan bot di kolom Catatan. Catatan manual tidak disentuh."""
        if role not in self.cfg.plan_cols:
            return False
        _, note_col = self.cfg.plan_cols[role]
        with self.lock:
            ws = self.sh.worksheet(self.cfg.plan_sheet)
            row = self._plan_row(ws, feature)
            if row is None:
                return False
            current = ws.cell(row, note_col).value or ""
            lines = [ln for ln in current.split("\n") if ln.strip() and not ln.startswith(BOT_NOTE_PREFIX)]
            lines.append(f"{BOT_NOTE_PREFIX} {text}")
            ws.update(range_name=rowcol_to_a1(row, note_col), values=[["\n".join(lines)]],
                      value_input_option="RAW")
            return True

    def set_plan_done(self, feature: str, role: str, done: bool = True) -> bool:
        if role not in self.cfg.plan_cols:
            return False
        done_col, _ = self.cfg.plan_cols[role]
        with self.lock:
            ws = self.sh.worksheet(self.cfg.plan_sheet)
            row = self._plan_row(ws, feature)
            if row is None:
                return False
            ws.update(range_name=rowcol_to_a1(row, done_col), values=[["TRUE" if done else "FALSE"]],
                      value_input_option="USER_ENTERED")
            return True

    # ---------------- Drive ----------------
    def _subfolder(self, name: str) -> str:
        if name in self._folders:
            return self._folders[name]
        parent = self.cfg.drive_folder_id
        safe = name.replace("'", "\\'")
        q = (f"'{parent}' in parents and name='{safe}' and "
             "mimeType='application/vnd.google-apps.folder' and trashed=false")
        found = self.drive.files().list(
            q=q, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute().get("files", [])
        if found:
            fid = found[0]["id"]
        else:
            fid = self.drive.files().create(
                body={"name": name, "parents": [parent], "mimeType": "application/vnd.google-apps.folder"},
                fields="id", supportsAllDrives=True,
            ).execute()["id"]
        self._folders[name] = fid
        return fid

    def upload_video(self, path: Path, filename: str, subfolder: str, mime: str = "video/mp4") -> str:
        with self.lock:
            parent = self._subfolder(subfolder)
            media = MediaFileUpload(str(path), mimetype=mime, resumable=True, chunksize=8 * 1024 * 1024)
            req = self.drive.files().create(
                body={"name": filename, "parents": [parent]},
                media_body=media, fields="id,webViewLink", supportsAllDrives=True,
            )
            resp = None
            while resp is None:
                _, resp = req.next_chunk()
            return resp.get("webViewLink") or f"https://drive.google.com/file/d/{resp['id']}/view"
