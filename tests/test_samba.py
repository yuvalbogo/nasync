import io
import os
import socket
import tempfile
import threading
from pathlib import Path

import nasync.core.samba as samba_module
from nasync.core.samba import SAMBAClient, SyncCancelled
from smbprotocol.exceptions import (
    AccessDenied, BadNetworkName, LogonFailure, SMBException, SMBOSError,
)
from smbprotocol.header import NtStatus


# --------------------------------------------------------------------------- #
# Fake SMB layer — lets us exercise the real logic without touching a network.
# --------------------------------------------------------------------------- #
class _FakeEntry:
    """Stands in for smbclient's DirEntry.

    The point of the real one — and of the indexing rewrite — is that name,
    type *and size* all arrive in the single directory listing, so this fake
    deliberately carries a size that never requires a `stat()` to read.
    """

    def __init__(self, path, size=0, is_dir=False, symlink=False):
        self.path = path
        self.name = path.rsplit("\\", 1)[-1]
        self.smb_info = type("Info", (), {"end_of_file": size})()
        self._is_dir = is_dir
        self._symlink = symlink

    def is_symlink(self):
        return self._symlink

    def is_dir(self, follow_symlinks=True):
        # Mirrors the real entry: a symlink is never itself a directory.
        return self._is_dir and not self._symlink


class _FakeSMB:
    """Stands in for the `smbclient` module inside nasync.samba."""

    def __init__(self, tree=None, sizes=None, symlinks=(), contents=None,
                 volume=None):
        self._tree = tree or {}      # dirpath -> (dirnames, filenames)
        self._sizes = sizes or {}    # full UNC path -> size
        self._symlinks = set(symlinks)   # full UNC paths that are reparse points
        self._contents = contents or {}  # full UNC path -> bytes, for hashing
        self._volume = volume        # (total, caller_available, actual_available)
        self.removed = []
        self.renamed = []
        self.removed_dirs = []
        self.stamped = {}            # UNC path -> (atime, mtime) set on it
        self.made_dirs = []
        self.written = {}
        self.scandir_calls = []
        self.stat_calls = []
        self.sessions = []           # every register_session, with its kwargs

    def scandir(self, path, **kwargs):
        self.scandir_calls.append(path)
        if path not in self._tree:
            # What a real server answers, wrapped the way smbclient wraps it —
            # an OSError carrying ENOENT, *not* a raw protocol exception.
            raise SMBOSError(NtStatus.STATUS_OBJECT_PATH_NOT_FOUND, path)
        dirnames, filenames = self._tree[path]
        entries = [_FakeEntry(f"{path}\\{d}", is_dir=True,
                              symlink=f"{path}\\{d}" in self._symlinks)
                   for d in dirnames]
        entries += [_FakeEntry(f"{path}\\{f}",
                               size=self._sizes.get(f"{path}\\{f}", 0),
                               symlink=f"{path}\\{f}" in self._symlinks)
                    for f in filenames]
        return _CM(iter(entries))

    def stat(self, path, **kwargs):
        self.stat_calls.append(path)
        if path not in self._sizes:
            raise samba_module.ObjectNameNotFound()
        size = self._sizes[path]
        # A directory in this fake is anything the tree knows as a directory.
        mode = 0o040000 if path in self._tree else 0o100000
        return type("S", (), {"st_size": size, "st_mode": mode})()

    def makedirs(self, path, exist_ok=False, **kwargs):
        self.made_dirs.append(path)

    def remove(self, path, **kwargs):
        self.removed.append(path)

    def rename(self, src, dst, **kwargs):
        self.renamed.append((src, dst))

    def rmdir(self, path, **kwargs):
        self.removed_dirs.append(path)

    def utime(self, path, times=None, **kwargs):
        self.stamped[path] = times

    def register_session(self, server, **kwargs):
        self.sessions.append({"server": server, **kwargs})

    def listdir(self, path, **kwargs):
        return []

    def stat_volume(self, path, **kwargs):
        if self._volume is None:
            raise Exception("volume information unavailable")
        from smbclient import SMBStatVolumeResult
        return SMBStatVolumeResult(*self._volume)

    def open_file(self, path, mode="rb", **kwargs):
        if "w" in mode:
            buf = io.BytesIO()
            self.written[path] = buf
            buf.close = lambda: None       # keep contents readable after close
            return _CM(buf)
        return _CM(io.BytesIO(self._contents.get(path, b"")))


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
# Browsing — one directory, for a picker rather than a sync
# --------------------------------------------------------------------------- #
def _browsable_tree():
    """A share root holding a mix of the things a picker has to render."""
    return _FakeSMB(
        tree={
            "\\\\1.2.3.4\\share": (["photos", "Archive"], ["readme.txt", "a.bin"]),
            "\\\\1.2.3.4\\share\\photos": (["2024"], ["cat.jpg"]),
        },
        sizes={
            "\\\\1.2.3.4\\share\\readme.txt": 120,
            "\\\\1.2.3.4\\share\\a.bin": 4096,
            "\\\\1.2.3.4\\share\\photos\\cat.jpg": 900,
        },
    )


