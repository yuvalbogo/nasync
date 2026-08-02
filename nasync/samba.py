import os
import re
import subprocess
from pathlib import Path

# One entry line of `smbclient ls`:  "  name<pad>ATTRS   size  Day Mon DD HH:MM:SS YYYY"
_LS_ENTRY = re.compile(
    r"^\s{2}(.*?)\s+([A-Za-z]+)\s+(\d+)\s+"
    r"[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4}\s*$"
)


class SAMBAClient:
    """Thin wrapper around the system `smbclient` tool.

    Each sync operation runs in a *single* smbclient session (one SMB
    connection) rather than spawning a process per file, which keeps large
    directory trees fast. Recursion, directory creation and transfers are
    driven by smbclient's own scripting commands fed over stdin.
    """

    def __init__(self, ip: str, username: str, password: str, share: str,
                 port: int = 445, reporter=None, progress=None):
        self.ip = ip
        self.username = username
        self.password = password
        self.share = share
        self.port = int(port)
        self.url = f"//{ip}/{share}"
        # Progress lines go through this callback; defaults to stdout so the
        # CLI behaves exactly as before. A TUI/GUI can inject its own sink.
        self._report = reporter or print
        # Per-file progress callback: progress(done, total, name, verb).
        # `total` may be None when it isn't known ahead of time (pulls).
        self._progress = progress or (lambda done, total, name, verb: None)

    # ------------------------------------------------------------------ #
    # Low-level session runner
    # ------------------------------------------------------------------ #
    def _run_smbclient(self, args: list, script: str = None,
                       timeout: int = 1800) -> subprocess.CompletedProcess:
        """Invoke smbclient with the given args (auth/port appended)."""
        env = os.environ.copy()
        env["PASSWD"] = self.password  # smbclient reads password from $PASSWD
        try:
            return subprocess.run(
                ["smbclient", *args, "-U", self.username, "-p", str(self.port)],
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError:
            raise Exception(
                "smbclient not found. Install samba-client:\n"
                "  Ubuntu/Debian: sudo apt install smbclient\n"
                "  Fedora/RHEL:   sudo dnf install samba-client\n"
                "  macOS:         brew install samba"
            )

    def _run_session(self, commands: list) -> subprocess.CompletedProcess:
        """Feed a list of smbclient commands to a single session over stdin."""
        script = "\n".join(commands) + "\n"
        return self._run_smbclient([self.url], script=script)

    def _stream_session(self, commands: list, on_line) -> tuple:
        """Run a single smbclient session, invoking on_line(line) as each line
        of output arrives (so callers can report live progress). Returns
        (returncode, combined_output).

        smbclient block-buffers stdout when it's a pipe, which makes per-file
        progress arrive in one lump. Running it against a pseudo-terminal makes
        it line-buffer, and we read raw bytes so nothing is held back. The
        command script is fed on a writer thread to avoid a stdin/stdout
        deadlock when there are many files.
        """
        import pty
        import threading

        script = ("\n".join(commands) + "\n").encode()
        env = os.environ.copy()
        env["PASSWD"] = self.password

        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(
                ["smbclient", self.url, "-U", self.username, "-p", str(self.port)],
                stdin=subprocess.PIPE,
                stdout=slave,
                stderr=slave,
                env=env,
                close_fds=True,
            )
        except FileNotFoundError:
            os.close(master)
            os.close(slave)
            raise Exception(
                "smbclient not found. Install samba-client:\n"
                "  Ubuntu/Debian: sudo apt install smbclient\n"
                "  Fedora/RHEL:   sudo dnf install samba-client\n"
                "  macOS:         brew install samba"
            )
        os.close(slave)

        def _feed():
            try:
                proc.stdin.write(script)
                proc.stdin.close()
            except BrokenPipeError:
                pass

        writer = threading.Thread(target=_feed, daemon=True)
        writer.start()

        collected = []
        buffer = b""
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:  # EIO on Linux when the child exits and closes the pty
                break
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                collected.append(line)
                on_line(line)
        if buffer:
            line = buffer.decode("utf-8", "replace").rstrip("\r")
            collected.append(line)
            on_line(line)

        os.close(master)
        writer.join(timeout=5)
        proc.wait()
        return proc.returncode, "\n".join(collected)

    def list_shares(self) -> list:
        """Return the names of disk shares the server exposes (best effort)."""
        result = self._run_smbclient(["-L", f"//{self.ip}"], timeout=60)
        output = self._combined_output(result)
        fatal = self._fatal_error(output)
        if fatal:
            raise Exception(fatal)

        shares = []
        in_section = False
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("Sharename"):
                in_section = True
                continue
            if not in_section:
                continue
            if stripped.startswith("---"):
                continue
            if not stripped:          # blank line ends the share block
                break
            parts = stripped.split()
            name, kind = parts[0], (parts[1] if len(parts) > 1 else "")
            # Skip administrative/IPC shares (IPC$, print$, ADMIN$, ...).
            if kind == "Disk" and not name.endswith("$"):
                shares.append(name)
        return shares

    @staticmethod
    def _combined_output(result: subprocess.CompletedProcess) -> str:
        return (result.stdout or "") + (result.stderr or "")

    @staticmethod
    def _fatal_error(output: str) -> str:
        """Return a human message for a connection/auth failure, else ''."""
        checks = {
            "NT_STATUS_LOGON_FAILURE": "Authentication failed. Check username and password.",
            "NT_STATUS_ACCESS_DENIED": "Access denied. Check share permissions for this user.",
            "NT_STATUS_BAD_NETWORK_NAME": "Share not found. Check the share name.",
            "NT_STATUS_HOST_UNREACHABLE": "Server unreachable. Check the IP and network.",
            "NT_STATUS_CONNECTION_REFUSED": "Connection refused. Is SMB (port 445) open?",
            "NT_STATUS_IO_TIMEOUT": "Connection timed out. Check the IP and network.",
            "Connection to": "Could not reach the server. Check the IP and network.",
        }
        for token, message in checks.items():
            if token in output:
                return message
        return ""

    # ------------------------------------------------------------------ #
    # Connection check
    # ------------------------------------------------------------------ #
    def connect(self):
        result = self._run_session(["quit"])
        output = self._combined_output(result)
        error = self._fatal_error(output)
        if error:
            raise Exception(error)
        if result.returncode != 0:
            raise Exception(f"Failed to connect to SAMBA server:\n{output.strip()}")

    def disconnect(self):
        pass

    # ------------------------------------------------------------------ #
    # Path helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _remote(path: str) -> str:
        """Normalise a config server path to an smbclient (backslash) path."""
        return path.strip("/").replace("/", "\\")

    @staticmethod
    def _safe(name: str) -> bool:
        """smbclient's scripting can't safely carry quotes/newlines in names."""
        return '"' not in name and "\n" not in name and "\r" not in name

    def _remote_file_map(self, base: str) -> dict:
        """Return {posix_relative_path: size} for every file under `base` on
        the server (recursively). Empty dict if the destination doesn't exist."""
        result = self._run_session([f'cd "{base}"', "recurse ON", "ls", "quit"])
        output = self._combined_output(result)
        if ("NT_STATUS_OBJECT_NAME_NOT_FOUND" in output
                or "NT_STATUS_OBJECT_PATH_NOT_FOUND" in output):
            return {}  # nothing there yet — first sync
        fatal = self._fatal_error(output)
        if fatal:
            raise Exception(fatal)

        prefix = "\\" + base  # directory headers look like "\base\sub"
        files = {}
        current = ""  # current dir relative to base, in posix form
        for line in output.splitlines():
            if line.startswith("\\"):
                header = line.strip()
                if header == prefix:
                    current = ""
                elif header.startswith(prefix + "\\"):
                    current = header[len(prefix) + 1:].replace("\\", "/")
                else:
                    current = header.lstrip("\\").replace("\\", "/")
                continue
            m = _LS_ENTRY.match(line)
            if not m:
                continue
            name, attrs, size = m.group(1).rstrip(), m.group(2), int(m.group(3))
            if name in (".", "..") or "D" in attrs:
                continue
            rel = f"{current}/{name}" if current else name
            files[rel] = size
        return files

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
        local_map = {}   # rel -> (abs_local, size)
        unsafe = []
        for item in sorted(Path(local_dir).rglob("*")):
            if not item.is_file():
                continue
            rel_parts = item.relative_to(local_dir).parts
            if not all(self._safe(p) for p in rel_parts):
                unsafe.append("/".join(rel_parts))
                continue
            local_map["/".join(rel_parts)] = (str(item), item.stat().st_size)

        # --- 2. index remote files, then diff ---
        remote_map = self._remote_file_map(base)
        to_upload = [r for r in local_map
                     if r not in remote_map or remote_map[r] != local_map[r][1]]
        unchanged = [r for r in local_map
                     if r in remote_map and remote_map[r] == local_map[r][1]]
        to_delete = sorted(r for r in remote_map if r not in local_map) if mirror else []
        to_upload.sort()

        if dry_run:
            self._report(f"  [dry-run] {len(to_upload)} to upload, "
                         f"{len(unchanged)} unchanged, {len(to_delete)} to delete")
            for r in to_upload:
                self._report(f"    ↑ {r}")
            for r in to_delete:
                self._report(f"    ✗ {r} (removed locally)")
            for name in unsafe:
                self._report(f"    ! skipped (unsupported name): {name}")
            return {"transferred": len(to_upload), "skipped": len(unchanged),
                    "deleted": len(to_delete), "errors": 0}

        if not to_upload and not to_delete:
            self._report(f"  Already up to date ({len(unchanged)} file(s)).")
            return {"transferred": 0, "skipped": len(unchanged), "deleted": 0, "errors": 0}

        commands = self._build_push_commands(base, local_map, to_upload, to_delete)

        total = len(to_upload)
        done = 0

        def on_line(line):
            nonlocal done
            if line.startswith("putting file"):
                done += 1
                name = line[len("putting file"):].strip().split(" as ")[0].strip()
                self._progress(done, total, os.path.basename(name), "Uploading")

        _rc, output = self._stream_session(commands, on_line)
        fatal = self._fatal_error(output)
        if fatal:
            raise Exception(fatal)

        errors = [ln for ln in output.splitlines()
                  if "NT_STATUS" in ln and "OBJECT_NAME_COLLISION" not in ln
                  and "NO_SUCH_FILE" not in ln]
        transferred = len(to_upload) - sum(1 for ln in errors if "deleting" not in ln)
        deleted = len(to_delete) - sum(1 for ln in errors if "deleting" in ln)
        self._report(f"  Uploaded {transferred} · unchanged {len(unchanged)} · "
                     f"deleted {deleted}")
        for name in unsafe:
            self._report(f"  ! skipped (unsupported name): {name}")
        for ln in errors:
            self._report(f"  ! {ln.strip()}")
        return {"transferred": transferred, "skipped": len(unchanged),
                "deleted": deleted, "errors": len(errors)}

    def _build_push_commands(self, base, local_map, to_upload, to_delete) -> list:
        """Build one smbclient script: mkdir needed dirs, put changed files,
        then delete files that no longer exist locally."""
        commands = ["prompt OFF"]

        base_parts = tuple(p for p in base.split("\\") if p)
        needed_dirs = set()
        for depth in range(1, len(base_parts) + 1):
            needed_dirs.add(base_parts[:depth])
        for rel in to_upload:
            parts = rel.split("/")
            for depth in range(1, len(parts)):
                needed_dirs.add(base_parts + tuple(parts[:depth]))
        for parts in sorted(needed_dirs, key=len):
            commands.append(f'mkdir "{chr(92).join(parts)}"')

        # uploads run from the share root, using full backslash paths
        for rel in to_upload:
            abs_local = local_map[rel][0]
            remote_path = "\\".join(base_parts + tuple(rel.split("/")))
            commands.append(f'put "{abs_local}" "{remote_path}"')

        # deletes: cd into each directory (absolute), then del the files there
        by_dir = {}
        for rel in to_delete:
            parts = rel.split("/")
            parent = "\\".join(base_parts + tuple(parts[:-1]))
            by_dir.setdefault(parent, []).append(parts[-1])
        for parent, names in by_dir.items():
            commands.append(f'cd "\\{parent}"')
            for n in names:
                commands.append(f'del "{n}"')

        commands.append("quit")
        return commands

    # ------------------------------------------------------------------ #
    # Pull  (server -> local)
    # ------------------------------------------------------------------ #
    def _pull_directory(self, local_dir: str, remote_dir: str,
                        dry_run: bool = False) -> dict:
        local_dir = os.path.abspath(os.path.expanduser(local_dir))
        base = self._remote(remote_dir)

        if dry_run:
            listing = self._run_session([f'cd "{base}"', "recurse ON", "ls", "quit"])
            output = self._combined_output(listing)
            fatal = self._fatal_error(output)
            if fatal:
                raise Exception(fatal)
            self._report(f"  [dry-run] would restore from \\{base} into {local_dir}")
            return {"transferred": 0, "skipped": 0, "errors": 0}

        os.makedirs(local_dir, exist_ok=True)
        commands = [
            f'lcd "{local_dir}"',
            f'cd "{base}"',
            "prompt OFF",
            "recurse ON",
            "mget *",
            "quit",
        ]

        done = 0

        def on_line(line):
            nonlocal done
            # smbclient prints "getting file \path\name of size N as localname (...)"
            if line.startswith("getting file"):
                done += 1
                name = line.split(" as ", 1)[-1].split(" (")[0].strip()
                self._progress(done, None, os.path.basename(name), "Downloading")

        _rc, output = self._stream_session(commands, on_line)
        fatal = self._fatal_error(output)
        if fatal:
            raise Exception(fatal)

        transferred = done
        errors = [
            line for line in output.splitlines()
            if "NT_STATUS" in line and "NO_SUCH_FILE" not in line
        ]
        self._report(f"  Downloaded {transferred} file(s)")
        for line in errors:
            self._report(f"  ! {line.strip()}")
        return {"transferred": transferred, "skipped": 0, "errors": len(errors)}
