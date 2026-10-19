"""Record adapter.

`RecordStore` is the interface the agent uses for company records. `MemoryStore` is the
local implementation built from a pack's seed; an ERP adapter would implement the same
interface. Writes return a `Change` (before/after) for the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from pydantic import BaseModel

from sdra.pack import Seed
from sdra.schema import AuditEntry

TABLES = list(Seed.model_fields)

KEYS: dict[str, Callable[[BaseModel], str]] = {
    "stock": lambda r: f"{r.item_id}@{r.location_id}",
    "bom": lambda r: f"{r.parent_item_id}>{r.component_item_id}",
    "supplier_items": lambda r: f"{r.supplier_id}:{r.item_id}",
}

# Prefixes for ids the agent creates, so runs are reproducible and scenarios can reference them.
NEW_ID_PREFIX = {"purchase_orders": "PO", "outbox": "OUT", "approval_requests": "REQ", "review_tasks": "TASK",
                 "work_order_substitutions": "WOS"}


def key_of(table: str, row: BaseModel) -> str:
    return KEYS.get(table, lambda r: r.id)(row)


@dataclass(frozen=True)
class Change:
    table: str
    key: str
    before: dict | None
    after: dict | None


class RecordStore(Protocol):
    def rows(self, table: str) -> list: ...
    def get(self, table: str, key: str): ...
    def put(self, table: str, row: BaseModel) -> Change: ...
    def update(self, table: str, key: str, **fields) -> Change: ...
    def new_id(self, kind: str) -> str: ...
    # audit log and runner state, committed together with record writes
    def audit_entries(self) -> list[AuditEntry]: ...
    def append_audit(self, entry: AuditEntry) -> None: ...
    def load_runtime(self) -> str | None: ...
    def save_runtime(self, state_json: str) -> None: ...
    def commit(self) -> None: ...


class MemoryStore:
    def __init__(self, seed: Seed):
        self._tables: dict[str, dict[str, BaseModel]] = {
            t: {key_of(t, r): r for r in getattr(seed, t)} for t in TABLES
        }
        self._counters: dict[str, int] = {}
        self._audit: list[AuditEntry] = []
        self._runtime: str | None = None

    def rows(self, table: str) -> list:
        return list(self._tables[table].values())

    def get(self, table: str, key: str):
        return self._tables[table].get(key)

    def put(self, table: str, row: BaseModel) -> Change:
        key = key_of(table, row)
        before = self._tables[table].get(key)
        self._tables[table][key] = row
        return Change(table, key, before.model_dump(mode="json") if before else None, row.model_dump(mode="json"))

    def update(self, table: str, key: str, **fields) -> Change:
        row = self._tables[table][key]
        updated = type(row).model_validate(row.model_dump() | fields)  # re-validates
        return self.put(table, updated)

    def new_id(self, kind: str) -> str:
        n = self._counters[kind] = self._counters.get(kind, 0) + 1
        return f"{NEW_ID_PREFIX[kind]}-N{n:04d}"

    def audit_entries(self) -> list[AuditEntry]:
        return list(self._audit)

    def append_audit(self, entry: AuditEntry) -> None:
        self._audit.append(entry)

    def load_runtime(self) -> str | None:
        return self._runtime

    def save_runtime(self, state_json: str) -> None:
        self._runtime = state_json

    def commit(self) -> None:
        pass  # writes are immediate in memory

    def snapshot(self) -> Seed:
        return Seed.model_validate({t: [r.model_dump() for r in self._tables[t].values()] for t in TABLES})