def test_listing_puts_directories_first_then_sorts_case_insensitively():
    """A picker's order, not the server's: 'Archive' belongs beside 'photos',
    not before everything because of its capital A."""
    listing = _client(_browsable_tree()).list_directory()
    assert [e["name"] for e in listing] == \
        ["Archive", "photos", "a.bin", "readme.txt"]
    assert [e["is_dir"] for e in listing] == [True, True, False, False]


def test_listing_reads_one_directory_not_the_whole_tree():
    """The point of a browser over `_remote_file_map`: a big share must not be
    indexed before the first screen appears."""
    fake = _browsable_tree()
    _client(fake).list_directory()
    assert fake.scandir_calls == ["\\\\1.2.3.4\\share"]


def test_listing_returns_paths_a_sync_pair_can_store():
    """The `path` handed back goes straight into `Config.add_pair`, so it has
    to be the POSIX relative form, not a UNC path."""
    listing = _client(_browsable_tree()).list_directory("photos")
    assert {e["path"] for e in listing} == {"photos/2024", "photos/cat.jpg"}


def test_listing_carries_file_sizes_from_the_listing_itself():
    listing = _client(_browsable_tree()).list_directory()
    sizes = {e["name"]: e["size"] for e in listing}
    assert sizes["readme.txt"] == 120
    assert sizes["a.bin"] == 4096
    assert sizes["photos"] is None          # directories have no useful size


def test_a_symlinks_size_is_reported_as_unknown_rather_than_the_links_own():
    """A listing describes the link, not its target, so its recorded size is
    not the file's — reporting it would be a confident lie."""
    fake = _FakeSMB(
        tree={"\\\\1.2.3.4\\share": ([], ["link.txt"])},
        sizes={"\\\\1.2.3.4\\share\\link.txt": 7},
        symlinks=["\\\\1.2.3.4\\share\\link.txt"],
    )
    entry = _client(fake).list_directory()[0]
    assert entry["is_symlink"] is True
    assert entry["size"] is None


def test_browsing_a_missing_directory_says_so_instead_of_looking_empty():
    """Unlike indexing, where a missing path is the ordinary first-sync case."""
    client = _client(_browsable_tree())
    try:
        client.list_directory("nope")
    except Exception as e:
        assert "doesn't exist" in str(e)
    else:
        raise AssertionError("a missing directory listed as empty")


# --------------------------------------------------------------------------- #
# Storage insight
# --------------------------------------------------------------------------- #
def _usage_tree():
    """A share where the big thing is buried two levels down — the case a flat
    listing can't answer and a usage report exists for."""
    return _FakeSMB(
        tree={
            "\\\\1.2.3.4\\share": (["movies", "docs"], ["loose.txt"]),
            "\\\\1.2.3.4\\share\\movies": (["2024"], []),
            "\\\\1.2.3.4\\share\\movies\\2024": ([], ["big.mkv", "small.mkv"]),
            "\\\\1.2.3.4\\share\\docs": ([], ["a.txt", "b.txt"]),
        },
        sizes={
            "\\\\1.2.3.4\\share\\loose.txt": 50,
            "\\\\1.2.3.4\\share\\movies\\2024\\big.mkv": 9000,
            "\\\\1.2.3.4\\share\\movies\\2024\\small.mkv": 1000,
            "\\\\1.2.3.4\\share\\docs\\a.txt": 300,
            "\\\\1.2.3.4\\share\\docs\\b.txt": 200,
        },
    )


def test_usage_ranks_children_by_what_is_under_them_not_beside_them():
    """`movies` holds no files itself; all 10 KB is two levels down. Ranking by
    direct contents would put it last, which is the answer nobody wants."""
    usage = _client(_usage_tree()).usage_tree()
    assert [(c["name"], c["total"]) for c in usage["children"]] == [
        ("movies", 10000), ("docs", 500), ("loose.txt", 50)]
    assert usage["total"] == 10550
    assert usage["files"] == 5


def test_usage_marks_which_children_are_folders():
    usage = _client(_usage_tree()).usage_tree()
    kinds = {c["name"]: c["is_dir"] for c in usage["children"]}
    assert kinds == {"movies": True, "docs": True, "loose.txt": False}


