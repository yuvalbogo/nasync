"""Tests for live sync and scheduled sync.

The scheduling rule and the LAN gate are the two things that decide whether an
unattended machine syncs or quietly doesn't, so they're tested directly rather
than through a running observer.
"""

import datetime
import os
import threading
import time
import tempfile

from nasync.system import lan
from nasync.core.automation import (AutomationRunner, Automator, run_all,
                                    scheduled_run_is_due)
from nasync.core.config import Config


def _at(hour, minute=0):
    return datetime.datetime(2026, 8, 6, hour, minute)


def test_scheduled_run_waits_for_its_time():
    assert scheduled_run_is_due("02:00", None, _at(1, 59)) is False
    assert scheduled_run_is_due("02:00", None, _at(2, 0)) is True
    print("✅ test_scheduled_run_waits_for_its_time passed")


def test_scheduled_run_happens_once_a_day():
    today = _at(3).date().isoformat()
    assert scheduled_run_is_due("02:00", today, _at(3)) is False
    assert scheduled_run_is_due("02:00", "2026-08-05", _at(3)) is True
    print("✅ test_scheduled_run_happens_once_a_day passed")


def test_a_missed_run_catches_up_rather_than_being_skipped():
    """Asleep at 02:00, or away from home — the day still owes a sync, and it
    happens at the first opportunity instead of waiting 24 hours."""
    assert scheduled_run_is_due("02:00", "2026-08-05", _at(9, 30)) is True
    assert scheduled_run_is_due("02:00", "2026-08-05", _at(23, 59)) is True
    print("✅ test_a_missed_run_catches_up_rather_than_being_skipped passed")


def _configured(tmp):
    os.environ["XDG_CONFIG_HOME"] = tmp
    config = Config()
    config.add_or_update_server("nas", "192.168.1.50", "u", "p", "share", 445)
    config.set_active_server("nas")
    config.add_pair(tmp, "/dest", "pair")
    return config


def test_sync_is_skipped_when_away_from_the_network(monkeypatch):
    """The whole point of the gate: no sync attempt at all when not home."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: False)

        lines = []
        automator = Automator(reporter=lines.append, config=config)
        assert automator.sync_now("file changed") is False
        assert any("Paused" in line for line in lines), lines
        print("✅ test_sync_is_skipped_when_away_from_the_network passed")


def test_leaving_and_returning_are_logged_once_each(monkeypatch):
    """This runs for days as a service — a line per poll would bury the syncs
    that actually happened."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        home = {"value": False}
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: home["value"])

        lines = []
        automator = Automator(reporter=lines.append, config=config)
        automator.LAN_CACHE_TTL = 0            # force a fresh check each call

        for _ in range(5):
            automator.sync_now("file changed")
        assert sum("Paused" in line for line in lines) == 1, lines

        import nasync.core.automation as automation_module

        class _FakeSyncer:
            def __init__(self, *a, **k):
                pass

            def sync_pair(self, pair_name=None):
                return {"ok": 1, "failed": 0}

        monkeypatch.setattr(automation_module, "Syncer", _FakeSyncer)
        home["value"] = True
        for _ in range(3):
            automator.sync_now("file changed")
        assert sum("Back on the home network" in line for line in lines) == 1, lines
        print("✅ test_leaving_and_returning_are_logged_once_each passed")


def test_the_lan_verdict_is_cached_between_checks(monkeypatch):
    """A pending scheduled sync checks every tick; each check must not be a
    fresh network probe."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        probes = []
        monkeypatch.setattr(lan, "on_lan",
                            lambda ip, port=445: probes.append(ip) or False)

        automator = Automator(reporter=lambda _m: None, config=config)
        for _ in range(10):
            automator.on_lan()
        assert len(probes) == 1, probes

        automator._lan_checked_at = None       # TTL expired
        automator.on_lan()
        assert len(probes) == 2, probes
        print("✅ test_the_lan_verdict_is_cached_between_checks passed")


def test_sync_runs_when_on_the_network(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)

        synced = []
        import nasync.core.automation as automation_module

        class _FakeSyncer:
            def __init__(self, *a, **k):
                pass

            def sync_pair(self, pair_name=None):
                synced.append(pair_name)
                return {"ok": 1, "failed": 0}

        monkeypatch.setattr(automation_module, "Syncer", _FakeSyncer)
        automator = Automator(reporter=lambda _m: None, config=config)
        assert automator.sync_now("file changed") is True
        assert synced == [None], synced
        print("✅ test_sync_runs_when_on_the_network passed")


def test_overlapping_triggers_do_not_stack(monkeypatch):
    """A burst of file events must not start a second sync mid-flight."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)

        automator = Automator(reporter=lambda _m: None, config=config)
        automator._sync_lock.acquire()
        assert automator.sync_now("file changed") is False
        automator._sync_lock.release()
        print("✅ test_overlapping_triggers_do_not_stack passed")


