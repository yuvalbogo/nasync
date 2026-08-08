"""Automation layer: keep folders synced without being asked.

Two independent triggers, both **off** by default, usable together:

  * **Live sync** — watch the local folders and sync shortly after edits
    settle, but only while this machine is on the same network as the server
    (see :mod:`nasync.lan`). Away from home the watcher keeps running and
    simply doesn't fire, so coming home syncs whatever piled up.

  * **Scheduled sync** — sync once a day at a set time. A run that couldn't
    happen (machine asleep, or away from home) is not lost: the day's run stays
    due until it succeeds, so it catches up on wake or on arriving home.

A legacy "every N minutes" trigger is still honoured for anyone who set one.

One lock guarantees syncs never overlap, and the loop re-reads the config when
it changes on disk — so toggling a switch in the desktop app takes effect in
the background service without restarting it.
"""

import datetime
import os
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from nasync.system import lan
from nasync.core.config import Config
from nasync.system.notify import Notifier
from nasync.core.sync import Syncer


class _DebouncedHandler(FileSystemEventHandler):
    """Coalesce a burst of file events and fire `trigger` once things settle."""

    def __init__(self, trigger, debounce: float = 2.0):
        self._trigger = trigger
        self._debounce = debounce
        self._timer = None
        self._lock = threading.Lock()

    def on_any_event(self, event):
        if event.is_directory:
            return
        if getattr(event, "event_type", None) in ("opened", "closed"):
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._trigger)
            self._timer.daemon = True
            self._timer.start()

    def cancel(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()


def scheduled_run_is_due(scheduled_time: str, last_run: str, now=None) -> bool:
    """True when today's scheduled sync should have happened and hasn't.

    Deliberately "has today's slot passed and not run yet" rather than "is it
    exactly HH:MM": a machine that was asleep or away at the appointed minute
    still owes the day a sync, and gets it at the first opportunity.
    """
    now = now or datetime.datetime.now()
    hour, minute = (int(part) for part in scheduled_time.split(":"))
    due_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < due_at:
        return False
    return last_run != now.date().isoformat()


class Automator:
    """Runs the automatic triggers until stopped."""

    # How long to wait before re-checking a config file that hasn't changed.
    TICK = 1.0
    # Editors write a file several times in quick succession; sync once, after
    # the last of them.
    DEBOUNCE = 2.0
    # How long a "am I on the home network?" answer is trusted. Without this a
    # pending scheduled sync would re-probe the network every tick; with it,
    # coming home is still noticed within half a minute.
    LAN_CACHE_TTL = 30.0
    # How long to wait before retrying a scheduled sync in which every pair
    # failed. The day's run correctly stays due, and the loop ticks once a
    # second — so without a gap here a NAS that rejects everything would be
    # hammered with a full sync attempt per second until midnight.
    SCHEDULED_RETRY_SECONDS = 900.0

    def __init__(self, reporter=print, config=None, notifier=None):
        self.config = config or Config()
        self._report = reporter
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._config_mtime = None
        self._only_pair = None      # `nasync watch --pair` narrows to just one
        self._lan_state = False
        self._lan_checked_at = None
        self._reported_lan = None   # None = nothing said yet, so the first
                                    # verdict is always worth logging
        # This is the one place a sync happens with nobody watching, so the
        # desktop notification *is* the user interface for it.
        self._notifier = (notifier if notifier is not None
                          else Notifier(enabled=self.config.get_notifications()))
        self._blocked_notice = None
        self._lan_reason_text = None
        self._lan_reason_at = None
        # When a wholly-failed scheduled run may be attempted again.
        self._scheduled_retry_at = None

    # ------------------------------------------------------------------ #
    # Syncing
    # ------------------------------------------------------------------ #
    def on_lan(self) -> bool:
        """Are we on the server's network? Cached for `LAN_CACHE_TTL` seconds."""
        now = time.monotonic()
        if (self._lan_checked_at is not None
                and now - self._lan_checked_at < self.LAN_CACHE_TTL):
            return self._lan_state

        server = self.config.get_server_config()
        state = bool(server.get("ip")) and lan.on_lan(server["ip"],
                                                      server.get("port", 445))
        self._lan_state = state
        self._lan_checked_at = now
        return state

    def _report_lan_change(self, state: bool) -> None:
        """Log arriving and leaving, not every check in between.

        This runs as a background service for days at a time: a line per poll
        would bury the syncs that actually happened.
        """
        if state == self._reported_lan:
            return
        self._reported_lan = state
        if state:
            self._report("▶️  Back on the home network — automatic syncing resumed.")
        else:
            self._report(f"⏸️  Paused — {self._lan_reason()}")

    def sync_now(self, trigger: str = "", scheduled: bool = False) -> bool:
        """Sync every pair. Returns True only if at least one pair succeeded.

        Skips (rather than fails) when another sync is in flight or the server
        isn't on this network — both are ordinary conditions for a background
        process, not errors worth waking anyone over.

        **A run in which every pair failed returns False**, because the caller
        uses this to decide whether the day's scheduled sync is done. `Syncer`
        reports per-pair failures in its result rather than raising, so a run
        that transferred nothing at all — share renamed, permissions changed,
        the local folder not mounted — used to be recorded as the day's
        backup and never retried. That is the exact failure this module's
        docstring promises not to have.

        A *partial* success still counts. Otherwise one pair pointing at an
        unmounted disk would keep the day's run due forever, re-syncing every
        healthy pair on every retry.

        `scheduled=True` marks the daily run, which is the one case where *not*
        syncing is worth a notification: nobody is watching the log, and a
        scheduled sync that silently never happens is the failure people only
        discover when they need the backup.
        """
        if not self._sync_lock.acquire(blocking=False):
            # Already syncing; this trigger is moot and the day's run is still
            # due, so it'll be picked up on a later tick. Not worth reporting.
            return False
        try:
            state = self.on_lan()
            self._report_lan_change(state)
            if not state:
                if scheduled:
                    # The detail is a callable so the guard can reject a repeat
                    # before it's computed: describing *why* we're off-network
                    # costs a blocking TCP probe, and this runs every tick for
                    # as long as the day's run stays due.
                    self._notify_scheduled_blocked("off-network", self._lan_reason)
                return False
            label = f" ({trigger})" if trigger else ""
            self._report(f"\n🔄 Auto-sync at {time.strftime('%H:%M:%S')}{label}")
            self._notify_started(trigger)
            # The Automator's Config, not a fresh one: `_settings_changed`
            # reloads it when the file changes, and a Syncer that built
            # its own would sync from the settings as they were at start.
            result = Syncer(reporter=self._report,
                            config=self.config).sync_pair(self._only_pair)
            self._notify_finished(result)
            if not result.get("ok"):
                # Nothing got through. Reported as such on the scheduled path so
                # the day's run stays due, and announced under the once-a-day
                # guard rather than by `_notify_finished`, which would repeat on
                # every retry.
                if scheduled:
                    self._notify_scheduled_blocked(
                        "every pair failed",
                        f"{result.get('failed', 0)} folder(s) couldn't be "
                        f"synced. NASync will try again shortly.")
                return False
            # A run got through, so the next failure is news again rather than
            # a repeat of one already announced.
            self._blocked_notice = None
            return True
        except Exception as e:
            self._report(f"❌ {e}")
            if scheduled:
                self._notify_scheduled_blocked(str(e))
            else:
                self._notifier.notify("Sync failed", str(e), urgency="critical")
            return False
        finally:
            self._sync_lock.release()

    # ------------------------------------------------------------------ #
    # Notifications
    # ------------------------------------------------------------------ #
    def _lan_reason(self) -> str:
        """Why we aren't syncing — computed at most once per LAN check.

        Both the log line and the notification want this sentence, and
        producing it costs another blocking probe, so it's cached against the
        check that prompted it rather than recomputed per caller.
        """
        if self._lan_reason_text and self._lan_reason_at == self._lan_checked_at:
            return self._lan_reason_text

        server = self.config.get_server_config()
        self._lan_reason_text = (
            lan.describe(server["ip"], server.get("port", 445))
            if server.get("ip") else "No server selected.")
        self._lan_reason_at = self._lan_checked_at
        return self._lan_reason_text

    def _server_label(self) -> str:
        return self.config.get_active_server_name() or "your NAS"

    def _notify_started(self, trigger: str) -> None:
        self._notifier.notify(
            "Syncing…",
            f"{self._server_label()}  ·  started by {trigger or 'request'}")

    def _notify_finished(self, result: dict) -> None:
        """Replace the "started" notification with what the run achieved.

        Errors are left at normal urgency: a couple of files that didn't copy
        say so in the summary, but they don't warrant a notification that sits
        on screen until dismissed the way a run that never happened does.
        """
        problems = result.get("failed", 0) + result.get("errors", 0)
        counts = "  ·  ".join([
            f"{result.get('transferred', 0)} transferred",
            f"{result.get('skipped', 0)} unchanged",
            f"{result.get('deleted', 0)} removed",
        ])
        self._notifier.notify(
            f"Sync finished with {problems} error(s)" if problems else "Sync complete",
            f"{self._server_label()}  ·  {counts}")

    def _notify_scheduled_blocked(self, cause: str, detail=None) -> None:
        """Say once that today's scheduled sync couldn't run.

        The day's run stays due until it succeeds, so this is reached on every
        tick until then — without the guard it would post a notification per
        second. Keyed on the cause as well as the date, so a machine that goes
        from "away from home" to "the server refused the connection" says so.

        `detail` may be a callable, evaluated only if this is going to be
        shown; see the call site in `sync_now` for why that matters.
        """
        key = (datetime.date.today().isoformat(), cause)
        if key == self._blocked_notice:
            return
        self._blocked_notice = key
        body = detail() if callable(detail) else (detail or cause)
        self._notifier.notify("Scheduled sync didn't run", body,
                              urgency="critical")

    # ------------------------------------------------------------------ #
    # Watching
    # ------------------------------------------------------------------ #
    def _watched_pairs(self):
        pairs = self.config.get_pairs()
        if self._only_pair:
            pairs = [p for p in pairs if p["name"] == self._only_pair]
        return pairs

    def _start_watching(self):
        observer = Observer()
        handlers = []
        for pair in self._watched_pairs():
            if os.path.isdir(pair["local"]):
                handler = _DebouncedHandler(lambda: self.sync_now("file changed"),
                                            debounce=self.DEBOUNCE)
                handlers.append(handler)
                observer.schedule(handler, pair["local"], recursive=True)
            else:
                self._report(f"⚠️  Not watching '{pair['name']}': "
                             f"{pair['local']} doesn't exist")
        observer.start()
        self._report(f"👁️  Live sync watching {len(handlers)} folder(s)")
        return observer, handlers

    @staticmethod
    def _stop_watching(observer, handlers):
        for handler in handlers:
            handler.cancel()
        if observer:
            observer.stop()
            observer.join()

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def stop(self):
        self._stop.set()

    def _scheduled_retry_due(self) -> bool:
        """False only while backing off after a scheduled run that got nowhere.

        Note this gates the *retry*, not the first attempt of the day: nothing
        sets the timer until a run has actually failed outright.
        """
        if self._scheduled_retry_at is None:
            return True
        if time.monotonic() < self._scheduled_retry_at:
            return False
        self._scheduled_retry_at = None
        return True

    def _settings_changed(self) -> bool:
        """True when config.json has been rewritten since we last looked.

        Re-reads it too: `Config` caches the file in memory, so a service that
        only noticed the mtime would keep acting on the settings it started
        with — which is exactly the switch the user just flipped in the app.
        """
        try:
            mtime = self.config.config_file.stat().st_mtime
        except OSError:
            return False
        if mtime == self._config_mtime:
            return False
        self._config_mtime = mtime
        self.config.reload()
        self._notifier.enabled = self.config.get_notifications()
        return True

    def run(self, live_sync=None, scheduled_sync=None, scheduled_time=None,
            interval_minutes=None, only_pair=None, sync_at_start=None):
        """Run until stopped. Arguments override the saved settings.

        `sync_at_start` defaults to **only when live sync is on**, which is a
        deliberate narrowing. This process is restarted far more often than it
        looks: `service.apply()` restarts the unit after any automation setting
        change, and the systemd unit restarts it on failure every 30 seconds.
        Syncing unconditionally on start therefore meant that changing the
        *scheduled time* — a setting whose entire purpose is to defer work —
        immediately performed a full sync, in mirror mode, deleting on the
        server. Nobody asked for that and nobody is watching when it happens.

        Live sync is the one trigger with a genuine gap to fill: watchdog only
        sees changes made while it is running, so edits made while the service
        was down would otherwise wait for a later edit. A schedule-only
        configuration has no such gap — it has a slot, and `scheduled_run_is_due`
        already catches up a missed one.

        Pass it explicitly to force either behaviour.
        """
        self._only_pair = only_pair
        overrides = {
            "live_sync": live_sync,
            "scheduled_sync": scheduled_sync,
            "scheduled_time": scheduled_time,
            "interval_minutes": interval_minutes,
        }

        def settings():
            values = self.config.get_automation()
            values.update({k: v for k, v in overrides.items() if v is not None})
            return values

        self._settings_changed()             # prime the mtime
        current = settings()
        if not (current["live_sync"] or current["scheduled_sync"]
                or current["interval_minutes"] > 0):
            raise Exception(
                "Nothing to automate — live sync and scheduled sync are both "
                "off. Turn one on in the desktop app's Settings, or with "
                "'nasync live-sync on' / 'nasync scheduled-sync on'."
            )
        if not self.config.get_pairs():
            raise Exception("No sync pairs configured. Run 'nasync setup' first.")

        observer = handlers = None
        watching = False
        try:
            if current["live_sync"]:
                observer, handlers = self._start_watching()
                watching = True
            if current["scheduled_sync"]:
                self._report(f"📅 Scheduled sync every day at {current['scheduled_time']}")
            if current["interval_minutes"] > 0:
                self._report(f"⏱️  Also syncing every {current['interval_minutes']} min")
            self._report("Automation running — press Ctrl+C to stop.")

            catch_up = (bool(current["live_sync"]) if sync_at_start is None
                        else bool(sync_at_start))
            if catch_up:
                self.sync_now("startup")

            interval = current["interval_minutes"]
            next_tick = time.monotonic() + interval * 60 if interval > 0 else None

            while not self._stop.is_set():
                self._stop.wait(self.TICK)
                if self._stop.is_set():
                    break

                # Pick up switches flipped in the desktop app while we run.
                if self._settings_changed():
                    current = settings()
                    if current["live_sync"] and not watching:
                        observer, handlers = self._start_watching()
                        watching = True
                    elif not current["live_sync"] and watching:
                        self._stop_watching(observer, handlers)
                        observer = handlers = None
                        watching = False
                        self._report("👁️  Live sync switched off")
                    interval = current["interval_minutes"]
                    next_tick = (time.monotonic() + interval * 60) if interval > 0 else None

                if (current["scheduled_sync"]
                        and self._scheduled_retry_due()
                        and scheduled_run_is_due(current["scheduled_time"],
                                                 current["last_scheduled_run"])):
                    if self.sync_now(f"scheduled {current['scheduled_time']}",
                                     scheduled=True):
                        self._scheduled_retry_at = None
                        today = datetime.date.today().isoformat()
                        self.config.record_scheduled_run(today)
                        # We just wrote the file; don't treat it as a change.
                        self._settings_changed()
                        current = settings()
                    else:
                        # Still due — deliberately, so it catches up — but not
                        # again immediately. See SCHEDULED_RETRY_SECONDS.
                        self._scheduled_retry_at = (time.monotonic()
                                                    + self.SCHEDULED_RETRY_SECONDS)

                if next_tick and time.monotonic() >= next_tick:
                    self.sync_now("interval")
                    next_tick = time.monotonic() + interval * 60
        except KeyboardInterrupt:
            self._report("\n⏹️  Stopping automation…")
        finally:
            self._stop_watching(observer, handlers or [])
