"""Automation layer: keep folders synced automatically.

Two triggers, usable together:
  * on-change — watch the local folders and sync shortly after edits settle
  * interval  — also sync every N minutes

A single lock guarantees syncs never overlap.
"""

import os
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from nasync.config import Config
from nasync.sync import Syncer


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


class Automator:
    def __init__(self, reporter=print):
        self.config = Config()
        self._report = reporter
        self._sync_lock = threading.Lock()

    def sync_now(self):
        """Sync every pair to the active server; never overlaps a running sync."""
        if not self._sync_lock.acquire(blocking=False):
            return  # a sync is already in progress; skip this trigger
        try:
            self._report(f"\n🔄 Auto-sync at {time.strftime('%H:%M:%S')}")
            Syncer(reporter=self._report).sync_pair()
        except Exception as e:
            self._report(f"❌ {e}")
        finally:
            self._sync_lock.release()

    def run(self, on_change: bool = True, interval_minutes: int = 0):
        pairs = self.config.get_pairs()
        if not pairs:
            raise Exception("No sync pairs configured. Run 'nasync setup' first.")

        observer = None
        handlers = []
        if on_change:
            observer = Observer()
            for pair in pairs:
                if os.path.isdir(pair["local"]):
                    handler = _DebouncedHandler(self.sync_now)
                    handlers.append(handler)
                    observer.schedule(handler, pair["local"], recursive=True)
            observer.start()
            self._report(f"👁️  Watching {len(handlers)} folder(s) for changes")
        if interval_minutes > 0:
            self._report(f"⏱️  Also syncing every {interval_minutes} min")
        self._report("Automation running — press Ctrl+C to stop.")

        self.sync_now()  # sync once at startup

        next_tick = (time.monotonic() + interval_minutes * 60) if interval_minutes > 0 else None
        try:
            while True:
                time.sleep(1)
                if next_tick and time.monotonic() >= next_tick:
                    self.sync_now()
                    next_tick = time.monotonic() + interval_minutes * 60
        except KeyboardInterrupt:
            self._report("\n⏹️  Stopping automation…")
        finally:
            for h in handlers:
                h.cancel()
            if observer:
                observer.stop()
                observer.join()
