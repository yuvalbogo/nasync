"""Which sync is running right now — across processes, not just threads.

The background service (`nasync auto`), the desktop app and the CLI are
separate processes. Until 0.20.0 nothing connected them: the service could be
twenty minutes into a scheduled mirror run while the app, opened to check on
it, said "Idle" — and pressing Sync there started a second run over the same
folders, the two colliding on files each other had open.

Three small files in the config directory fix that, and they are the whole
protocol:

  * ``sync.lock`` — held with ``flock`` for the length of a run. The kernel
    drops it when the holder exits, however it exits, so a crash can never
    leave a stale lock behind.
  * ``sync-status.json`` — what the holder is doing: who started it, which
    pair, how far through. Readers trust it only while the lock is held.
  * ``last-sync.json`` — what the most recent run did, so a run that finished
    while nobody was looking can still be reported.

And ``sync-cancel``, created by anyone who wants the running sync stopped.

The config directory rather than ``$XDG_RUNTIME_DIR`` because it is the one
place every process that shares a config is guaranteed to share — including
the app and the service inside one Flatpak.
"""

import json
import os
import threading
import time

try:
    import fcntl
except ImportError:          # not POSIX: run without cross-process exclusion
    fcntl = None

LOCK_NAME = "sync.lock"
STATUS_NAME = "sync-status.json"
LAST_NAME = "last-sync.json"
CANCEL_NAME = "sync-cancel"

#: How often progress is written to disk. The app polls once a second; writing
#: more often than it reads would be disk traffic nobody sees.
WRITE_INTERVAL = 0.5

#: Taking the lock is retried briefly before giving up, because a *reader*
#: checking whether a sync is running takes it for a moment too.
ACQUIRE_ATTEMPTS = 10
ACQUIRE_PAUSE = 0.05

#: Who can be holding the lock, as people would say it.
SOURCES = {"background": "the background service",
           "app": "the NASync app",
           "cli": "the nasync command"}


class SyncBusy(Exception):
    """Another sync holds the lock. Carries its status, when it has one."""

    def __init__(self, status: dict = None):
        self.status = status or {}
        super().__init__("Another sync is already running — "
                         + (describe(self.status) if self.status
                            else "it is just starting.")
                         + " Wait for it to finish, or cancel it.")


def _path(config_dir, name: str) -> str:
    return os.path.join(str(config_dir), name)


