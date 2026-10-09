"""Mode polling: bot mengecek sendiri commit baru lewat GitHub API.

Dipakai saat server tidak bisa diakses dari internet (tidak bisa menerima webhook).
Setiap POLL_INTERVAL detik:
  1. GET /repos/{repo}/branches  (pakai ETag -> respons 304 tidak menghabiskan kuota)
  2. Untuk branch yang dipantau dan head-nya berubah: GET /compare/{lama}...{baru}
  3. Hasilnya diproses persis seperti webhook push.
"""
from __future__ import annotations

import logging
import time

import aiohttp

from .github import Commit, PushEvent
from .pushflow import process_push
from .services import AppServices, notify_admins

log = logging.getLogger(__name__)

KEY_SHA = "poll_sha:"
KEY_INIT = "poll_initialized"


class GitHubPoller:
    def __init__(self, svc: AppServices, bot, session: aiohttp.ClientSession | None = None):
        self.svc = svc
        self.bot = bot
        self.cfg = svc.cfg
        self._session = session
        self._etag: str | None = None
        self._backoff_until = 0.0
        self._warned_auth = False

    # ------------------------------------------------------------------ http
    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self, etag: str | None = None) -> dict:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "infruity-devbot",
        }
        if self.cfg.github_token:
            h["Authorization"] = f"Bearer {self.cfg.github_token}"
        if etag:
            h["If-None-Match"] = etag
        return h

    async def _get(self, path: str, etag: str | None = None):
        url = f"{self.cfg.github_api_url}{path}"
        async with self.session.get(url, headers=self._headers(etag)) as r:
            if r.status == 304:
                return 304, None, r.headers
            if r.status in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0":
                reset = float(r.headers.get("X-RateLimit-Reset", time.time() + 600))
                self._backoff_until = reset + 5
                log.warning("Kena rate limit GitHub, jeda sampai %s", time.strftime("%H:%M", time.localtime(reset)))
                return r.status, None, r.headers
            if r.status in (401, 404):
                await self._warn_auth(r.status)
                return r.status, None, r.headers
            r.raise_for_status()
            return r.status, await r.json(), r.headers

    async def _warn_auth(self, status: int) -> None:
        if self._warned_auth:
            return
        self._warned_auth = True
        msg = ("⚠️ Bot tidak bisa membaca repo <code>{}</code> (HTTP {}).\n"
               "Cek nama repo di GITHUB_REPO, dan isi GITHUB_TOKEN kalau repo-nya private."
               ).format(self.cfg.repo_full_name, status)
        log.error(msg)
        await notify_admins(self.bot, self.svc, msg)

    # ------------------------------------------------------------------ logic
    def _watched(self, branch: str) -> bool:
        return branch in self.cfg.release_branches or self.cfg.role_for_branch(branch) is not None

    def _pusher_login(self, branch: str, commits: list[dict]) -> str:
        """Tentukan siapa yang push.

        1. Login GitHub penulis commit terakhir, kalau terdaftar di bot.
        2. Kalau tidak: developer terdaftar dengan role sesuai branch (frontend -> FE).
        3. Kalau tidak ada juga: login penulis commit apa adanya.
        """
        logins = [((c.get("author") or {}).get("login") or "") for c in reversed(commits)]
        for login in logins:
            if login and self.svc.db.get_user_by_github(login):
                return login
        role = self.cfg.role_for_branch(branch)
        if role:
            users = self.svc.db.active_by_role(role)
            if len(users) == 1:
                return users[0]["github_login"]
        return next((lg for lg in logins if lg), "")

    async def _process_branch(self, branch: str, old: str, new: str) -> None:
        repo = self.cfg.repo_full_name
        status, data, _ = await self._get(f"/repos/{repo}/compare/{old}...{new}")
        if status != 200 or not data:
            return
        raw = data.get("commits") or []
        if not raw:
            return
        commits = [
            Commit.from_payload({
                "id": c["sha"],
                "message": (c.get("commit") or {}).get("message", ""),
                "url": c.get("html_url", ""),
                "distinct": True,
            })
            for c in raw
        ]
        evt = PushEvent(
            repo=repo,
            ref=f"refs/heads/{branch}",
            branch=branch,
            pusher_login=self._pusher_login(branch, raw),
            pusher_name="",
            compare_url=data.get("html_url") or f"https://github.com/{repo}/compare/{old[:12]}...{new[:12]}",
            head_sha=new,
            commits=commits,
            forced=data.get("status") in ("diverged", "behind"),
        )
        delivery = f"poll:{branch}:{new}"
        if self.svc.db.delivery_seen(delivery):
            return
        await process_push(self.svc, self.bot, evt, delivery)

    async def tick(self) -> None:
        if time.time() < self._backoff_until:
            return
        repo = self.cfg.repo_full_name
        status, data, headers = await self._get(f"/repos/{repo}/branches?per_page=100", self._etag)
        if status == 304 or data is None:
            return
        self._etag = headers.get("ETag")
        db = self.svc.db
        first_run = db.get_setting(KEY_INIT) is None

        for b in data:
            name, sha = b["name"], (b.get("commit") or {}).get("sha", "")
            if not sha or not self._watched(name):
                continue
            old = db.get_setting(KEY_SHA + name)
            if old == sha:
                continue
            if old is None:
                # Pertama kali melihat branch ini: catat posisi, jangan laporkan riwayat lama.
                log.info("Mulai memantau %s @ %s", name, sha[:7])
            else:
                try:
                    await self._process_branch(name, old, sha)
                except Exception:
                    log.exception("Gagal memproses perubahan %s %s..%s", name, old[:7], sha[:7])
            db.set_setting(KEY_SHA + name, sha)

        if first_run:
            db.set_setting(KEY_INIT, "1")
            log.info("Polling GitHub aktif untuk %s", repo)


async def poll_job(ctx) -> None:
    poller: GitHubPoller = ctx.application.bot_data["poller"]
    try:
        await poller.tick()
    except Exception:
        log.exception("Polling GitHub gagal")
