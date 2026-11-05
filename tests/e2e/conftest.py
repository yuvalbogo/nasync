"""The end-to-end suite: the whole app against throwaway Samba servers.

The data-safety lab (tests/lab) drives the sync engine directly. This suite
does what a person does — runs `nasync` commands, leaves the background
automation running, presses buttons in the real window — in an isolated test
profile, and checks the outcome on the server's own disk.

    pytest --e2e tests/e2e          # or: make e2e

Needs podman (or docker). The window tests also need a graphical session and
are skipped without one. Nothing here can reach a real NAS or the real
config: servers are containers on 127.0.0.1, the profile is a temporary
directory, and passwords never touch the keyring.
"""

import os
import shutil
import socket
import subprocess
import sys
import time
import uuid

import pytest

from lab.conftest import IMAGE, PASSWORD, SHARE, USER, Lab
from lab.conftest import samba  # noqa: F401  (the fixture, shared)

HERE = os.path.dirname(os.path.abspath(__file__))


def pytest_collection_modifyitems(config, items):
    if config.getoption("--e2e") or os.environ.get("NASYNC_E2E"):
        return
    skip = pytest.mark.skip(reason="end-to-end suite: run with --e2e")
    for item in items:
        if "tests/e2e/" in str(item.fspath).replace(os.sep, "/"):
            item.add_marker(skip)


class World(Lab):
    """A lab world, plus the `nasync` command run as a person would run it."""

    def nasync(self, *args, timeout=300, check=None, stdin=None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, "run_nasync.py"), *args],
            env=env, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL if stdin is None else None, input=stdin)
        self.last_output = result.stdout + result.stderr
        if check is not None:
            assert result.returncode == check, (
                f"nasync {' '.join(args)} exited {result.returncode}, "
                f"expected {check}:\n{self.last_output}")
        return result

    def start_nasync(self, *args) -> subprocess.Popen:
        env = dict(os.environ)
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        return subprocess.Popen(
            [sys.executable, os.path.join(HERE, "run_nasync.py"), *args],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True)

    def server_path(self, pair: str, rel: str = "") -> str:
        self.config.refresh()          # a `nasync` command may have changed it
        return super().server_path(pair, rel)

    def many_files(self, pair: str, count: int) -> None:
        """Lots of small files: a run long enough to catch, without big data."""
        folder = self.local(pair)
        os.makedirs(folder, exist_ok=True)
        for i in range(count):
            with open(os.path.join(folder, f"n{i:05d}.txt"), "w") as handle:
                handle.write(f"note {i}\n")

    def samba_files(self, box: dict, path: str) -> list:
        """Files under /share/`path` on another container, sorted."""
        out = subprocess.run(
            [box["engine"], "exec", box["container"], "sh", "-c",
             f"cd '/share/{path}' 2>/dev/null && find . -type f"],
            capture_output=True, text=True).stdout
        return sorted(line[2:] for line in out.splitlines())

    def status(self):
        from nasync.core import activity
        return activity.read_status(self.config.config_dir)

    def wait_for(self, predicate, timeout=60, interval=0.1, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        raise AssertionError(f"timed out waiting for {what}")

    def big_files(self, pair: str, count: int, size: int) -> None:
        folder = self.local(pair)
        os.makedirs(folder, exist_ok=True)
        for i in range(count):
            with open(os.path.join(folder, f"big{i:03d}.bin"), "wb") as handle:
                handle.write(os.urandom(size))


@pytest.fixture
def world(samba, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    w = World(samba, tmp_path, monkeypatch)
    w.config.set_notifications(False)
    yield w
    w.server_exec(f"rm -rf '/share/{w.root}'")


@pytest.fixture
def make_samba():
    """Start extra Samba servers: ``make_samba(hostname=..., globals_=[...])``."""
    started = []
    engine = next((e for e in ("podman", "docker") if shutil.which(e)), None)
    if engine is None:
        pytest.skip("needs podman or docker")

    def start(hostname="nas", globals_=()):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        name = f"nasync-e2e-{uuid.uuid4().hex[:8]}"
        cmd = [engine, "run", "-d", "--rm", "--name", name, "--hostname", hostname,
               "-p", f"{port}:445", IMAGE, "-u", f"{USER};{PASSWORD}",
               "-s", f"{SHARE};/share;yes;no;no;{USER}", "-p"]
        for option in globals_:
            cmd += ["-g", option]
        subprocess.run(cmd, check=True, capture_output=True)
        started.append(name)
        import smbclient
        deadline = time.monotonic() + 60
        while True:
            try:
                smbclient.register_session("127.0.0.1", username=USER,
                                           password=PASSWORD, port=port)
                smbclient.listdir(f"\\\\127.0.0.1\\{SHARE}", port=port)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        subprocess.run([engine, "exec", name, "sh", "-c", "chmod 777 /share"],
                       capture_output=True)
        return {"engine": engine, "container": name, "port": port}

    yield start
    for name in started:
        subprocess.run([engine, "rm", "-f", name], capture_output=True)


def gtk_or_skip():
    """The GTK main loop, or a skip when there's no graphical session."""
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("the window tests need a graphical session")
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, GLib  # noqa: F401
    except (ImportError, ValueError):
        pytest.skip("GTK 4 / libadwaita not available")
    return GLib


def pump(predicate, timeout=60, what="the window"):
    """Run the GTK main loop until `predicate()` holds."""
    GLib = gtk_or_skip()
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")
