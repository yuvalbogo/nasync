import threading
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from nasync.sync import Syncer


class SyncEventHandler(FileSystemEventHandler):
    """Trailing-edge debounce: coalesce a burst of file events and sync once,
    `debounce` seconds after the *last* change. This guarantees the final
    on-disk state is always synced (editors often write a file several times
    in quick succession)."""

    def __init__(self, sync_func, pair_name: str, debounce: float = 2.0):
        self.sync_func = sync_func
        self.pair_name = pair_name
        self.debounce = debounce
        self._timer = None
        self._lock = threading.Lock()

    def on_any_event(self, event):
        if event.is_directory:
            return
        # Ignore watchdog's "opened"/"closed" noise where present.
        if getattr(event, "event_type", None) in ("opened", "closed"):
            return
        self._schedule()

    def _schedule(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        print(f"\n🔄 Changes detected — syncing '{self.pair_name}'...")
        try:
            result = self.sync_func(self.pair_name)
            if result and result.get("failed"):
                print(f"⚠️  {result['failed']} error(s) syncing '{self.pair_name}'")
        except Exception as e:
            print(f"❌ Sync failed: {e}")

    def cancel(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()


class Watcher:
    def __init__(self):
        self.syncer = Syncer()
        self.observer = Observer()
        self._handlers = []

    def watch_pairs(self, pair_name: str = None):
        pairs = self.syncer.config.get_pairs()
        if not pairs:
            raise Exception("No sync pairs configured. Run 'nasync add' first.")

        if pair_name:
            pairs = [p for p in pairs if p["name"] == pair_name]
            if not pairs:
                raise Exception(f"Pair '{pair_name}' not found.")

        import os
        for pair in pairs:
            if not os.path.isdir(pair["local"]):
                print(f"⚠️  Skipping '{pair['name']}': local path not found ({pair['local']})")
                continue
            handler = SyncEventHandler(self.syncer.sync_pair, pair["name"])
            self._handlers.append(handler)
            self.observer.schedule(handler, path=pair["local"], recursive=True)
            print(f"👁️  Watching: {pair['local']} → {pair['server']}")

        if not self._handlers:
            raise Exception("No watchable directories found.")

        print("Press Ctrl+C to stop.\n")
        self.observer.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n⏹️  Stopping watcher...")
            for handler in self._handlers:
                handler.cancel()
            self.observer.stop()
            self.observer.join()