def _write_json(path: str, data: dict) -> None:
    """Replace a file atomically, so a reader never sees half of it."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _read_json(path: str):
    try:
        with open(path) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _held(config_dir) -> bool:
    """Is anyone holding the sync lock right now?"""
    if fcntl is None:
        return os.path.exists(_path(config_dir, STATUS_NAME))
    try:
        fd = os.open(_path(config_dir, LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def read_status(config_dir):
    """The running sync's status, or None when nothing is running.

    A status file without a held lock is a leftover from a process that died
    mid-run, and is ignored rather than reported as a sync that never ends.
    """
    status = _read_json(_path(config_dir, STATUS_NAME))
    if status is None or not _held(config_dir):
        return None
    return status


def read_last(config_dir):
    """What the most recent finished run did, or None."""
    return _read_json(_path(config_dir, LAST_NAME))


def request_cancel(config_dir) -> bool:
    """Ask whichever process is syncing to stop. False if nothing is."""
    if read_status(config_dir) is None:
        return False
    try:
        with open(_path(config_dir, CANCEL_NAME), "w") as handle:
            handle.write(str(time.time()))
        return True
    except OSError:
        return False


def describe(status: dict) -> str:
    """One line about a running sync, for a person."""
    who = SOURCES.get(status.get("source"), "another process")
    parts = [f"started by {who}"]
    if status.get("trigger"):
        parts.append(f"({status['trigger']})")
    started = status.get("started")
    if started:
        parts.append(f"at {time.strftime('%H:%M', time.localtime(started))}")
    if status.get("pair_total"):
        parts.append(f"— folder {status.get('pair_index', 0)} of "
                     f"{status['pair_total']}, '{status.get('pair', '')}'")
    return " ".join(parts)


def describe_last(last: dict) -> str:
    """One line about the most recent finished run, for a person."""
    when = time.strftime("%a %d %b %H:%M", time.localtime(last.get("finished", 0)))
    who = SOURCES.get(last.get("source"), "another process")
    outcome = last.get("result", {})
    parts = [f"{outcome.get('transferred', 0)} transferred",
             f"{outcome.get('deleted', 0)} removed"]
    if outcome.get("moved"):
        parts.append(f"{outcome['moved']} moved")
    if outcome.get("cancelled"):
        parts.append("cancelled")
    problems = outcome.get("failed", 0) + outcome.get("errors", 0)
    if problems:
        parts.append(f"{problems} problem(s)")
    if outcome.get("refused"):
        parts.append("held back: " + ", ".join(outcome["refused"]))
    trigger = f" ({last['trigger']})" if last.get("trigger") else ""
    return f"Last sync {when}, by {who}{trigger} — " + " · ".join(parts)


def progress_fraction(status: dict) -> float:
    """How far through the whole run, 0–1, from pairs and files within one."""
    total = status.get("pair_total") or 0
    if not total:
        return 0.0
    within = 0.0
    if status.get("files_total"):
        within = min(1.0, status.get("files_done", 0) / status["files_total"])
    done = max(0, (status.get("pair_index") or 1) - 1) + within
    return max(0.0, min(1.0, done / total))


class Activity:
    """One run's hold on the lock, and the status it publishes.

    Use as a context manager. ``SyncBusy`` is raised from ``__enter__`` when
    another process is already syncing.
    """

    def __init__(self, config_dir, source: str = "app", trigger: str = "",
                 server: str = None, cancel_event: threading.Event = None):
        self.config_dir = str(config_dir)
        self.cancel_event = cancel_event
        self._fd = None
        self._last_write = 0.0
        self._status = {"pid": os.getpid(), "source": source,
                        "trigger": trigger or "", "server": server or "",
                        "started": time.time()}
        self._watcher = None
        self._done = threading.Event()

    # -- lock ----------------------------------------------------------- #
    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()
        return False

    def acquire(self) -> None:
        os.makedirs(self.config_dir, exist_ok=True)
        if fcntl is not None:
            fd = os.open(_path(self.config_dir, LOCK_NAME),
                         os.O_RDWR | os.O_CREAT, 0o600)
            for attempt in range(ACQUIRE_ATTEMPTS):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if attempt == ACQUIRE_ATTEMPTS - 1:
                        os.close(fd)
                        raise SyncBusy(_read_json(
                            _path(self.config_dir, STATUS_NAME)))
                    time.sleep(ACQUIRE_PAUSE)
            self._fd = fd
        # A cancel request left from an earlier run is not about this one.
        self._clear_cancel()
        self._write(force=True)
        if self.cancel_event is not None:
            self._watcher = threading.Thread(target=self._watch_cancel,
                                             daemon=True)
            self._watcher.start()

    def release(self, result: dict = None) -> None:
        self._done.set()
        if result is not None:
            self.finish(result)
        try:
            os.unlink(_path(self.config_dir, STATUS_NAME))
        except OSError:
            pass
        self._clear_cancel()
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def finish(self, result: dict) -> None:
        """Record what the run did, for whoever looks after it is over."""
        summary = {key: result.get(key, 0) for key in
                   ("ok", "failed", "transferred", "skipped", "deleted",
                    "moved", "errors")}
        summary["cancelled"] = bool(result.get("cancelled"))
        summary["refused"] = [r.get("pair") for r in result.get("refused", [])]
        _write_json(_path(self.config_dir, LAST_NAME),
                    {**{k: self._status[k] for k in
                        ("source", "trigger", "server", "started")},
                     "finished": time.time(), "result": summary})

    # -- status ----------------------------------------------------------- #
    def update(self, force: bool = False, **fields) -> None:
        self._status.update(fields)
        self._write(force)

    def _write(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < WRITE_INTERVAL:
            return
        self._last_write = now
        self._status["updated"] = time.time()
        _write_json(_path(self.config_dir, STATUS_NAME), self._status)

    # -- cancel ----------------------------------------------------------- #
    def _clear_cancel(self) -> None:
        try:
            os.unlink(_path(self.config_dir, CANCEL_NAME))
        except OSError:
            pass

    def _watch_cancel(self) -> None:
        """Turn a cancel request from another process into our cancel event.

        Polled from a thread rather than checked in the progress callbacks,
        because the slowest phase of a run — indexing the server — reports no
        progress at all, and a cancel should not wait for it to finish.
        """
        path = _path(self.config_dir, CANCEL_NAME)
        while not self._done.wait(WRITE_INTERVAL):
            if os.path.exists(path):
                self.cancel_event.set()
                return
