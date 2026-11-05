"""Tests for the sync-mode decision.

Which files get deleted is decided in `Syncer` (from the saved mode, or an
explicit override) and carried down to the transport as `mirror`, so these
tests record what the transport is asked to do rather than moving any bytes.
"""

import os
import tempfile

import nasync.core.sync as sync_mod
from nasync.core.config import Config
from nasync.core.sync import Syncer


class _RecordingClient:
    """Stand-in for SAMBAClient that captures the flags it is handed."""

    calls = []
    built = []       # the kwargs each client was constructed with
    allowed = []     # allow_mass_delete as each pair was handed it

    def __init__(self, *a, **k):
        _RecordingClient.built.append(k)
        self.identity = k.get("identity")

    def connect(self):
        return None

    def disconnect(self):
        return None

    def release(self):
        return None

    def sync_directory(self, local_dir, remote_dir, direction="push",
                       dry_run=False, mirror=True, allow_mass_delete=False,
                       state=None, unattended=False, outstanding=False):
        _RecordingClient.calls.append({"direction": direction, "mirror": mirror})
        _RecordingClient.allowed.append(allow_mass_delete)
        return {"transferred": 0, "skipped": 0, "deleted": 0, "errors": 0,
                "unsupported": 0}


def _configured(tmp):
    os.environ["XDG_CONFIG_HOME"] = tmp
    config = Config()
    config.add_or_update_server("nas", "1.2.3.4", "u", "p", "share", 445)
    config.set_active_server("nas")
    config.add_pair(tmp, "/dest", "pair")
    return config


def _run(fn):
    """Run `fn` with the transport swapped out, returning the recorded calls."""
    original = sync_mod.SAMBAClient
    sync_mod.SAMBAClient = _RecordingClient
    _RecordingClient.calls = []
    _RecordingClient.built = []
    _RecordingClient.allowed = []
    try:
        fn()
    finally:
        sync_mod.SAMBAClient = original
    return _RecordingClient.calls


def test_the_servers_encryption_setting_reaches_the_transport():
    """A per-server switch is worthless if the code that moves the files
    doesn't read it — this is the path every automatic sync takes."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)

        _run(lambda: Syncer(reporter=lambda _m: None).sync_pair())
        assert _RecordingClient.built[0]["encrypt"] is False

        config.save_server("nas", "1.2.3.4", "u", "p", "share", 445,
                           encrypt=True)
        _run(lambda: Syncer(reporter=lambda _m: None).sync_pair())
        assert _RecordingClient.built[0]["encrypt"] is True
    print("✅ test_the_servers_encryption_setting_reaches_the_transport passed")


def test_push_follows_the_saved_sync_mode():
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)

        config.set_sync_mode("mirror")
        calls = _run(lambda: Syncer(reporter=lambda _m: None).sync_pair())
        assert calls == [{"direction": "push", "mirror": True}], calls

        config.set_sync_mode("update")
        calls = _run(lambda: Syncer(reporter=lambda _m: None).sync_pair())
        assert calls == [{"direction": "push", "mirror": False}], calls
        print("✅ test_push_follows_the_saved_sync_mode passed")


def test_explicit_mirror_argument_overrides_the_saved_mode():
    """`nasync sync --mode mirror` must win over a saved 'update'."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        config.set_sync_mode("update")

        calls = _run(
            lambda: Syncer(reporter=lambda _m: None).sync_pair(mirror=True))
        assert calls == [{"direction": "push", "mirror": True}], calls
        print("✅ test_explicit_mirror_argument_overrides_the_saved_mode passed")


def test_restore_never_mirrors():
    """Mirror mode is about the *server*; a restore must not delete local files
    just because the server doesn't have them."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        config.set_sync_mode("mirror")

        calls = _run(lambda: Syncer(reporter=lambda _m: None).restore_pair())
        assert calls == [{"direction": "pull", "mirror": False}], calls
        print("✅ test_restore_never_mirrors passed")


def test_every_pair_reports_progress_even_with_nothing_to_transfer():
    """Front-ends drive their progress bar off this, so it has to fire once per
    pair regardless of whether the pair turns out to have any work to do."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        config.add_pair(tmp, "/dest2", "pair2")
        config.add_pair(tmp, "/dest3", "pair3")

        seen = []
        _run(lambda: Syncer(
            reporter=lambda _m: None,
            pair_progress=lambda index, total, name, verb: seen.append(
                (index, total, name, verb)),
        ).sync_pair())

        # _RecordingClient transfers nothing at all, and all three still report.
        assert seen == [(1, 3, "pair", "Syncing"),
                        (2, 3, "pair2", "Syncing"),
                        (3, 3, "pair3", "Syncing")], seen
        print("✅ test_every_pair_reports_progress_even_with_nothing_to_transfer passed")


