from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def cache_key(model: str, prompt_version: str, chunk_text: str) -> str:
    return hashlib.sha256((model + prompt_version + chunk_text).encode()).hexdigest()


class Cache:
    """Each completed response commits immediately; WAL tolerates interruption."""
    def __init__(self, path: Path, enabled: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.enabled = enabled
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS captions (hash TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS glossary
                (scope TEXT, term TEXT, rendering TEXT, PRIMARY KEY(scope, term));
            CREATE TABLE IF NOT EXISTS selections (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chunks
                (scope TEXT, idx INTEGER, part INTEGER, value TEXT, PRIMARY KEY(scope, idx, part));
            CREATE TABLE IF NOT EXISTS chapters
                (scope TEXT, idx INTEGER, value TEXT, PRIMARY KEY(scope, idx));
        """)

    def get_response(self, key: str) -> str | None:
        if not self.enabled:
            return None
        row = self.db.execute("SELECT value FROM responses WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put_response(self, key: str, value: str) -> None:
        if self.enabled:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO responses VALUES (?,?)", (key, value))

    def caption(self, digest: str) -> str | None:
        row = self.db.execute("SELECT value FROM captions WHERE hash=?", (digest,)).fetchone()
        return row[0] if row else None

    def save_caption(self, digest: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO captions VALUES (?,?)", (digest, value))

    def selection(self, key: str) -> list[int] | None:
        row = self.db.execute("SELECT value FROM selections WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_selection(self, key: str, value: list[int]) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO selections VALUES (?,?)", (key, json.dumps(value)))

    def load_glossary(self, scope: str) -> dict[str, str]:
        return dict(self.db.execute("SELECT term, rendering FROM glossary WHERE scope=? ORDER BY term", (scope,)))

    def chapter(self, scope: str, idx: int) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        row = self.db.execute("SELECT value FROM chapters WHERE scope=? AND idx=?", (scope, idx)).fetchone()
        return json.loads(row[0]) if row else None

    def chunk(self, scope: str, idx: int, part: int) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        row = self.db.execute("SELECT value FROM chunks WHERE scope=? AND idx=? AND part=?", (scope, idx, part)).fetchone()
        return json.loads(row[0]) if row else None

    def complete_chunk(self, scope: str, idx: int, part: int, value: dict[str, Any], glossary: dict[str, str]) -> None:
        with self.db:
            for term, rendering in glossary.items():
                self.db.execute("INSERT OR IGNORE INTO glossary VALUES (?,?,?)", (scope, term, rendering))
            if self.enabled:
                self.db.execute("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?)", (scope, idx, part, json.dumps(value, ensure_ascii=False)))

    def complete_chapter(self, scope: str, idx: int, value: dict[str, Any], glossary: dict[str, str]) -> None:
        # Chapter and terminology become visible atomically.
        with self.db:
            for term, rendering in glossary.items():
                self.db.execute("INSERT OR IGNORE INTO glossary VALUES (?,?,?)", (scope, term, rendering))
            if self.enabled:
                self.db.execute("INSERT OR REPLACE INTO chapters VALUES (?,?,?)", (scope, idx, json.dumps(value, ensure_ascii=False)))

    def close(self) -> None:
        self.db.close()