def test_usage_drills_down_with_the_path_it_handed_back():
    """The child's `path` is what the next call takes — that is the drill-down."""
    client = _client(_usage_tree())
    child = client.usage_tree()["children"][0]
    deeper = client.usage_tree(child["path"])
    assert deeper["path"] == "movies"
    assert [c["name"] for c in deeper["children"]] == ["2024"]
    assert deeper["children"][0]["path"] == "movies/2024"


def test_usage_reports_scan_progress_per_directory():
    """A whole-share walk is minutes of silence otherwise, which reads as a hang."""
    seen = []
    _client(_usage_tree()).usage_tree(on_scan=lambda d, f: seen.append((d, f)))
    assert len(seen) == 4                      # one per directory
    assert [d for d, _ in seen] == [1, 2, 3, 4]
    assert seen[-1][1] == 5                    # every file counted by the end


def test_usage_of_a_missing_directory_says_so_rather_than_reporting_zero():
    """`_remote_file_map` returns {} for a missing path *and* an empty one;
    reporting "0 bytes" for a typo would be a confident wrong answer."""
    try:
        _client(_usage_tree()).usage_tree("nope")
    except Exception as e:
        assert "doesn't exist" in str(e)
    else:
        raise AssertionError("a missing directory reported as empty")


def test_volume_info_prefers_what_this_user_may_actually_write():
    """With a quota, the disk's free space promises room a sync can't use."""
    fake = _FakeSMB(volume=(1000, 200, 400))
    info = _client(fake).volume_info()
    assert info["total"] == 1000
    assert info["free"] == 200          # the caller's headroom
    assert info["disk_free"] == 400     # the disk's, kept so the gap can be shown
    assert info["used"] == 600


def _duplicate_tree(contents):
    sizes = {path: len(body) for path, body in contents.items()}
    return _FakeSMB(
        tree={"\\\\1.2.3.4\\share": ([], [p.rsplit("\\", 1)[-1] for p in contents])},
        sizes=sizes, contents=contents,
    )


def test_duplicates_group_by_size_without_reading_anything():
    """Grouping is free — every size is already in the index."""
    fake = _duplicate_tree({
        "\\\\1.2.3.4\\share\\a.bin": b"x" * 100,
        "\\\\1.2.3.4\\share\\b.bin": b"x" * 100,
        "\\\\1.2.3.4\\share\\c.bin": b"y" * 7,
    })
    groups = _client(fake).find_duplicates()
    assert len(groups) == 1
    assert groups[0]["paths"] == ["a.bin", "b.bin"]
    assert groups[0]["wasted"] == 100
    assert groups[0]["hash"] is None            # nothing was read to say this
    assert fake.written == {}


def test_verifying_splits_same_size_files_that_differ():
    """Equal size is not equal content — reporting them as duplicates would
    invite someone to delete a file that isn't a copy of anything."""
    fake = _duplicate_tree({
        "\\\\1.2.3.4\\share\\a.bin": b"A" * 100,
        "\\\\1.2.3.4\\share\\b.bin": b"A" * 100,
        "\\\\1.2.3.4\\share\\c.bin": b"B" * 100,       # same size, different bytes
    })
    groups = _client(fake).find_duplicates(verify=True)
    assert len(groups) == 1
    assert groups[0]["paths"] == ["a.bin", "b.bin"]
    assert groups[0]["hash"] is not None


def test_duplicates_are_ranked_by_space_recoverable():
    fake = _duplicate_tree({
        "\\\\1.2.3.4\\share\\small1": b"s" * 10,
        "\\\\1.2.3.4\\share\\small2": b"s" * 10,
        "\\\\1.2.3.4\\share\\big1": b"b" * 5000,
        "\\\\1.2.3.4\\share\\big2": b"b" * 5000,
    })
    groups = _client(fake).find_duplicates()
    assert [g["wasted"] for g in groups] == [5000, 10]


def test_empty_files_are_not_reported_as_duplicates_of_each_other():
    """Every empty file matches every other one; that is noise, not a finding."""
    fake = _duplicate_tree({
        "\\\\1.2.3.4\\share\\one": b"",
        "\\\\1.2.3.4\\share\\two": b"",
    })
    assert _client(fake).find_duplicates() == []


def test_the_cost_of_verifying_can_be_stated_before_paying_it():
    """There is no server-side hash in SMB, so verifying reads every candidate
    byte over the network — the caller has to be able to warn first."""
    fake = _duplicate_tree({
        "\\\\1.2.3.4\\share\\a.bin": b"x" * 100,
        "\\\\1.2.3.4\\share\\b.bin": b"x" * 100,
    })
    client = _client(fake)
    groups = client.find_duplicates()
    assert client.duplicate_scan_cost(groups) == 200      # both files, in full