def test_live_sync_fires_on_a_real_file_change(monkeypatch):
    """End-to-end through watchdog: touching a watched folder syncs, once,
    after the writes settle."""
    with tempfile.TemporaryDirectory() as tmp:
        watched = os.path.join(tmp, "watched")
        os.makedirs(watched)
        os.environ["XDG_CONFIG_HOME"] = tmp
        config = Config()
        config.add_or_update_server("nas", "192.168.1.50", "u", "p", "share", 445)
        config.set_active_server("nas")
        config.add_pair(watched, "/dest", "pair")

        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        import nasync.core.automation as automation_module

        syncs = []

        class _FakeSyncer:
            def __init__(self, *a, **k):
                pass

            def sync_pair(self, pair_name=None):
                syncs.append(pair_name)
                return {"ok": 1, "failed": 0}

        monkeypatch.setattr(automation_module, "Syncer", _FakeSyncer)
        monkeypatch.setattr(Automator, "DEBOUNCE", 0.2)

        automator = Automator(reporter=lambda _m: None, config=config)
        observer, handlers = automator._start_watching()
        try:
            # A burst of writes, as an editor would produce.
            for i in range(3):
                with open(os.path.join(watched, "note.txt"), "w") as f:
                    f.write(f"take {i}")

            deadline = time.monotonic() + 5
            while not syncs and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            Automator._stop_watching(observer, handlers)

        assert syncs == [None], syncs      # fired, and coalesced into one run
        print("✅ test_live_sync_fires_on_a_real_file_change passed")


def _fake_syncer(monkeypatch, result=None):
    import nasync.core.automation as automation_module

    class _FakeSyncer:
        def __init__(self, *a, **k):
            pass

        def sync_pair(self, pair_name=None):
            return result or {"ok": 1, "failed": 0, "transferred": 3,
                              "skipped": 9, "deleted": 1, "errors": 0}

    monkeypatch.setattr(automation_module, "Syncer", _FakeSyncer)


def test_a_background_sync_announces_itself(monkeypatch, recording_notifier):
    """Nobody is watching the log, so the notification is the only report a
    background sync makes."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch)

        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        assert automator.sync_now("file changed") is True
        # The summary waits for the burst to go quiet; flushing is what the
        # timer does 20 seconds later, and is how a test sees it now.
        automator.flush_notification()

        started, finished = recording_notifier.sent
        assert started[0] == "Syncing…"
        assert "file changed" in started[1]
        assert finished[0] == "Sync complete"
        assert "3 transferred" in finished[1]
        assert "nas" in finished[1]
        print("✅ test_a_background_sync_announces_itself passed")


def test_a_sync_with_errors_says_so_in_the_notification(monkeypatch, recording_notifier):
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch, result={"ok": 1, "failed": 0, "errors": 2,
                                          "transferred": 1, "skipped": 0,
                                          "deleted": 0})

        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        automator.sync_now("startup")
        automator.flush_notification()

        summary, _body, urgency = recording_notifier.sent[-1]
        assert summary == "Sync finished with 2 error(s)"
        # Not critical: a couple of files that didn't copy shouldn't leave a
        # notification on screen until it's dismissed.
        assert urgency == "normal"
        print("✅ test_a_sync_with_errors_says_so_in_the_notification passed")


def test_a_scheduled_sync_that_cannot_run_says_so_once(monkeypatch, recording_notifier):
    """The day's run stays due until it succeeds, so this path is reached on
    every tick — it must not post a notification per second."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: False)

        described = 0

        def describe(ip, port=445):
            nonlocal described
            described += 1
            return "Not on the home network."

        monkeypatch.setattr(lan, "describe", describe)

        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        automator.LAN_CACHE_TTL = 0

        for _ in range(5):
            assert automator.sync_now("scheduled 02:00", scheduled=True) is False

        assert len(recording_notifier.sent) == 1, recording_notifier.sent
        # Describing *why* costs a blocking TCP probe, so the repeat ticks must
        # not even compute it — only the one notification that got shown did.
        assert described == 1, described
        summary, body, urgency = recording_notifier.sent[0]
        assert summary == "Scheduled sync didn't run"
        assert "home network" in body
        # Sticky: a backup that never happened is worth interrupting for.
        assert urgency == "critical"
        print("✅ test_a_scheduled_sync_that_cannot_run_says_so_once passed")


