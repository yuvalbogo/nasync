"""What NASync last put on the server, per pair — the memory a comparison needs.

Comparing the two sides' sizes and times can't tell some things apart:

  * an older version of a file put back locally (same size, *older* time)
    from a file uploaded by an older NASync (same size, server time later);
  * a file someone edited **on the NAS** from one that simply hasn't changed;
  * a file someone else **added to the NAS** from one the user deleted
    locally — the second should go in a mirror, the first must never;
  * a renamed file from a deleted one plus a new one.

A record of what was uploaded — the local size and time it had, the server
size and time it got, and its SHA-256, taken while the bytes were being sent
anyway — answers all four. It is also what lets a restore give a file back
its permissions, which an SMB share has nowhere to keep.

One SQLite file in the config directory, so it travels with the settings and
is shared by the app, the CLI and the background service. Only one of them
syncs at a time (see :mod:`nasync.core.activity`), so there is one writer.

A pair's record is keyed by *where its files land* — server, share and
destination — not by its local folder: the record describes the server's
contents. Pointing a pair at a new destination starts a new record; the old
one is pruned. Until a pair has finished one complete sync with a record
(``initialised``), it is in **bootstrap**: everything on the server was put
there by an older version or by hand, and the rules fall back to the ones
that applied before the record existed.
"""

import hashlib
import os
import sqlite3
import time
from typing import NamedTuple, Optional

DB_NAME = "sync-state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pairs (
    key TEXT PRIMARY KEY,
    server TEXT NOT NULL,
    destination TEXT NOT NULL,
    initialised REAL
);
CREATE TABLE IF NOT EXISTS entries (
    key TEXT NOT NULL,
    rel TEXT NOT NULL,
    size INTEGER,
    mtime REAL,
    remote_size INTEGER,
    remote_mtime REAL,
    sha256 TEXT,
    mode INTEGER,
    kept INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, rel)
);
"""

#: Commit at least this often during a run, so a crash or cancel keeps what
#: was already recorded rather than forcing it all to be compared again.
COMMIT_EVERY = 2.0


class Entry(NamedTuple):
    """One server file, as NASync last knew it.

    ``size``/``mtime`` are the *local* file's at the moment it was read for
    upload (or found already in place). ``remote_size``/``remote_mtime`` are
    the server's afterwards; ``remote_mtime`` may be None until first seen.
    ``kept`` marks a file NASync must never delete: a NAS-side version set
    aside in a conflict, or a file someone else put there.
    """
    size: Optional[int]
    mtime: Optional[float]
    remote_size: Optional[int]
    remote_mtime: Optional[float]
    sha256: Optional[str] = None
    mode: Optional[int] = None
    kept: bool = False


def pair_key(server: str, share: str, destination: str) -> str:
    text = f"{server}\0{share}\0{destination}".casefold()
    return hashlib.sha256(text.encode()).hexdigest()[:24]


class SyncState:
    """One pair's record. Open, read with :meth:`load`, write, then close."""

    def __init__(self, config_dir, server: str, share: str, destination: str,
                 read_only: bool = False):
        self.key = pair_key(server, share, destination)
        self.server = server
        self.destination = destination
        self.read_only = read_only
        path = os.path.join(str(config_dir), DB_NAME)
        os.makedirs(str(config_dir), exist_ok=True)
        self._db = sqlite3.connect(path, timeout=30)
        self._db.executescript(_SCHEMA)
        self._last_commit = time.monotonic()

    # -- reading ---------------------------------------------------------- #
    @property
    def initialised(self) -> bool:
        row = self._db.execute("SELECT initialised FROM pairs WHERE key = ?",
                               (self.key,)).fetchone()
        return bool(row and row[0])

    def load(self) -> dict:
        rows = self._db.execute(
            "SELECT rel, size, mtime, remote_size, remote_mtime, sha256, mode, kept "
            "FROM entries WHERE key = ?", (self.key,))
        return {rel: Entry(size, mtime, rsize, rmtime, sha, mode, bool(kept))
                for rel, size, mtime, rsize, rmtime, sha, mode, kept in rows}

    # -- writing ---------------------------------------------------------- #
    def put(self, rel: str, entry: Entry) -> None:
        if self.read_only:
            return
        self._db.execute(
            "INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.key, rel, entry.size, entry.mtime, entry.remote_size,
             entry.remote_mtime, entry.sha256, entry.mode, int(entry.kept)))
        self._maybe_commit()

    def forget(self, rel: str) -> None:
        if self.read_only:
            return
        self._db.execute("DELETE FROM entries WHERE key = ? AND rel = ?",
                         (self.key, rel))
        self._maybe_commit()

    def move(self, old: str, new: str) -> None:
        if self.read_only:
            return
        self._db.execute("DELETE FROM entries WHERE key = ? AND rel = ?",
                         (self.key, new))
        self._db.execute("UPDATE entries SET rel = ? WHERE key = ? AND rel = ?",
                         (new, self.key, old))
        self._maybe_commit()

    def mark_initialised(self) -> None:
        """The pair has finished a complete sync with a record: from now on,
        a server file without one was not put there by NASync."""
        if self.read_only:
            return
        self._db.execute(
            "INSERT INTO pairs (key, server, destination, initialised) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "initialised = excluded.initialised",
            (self.key, self.server, self.destination, time.time()))
        self._db.commit()

    def register(self) -> None:
        """Note this pair's destination, so pruning knows it is current."""
        if self.read_only:
            return
        self._db.execute(
            "INSERT OR IGNORE INTO pairs (key, server, destination, initialised) "
            "VALUES (?, ?, ?, NULL)", (self.key, self.server, self.destination))
        self._db.commit()

    def _maybe_commit(self) -> None:
        if time.monotonic() - self._last_commit >= COMMIT_EVERY:
            self._db.commit()
            self._last_commit = time.monotonic()

    def close(self) -> None:
        try:
            if not self.read_only:
                self._db.commit()
        finally:
            self._db.close()


def prune(config_dir, server: str, current_keys: set) -> None:
    """Drop records for destinations no pair of `server` points at any more.

    A stale record is worse than none: pointed back at an old destination
    later, a pair would trust a description of files that may have changed
    since — so it is removed, and that destination bootstraps afresh.
    """
    path = os.path.join(str(config_dir), DB_NAME)
    if not os.path.exists(path):
        return
    db = sqlite3.connect(path, timeout=30)
    try:
        db.executescript(_SCHEMA)
        stale = [key for (key,) in db.execute(
            "SELECT key FROM pairs WHERE server = ?", (server,))
            if key not in current_keys]
        for key in stale:
            db.execute("DELETE FROM entries WHERE key = ?", (key,))
            db.execute("DELETE FROM pairs WHERE key = ?", (key,))
        db.commit()
    finally:
        db.close()