def test_status_codes_wrapped_in_smboserror_still_get_a_friendly_message():
    """Regression: `smbclient` reports protocol failures through SMBOSError
    rather than the typed exceptions, so matching on the class alone let a raw
    "[NtStatus 0xc000006d]" reach the user — which is what the mapping exists
    to prevent. Found when share listing moved onto the IPC$ pipe."""
    from smbprotocol.exceptions import SMBOSError

    assert SAMBAClient._friendly_error(SMBOSError(0xC000006D, "x")) == \
        "Authentication failed. Check username and password."
    assert "Access denied" in SAMBAClient._friendly_error(SMBOSError(0xC0000022, "x"))


def test_share_listing_hides_administrative_shares_but_can_show_them():
    """The picker wants somewhere to sync; the info panel wants the truth."""
    shares = [
        {"name": "backup", "is_disk": True, "is_special": False},
        {"name": "IPC$", "is_disk": False, "is_special": True},
        {"name": "C$", "is_disk": True, "is_special": True},
    ]
    client = _client(_FakeSMB())
    client.share_details = lambda: shares

    assert client.list_shares() == ["backup"]
    assert client.list_shares(include_all=True) == ["backup", "IPC$", "C$"]


# --------------------------------------------------------------------------- #
# Encryption
# --------------------------------------------------------------------------- #
def test_encryption_off_leaves_the_session_to_negotiate():
    """`encrypt=None`, never `False`.

    Passing False actively *disables* encryption, which breaks a connection to
    a share whose own config requires it — a server that works today would
    stop working purely because NASync started being explicit.
    """
    fake = _FakeSMB()
    _client(fake, encrypt=False).connect()
    assert fake.sessions[0]["encrypt"] is None


def test_encryption_on_is_requested_of_the_session():
    fake = _FakeSMB()
    _client(fake, encrypt=True).connect()
    assert fake.sessions[0]["encrypt"] is True


def test_a_server_too_old_to_encrypt_is_named_as_the_cause():
    """The raw message is accurate and useless: it says "the connection", which
    tells a home user nothing about which end to change."""
    message = SAMBAClient._friendly_error(SMBException(
        "SMB encryption is required but the connection does not support it"))
    assert "doesn't support encrypted connections" in message
    assert "SMB 3" in message
    assert "Turn encryption off" in message


def test_encryption_with_guest_access_says_what_to_do_instead():
    message = SAMBAClient._friendly_error(SMBException(
        "SMB encryption or signing was required but session was authenticated "
        "as a guest which does not support encryption or signing"))
    assert "guest" in message
    assert "username and password" in message


