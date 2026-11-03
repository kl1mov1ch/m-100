"""SQLite stores drafts and absolute write intents before any external side effect."""

import json
import sqlite3
from pathlib import Path

from .domain import Draft, UserError


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY, owner INTEGER NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                source TEXT PRIMARY KEY, draft_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, owner INTEGER NOT NULL, stage TEXT NOT NULL,
                payload TEXT NOT NULL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS inputs (
                owner INTEGER PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
        """)

    def bind(self, identity: str) -> None:
        row = self.db.execute("SELECT value FROM metadata WHERE key='storage' ").fetchone()
        if row and row[0] != identity:
            raise UserError(
                "Эта база относится к другому режиму или объекту. Укажите отдельный DATABASE_PATH."
            )
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('storage',?)", (identity,))

    def save(self, draft: Draft, source: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO drafts VALUES (?,?,?)",
                (draft.id, draft.owner, json.dumps(draft.to_dict(), ensure_ascii=False)),
            )
            if source:
                self.db.execute("INSERT OR IGNORE INTO messages VALUES (?,?)", (source, draft.id))

    def bind_target(self, name: str, value: str) -> None:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (name,)).fetchone()
        if row and row[0] != value:
            raise UserError("Изменился портал назначения. Используйте отдельную базу для другого портала.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (name, value))

    def load(self, draft_id: str, owner: int) -> Draft:
        row = self.db.execute(
            "SELECT payload FROM drafts WHERE id=? AND owner=?", (draft_id, owner)
        ).fetchone()
        if row is None:
            raise UserError("Черновик не найден или принадлежит другому пользователю.")
        return Draft.from_dict(json.loads(row[0]))

    def active(self, owner: int) -> Draft | None:
        rows = self.db.execute("SELECT payload FROM drafts WHERE owner=? ORDER BY rowid DESC", (owner,))
        for row in rows:
            draft = Draft.from_dict(json.loads(row[0]))
            if draft.status in {"draft", "pending"}:
                return draft
        return None

    def from_source(self, source: str, owner: int) -> Draft | None:
        row = self.db.execute("SELECT draft_id FROM messages WHERE source=?", (source,)).fetchone()
        return self.load(row[0], owner) if row else None

    def operation(self, op_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def history(self, owner: int | None, start: str, end: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT id,owner,stage,payload FROM operations WHERE (? IS NULL OR owner=?) "
            "AND json_extract(payload,'$.day') BETWEEN ? AND ? ORDER BY rowid DESC LIMIT 10001",
            (owner, owner, start, end),
        )
        result = [dict(row, payload=json.loads(row["payload"])) for row in rows]
        if len(result) > 10000:
            raise UserError("Слишком много операций. Выберите меньший период.")
        return result

    def pending(self) -> list[dict]:
        return [
            dict(row) for row in self.db.execute("SELECT id,owner,stage FROM operations WHERE stage!='done'")
        ]

    def prepare(self, draft: Draft, payload: dict) -> None:
        draft.status = "pending"
        with self.db:
            self.db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,NULL)",
                (draft.id, draft.owner, "prepared", json.dumps(payload, ensure_ascii=False)),
            )
            self.db.execute(
                "UPDATE drafts SET payload=? WHERE id=?",
                (json.dumps(draft.to_dict(), ensure_ascii=False), draft.id),
            )

    def stage(self, op_id: str, stage: str, error: str | None = None) -> None:
        with self.db:
            self.db.execute("UPDATE operations SET stage=?,error=? WHERE id=?", (stage, error, op_id))

    def finish(self, draft: Draft) -> None:
        draft.status = "done"
        with self.db:
            self.db.execute("UPDATE operations SET stage='done',error=NULL WHERE id=?", (draft.id,))
            self.db.execute(
                "UPDATE drafts SET payload=? WHERE id=?",
                (json.dumps(draft.to_dict(), ensure_ascii=False), draft.id),
            )
            self.db.execute("DELETE FROM inputs WHERE owner=?", (draft.owner,))

    def close(self) -> None:
        self.db.close()

    def input(self, owner: int, value: dict | None = None, clear: bool = False) -> dict | None:
        with self.db:
            if clear:
                self.db.execute("DELETE FROM inputs WHERE owner=?", (owner,))
                return None
            if value is not None:
                self.db.execute("INSERT OR REPLACE INTO inputs VALUES (?,?)", (owner, json.dumps(value)))
            row = self.db.execute("SELECT payload FROM inputs WHERE owner=?", (owner,)).fetchone()
            return json.loads(row[0]) if row else None


class ProcessLock:
    """One process per database, on Windows and Unix. OS releases locks on crashes."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        try:
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            import os

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise UserError("Другой экземпляр бота уже использует эту базу.") from None

    def close(self) -> None:
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
