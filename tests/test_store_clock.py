from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from sdra.clock import SimClock
from sdra.pack import load_pack
from sdra.store import MemoryStore

PACK = load_pack(Path(__file__).parents[1] / "packs" / "riyadh_ac")
T0 = datetime.fromisoformat("2026-11-03T08:00:00+03:00")


def test_store_update_revalidates_and_records_change():
    store = MemoryStore(PACK.seed)
    change = store.update("purchase_order_lines", "POL-4471-1", qty_ordered=300)
    assert (change.before["qty_ordered"], change.after["qty_ordered"]) == (500, 300)
    with pytest.raises(ValidationError):
        store.update("purchase_order_lines", "POL-4471-1", qty_received=999)  # > qty_ordered


def test_store_ids_are_deterministic_and_snapshot_round_trips():
    store = MemoryStore(PACK.seed)
    assert [store.new_id("purchase_orders") for _ in range(2)] == ["PO-N0001", "PO-N0002"]
    assert store.new_id("outbox") == "OUT-N0001"
    store.update("stock", "CMP-24@PLANT-RUH", on_hand=150)
    snap = store.snapshot()
    assert next(s.on_hand for s in snap.stock if s.item_id == "CMP-24") == 150
    assert len(snap.items) == len(PACK.seed.items)


def test_clock_orders_by_time_then_schedule_order():
    clock = SimClock(T0)
    clock.schedule(T0 + timedelta(hours=2), "b")
    clock.schedule(T0 + timedelta(hours=1), "a")
    clock.schedule(T0 + timedelta(hours=2), "c")
    assert list(clock.run_until(T0 + timedelta(hours=1))) == ["a"]
    assert clock.now == T0 + timedelta(hours=1)
    assert list(clock.run_until(T0 + timedelta(days=1))) == ["b", "c"]
    assert clock.now == T0 + timedelta(days=1)
    with pytest.raises(ValueError):
        clock.schedule(T0, "past")


def test_clock_requires_timezone():
    with pytest.raises(ValueError):
        SimClock(datetime(2026, 11, 3, 8))