def test_an_ordinary_auth_failure_is_not_mistaken_for_an_encryption_one():
    """The encryption check runs first, so it must not swallow its neighbours."""
    assert SAMBAClient._friendly_error(LogonFailure()) == \
        "Authentication failed. Check username and password."


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
# Remote indexing — one listing per directory, sizes taken from the listing
# --------------------------------------------------------------------------- #
def _nested_tree():
    root = "\\\\1.2.3.4\\share\\base"
    return root, _FakeSMB(
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


def test_remote_file_map_walks_tree():
    _root, fake = _nested_tree()
    m = _client(fake)._remote_file_map("base")
    assert m == {"root.txt": 5, ".hidden": 2,
                 "sub/mid.txt": 3, "sub/deep/leaf.txt": 7}, m
    print("✅ test_remote_file_map_walks_tree passed")


def test_remote_index_costs_one_round_trip_per_directory():
    """The whole point of the rewrite: sizes come from the listing.

    The previous implementation stat()ed every file after walking, which is one
    extra round trip per file on every sync — a minute of "comparing files…"
    on a large share before anything has even been decided.
    """
    root, fake = _nested_tree()
    _client(fake)._remote_file_map("base")

    assert sorted(fake.scandir_calls) == sorted(
        [root, f"{root}\\sub", f"{root}\\sub\\deep"]), fake.scandir_calls
    assert fake.stat_calls == [], fake.stat_calls
    print("✅ test_remote_index_costs_one_round_trip_per_directory passed")


def test_remote_file_map_missing_dir_is_empty():
    """A destination that doesn't exist yet reads as empty, not an error."""
    assert _client(_FakeSMB())._remote_file_map("nope") == {}
    print("✅ test_remote_file_map_missing_dir_is_empty passed")


def test_missing_path_is_recognised_in_both_error_shapes():
    """smbclient wraps a missing path as OSError/ENOENT; the layer below it
    raises the raw status. A first sync must read as empty either way."""
    assert SAMBAClient._is_missing(
        SMBOSError(NtStatus.STATUS_OBJECT_PATH_NOT_FOUND, "x"))
    assert SAMBAClient._is_missing(samba_module.ObjectNameNotFound())
    assert not SAMBAClient._is_missing(AccessDenied())
    print("✅ test_missing_path_is_recognised_in_both_error_shapes passed")


def test_remote_index_survives_a_directory_vanishing_mid_scan():
    """A subdirectory removed between listings is skipped, not fatal."""
    root = "\\\\1.2.3.4\\share\\base"
    fake = _FakeSMB(
        tree={root: (["gone"], ["kept.txt"])},   # "gone" is never listable
        sizes={f"{root}\\kept.txt": 4},
    )
    assert _client(fake)._remote_file_map("base") == {"kept.txt": 4}
    print("✅ test_remote_index_survives_a_directory_vanishing_mid_scan passed")


def test_remote_index_resolves_symlinks_instead_of_trusting_the_listing():
    """A reparse point's listed size describes the link, not the target — and
    a symlinked directory must not be indexed as if it were a file."""
    root = "\\\\1.2.3.4\\share\\base"
    fake = _FakeSMB(
        tree={
            root: (["realdir", "linkdir"], ["linkfile.txt"]),
            f"{root}\\realdir": ([], []),
            f"{root}\\linkdir": ([], ["never-reached.txt"]),
        },
        sizes={f"{root}\\linkfile.txt": 42, f"{root}\\linkdir": 0},
        symlinks={f"{root}\\linkfile.txt", f"{root}\\linkdir"},
    )
    m = _client(fake)._remote_file_map("base")
    assert m == {"linkfile.txt": 42}, m               # target size, via stat
    assert f"{root}\\linkdir" not in fake.scandir_calls, fake.scandir_calls
    print("✅ test_remote_index_resolves_symlinks_instead_of_trusting_the_listing passed")


# --------------------------------------------------------------------------- #
# Local indexing
# --------------------------------------------------------------------------- #
def test_local_file_map_indexes_tree_and_separates_illegal_names():
    client = _client(_FakeSMB())
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "a.txt").write_text("aaa")
        os.makedirs(os.path.join(tmpdir, "sub", "deep"))
        Path(tmpdir, "sub", "deep", "c.txt").write_text("c")
        Path(tmpdir, "bad:name.txt").write_text("x")

        files, unsupported = client._local_file_map(tmpdir)
        assert set(files) == {"a.txt", "sub/deep/c.txt"}, files
        assert files["a.txt"][1] == 3, files
        assert files["sub/deep/c.txt"][0] == os.path.join(tmpdir, "sub", "deep", "c.txt")
        assert unsupported == ["bad:name.txt"], unsupported
        print("✅ test_local_file_map_indexes_tree_and_separates_illegal_names passed")


def test_local_index_does_not_follow_directory_symlinks():
    """Following them is how a scan turns into an infinite loop."""
    client = _client(_FakeSMB())
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "real"))
        Path(tmpdir, "real", "f.txt").write_text("f")
        os.symlink(tmpdir, os.path.join(tmpdir, "loop"))   # points at its parent

        files, _ = client._local_file_map(tmpdir)
        assert set(files) == {"real/f.txt"}, files
        print("✅ test_local_index_does_not_follow_directory_symlinks passed")


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
    client._remote_file_map = lambda base, **_: {}
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
    client._remote_file_map = lambda base, **_: {}
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
    client._remote_file_map = lambda base, **_: {}   # empty remote -> all new
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
        client._remote_file_map = lambda base, **_: {"a.txt": 4, "stale.txt": 9}

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
    client._remote_file_map = lambda base, **_: {"stale.txt": 9}
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


def test_directories_are_created_once_each_not_once_per_file():
    """An album of a thousand photos used to cost a thousand identical mkdirs.

    `makedirs` builds intermediate levels itself, so only the base and the
    deepest parent of each upload ever need asking for.
    """
    fake = _FakeSMB()
    client = _client(fake)
    client._remote_file_map = lambda base, **_: {}
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "photos", "2024", "june"))
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            Path(tmpdir, "photos", "2024", "june", name).write_text("x")
        Path(tmpdir, "photos", "top.jpg").write_text("y")

        client.sync_directory(tmpdir, "/base", direction="push")

    # "photos" is implied by "photos/2024/june" and must not be asked for.
    assert fake.made_dirs == [
        "\\\\1.2.3.4\\share\\base",
        "\\\\1.2.3.4\\share\\base\\photos\\2024\\june",
    ], fake.made_dirs
    print("✅ test_directories_are_created_once_each_not_once_per_file passed")