def test_pair_progress_is_optional():
    """The CLI doesn't pass one; Syncer must not require it."""
    with tempfile.TemporaryDirectory() as tmp:
        _configured(tmp)
        calls = _run(lambda: Syncer(reporter=lambda _m: None).sync_pair())
        assert calls == [{"direction": "push", "mirror": True}], calls
        print("✅ test_pair_progress_is_optional passed")


def test_the_run_announces_which_mode_it_is_in():
    """The log line is the only warning a CLI user gets before deletes."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        lines = []

        config.set_sync_mode("mirror")
        _run(lambda: Syncer(reporter=lines.append).sync_pair())
        assert any("Mirror mode" in line for line in lines), lines

        lines.clear()
        config.set_sync_mode("update")
        _run(lambda: Syncer(reporter=lines.append).sync_pair())
        assert any("Update mode" in line for line in lines), lines
        assert not any("Mirror mode" in line for line in lines), lines

        # Restore says nothing about modes, because none applies to it.
        lines.clear()
        _run(lambda: Syncer(reporter=lines.append).restore_pair())
        assert not any("mode" in line for line in lines), lines
        print("✅ test_the_run_announces_which_mode_it_is_in passed")


if __name__ == "__main__":
    test_push_follows_the_saved_sync_mode()
    test_explicit_mirror_argument_overrides_the_saved_mode()
    test_restore_never_mirrors()
    test_every_pair_reports_progress_even_with_nothing_to_transfer()
    test_pair_progress_is_optional()
    test_the_run_announces_which_mode_it_is_in()


# --------------------------------------------------------------------------- #
# 0.20.0 — overlapping pairs and held-back mirror runs
# --------------------------------------------------------------------------- #
def test_pairs_sharing_a_server_folder_are_skipped_not_synced():
    """Two mirror pairs in one folder deleted each other's files every night.
    A config from before 0.20.0 can still hold such pairs; a push skips them."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        # Written the way an old config holds it — add_pair now refuses this.
        config.get_pairs("nas").append({"name": "intruder", "local": tmp,
                                        "server": "dest"})
        lines = []
        result = {}
        calls = _run(lambda: result.update(
            Syncer(config=config, reporter=lines.append).sync_pair()))
    assert calls == [], "an overlapping pair reached the transport"
    assert sorted(result["overlapping"]) == ["intruder", "pair"], result
    assert result["failed"] == 2, result
    assert any("overlaps" in line for line in lines), lines


def test_a_mass_delete_yes_reaches_only_the_pairs_it_was_given_for():
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        os.makedirs(os.path.join(tmp, "b"))
        config.add_pair(os.path.join(tmp, "b"), "/other", "second")
        _run(lambda: Syncer(config=config, reporter=lambda _m: None)
             .sync_pair(allow_mass_delete={"second"}))
    assert _RecordingClient.allowed == [False, True], _RecordingClient.allowed


def test_a_refused_pair_is_reported_for_someone_to_confirm():
    from nasync.core.samba import MirrorRefused

    class _Refusing(_RecordingClient):
        def sync_directory(self, *a, **k):
            raise MirrorRefused("would delete 900 of 1000", 900, 1000, True)

    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        original = sync_mod.SAMBAClient
        sync_mod.SAMBAClient = _Refusing
        try:
            result = Syncer(config=config, reporter=lambda _m: None).sync_pair()
        finally:
            sync_mod.SAMBAClient = original
    assert result["failed"] == 1 and result["ok"] == 0, result
    assert result["refused"] == [{"pair": "pair", "message": "would delete 900 of 1000",
                                  "deletes": 900, "total": 1000,
                                  "confirmable": True, "fingerprint": "", "sample": []}], result


def test_a_sync_refuses_to_start_while_another_process_holds_the_lock():
    """The app's Sync button used to start a second run on top of the
    background service's, the two colliding on files each had open."""
    from nasync.core.activity import Activity, SyncBusy

    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        with Activity(config.config_dir, source="background",
                      trigger="scheduled 19:25"):
            try:
                _run(lambda: Syncer(config=config,
                                    reporter=lambda _m: None).sync_pair())
            except SyncBusy as e:
                assert "background service" in str(e), e
            else:
                raise AssertionError("two syncs ran at once")
        # Released: the next run goes ahead.
        assert _run(lambda: Syncer(config=config,
                                   reporter=lambda _m: None).sync_pair())


def test_a_dry_run_does_not_need_the_lock():
    from nasync.core.activity import Activity

    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        with Activity(config.config_dir, source="background"):
            assert _run(lambda: Syncer(config=config, reporter=lambda _m: None)
                        .sync_pair(dry_run=True))
