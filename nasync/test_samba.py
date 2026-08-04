import io
import os
import socket
import tempfile
import threading
from pathlib import Path

import nasync.samba as samba_module
from nasync.samba import SAMBAClient, SyncCancelled
from smbprotocol.exceptions import AccessDenied, BadNetworkName, LogonFailure


# --------------------------------------------------------------------------- #
# Fake SMB layer — lets us exercise the real logic without touching a network.
# --------------------------------------------------------------------------- #
class _FakeSMB:
    """Stands in for the `smbclient` module inside nasync.samba."""

    def __init__(self, tree=None, sizes=None):
        self._tree = tree or {}      # dirpath -> (dirnames, filenames)
        self._sizes = sizes or {}    # full UNC path -> size
        self.removed = []
        self.made_dirs = []
        self.written = {}

    def walk(self, root, **kwargs):
        if root not in self._tree:
            raise samba_module.ObjectPathNotFound()
        stack = [root]
        while stack:
            current = stack.pop(0)
            dirnames, filenames = self._tree[current]
            yield current, list(dirnames), list(filenames)
            stack.extend(f"{current}\\{d}" for d in dirnames)

    def stat(self, path, **kwargs):
        if path not in self._sizes:
            raise samba_module.ObjectNameNotFound()
        return type("S", (), {"st_size": self._sizes[path]})()

    def makedirs(self, path, exist_ok=False, **kwargs):
        self.made_dirs.append(path)

    def remove(self, path, **kwargs):
        self.removed.append(path)

    def open_file(self, path, mode="rb", **kwargs):
        if "w" in mode:
            buf = io.BytesIO()
            self.written[path] = buf
            buf.close = lambda: None       # keep contents readable after close
            return _CM(buf)
        return _CM(io.BytesIO(b""))


class _CM:
    """Minimal context manager wrapper around a stream."""

    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        return self.stream

    def __exit__(self, *a):
        return False


def _client(fake, **kwargs):
    """Build a client wired to a fake SMB layer."""
    samba_module.smbclient = fake
    return SAMBAClient("1.2.3.4", "user", "pass", "share", **kwargs)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def test_remote_path_normalisation():
    assert SAMBAClient._remote("/backups/docs") == "backups\\docs"
    assert SAMBAClient._remote("backups/docs/") == "backups\\docs"
    assert SAMBAClient._remote("/a/b/c") == "a\\b\\c"
    print("✅ test_remote_path_normalisation passed")


def test_unc_construction():
    client = SAMBAClient("1.2.3.4", "user", "pass", "share")
    assert client._unc() == "\\\\1.2.3.4\\share"
    assert client._unc("base") == "\\\\1.2.3.4\\share\\base"
    assert client._unc("base", "sub\\f.txt") == "\\\\1.2.3.4\\share\\base\\sub\\f.txt"
    print("✅ test_unc_construction passed")


# --------------------------------------------------------------------------- #
# Errors — now mapped from typed exceptions rather than scraped strings
# --------------------------------------------------------------------------- #
def test_friendly_error_mapping():
    assert "Authentication" in SAMBAClient._friendly_error(LogonFailure())
    assert "Access denied" in SAMBAClient._friendly_error(AccessDenied())
    assert "Share not found" in SAMBAClient._friendly_error(BadNetworkName())
    assert "refused" in SAMBAClient._friendly_error(ConnectionRefusedError())
    assert "unreachable" in SAMBAClient._friendly_error(socket.gaierror())
    print("✅ test_friendly_error_mapping passed")


# --------------------------------------------------------------------------- #
# Remote indexing — walks the tree instead of regex-parsing terminal output
# --------------------------------------------------------------------------- #
def test_remote_file_map_walks_tree():
    root = "\\\\1.2.3.4\\share\\base"
    fake = _FakeSMB(
        tree={
            root: (["sub"], ["root.txt", ".hidden"]),
            f"{root}\\sub": (["deep"], ["mid.txt"]),
            f"{root}\\sub\\deep": ([], ["leaf.txt"]),
        },
        sizes={
            f"{root}\\root.txt": 5,
            f"{root}\\.hidden": 2,
            f"{root}\\sub\\mid.txt": 3,
            f"{root}\\sub\\deep\\leaf.txt": 7,
        },
    )
    m = _client(fake)._remote_file_map("base")
    assert m == {"root.txt": 5, ".hidden": 2,
                 "sub/mid.txt": 3, "sub/deep/leaf.txt": 7}, m
    print("✅ test_remote_file_map_walks_tree passed")