def test_being_away_is_only_worth_saying_for_the_scheduled_run(monkeypatch, recording_notifier):
    """A live-sync trigger away from home is the design working, not a
    failure — notifying on every edit made off-network would be noise."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: False)

        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        assert automator.sync_now("file changed") is False
        assert recording_notifier.sent == []
        print("✅ test_being_away_is_only_worth_saying_for_the_scheduled_run passed")


def test_a_recovered_run_lets_the_next_failure_be_news_again(monkeypatch, recording_notifier):
    """Otherwise the once-only guard would swallow a genuinely new failure a
    week later, because it happened to match the last one."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        home = {"value": False}
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: home["value"])
        monkeypatch.setattr(lan, "describe", lambda ip, port=445: "Away.")
        _fake_syncer(monkeypatch)

        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        automator.LAN_CACHE_TTL = 0

        automator.sync_now("scheduled 02:00", scheduled=True)
        home["value"] = True
        automator.sync_now("scheduled 02:00", scheduled=True)
        home["value"] = False
        automator.sync_now("scheduled 02:00", scheduled=True)

        blocked = [s for s in recording_notifier.sent if s[0] == "Scheduled sync didn't run"]
        assert len(blocked) == 2, recording_notifier.sent
        print("✅ test_a_recovered_run_lets_the_next_failure_be_news_again passed")


def test_the_notifications_switch_reaches_a_running_service(monkeypatch, recording_notifier):
    """`Config` caches the file, so a service that only watched the mtime kept
    acting on the settings it started with."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch)

        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        # Watching the file is the loop's job, so it is the runner that carries
        # a change through to every server's notifier.
        runner = AutomationRunner([automator], config=config,
                                  reporter=lambda _m: None)
        runner._settings_changed()             # prime the mtime

        # Another process — the desktop app — turns notifications off.
        Config().set_notifications(False)
        assert runner._settings_changed() is True
        assert recording_notifier.enabled is False

        automator.sync_now("file changed")
        assert recording_notifier.sent == []
        print("✅ test_the_notifications_switch_reaches_a_running_service passed")


def test_running_with_everything_off_is_an_error():
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        automator = Automator(reporter=lambda _m: None, config=config)
        try:
            automator.run(sync_at_start=False)
            raise AssertionError("expected a refusal to run with nothing enabled")
        except Exception as e:
            assert "Nothing to automate" in str(e), e
        print("✅ test_running_with_everything_off_is_an_error passed")


# --------------------------------------------------------------------------- #
# A failed scheduled run must stay due (0.14.1)
# --------------------------------------------------------------------------- #
_ALL_FAILED = {"ok": 0, "failed": 2, "cancelled": False, "transferred": 0,
               "skipped": 0, "deleted": 0, "errors": 0}
_PARTIAL = {"ok": 1, "failed": 1, "cancelled": False, "transferred": 2,
            "skipped": 0, "deleted": 0, "errors": 0}


def test_a_scheduled_run_where_everything_failed_is_not_recorded_as_done(
        monkeypatch, recording_notifier):
    """`Syncer` reports per-pair failures instead of raising, so a run that
    transferred nothing used to count as the day's backup and never retry —
    contradicting this module's own docstring."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch, result=_ALL_FAILED)

        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        assert automator.sync_now("scheduled 02:00", scheduled=True) is False
        print("✅ test_a_scheduled_run_where_everything_failed_is_not_recorded passed")


def test_a_partly_successful_run_still_counts_as_the_days_sync(monkeypatch,
                                                               recording_notifier):
    """Otherwise one pair on an unmounted disk keeps the day's run due forever,
    re-syncing every healthy pair on each retry."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch, result=_PARTIAL)

        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        assert automator.sync_now("scheduled 02:00", scheduled=True) is True
        print("✅ test_a_partly_successful_run_still_counts passed")


def test_a_total_failure_is_announced_once_not_once_per_retry(monkeypatch,
                                                              recording_notifier):
    """The day's run correctly stays due, so this path is reached repeatedly."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch, result=_ALL_FAILED)

        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        for _ in range(4):
            automator.sync_now("scheduled 02:00", scheduled=True)

        blocked = [s for s in recording_notifier.sent
                   if s[0] == "Scheduled sync didn't run"]
        assert len(blocked) == 1, recording_notifier.sent
        print("✅ test_a_total_failure_is_announced_once passed")


def test_a_failed_scheduled_run_backs_off_before_retrying():
    """Without a gap, a NAS rejecting everything would be hit with a full sync
    attempt every tick — once a second — until midnight."""
    with tempfile.TemporaryDirectory() as tmp:
        automator = Automator(reporter=lambda _m: None, config=_configured(tmp))

        assert automator._scheduled_retry_due() is True     # nothing failed yet
        automator._scheduled_retry_at = time.monotonic() + 900
        assert automator._scheduled_retry_due() is False
        automator._scheduled_retry_at = time.monotonic() - 1
        assert automator._scheduled_retry_due() is True     # window elapsed
        assert automator._scheduled_retry_at is None        # and cleared
        print("✅ test_a_failed_scheduled_run_backs_off_before_retrying passed")


