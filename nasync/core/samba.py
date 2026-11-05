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
import re
import socket
import time
from typing import NamedTuple
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
_ILLEGAL_CHARS = set('<>:"|?*\\') | {chr(c) for c in range(32)}

# Names Samba accepts but stores under an 8.3 alias ("AHY9U3~9.C" for "aux.c")
# with its default `mangled names` setting: Windows' reserved device names,
# with any extension, and names ending in a dot or a space. Uploaded, they
# came back under the alias — re-sent on every run, restored under the wrong
# name, and deleted as "someone else's" by a mirror run.
_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} \
    | {f"lpt{i}" for i in range(1, 10)}
# What such an alias looks like in a listing: never deleted, never adopted.
_MANGLED_RE = re.compile(r"^[A-Z0-9_$]{1,6}~[A-Z0-9]{1,2}(\.[A-Z0-9_]{1,3})?$")

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
#: The background service holds back at a lower share than a person is asked
#: at: nobody is watching it, and a source that shrinks by 48% in one unattended
#: run (a mount point whose disk was unplugged) is not something to wave through.
UNATTENDED_FRACTION = 0.2

#: How many trailing path components rename detection will compare before
#: giving up on telling two same-named files apart.
_MOVE_MATCH_DEPTH = 4


class SyncCancelled(Exception):
    """Raised when a transfer is aborted through its cancel event."""


class MirrorRefused(Exception):
    """Part of a run held back, because of what it would destroy.

    Raised by :meth:`SAMBAClient.check_mirror`; a push catches it, still does
    everything that destroys nothing (new files, moves), and reports what it
    held back. ``confirmable`` says whether a person may wave it through: a
    large delete can be intended, deleting *everything* because the local
    folder is empty never is. ``fingerprint`` names exactly the deletions and
    overwrites that were shown, so a yes covers those and nothing found later.
    """

    def __init__(self, message: str, deletes: int, total: int,
                 confirmable: bool, fingerprint: str = "", overwrites: int = 0,
                 hold_deletes: bool = True, hold_overwrites: bool = False,
                 sample: list = ()):
        super().__init__(message)
        #: A few of the files held, so whoever is asked sees names, not a count.
        self.sample = list(sample)
        self.deletes = deletes
        self.total = total
        self.confirmable = confirmable
        self.fingerprint = fingerprint
        self.overwrites = overwrites
        self.hold_deletes = hold_deletes
        self.hold_overwrites = hold_overwrites


class LocalFileError(Exception):
    """A file on *this* machine couldn't be read or written.

    Kept apart from network errors: an unreadable local file used to be
    reported as "Server unreachable. Check the IP and network."
    """


