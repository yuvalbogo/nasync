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

Change detection is by size **and** modification time. Size alone — which is
what this did until 0.20.0 — silently missed every edit that kept a file the
same length: a corrected typo, a document saved twice, a database page
rewritten in place. Uploads carry the local modification time onto the server,
so the two clocks compare exactly from then on.
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

#: How far apart two modification times may be and still count as the same.
#: Some NAS volumes (FAT, exFAT) store times in 2-second steps, so an exact
#: comparison would re-upload a file whose time merely got rounded.
MTIME_SLACK = 2.0

#: A mirror run that would delete more than this share of a pair's files on
#: the server is held back until a person says yes…
MASS_DELETE_FRACTION = 0.5
#: …provided it is at least this many files. Clearing out a folder of three is
#: ordinary; clearing out half of twenty thousand is almost always a mistake —
#: a folder that moved, a disk that didn't mount, a pair pointed at the wrong
#: place. That is how 57,000 files disappeared from a NAS in one evening.
MASS_DELETE_MIN = 50

#: How many trailing path components rename detection will compare before
#: giving up on telling two same-named files apart.
_MOVE_MATCH_DEPTH = 4


class SyncCancelled(Exception):
    """Raised when a transfer is aborted through its cancel event."""


class MirrorRefused(Exception):
    """A mirror run held back before changing anything, because of what it
    would delete.

    ``confirmable`` says whether a person may wave it through. A large delete
    can be intended (a folder really was cleaned out); deleting *everything*
    because the local folder is empty never is, so that one is not offered.
    """

    def __init__(self, message: str, deletes: int, total: int,
                 confirmable: bool):
        super().__init__(message)
        self.deletes = deletes
        self.total = total
        self.confirmable = confirmable


def _epoch(stamp):
    """An SMB timestamp (a UTC datetime) as POSIX seconds, or None."""
    try:
        return stamp.timestamp()
    except (AttributeError, OverflowError, ValueError, OSError):
        return None


def _newer(candidate, reference) -> bool:
    """True when `candidate` is later than `reference` beyond the slack.

    Unknown on either side is "not newer": with no time to compare, size is
    the only evidence, which is exactly what the comparison used to be.
    """
    return (candidate is not None and reference is not None
            and candidate > reference + MTIME_SLACK)


