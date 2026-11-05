"""The data-safety lab: NASync against a real, throwaway Samba server.

Unit tests drive the engine through a fake SMB layer, and a fake can only
fail in the ways its author imagined — the size-only fake is why a size-only
comparison went unnoticed for months. Everything here runs the real code
against a real Samba server in a container, from an isolated config, and
checks the outcome on the server's own disk: names, bytes and times.

    pytest --lab tests/lab          # or: make lab

Nothing here can reach a real NAS: the server is 127.0.0.1 on a random
port, the config directory is a temporary one, and the session bus is
hidden so the real keyring is never touched.
"""

import hashlib
import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest

IMAGE = "docker.io/dperson/samba"
USER, PASSWORD, SHARE = "labuser", "labpass", "backup"


def _engine():
    for name in ("podman", "docker"):
        if shutil.which(name):
            return name
    return None


def pytest_collection_modifyitems(config, items):
    if config.getoption("--lab") or os.environ.get("NASYNC_LAB"):
        return
    skip = pytest.mark.skip(reason="data-safety lab: run with --lab")
    for item in items:
        if "tests/lab/" in str(item.fspath).replace(os.sep, "/"):
            item.add_marker(skip)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def samba():
    """One Samba container for the whole lab session."""
    engine = _engine()
    if engine is None:
        pytest.skip("the lab needs podman or docker")
    port = _free_port()
    name = f"nasync-lab-{uuid.uuid4().hex[:8]}"
    started = subprocess.run(
        [engine, "run", "-d", "--rm", "--name", name, "-p", f"{port}:445", IMAGE,
         "-u", f"{USER};{PASSWORD}", "-s", f"{SHARE};/share;yes;no;no;{USER}",
         "-p"], capture_output=True, text=True)
    if started.returncode != 0:
        pytest.skip(f"could not start the Samba container: {started.stderr.strip()}")
    try:
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
        yield {"engine": engine, "container": name, "port": port}
    finally:
        subprocess.run([engine, "rm", "-f", name], capture_output=True)


class Lab:
    """One test's world: an isolated config, a local tree, a server folder."""

    def __init__(self, samba, tmp_path, monkeypatch):
        self.samba = samba
        self.root = f"lab-{uuid.uuid4().hex[:8]}"
        self.home = tmp_path
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        from nasync.system import vault
        monkeypatch.setattr(vault, "backend", lambda: "")
        from nasync.core.config import Config
        self.config = Config()
        self.config.add_or_update_server("lab", "127.0.0.1", USER, PASSWORD, SHARE,
                                         samba["port"], root=self.root)
        self.config.set_active_server("lab")
        self.config.set_sync_mode("mirror", server="lab")
        self.log = []
        self.server_exec(f"mkdir -p '/share/{self.root}' && chmod 777 '/share/{self.root}'")

    # -- local side ------------------------------------------------------- #
    def local(self, pair: str) -> str:
        return str(self.home / "src" / pair)

    def pair(self, name: str, files: dict = None, dest: str = None) -> str:
        folder = self.local(name)
        os.makedirs(folder, exist_ok=True)
        self.config.add_pair(folder, dest or name, name, server="lab")
        for rel, content in (files or {}).items():
            self.write(name, rel, content)
        return folder

    def write(self, pair: str, rel: str, content, mtime: float = None) -> str:
        path = os.path.join(self.local(pair), *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content if isinstance(content, bytes) else content.encode())
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def local_tree(self, pair: str) -> dict:
        """{rel: sha256} of every regular file under the pair's folder."""
        out = {}
        base = self.local(pair)
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                path = os.path.join(dirpath, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                rel = os.path.relpath(path, base).replace(os.sep, "/")
                with open(path, "rb") as handle:
                    out[rel] = hashlib.sha256(handle.read()).hexdigest()
        return out

    # -- server side ------------------------------------------------------- #
    def server_exec(self, script: str) -> str:
        result = subprocess.run(
            [self.samba["engine"], "exec", self.samba["container"], "sh", "-c", script],
            capture_output=True, text=True)
        return result.stdout

    def server_path(self, pair: str, rel: str = "") -> str:
        dest = self.config.resolve_remote(self.config.get_pair(pair, "lab")["server"], "lab")
        return f"/share/{dest}" + (f"/{rel}" if rel else "")

    def server_tree(self, pair: str) -> dict:
        """{rel: sha256} of every file in the pair's server folder."""
        base = self.server_path(pair)
        out = self.server_exec(
            f"cd '{base}' 2>/dev/null && find . -type f -exec sha256sum {{}} +")
        tree = {}
        for line in out.splitlines():
            digest, path = line.split(None, 1)
            tree[path[2:] if path.startswith("./") else path] = digest
        return tree

    def server_dirs(self, pair: str) -> set:
        base = self.server_path(pair)
        out = self.server_exec(f"cd '{base}' 2>/dev/null && find . -mindepth 1 -type d")
        return {line[2:] for line in out.splitlines()}

    def server_mtime(self, pair: str, rel: str) -> int:
        out = self.server_exec(f"stat -c %Y '{self.server_path(pair, rel)}'")
        return int(out.strip())

    def server_write(self, pair: str, rel: str, content: str, mtime: int = None) -> None:
        """Another device changing the NAS behind NASync's back."""
        path = self.server_path(pair, rel)
        script = (f"mkdir -p \"$(dirname '{path}')\" && printf %s '{content}' > '{path}'"
                  f" && chmod 666 '{path}'")
        if mtime is not None:
            script += f" && touch -d @{int(mtime)} '{path}'"
        self.server_exec(script)

    def server_remove(self, pair: str, rel: str) -> None:
        self.server_exec(f"rm -f '{self.server_path(pair, rel)}'")

    # -- NASync --------------------------------------------------------------- #
    def sync(self, pair: str = None, **kwargs) -> dict:
        from nasync.core.sync import Syncer
        self.log = []
        return Syncer(reporter=self.log.append, config=self.config,
                      server="lab", source="cli").sync_pair(pair, **kwargs)

    def restore(self, pair: str = None) -> dict:
        from nasync.core.sync import Syncer
        self.log = []
        return Syncer(reporter=self.log.append, config=self.config,
                      server="lab", source="cli").restore_pair(pair)

    def assert_mirrored(self, pair: str) -> None:
        local, server = self.local_tree(pair), self.server_tree(pair)
        missing = sorted(set(local) - set(server))
        extra = sorted(set(server) - set(local))
        stale = sorted(r for r in set(local) & set(server) if local[r] != server[r])
        assert not (missing or extra or stale), (
            f"server differs from local — missing {missing}, extra {extra}, "
            f"stale {stale}\nlog:\n" + "\n".join(self.log))


@pytest.fixture
def lab(samba, tmp_path, monkeypatch):
    world = Lab(samba, tmp_path, monkeypatch)
    yield world
    world.server_exec(f"rm -rf '/share/{world.root}'")