def test_leaf_dirs_keeps_only_the_deepest_paths():
    assert SAMBAClient._leaf_dirs({"a", "a/b", "a/b/c"}) == ["a/b/c"]
    assert SAMBAClient._leaf_dirs({"a/b", "c"}) == ["a/b", "c"]
    assert SAMBAClient._leaf_dirs(set()) == []
    print("✅ test_leaf_dirs_keeps_only_the_deepest_paths passed")


def test_push_in_update_mode_uploads_but_deletes_nothing():
    """Update mode still uploads; it just never removes server-only files."""
    fake = _FakeSMB()
    client = _client(fake)
    client._remote_file_map = lambda base, **_: {"stale.txt": 9}
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "new.txt").write_text("hello")

        stats = client.sync_directory(tmpdir, "/base", direction="push",
                                      mirror=False)
        assert stats["transferred"] == 1, stats
        assert stats["deleted"] == 0, stats
        assert stats["errors"] == 0, stats
        assert fake.removed == [], fake.removed
        written = fake.written["\\\\1.2.3.4\\share\\base\\new.txt"]
        assert written.getvalue() == b"hello"
        print("✅ test_push_in_update_mode_uploads_but_deletes_nothing passed")


def test_update_mode_dry_run_promises_no_deletes():
    """The dry run is what a user checks before trusting the mode."""
    lines = []
    client = _client(_FakeSMB(), reporter=lines.append)
    client._remote_file_map = lambda base, **_: {"stale.txt": 9}
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "new.txt").write_text("hello")

        stats = client.sync_directory(tmpdir, "/base", direction="push",
                                      dry_run=True, mirror=False)
        assert stats["deleted"] == 0, stats
        assert not any("removed locally" in line for line in lines), lines
        assert any("0 to delete" in line for line in lines), lines
        print("✅ test_update_mode_dry_run_promises_no_deletes passed")


# --------------------------------------------------------------------------- #
# Pull is incremental too
# --------------------------------------------------------------------------- #
def test_pull_skips_files_that_match_locally():
    client = _client(_FakeSMB())
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "same.txt").write_text("1234")     # 4 bytes, matches remote
        client._remote_file_map = lambda base, **_: {"same.txt": 4, "missing.txt": 6}

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
    client._remote_file_map = lambda base, **_: {}
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
    test_remote_index_costs_one_round_trip_per_directory()
    test_remote_file_map_missing_dir_is_empty()
    test_missing_path_is_recognised_in_both_error_shapes()
    test_remote_index_survives_a_directory_vanishing_mid_scan()
    test_remote_index_resolves_symlinks_instead_of_trusting_the_listing()
    test_local_file_map_indexes_tree_and_separates_illegal_names()
    test_local_index_does_not_follow_directory_symlinks()
    test_storable_rejects_smb_illegal_characters()
    test_colon_names_are_skipped_not_silently_truncated()
    test_legal_awkward_filenames_still_sync()
    test_push_dry_run_discovers_files()
    test_push_incremental_plan()
    test_push_missing_local_dir_raises()
    test_push_uploads_and_mirrors_deletes()
    test_directories_are_created_once_each_not_once_per_file()
    test_leaf_dirs_keeps_only_the_deepest_paths()
    test_push_in_update_mode_uploads_but_deletes_nothing()
    test_update_mode_dry_run_promises_no_deletes()
    test_pull_skips_files_that_match_locally()
    test_cancel_event_aborts_transfer()
    test_byte_progress_reports_incrementally()
    test_file_progress_signature_unchanged()


# --------------------------------------------------------------------------- #
# 0.20.0 — change detection and mirror safety
#
# Each of these reproduces something that went wrong on a real NAS: an edit
# that never reached the backup, a renamed photo deleted from it, a renamed
# folder copied over again in full, and a mirror run that deleted 57,000 files.
# --------------------------------------------------------------------------- #
from nasync.core.samba import MASS_DELETE_MIN, MirrorRefused  # noqa: E402


def _local(entries):
    """{rel: (size, mtime)} → the shape `_local_file_map` returns."""
    return {rel: (f"/local/{rel}", size, mtime)
            for rel, (size, mtime) in entries.items()}


def _plan(local, remote, mtimes=None, dirs=None, mirror=True):
    return SAMBAClient.plan_push(_local(local), remote, mtimes or {},
                                 set(dirs or ()), mirror)


def test_an_edit_that_keeps_the_size_is_still_uploaded():
    """Size-only comparison missed every same-length edit, silently."""
    plan = _plan({"notes.txt": (100, 2_000.0)}, {"notes.txt": 100},
                 {"notes.txt": 1_000.0})
    assert plan["upload"] == [("notes.txt", "notes.txt")], plan