def _parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _tail(rel: str, depth: int) -> str:
    """The last `depth` components of a relative path, case-folded."""
    return "/".join(rel.split("/")[-depth:]).casefold()


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

    def _remote_file_map(self, base: str, on_scan=None, dirs: set = None,
                         mtimes: dict = None) -> dict:
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

        Pass a set as ``dirs`` to also collect every directory beneath `base`,
        relative to it. Mirror mode needs them: deleting only files leaves the
        folders that held them standing empty, so the server ends up matching
        the local tree in content but not in shape.

        Pass a dict as ``mtimes`` to also collect each file's modification
        time (POSIX seconds). Like the size, it arrives in the listing itself,
        so asking for it costs nothing extra.

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
                            if dirs is not None:
                                dirs.add(rel)
                            pending.append((entry.path, rel))
                        else:
                            info = entry.smb_info
                            files[rel] = info.end_of_file
                            if mtimes is not None:
                                mtimes[rel] = _epoch(
                                    getattr(info, "last_write_time", None))
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
    def _stale_dirs(remote_dirs: set, local_map: dict) -> list:
        """Server directories with no counterpart locally, deepest first.

        Deepest first so a nested tree empties from the inside out; each removal
        is an ``rmdir``, which refuses a directory that still has anything in
        it. That refusal is the safety property — a folder holding something
        unaccounted for is left alone rather than forced.

        Compared case-insensitively, like the server compares them: a folder
        renamed from "Photos" to "photos" locally is the same folder on an SMB
        share, and is not stale just because the server kept the old casing.
        """
        local_dirs = set()
        for rel in local_map:
            parts = rel.split("/")[:-1]
            for depth in range(len(parts)):
                local_dirs.add("/".join(parts[:depth + 1]).casefold())
        return sorted((d for d in remote_dirs if d.casefold() not in local_dirs),
                      key=lambda d: d.count("/"), reverse=True)

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
        """Index a local tree as ``({posix_rel: (abs, size, mtime)}, unsupported)``.

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
                    info = entry.stat()
                    files[rel] = (entry.path, info.st_size, info.st_mtime)
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

    def _upload_file(self, local_path: str, remote_path: str, name: str,
                     mtime: float = None) -> None:
        size = os.path.getsize(local_path)
        with open(local_path, "rb") as src:
            with smbclient.open_file(remote_path, mode="wb", **self._opts) as dst:
                self._copy_stream(src, dst, size, name, "Uploading")
        # After closing, not before: the server stamps the write time when the
        # handle closes, which would overwrite anything set while it was open.
        if mtime is not None:
            self._stamp(remote_path, mtime)

    def _rename_case(self, old: str, new: str) -> None:
        """Rename a server file to a name differing only in letter case.

        Samba refuses that as a single rename (ACCESS_DENIED: it sees the
        target as already existing — it *is* the file), so on refusal it goes
        through a temporary name. If the second step fails the first is undone,
        so the file is never left under the temporary name.
        """
        try:
            smbclient.rename(old, new, **self._opts)
            return
        except Exception:
            pass
        temp = f"{old}.nasync-case-{os.getpid()}"
        smbclient.rename(old, temp, **self._opts)
        try:
            smbclient.rename(temp, new, **self._opts)
        except Exception:
            smbclient.rename(temp, old, **self._opts)
            raise

    def _stamp(self, remote_path: str, mtime: float) -> bool:
        """Give a server file the local file's modification time.

        This is what makes the next comparison exact. Failure is tolerated: a
        server that refuses keeps its own write time, which is *later* than
        the local one, so the file still reads as unchanged rather than being
        uploaded again on every run.
        """
        try:
            smbclient.utime(remote_path, times=(mtime, mtime), **self._opts)
            return True
        except SyncCancelled:
            raise
        except Exception:
            return False

    def _download_file(self, remote_path: str, local_path: str, name: str,
                       size: int, mtime: float = None) -> None:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with smbclient.open_file(remote_path, mode="rb", **self._opts) as src:
            with open(local_path, "wb") as dst:
                self._copy_stream(src, dst, size, name, "Downloading")
        if mtime is not None:
            try:
                os.utime(local_path, (mtime, mtime))
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # Public sync entrypoint
    # ------------------------------------------------------------------ #
    def sync_directory(self, local_dir: str, remote_dir: str,
                       direction: str = "push", dry_run: bool = False,
                       mirror: bool = True,
                       allow_mass_delete: bool = False) -> dict:
        if direction == "push":
            return self._push_directory(local_dir, remote_dir, dry_run, mirror,
                                        allow_mass_delete)
        elif direction == "pull":
            return self._pull_directory(local_dir, remote_dir, dry_run)
        raise ValueError(f"Unknown direction: {direction}")

    # ------------------------------------------------------------------ #
    # Push  (local -> server)
    # ------------------------------------------------------------------ #
    @staticmethod
    def plan_push(local_map: dict, remote_map: dict, remote_mtimes: dict,
                  remote_dirs: set, mirror: bool) -> dict:
        """Decide what a push does, without doing any of it.

        Separate from the transfer so the decisions — which are where data is
        won or lost — can be tested as plain data. Returns::

            upload    [(local_rel, remote_rel)]  new or changed files
            unchanged [local_rel]
            delete    [remote_rel]               mirror only
            renames   [(old_remote, new_rel)]    same file, new letter case
            moves     [(old_remote, new_rel)]    same file, new place
            stamps    [(remote_rel, mtime)]      unchanged, but carrying the
                                                 time of an older upload
            stale     [remote_dir]               mirror only, deepest first

        **A file is changed** when its size differs, or when the local copy is
        newer than the server's. Not "different": an older local time with the
        same size is a file uploaded before 0.20.0, when uploads didn't carry
        the time across and the server simply recorded when it arrived.

        **Letter case is not identity.** An SMB share almost always matches
        names case-insensitively, so renaming ``img.jpg`` to ``IMG.jpg`` used
        to upload "IMG.jpg" — which the server wrote *into* img.jpg — and then
        delete "img.jpg", which was the same file. The backup lost it until
        the next run. Such a pair is now one file: renamed where only the file
        name changed, and left at its old casing where a folder's did.

        **A moved file is moved, not copied again.** A renamed folder used to
        cost a full upload plus a full delete. Where a new file and a
        to-be-deleted one agree on name, size and modification time, the
        server is asked to rename. The time has to match within the slack,
        which only holds for files uploaded since 0.20.0 (or stamped since);
        anything less certain is uploaded as before, because a wrong match
        would be recorded as unchanged and never corrected.
        """
        def differs(rel, remote_rel):
            _, size, mtime = local_map[rel]
            return (remote_map[remote_rel] != size
                    or _newer(mtime, remote_mtimes.get(remote_rel)))

        by_fold = {}
        for remote_rel in remote_map:
            by_fold.setdefault(remote_rel.casefold(), []).append(remote_rel)

        upload, unchanged, renames, stamps = [], [], [], []
        claimed = set()      # server files spoken for by a local one
        matched = set()      # local files that have a server counterpart
        for rel in sorted(local_map):
            remote_rel = rel if rel in remote_map else None
            if remote_rel is None:
                candidates = [r for r in by_fold.get(rel.casefold(), ())
                              if r not in local_map and r not in claimed]
                if len(candidates) == 1:
                    remote_rel = candidates[0]
            if remote_rel is None:
                upload.append((rel, rel))
                continue
            claimed.add(remote_rel)
            matched.add(rel)
            target = remote_rel
            if remote_rel != rel and _parent(remote_rel) == _parent(rel):
                # Only the file name's case changed: rename it on the server,
                # then treat it like any other file at its new name.
                renames.append((remote_rel, rel))
                target = rel
            if differs(rel, remote_rel):
                upload.append((rel, target))
                continue
            unchanged.append(rel)
            remote_time = remote_mtimes.get(remote_rel)
            local_time = local_map[rel][2]
            if remote_time is not None and abs(remote_time - local_time) > MTIME_SLACK:
                stamps.append((target, local_time))

        delete = sorted(r for r in remote_map if r not in claimed) if mirror else []

        moves = []
        if mirror and delete:
            # Zero-byte files are not worth matching: re-creating one costs
            # less than the risk of pairing up two unrelated empty files.
            fresh = [rel for rel, _ in upload
                     if rel not in matched and local_map[rel][1] > 0]
            gone = list(delete)
            for depth in range(1, _MOVE_MATCH_DEPTH + 1):
                if not fresh or not gone:
                    break
                new_keys, old_keys = {}, {}
                for rel in fresh:
                    new_keys.setdefault((_tail(rel, depth), local_map[rel][1]),
                                        []).append(rel)
                for old in gone:
                    old_keys.setdefault((_tail(old, depth), remote_map[old]),
                                        []).append(old)
                for key, news in new_keys.items():
                    olds = old_keys.get(key, [])
                    if len(news) != 1 or len(olds) != 1:
                        continue                 # ambiguous at this depth
                    new, old = news[0], olds[0]
                    remote_time = remote_mtimes.get(old)
                    if remote_time is None or \
                            abs(remote_time - local_map[new][2]) > MTIME_SLACK:
                        continue
                    moves.append((old, new))
                    fresh.remove(new)
                    gone.remove(old)
            moved_new = {new for _, new in moves}
            moved_old = {old for old, _ in moves}
            upload = [u for u in upload if u[0] not in moved_new]
            delete = [d for d in delete if d not in moved_old]

        stale = []
        if mirror:
            # The local tree's shape, with moved files at their new places.
            stale = SAMBAClient._stale_dirs(remote_dirs or set(), local_map)

        return {"upload": upload, "unchanged": unchanged, "delete": delete,
                "renames": renames, "moves": sorted(moves), "stamps": stamps,
                "stale": stale}

    @staticmethod
    def check_mirror(plan: dict, local_count: int, remote_count: int,
                     allow_mass_delete: bool = False) -> None:
        """Refuse a mirror run whose deletions look like a mistake.

        Raises :class:`MirrorRefused` before anything has been touched.

        * **An empty local folder is never mirrored** onto a server folder that
          has files. Every time this has happened it meant the folder moved, a
          disk didn't mount, or a pair pointed at the wrong place — and the
          price of being wrong is the whole backup.
        * **Deleting more than half** of a pair's server files (and at least
          `MASS_DELETE_MIN` of them) needs a yes from a person. The background
          service never has one, so it holds the run back and says so.
        """
        deletes = len(plan["delete"])
        if not remote_count or not deletes:
            return
        if local_count == 0:
            raise MirrorRefused(
                f"the local folder is empty, so mirroring it would delete all "
                f"{remote_count:,} file(s) in its server folder. Nothing was "
                f"changed. If the folder moved, point the pair at its new "
                f"place; if you emptied it on purpose, delete the server copy "
                f"yourself.",
                deletes, remote_count, confirmable=False)
        if allow_mass_delete:
            return
        if deletes >= MASS_DELETE_MIN and deletes > MASS_DELETE_FRACTION * remote_count:
            raise MirrorRefused(
                f"this would delete {deletes:,} of the {remote_count:,} file(s) "
                f"in its server folder ({deletes * 100 // remote_count}%). "
                f"Nothing was changed. Check the folder is the one you meant, "
                f"then confirm the run to let it delete.",
                deletes, remote_count, confirmable=True)

    def _push_directory(self, local_dir: str, remote_dir: str,
                        dry_run: bool = False, mirror: bool = True,
                        allow_mass_delete: bool = False) -> dict:
        local_dir = os.path.abspath(os.path.expanduser(local_dir))
        if not os.path.isdir(local_dir):
            raise Exception(f"Local directory not found: {local_dir}")

        base = self._remote(remote_dir)

        # --- 1. index both sides ---
        local_map, unsupported = self._local_file_map(local_dir)
        # Directories too when mirroring: files alone leave empty folders
        # behind, so the server matches the local tree in content but not shape.
        remote_dirs = set() if mirror else None
        remote_mtimes = {}
        remote_map = self._remote_file_map(base, dirs=remote_dirs,
                                           mtimes=remote_mtimes)

        # --- 2. decide, and refuse before touching anything ---
        plan = self.plan_push(local_map, remote_map, remote_mtimes,
                              remote_dirs, mirror)
        refusal = None
        if mirror:
            try:
                self.check_mirror(plan, len(local_map), len(remote_map),
                                  allow_mass_delete)
            except MirrorRefused as e:
                if not dry_run:
                    raise
                refusal = e

        upload, delete = plan["upload"], plan["delete"]
        moves, renames = plan["moves"], plan["renames"]
        stamps, stale = plan["stamps"], plan["stale"]

        if dry_run:
            extras = []
            if moves:
                extras.append(f"{len(moves)} to move")
            if stale:
                extras.append(f"{len(stale)} empty folder(s) to remove")
            self._report(f"  [dry-run] {len(upload)} to upload, "
                         f"{len(plan['unchanged'])} unchanged, "
                         f"{len(delete)} to delete"
                         + "".join(f", {e}" for e in extras))
            for rel, _ in upload:
                self._report(f"    ↑ {rel}")
            for old, new in moves:
                self._report(f"    → {old}  ⇒  {new}")
            for rel in delete:
                self._report(f"    ✗ {rel} (removed locally)")
            for rel in unsupported:
                self._report(f"    ! skipped (name illegal on SMB): {rel}")
            if refusal is not None:
                self._report(f"  🛑 A real run would stop here: {refusal}")
            return {"transferred": len(upload), "skipped": len(plan["unchanged"]),
                    "deleted": len(delete), "moved": len(moves), "errors": 0,
                    "unsupported": len(unsupported),
                    "refused": str(refusal) if refusal else None}

        if not (upload or delete or moves or renames or stamps or stale):
            self._report(f"  Already up to date ({len(plan['unchanged'])} file(s)).")
            for r in unsupported:
                self._report(f"  ! skipped (name illegal on SMB): {r}")
            return {"transferred": 0, "skipped": len(plan["unchanged"]),
                    "deleted": 0, "errors": 0,
                    "unsupported": len(unsupported)}

        errors = []

        def unc(rel):
            return self._unc(base, rel.replace("/", "\\"))

        # --- 3. letter-case renames, before anything is written at the name ---
        renamed = 0
        for old, new in renames:
            self._check_cancelled()
            try:
                self._rename_case(unc(old), unc(new))
                renamed += 1
            except SyncCancelled:
                raise
            except Exception:
                # Harmless: the server keeps the old casing, and the file is
                # still the same file — nothing is deleted on its account.
                upload[:] = [(rel, old if rel == new else target)
                             for rel, target in upload]
                stamps[:] = [(old if rel == new else rel, t) for rel, t in stamps]

        # --- 4. directories, then moves and uploads ---
        # One makedirs per *directory* rather than per file: a thousand photos
        # in one album used to cost a thousand identical mkdir round trips.
        if upload or moves:
            self._ensure_remote_dir(self._unc(base))
        parents = {target.rsplit("/", 1)[0] for _, target in upload if "/" in target}
        parents |= {new.rsplit("/", 1)[0] for _, new in moves if "/" in new}
        for parent in self._leaf_dirs(parents):
            self._ensure_remote_dir(self._unc(base, parent.replace("/", "\\")))

        moved = 0
        for old, new in moves:
            self._check_cancelled()
            try:
                smbclient.rename(unc(old), unc(new), **self._opts)
                moved += 1
            except SyncCancelled:
                raise
            except Exception:
                # Fall back to what would have happened without the match.
                upload.append((new, new))
                delete.append(old)

        total = len(upload)
        transferred = 0
        for rel, target in upload:
            self._check_cancelled()
            abs_local, _, mtime = local_map[rel]
            name = os.path.basename(rel)
            self._progress(transferred + 1, total, name, "Uploading")
            try:
                self._upload_file(abs_local, unc(target), name, mtime)
                transferred += 1
            except SyncCancelled:
                raise
            except Exception as e:
                errors.append(f"{rel}: {self._friendly_error(e)}")

        # --- 5. one-time: give older uploads their real modification time ---
        stamped = 0
        for rel, mtime in stamps:
            self._check_cancelled()
            stamped += self._stamp(unc(rel), mtime)

        # --- 6. mirror deletes ---
        deleted = 0
        for rel in delete:
            self._check_cancelled()
            try:
                smbclient.remove(unc(rel), **self._opts)
                deleted += 1
            except SyncCancelled:
                raise
            except Exception as e:
                errors.append(f"{rel}: {self._friendly_error(e)}")

        # --- 7. and the directories left empty — including ones an earlier
        # run (or an older version) left behind, which is why this no longer
        # waits for a file to change first ---
        removed_dirs = 0
        for rel in stale:
            self._check_cancelled()
            try:
                smbclient.rmdir(unc(rel), **self._opts)
                removed_dirs += 1
            except SyncCancelled:
                raise
            except Exception:
                # Not empty, or already gone. Both are fine: rmdir refusing a
                # directory with something still in it is the guard, not a
                # failure worth reporting.
                pass

        self._report(f"  Uploaded {transferred} · unchanged {len(plan['unchanged'])} · "
                     f"deleted {deleted}"
                     + (f" · moved {moved}" if moved else "")
                     + (f" · renamed {renamed}" if renamed else "")
                     + (f" · removed {removed_dirs} empty folder(s)"
                        if removed_dirs else ""))
        if stamped:
            self._report(f"  Recorded the modification time of {stamped} file(s) "
                         f"uploaded by an older version (once only).")
        for rel in unsupported:
            self._report(f"  ! skipped (name illegal on SMB): {rel}")
        for message in errors:
            self._report(f"  ! {message}")
        return {"transferred": transferred, "skipped": len(plan["unchanged"]),
                "deleted": deleted, "deleted_dirs": removed_dirs,
                "moved": moved, "errors": len(errors),
                "unsupported": len(unsupported)}

    # ------------------------------------------------------------------ #
    # Pull  (server -> local)
    # ------------------------------------------------------------------ #
    def _pull_directory(self, local_dir: str, remote_dir: str,
                        dry_run: bool = False) -> dict:
        local_dir = os.path.abspath(os.path.expanduser(local_dir))
        base = self._remote(remote_dir)

        remote_mtimes = {}
        remote_map = self._remote_file_map(base, mtimes=remote_mtimes)

        # Fetch what is missing, a different size, or newer on the server —
        # the push side's rule, facing the other way. A local copy newer than
        # the server's is left alone: it is work the backup hasn't seen yet.
        to_download = []
        unchanged = []
        for rel, size in sorted(remote_map.items()):
            local_path = os.path.join(local_dir, *rel.split("/"))
            try:
                info = os.stat(local_path)
                present = os.path.isfile(local_path)
            except OSError:
                present = False
            if (present and info.st_size == size
                    and not _newer(remote_mtimes.get(rel), info.st_mtime)):
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
                                    local_path, name, remote_map[rel],
                                    remote_mtimes.get(rel))
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
