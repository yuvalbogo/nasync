"""Automation layer: keep folders synced without being asked.

Two independent triggers, both **off** by default, usable together:

  * **Live sync** — watch the local folders and sync shortly after edits
    settle, but only while this machine is on the same network as the server
    (see :mod:`nasync.lan`). Away from home the watcher keeps running and
    remembers that something changed, so coming home syncs whatever piled up.
    The same goes for an edit made while another sync was running.

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
from nasync.core import activity
from nasync.core.activity import SyncBusy
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
    """One server's automatic triggers: its watchers, schedule and LAN state.

    Since 0.18.0 the triggers belong to a server rather than to the app, so
    this is per server — its own answer to "am I on that NAS's network?", its
    own catch-up date, its own watched folders. `AutomationRunner` owns the
    loop that drives one of these per automated server.

    The sync lock is shared between them on purpose: watchers run concurrently,
    but only one sync happens at a time. Two servers syncing at once would
    contend for the same uplink and break the "syncs never overlap" guarantee
    this module's docstring makes.
    """

    # How long to wait before re-checking a config file that hasn't changed.
    TICK = 1.0
    # How long file activity must stop before a sync starts. Two seconds was
    # tuned for an editor saving the same file several times, and is far too
    # short for the other thing that happens to a watched folder: copying one
    # in. A two-second lull mid-copy is ordinary, so each lull started a full
    # sync — and a sync re-indexes the whole pair, which on a 20k-file folder
    # is minutes of work to move one file. Ten seconds still feels immediate
    # for a save and stops a copy from being sliced into dozens of runs.
    DEBOUNCE = 10.0
    # How long a "am I on the home network?" answer is trusted. Without this a
    # pending scheduled sync would re-probe the network every tick; with it,
    # coming home is still noticed within half a minute.
    LAN_CACHE_TTL = 30.0
    # How long to wait before retrying a scheduled sync in which every pair
    # failed. The day's run correctly stays due, and the loop ticks once a
    # second — so without a gap here a NAS that rejects everything would be
    # hammered with a full sync attempt per second until midnight.
    SCHEDULED_RETRY_SECONDS = 900.0
    # How long after a run to wait for another before saying anything. Live
    # sync fires once per lull in file activity, and a lull is not the end of
    # the job: copying a folder in produces a run every couple of seconds, each
    # moving a file or two. Notifying per run turned one action into a column
    # of "1 transferred" banners. The syncing itself stays prompt; only the
    # telling waits, and what it then says covers everything that happened.
    NOTIFY_QUIET_SECONDS = 20.0
    # How long a gap makes the *next* sync a new episode rather than more of
    # the same one. Batching inside a burst was not enough: a laptop left alone
    # runs dozens of separate bursts, and each posted its own notification, so
    # coming back to it meant a column of them. Totals now accumulate across
    # bursts into one running summary — "12 syncs · 340 transferred" — until a
    # gap this long says the episode is over and the next one starts fresh.
    NOTIFY_SESSION_SECONDS = 4 * 3600.0
    # How soon to try again when a live-sync catch-up could not run because
    # another sync was busy or the NAS was out of reach. Short: both clear up
    # on their own, and the check itself costs a file read and a cached probe.
    CATCH_UP_PAUSE = 5.0

    def __init__(self, reporter=print, config=None, notifier=None,
                 server: str = None, sync_lock=None):
        self.config = config or Config()
        #: Which server this drives; None means the active one, which is what
        #: `nasync watch` and every pre-0.18.0 caller mean.
        self.server = server
        self._report = reporter
        # Shared when a runner drives several servers, so one sync at a time.
        self._sync_lock = sync_lock or threading.Lock()
        # Shared with the runner that drives this automator, so `stop()` on
        # either reaches the loop.
        self._stop = threading.Event()
        self._only_pair = None      # `nasync watch --pair` narrows to just one
        self._overrides = {}        # CLI flags that beat the saved settings
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
        # Results accumulated since the last notification, and the timer that
        # will report them. Touched from the timer thread as well as the loop.
        self._pending = None
        self._notify_timer = None
        self._notify_lock = threading.Lock()
        # Totals for the whole episode, surviving across bursts, and when they
        # were last shown. `None` means nothing is being reported right now.
        self._session = None
        self._session_at = 0.0
        self._session_began = None
        # Whether what NASync has on screen is its own "Syncing…" placeholder,
        # the only notification a run that changed nothing may take back.
        self._placeholder_up = False
        # Refusals already announced today, so a held-back mirror run is said
        # once rather than on every retry.
        self._held_notice = None
        # Live sync's memory. Every file event bumps `_changes_seen`; a sync
        # that gets through records how far it got. Anything seen after a sync
        # *started* is still owed one — including edits made while it ran,
        # which until 0.20.0 found the lock busy and were simply dropped.
        self._changes_lock = threading.Lock()
        self._changes_seen = 0
        self._changes_synced = 0
        self._catch_up_at = None
        #: Why the last `sync_now` didn't run: "busy", "away", or None.
        self.last_skip = None

    # ------------------------------------------------------------------ #
    # Syncing
    # ------------------------------------------------------------------ #
    def on_lan(self) -> bool:
        """Are we on the server's network? Cached for `LAN_CACHE_TTL` seconds."""
        now = time.monotonic()
        if (self._lan_checked_at is not None
                and now - self._lan_checked_at < self.LAN_CACHE_TTL):
            return self._lan_state

        server = self.config.get_server_config(self.server)
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
            # Already syncing. Not lost: the day's run stays due, and a file
            # change stays owed (see `on_files_changed`), so either is picked
            # up on a later tick. Not worth reporting.
            self.last_skip = "busy"
            return False
        try:
            self.last_skip = None
            state = self.on_lan()
            self._report_lan_change(state)
            if not state:
                self.last_skip = "away"
                if scheduled:
                    # The detail is a callable so the guard can reject a repeat
                    # before it's computed: describing *why* we're off-network
                    # costs a blocking TCP probe, and this runs every tick for
                    # as long as the day's run stays due.
                    self._notify_scheduled_blocked("off-network", self._lan_reason)
                return False
            # The app or the CLI may be mid-run in another process. Checked
            # before saying "Syncing…", so a skipped run posts nothing.
            if activity.read_status(self.config.config_dir) is not None:
                self.last_skip = "busy"
                return False
            with self._changes_lock:
                started_at_change = self._changes_seen
            label = f" ({trigger})" if trigger else ""
            self._report(f"\n🔄 Auto-sync at {time.strftime('%H:%M:%S')}{label}")
            self._notify_started(trigger)
            # The Automator's Config, not a fresh one: `_settings_changed`
            # reloads it when the file changes, and a Syncer that built
            # its own would sync from the settings as they were at start.
            try:
                result = Syncer(reporter=self._report, config=self.config,
                                server=self.server, source="background",
                                trigger=trigger or "automatic",
                                ).sync_pair(self._only_pair)
            except SyncBusy:
                # Lost a race with another process starting at the same
                # moment. Take the placeholder back and try again later.
                self._take_back_placeholder()
                self.last_skip = "busy"
                return False
            self._notify_finished(result)
            if result.get("cancelled"):
                # Someone pressed Cancel. That is an answer, not a failure to
                # retry in fifteen minutes behind their back.
                self._mark_synced(started_at_change)
                return True
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
            self._mark_synced(started_at_change)
            return True
        except Exception as e:
            self._report(f"❌ {e}")
            # The message alone has proved not to be enough: a background
            # failure is seen days later in the journal, with no way to tell
            # where it came from. Opt-in so the normal log stays readable.
            if os.environ.get("NASYNC_DEBUG"):
                import traceback
                self._report(traceback.format_exc())
            if scheduled:
                self._notify_scheduled_blocked(str(e))
            else:
                self._post("Sync failed", str(e), urgency="critical")
            return False
        finally:
            self._sync_lock.release()

    # ------------------------------------------------------------------ #
    # Live sync's memory of what is still owed
    # ------------------------------------------------------------------ #
    def on_files_changed(self) -> None:
        """A watched folder settled after changing: note it, then try to sync.

        Noted first, so that if the sync can't run now — another one is busy,
        or the NAS is out of reach — the loop knows to come back for it.
        """
        with self._changes_lock:
            self._changes_seen += 1
        self.sync_now("file changed")

    def _mark_synced(self, upto: int) -> None:
        with self._changes_lock:
            self._changes_synced = max(self._changes_synced, upto)
        self._catch_up_at = None

    def changes_pending(self) -> bool:
        with self._changes_lock:
            return self._changes_seen > self._changes_synced

    def catch_up_due(self) -> bool:
        """True when file changes are owed a sync and it is worth trying now."""
        if not self.changes_pending() or self._sync_lock.locked():
            return False
        return self._catch_up_at is None or time.monotonic() >= self._catch_up_at

    def catch_up(self) -> bool:
        """Sync changes that an earlier trigger couldn't, backing off on failure."""
        if self.sync_now("catch-up"):
            return True
        pause = (self.CATCH_UP_PAUSE if self.last_skip in ("busy", "away")
                 else self.SCHEDULED_RETRY_SECONDS)
        self._catch_up_at = time.monotonic() + pause
        return False

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

        server = self.config.get_server_config(self.server)
        self._lan_reason_text = (
            lan.describe(server["ip"], server.get("port", 445))
            if server.get("ip") else "No server selected.")
        self._lan_reason_at = self._lan_checked_at
        return self._lan_reason_text

    def _server_label(self) -> str:
        return self.server or self.config.get_active_server_name() or "your NAS"

    def _post(self, summary: str, body: str = "", urgency: str = "normal",
              placeholder: bool = False) -> None:
        """Post through the notifier, remembering whether it's the placeholder."""
        self._notifier.notify(summary, body, urgency=urgency)
        self._placeholder_up = placeholder

    def _take_back_placeholder(self) -> None:
        """Withdraw "Syncing…" — and only that, never a real summary."""
        if self._placeholder_up:
            self._notifier.close()
            self._placeholder_up = False

    def _notify_started(self, trigger: str) -> None:
        """Say a sync is under way — once per burst, not once per run.

        A burst is already being reported on if something is pending, and
        re-posting "Syncing…" over a summary that has not been sent yet would
        be the same flood in a different order.
        """
        with self._notify_lock:
            # An episode that went quiet long ago is over. Until 0.20.0 this
            # was only noticed when the *next summary* was built, so once any
            # summary had been shown, "Syncing…" never appeared again for as
            # long as the service ran.
            if (self._session is not None
                    and time.monotonic() - self._session_at
                    > self.NOTIFY_SESSION_SECONDS):
                self._session = None
            # Mid-burst, or mid-episode with a summary already on screen: the
            # slot is in use and saying "Syncing…" again would replace a real
            # total with a placeholder, over and over, all day.
            if self._pending is not None or self._session is not None:
                return
        self._post("Syncing…",
                   f"{self._server_label()}  ·  started by {trigger or 'request'}",
                   placeholder=True)

    def _notify_finished(self, result: dict) -> None:
        """Fold a run into the pending summary, and report once it goes quiet.

        **A run that changed nothing says nothing.** Most scheduled runs over
        stable folders move no files, and a daily "0 transferred" is the kind
        of noise that teaches people to ignore — or switch off — the channel
        that also carries "your scheduled sync didn't run".

        **Runs that did change something are batched.** Live sync fires once
        per lull in file activity, so copying a folder in produces a series of
        runs moving a file or two each; one notification apiece turned a single
        action into a column of near-identical banners. The totals accumulate
        and a timer reports them together once `NOTIFY_QUIET_SECONDS` pass with
        nothing further — so what arrives describes the whole thing that
        happened rather than the last slice of it.

        The "Syncing…" banner is already up by this point, so a burst that
        turns out to have moved nothing *withdraws* it rather than leaving it.
        """
        problems = result.get("failed", 0) + result.get("errors", 0)
        held = self._new_refusals(result)
        if not held:
            # A refusal already announced today is not news on its retry.
            problems -= self._known_refusals(result)
        # `unsupported` counts as worth mentioning: those files were skipped
        # because SMB rejects their names, which is a thing to act on rather
        # than a quiet success.
        moved = (result.get("transferred", 0) + result.get("deleted", 0)
                 + result.get("deleted_dirs", 0) + result.get("unsupported", 0)
                 + result.get("moved", 0))
        if not moved and problems <= 0 and not held:
            with self._notify_lock:
                idle = self._pending is None
            # Only the placeholder is taken back. A run that changed nothing
            # used to withdraw whatever was up — including the summary of the
            # run before it, which then vanished from the tray.
            if idle:
                self._take_back_placeholder()
            return

        with self._notify_lock:
            if self._pending is None:
                self._pending = dict.fromkeys(
                    ("transferred", "skipped", "deleted", "deleted_dirs",
                     "moved", "errors", "failed", "unsupported"), 0)
                self._pending["runs"] = 0
                self._pending["held"] = []
            for key in self._pending:
                if key == "skipped":
                    # How many files were already in place is a fact about the
                    # folder, not a quantity of work: the latest run's count is
                    # the true one. Summed, it read "135 unchanged" for a
                    # folder of 30 files.
                    self._pending[key] = result.get(key, 0)
                elif key not in ("runs", "held"):
                    self._pending[key] += result.get(key, 0)
            self._pending["failed"] -= (0 if held
                                        else self._known_refusals(result))
            self._pending["runs"] += 1
            self._pending["held"].extend(held)

            if self._notify_timer is not None:
                self._notify_timer.cancel()
            self._notify_timer = threading.Timer(self.NOTIFY_QUIET_SECONDS,
                                                 self.flush_notification)
            self._notify_timer.daemon = True
            self._notify_timer.start()

    def flush_notification(self) -> None:
        """Report everything this episode has done, in one notification.

        Called by the quiet timer, and directly on shutdown — a service being
        stopped mid-burst should still say what it moved rather than swallowing
        it.

        The totals are cumulative across bursts, not just across the runs in
        one. A machine left alone syncs in dozens of separate bursts, and one
        notification apiece is what filled the tray; this keeps a single
        running summary instead, which the notifier posts into a single slot.
        A gap of `NOTIFY_SESSION_SECONDS` retires the episode so the count does
        not run forever.
        """
        with self._notify_lock:
            pending = self._pending
            self._pending = None
            if self._notify_timer is not None:
                self._notify_timer.cancel()
                self._notify_timer = None
            if not pending:
                return

            now = time.monotonic()
            if (self._session is None
                    or now - self._session_at > self.NOTIFY_SESSION_SECONDS):
                self._session = {key: ([] if key == "held" else 0)
                                 for key in pending}
                self._session_began = time.time()
            for key, value in pending.items():
                if key == "skipped":
                    self._session[key] = value
                elif key == "held":
                    self._session[key] = (value or self._session[key])
                else:
                    self._session[key] += value
            self._session_at = now
            totals = dict(self._session)
            began = self._session_began

        problems = max(0, totals["failed"]) + totals["errors"]
        if totals["runs"] > 1:
            # A running total of the episode, and said to be one. Each burst
            # re-posts it, and without "since" a burst that deleted one file
            # read "Sync complete · 30 transferred". Unchanged is left out: it
            # describes one folder at one moment, not the episode.
            parts = [f"{totals['transferred']} transferred",
                     f"{totals['deleted']} removed"]
            if totals["moved"]:
                parts.append(f"{totals['moved']} moved")
            parts.append(f"{totals['runs']} syncs since "
                         f"{time.strftime('%H:%M', time.localtime(began))}")
        else:
            parts = [f"{totals['transferred']} transferred",
                     f"{totals['skipped']} unchanged",
                     f"{totals['deleted']} removed"]
            if totals["moved"]:
                parts.append(f"{totals['moved']} moved")
        counts = "  ·  ".join(parts)
        if totals["held"]:
            # A mirror run held back is the one thing here that needs a
            # person: it will not happen until someone says yes.
            self._post("Sync held back — open NASync to review",
                       f"{self._server_label()}  ·  {totals['held'][0]}"
                       + (f"  (+{len(totals['held']) - 1} more)"
                          if len(totals["held"]) > 1 else ""),
                       urgency="critical")
            return
        self._post(
            f"Sync finished with {problems} error(s)" if problems
            else "Sync complete",
            f"{self._server_label()}  ·  {counts}")

    def _refusal_key(self, result: dict):
        held = sorted(r["pair"] for r in result.get("refused", [])
                      if not r.get("dry_run"))
        clashes = sorted(result.get("overlapping", []))
        if not held and not clashes:
            return None
        return (datetime.date.today().isoformat(), tuple(held), tuple(clashes))

    def _known_refusals(self, result: dict) -> int:
        """Pairs in `result` held back for reasons already announced today."""
        key = self._refusal_key(result)
        if key is None or key != self._held_notice:
            return 0
        return len(key[1]) + len(key[2])

    def _new_refusals(self, result: dict) -> list:
        """Descriptions of held-back pairs not yet announced today."""
        key = self._refusal_key(result)
        if key is None or key == self._held_notice:
            return []
        self._held_notice = key
        lines = [f"{r['pair']}: {r['message']}" for r in result.get("refused", [])
                 if not r.get("dry_run")]
        if result.get("overlapping"):
            lines.append("skipped " + ", ".join(result["overlapping"])
                         + " — they share a server folder")
        return lines

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
        self._post("Scheduled sync didn't run", body, urgency="critical")

    # ------------------------------------------------------------------ #
    # Watching
    # ------------------------------------------------------------------ #
    def _watched_pairs(self):
        pairs = self.config.get_pairs(self.server)
        if self._only_pair:
            pairs = [p for p in pairs if p["name"] == self._only_pair]
        return pairs

    def _start_watching(self):
        observer = Observer()
        handlers = []
        for pair in self._watched_pairs():
            if os.path.isdir(pair["local"]):
                handler = _DebouncedHandler(self.on_files_changed,
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
    # What the loop asks of one server
    # ------------------------------------------------------------------ #
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

    def settings(self) -> dict:
        """This server's triggers, with any CLI overrides applied on top."""
        values = self.config.get_automation(self.server)
        values.update({k: v for k, v in self._overrides.items() if v is not None})
        return values

    def wants_automation(self, settings=None) -> bool:
        current = settings or self.settings()
        return bool(current["live_sync"] or current["scheduled_sync"]
                    or current["interval_minutes"] > 0)

    def has_pairs(self) -> bool:
        return bool(self._watched_pairs())

    def label(self) -> str:
        return self._server_label()

    def scheduled_due(self, settings) -> bool:
        """True when this server owes today's scheduled run and may retry now."""
        return (settings["scheduled_sync"]
                and self._scheduled_retry_due()
                and scheduled_run_is_due(settings["scheduled_time"],
                                         settings["last_scheduled_run"]))

    def run_scheduled(self, settings) -> None:
        """Attempt the day's run, and record it only if something got through.

        A run where every pair failed leaves the day due — deliberately, so it
        catches up — but backs off, because the loop ticks once a second and
        would otherwise hammer a NAS that is rejecting everything until
        midnight.
        """
        if self.sync_now(f"scheduled {settings['scheduled_time']}", scheduled=True):
            self._scheduled_retry_at = None
            self.config.record_scheduled_run(
                datetime.date.today().isoformat(), server=self.server)
        else:
            # Another sync being busy clears up by itself, usually within
            # minutes; a NAS rejecting everything does not, and gets the
            # longer pause.
            pause = (self.CATCH_UP_PAUSE * 12 if self.last_skip == "busy"
                     else self.SCHEDULED_RETRY_SECONDS)
            self._scheduled_retry_at = time.monotonic() + pause

    def refresh_notifier(self) -> None:
        self._notifier.enabled = self.config.get_notifications()

    def stop(self):
        # Whatever this burst moved is still worth saying; being stopped is not
        # a reason to swallow it.
        self.flush_notification()
        self._stop.set()

    def run(self, live_sync=None, scheduled_sync=None, scheduled_time=None,
            interval_minutes=None, only_pair=None, sync_at_start=None):
        """Run this one server until stopped. Arguments override its settings.

        The single-server entry point, which is what `nasync watch` wants: one
        NAS, in the foreground, with flags that beat the saved settings and
        change nothing on disk. `nasync auto` drives `AutomationRunner` across
        every automated server instead.
        """
        self._only_pair = only_pair
        self._overrides = {
            "live_sync": live_sync,
            "scheduled_sync": scheduled_sync,
            "scheduled_time": scheduled_time,
            "interval_minutes": interval_minutes,
        }
        return AutomationRunner([self], config=self.config,
                                reporter=self._report,
                                stop_event=self._stop).run(sync_at_start=sync_at_start)


class _WatchState:
    """What the loop has to remember about one server between ticks."""

    def __init__(self, automator):
        self.automator = automator
        self.observer = None
        self.handlers = []
        self.watching = False
        self.next_interval_tick = None


class AutomationRunner:
    """Drives one `Automator` per automated server from a single loop.

    One loop rather than a thread per server, because the expensive thing —
    the sync itself — is serialised anyway: they share a lock so that syncs
    never overlap, which is what this module promises. Watchers are cheap and
    do run concurrently, so a burst of edits on one NAS cannot defer another's
    schedule.

    Each server keeps its own catch-up date, so one being unreachable never
    marks another's day as done.
    """

    TICK = 1.0

    def __init__(self, automators, config=None, reporter=print, stop_event=None):
        self._states = [_WatchState(a) for a in automators]
        self.config = config or Config()
        self._report = reporter
        self._stop = stop_event or threading.Event()
        self._config_mtime = None

    def stop(self):
        self._stop.set()

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
        for state in self._states:
            state.automator.refresh_notifier()
        return True

    # ------------------------------------------------------------------ #
    # Watching
    # ------------------------------------------------------------------ #
    def _sync_watchers(self, state, wants_live: bool) -> None:
        """Start or stop one server's watchers to match its live-sync setting."""
        if wants_live and not state.watching:
            state.observer, state.handlers = state.automator._start_watching()
            state.watching = True
        elif not wants_live and state.watching:
            state.automator._stop_watching(state.observer, state.handlers)
            state.observer, state.handlers = None, []
            state.watching = False
            self._report(f"👁️  Live sync switched off for {state.automator.label()}")

    def _stop_all_watchers(self) -> None:
        for state in self._states:
            if state.watching or state.observer:
                state.automator._stop_watching(state.observer, state.handlers)
                state.observer, state.handlers, state.watching = None, [], False

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def run(self, sync_at_start=None):
        """Run until stopped.

        `sync_at_start` defaults to **only when live sync is on**, per server,
        which is a deliberate narrowing. This process is restarted far more
        often than it looks: `service.apply()` restarts the unit after any
        automation setting change, and the systemd unit restarts it on failure
        every 30 seconds. Syncing unconditionally on start therefore meant that
        changing the *scheduled time* — a setting whose entire purpose is to
        defer work — immediately performed a full sync, in mirror mode,
        deleting on the server. Nobody asked for that and nobody is watching
        when it happens.

        Live sync is the one trigger with a genuine gap to fill: watchdog only
        sees changes made while it is running, so edits made while the service
        was down would otherwise wait for a later edit. A schedule-only
        configuration has no such gap — it has a slot, and
        `scheduled_run_is_due` already catches up a missed one.

        Pass it explicitly to force either behaviour.
        """
        self._settings_changed()             # prime the mtime
        runnable = [s for s in self._states if self._runnable(s)]
        if not any(s.automator.wants_automation() for s in self._states):
            raise Exception(
                "Nothing to automate — live sync and scheduled sync are both "
                "off. Turn one on in the desktop app, or with "
                "'nasync live-sync on' / 'nasync scheduled-sync on'."
            )
        # A server with no folders is skipped rather than fatal: with several
        # servers, one half-configured NAS must not stop the others syncing.
        for state in self._states:
            if state.automator.wants_automation() and not state.automator.has_pairs():
                self._report(f"⚠️  Nothing to sync for "
                             f"'{state.automator.label()}' — no sync pairs.")
        if not runnable:
            raise Exception("No sync pairs configured. Run 'nasync setup' first.")

        try:
            for state in runnable:
                current = state.automator.settings()
                label = state.automator.label()
                self._sync_watchers(state, current["live_sync"])
                if current["scheduled_sync"]:
                    self._report(f"📅 {label}: scheduled sync every day at "
                                 f"{current['scheduled_time']}")
                if current["interval_minutes"] > 0:
                    self._report(f"⏱️  {label}: also syncing every "
                                 f"{current['interval_minutes']} min")
                    state.next_interval_tick = (time.monotonic()
                                                + current["interval_minutes"] * 60)

            self._report("Automation running — press Ctrl+C to stop.")

            for state in runnable:
                current = state.automator.settings()
                catch_up = (bool(current["live_sync"]) if sync_at_start is None
                            else bool(sync_at_start))
                if catch_up:
                    state.automator.sync_now("startup")

            while not self._stop.is_set():
                self._stop.wait(self.TICK)
                if self._stop.is_set():
                    break

                # Pick up switches flipped in the desktop app while we run.
                changed = self._settings_changed()

                # Re-evaluated every tick, over *every* server rather than the
                # set that was automated at startup: turning a trigger on for a
                # second NAS has to take effect in a running service, which is
                # what the docs promise and what a foreground `nasync auto`
                # would otherwise never notice.
                for state in self._states:
                    current = state.automator.settings()
                    live = self._runnable(state) and current["live_sync"]
                    if changed:
                        self._sync_watchers(state, live)
                        state.next_interval_tick = (
                            time.monotonic() + current["interval_minutes"] * 60
                            if current["interval_minutes"] > 0 else None)

                    if not self._runnable(state):
                        continue

                    if state.automator.scheduled_due(current):
                        state.automator.run_scheduled(current)
                        # We just wrote the run date; that's our own edit, not
                        # the user's, so don't spend the next tick reloading
                        # and resetting every interval timer because of it.
                        self._settings_changed()

                    # Changes a watcher saw but couldn't sync — the NAS was
                    # away, or another sync was running — are owed one now.
                    if live and state.automator.catch_up_due():
                        state.automator.catch_up()

                    if (state.next_interval_tick
                            and time.monotonic() >= state.next_interval_tick):
                        state.automator.sync_now("interval")
                        state.next_interval_tick = (
                            time.monotonic() + current["interval_minutes"] * 60)
        except KeyboardInterrupt:
            self._report("\n⏹️  Stopping automation…")
        finally:
            self._stop_all_watchers()

    @staticmethod
    def _runnable(state) -> bool:
        """A server with a trigger on and somewhere to sync from."""
        return (state.automator.wants_automation()
                and state.automator.has_pairs())


def run_all(reporter=print, config=None, notifier=None, sync_at_start=None):
    """Automate every server that has a trigger switched on.

    The entry point for `nasync auto`, and the reason the settings moved onto
    servers: before 0.18.0 this ran whichever server happened to be active, so
    a second NAS was never automated at all.
    """
    config = config or Config()
    lock = threading.Lock()
    # One per *configured* server, not per currently-automated one: an
    # automator holds nothing but state until a trigger turns on, and building
    # them all is what lets the loop notice a server being automated while it
    # runs. The runner refuses to start if none of them has a trigger.
    automators = [
        Automator(reporter=reporter, config=config, notifier=notifier,
                  server=server["name"], sync_lock=lock)
        for server in config.list_servers()
    ]
    if not automators:
        raise Exception(
            "No servers configured. Run 'nasync setup' first."
        )
    return AutomationRunner(automators, config=config,
                            reporter=reporter).run(sync_at_start=sync_at_start)