def test_an_older_upload_is_not_mistaken_for_a_change():
    """Before 0.20.0 the server's time was the upload time — later than the
    local edit. Treating "different" as "changed" would re-upload everything
    once; it is unchanged, and gets the real time recorded instead."""
    plan = _plan({"a.jpg": (5, 1_000.0)}, {"a.jpg": 5}, {"a.jpg": 9_000.0})
    assert plan["upload"] == [] and plan["unchanged"] == ["a.jpg"], plan
    assert plan["stamps"] == [("a.jpg", 1_000.0)], plan


def test_times_within_the_slack_are_the_same_time():
    plan = _plan({"a": (5, 1_001.5)}, {"a": 5}, {"a": 1_000.0})
    assert plan["upload"] == [] and plan["stamps"] == [], plan


def test_an_upload_carries_the_local_time_to_the_server():
    fake = _FakeSMB()
    client = _client(fake)
    client._remote_file_map = lambda base, **_: {}
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir, "doc.txt")
        path.write_text("hello")
        os.utime(path, (1_500_000_000, 1_500_000_000))
        client.sync_directory(tmpdir, "/base", direction="push")
    assert fake.stamped["\\\\1.2.3.4\\share\\base\\doc.txt"] == (
        1_500_000_000, 1_500_000_000), fake.stamped


def test_a_case_only_rename_renames_instead_of_deleting():
    """img_09 → IMG_09 used to upload into the same file on a case-insensitive
    share and then delete it, losing it from the backup until the next run."""
    plan = _plan({"album/IMG_09.jpg": (7, 1.0)}, {"album/img_09.jpg": 7},
                 {"album/img_09.jpg": 1.0}, dirs={"album"})
    assert plan["delete"] == [], plan
    assert plan["renames"] == [("album/img_09.jpg", "album/IMG_09.jpg")], plan
    assert plan["upload"] == [], plan


def test_a_case_only_rename_never_reaches_remove():
    fake = _FakeSMB()
    client = _client(fake)
    client._remote_file_map = lambda base, **_: {"img_09.jpg": 4}
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "IMG_09.jpg").write_text("abcd")
        stats = client.sync_directory(tmpdir, "/base", direction="push")
    assert fake.removed == [], fake.removed
    assert fake.renamed == [("\\\\1.2.3.4\\share\\base\\img_09.jpg",
                             "\\\\1.2.3.4\\share\\base\\IMG_09.jpg")], fake.renamed
    assert stats["deleted"] == 0, stats


def test_a_folder_whose_case_changed_is_left_alone_not_deleted():
    plan = _plan({"photos/x.jpg": (3, 1.0)}, {"Photos/x.jpg": 3},
                 {"Photos/x.jpg": 1.0}, dirs={"Photos"})
    assert plan["delete"] == [] and plan["upload"] == [], plan
    assert plan["stale"] == [], "the folder is not stale just for its casing"


def test_a_renamed_folder_is_moved_on_the_server_not_copied_again():
    local = {f"album_renamed/img_{i}.jpg": (100 + i, 50.0) for i in range(40)}
    remote = {f"album/img_{i}.jpg": 100 + i for i in range(40)}
    mtimes = {rel: 50.0 for rel in remote}
    plan = _plan(local, remote, mtimes, dirs={"album"})
    assert plan["upload"] == [] and plan["delete"] == [], plan
    assert len(plan["moves"]) == 40, plan["moves"]
    assert plan["stale"] == ["album"], plan


def test_same_named_files_are_told_apart_by_their_folders():
    """A renamed project folder is full of __init__.py — identical names that
    only the path above them distinguishes."""
    local = {"new/a/__init__.py": (10, 5.0), "new/b/__init__.py": (10, 5.0)}
    remote = {"old/a/__init__.py": 10, "old/b/__init__.py": 10}
    plan = _plan(local, remote, {r: 5.0 for r in remote})
    assert sorted(plan["moves"]) == [("old/a/__init__.py", "new/a/__init__.py"),
                                     ("old/b/__init__.py", "new/b/__init__.py")]


def test_an_uncertain_match_is_uploaded_rather_than_moved():
    """Without a matching time — every file uploaded before 0.20.0 — a match
    could be wrong, and a wrong one would be recorded as unchanged forever."""
    plan = _plan({"b/x.jpg": (9, 1_000.0)}, {"a/x.jpg": 9}, {"a/x.jpg": 8_000.0})
    assert plan["moves"] == [], plan
    assert plan["upload"] == [("b/x.jpg", "b/x.jpg")] and plan["delete"] == ["a/x.jpg"]


