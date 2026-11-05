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


def test_renaming_a_server_entry_keeps_its_records(tmp_path):
    """A rename re-keyed every record, so each pair went back to bootstrap
    and mirror deleted conflict copies (second audit, F2)."""
    record = SyncState(tmp_path, "nas", "share", "root/docs")
    record.register()
    record.put("a.txt", Entry(3, 1.5, 3, 1.5, "h"))
    record.mark_initialised()
    record.mark_mirrored()
    record.close()
    state.rename_server(tmp_path, "nas", "home-nas", "share")
    moved = SyncState(tmp_path, "home-nas", "share", "root/docs")
    assert moved.initialised and moved.mirrored and list(moved.load()) == ["a.txt"]
    moved.close()
    old = SyncState(tmp_path, "nas", "share", "root/docs")
    assert old.load() == {} and not old.initialised
    old.close()


def test_a_removed_server_leaves_no_records_behind(tmp_path):
    record = SyncState(tmp_path, "nas", "share", "d")
    record.register()
    record.put("x", Entry(1, 1.0, 1, 1.0))
    record.close()
    state.forget_server(tmp_path, "nas")
    again = SyncState(tmp_path, "nas", "share", "d")
    assert again.load() == {}
    again.close()


def test_a_database_from_before_the_mirrored_marker_is_upgraded(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / state.DB_NAME)
    db.executescript("CREATE TABLE pairs (key TEXT PRIMARY KEY, server TEXT NOT NULL, "
                     "destination TEXT NOT NULL, initialised REAL);")
    db.close()
    record = SyncState(tmp_path, "nas", "share", "d")
    record.register()
    assert not record.mirrored
    record.mark_mirrored()
    assert record.mirrored
    record.close()