# --------------------------------------------------------------------------- #
# Starting the service must not sync unasked (0.14.1)
# --------------------------------------------------------------------------- #
def _startup_syncs(monkeypatch, config, **run_kwargs) -> list:
    """Run the loop just long enough to see whether it synced on startup."""
    import threading

    triggers = []
    monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
    monkeypatch.setattr(Automator, "sync_now",
                        lambda self, trigger="", scheduled=False:
                        triggers.append(trigger) or True)

    automator = Automator(reporter=lambda _m: None, config=config)
    thread = threading.Thread(target=lambda: automator.run(**run_kwargs),
                              daemon=True)
    thread.start()
    time.sleep(0.4)
    automator.stop()
    thread.join(timeout=5)
    return triggers


def test_a_schedule_only_service_does_not_sync_when_it_starts(monkeypatch):
    """`service.apply()` restarts the unit after any automation change, and
    systemd restarts it on failure every 30s. Syncing unconditionally meant
    changing the scheduled *time* immediately performed a mirror push."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        config.set_automation(scheduled_sync=True, live_sync=False,
                              scheduled_time="23:59")
        assert _startup_syncs(monkeypatch, config) == []
        print("✅ test_a_schedule_only_service_does_not_sync_when_it_starts passed")


def test_live_sync_still_catches_up_on_startup(monkeypatch):
    """watchdog only sees changes made while it runs, so this trigger has a
    real gap that a startup sync fills."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        config.set_automation(live_sync=True, scheduled_sync=False)
        assert _startup_syncs(monkeypatch, config) == ["startup"]
        print("✅ test_live_sync_still_catches_up_on_startup passed")


def test_startup_sync_can_still_be_forced_or_refused(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        config.set_automation(scheduled_sync=True, live_sync=False,
                              scheduled_time="23:59")
        assert _startup_syncs(monkeypatch, config,
                              sync_at_start=True) == ["startup"]

        config.set_automation(live_sync=True, scheduled_sync=False)
        assert _startup_syncs(monkeypatch, config, sync_at_start=False) == []
        print("✅ test_startup_sync_can_still_be_forced_or_refused passed")


if __name__ == "__main__":
    test_scheduled_run_waits_for_its_time()
    test_scheduled_run_happens_once_a_day()
    test_a_missed_run_catches_up_rather_than_being_skipped()
    test_running_with_everything_off_is_an_error()


# --------------------------------------------------------------------------- #
# Per-server automation (0.18.0)
# --------------------------------------------------------------------------- #
def _two_servers(tmp):
    """Two servers, each with its own folder, mode and triggers."""
    os.environ["XDG_CONFIG_HOME"] = tmp
    config = Config()
    for name, ip in (("home", "192.168.1.50"), ("work", "10.9.9.9")):
        folder = os.path.join(tmp, name)
        os.makedirs(folder, exist_ok=True)
        config.add_or_update_server(name, ip, "u", "p", f"{name}-share", 445)
        config.add_pair(folder, f"/{name}", f"{name}-pair", server=name)
    config.set_active_server("home")
    return config


def _recording_syncer(monkeypatch, result=None):
    """Capture which server each Syncer was built for, in order."""
    import nasync.core.automation as automation_module
    seen = []

    class _FakeSyncer:
        def __init__(self, *a, **k):
            self._server = k.get("server")

        def sync_pair(self, pair_name=None):
            seen.append(self._server)
            return result or {"ok": 1, "failed": 0, "transferred": 1,
                              "skipped": 0, "deleted": 0, "errors": 0}

    monkeypatch.setattr(automation_module, "Syncer", _FakeSyncer)
    return seen


def test_each_server_syncs_under_its_own_mode(monkeypatch):
    """The whole point: one NAS mirroring while another only ever adds."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _two_servers(tmp)
        config.set_sync_mode("mirror", server="home")
        config.set_sync_mode("update", server="work")

        from nasync.core.sync import Syncer
        assert Syncer(config=config, server="home").config.get_sync_mode("home") == "mirror"
        assert Syncer(config=config, server="work").config.get_sync_mode("work") == "update"

        # And the automator hands its own server to the syncer it builds.
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        seen = _recording_syncer(monkeypatch)
        for name in ("home", "work"):
            Automator(reporter=lambda _m: None, config=config,
                      server=name).sync_now("test")
        assert seen == ["home", "work"], seen
        print("✅ test_each_server_syncs_under_its_own_mode passed")


def test_the_network_gate_is_asked_per_server(monkeypatch):
    """A work NAS is never on the home network, and must not block the home one."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _two_servers(tmp)
        reachable = {"192.168.1.50": True, "10.9.9.9": False}
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: reachable[ip])
        monkeypatch.setattr(lan, "describe", lambda ip, port=445: "not on that network")
        seen = _recording_syncer(monkeypatch)

        home = Automator(reporter=lambda _m: None, config=config, server="home")
        work = Automator(reporter=lambda _m: None, config=config, server="work")
        assert home.sync_now("test") is True
        assert work.sync_now("test") is False
        assert seen == ["home"], seen
        print("✅ test_the_network_gate_is_asked_per_server passed")


