"""Tests for the per-pair record of what was uploaded."""

from nasync.core import state
from nasync.core.state import Entry, SyncState


def test_a_record_round_trips_and_survives_reopening(tmp_path):
    record = SyncState(tmp_path, "nas", "share", "root/docs")
    assert record.load() == {} and not record.initialised
    record.put("a.txt", Entry(3, 1.5, 3, 1.5, "h", 0o755))
    record.mark_initialised()
    record.close()

    again = SyncState(tmp_path, "nas", "share", "root/docs")
    assert again.initialised
    assert again.load() == {"a.txt": Entry(3, 1.5, 3, 1.5, "h", 0o755, False)}
    again.move("a.txt", "b.txt")
    again.forget("missing")
    assert list(again.load()) == ["b.txt"]
    again.close()


def test_pairs_with_different_destinations_have_separate_records(tmp_path):
    first = SyncState(tmp_path, "nas", "share", "root/a")
    first.put("x", Entry(1, 1.0, 1, 1.0))
    first.close()
    second = SyncState(tmp_path, "nas", "share", "root/b")
    assert second.load() == {}
    second.close()


def test_a_read_only_record_writes_nothing(tmp_path):
    record = SyncState(tmp_path, "nas", "share", "d", read_only=True)
    record.put("x", Entry(1, 1.0, 1, 1.0))
    record.mark_initialised()
    record.close()
    check = SyncState(tmp_path, "nas", "share", "d")
    assert check.load() == {} and not check.initialised
    check.close()


def test_records_for_abandoned_destinations_are_pruned(tmp_path):
    for dest in ("old", "current"):
        record = SyncState(tmp_path, "nas", "share", dest)
        record.register()
        record.put("x", Entry(1, 1.0, 1, 1.0))
        record.close()
    state.prune(tmp_path, "nas", {state.pair_key("nas", "share", "current")})
    assert SyncState(tmp_path, "nas", "share", "old").load() == {}
    assert SyncState(tmp_path, "nas", "share", "current").load() != {}
