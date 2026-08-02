import os
import tempfile
import types
from pathlib import Path
from nasync.samba import SAMBAClient


def test_remote_path_normalisation():
    assert SAMBAClient._remote("/backups/docs") == "backups\\docs"
    assert SAMBAClient._remote("backups/docs/") == "backups\\docs"
    assert SAMBAClient._remote("/a/b/c") == "a\\b\\c"
    print("✅ test_remote_path_normalisation passed")


def test_safe_names():
    assert SAMBAClient._safe("normal.txt")
    assert not SAMBAClient._safe('has"quote.txt')
    assert not SAMBAClient._safe("has\nnewline.txt")
    print("✅ test_safe_names passed")


def test_fatal_error_detection():
    assert "Authentication" in SAMBAClient._fatal_error("session setup failed: NT_STATUS_LOGON_FAILURE")
    assert SAMBAClient._fatal_error("putting file foo.txt") == ""
    print("✅ test_fatal_error_detection passed")


# Real `smbclient ls` recursive output (captured from a live server).
_LS_OUTPUT = """\
  .                                   D        0  Mon Aug  3 00:16:47 2026
  ..                                  D        0  Mon Aug  3 00:16:47 2026
  sub                                 D        0  Mon Aug  3 00:16:47 2026
  root.txt                            N        5  Mon Aug  3 00:16:47 2026
  .hidden                             H        2  Mon Aug  3 00:16:47 2026

\\base\\sub
  .                                   D        0  Mon Aug  3 00:16:47 2026
  ..                                  D        0  Mon Aug  3 00:16:47 2026
  deep                                D        0  Mon Aug  3 00:16:47 2026
  mid.txt                             N        3  Mon Aug  3 00:16:47 2026

\\base\\sub\\deep
  .                                   D        0  Mon Aug  3 00:16:47 2026
  ..                                  D        0  Mon Aug  3 00:16:47 2026
  leaf.txt                            N        7  Mon Aug  3 00:16:47 2026

\t\t95590400 blocks of size 1024. 41094512 blocks available
"""


def test_remote_file_map_parsing():
    client = SAMBAClient("1.2.3.4", "user", "pass", "share")
    client._run_session = lambda cmds: types.SimpleNamespace(
        stdout=_LS_OUTPUT, stderr="", returncode=0)
    m = client._remote_file_map("base")
    assert m == {"root.txt": 5, ".hidden": 2,
                 "sub/mid.txt": 3, "sub/deep/leaf.txt": 7}, m
    print("✅ test_remote_file_map_parsing passed")


def test_push_dry_run_discovers_files():
    client = SAMBAClient("1.2.3.4", "user", "pass", "share")
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
    client = SAMBAClient("1.2.3.4", "user", "pass", "share")
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "a.txt").write_text("aaaa")   # 4 bytes, unchanged remotely
        Path(tmpdir, "b.txt").write_text("bb")     # new
        # remote already has a.txt (same size) + a stale file no longer local
        client._remote_file_map = lambda base: {"a.txt": 4, "stale.txt": 9}

        stats = client.sync_directory(tmpdir, "/mir", direction="push", dry_run=True)
        assert stats["transferred"] == 1, stats   # only b.txt
        assert stats["skipped"] == 1, stats        # a.txt unchanged
        assert stats["deleted"] == 1, stats        # stale.txt removed
        print("✅ test_push_incremental_plan passed")


def test_push_missing_local_dir_raises():
    client = SAMBAClient("1.2.3.4", "user", "pass", "share")
    try:
        client.sync_directory("/no/such/dir/xyz", "/remote", direction="push", dry_run=True)
    except Exception as e:
        assert "not found" in str(e)
        print("✅ test_push_missing_local_dir_raises passed")
        return
    raise AssertionError("Expected an exception for a missing local directory")


if __name__ == "__main__":
    test_remote_path_normalisation()
    test_safe_names()
    test_fatal_error_detection()
    test_remote_file_map_parsing()
    test_push_dry_run_discovers_files()
    test_push_incremental_plan()
    test_push_missing_local_dir_raises()
