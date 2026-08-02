import os
import tempfile
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


def test_push_dry_run_discovers_files():
    client = SAMBAClient("1.2.3.4", "user", "pass", "share")
    with tempfile.TemporaryDirectory() as tmpdir:
        # nested tree, including a hidden file and a nested subdir
        Path(tmpdir, "a.txt").write_text("a")
        os.makedirs(os.path.join(tmpdir, "sub", "deep"))
        Path(tmpdir, "sub", "b.txt").write_text("b")
        Path(tmpdir, "sub", "deep", "c.txt").write_text("c")
        Path(tmpdir, ".hidden").write_text("h")

        stats = client.sync_directory(tmpdir, "/remote/base",
                                      direction="push", dry_run=True)
        # 4 real files including the dotfile and the nested ones
        assert stats["transferred"] == 4, stats
        assert stats["errors"] == 0, stats
        print("✅ test_push_dry_run_discovers_files passed")


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
    test_push_dry_run_discovers_files()
    test_push_missing_local_dir_raises()