def test_scheduled_runs_are_recorded_against_their_own_server(monkeypatch):
    """One NAS failing must not mark another's day as done."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _two_servers(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _recording_syncer(monkeypatch)

        home = Automator(reporter=lambda _m: None, config=config, server="home")
        settings = config.get_automation("home")
        settings["scheduled_time"] = "00:01"
        home.run_scheduled(settings)

        today = datetime.date.today().isoformat()
        assert config.get_automation("home")["last_scheduled_run"] == today
        assert config.get_automation("work")["last_scheduled_run"] is None
        print("✅ test_scheduled_runs_are_recorded_against_their_own_server passed")


def test_a_failed_scheduled_run_backs_off_without_marking_the_day_done(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _two_servers(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _recording_syncer(monkeypatch, result=_ALL_FAILED)

        home = Automator(reporter=lambda _m: None, config=config, server="home")
        home.run_scheduled(config.get_automation("home"))

        assert config.get_automation("home")["last_scheduled_run"] is None
        # Backed off, so the once-a-second loop can't hammer a failing NAS.
        assert home._scheduled_retry_due() is False
        print("✅ test_a_failed_scheduled_run_backs_off_without_marking_the_day_done passed")


def test_syncs_never_overlap_across_servers(monkeypatch):
    """Watchers run per server, but the sync itself is serialised — the
    guarantee this module's docstring makes."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _two_servers(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _recording_syncer(monkeypatch)

        lock = threading.Lock()
        home = Automator(reporter=lambda _m: None, config=config,
                         server="home", sync_lock=lock)
        work = Automator(reporter=lambda _m: None, config=config,
                         server="work", sync_lock=lock)

        lock.acquire()                      # stand in for a sync in flight
        try:
            assert work.sync_now("test") is False   # skipped, not queued
        finally:
            lock.release()
        assert work.sync_now("test") is True
        print("✅ test_syncs_never_overlap_across_servers passed")