class ServerIdentityChanged(Exception):
    """The machine answering at the server's address isn't the one set up.

    Raised before any password is sent. On a strange network that happens to
    use the same addresses as home, the service would otherwise offer the NAS
    password to whatever answered there.
    """


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
                 byte_progress=None, cancel_event=None, encrypt: bool = False,
                 identity: str = None, stage=None):
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
        #: ``stage(text)``: what a run is doing between files, for a person —
        #: reading the server can take minutes and moves no file counter.
        self._stage = stage or (lambda text: None)
        self._cancel = cancel_event
        self._connected = False
        #: The server's identity (its SMB server GUID) as first seen, or None
        #: to learn it. See `connect`; after it, the identity actually seen.
        self.identity = identity
        #: Whether the server accepts a rename onto an existing name; learned
        #: on the first upload that needs it. See `_swap_into_place`.
        self._replace_works = None
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
        return SAMBAClient._unstorable(rel) is None

    @staticmethod
    def _unstorable(rel: str):
        """Why `rel` can't be stored on the NAS under its own name, or None.

        A backslash is a path separator on SMB: "a\\b.txt" landed on "a/b.txt"
        and replaced that file's backup. A name that isn't UTF-8 (from an old
        archive) fails on every run. Neither is an error to retry.
        """
        for part in rel.split("/"):
            if any(ch in _ILLEGAL_CHARS for ch in part):
                return "name not allowed on SMB"
            if any("\udc80" <= ch <= "\udcff" for ch in part):
                return "name isn't valid UTF-8, so the NAS can't store it"
            if part.endswith((".", " ")):
                return "name ends in a dot or a space, which the NAS would change"
            if part.split(".", 1)[0].casefold() in _RESERVED_NAMES:
                return "a reserved Windows name, which the NAS would change"
        return None

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
        seen = self.server_identity()
        if self.identity and seen and seen != self.identity:
            raise ServerIdentityChanged(
                f"The device answering at {self.ip} isn't the NAS this server "
                f"was set up with (its identity changed), so NASync didn't "
                f"send it your password. If you're on another network that "
                f"uses the same addresses, nothing is wrong — it will sync at "
                f"home. If you replaced or reinstalled the NAS, trust the new "
                f"one with 'nasync trust' or in the app.")
        if seen:
            self.identity = seen
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

    def server_identity(self):
        """The server's SMB GUID, read from the unauthenticated handshake.

        Costs one extra connection, before any credentials are sent — which is
        the point. None when the server can't be asked (an older one, or one
        not answering), in which case nothing is compared.
        """
        import uuid
        from smbprotocol.connection import Connection
        connection = Connection(uuid.uuid4(), self.ip, self.port)
        try:
            connection.connect(timeout=10)
            guid = getattr(connection, "server_guid", None)
            return str(guid) if guid else None
        except Exception:
            return None
        finally:
            try:
                connection.disconnect(True)
            except Exception:
                pass

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
                         mtimes: dict = None, must_exist: bool = False) -> dict:
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
                # and reads as an empty remote — but not for a restore, where
                # "nothing there" used to be reported as a completed restore of
                # nothing. A *subdirectory* disappearing mid-scan is a race,
                # and skipping it is what walk() did.
                if not rel_dir:
                    if must_exist:
                        raise Exception(
                            f"'{base.replace(chr(92), '/') or '/'}' doesn't exist "
                            f"on the server, so there is nothing to restore. "
                            f"Check the pair's destination and the server's "
                            f"sync root.")
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
    def _stale_dirs(remote_dirs: set, local_map: dict, local_dirs=(),
                    protected=()) -> list:
        """Server directories with no counterpart locally, deepest first.

        Deepest first so a nested tree empties from the inside out; each removal
        is an ``rmdir``, which refuses a directory that still has anything in
        it. That refusal is the safety property — a folder holding something
        unaccounted for is left alone rather than forced.

        Compared case-insensitively, like the server compares them: a folder
        renamed from "Photos" to "photos" locally is the same folder on an SMB
        share, and is not stale just because the server kept the old casing.
        A local folder that exists but is *empty* still counts, and so does
        anything under a local folder that couldn't be read (`protected`).
        """
        present = {d.casefold() for d in local_dirs}
        for rel in local_map:
            parts = rel.split("/")[:-1]
            for depth in range(len(parts)):
                present.add("/".join(parts[:depth + 1]).casefold())
        return sorted((d for d in remote_dirs
                       if d.casefold() not in present and not _under(d, protected)),
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
    def _scan_local(self, local_dir: str) -> "LocalScan":
        """Index a local tree: files, folders, and everything that can't go.

        ``os.scandir`` reports the entry type from the directory read itself,
        so an ordinary file costs a single ``stat`` for its size instead of the
        two that ``rglob("*")`` + ``Path.is_file()`` + ``Path.stat()`` cost —
        measured 5× faster over a 3,757-file tree.

        Nothing is dropped silently any more. Each of these used to vanish
        from the backup without a word, and is now listed with its reason:

          * a folder that **couldn't be read** — and it is also *protected*:
            until 0.21.0 its files looked deleted, so mirror deleted their
            backup when a folder's permissions changed;
          * a **symlinked folder** (not followed: it could loop, or lead out of
            the folder entirely) and a **broken symlink**;
          * names SMB refuses, and two names that differ **only in letter
            case** — Linux keeps README and readme apart, an SMB share cannot,
            and uploading both meant one silently replaced the other.
        """
        files = {}
        unsupported = []
        dirs = set()
        unreadable = []
        temps = []
        pending = [(local_dir, "")]
        while pending:
            dirpath, rel_dir = pending.pop()
            self._check_cancelled()
            try:
                with os.scandir(dirpath) as scan:
                    entries = list(scan)
            except OSError:
                unreadable.append(rel_dir)
                continue
            for entry in entries:
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        why = self._unstorable(rel)
                        if why:
                            unsupported.append((rel, why))
                            continue
                        dirs.add(rel)
                        pending.append((entry.path, rel))
                        continue
                    if entry.is_symlink():
                        if entry.is_dir():
                            unsupported.append((rel, "symlinked folder, not followed"))
                            continue
                        if not entry.is_file():
                            unsupported.append((rel, "broken symlink"))
                            continue
                    elif not entry.is_file():
                        continue         # sockets, fifos, devices: not data
                    why = self._unstorable(rel)
                    if why:
                        unsupported.append((rel, why))
                        continue
                    if _is_own_temp(rel):
                        # A download a killed restore left half-written: never
                        # a backup of anything (it used to be uploaded, and
                        # re-uploaded on every run).
                        temps.append((rel, entry.path))
                        continue
                    info = entry.stat()
                    files[rel] = (entry.path, info.st_size, info.st_mtime)
                except OSError as e:
                    # Gone between listing and stat is a race and harmless.
                    # Anything else — a folder that can be listed but not
                    # entered, a FUSE mount returning EIO — is not: the file
                    # used to look deleted, and mirror deleted its backup.
                    # Decided by the error, not by asking again: asking again
                    # fails for the same reason and also looks like "gone".
                    if e.errno != errno.ENOENT:
                        unreadable.append(rel)
                    continue
        by_fold = {}
        for rel in sorted(files):
            by_fold.setdefault(rel.casefold(), []).append(rel)
        twins = [group for group in by_fold.values() if len(group) > 1]
        return LocalScan(files, sorted(unsupported), dirs, sorted(unreadable),
                         twins, temps)

    def _local_file_map(self, local_dir: str):
        """``({posix_rel: (abs, size, mtime)}, [unsupported rel])`` — see `_scan_local`."""
        scan = self._scan_local(local_dir)
        dropped, _ = _settle_case_twins(scan.files, scan.twins)
        return scan.files, [rel for rel, _ in scan.unsupported + dropped]

    # ------------------------------------------------------------------ #
    # Chunked transfer primitives
    # ------------------------------------------------------------------ #
    def _copy_stream(self, src, dst, size: int, name: str, verb: str,
                     digest=None) -> int:
        """Copy `src` → `dst` in chunks, reporting bytes and honouring cancel."""
        moved = 0
        while True:
            self._check_cancelled()
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            dst.write(chunk)
            if digest is not None:
                digest.update(chunk)
            moved += len(chunk)
            self._byte_progress(moved, size, name, verb)
        return moved

    def _upload_file(self, local_path: str, remote_path: str, name: str,
                     mtime: float = None) -> tuple:
        """Upload, then return ``(bytes_sent, sha256, stamped)``.

        Written under a temporary name and renamed over the real one only once
        complete. Until 0.21.0 the real file was opened for writing directly,
        so a cancel, a dropped connection or a full disk left a truncated file
        exactly where the good copy had been — and a restore before the next
        sync would have handed back half a file.
        """
        try:
            src_handle = open(local_path, "rb")
            size = os.fstat(src_handle.fileno()).st_size
        except OSError as e:
            raise LocalFileError(f"can't read the local file "
                                 f"({e.strerror or e})") from e
        folder, _, base = remote_path.rpartition("\\")
        temp = f"{folder}\\{_temp_name(base, _TEMP_MARK)}"
        digest = hashlib.sha256()
        try:
            with src_handle as src:
                with smbclient.open_file(temp, mode="wb", **self._opts) as dst:
                    sent = self._copy_stream(src, dst, size, name, "Uploading",
                                             digest)
            self._swap_into_place(temp, remote_path)
        except BaseException:
            try:
                smbclient.remove(temp, **self._opts)
            except Exception:
                pass
            raise
        # After the rename, not before: the server stamps the write time when
        # the handle closes, which would overwrite anything set while it was open.
        stamped = mtime is not None and self._stamp(remote_path, mtime)
        return sent, digest.hexdigest(), stamped

    def _swap_into_place(self, temp: str, final: str) -> None:
        """Rename a finished upload over the file it replaces.

        One atomic replace where the server allows it. Samba refuses that
        (ACCESS_DENIED for any rename onto an existing name), so the fallback
        moves the old copy aside first, then the new one in, then deletes the
        old. At every instant a complete copy exists under some name; if the
        middle step fails *or is interrupted* — Ctrl+C, a cancel, a killed
        process — the old copy is put back. Until 0.21.1 only an error did
        that: an interrupt left the file under its hidden aside name, which a
        restore skipped and the next sync deleted as a leftover. Learned once
        per session, so a server that refuses costs one wasted call, not one
        per file.
        """
        try:
            smbclient.rename(temp, final, **self._opts)      # nothing there yet
            return
        except Exception:
            pass
        # Something is there. Learned only from a replace onto an existing
        # file: one onto a free name succeeds anywhere, and learning from that
        # made every later edit in the run fail with "Access denied" on Samba
        # (third audit, #3). A refused replace always falls back.
        if self._replace_works is not False:
            try:
                smbclient.replace(temp, final, **self._opts)
                self._replace_works = True
                return
            except Exception:
                self._replace_works = False
        if S_ISDIR(smbclient.stat(final, **self._opts).st_mode):
            # Moving it aside would hide a whole folder under a temp name.
            raise OSError("a folder of this name is on the NAS")
        folder, _, base = final.rpartition("\\")
        aside = f"{folder}\\{_temp_name(base, _OLD_MARK)}"
        smbclient.rename(final, aside, **self._opts)
        try:
            smbclient.rename(temp, final, **self._opts)
        except BaseException:
            try:
                smbclient.rename(aside, final, **self._opts)
            except Exception:
                pass                      # recovered by the next run (see push)
            raise
        try:
            smbclient.remove(aside, **self._opts)
        except Exception:
            pass                          # a leftover the next run removes

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
        folder, _, base = old.rpartition("\\")
        temp = f"{folder}\\{_temp_name(base, _CASE_TEMP_MARK)}"
        smbclient.rename(old, temp, **self._opts)
        try:
            smbclient.rename(temp, new, **self._opts)
        except BaseException:
            try:
                smbclient.rename(temp, old, **self._opts)
            except Exception:
                pass                      # recovered by the next run (see push)
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

    def _digest_unc(self, unc_path: str) -> str:
        digest = hashlib.sha256()
        with smbclient.open_file(unc_path, mode="rb", **self._opts) as handle:
            while chunk := handle.read(CHUNK_SIZE):
                self._check_cancelled()
                digest.update(chunk)
        return digest.hexdigest()

    def _download_file(self, remote_path: str, local_path: str, name: str,
                       size: int, mtime: float = None, mode: int = None) -> str:
        """Download atomically, like `_upload_file`; returns the SHA-256.

        A cancelled restore used to leave a truncated *local* file in place of
        whatever was there — the one copy that wasn't on the server.
        """
        folder = os.path.dirname(local_path)
        os.makedirs(folder, exist_ok=True)
        temp = os.path.join(folder, _temp_name(os.path.basename(local_path),
                                               _TEMP_MARK))
        digest = hashlib.sha256()
        try:
            with smbclient.open_file(remote_path, mode="rb", **self._opts) as src:
                try:
                    dst_handle = open(temp, "wb")
                except OSError as e:
                    raise LocalFileError(f"can't write the local file "
                                         f"({e.strerror or e})") from e
                with dst_handle as dst:
                    self._copy_stream(src, dst, size, name, "Downloading", digest)
            if mode is not None:
                os.chmod(temp, mode & 0o7777)
            if mtime is not None:
                os.utime(temp, (mtime, mtime))
            os.replace(temp, local_path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        return digest.hexdigest()

    def _scan_stage(self, _directories: int, files: int) -> None:
        self._stage(f"Reading the server's copy… {files:,} file(s) so far")

    def _recover_set_aside(self, base: str, remote_map: dict, remote_mtimes: dict,
                           record: dict) -> None:
        """Put back files an interrupted swap or case rename left aside.

        Both steps move a complete file to a hidden name for a moment. If the
        process dies right then, the file exists only under that name — which
        used to look like a leftover, skipped by restore and deleted by the
        next sync. Renamed back here when its own name is free; deleted (as a
        leftover) by the plan only when the real file is in place.
        """
        present = {rel.casefold() for rel in remote_map}
        for rel in sorted(remote_map):
            folder, _, name = rel.rpartition("/")
            mark = _OLD_MARK if _OLD_MARK in name else (
                _CASE_TEMP_MARK if _CASE_TEMP_MARK in name else None)
            if mark is None:
                continue
            missing = [r.rpartition("/")[2] for r in record
                       if r.rpartition("/")[0] == folder and r.casefold() not in present]
            original = _temp_original(name, mark, missing)
            if not original:
                continue
            target = f"{folder}/{original}" if folder else original
            if target.casefold() in present:
                continue
            try:
                smbclient.rename(self._unc(base, rel.replace("/", "\\")),
                                 self._unc(base, target.replace("/", "\\")),
                                 **self._opts)
            except SyncCancelled:
                raise
            except Exception:
                continue
            remote_map[target] = remote_map.pop(rel)
            if rel in remote_mtimes:
                remote_mtimes[target] = remote_mtimes.pop(rel)
            present.add(target.casefold())
            self._report(f"  Recovered '{target}', left aside by an interrupted sync")

    def _clear_local_temps(self, temps) -> None:
        """Remove half-written downloads whose restore is no longer running."""
        for rel, path in temps:
            pid = _temp_pid(rel.rsplit("/", 1)[-1])
            if pid is None or pid == os.getpid() or _pid_alive(pid):
                continue
            try:
                os.unlink(path)
                self._report(f"  Removed '{rel}', an incomplete download left "
                             f"by an interrupted restore")
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # Public sync entrypoint
    # ------------------------------------------------------------------ #
    def sync_directory(self, local_dir: str, remote_dir: str,
                       direction: str = "push", dry_run: bool = False,
                       mirror: bool = True, allow_mass_delete=False,
                       state=None, unattended: bool = False,
                       outstanding: bool = False) -> dict:
        """Push or pull one pair. `state` is its :class:`~nasync.core.state.SyncState`."""
        if direction == "push":
            return self._push_directory(local_dir, remote_dir, dry_run, mirror,
                                        allow_mass_delete, state, unattended,
                                        outstanding)
        elif direction == "pull":
            return self._pull_directory(local_dir, remote_dir, dry_run, state)
        raise ValueError(f"Unknown direction: {direction}")

    # ------------------------------------------------------------------ #
    # Push  (local -> server)
    # ------------------------------------------------------------------ #
    @staticmethod
    def plan_push(local_map: dict, remote_map: dict, remote_mtimes: dict,
                  remote_dirs: set, mirror: bool, record: dict = None,
                  bootstrap: bool = True, local_hash=None, remote_hash=None,
                  local_dirs=(), protected=(), shielded=()) -> dict:
        """Decide what a push does, without doing any of it.

        Separate from the transfer so the decisions — which are where data is
        won or lost — can be tested as plain data. Returns::

            upload     [(local_rel, remote_rel)]  new or changed files
            unchanged  [local_rel]
            delete     [remote_rel]               mirror only
            renames    [(old_remote, new_rel)]    same file, new letter case
            moves      [(old_remote, new_rel)]    same file, new place
            stamps     [(remote_rel, mtime)]      older uploads, given the real time
            stale      [remote_dir]               mirror only, deepest first
            conflicts  [remote_rel]               changed on the NAS: set aside
            foreign    [remote_rel]               on the NAS, not put there by us
            kept       [remote_rel]               recorded, deleted locally, but
                                                  changed on the NAS since: kept
            learn      {remote_rel: Entry}        record updates, no transfer
            forget     [remote_rel]               records of files gone both sides
            temps      [remote_rel]               our own interrupted uploads
            mkdirs     [local_dir]                folders the server lacks

        **With a record** (``record``, from :mod:`nasync.core.state`), a file
        has changed locally when its size or time differs *at all* from what
        was uploaded — an older version put back is a change — unless its
        checksum proves the content the same (a touch, a copy that kept its
        bytes). It has changed **on the NAS** when the server's size or time
        moved since the upload, beyond the slack and beyond a whole-hour DST
        shift, unless the server copy's checksum proves it the same.

        A file changed on the NAS is never overwritten: it is set aside under
        a new name first (a *conflict copy*), then the local file is uploaded.
        A server file NASync never uploaded is never deleted — it was put there
        by someone else — once the pair has completed one sync with a record.
        Before that (``bootstrap``), the server's contents came from an older
        version, and the rules are the ones that applied then: mirror deletes
        server files with no local counterpart, and update mode records them
        as NASync's own, so turning mirror on later still removes them.

        Never deleted, whatever the record says: anything under a local folder
        that couldn't be read (`protected`), a server file whose local twin
        differs only in letter case (`shielded`), and conflict copies, which
        exist on the NAS only.

        **Without a record**, a file is changed when its size differs or the
        local copy is newer; files uploaded before 0.20.0 carry their upload
        time and are given the real one (``stamps``).

        **Letter case is not identity**, and **a moved file is moved, not
        copied again** — by checksum when the record has one, otherwise only
        where name, size and time all match within the slack.
        """
        record = record or {}

        # A FAT/exFAT volume shows every time an hour off after a DST change.
        # That moves most files at once; one file an hour off is an edit
        # (until 0.21.1 it was waved through, and the edit overwritten).
        timed = hour_off = 0
        for remote_rel, rec in record.items():
            rtime = remote_mtimes.get(remote_rel)
            if rtime is None or rec.remote_mtime is None or remote_rel not in remote_map:
                continue
            timed += 1
            hour_off += abs(abs(rtime - rec.remote_mtime) - 3600) <= MTIME_SLACK
        dst_shift = hour_off >= 10 and hour_off * 2 >= timed

        def changed_on_server(remote_rel, rec):
            rsize, rtime = remote_map[remote_rel], remote_mtimes.get(remote_rel)
            if rsize != rec.remote_size:
                return True
            if rtime is None or rec.remote_mtime is None:
                return False
            drift = abs(rtime - rec.remote_mtime)
            if drift <= MTIME_SLACK:
                return False
            if dst_shift and abs(drift - 3600) <= MTIME_SLACK:
                return False             # the volume's DST hour, not an edit
            if rec.sha256 and remote_hash is not None:
                return remote_hash(remote_rel) != rec.sha256
            return True

        def same_as_local(rel, remote_rel):
            """The local file's checksum, when the server copy has the same bytes."""
            _, size, mtime = local_map[rel]
            rtime = remote_mtimes.get(remote_rel)
            if (remote_map[remote_rel] != size or rtime is None
                    or abs(rtime - mtime) > MTIME_SLACK
                    or local_hash is None or remote_hash is None):
                return None
            digest = local_hash(rel)
            return digest if digest and digest == remote_hash(remote_rel) else None

        by_fold = {}
        for remote_rel in remote_map:
            by_fold.setdefault(remote_rel.casefold(), []).append(remote_rel)

        upload, unchanged, renames, stamps, conflicts = [], [], [], [], []
        set_aside = set()    # local files whose server copy is set aside first
        learn = {}
        claimed = set()      # server files spoken for by a local one
        matched = set()      # local files that have a server counterpart
        for rel in sorted(local_map):
            remote_rel = rel if rel in remote_map else None
            if remote_rel is None:
                candidates = [r for r in by_fold.get(rel.casefold(), ())
                              if r not in local_map and r not in claimed]
                if len(candidates) == 1:
                    remote_rel = candidates[0]
            if remote_rel is None or _is_own_temp(remote_rel):
                upload.append((rel, rel))
                continue
            claimed.add(remote_rel)
            matched.add(rel)
            _, size, mtime = local_map[rel]
            rsize, rtime = remote_map[remote_rel], remote_mtimes.get(remote_rel)
            rec = record.get(remote_rel)
            case_only = remote_rel != rel and _parent(remote_rel) == _parent(rel)
            target = rel if case_only else remote_rel

            if rec is not None and not rec.kept:
                local_changed = (size != rec.size or rec.mtime is None
                                 or abs(mtime - rec.mtime) > 1e-6)
                if local_changed and size == rec.size and rec.sha256 \
                        and local_hash is not None \
                        and local_hash(rel) == rec.sha256:
                    local_changed = False            # touched, not edited
                    learn[remote_rel] = rec._replace(mtime=mtime)
                if changed_on_server(remote_rel, rec):
                    digest = same_as_local(rel, remote_rel)
                    if digest:
                        # NASync's own upload, whose record a crash didn't
                        # commit — not someone's edit (third audit, #13).
                        learn[remote_rel] = _entry(size, mtime, rsize, rtime, digest,
                                                   rec.mode)
                        unchanged.append(rel)
                        continue
                    conflicts.append(remote_rel)
                    set_aside.add(rel)
                    upload.append((rel, rel))
                    continue
                if rtime is not None and (rec.remote_mtime is None
                                          or abs(rtime - rec.remote_mtime) > MTIME_SLACK):
                    # First sight of its time, or a DST shift: remember it.
                    learn[remote_rel] = learn.get(remote_rel, rec)._replace(
                        remote_mtime=rtime)
                if case_only:
                    renames.append((remote_rel, rel))
                if local_changed:
                    upload.append((rel, target))
                else:
                    unchanged.append(rel)
                continue

            # No record for this server file.
            same = rsize == size and (rtime is None or abs(rtime - mtime) <= MTIME_SLACK)
            if not bootstrap and not same:
                # Someone else's file at the path of a local one: keep theirs.
                conflicts.append(remote_rel)
                set_aside.add(rel)
                upload.append((rel, rel))
                continue
            if case_only:
                renames.append((remote_rel, rel))
            if rsize != size or _newer(mtime, rtime):
                upload.append((rel, target))
                continue
            unchanged.append(rel)
            theirs = rec is not None and rec.kept
            if rtime is not None and abs(rtime - mtime) > MTIME_SLACK and not theirs:
                stamps.append((target, mtime))
            learn[target] = _entry(size, mtime, rsize,
                                   mtime if rtime is None or abs(rtime - mtime)
                                   <= MTIME_SLACK else rtime if theirs else None,
                                   kept=theirs)

        temps, delete, foreign, kept = [], [], [], []
        for remote_rel in sorted(remote_map):
            if remote_rel in claimed:
                continue
            if _is_own_temp(remote_rel):
                temps.append(remote_rel)
                continue
            if _under(remote_rel, protected) or remote_rel in shielded:
                continue                 # its local side couldn't be read
            rec = record.get(remote_rel)
            if rec is not None and rec.kept or _is_conflict_copy(remote_rel) \
                    or _is_mangled(remote_rel):
                continue
            if rec is not None:
                if changed_on_server(remote_rel, rec):
                    kept.append(remote_rel)
                elif mirror:
                    delete.append(remote_rel)
            elif bootstrap:
                if mirror:
                    delete.append(remote_rel)
                else:
                    # Left by an older version (or deleted locally before the
                    # record existed): NASync's, so a later mirror removes it.
                    learn[remote_rel] = _entry(None, None, remote_map[remote_rel],
                                               remote_mtimes.get(remote_rel))
            else:
                foreign.append(remote_rel)

        moves = []
        if mirror and delete:
            fresh = [rel for rel, _ in upload
                     if rel not in matched and local_map[rel][1] > 0]
            gone = list(delete)
            # Exact first: same content, proven by the record's checksum.
            if local_hash is not None:
                by_digest = {}
                for old in gone:
                    rec = record.get(old)
                    if rec is not None and rec.sha256 \
                            and remote_map[old] == rec.remote_size:
                        by_digest.setdefault((remote_map[old], rec.sha256),
                                             []).append(old)
                sizes = {size for size, _ in by_digest}
                for new in list(fresh):
                    size = local_map[new][1]
                    if size not in sizes:
                        continue
                    olds = by_digest.get((size, local_hash(new)), [])
                    olds = [o for o in olds if o in gone]
                    if not olds:
                        continue
                    olds.sort(key=lambda o: (o.rsplit("/", 1)[-1]
                                             != new.rsplit("/", 1)[-1], o))
                    moves.append((olds[0], new))
                    fresh.remove(new)
                    gone.remove(olds[0])
            # Then, without a checksum: name, size and time must all agree.
            for depth in range(1, _MOVE_MATCH_DEPTH + 1):
                if not fresh or not gone:
                    break
                new_keys, old_keys = {}, {}
                for rel in fresh:
                    new_keys.setdefault((_tail(rel, depth), local_map[rel][1]),
                                        []).append(rel)
                for old in gone:
                    if record.get(old) is not None and record[old].sha256:
                        continue         # had a checksum, and it didn't match
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

        forget = sorted(r for r in record if r not in remote_map)

        remote_folded = {d.casefold() for d in (remote_dirs or ())}
        mkdirs = sorted(d for d in local_dirs if d.casefold() not in remote_folded)

        stale = []
        if mirror:
            stale = SAMBAClient._stale_dirs(remote_dirs or set(), local_map,
                                            local_dirs, protected)

        # Uploads that replace a server copy nothing else keeps. Counted for
        # the guard: one unattended run overwriting every backup with the
        # same garbage (ransomware, a tool that truncated everything) loses
        # as much as deleting them.
        overwrites = [rel for rel, _ in upload
                      if rel in matched and rel not in set_aside]

        # The guard's yardstick: server files a run *could* delete or replace.
        # Other people's files, conflict copies and protected ones can't be,
        # and counting them let 99 of your 100 photos go in a folder that also
        # held 400 of someone else's (third audit, #1).
        def could_go(remote_rel):
            if (_is_own_temp(remote_rel) or _under(remote_rel, protected)
                    or remote_rel in shielded or _is_conflict_copy(remote_rel)
                    or _is_mangled(remote_rel)):
                return False
            rec = record.get(remote_rel)
            return (not rec.kept) if rec is not None else bootstrap
        eligible = sum(1 for remote_rel in remote_map if could_go(remote_rel))

        return {"upload": upload, "unchanged": unchanged, "delete": delete,
                "renames": renames, "moves": sorted(moves), "stamps": stamps,
                "stale": stale, "conflicts": conflicts, "foreign": foreign,
                "kept": kept, "learn": learn, "forget": forget, "temps": temps,
                "mkdirs": mkdirs, "overwrites": overwrites, "eligible": eligible}

    @staticmethod
    def check_mirror(plan: dict, local_count: int, remote_count: int,
                     allow_mass_delete=False, unattended: bool = False,
                     bootstrap: bool = False, outstanding: bool = False) -> None:
        """Refuse the part of a run that looks like a mistake.

        Raises :class:`MirrorRefused`, before anything has been touched, when:

        * **the local folder is empty** and its server folder isn't. Never
          confirmable: it means the folder moved, a disk didn't mount, or a
          pair points at the wrong place, and the price is the whole backup.
        * **a large share would be deleted** — more than half of the pair's
          server files when a person runs it, more than a fifth when nobody is
          watching (`UNATTENDED_FRACTION`), and at least `MASS_DELETE_MIN`.
        * **a large share would be overwritten**, by the same measure. A run
          that replaces every backup with ransomware's output destroys as much
          as one that deletes them.
        * **this is the first mirror run under a record** (`bootstrap`) and it
          would delete `MASS_DELETE_MIN` or more: the record can't yet tell
          files the user deleted from files someone else put there, and a
          server switched from update to mirror deletes years of leftovers.

        Shares are of the files a run could delete (``plan["eligible"]``), not
        of everything in the folder. And a hold the background service made
        and nobody has answered (`outstanding`) is asked about by the next
        run a person starts, whatever its share — until 0.22.1 pressing Sync
        waved a 30% delete the service had held straight through.

        `allow_mass_delete` is a person's yes: ``True`` for any amount (the
        CLI's explicit flag), or the `fingerprint` of the refusal they saw.
        That names the deletions and the overwrites that were held, each on
        its own — so a yes still holds after the run that asked went ahead
        with the rest, and anything new found by then is asked about again.
        """
        deletes = len(plan["delete"])
        overwrites = len(plan.get("overwrites", ()))
        held_deletes = "d" + _list_digest(plan["delete"])
        held_overwrites = "o" + _list_digest(plan.get("overwrites", ()))
        if not remote_count or not (deletes or overwrites):
            return
        if local_count == 0 and deletes:
            raise MirrorRefused(
                f"the local folder is empty, so mirroring it would delete all "
                f"{remote_count:,} file(s) in its server folder. Nothing was "
                f"deleted. If the folder moved, point the pair at its new "
                f"place; if you emptied it on purpose, delete the server copy "
                f"yourself.",
                deletes, remote_count, confirmable=False, fingerprint=held_deletes)
        if allow_mass_delete is True:
            return
        approved = set(str(allow_mass_delete).split(":")) if allow_mass_delete else set()
        share = UNATTENDED_FRACTION if unattended else MASS_DELETE_FRACTION
        yardstick = plan.get("eligible", remote_count)
        big_delete = deletes >= MASS_DELETE_MIN and (
            deletes > share * yardstick or bootstrap or outstanding) \
            and held_deletes not in approved
        big_overwrite = overwrites >= MASS_DELETE_MIN and \
            overwrites > share * yardstick and held_overwrites not in approved
        if not (big_delete or big_overwrite):
            return
        fingerprint = ":".join(([held_deletes] if big_delete else [])
                               + ([held_overwrites] if big_overwrite else []))
        what = []
        if big_delete:
            what.append(f"delete {deletes:,}")
        if big_overwrite:
            what.append(f"overwrite {overwrites:,}")
        why = (" This is the first sync under NASync's new record, so it can't "
               "yet tell files you deleted from files someone else put there."
               if bootstrap and big_delete else "")
        raise MirrorRefused(
            f"this would {' and '.join(what)} of the {remote_count:,} file(s) in "
            f"its server folder, so that part was held back; new files were "
            f"still backed up.{why} Check the list, then confirm to let it go "
            f"ahead.",
            deletes, remote_count, confirmable=True, fingerprint=fingerprint,
            overwrites=overwrites, hold_deletes=big_delete,
            hold_overwrites=big_overwrite,
            sample=([f"✗ {r}" for r in sorted(plan["delete"])[:10]] if big_delete else [])
            + ([f"↻ {r}" for r in sorted(plan.get("overwrites", ()))[:10]]
               if big_overwrite else []))

    def _push_directory(self, local_dir: str, remote_dir: str,
                        dry_run: bool = False, mirror: bool = True,
                        allow_mass_delete=False, state=None,
                        unattended: bool = False, outstanding: bool = False) -> dict:
        local_dir = os.path.abspath(os.path.expanduser(local_dir))
        if not os.path.isdir(local_dir):
            raise Exception(f"Local directory not found: {local_dir}")

        base = self._remote(remote_dir)

        def unc(rel):
            return self._unc(base, rel.replace("/", "\\"))

        # --- 1. index both sides, and recall what was last put there ---
        self._stage("Reading your folder…")
        scan = self._scan_local(local_dir)
        local_map = scan.files
        remote_dirs = set()
        remote_mtimes = {}
        self._stage("Reading the server's copy…")
        remote_map = self._remote_file_map(base, dirs=remote_dirs,
                                           mtimes=remote_mtimes,
                                           on_scan=self._scan_stage)
        record = state.load() if state is not None else None
        bootstrap = state is None or not state.initialised
        first_mirror = bootstrap or not state.mirrored
        # Names differing only in case: keep the one the server already has,
        # so a new "Notes.txt" never replaces the backup of "notes.txt".
        dropped, shielded = _settle_case_twins(local_map, scan.twins,
                                               remote_map, record or {})
        unsupported = sorted(scan.unsupported + dropped)
        if not dry_run:
            self._recover_set_aside(base, remote_map, remote_mtimes, record or {})
            self._clear_local_temps(scan.temps)

        def local_hash(rel):
            return _file_digest(local_map[rel][0])

        def remote_hash(remote_rel):
            try:
                return self._digest_unc(unc(remote_rel))
            except SyncCancelled:
                raise
            except Exception:
                return None              # unreadable: treat as changed

        # --- 2. decide, and refuse before touching anything ---
        self._stage(f"Comparing {len(local_map):,} local and "
                    f"{len(remote_map):,} server file(s)…")
        plan = self.plan_push(local_map, remote_map, remote_mtimes, remote_dirs,
                              mirror, record=record, bootstrap=bootstrap,
                              local_hash=local_hash, remote_hash=remote_hash,
                              local_dirs=scan.dirs, protected=scan.unreadable,
                              shielded=shielded)
        refusal = None
        try:
            self.check_mirror(plan, len(local_map), len(remote_map),
                              allow_mass_delete, unattended=unattended,
                              bootstrap=first_mirror, outstanding=outstanding)
        except MirrorRefused as e:
            refusal = e
        if refusal is not None and not dry_run:
            # Hold back only what was refused; everything that destroys
            # nothing still happens, so new work is never left unbacked-up
            # while a question waits for an answer.
            if refusal.hold_deletes:
                plan["delete"], plan["stale"] = [], []
            if refusal.hold_overwrites:
                held = set(plan["overwrites"])
                plan["upload"] = [u for u in plan["upload"] if u[0] not in held]

        upload, delete = plan["upload"], plan["delete"]
        moves, renames = plan["moves"], plan["renames"]
        stamps, stale = plan["stamps"], plan["stale"]
        conflicts, foreign, kept = plan["conflicts"], plan["foreign"], plan["kept"]

        def report_skipped():
            for rel, reason in unsupported:
                self._report(f"  ! skipped ({reason}): {rel}")
            for rel in scan.unreadable:
                self._report(f"  ! couldn't read '{rel or '.'}' — its backup "
                             f"was left as it was")

        if dry_run:
            extras = []
            if moves:
                extras.append(f"{len(moves)} to move")
            if conflicts or kept:
                extras.append(f"{len(conflicts) + len(kept)} changed on the NAS "
                              f"(kept as copies)")
            if foreign:
                extras.append(f"{len(foreign)} not put there by NASync (kept)")
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
            for rel in conflicts + kept:
                self._report(f"    ⚠ {rel} changed on the NAS — would be kept as a copy")
            for rel in delete:
                self._report(f"    ✗ {rel} (removed locally)")
            report_skipped()
            if refusal is not None:
                self._report(f"  🛑 A real run would hold back part of this: {refusal}")
            return {"transferred": len(upload), "skipped": len(plan["unchanged"]),
                    "deleted": len(delete), "moved": len(moves), "errors": 0,
                    "unsupported": len(unsupported),
                    "conflicts": len(conflicts) + len(kept),
                    "foreign": len(foreign),
                    "refused": str(refusal) if refusal else None}

        quiet = not (upload or delete or moves or renames or stamps or stale
                     or conflicts or foreign or kept or plan["temps"]
                     or plan["mkdirs"] or plan["learn"] or plan["forget"]
                     or refusal)
        errors = [f"couldn't read '{rel or '.'}'" for rel in scan.unreadable]
        # Problems on this machine (unreadable files and folders) are counted
        # apart: a retry can't fix them, and they don't stop the record from
        # vouching for the server — everything under them is left alone.
        local_errors = len(errors)
        if quiet:
            self._report(f"  Already up to date ({len(plan['unchanged'])} file(s)).")
            report_skipped()
            return {"transferred": 0, "skipped": len(plan["unchanged"]),
                    "deleted": 0, "errors": len(errors),
                    "local_errors": local_errors,
                    "unsupported": len(unsupported)}

        def record_put(rel, entry):
            if state is not None:
                state.put(rel, entry)

        # --- 3. our own interrupted uploads, from an earlier run ---
        for rel in plan["temps"]:
            try:
                smbclient.remove(unc(rel), **self._opts)
            except Exception:
                pass

        # --- 4. NAS-side changes are set aside before anything overwrites them ---
        set_aside = 0
        stamp_text = time.strftime("%Y-%m-%d %H%M")
        taken = {r.casefold() for r in remote_map}
        for rel in conflicts + kept:
            self._check_cancelled()
            copy = _conflict_name(rel, stamp_text, taken)
            try:
                smbclient.rename(unc(rel), unc(copy), **self._opts)
            except SyncCancelled:
                raise
            except Exception as e:
                errors.append(f"{rel}: changed on the NAS, and couldn't be set "
                              f"aside ({self._friendly_error(e)}) — not overwritten")
                upload = [u for u in upload if u[0] != rel and u[1] != rel]
                continue
            taken.add(copy.casefold())
            set_aside += 1
            record_put(copy, _entry(None, None, remote_map[rel],
                                    remote_mtimes.get(rel), kept=True))
            if state is not None:
                state.forget(rel)
            self._report(f"  ⚠ {rel} was changed on the NAS since the last sync — "
                         f"kept as '{copy.rsplit('/', 1)[-1]}'")
        for rel in foreign:
            record_put(rel, _entry(None, None, remote_map[rel],
                                   remote_mtimes.get(rel), kept=True))

        # --- 5. letter-case renames, before anything is written at the name ---
        renamed = 0
        for old, new in renames:
            self._check_cancelled()
            try:
                self._rename_case(unc(old), unc(new))
                renamed += 1
                if state is not None:
                    state.move(old, new)
            except SyncCancelled:
                raise
            except Exception:
                # Harmless: the server keeps the old casing, and the file is
                # still the same file — nothing is deleted on its account.
                upload[:] = [(rel, old if rel == new else target)
                             for rel, target in upload]
                stamps[:] = [(old if rel == new else rel, t) for rel, t in stamps]
                if new in plan["learn"]:
                    plan["learn"][old] = plan["learn"].pop(new)

        # --- 6. directories, then moves and uploads ---
        # A file and a folder of the same name, one on each side. In mirror
        # mode the old one goes when the deletes run, and the new one follows
        # next sync; until then only its own path waits — not the whole pair
        # (a failed mkdir used to abort it), and no folder is ever moved.
        remote_dirs_folded = {d.casefold() for d in remote_dirs}
        remote_files_folded = {r.casefold(): r for r in remote_map}
        doomed = {r.casefold() for r in delete}

        def blocked(target):
            if target.casefold() in remote_dirs_folded:
                return "the NAS has a folder of this name"
            parts = target.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                folder = "/".join(parts[:depth]).casefold()
                if folder in remote_files_folded:
                    if folder in doomed:
                        return None          # deleted below, before its folder is made
                    return "the NAS has a file where this folder would go"
            return None

        early, deleted_early = [], 0
        for rel, target in list(upload):
            why = blocked(target)
            if why:
                upload.remove((rel, target))
                errors.append(f"{rel}: {why} — "
                              + ("it goes when this sync's deletions run; the "
                                 "file follows next sync" if mirror else
                                 "remove or rename one of them"))
                continue
            for depth in range(1, target.count("/") + 1):
                folder = "/".join(target.split("/")[:depth]).casefold()
                if folder in remote_files_folded and folder in doomed:
                    early.append(remote_files_folded[folder])
        for rel in sorted(set(early)):
            # A file standing where a new folder goes is deleted first.
            try:
                smbclient.remove(unc(rel), **self._opts)
                delete.remove(rel)
                deleted_early += 1
                if state is not None:
                    state.forget(rel)
                remote_files_folded.pop(rel.casefold(), None)
            except SyncCancelled:
                raise
            except Exception as e:
                errors.append(f"{rel}: {self._friendly_error(e)}")
        # One makedirs per *directory* rather than per file: a thousand photos
        # in one album used to cost a thousand identical mkdir round trips.
        self._ensure_remote_dir(self._unc(base))
        parents = {target.rsplit("/", 1)[0] for _, target in upload if "/" in target}
        parents |= {new.rsplit("/", 1)[0] for _, new in moves if "/" in new}
        parents |= set(plan["mkdirs"])
        for parent in self._leaf_dirs(parents):
            try:
                self._ensure_remote_dir(self._unc(base, parent.replace("/", "\\")))
            except SyncCancelled:
                raise
            except Exception as e:
                errors.append(f"{parent}/: {e}")
                upload = [u for u in upload if not u[1].startswith(parent + "/")]

        moved = 0
        for index, (old, new) in enumerate(moves, start=1):
            self._check_cancelled()
            self._progress(index, len(moves), new.rsplit("/", 1)[-1], "Moving")
            try:
                smbclient.rename(unc(old), unc(new), **self._opts)
                moved += 1
                if state is not None:
                    rec = (record or {}).get(old)
                    state.move(old, new)
                    _, size, mtime = local_map[new]
                    state.put(new, (rec or _entry(size, mtime, size, mtime))
                              ._replace(size=size, mtime=mtime))
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
            abs_local, size, mtime = local_map[rel]
            name = os.path.basename(rel)
            self._progress(transferred + 1, total, name, "Uploading")
            try:
                sent, digest, stamped = self._upload_file(abs_local, unc(target),
                                                          name, mtime)
                transferred += 1
            except SyncCancelled:
                raise
            except LocalFileError as e:
                errors.append(f"{rel}: {e}")
                local_errors += 1
                continue
            except Exception as e:
                errors.append(f"{rel}: {self._friendly_error(e)}")
                continue
            try:
                after = os.stat(abs_local)
                mode = after.st_mode & 0o7777
                if after.st_size != size or after.st_mtime != mtime:
                    # Changed while it was being read. The record keeps the
                    # earlier stat, so the next run sends it again.
                    self._report(f"  ! {rel} changed while it was uploading — "
                                 f"it will be sent again next sync")
            except OSError:
                mode = None
            if target != rel and state is not None:
                state.forget(rel)
            record_put(target, _entry(size, mtime, sent,
                                      mtime if stamped else None, digest, mode))

        # --- 7. one-time: give older uploads their real modification time ---
        stamped_count = 0
        for rel, mtime in stamps:
            self._check_cancelled()
            ok = self._stamp(unc(rel), mtime)
            stamped_count += ok
            entry = plan["learn"].get(rel)
            if entry is not None:
                plan["learn"][rel] = entry._replace(
                    remote_mtime=mtime if ok else remote_mtimes.get(rel))
        for rel, entry in plan["learn"].items():
            if entry.mode is None:
                try:
                    source = local_map.get(rel) or next(
                        (v for k, v in local_map.items() if k.casefold() == rel.casefold()),
                        None)
                    if source is not None:
                        entry = entry._replace(mode=os.stat(source[0]).st_mode & 0o7777)
                except OSError:
                    pass
            record_put(rel, entry)

        # --- 8. mirror deletes ---
        deleted = deleted_early
        for index, rel in enumerate(delete, start=1):
            self._check_cancelled()
            self._progress(index, len(delete), rel.rsplit("/", 1)[-1], "Deleting")
            try:
                smbclient.remove(unc(rel), **self._opts)
                deleted += 1
                if state is not None:
                    state.forget(rel)
            except SyncCancelled:
                raise
            except Exception as e:
                errors.append(f"{rel}: {self._friendly_error(e)}")
        for rel in plan["forget"]:
            if state is not None:
                state.forget(rel)

        # --- 9. and the directories left empty — including ones an earlier
        # run (or an older version) left behind ---
        removed_dirs = 0
        if stale:
            self._stage(f"Removing {len(stale):,} empty folder(s)…")
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
        if stamped_count:
            self._report(f"  Recorded the modification time of {stamped_count} file(s) "
                         f"uploaded by an older version (once only).")
        if foreign:
            self._report(f"  Kept {len(foreign)} file(s) on the NAS that NASync "
                         f"didn't put there (said once): "
                         + ", ".join(foreign[:5]) + (" …" if len(foreign) > 5 else ""))
        report_skipped()
        for message in errors:
            self._report(f"  ! {message}")
        held = None
        if refusal is not None:
            self._report(f"  🛑 Held back: {refusal}")
            held = {"message": str(refusal), "deletes": refusal.deletes,
                    "overwrites": refusal.overwrites, "total": refusal.total,
                    "confirmable": refusal.confirmable,
                    "fingerprint": refusal.fingerprint,
                    "hold_deletes": refusal.hold_deletes,
                    "hold_overwrites": refusal.hold_overwrites,
                    "sample": refusal.sample}
        return {"transferred": transferred, "skipped": len(plan["unchanged"]),
                "deleted": deleted, "deleted_dirs": removed_dirs,
                "moved": moved, "errors": len(errors),
                "local_errors": local_errors,
                "unsupported": len(unsupported), "conflicts": set_aside,
                "foreign": len(foreign), "held": held}

    # ------------------------------------------------------------------ #
    # Pull  (server -> local)
    # ------------------------------------------------------------------ #
    def _pull_directory(self, local_dir: str, remote_dir: str,
                        dry_run: bool = False, state=None) -> dict:
        local_dir = os.path.abspath(os.path.expanduser(local_dir))
        base = self._remote(remote_dir)

        remote_mtimes = {}
        self._stage("Reading the server's copy…")
        remote_map = self._remote_file_map(base, mtimes=remote_mtimes,
                                           must_exist=True,
                                           on_scan=self._scan_stage)
        self._stage(f"Comparing {len(remote_map):,} server file(s) with yours…")
        record = state.load() if state is not None else {}
        # Where each file is read from: itself, or — for a file an interrupted
        # sync left only under its hidden aside name — that name. Restore
        # used to skip those as leftovers, so the file was silently missing.
        source = {rel: rel for rel in remote_map if not _is_own_temp(rel)}
        present = {rel.casefold() for rel in source}
        for rel in sorted(remote_map):
            folder, _, name = rel.rpartition("/")
            for mark in (_OLD_MARK, _CASE_TEMP_MARK):
                if mark not in name:
                    continue
                missing = [r.rpartition("/")[2] for r in record
                           if r.rpartition("/")[0] == folder
                           and r.casefold() not in present]
                original = _temp_original(name, mark, missing)
                target = (f"{folder}/{original}" if folder else original) if original else None
                if target and target.casefold() not in present:
                    source[target] = rel
                    present.add(target.casefold())

        # Restore never destroys work the backup hasn't seen. A local file
        # that changed since it was last backed up — per the record, or, with
        # no record, newer than the server's copy — is kept, whatever its
        # size. Until 0.21.0 any size difference meant "download", so an
        # unsynced edit that changed a file's length was overwritten by the
        # older backup. Everything else that is missing, a different size or
        # newer on the server is fetched.
        to_download = []
        replaces = set()
        unchanged = []
        kept_local = []
        for rel in sorted(source):
            size = remote_map[source[rel]]
            local_path = os.path.join(local_dir, *rel.split("/"))
            try:
                info = os.stat(local_path)
                present = os.path.isfile(local_path)
            except OSError:
                present = False
            if not present:
                to_download.append(rel)
                continue
            rtime = remote_mtimes.get(source[rel])
            rec = record.get(rel)
            if rec is not None and rec.size is not None:
                unsynced = (info.st_size != rec.size
                            or abs(info.st_mtime - (rec.mtime or 0)) > 1e-6)
                if unsynced and rec.sha256 and info.st_size == rec.size:
                    unsynced = _file_digest(local_path) != rec.sha256
            else:
                unsynced = _newer(info.st_mtime, rtime)
            if unsynced:
                kept_local.append(rel)
            elif info.st_size == size and not _newer(rtime, info.st_mtime):
                unchanged.append(rel)
            else:
                to_download.append(rel)
                replaces.add(rel)

        def report_kept():
            for rel in kept_local:
                self._report(f"  ! kept your local '{rel}' — it changed since "
                             f"the last backup. Sync it, or move it aside and "
                             f"restore again to get the server's copy.")

        if dry_run:
            self._report(f"  [dry-run] would restore from \\{base} into {local_dir}")
            self._report(f"  [dry-run] {len(to_download)} to download "
                         f"({len(replaces)} replacing a local file), "
                         f"{len(unchanged)} unchanged, {len(kept_local)} kept")
            for rel in to_download:
                self._report(f"    ↓ {rel}" + ("  (replaces your local copy)"
                                               if rel in replaces else ""))
            report_kept()
            return {"transferred": 0, "skipped": len(unchanged),
                    "deleted": 0, "errors": 0, "kept_local": len(kept_local)}

        os.makedirs(local_dir, exist_ok=True)

        total = len(to_download)
        transferred = 0
        errors = []
        for rel in to_download:
            self._check_cancelled()
            name = os.path.basename(rel)
            local_path = os.path.join(local_dir, *rel.split("/"))
            rec = record.get(rel)
            mode = rec.mode if rec is not None else None
            mtime = remote_mtimes.get(source[rel])
            self._progress(transferred + 1, total, name, "Downloading")
            try:
                digest = self._download_file(
                    self._unc(base, source[rel].replace("/", "\\")), local_path,
                    name, remote_map[source[rel]], mtime, mode)
                transferred += 1
            except SyncCancelled:
                raise
            except LocalFileError as e:
                errors.append(f"{rel}: {e}")
                continue
            except Exception as e:
                errors.append(f"{rel}: {self._friendly_error(e)}")
                continue
            if state is not None:
                try:
                    info = os.stat(local_path)
                    if source[rel] != rel:
                        continue          # recorded once a push puts it back
                    state.put(rel, _entry(info.st_size, info.st_mtime,
                                          remote_map[rel], mtime, digest,
                                          info.st_mode & 0o7777,
                                          kept=bool(rec and rec.kept)))
                except OSError:
                    pass

        self._report(f"  Downloaded {transferred} file(s) · "
                     f"unchanged {len(unchanged)}"
                     + (f" · kept {len(kept_local)} newer local file(s)"
                        if kept_local else ""))
        report_kept()
        for message in errors:
            self._report(f"  ! {message}")
        return {"transferred": transferred, "skipped": len(unchanged),
                "deleted": 0, "errors": len(errors), "kept_local": len(kept_local)}


class LocalScan(NamedTuple):
    files: dict          # rel -> (abs_path, size, mtime)
    unsupported: list    # [(rel, reason)] — listed, never silently dropped
    dirs: set            # every folder, including empty ones
    unreadable: list     # folders that couldn't be listed (protected)
    twins: list = ()     # [[rel, ...]] names differing only in letter case
    temps: list = ()     # [(rel, abs_path)] our own half-written downloads


#: What NASync names its own in-flight files on the server. Always safe to
#: remove, never reported as someone else's, never restored.
_TEMP_MARK = ".nasync-part-"
_OLD_MARK = ".nasync-old-"
_CASE_TEMP_MARK = ".nasync-case-"


#: Longest name, in bytes, that the disks NASync meets will take (ext4, btrfs,
#: and Samba serving them: 255 bytes of UTF-8).
_NAME_MAX = 255


def _is_own_temp(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(mark in name for mark in (_TEMP_MARK, _OLD_MARK, _CASE_TEMP_MARK))


def _name_digest(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def _temp_name(base: str, mark: str) -> str:
    """A hidden name for one of NASync's in-flight files beside `base`.

    Normally `.{base}{mark}{pid}`, so a leftover says whose it was. A name
    already near the limit can't take the extra ~20 bytes — until 0.21.1 such
    a file could be neither uploaded nor restored — and gets a short form,
    `{mark}{pid}-{digest of base}`, still recognisably NASync's own.
    """
    name = f".{base}{mark}{os.getpid()}"
    if len(name.encode("utf-8")) <= _NAME_MAX:
        return name
    return f"{mark}{os.getpid()}-{_name_digest(base)}"


def _temp_original(name: str, mark: str, candidates=()):
    """Which file a temp name of ours stood in for, or None if unknown.

    The short form names only a digest, so it is matched against
    `candidates` — names the record expects that the server no longer has.
    """
    if name.startswith(mark):
        digest = name.rsplit("-", 1)[-1]
        return next((c for c in candidates if _name_digest(c) == digest), None)
    if name.startswith(".") and mark in name:
        return name[1:name.rindex(mark)] or None
    return None


def _temp_pid(name: str):
    """The process that made a temp name of ours, or None."""
    for mark in (_TEMP_MARK, _OLD_MARK, _CASE_TEMP_MARK):
        if mark in name:
            tail = name[name.rindex(mark) + len(mark):].split("-", 1)[0]
            return int(tail) if tail.isdigit() else None
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True              # exists, but someone else's
    return True


def _settle_case_twins(files: dict, twins, remote_map=(), record=()):
    """Keep one of each group of local names that differ only in letter case.

    An SMB share can't keep "notes.txt" and "Notes.txt" apart, so only one
    goes. The one kept is the one the server already has (then the record's,
    then the first by name): until 0.21.1 it was simply the first by name, so
    creating "Notes.txt" beside "notes.txt" replaced the backup of a file
    that was still there. Returns ``([(rel, reason)], shielded)``: the names
    left out, and the ones whose server copy must not be deleted on that
    account — the server, too, may hold both (made there by something else).
    """
    dropped, shielded = [], set()
    for group in twins:
        on_server = [rel for rel in group if rel in remote_map]
        recorded = [rel for rel in group if rel in record]
        keep = (on_server or recorded or group)[0]
        for other in group:
            if other == keep:
                continue
            files.pop(other, None)
            dropped.append((other, f"differs from '{keep}' only in letter case, "
                                   f"which an SMB share can't keep apart"))
            if other in remote_map:
                shielded.add(other)
    return dropped, shielded


_CONFLICT_RE = re.compile(r" \(NAS copy \d{4}-\d{2}-\d{2} \d{4}(?: \d+)?\)(?:\.[^.]*)?$")


def _is_conflict_copy(rel: str) -> bool:
    """A NAS-side version NASync set aside: exists on the NAS only, so a
    mirror never deletes it — even with no record (one lost to a renamed
    server entry, say)."""
    return bool(_CONFLICT_RE.search(rel.rsplit("/", 1)[-1]))


def _is_mangled(rel: str) -> bool:
    """An 8.3 alias Samba listed for a name it couldn't store as given."""
    return bool(_MANGLED_RE.match(rel.rsplit("/", 1)[-1]))


def _list_digest(items) -> str:
    return hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest()[:16]


def _under(rel: str, folders) -> bool:
    """True when `rel` is inside any of `folders` ("" is the whole tree)."""
    folded = rel.casefold()
    for folder in folders:
        if folder == "" or folded == folder.casefold() \
                or folded.startswith(folder.casefold() + "/"):
            return True
    return False


def _conflict_name(rel: str, stamp: str, taken: set) -> str:
    """'docs/report.docx' → 'docs/report (NAS copy 2026-11-05 1925).docx'."""
    folder, _, name = rel.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not stem:
        stem, dot, ext = name, "", ""
    for n in range(1, 1000):
        suffix = f" (NAS copy {stamp})" if n == 1 else f" (NAS copy {stamp} {n})"
        candidate = f"{stem}{suffix}{dot}{ext}"
        while len(candidate.encode("utf-8")) > _NAME_MAX and len(stem) > 1:
            stem = stem[:-1]                    # a long name: shorten its stem
            candidate = f"{stem}{suffix}{dot}{ext}"
        full = f"{folder}/{candidate}" if folder else candidate
        if full.casefold() not in taken:
            return full
    raise RuntimeError(f"no free name to set aside {rel}")


def _file_digest(path: str):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _entry(size, mtime, remote_size, remote_mtime, sha256=None, mode=None,
           kept=False):
    from nasync.core.state import Entry
    return Entry(size, mtime, remote_size, remote_mtime, sha256, mode, kept)
