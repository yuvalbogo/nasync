import os
import subprocess
from pathlib import Path


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
        (returncode, combined_output)."""
        script = "\n".join(commands) + "\n"
        env = os.environ.copy()
        env["PASSWD"] = self.password
        try:
            proc = subprocess.Popen(
                ["smbclient", self.url, "-U", self.username, "-p", str(self.port)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
        except FileNotFoundError:
            raise Exception(
                "smbclient not found. Install samba-client:\n"
                "  Ubuntu/Debian: sudo apt install smbclient\n"
                "  Fedora/RHEL:   sudo dnf install samba-client\n"
                "  macOS:         brew install samba"
            )
        collected = []
        proc.stdin.write(script)
        proc.stdin.close()
        for line in proc.stdout:
            line = line.rstrip("\n")
            collected.append(line)
            on_line(line)
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

    # ------------------------------------------------------------------ #
    # Public sync entrypoint
    # ------------------------------------------------------------------ #
    def sync_directory(self, local_dir: str, remote_dir: str,
                       direction: str = "push", dry_run: bool = False) -> dict:
        if direction == "push":
            return self._push_directory(local_dir, remote_dir, dry_run)
        elif direction == "pull":
            return self._pull_directory(local_dir, remote_dir, dry_run)
        raise ValueError(f"Unknown direction: {direction}")

    # ------------------------------------------------------------------ #
    # Push  (local -> server)
    # ------------------------------------------------------------------ #
    def _push_directory(self, local_dir: str, remote_dir: str,
                        dry_run: bool = False) -> dict:
        local_dir = os.path.abspath(os.path.expanduser(local_dir))
        if not os.path.isdir(local_dir):
            raise Exception(f"Local directory not found: {local_dir}")

        base = self._remote(remote_dir)
        files = []       # (abs_local, remote_backslash_path)
        dirs = set()     # remote directories that must exist (as part tuples)
        skipped = []

        for item in sorted(Path(local_dir).rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(local_dir)
            if not all(self._safe(p) for p in rel.parts):
                skipped.append(str(rel))
                continue
            remote_path = base + "\\" + "\\".join(rel.parts) if base else "\\".join(rel.parts)
            files.append((str(item), remote_path))
            # every ancestor directory of this file must exist on the server
            for depth in range(1, len(rel.parts)):
                dirs.add(tuple(rel.parts[:depth]))

        if dry_run:
            self._report(f"  [dry-run] would upload {len(files)} file(s)")
            for _, remote_path in files:
                self._report(f"    + {remote_path}")
            for name in skipped:
                self._report(f"    ! skipped (unsupported name): {name}")
            return {"transferred": len(files), "skipped": len(skipped), "errors": 0}

        if not files:
            self._report("  (nothing to upload)")
            return {"transferred": 0, "skipped": len(skipped), "errors": 0}

        commands = ["prompt OFF"]
        # create base dir (each component) then per-file subdirectories,
        # shallowest first so parents always exist before children.
        base_parts = tuple(p for p in base.split("\\") if p)
        all_dirs = set()
        for depth in range(1, len(base_parts) + 1):
            all_dirs.add(base_parts[:depth])
        for d in dirs:
            all_dirs.add(base_parts + d)
        for parts in sorted(all_dirs, key=len):
            commands.append(f'mkdir "{chr(92).join(parts)}"')
        for local_path, remote_path in files:
            commands.append(f'put "{local_path}" "{remote_path}"')
        commands.append("quit")

        total = len(files)
        done = 0

        def on_line(line):
            nonlocal done
            # smbclient prints "putting file <local> as <remote> (speed)" per file
            if line.startswith("putting file"):
                done += 1
                name = line[len("putting file"):].strip().split(" as ")[0].strip()
                self._progress(done, total, os.path.basename(name), "Uploading")

        returncode, output = self._stream_session(commands, on_line)
        fatal = self._fatal_error(output)
        if fatal:
            raise Exception(fatal)

        # Count real transfer failures (ignore benign "directory exists").
        errors = [
            line for line in output.splitlines()
            if "NT_STATUS" in line and "OBJECT_NAME_COLLISION" not in line
        ]
        transferred = len(files) - len(errors)
        self._report(f"  Uploaded {transferred}/{len(files)} file(s)")
        for name in skipped:
            self._report(f"  ! skipped (unsupported name): {name}")
        for line in errors:
            self._report(f"  ! {line.strip()}")
        return {"transferred": transferred, "skipped": len(skipped), "errors": len(errors)}

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

        returncode, output = self._stream_session(commands, on_line)
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