def test_run_all_drives_only_the_automated_servers(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _two_servers(tmp)
        # Nothing on anywhere: refuse rather than idle in the background.
        try:
            run_all(reporter=lambda _m: None, config=config)
            raise AssertionError("expected a refusal with no triggers on")
        except Exception as e:
            assert "Nothing to automate" in str(e), e

        config.set_automation(live_sync=True, server="work")
        assert config.automated_servers() == ["work"]

        driven = []
        import nasync.core.automation as automation_module
        real_runner = automation_module.AutomationRunner

        class _CapturingRunner(real_runner):
            def run(self, sync_at_start=None):
                # An automator exists per configured server, so that one being
                # automated later can be noticed; what matters is which the
                # loop will actually act on.
                driven.extend(a.automator.server for a in self._states
                              if self._runnable(a))
                return None                 # don't actually loop

        monkeypatch.setattr(automation_module, "AutomationRunner", _CapturingRunner)
        run_all(reporter=lambda _m: None, config=config)
        # 'home' has no triggers, so it is not watched or scheduled at all.
        assert driven == ["work"], driven
        print("✅ test_run_all_drives_only_the_automated_servers passed")


def test_a_server_automated_while_running_is_picked_up(monkeypatch):
    """The docs promise settings changes reach a running service without a
    restart. Iterating only the set that was automated at startup broke that
    for a second NAS — invisible until the process happened to restart."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _two_servers(tmp)
        config.set_automation(live_sync=True, server="home")
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        seen = _recording_syncer(monkeypatch)

        lock = threading.Lock()
        automators = [Automator(reporter=lambda _m: None, config=config,
                                server=name, sync_lock=lock)
                      for name in ("home", "work")]
        runner = AutomationRunner(automators, config=config,
                                  reporter=lambda _m: None)
        runner.TICK = 0.05

        # 'work' has no triggers at startup, so it starts unwatched.
        assert config.automated_servers() == ["home"]

        def enable_work_then_stop():
            time.sleep(0.3)
            # Another process — the desktop app — automates the second server.
            Config().set_automation(scheduled_sync=True, scheduled_time="00:01",
                                    server="work")
            deadline = time.monotonic() + 5
            while "work" not in seen and time.monotonic() < deadline:
                time.sleep(0.05)
            runner.stop()

        flipper = threading.Thread(target=enable_work_then_stop, daemon=True)
        flipper.start()
        runner.run(sync_at_start=False)
        flipper.join(timeout=5)

        assert "work" in seen, seen
        print("✅ test_a_server_automated_while_running_is_picked_up passed")


# --------------------------------------------------------------------------- #
# A run that changed nothing says nothing (0.18.1)
# --------------------------------------------------------------------------- #
_NOTHING_CHANGED = {"ok": 1, "failed": 0, "cancelled": False, "transferred": 0,
                    "skipped": 2257, "deleted": 0, "errors": 0, "unsupported": 0}


def _quiet_automator(config, recording_notifier):
    return Automator(reporter=lambda _m: None, config=config,
                     notifier=recording_notifier)


def test_a_run_that_moved_nothing_notifies_nothing(monkeypatch,
                                                   recording_notifier):
    """Most scheduled runs over stable folders move no files. A daily
    "0 transferred" is the noise that teaches people to ignore the channel that
    also carries "your scheduled sync didn't run"."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch, result=_NOTHING_CHANGED)

        automator = _quiet_automator(config, recording_notifier)
        assert automator.sync_now("scheduled 02:00", scheduled=True) is True

        summaries = [sent[0] for sent in recording_notifier.sent]
        assert not any("complete" in s.lower() for s in summaries), summaries
        print("✅ test_a_run_that_moved_nothing_notifies_nothing passed")


def test_the_syncing_banner_is_withdrawn_when_nothing_changed(monkeypatch,
                                                              recording_notifier):
    """"Syncing…" is posted before the run, so a quiet run has to take it back
    rather than leave it on screen for good."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch, result=_NOTHING_CHANGED)

        _quiet_automator(config, recording_notifier).sync_now("scheduled 02:00",
                                                              scheduled=True)
        assert len(recording_notifier.withdrawn) == 1, "the banner was left up"
        print("✅ test_the_syncing_banner_is_withdrawn_when_nothing_changed passed")


def test_a_run_that_moved_something_still_reports(monkeypatch,
                                                  recording_notifier):
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch)          # the default result transfers 3

        automator = _quiet_automator(config, recording_notifier)
        automator.sync_now("file changed")
        automator.flush_notification()
        summaries = [sent[0] for sent in recording_notifier.sent]
        assert any("complete" in s.lower() for s in summaries), summaries
        print("✅ test_a_run_that_moved_something_still_reports passed")


def test_a_quiet_run_with_failures_still_reports(monkeypatch,
                                                 recording_notifier):
    """Nothing got through, but something went wrong — silence would be the
    failure this module exists to prevent."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch, result={**_NOTHING_CHANGED, "errors": 2})

        automator = _quiet_automator(config, recording_notifier)
        automator.sync_now("file changed")
        automator.flush_notification()
        summaries = [sent[0] for sent in recording_notifier.sent]
        assert any("error" in s.lower() for s in summaries), summaries
        print("✅ test_a_quiet_run_with_failures_still_reports passed")


def test_deletions_alone_are_worth_reporting(monkeypatch, recording_notifier):
    """Mirror mode removing files on the server is the most consequential thing
    an unattended run does, even when it uploaded nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch, result={**_NOTHING_CHANGED, "deleted": 4})

        automator = _quiet_automator(config, recording_notifier)
        automator.sync_now("scheduled 02:00")
        automator.flush_notification()
        summaries = [sent[0] for sent in recording_notifier.sent]
        assert any("complete" in s.lower() for s in summaries), summaries
        print("✅ test_deletions_alone_are_worth_reporting passed")


# --------------------------------------------------------------------------- #
# One notification per burst, not per run (0.19.0)
# --------------------------------------------------------------------------- #
def test_a_burst_of_runs_produces_one_notification(monkeypatch,
                                                   recording_notifier):
    """Live sync fires once per lull in file activity, so copying a folder in
    produces a run every couple of seconds moving a file or two. One
    notification apiece turned a single action into a column of near-identical
    "1 transferred" banners."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch, result={"ok": 1, "failed": 0, "transferred": 1,
                                          "skipped": 0, "deleted": 0,
                                          "errors": 0, "unsupported": 0})
        automator = _quiet_automator(config, recording_notifier)
        for _ in range(7):
            automator.sync_now("file changed")

        # Nothing said yet — the burst hasn't gone quiet.
        assert [s[0] for s in recording_notifier.sent] == ["Syncing…"]

        automator.flush_notification()
        summaries = [s[0] for s in recording_notifier.sent]
        assert summaries == ["Syncing…", "Sync complete"], summaries
        body = recording_notifier.sent[-1][1]
        # The totals cover the whole burst, and say how many runs it took —
        # otherwise 7 would read as one improbable transfer.
        assert "7 transferred" in body, body
        assert "7 syncs" in body, body
        print("✅ test_a_burst_of_runs_produces_one_notification passed")


