"""SQLite record store: same interface as `MemoryStore`, durable across restarts.

One file holds the records, id counters, the audit log and the runner's state. Writes join
an open transaction that `commit()` closes, so each processed event lands atomically.
The audit table refuses UPDATE and DELETE (triggers), on top of the hash chain.
The LangGraph checkpointer keeps its own tables (use a separate file or connection).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import BaseModel

from sdra.pack import Seed
from sdra.schema import AuditEntry
from sdra.store import NEW_ID_PREFIX, TABLES, Change, key_of

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (tbl TEXT NOT NULL, key TEXT NOT NULL, row TEXT NOT NULL, PRIMARY KEY (tbl, key));
CREATE TABLE IF NOT EXISTS counters (kind TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, hash TEXT NOT NULL, entry TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runtime (id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""

ROW_MODEL: dict[str, type[BaseModel]] = {t: Seed.model_fields[t].annotation.__args__[0] for t in TABLES}


class SqliteStore:
    def __init__(self, path: str | Path, seed: Seed | None = None):
        """Open `path`. With `seed`, the file must be new and is filled from it."""
        self.path = Path(path)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        has_rows = self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] > 0
        if seed is not None:
            if has_rows:
                raise ValueError(f"{self.path} already holds records; open it without a seed")
            self.db.executemany("INSERT INTO records VALUES (?, ?, ?)", [
                (t, key_of(t, r), r.model_dump_json()) for t in TABLES for r in getattr(seed, t)])
            self.db.commit()
        elif not has_rows:
            raise ValueError(f"{self.path} is empty; pass a seed to create it")
        self._cache: dict[str, dict[str, BaseModel]] = {t: {} for t in TABLES}
        for tbl, key, row in self.db.execute("SELECT tbl, key, row FROM records ORDER BY rowid"):
            self._cache[tbl][key] = ROW_MODEL[tbl].model_validate_json(row)

    # --- records ---------------------------------------------------------------------------

    def rows(self, table: str) -> list:
        return list(self._cache[table].values())

    def get(self, table: str, key: str):
        return self._cache[table].get(key)

    def put(self, table: str, row: BaseModel) -> Change:
        key = key_of(table, row)
        before = self._cache[table].get(key)
        self.db.execute("INSERT INTO records VALUES (?, ?, ?) ON CONFLICT (tbl, key) DO UPDATE SET row = excluded.row",
                        (table, key, row.model_dump_json()))
        self._cache[table][key] = row
        return Change(table, key, before.model_dump(mode="json") if before else None, row.model_dump(mode="json"))

    def update(self, table: str, key: str, **fields) -> Change:
        row = self._cache[table][key]
        return self.put(table, type(row).model_validate(row.model_dump() | fields))

    def new_id(self, kind: str) -> str:
        self.db.execute("INSERT INTO counters VALUES (?, 1) ON CONFLICT (kind) DO UPDATE SET n = n + 1", (kind,))
        (n,) = self.db.execute("SELECT n FROM counters WHERE kind = ?", (kind,)).fetchone()
        return f"{NEW_ID_PREFIX[kind]}-N{n:04d}"

    def snapshot(self) -> Seed:
        return Seed.model_validate({t: [r.model_dump() for r in self._cache[t].values()] for t in TABLES})

    # --- audit and runtime ------------------------------------------------------------------

    def audit_entries(self) -> list[AuditEntry]:
        return [AuditEntry.model_validate_json(e) for (e,) in self.db.execute("SELECT entry FROM audit ORDER BY seq")]

    def append_audit(self, entry: AuditEntry) -> None:
        self.db.execute("INSERT INTO audit VALUES (?, ?, ?)", (entry.seq, entry.hash, entry.model_dump_json()))

    def load_runtime(self) -> str | None:
        row = self.db.execute("SELECT state FROM runtime WHERE id = 1").fetchone()
        return row[0] if row else None

    def save_runtime(self, state_json: str) -> None:
        self.db.execute("INSERT INTO runtime VALUES (1, ?) ON CONFLICT (id) DO UPDATE SET state = excluded.state",
                        (state_json,))

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.close()