def test_remote_file_map_missing_dir_is_empty():
    """A destination that doesn't exist yet reads as empty, not an error."""
    assert _client(_FakeSMB())._remote_file_map("nope") == {}
    print("✅ test_remote_file_map_missing_dir_is_empty passed")


# --------------------------------------------------------------------------- #
# Filename legality — verified against a real Samba server, not assumed
# --------------------------------------------------------------------------- #
def test_storable_rejects_smb_illegal_characters():
    """SMB refuses < > : " | ? * — a protocol limit, not a client one."""
    for bad in ['q"uote.txt', "star*.txt", "q?.txt", "pipe|.txt",
                "lt<.txt", "gt>.txt", "colon:.txt", "sub/bad*.txt"]:
        assert not SAMBAClient._storable(bad), bad
    for good in ["plain.txt", "has'apostrophe.txt", "with space.txt",
                 "ünïcode-ß.txt", "sub/deep/nested.txt", "dots.tar.gz"]:
        assert SAMBAClient._storable(good), good
    print("✅ test_storable_rejects_smb_illegal_characters passed")


def test_colon_names_are_skipped_not_silently_truncated():
    """Samba turns 'note:x.txt' into 'note' and loses the contents — so a
    colon has to be caught here rather than shipped to the server."""
    client = _client(_FakeSMB())
    client._remote_file_map = lambda base: {}
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "safe.txt").write_text("ok")
        Path(tmpdir, "note:stream.txt").write_text("would be lost")

        stats = client.sync_directory(tmpdir, "/base", direction="push", dry_run=True)
        assert stats["transferred"] == 1, stats      # only safe.txt
        assert stats["unsupported"] == 1, stats      # colon file reported, not sent
        print("✅ test_colon_names_are_skipped_not_silently_truncated passed")


def test_legal_awkward_filenames_still_sync():
    """Apostrophes, spaces and unicode are legal and must not be filtered."""
    client = _client(_FakeSMB())
    client._remote_file_map = lambda base: {}
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "has'apostrophe.txt").write_text("a")
        Path(tmpdir, "with space.txt").write_text("s")
        Path(tmpdir, "ünïcode.txt").write_text("u")

        stats = client.sync_directory(tmpdir, "/base", direction="push", dry_run=True)
        assert stats["transferred"] == 3, stats
        assert stats["unsupported"] == 0, stats
        print("✅ test_legal_awkward_filenames_still_sync passed")


# --------------------------------------------------------------------------- #
# Sync planning — transport-independent, unchanged from the previous backend
# --------------------------------------------------------------------------- #
def test_push_dry_run_discovers_files():
    client = _client(_FakeSMB())
    client._remote_file_map = lambda base: {}   # empty remote -> all new
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "a.txt").write_text("a")
        os.makedirs(os.path.join(tmpdir, "sub", "deep"))
        Path(tmpdir, "sub", "b.txt").write_text("b")
        Path(tmpdir, "sub", "deep", "c.txt").write_text("c")
        Path(tmpdir, ".hidden").write_text("h")

        stats = client.sync_directory(tmpdir, "/remote/base",
                                      direction="push", dry_run=True)
        assert stats["transferred"] == 4, stats
        assert stats["deleted"] == 0 and stats["errors"] == 0, stats
        print("✅ test_push_dry_run_discovers_files passed")


def test_push_incremental_plan():
    """Only changed/new files upload; unchanged skip; server-only files delete."""
    client = _client(_FakeSMB())
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "a.txt").write_text("aaaa")   # 4 bytes, unchanged remotely
        Path(tmpdir, "b.txt").write_text("bb")     # new
        client._remote_file_map = lambda base: {"a.txt": 4, "stale.txt": 9}

        stats = client.sync_directory(tmpdir, "/mir", direction="push", dry_run=True)
        assert stats["transferred"] == 1, stats   # only b.txt
        assert stats["skipped"] == 1, stats        # a.txt unchanged
        assert stats["deleted"] == 1, stats        # stale.txt removed
        print("✅ test_push_incremental_plan passed")


