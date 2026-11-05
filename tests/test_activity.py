"""Tests for the cross-process sync lock and status files.

The desktop app, the CLI and the background service are separate processes;
these files are how each learns that another is syncing. Separate file
descriptors in one process conflict under `flock` exactly as two processes do,
so the tests can stay in-process.
"""

import os
import tempfile
import threading
import time

from nasync.core import activity
from nasync.core.activity import Activity, SyncBusy


def test_nothing_running_reads_as_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        assert activity.read_status(tmp) is None
        assert activity.read_last(tmp) is None
        assert activity.request_cancel(tmp) is False


def test_a_running_sync_is_visible_to_others_and_gone_after():
    with tempfile.TemporaryDirectory() as tmp:
        with Activity(tmp, source="background", trigger="scheduled 19:25",
                      server="home") as run:
            run.update(force=True, pair="Pictures", pair_index=2, pair_total=9)
            status = activity.read_status(tmp)
            assert status["source"] == "background", status
            assert status["pair"] == "Pictures" and status["pair_total"] == 9
            assert "background service" in activity.describe(status)
        assert activity.read_status(tmp) is None


def test_a_second_holder_is_refused_with_the_first_ones_story():
    with tempfile.TemporaryDirectory() as tmp:
        with Activity(tmp, source="background", trigger="scheduled 19:25"):
            try:
                Activity(tmp, source="app").acquire()
            except SyncBusy as e:
                assert e.status.get("trigger") == "scheduled 19:25", e.status
            else:
                raise AssertionError("the lock was taken twice")


def test_a_status_left_by_a_dead_process_is_ignored():
    """The kernel drops the lock when a process dies; the file may remain."""
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, activity.STATUS_NAME), "w") as handle:
            handle.write('{"source": "background", "pair_total": 3}')
        assert activity.read_status(tmp) is None


def test_the_finished_run_is_recorded_for_whoever_looks_later():
    with tempfile.TemporaryDirectory() as tmp:
        run = Activity(tmp, source="background", trigger="scheduled 19:25")
        run.acquire()
        run.release({"ok": 2, "failed": 0, "transferred": 31, "deleted": 1,
                     "refused": [{"pair": "vpn"}]})
        last = activity.read_last(tmp)
        assert last["result"]["transferred"] == 31, last
        assert last["result"]["refused"] == ["vpn"], last
        assert last["trigger"] == "scheduled 19:25" and last["finished"]


def test_another_process_can_cancel_the_running_sync():
    with tempfile.TemporaryDirectory() as tmp:
        cancel = threading.Event()
        with Activity(tmp, source="background", cancel_event=cancel):
            assert activity.request_cancel(tmp) is True
            deadline = time.monotonic() + 3
            while not cancel.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert cancel.is_set(), "the cancel request never arrived"


def test_a_stale_cancel_request_does_not_cancel_the_next_run():
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, activity.CANCEL_NAME), "w").close()
        cancel = threading.Event()
        with Activity(tmp, cancel_event=cancel):
            time.sleep(activity.WRITE_INTERVAL * 2)
            assert not cancel.is_set()


def test_progress_runs_across_folders_and_within_one():
    assert activity.progress_fraction({}) == 0.0
    assert activity.progress_fraction({"pair_index": 1, "pair_total": 4}) == 0.0
    half_of_second = {"pair_index": 2, "pair_total": 4,
                      "files_done": 5, "files_total": 10}
    assert activity.progress_fraction(half_of_second) == 1.5 / 4