def test_one_run_alone_does_not_mention_a_run_count(monkeypatch,
                                                    recording_notifier):
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch)
        automator = _quiet_automator(config, recording_notifier)
        automator.sync_now("file changed")
        automator.flush_notification()
        assert "syncs" not in recording_notifier.sent[-1][1]
        print("✅ test_one_run_alone_does_not_mention_a_run_count passed")


def test_stopping_mid_burst_still_reports_what_moved(monkeypatch,
                                                     recording_notifier):
    """Being stopped is not a reason to swallow what a burst already did."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch)
        automator = _quiet_automator(config, recording_notifier)
        automator.sync_now("file changed")
        assert [s[0] for s in recording_notifier.sent] == ["Syncing…"]

        automator.stop()
        assert "Sync complete" in [s[0] for s in recording_notifier.sent]
        print("✅ test_stopping_mid_burst_still_reports_what_moved passed")


def test_the_syncing_banner_is_not_reposted_during_a_burst(monkeypatch,
                                                           recording_notifier):
    """Re-posting "Syncing…" over a summary that hasn't been sent yet would be
    the same flood in a different order."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch)
        automator = _quiet_automator(config, recording_notifier)
        for _ in range(5):
            automator.sync_now("file changed")
        assert [s[0] for s in recording_notifier.sent].count("Syncing…") == 1
        print("✅ test_the_syncing_banner_is_not_reposted_during_a_burst passed")


def test_a_laptop_left_alone_comes_back_to_one_notification(monkeypatch,
                                                            recording_notifier):
    """The bug this closes: batching within a burst still left one notification
    *per burst*, and a machine left alone runs dozens. Coming back to it meant a
    column of "1 transferred" banners rather than one summary of the day."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch, result={"ok": 1, "failed": 0, "transferred": 2,
                                          "skipped": 0, "deleted": 0,
                                          "errors": 0, "unsupported": 0})
        automator = _quiet_automator(config, recording_notifier)

        # Twelve separate bursts, each flushing as its own quiet timer would.
        for _ in range(12):
            automator.sync_now("file changed")
            automator.flush_notification()

        summaries = [s[0] for s in recording_notifier.sent]
        # One "Syncing…" at the very start, then summaries that each replace
        # the last — never twelve independent announcements.
        assert summaries.count("Syncing…") == 1, summaries
        assert all(s in ("Syncing…", "Sync complete") for s in summaries)

        body = recording_notifier.sent[-1][1]
        assert "24 transferred" in body, body     # cumulative, not per burst
        assert "12 syncs" in body, body
        print("✅ test_a_laptop_left_alone_comes_back_to_one_notification passed")


def test_only_one_notification_is_ever_on_screen(monkeypatch,
                                                 recording_notifier):
    """Each summary withdraws the one before it, so the tray holds one entry
    however long the service has been running."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch)
        automator = _quiet_automator(config, recording_notifier)

        for _ in range(5):
            automator.sync_now("file changed")
            automator.flush_notification()

        # The double counts what the real notifier does: every post after the
        # first takes its predecessor down.
        assert len(recording_notifier.withdrawn) >= 4, recording_notifier.withdrawn
        print("✅ test_only_one_notification_is_ever_on_screen passed")


def test_a_long_gap_starts_the_count_again(monkeypatch, recording_notifier):
    """Otherwise the running total never resets and a week later reads as one
    absurd sync."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch, result={"ok": 1, "failed": 0, "transferred": 3,
                                          "skipped": 0, "deleted": 0,
                                          "errors": 0, "unsupported": 0})
        automator = _quiet_automator(config, recording_notifier)

        automator.sync_now("file changed")
        automator.flush_notification()
        assert "3 transferred" in recording_notifier.sent[-1][1]

        # Pretend the machine sat idle past the session window.
        automator._session_at -= automator.NOTIFY_SESSION_SECONDS + 1
        automator.sync_now("file changed")
        automator.flush_notification()

        body = recording_notifier.sent[-1][1]
        assert "3 transferred" in body, body      # a fresh episode, not 6
        assert "syncs" not in body, body
        print("✅ test_a_long_gap_starts_the_count_again passed")


# --------------------------------------------------------------------------- #
# 0.20.0 — nothing a watcher saw is dropped, and the notifications say what
# actually happened
# --------------------------------------------------------------------------- #
def _counting_syncer(monkeypatch, result=None):
    import nasync.core.automation as automation_module
    runs = []

    class _FakeSyncer:
        def __init__(self, *a, **k):
            self.kwargs = k

        def sync_pair(self, pair_name=None):
            runs.append(self.kwargs.get("trigger"))
            return dict(result or {"ok": 1, "failed": 0, "transferred": 1,
                                   "skipped": 4, "deleted": 0, "errors": 0})

    monkeypatch.setattr(automation_module, "Syncer", _FakeSyncer)
    return runs


def test_an_edit_made_during_another_sync_is_synced_afterwards(monkeypatch):
    """It used to find the lock busy and be dropped until the next edit."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        runs = _counting_syncer(monkeypatch)
        automator = Automator(reporter=lambda _m: None, config=config)

        automator._sync_lock.acquire()          # another server is syncing
        automator.on_files_changed()
        assert runs == [] and automator.changes_pending()
        assert not automator.catch_up_due(), "must wait for the lock"
        automator._sync_lock.release()

        assert automator.catch_up_due()
        assert automator.catch_up() is True
        assert runs == ["catch-up"] and not automator.changes_pending()