def test_an_empty_local_folder_is_never_mirrored():
    """~/Programming emptied by a move would have deleted ~23,000 files."""
    fake = _FakeSMB()
    client = _client(fake)
    client._remote_file_map = lambda base, **_: {"keep.txt": 1, "also.txt": 2}
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            client.sync_directory(tmpdir, "/base", direction="push",
                                  allow_mass_delete=True)
        except MirrorRefused as e:
            assert not e.confirmable
        else:
            raise AssertionError("an empty source was mirrored")
    assert fake.removed == [], fake.removed


def test_a_mass_delete_waits_for_a_yes():
    fake = _FakeSMB()
    client = _client(fake)
    remote = {f"f{i}.bin": 1 for i in range(MASS_DELETE_MIN * 2)}
    client._remote_file_map = lambda base, **_: dict(remote)
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "f0.bin").write_text("x")
        try:
            client.sync_directory(tmpdir, "/base", direction="push")
        except MirrorRefused as e:
            assert e.confirmable and e.deletes == len(remote) - 1, e
        else:
            raise AssertionError("a 99% delete went through unasked")
        assert fake.removed == [] and fake.written == {}, "touched before refusing"

        client.sync_directory(tmpdir, "/base", direction="push",
                              allow_mass_delete=True)
    assert len(fake.removed) == len(remote) - 1


def test_small_or_partial_deletes_are_not_questioned():
    few = _plan({"a": (1, 1.0)}, {"a": 1, "b": 1, "c": 1}, {"a": 1.0})
    SAMBAClient.check_mirror(few, 1, 3)                       # 2 of 3: too few
    local = {f"k{i}": (1, 1.0) for i in range(100)}
    remote = {**{f"k{i}": 1 for i in range(100)}, **{f"d{i}": 1 for i in range(60)}}
    some = _plan(local, remote, {r: 1.0 for r in remote})
    SAMBAClient.check_mirror(some, len(local), len(remote))   # 60 of 160


def test_a_dry_run_says_where_a_real_run_would_stop():
    lines = []
    client = _client(_FakeSMB(), reporter=lines.append)
    client._remote_file_map = lambda base, **_: {"keep.txt": 1}
    with tempfile.TemporaryDirectory() as tmpdir:
        stats = client.sync_directory(tmpdir, "/base", direction="push",
                                      dry_run=True)
    assert stats["refused"], stats
    assert any("would stop here" in line for line in lines), lines


def test_leftover_empty_folders_go_even_when_nothing_else_changed():
    """The "Already up to date" shortcut used to skip the folder cleanup, so
    folders an older version left behind stayed until some file changed."""
    fake = _FakeSMB()
    client = _client(fake)

    def remote(base, dirs=None, **_):
        if dirs is not None:
            dirs.update({"old_leftover", "old_leftover/inner"})
        return {"a.txt": 1}

    client._remote_file_map = remote
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "a.txt").write_text("x")
        os.utime(Path(tmpdir, "a.txt"), (0, 0))
        client.sync_directory(tmpdir, "/base", direction="push")
    assert fake.removed_dirs == ["\\\\1.2.3.4\\share\\base\\old_leftover\\inner",
                                 "\\\\1.2.3.4\\share\\base\\old_leftover"]


def test_restore_fetches_a_same_size_file_that_is_newer_on_the_server():
    client = _client(_FakeSMB())
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "old.txt").write_text("1234")
        os.utime(Path(tmpdir, "old.txt"), (1_000, 1_000))
        Path(tmpdir, "mine.txt").write_text("1234")
        os.utime(Path(tmpdir, "mine.txt"), (9_000, 9_000))

        def remote(base, mtimes=None, **_):
            mtimes.update({"old.txt": 5_000.0, "mine.txt": 5_000.0})
            return {"old.txt": 4, "mine.txt": 4}

        client._remote_file_map = remote
        lines = []
        client._report = lines.append
        stats = client.sync_directory(tmpdir, "/base", direction="pull",
                                      dry_run=True)
    assert any("↓ old.txt" in line for line in lines), lines
    assert stats["skipped"] == 1, "a newer local copy must not be overwritten"


def test_a_case_rename_the_server_refuses_goes_through_a_temporary_name():
    """Samba answers ACCESS_DENIED to renaming img.jpg to IMG.jpg directly."""
    fake = _FakeSMB()
    first = []

    def rename(src, dst, **kwargs):
        if not first and dst.endswith("IMG.jpg"):
            first.append(dst)
            raise OSError("STATUS_ACCESS_DENIED")
        fake.renamed.append((src, dst))

    fake.rename = rename
    client = _client(fake)
    client._rename_case("\\\\s\\img.jpg", "\\\\s\\IMG.jpg")
    temp = f"\\\\s\\img.jpg.nasync-case-{os.getpid()}"
    assert fake.renamed == [("\\\\s\\img.jpg", temp), (temp, "\\\\s\\IMG.jpg")]
