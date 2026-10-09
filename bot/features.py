"""Pemetaan scope commit -> Nama Fitur di sheet Plan."""
from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path

import yaml

from .github import Commit

GENERAL = "Umum"


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


class FeatureMapper:
    def __init__(self, path: Path):
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        self.features: list[str] = [str(k) for k in data]
        self._alias: dict[str, str] = {}
        self._token: dict[str, str] = {}
        for feature, aliases in data.items():
            feature = str(feature)
            for alias in [feature, *(aliases or [])]:
                self._alias.setdefault(norm(str(alias)), feature)
            self._token[self.token(feature)] = feature

    @staticmethod
    def token(feature: str) -> str:
        return hashlib.sha1(feature.encode()).hexdigest()[:10]

    def by_token(self, token: str) -> str | None:
        return self._token.get(token)

    def from_scope(self, scope: str | None) -> str | None:
        if not scope:
            return None
        for part in re.split(r"[,/|]", scope):
            hit = self._alias.get(norm(part))
            if hit:
                return hit
        return None

    def search(self, query: str, limit: int = 6) -> list[str]:
        q = norm(query)
        if not q:
            return []
        if q in self._alias:
            return [self._alias[q]]
        found: list[str] = []
        for key, feature in self._alias.items():
            if q in key and feature not in found:
                found.append(feature)
        for key in difflib.get_close_matches(q, list(self._alias), n=limit, cutoff=0.6):
            if self._alias[key] not in found:
                found.append(self._alias[key])
        return found[:limit]


def group_commits(commits: list[Commit], mapper: FeatureMapper) -> list[dict]:
    """Kelompokkan commit per fitur, urut sesuai kemunculan. 'Umum' selalu terakhir."""
    groups: dict[str, list[dict]] = {}
    for c in commits:
        feature = mapper.from_scope(c.scope) or GENERAL
        groups.setdefault(feature, []).append(c.to_dict())
    ordered = [f for f in groups if f != GENERAL] + ([GENERAL] if GENERAL in groups else [])
    return [{"feature": f, "commits": groups[f]} for f in ordered]


def mapped_features(groups: list[dict]) -> list[str]:
    return [g["feature"] for g in groups if g["feature"] != GENERAL]
