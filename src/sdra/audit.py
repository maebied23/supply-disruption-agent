"""Append-only, hash-chained audit log.

Each entry's hash covers its content and the previous entry's hash, so removing, reordering
or editing any entry breaks every hash after it. `verify_chain` finds the first break.
A store can persist entries through `sink` (the SQLite store also refuses UPDATE/DELETE).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Callable

from sdra.schema import AuditEntry

GENESIS = "0" * 64


def entry_hash(entry: AuditEntry | dict) -> str:
    data = entry.model_dump(mode="json") if isinstance(entry, AuditEntry) else dict(entry)
    data.pop("hash", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AuditLog:
    def __init__(self, entries: list[AuditEntry] | None = None, sink: Callable[[AuditEntry], None] | None = None):
        self.entries: list[AuditEntry] = list(entries or [])
        self.sink = sink

    def append(self, at: datetime, actor: str, event: str, incident_id: str | None = None,
               ref_ids: list[str] | tuple = (), before: dict | None = None, after: dict | None = None) -> AuditEntry:
        prev = self.entries[-1].hash if self.entries else GENESIS
        draft = dict(seq=len(self.entries), at=at, actor=actor, event=event, incident_id=incident_id,
                     ref_ids=list(ref_ids), before=before, after=after, prev_hash=prev, hash="")
        draft = AuditEntry.model_validate(draft).model_dump(mode="json")
        entry = AuditEntry.model_validate(draft | {"hash": entry_hash(draft)})
        self.entries.append(entry)
        if self.sink:
            self.sink(entry)
        return entry

    def of(self, event: str) -> list[AuditEntry]:
        return [e for e in self.entries if e.event == event]


def verify_chain(entries: list[AuditEntry]) -> list[str]:
    """Problems found, empty if the chain is intact."""
    prev = GENESIS
    for i, e in enumerate(entries):
        if e.seq != i:
            return [f"entry {i}: sequence {e.seq}, expected {i}"]
        if e.prev_hash != prev:
            return [f"entry {i}: previous hash does not match entry {i - 1}"]
        if entry_hash(e) != e.hash:
            return [f"entry {i}: content does not match its hash"]
        prev = e.hash
    return []