def test_push_missing_local_dir_raises():
    client = _client(_FakeSMB())
    try:
        client.sync_directory("/no/such/dir/xyz", "/remote", direction="push", dry_run=True)
    except Exception as e:
        assert "not found" in str(e)
        print("✅ test_push_missing_local_dir_raises passed")
        return
    raise AssertionError("Expected an exception for a missing local directory")


def test_push_uploads_and_mirrors_deletes():
    """A real (faked) push writes changed files and removes server-only ones."""
    fake = _FakeSMB()
    client = _client(fake)
    client._remote_file_map = lambda base: {"stale.txt": 9}
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "new.txt").write_text("hello")

        stats = client.sync_directory(tmpdir, "/base", direction="push")
        assert stats["transferred"] == 1, stats
        assert stats["deleted"] == 1, stats
        assert stats["errors"] == 0, stats
        assert fake.removed == ["\\\\1.2.3.4\\share\\base\\stale.txt"], fake.removed
        written = fake.written["\\\\1.2.3.4\\share\\base\\new.txt"]
        assert written.getvalue() == b"hello"
        print("✅ test_push_uploads_and_mirrors_deletes passed")


# --------------------------------------------------------------------------- #
# Pull is incremental too
# --------------------------------------------------------------------------- #
def test_pull_skips_files_that_match_locally():
    client = _client(_FakeSMB())
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "same.txt").write_text("1234")     # 4 bytes, matches remote
        client._remote_file_map = lambda base: {"same.txt": 4, "missing.txt": 6}

        stats = client.sync_directory(tmpdir, "/base", direction="pull", dry_run=True)
        assert stats["skipped"] == 1, stats     # same.txt
        print("✅ test_pull_skips_files_that_match_locally passed")


# --------------------------------------------------------------------------- #
# Cancellation + per-byte progress — both new capabilities
# --------------------------------------------------------------------------- #
def test_cancel_event_aborts_transfer():
    cancel = threading.Event()
    cancel.set()
    client = _client(_FakeSMB(), cancel_event=cancel)
    try:
        client._copy_stream(io.BytesIO(b"x" * 10), io.BytesIO(), 10, "f", "Uploading")
    except SyncCancelled:
        print("✅ test_cancel_event_aborts_transfer passed")
        return
    raise AssertionError("Expected SyncCancelled when the cancel event is set")


def test_byte_progress_reports_incrementally():
    seen = []
    client = _client(
        _FakeSMB(),
        byte_progress=lambda done, total, name, verb: seen.append((done, total)),
    )
    payload = b"y" * (samba_module.CHUNK_SIZE * 2 + 5)
    client._copy_stream(io.BytesIO(payload), io.BytesIO(), len(payload), "big", "Uploading")

    assert len(seen) == 3, seen                      # two full chunks + remainder
    assert seen[-1] == (len(payload), len(payload)), seen
    assert seen[0][0] == samba_module.CHUNK_SIZE, seen
    print("✅ test_byte_progress_reports_incrementally passed")


def test_file_progress_signature_unchanged():
    """Front-ends written against the old 4-arg callback keep working."""
    seen = []
    client = _client(
        _FakeSMB(),
        progress=lambda done, total, name, verb: seen.append((done, total, name, verb)),
    )
    client._remote_file_map = lambda base: {}
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "one.txt").write_text("x")
        client.sync_directory(tmpdir, "/base", direction="push")

    assert seen == [(1, 1, "one.txt", "Uploading")], seen
    print("✅ test_file_progress_signature_unchanged passed")


if __name__ == "__main__":
    test_remote_path_normalisation()
    test_unc_construction()
    test_friendly_error_mapping()
    test_remote_file_map_walks_tree()
    test_remote_file_map_missing_dir_is_empty()
    test_storable_rejects_smb_illegal_characters()
    test_colon_names_are_skipped_not_silently_truncated()
    test_legal_awkward_filenames_still_sync()
    test_push_dry_run_discovers_files()
    test_push_incremental_plan()
    test_push_missing_local_dir_raises()
    test_push_uploads_and_mirrors_deletes()
    test_pull_skips_files_that_match_locally()
    test_cancel_event_aborts_transfer()
    test_byte_progress_reports_incrementally()
    test_file_progress_signature_unchanged()
