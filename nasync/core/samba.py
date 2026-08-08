"""SMB transport built on `smbprotocol` — a pure-Python SMB 2/3 client.

This replaces the previous driver, which shelled out to the system `smbclient`
binary and scraped its terminal output with regexes. Talking SMB directly buys
us four things the subprocess approach structurally could not provide:

  * **Cancellation** — transfers check a `threading.Event` between chunks, so a
    running sync can actually be stopped.
  * **Per-byte progress** — a single large file reports smooth progress instead
    of jumping 0% → 100% when it finishes.
  * **Arbitrary filenames** — quotes and newlines were previously rejected
    because they could not be escaped through smbclient's scripting language.
  * **No external binary** — nothing to install, and it works inside a Flatpak
    sandbox, where `smbclient` is not present.

The wire protocol is unchanged: the server still negotiates SMB 2.0.2–3.1.1
exactly as before. Only *our* side of the conversation is different.

Change detection is by file size, matching the previous behaviour.
"""

import errno
import hashlib
import os
import socket
from stat import S_ISDIR

import smbclient
from smbprotocol.exceptions import (
    AccessDenied,
    BadNetworkName,
    IOTimeout,
    LogonFailure,
    ObjectNameNotFound,
    ObjectPathNotFound,
    SMBAuthenticationError,
    SMBException,
)

from nasync.core import srvsvc

#: The NT statuses worth translating, by value. `smbclient` reports these
#: through `SMBOSError` rather than as typed exceptions, so matching on the
#: class alone misses them — see `_friendly_error`.
_NTSTATUS_MESSAGES = {
    0xC000006D: "Authentication failed. Check username and password.",  # LOGON_FAILURE
    0xC0000022: "Access denied. Check share permissions for this user.",
    0xC00000CC: "Share not found. Check the share name.",              # BAD_NETWORK_NAME
    0xC0000034: "That path doesn't exist on the server.",              # OBJECT_NAME_NOT_FOUND
    0xC000009A: "The server is out of resources for new connections.",
    0xC0000203: "The server closed the session. Try connecting again.",  # USER_SESSION_DELETED
}

# Transfers move in chunks so progress stays smooth and cancellation stays
# responsive; 1 MiB is a good balance between syscall overhead and latency.
CHUNK_SIZE = 1024 * 1024

# Characters SMB itself refuses in a path component. This is a *protocol*
# constraint, not a client one — the server answers STATUS_OBJECT_NAME_INVALID
# regardless of which SMB implementation asks.
#
# ':' is the dangerous one and the reason we check ahead of time rather than
# letting the server reject things: Samba silently truncates "note:x.txt" to
# "note" and diverts the contents into an NTFS alternate data stream, so the
# file appears to sync while its data is quietly lost.
_ILLEGAL_CHARS = set('<>:"|?*') | {chr(c) for c in range(32)}


class SyncCancelled(Exception):
    """Raised when a transfer is aborted through its cancel event."""


