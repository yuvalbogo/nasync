import time
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from samba_sync.sync import Syncer


class SyncEventHandler(FileSystemEventHandler):
    def __init__(self, sync_func, pair_name: str):
        self.sync_func = sync_func
        self.pair_name = pair_name
        self.last_sync = 0
        self.sync_delay = 2

    def on_modified(self, event):
        self._handle_event(event, "modified")

    def on_created(self, event):
        self._handle_event(event, "created")

    def on_deleted(self, event):
        self._handle_event(event, "deleted")

    def _handle_event(self, event, event_type):
        if event.is_directory:
            return

        current_time = time.time()
        if current_time - self.last_sync >= self.sync_delay:
            print(f"📝 File {event_type}: {event.src_path}")
            try:
                self.sync_func(self.pair_name)
                self.last_sync = current_time
            except Exception as e:
                print(f"❌ Sync failed: {e}")


class Watcher:
    def __init__(self):
        self.syncer = Syncer()
        self.observer = Observer()

    def watch_pairs(self, pair_name: str = None):
        pairs = self.syncer.config.get_pairs()
        if not pairs:
            raise Exception("No sync pairs configured. Run 'samba-sync add' first.")

        if pair_name:
            pairs = [p for p in pairs if p["name"] == pair_name]
            if not pairs:
                raise Exception(f"Pair '{pair_name}' not found.")

        for pair in pairs:
            handler = SyncEventHandler(self.syncer.sync_pair, pair["name"])
            self.observer.schedule(handler, path=pair["local"], recursive=True)
            print(f"👁️  Watching: {pair['local']} → {pair['server']}")

        self.observer.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n⏹️  Stopping watcher...")
            self.observer.stop()
            self.observer.join()
