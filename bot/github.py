"""Parsing payload webhook GitHub (event `push`)."""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import asdict, dataclass, field

CONV_RE = re.compile(r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<desc>.+)$")
MERGE_RE = re.compile(r"^Merge (branch|pull request|remote-tracking)", re.IGNORECASE)


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@dataclass
class Commit:
    sha: str
    message: str
    type: str
    scope: str | None
    desc: str
    url: str = ""
    distinct: bool = True
    breaking: bool = False
    files: int = 0

    @property
    def short(self) -> str:
        return self.sha[:7]

    @property
    def headline(self) -> str:
        return self.message.splitlines()[0] if self.message else ""

    @classmethod
    def from_payload(cls, d: dict) -> "Commit":
        message = (d.get("message") or "").strip()
        first = message.splitlines()[0] if message else ""
        ctype, scope, desc, breaking = "other", None, first, False
        if MERGE_RE.match(first):
            ctype = "merge"
        else:
            m = CONV_RE.match(first)
            if m:
                ctype = m.group("type").lower()
                scope = (m.group("scope") or "").strip() or None
                desc = m.group("desc").strip()
                breaking = bool(m.group("bang")) or "BREAKING CHANGE" in message
        files = sum(len(d.get(k) or []) for k in ("added", "removed", "modified"))
        return cls(
            sha=d.get("id", ""),
            message=message,
            type=ctype,
            scope=scope,
            desc=desc,
            url=d.get("url", ""),
            distinct=bool(d.get("distinct", True)),
            breaking=breaking,
            files=files,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Commit":
        return cls(**d)


@dataclass
class PushEvent:
    repo: str
    ref: str
    branch: str
    pusher_login: str
    pusher_name: str
    compare_url: str
    head_sha: str
    commits: list[Commit] = field(default_factory=list)
    created: bool = False
    deleted: bool = False
    forced: bool = False


def parse_push(p: dict) -> PushEvent:
    ref = p.get("ref", "")
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
    sender = p.get("sender") or {}
    pusher = p.get("pusher") or {}
    head = (p.get("head_commit") or {}).get("id") or p.get("after", "")
    return PushEvent(
        repo=((p.get("repository") or {}).get("full_name") or "").lower(),
        ref=ref,
        branch=branch,
        pusher_login=sender.get("login") or pusher.get("name") or "",
        pusher_name=pusher.get("name") or sender.get("login") or "",
        compare_url=p.get("compare", ""),
        head_sha=head,
        commits=[Commit.from_payload(c) for c in p.get("commits") or []],
        created=bool(p.get("created")),
        deleted=bool(p.get("deleted")),
        forced=bool(p.get("forced")),
    )