class SAMBAClient:
    """SMB client for one server + share.

    Output is routed through an injectable ``reporter``. Progress is reported
    through two optional callbacks:

      ``progress(done, total, name, verb)``
          Per-*file* counter. Unchanged from the previous implementation, so
          existing front-ends keep working untouched.

      ``byte_progress(bytes_done, bytes_total, name, verb)``
          Per-*byte* counter within the current file. New, and optional —
          front-ends that don't pass it simply don't receive it.

    Passing a ``cancel_event`` (a ``threading.Event``) makes transfers abort at
    the next chunk boundary, raising :class:`SyncCancelled`.
    """

    def __init__(self, ip: str, username: str, password: str, share: str,
                 port: int = 445, reporter=None, progress=None,
                 byte_progress=None, cancel_event=None, encrypt: bool = False):
        self.ip = ip
        self.username = username
        self.password = password
        self.share = share
        self.port = int(port)
        #: Require SMB 3 encryption for this session. Off by default, and a
        #: hard requirement when on — see `connect`.
        self.encrypt = bool(encrypt)
        self._report = reporter or print
        self._progress = progress or (lambda done, total, name, verb: None)
        self._byte_progress = byte_progress or (
            lambda done, total, name, verb: None)
        self._cancel = cancel_event
        self._connected = False
        # smbclient's connection pool is keyed by (server, port), and every
        # path-based call defaults to 445 rather than inheriting the port used
        # at registration — so the port has to travel with each call.
        self._opts = {"port": self.port}
        if self.encrypt:
            # And so does encryption, for the same reason: a path call that
            # omits it can *create* the pooled session unencrypted, and every
            # later call reuses that session. Only ever True or absent —
            # `encrypt=False` actively disables encryption on a share that
            # requires it. See `connect` for why omitting this was a real leak.
            self._opts["encrypt"] = True

    # ------------------------------------------------------------------ #
    # Paths
    # ------------------------------------------------------------------ #
    @staticmethod
    def _remote(path: str) -> str:
        """Normalise a config server path to a backslash-separated relative path."""
        return path.strip("/").replace("/", "\\")

    def _unc(self, *parts: str) -> str:
        """Build a full UNC path: \\\\server\\share\\a\\b."""
        cleaned = [p.strip("\\") for p in parts if p and p.strip("\\")]
        return "\\".join([f"\\\\{self.ip}\\{self.share}"] + cleaned)

    @staticmethod
    def _storable(rel: str) -> bool:
        """True when every component of `rel` is a legal SMB filename.

        Checked before transferring rather than after, because some illegal
        characters fail loudly (STATUS_OBJECT_NAME_INVALID) while ':' fails
        *silently* by truncating the name and losing the file's contents.
        """
        return not any(ch in _ILLEGAL_CHARS for part in rel.split("/") for ch in part)

    # ------------------------------------------------------------------ #
    # Errors
    # ------------------------------------------------------------------ #
    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        """Translate a low-level SMB/socket error into an actionable message."""
        # Checked before the auth cases below: a refused *encrypted* session
        # otherwise reads as "SMB encryption is required but the connection
        # does not support it", which is accurate and tells a home user
        # nothing about which of the two ends to change.
        message = str(exc)
        if "encryption" in message.lower() and isinstance(exc, SMBException):
            if "guest" in message.lower():
                return ("Encryption can't be used with guest access. Set a real "
                        "username and password on the share, or turn encryption "
                        "off for this server.")
            return ("This server doesn't support encrypted connections. It needs "
                    "SMB 3 or newer — most NAS boxes made after about 2014 have "
                    "it, sometimes switched off by default. Turn encryption off "
                    "for this server to connect without it.")
        # `smbclient`'s os-layer wraps protocol failures in SMBOSError rather
        # than raising the typed exceptions below, so the status has to be read
        # off the error itself. Without this an authentication failure on the
        # IPC$ pipe surfaces as a raw "[NtStatus 0xc000006d]", which is exactly
        # the kind of message the friendly mapping exists to prevent.
        status = getattr(exc, "ntstatus", None)
        if status is not None:
            mapped = _NTSTATUS_MESSAGES.get(status)
            if mapped:
                return mapped

        if isinstance(exc, (LogonFailure, SMBAuthenticationError)):
            return "Authentication failed. Check username and password."
        if isinstance(exc, AccessDenied):
            return "Access denied. Check share permissions for this user."
        if isinstance(exc, BadNetworkName):
            return "Share not found. Check the share name."
        if isinstance(exc, IOTimeout):
            return "Connection timed out. Check the IP and network."
        if isinstance(exc, ConnectionRefusedError):
            return "Connection refused. Is SMB (port 445) open?"
        if isinstance(exc, socket.timeout):
            return "Connection timed out. Check the IP and network."
        if isinstance(exc, (socket.gaierror, OSError)) and not isinstance(exc, SMBException):
            return "Server unreachable. Check the IP and network."
        return str(exc)

    def _fail(self, exc: Exception) -> Exception:
        return Exception(self._friendly_error(exc))

    # ------------------------------------------------------------------ #
    # Cancellation
    # ------------------------------------------------------------------ #
    def _check_cancelled(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise SyncCancelled("Transfer cancelled.")

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    def connect(self):
        """Open a session and verify the share is actually reachable.

        With ``encrypt`` on this is a *requirement*, not a preference:
        `smbprotocol` refuses the session outright if the negotiated connection
        can't encrypt, rather than continuing in the clear. That is the whole
        point — a security setting that silently degrades to plaintext when the
        server is old is worse than no setting, because it reads as protection
        that isn't there.
        """
        try:
            smbclient.register_session(
                self.ip, username=self.username, password=self.password,
                port=self.port,
                # None, not False: `encrypt=False` would actively *disable*
                # encryption on a server whose share config demands it, which
                # would fail a connection that works today.
                encrypt=True if self.encrypt else None,
            )
            # register_session only proves the *server* answered; touching the
            # share root is what proves the share name is valid.
            smbclient.listdir(self._unc(), **self._opts)
        except Exception as e:
            raise self._fail(e)

        # Asking for encryption is not the same as getting it. `smbclient`
        # pools one session per host:port, and its reuse path tries to upgrade
        # an existing plaintext session by setting an attribute that
        # `smbprotocol.session.Session` does not have — so the upgrade is a
        # silent no-op and the transfer runs in the clear while every surface
        # reports "encrypted". Checking the live session is the only honest
        # way to know, and this class promises never to degrade quietly.
        if self.encrypt and self._negotiated_plaintext():
            raise Exception(
                "This connection isn't encrypted, although encryption is "
                "switched on for this server. Another part of NASync opened "
                "an unencrypted connection to it first; restart NASync and "
                "try again.")
        self._connected = True

    def _negotiated_plaintext(self) -> bool:
        """True only on *positive* evidence that this session is unencrypted.

        Fails open when the live session can't be inspected at all, which is
        not evidence of anything — refusing a working connection because the
        connection pool looked unfamiliar would be its own bug.
        """
        try:
            return not self.connection_info()["encrypted"]
        except Exception:
            return False

    def disconnect(self):
        """Close the shared connection to this server.

        **This is process-wide, not per-client.** `smbclient` pools one
        connection per ``host:port`` and `delete_session` closes it along with
        *every* session on it — so calling this while another client is mid
        transfer aborts that transfer with a bogus "server unreachable".

        Only a caller that owns the whole interaction should use it. Anything
        that runs alongside other work — the browser, the storage report, and
        anything else that only reads — wants :meth:`release`.
        """
        if not self._connected:
            return
        try:
            smbclient.delete_session(self.ip, port=self.port)
        except Exception:
            pass  # teardown is best-effort
        self._connected = False

    def release(self):
        """Finish with this client but leave the pooled connection alone.

        The connection stays open for whoever else is using it — which is the
        point of a pool, and makes the next listing faster besides. There is
        exactly one per server, so nothing accumulates.
        """
        self._connected = False

    def list_shares(self, include_all: bool = False) -> list:
        """Enumerate the server's shares, as names.

        Spoken to the server directly over SRVSVC — see :mod:`nasync.core.srvsvc`
        for why this isn't ``smbclient -L`` any more. In short: that binary is
        not in a Flatpak, and shipping Samba to read a list of names would be a
        strange thing to ask a reviewer to wave through.

        Disk shares only by default, and never the administrative ones (``C$``,
        ``ADMIN$``, ``IPC$``): they exist on every Windows machine and are never
        what somebody means to sync to. `include_all=True` returns the full
        picture for the server-info panel, which reports rather than chooses.
        """
        shares = self.share_details()
        if include_all:
            return [share["name"] for share in shares]
        return [share["name"] for share in shares
                if share["is_disk"] and not share["is_special"]]

    def share_details(self) -> list:
        """Every share the server admits to, with type and comment.

        Opened on ``IPC$`` rather than the configured share, because listing is
        a question about the *server* — and it has to work before a share has
        been chosen, which is exactly when the first-run flow asks it.
        """
        unc = f"\\\\{self.ip}\\IPC$\\srvsvc"
        try:
            # Unbuffered: this is a message channel, and a buffering wrapper
            # would hold a request back waiting for more to write.
            with smbclient.open_file(unc, mode="rb+", buffering=0,
                                     username=self.username,
                                     password=self.password,
                                     **self._opts) as pipe:
                return srvsvc.enumerate_shares(pipe)
        except srvsvc.SrvsvcError as e:
            raise Exception(
                f"{e} Enter the share name manually instead.")
        except Exception as e:
            raise self._fail(e)

    # ------------------------------------------------------------------ #
    # Remote indexing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_missing(exc: Exception) -> bool:
        """True when `exc` means "that path isn't there", not "the call failed".

        smbclient's os-layer reports a missing path as ``SMBOSError`` — an
        ``OSError`` carrying ENOENT — while the protocol layer underneath can
        surface the raw status types, so both shapes have to be recognised.
        """
        return (isinstance(exc, (ObjectNameNotFound, ObjectPathNotFound))
                or (isinstance(exc, OSError) and exc.errno == errno.ENOENT))

    def _symlink_target_size(self, path: str):
        """Size of a symlink's target, or ``None`` if it isn't an indexable file.

        A reparse point is the one entry a directory listing can't describe:
        its recorded size and attributes belong to the link, not the target.
        Symlinked *directories* are skipped rather than descended into —
        following them can loop forever on a link pointing at its own parent,
        which is why ``smbclient.walk()`` declined to follow them either.
        """
        try:
            st = smbclient.stat(path, **self._opts)
        except Exception:
            return None  # broken link, or it vanished mid-scan
        return None if S_ISDIR(st.st_mode) else st.st_size

    def list_directory(self, path: str = "") -> list:
        """List *one* directory on the share, for browsing rather than syncing.

        Returns ``[{"name", "path", "is_dir", "is_symlink", "size"}, …]`` with
        directories first and each group sorted case-insensitively — the order a
        file manager shows, because that is what this is for. ``path`` is the
        POSIX-style path relative to the share root that a sync pair stores, and
        it is what comes back in ``path`` so a caller can hand it straight to
        `Config.add_pair`.

        Distinct from :meth:`_remote_file_map` on purpose. That walks the whole
        tree to compare it; this reads one level and stops, because a browser
        that indexed the entire share before showing the first screen would be
        unusable on exactly the large shares the 0.8.0 work was about.

        Raises if the directory doesn't exist — unlike indexing, where a missing
        path is the ordinary first-sync case, asking to browse somewhere absent
        is a question that deserves an answer rather than an empty list.
        """
        entries = []
        base = self._remote(path)
        # What a child's stored path looks like: the POSIX form a sync pair
        # keeps, so `path` here and `path` in the result are the same shape.
        prefix = path.strip("/")
        try:
            with smbclient.scandir(self._unc(base), **self._opts) as listing:
                for entry in listing:
                    self._check_cancelled()
                    link = entry.is_symlink()
                    # follow_symlinks=False is free on an ordinary entry; on a
                    # reparse point it costs a round trip, so it is only paid
                    # for the entries that actually are links.
                    is_dir = entry.is_dir() if link \
                        else entry.is_dir(follow_symlinks=False)
                    entries.append({
                        "name": entry.name,
                        "path": f"{prefix}/{entry.name}" if prefix else entry.name,
                        "is_dir": is_dir,
                        "is_symlink": link,
                        # A listing describes the *link*, not its target, so a
                        # symlink's size would be a lie. None means "not known
                        # from here" and front-ends show it as such.
                        "size": None if (link or is_dir)
                        else entry.smb_info.end_of_file,
                    })
        except SyncCancelled:
            raise
        except Exception as e:
            if self._is_missing(e):
                raise Exception(
                    f"'{path or '/'}' doesn't exist on the server." if path
                    else f"Can't read the root of share '{self.share}'.")
            raise self._fail(e)

        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return entries

    def _remote_file_map(self, base: str, on_scan=None) -> dict:
        """Return ``{posix_relative_path: size}`` for every file beneath `base`.

        One listing round trip per *directory*, and no ``stat()`` at all in the
        common case. The previous implementation used ``smbclient.walk()`` and
        then ``stat()``-ed every file for its size — an extra round trip per
        *file*, paid on every sync including ones with nothing to do. On a
        20k-file share at 3 ms RTT that is a minute spent sitting at
        "comparing files… 0%", which reads as a hang rather than as work. The
        size is already in the directory listing (``end_of_file``), so it never
        needed asking for twice.

        An empty dict means the destination doesn't exist yet (first sync).

        ``on_scan(directories_done, files_seen)`` is called once per directory
        for callers that show this to a person. Sync doesn't pass it — it has
        its own per-pair progress — but a storage report walks the *whole*
        share, where several silent minutes are indistinguishable from a hang.
        """
        root = self._unc(base)
        files = {}
        scanned = 0
        pending = [(root, "")]           # (UNC path, path relative to root)
        while pending:
            dirpath, rel_dir = pending.pop()
            try:
                with smbclient.scandir(dirpath, **self._opts) as entries:
                    for entry in entries:
                        self._check_cancelled()
                        rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                        if entry.is_symlink():
                            size = self._symlink_target_size(entry.path)
                            if size is not None:
                                files[rel] = size
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append((entry.path, rel))
                        else:
                            files[rel] = entry.smb_info.end_of_file
            except SyncCancelled:
                raise
            except Exception as e:
                if not self._is_missing(e):
                    raise self._fail(e)
                # The destination itself not existing is the first-sync case
                # and reads as an empty remote. A *subdirectory* disappearing
                # mid-scan is a race, and skipping it is what walk() did.
                if not rel_dir:
                    return {}
            scanned += 1
            if on_scan is not None:
                on_scan(scanned, len(files))
        return files

    # ------------------------------------------------------------------ #
    # Server info
    # ------------------------------------------------------------------ #
    #: SMB dialects by their wire value, newest first in practice.
    DIALECT_NAMES = {
        0x0202: "SMB 2.0.2", 0x0210: "SMB 2.1",
        0x0300: "SMB 3.0", 0x0302: "SMB 3.0.2", 0x0311: "SMB 3.1.1",
    }

    def connection_info(self) -> dict:
        """What was actually negotiated with this server.

        Read off the live connection rather than from the settings, which is
        the entire point: the settings say what was *asked* for, and only the
        connection knows what the server agreed to. A server that quietly
        negotiated an older dialect than you expected is exactly the thing this
        panel exists to make visible.

        Requires `connect()` to have run.
        """
        import smbclient._pool as pool

        connection = pool._SMB_CONNECTIONS.get(f"{self.ip.lower()}:{self.port}")
        if connection is None:
            raise Exception("Not connected to the server.")

        sessions = list(connection.session_table.values())
        session = sessions[0] if sessions else None
        dialect = connection.dialect
        return {
            "dialect": dialect,
            "dialect_name": self.DIALECT_NAMES.get(dialect, f"0x{dialect:04x}"),
            # Whether *this session* is encrypted, not whether the server could
            # be asked to encrypt — the setting is elsewhere; this is the fact.
            "encrypted": bool(session.encrypt_data) if session else False,
            "signed": bool(session.signing_required) if session else False,
            "supports_encryption": bool(connection.supports_encryption),
            "requires_signing": bool(connection.require_signing),
            "max_read": connection.max_read_size,
            "max_write": connection.max_write_size,
        }

    def server_info(self) -> dict:
        """Everything the info panel shows, from one connection.

        Each part is optional and reported as missing rather than fatal: an old
        NAS may refuse the share list, a quota'd one may refuse volume figures,
        and neither is a reason to show nothing at all.
        """
        info = {"ip": self.ip, "port": self.port, "share": self.share,
                "username": self.username}
        info["connection"] = self.connection_info()

        try:
            info["volume"] = self.volume_info()
        except Exception as e:
            info["volume"] = None
            info["volume_error"] = str(e)

        try:
            info["shares"] = self.share_details()
        except Exception as e:
            info["shares"] = None
            info["shares_error"] = str(e)
        return info

    # ------------------------------------------------------------------ #
    # Storage insight
    # ------------------------------------------------------------------ #
    def volume_info(self) -> dict:
        """Total and free bytes of the volume the share sits on.

        ``free`` is what *this* user may still write, which is the number worth
        showing: on a NAS with per-user quotas it is smaller than the disk's
        own free space, and the disk's figure would promise room that a sync
        cannot actually use. ``disk_free`` carries the raw figure so a caller
        can point out the difference rather than quietly showing one of them.
        """
        try:
            volume = smbclient.stat_volume(self._unc(), **self._opts)
        except Exception as e:
            raise self._fail(e)
        return {
            "total": volume.total_size,
            "free": volume.caller_available_size,
            "disk_free": volume.actual_available_size,
            "used": volume.total_size - volume.actual_available_size,
        }

    def usage_tree(self, path: str = "", on_scan=None) -> dict:
        """What is using space under `path`, one level down.

        Returns ``{"path", "total", "files", "children"}`` where each child
        carries the **recursive** total beneath it, biggest first — which is
        the only ordering that answers the question people actually ask, "what
        do I delete to get some space back".

        Built on the same index the sync engine uses, deliberately. That walk
        already costs one round trip per directory and was tuned for exactly
        this shape of work in 0.8.0; a second traversal here would be a slower
        copy of it. The aggregation below is pure arithmetic on the result.
        """
        files = self._remote_file_map(self._remote(path), on_scan=on_scan)
        if not files:
            # An empty map is ambiguous — `_remote_file_map` returns one both
            # for a missing directory and for a genuinely empty one. Asking
            # costs a single round trip, and only in the empty case.
            self.list_directory(path)

        prefix = path.strip("/")
        totals, counts, is_dir = {}, {}, {}
        for rel, size in files.items():
            head, _, tail = rel.partition("/")
            totals[head] = totals.get(head, 0) + size
            counts[head] = counts.get(head, 0) + 1
            is_dir[head] = is_dir.get(head, False) or bool(tail)

        children = [{
            "name": name,
            "path": f"{prefix}/{name}" if prefix else name,
            "is_dir": is_dir[name],
            "total": totals[name],
            "files": counts[name],
        } for name in totals]
        children.sort(key=lambda c: (-c["total"], c["name"].lower()))

        return {
            "path": prefix,
            "total": sum(totals.values()),
            "files": sum(counts.values()),
            "children": children,
        }

    def find_duplicates(self, path: str = "", verify: bool = False,
                        min_size: int = 1, on_scan=None, on_hash=None) -> list:
        """Files that are probably the same, wasting the most space first.

        Two passes, because they cost wildly different amounts. **Grouping by
        size is free** — the index already carries every size, so a report of
        candidates costs nothing beyond the walk. **Confirming them is not**:
        there is no server-side hash in SMB, so `verify=True` reads every
        candidate byte over the network. On a share of large media files that
        is gigabytes of traffic, which is why it is opt-in and why the caller
        is given `bytes_to_read` to warn with beforehand.

        Each group is ``{"size", "paths", "wasted", "hash"}``; ``hash`` is None
        when unverified, and same-size files that turn out to differ are split
        into separate groups rather than reported together.
        """
        files = self._remote_file_map(self._remote(path), on_scan=on_scan)

        by_size = {}
        for rel, size in files.items():
            if size >= min_size:
                by_size.setdefault(size, []).append(rel)
        candidates = [(size, paths) for size, paths in by_size.items()
                      if len(paths) > 1]

        groups = []
        for size, paths in candidates:
            if not verify:
                groups.append({"size": size, "paths": sorted(paths),
                               "wasted": size * (len(paths) - 1), "hash": None})
                continue
            by_hash = {}
            for rel in sorted(paths):
                self._check_cancelled()
                if on_hash is not None:
                    on_hash(rel, size)
                digest = self._remote_digest(f"{path.strip('/')}/{rel}"
                                             if path.strip("/") else rel)
                by_hash.setdefault(digest, []).append(rel)
            # Same size but different contents is the ordinary case for things
            # like fixed-size headers, so only real matches survive.
            for digest, matched in by_hash.items():
                if len(matched) > 1:
                    groups.append({"size": size, "paths": matched,
                                   "wasted": size * (len(matched) - 1),
                                   "hash": digest})

        groups.sort(key=lambda g: (-g["wasted"], g["paths"][0].lower()))
        return groups

    def duplicate_scan_cost(self, groups: list) -> int:
        """Bytes `find_duplicates(verify=True)` would read for these groups."""
        return sum(group["size"] * len(group["paths"]) for group in groups)

    def _remote_digest(self, rel: str) -> str:
        """SHA-256 of one file on the share, streamed rather than loaded.

        Chunked because a duplicate hunt is aimed at *large* files by
        definition — reading a 4 GB video into memory to hash it would be the
        one operation guaranteed to run out of it.
        """
        digest = hashlib.sha256()
        try:
            with smbclient.open_file(self._unc(self._remote(rel)), mode="rb",
                                     **self._opts) as handle:
                while chunk := handle.read(CHUNK_SIZE):
                    self._check_cancelled()
                    digest.update(chunk)
        except SyncCancelled:
            raise
        except Exception as e:
            raise self._fail(e)
        return digest.hexdigest()

    def _ensure_remote_dir(self, path: str) -> None:
        try:
            smbclient.makedirs(path, exist_ok=True, **self._opts)
        except Exception as e:
            raise self._fail(e)

    @staticmethod
    def _leaf_dirs(dirs: set) -> list:
        """Drop every directory in `dirs` that is an ancestor of another.

        ``makedirs`` creates intermediate levels itself, so asking for
        "photos/2024" after "photos/2024/june" is a wasted round trip.
        """
        ancestors = set()
        for path in dirs:
            parts = path.split("/")
            ancestors.update("/".join(parts[:i]) for i in range(1, len(parts)))
        return sorted(dirs - ancestors)

    # ------------------------------------------------------------------ #
    # Local indexing
    # ------------------------------------------------------------------ #
    def _local_file_map(self, local_dir: str):
        """Index a local tree as ``({posix_rel: (abs_path, size)}, unsupported)``.

        ``os.scandir`` reports the entry type from the directory read itself,
        so an ordinary file costs a single ``stat`` for its size instead of the
        two that ``rglob("*")`` + ``Path.is_file()`` + ``Path.stat()`` cost —
        measured 5× faster over a 3,757-file tree. Comparing is the part of a
        no-op sync the user actually waits through, on both sides of the wire.

        Symlinked directories are not descended into, matching ``rglob``, which
        would otherwise be free to loop on a cycle.
        """
        files = {}
        unsupported = []
        pending = [(local_dir, "")]
        while pending:
            dirpath, rel_dir = pending.pop()
            self._check_cancelled()
            try:
                with os.scandir(dirpath) as scan:
                    entries = list(scan)
            except OSError:
                continue                 # unreadable or removed; skip the branch
            for entry in entries:
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append((entry.path, rel))
                        continue
                    if not entry.is_file():
                        continue         # sockets, fifos, broken or dir symlinks
                    if not self._storable(rel):
                        unsupported.append(rel)
                        continue
                    files[rel] = (entry.path, entry.stat().st_size)
                except OSError:
                    continue             # vanished between listing and stat
        return files, sorted(unsupported)

    # ------------------------------------------------------------------ #
    # Chunked transfer primitives
    # ------------------------------------------------------------------ #
    def _copy_stream(self, src, dst, size: int, name: str, verb: str) -> None:
        """Copy `src` → `dst` in chunks, reporting bytes and honouring cancel."""
        moved = 0
        while True:
            self._check_cancelled()
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            dst.write(chunk)
            moved += len(chunk)
            self._byte_progress(moved, size, name, verb)

    def _upload_file(self, local_path: str, remote_path: str, name: str) -> None:
        size = os.path.getsize(local_path)
        with open(local_path, "rb") as src:
            with smbclient.open_file(remote_path, mode="wb", **self._opts) as dst:
                self._copy_stream(src, dst, size, name, "Uploading")

    def _download_file(self, remote_path: str, local_path: str, name: str,
                       size: int) -> None:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with smbclient.open_file(remote_path, mode="rb", **self._opts) as src:
            with open(local_path, "wb") as dst:
                self._copy_stream(src, dst, size, name, "Downloading")

    # ------------------------------------------------------------------ #
    # Public sync entrypoint
    # ------------------------------------------------------------------ #
    def sync_directory(self, local_dir: str, remote_dir: str,
                       direction: str = "push", dry_run: bool = False,
                       mirror: bool = True) -> dict:
        if direction == "push":
            return self._push_directory(local_dir, remote_dir, dry_run, mirror)
        elif direction == "pull":
            return self._pull_directory(local_dir, remote_dir, dry_run)
        raise ValueError(f"Unknown direction: {direction}")

    # ------------------------------------------------------------------ #
    # Push  (local -> server)
    # ------------------------------------------------------------------ #
    def _push_directory(self, local_dir: str, remote_dir: str,
                        dry_run: bool = False, mirror: bool = True) -> dict:
        local_dir = os.path.abspath(os.path.expanduser(local_dir))
        if not os.path.isdir(local_dir):
            raise Exception(f"Local directory not found: {local_dir}")

        base = self._remote(remote_dir)

        # --- 1. index local files (posix rel path -> (abs, size)) ---
        local_map, unsupported = self._local_file_map(local_dir)

        # --- 2. index remote files, then diff ---
        remote_map = self._remote_file_map(base)
        to_upload = sorted(r for r in local_map
                           if r not in remote_map or remote_map[r] != local_map[r][1])
        unchanged = [r for r in local_map
                     if r in remote_map and remote_map[r] == local_map[r][1]]
        to_delete = sorted(r for r in remote_map if r not in local_map) if mirror else []

        if dry_run:
            self._report(f"  [dry-run] {len(to_upload)} to upload, "
                         f"{len(unchanged)} unchanged, {len(to_delete)} to delete")
            for r in to_upload:
                self._report(f"    ↑ {r}")
            for r in to_delete:
                self._report(f"    ✗ {r} (removed locally)")
            for r in unsupported:
                self._report(f"    ! skipped (name illegal on SMB): {r}")
            return {"transferred": len(to_upload), "skipped": len(unchanged),
                    "deleted": len(to_delete), "errors": 0,
                    "unsupported": len(unsupported)}

        if not to_upload and not to_delete:
            self._report(f"  Already up to date ({len(unchanged)} file(s)).")
            for r in unsupported:
                self._report(f"  ! skipped (name illegal on SMB): {r}")
            return {"transferred": 0, "skipped": len(unchanged),
                    "deleted": 0, "errors": 0,
                    "unsupported": len(unsupported)}

        # --- 3. create directories, then upload ---
        # One makedirs per *directory* rather than per file: a thousand photos
        # in one album used to cost a thousand identical mkdir round trips.
        self._ensure_remote_dir(self._unc(base))
        parents = {rel.rsplit("/", 1)[0] for rel in to_upload if "/" in rel}
        for parent in self._leaf_dirs(parents):
            self._ensure_remote_dir(self._unc(base, parent.replace("/", "\\")))

        total = len(to_upload)
        transferred = 0
        errors = []
        for rel in to_upload:
            self._check_cancelled()
            abs_local = local_map[rel][0]
            name = os.path.basename(rel)
            self._progress(transferred + 1, total, name, "Uploading")
            try:
                self._upload_file(abs_local, self._unc(base, rel.replace("/", "\\")), name)
                transferred += 1
            except SyncCancelled:
                raise
            except Exception as e:
                errors.append(f"{rel}: {self._friendly_error(e)}")

        # --- 4. mirror deletes ---
        deleted = 0
        for rel in to_delete:
            self._check_cancelled()
            try:
                smbclient.remove(self._unc(base, rel.replace("/", "\\")), **self._opts)
                deleted += 1
            except Exception as e:
                errors.append(f"{rel}: {self._friendly_error(e)}")

        self._report(f"  Uploaded {transferred} · unchanged {len(unchanged)} · "
                     f"deleted {deleted}")
        for rel in unsupported:
            self._report(f"  ! skipped (name illegal on SMB): {rel}")
        for message in errors:
            self._report(f"  ! {message}")
        return {"transferred": transferred, "skipped": len(unchanged),
                "deleted": deleted, "errors": len(errors),
                "unsupported": len(unsupported)}

    # ------------------------------------------------------------------ #
    # Pull  (server -> local)
    # ------------------------------------------------------------------ #
    def _pull_directory(self, local_dir: str, remote_dir: str,
                        dry_run: bool = False) -> dict:
        local_dir = os.path.abspath(os.path.expanduser(local_dir))
        base = self._remote(remote_dir)

        remote_map = self._remote_file_map(base)

        # Only fetch what differs in size, mirroring the push side's behaviour.
        to_download = []
        unchanged = []
        for rel, size in sorted(remote_map.items()):
            local_path = os.path.join(local_dir, *rel.split("/"))
            if os.path.isfile(local_path) and os.path.getsize(local_path) == size:
                unchanged.append(rel)
            else:
                to_download.append(rel)

        if dry_run:
            self._report(f"  [dry-run] would restore from \\{base} into {local_dir}")
            self._report(f"  [dry-run] {len(to_download)} to download, "
                         f"{len(unchanged)} unchanged")
            for rel in to_download:
                self._report(f"    ↓ {rel}")
            return {"transferred": 0, "skipped": len(unchanged),
                    "deleted": 0, "errors": 0}

        os.makedirs(local_dir, exist_ok=True)

        total = len(to_download)
        transferred = 0
        errors = []
        for rel in to_download:
            self._check_cancelled()
            name = os.path.basename(rel)
            local_path = os.path.join(local_dir, *rel.split("/"))
            self._progress(transferred + 1, total, name, "Downloading")
            try:
                self._download_file(self._unc(base, rel.replace("/", "\\")),
                                    local_path, name, remote_map[rel])
                transferred += 1
            except SyncCancelled:
                raise
            except Exception as e:
                errors.append(f"{rel}: {self._friendly_error(e)}")

        self._report(f"  Downloaded {transferred} file(s) · "
                     f"unchanged {len(unchanged)}")
        for message in errors:
            self._report(f"  ! {message}")
        return {"transferred": transferred, "skipped": len(unchanged),
                "deleted": 0, "errors": len(errors)}
