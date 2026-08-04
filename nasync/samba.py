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

import os
import socket
import threading
from pathlib import Path

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
                 byte_progress=None, cancel_event=None):
        self.ip = ip
        self.username = username
        self.password = password
        self.share = share
        self.port = int(port)
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
        """Open a session and verify the share is actually reachable."""
        try:
            smbclient.register_session(
                self.ip, username=self.username, password=self.password,
                port=self.port,
            )
            # register_session only proves the *server* answered; touching the
            # share root is what proves the share name is valid.
            smbclient.listdir(self._unc(), **self._opts)
        except Exception as e:
            raise self._fail(e)
        self._connected = True

    def disconnect(self):
        if not self._connected:
            return
        try:
            smbclient.delete_session(self.ip, port=self.port)
        except Exception:
            pass  # teardown is best-effort
        self._connected = False

    def list_shares(self) -> list:
        """Enumerate the server's disk shares.

        Share enumeration is an MS-SRVSVC RPC call, not part of SMB itself, and
        `smbprotocol` does not implement it. Where the `smbclient` binary is
        available we use it for this one read-only query; otherwise the caller
        must supply the share name directly.
        """
        import re
        import subprocess

        env = os.environ.copy()
        env["PASSWD"] = self.password
        try:
            result = subprocess.run(
                ["smbclient", "-L", f"//{self.ip}", "-U", self.username,
                 "-p", str(self.port)],
                capture_output=True, text=True, timeout=60, env=env,
            )
        except FileNotFoundError:
            raise Exception(
                "Share browsing needs the 'smbclient' command, which isn't "
                "installed. Enter the share name manually instead."
            )
        except subprocess.TimeoutExpired:
            raise Exception("Timed out listing shares. Check the IP and network.")

        output = (result.stdout or "") + (result.stderr or "")
        for token, message in (
            ("NT_STATUS_LOGON_FAILURE", "Authentication failed. Check username and password."),
            ("NT_STATUS_ACCESS_DENIED", "Access denied. Check share permissions for this user."),
            ("NT_STATUS_HOST_UNREACHABLE", "Server unreachable. Check the IP and network."),
            ("NT_STATUS_CONNECTION_REFUSED", "Connection refused. Is SMB (port 445) open?"),
            ("NT_STATUS_IO_TIMEOUT", "Connection timed out. Check the IP and network."),
        ):
            if token in output:
                raise Exception(message)

        shares = []
        in_section = False
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("Sharename"):
                in_section = True
                continue
            if not in_section or stripped.startswith("---"):
                continue
            if not stripped:            # blank line ends the share block
                break
            parts = re.split(r"\s{2,}", stripped)
            name = parts[0].strip()
            kind = parts[1].strip() if len(parts) > 1 else ""
            # Skip administrative/IPC shares (IPC$, print$, ADMIN$, ...).
            if kind == "Disk" and not name.endswith("$"):
                shares.append(name)
        return shares

    # ------------------------------------------------------------------ #
    # Remote indexing
    # ------------------------------------------------------------------ #
    def _remote_file_map(self, base: str) -> dict:
        """Return ``{posix_relative_path: size}`` for every file beneath `base`.

        An empty dict means the destination doesn't exist yet (first sync).
        """
        root = self._unc(base)
        files = {}
        try:
            for dirpath, _dirnames, filenames in smbclient.walk(root, **self._opts):
                rel_dir = dirpath[len(root):].strip("\\").replace("\\", "/")
                for filename in filenames:
                    self._check_cancelled()
                    rel = f"{rel_dir}/{filename}" if rel_dir else filename
                    full = self._unc(base, rel.replace("/", "\\"))
                    try:
                        files[rel] = smbclient.stat(full, **self._opts).st_size
                    except Exception:
                        continue  # vanished mid-walk; ignore
        except (ObjectNameNotFound, ObjectPathNotFound):
            return {}
        except SyncCancelled:
            raise
        except Exception as e:
            raise self._fail(e)
        return files

    def _ensure_remote_dir(self, path: str) -> None:
        try:
            smbclient.makedirs(path, exist_ok=True, **self._opts)
        except Exception as e:
            raise self._fail(e)

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
        local_map = {}
        unsupported = []
        for item in sorted(Path(local_dir).rglob("*")):
            if not item.is_file():
                continue
            rel = "/".join(item.relative_to(local_dir).parts)
            if not self._storable(rel):
                unsupported.append(rel)
                continue
            local_map[rel] = (str(item), item.stat().st_size)

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
        self._ensure_remote_dir(self._unc(base))
        for rel in to_upload:
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if parent:
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