def test_coming_home_syncs_what_changed_while_away(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        home = {"here": False}
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: home["here"])
        runs = _counting_syncer(monkeypatch)
        automator = Automator(reporter=lambda _m: None, config=config)
        automator.LAN_CACHE_TTL = 0

        automator.on_files_changed()
        assert runs == [] and automator.changes_pending()
        assert automator.catch_up() is False
        automator._catch_up_at = None           # skip the short pause
        home["here"] = True
        assert automator.catch_up() is True and runs == ["catch-up"]


def test_a_sync_running_in_another_process_is_waited_for_silently(
        monkeypatch, recording_notifier):
    from nasync.core.activity import Activity
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        runs = _counting_syncer(monkeypatch)
        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        with Activity(config.config_dir, source="app"):
            automator.on_files_changed()
        assert runs == [] and recording_notifier.sent == []
        assert automator.last_skip == "busy" and automator.changes_pending()


def test_a_run_that_changed_nothing_leaves_the_last_summary_up(
        monkeypatch, recording_notifier):
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch)
        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        automator.sync_now("file changed")
        automator.flush_notification()
        withdrawn = len(recording_notifier.withdrawn)

        _fake_syncer(monkeypatch, result=_NOTHING_CHANGED)
        automator.sync_now("file changed")
        assert len(recording_notifier.withdrawn) == withdrawn, \
            "a quiet run took the previous summary off screen"


def test_syncing_is_announced_again_once_an_episode_is_over(
        monkeypatch, recording_notifier):
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch)
        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        automator.sync_now("scheduled 19:25")
        automator.flush_notification()
        automator._session_at -= automator.NOTIFY_SESSION_SECONDS + 1
        automator.sync_now("scheduled 19:25")
        titles = [s[0] for s in recording_notifier.sent]
        assert titles.count("Syncing…") == 2, titles


def test_an_episode_total_says_it_is_one_and_does_not_sum_unchanged(
        monkeypatch, recording_notifier):
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _counting_syncer(monkeypatch)       # 1 transferred, 4 unchanged each
        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        for _ in range(5):
            automator.sync_now("file changed")
        automator.flush_notification()
        body = recording_notifier.sent[-1][1]
        assert "5 transferred" in body and "5 syncs since" in body, body
        assert "20 unchanged" not in body, body


def test_a_held_back_mirror_run_is_announced_once_as_needing_a_person(
        monkeypatch, recording_notifier):
    refused = {"ok": 0, "failed": 1, "cancelled": False, "transferred": 0,
               "skipped": 0, "deleted": 0, "errors": 0,
               "refused": [{"pair": "Programming", "confirmable": True,
                            "message": "this would delete 23,000 of 23,000"}]}
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _fake_syncer(monkeypatch, result=refused)
        automator = Automator(reporter=lambda _m: None, config=config,
                              notifier=recording_notifier)
        automator.sync_now("scheduled 19:25")
        automator.flush_notification()
        title, body, urgency = recording_notifier.sent[-1]
        assert title.startswith("Sync held back") and urgency == "critical"
        assert "Programming" in body, body

        posted = len(recording_notifier.sent)
        automator.sync_now("catch-up")          # the same refusal, again
        automator.flush_notification()
        assert all(s[0] != "Sync held back — open NASync to review"
                   for s in recording_notifier.sent[posted:]), \
            "the same refusal was announced twice"


def test_a_cancelled_background_run_is_not_retried_behind_the_users_back(
        monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _configured(tmp)
        monkeypatch.setattr(lan, "on_lan", lambda ip, port=445: True)
        _counting_syncer(monkeypatch, result={"ok": 0, "failed": 0,
                                              "cancelled": True})
        automator = Automator(reporter=lambda _m: None, config=config)
        automator.on_files_changed()
        assert not automator.changes_pending()
