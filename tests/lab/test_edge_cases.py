"""Edge cases, each phrased as what a user would naturally assume.

Every test here is a sentence a person would say about their backup — "if I
edit a file, the NAS has the edit" — checked against a real Samba server. A
failure is either a bug or an assumption NASync can't honour; the latter must
then be *said*, loudly, which these tests check too.
"""

import os
import time
import unicodedata

HOUR = 3600


# --------------------------------------------------------------------------- #
# "If I change a file, the NAS has the change."
# --------------------------------------------------------------------------- #
def test_a_same_size_edit_reaches_the_nas(lab):
    lab.pair("docs", {"notes.txt": "draft one"})
    lab.sync("docs")
    time.sleep(2.2)
    lab.write("docs", "notes.txt", "draft two")         # same length
    lab.sync("docs")
    lab.assert_mirrored("docs")


def test_an_older_version_restored_locally_reaches_the_nas(lab):
    """Restoring yesterday's draft from email: same size, *older* time."""
    now = time.time()
    lab.pair("docs")
    lab.write("docs", "report.txt", "version B", mtime=now)
    lab.sync("docs")
    lab.write("docs", "report.txt", "version A", mtime=now - 2 * 86400)
    lab.sync("docs")
    lab.assert_mirrored("docs")


def test_touching_a_file_does_not_upload_it_again(lab):
    lab.pair("docs", {"big.bin": os.urandom(4096)})
    lab.sync("docs")
    os.utime(os.path.join(lab.local("docs"), "big.bin"))
    result = lab.sync("docs")
    assert result["transferred"] == 0, lab.log
    lab.assert_mirrored("docs")


def test_a_file_that_changed_size_is_uploaded(lab):
    lab.pair("docs", {"a.txt": "short"})
    lab.sync("docs")
    lab.write("docs", "a.txt", "a much longer body now")
    assert lab.sync("docs")["transferred"] == 1
    lab.assert_mirrored("docs")


# --------------------------------------------------------------------------- #
# "Files with the same name, size or date are still different files."
# --------------------------------------------------------------------------- #
def test_same_names_in_different_folders_with_different_sizes(lab):
    lab.pair("p", {"a/index.html": "one", "b/index.html": "three333",
                   "c/index.html": "x" * 50})
    lab.sync("p")
    lab.assert_mirrored("p")


def test_same_name_same_size_different_dates_are_kept_apart(lab):
    now = time.time()
    lab.pair("p")
    lab.write("p", "x/photo.jpg", "AAAA", mtime=now - 10 * 86400)
    lab.write("p", "y/photo.jpg", "BBBB", mtime=now)
    lab.sync("p")
    lab.assert_mirrored("p")


def test_moving_one_of_two_lookalike_files_moves_the_right_one(lab):
    """Rename detection must not pair up the wrong twin."""
    now = time.time()
    lab.pair("p")
    lab.write("p", "x/photo.jpg", "AAAA", mtime=now)
    lab.write("p", "y/photo.jpg", "BBBB", mtime=now)
    lab.sync("p")
    os.rename(os.path.join(lab.local("p"), "x"), os.path.join(lab.local("p"), "z"))
    lab.sync("p")
    lab.assert_mirrored("p")


def test_identical_content_under_different_names_is_all_backed_up(lab):
    lab.pair("p", {"copy1.txt": "same", "copy2.txt": "same", "sub/copy3.txt": "same"})
    lab.sync("p")
    lab.assert_mirrored("p")


def test_swapping_two_files_names(lab):
    lab.pair("p", {"a.txt": "content of a", "b.txt": "content of b"})
    lab.sync("p")
    base = lab.local("p")
    os.rename(os.path.join(base, "a.txt"), os.path.join(base, "tmp"))
    os.rename(os.path.join(base, "b.txt"), os.path.join(base, "a.txt"))
    os.rename(os.path.join(base, "tmp"), os.path.join(base, "b.txt"))
    lab.sync("p")
    lab.assert_mirrored("p")


# --------------------------------------------------------------------------- #
# "Whatever I name a file, it is backed up — or I am told it isn't."
# --------------------------------------------------------------------------- #
def test_hebrew_spaces_and_emoji_names(lab):
    lab.pair("p", {"פרוייקטים/רעיון למוצר.docx": "x", "My Docs/a b  c.txt": "y",
                   "emoji 🎉/party.txt": "z", ".hidden/.dotfile": "h"})
    lab.sync("p")
    lab.assert_mirrored("p")


def test_composed_and_decomposed_names_are_both_kept_or_reported(lab):
    nfc = unicodedata.normalize("NFC", "café.txt")
    nfd = unicodedata.normalize("NFD", "café.txt")
    assert nfc != nfd
    lab.pair("p", {nfc: "composed", nfd: "decomposed"})
    result = lab.sync("p")
    server = lab.server_tree("p")
    both = len(server) == 2
    told = result.get("unsupported", 0) > 0 or result.get("errors", 0) > 0
    assert both or told, f"one of two files silently missing: {server}"


def test_names_differing_only_in_case_are_kept_or_reported(lab):
    """Linux keeps README and readme apart; an SMB share cannot."""
    lab.pair("p", {"README.txt": "upper", "readme.txt": "lower"})
    result = lab.sync("p")
    server = lab.server_tree("p")
    both = len(server) == 2 and len(set(server.values())) == 2
    told = result.get("unsupported", 0) > 0 or result.get("errors", 0) > 0
    assert both or told, f"one of two files silently lost: {server} {lab.log}"


def test_a_case_only_rename_keeps_the_file(lab):
    lab.pair("p", {"img_09.jpg": "pixels"})
    lab.sync("p")
    base = lab.local("p")
    os.rename(os.path.join(base, "img_09.jpg"), os.path.join(base, "IMG_09.jpg"))
    lab.sync("p")
    lab.assert_mirrored("p")


def test_a_folder_case_rename_keeps_its_files(lab):
    lab.pair("p", {"Photos/a.jpg": "1", "Photos/b.jpg": "2"})
    lab.sync("p")
    base = lab.local("p")
    os.rename(os.path.join(base, "Photos"), os.path.join(base, "photos"))
    lab.sync("p")
    server = lab.server_tree("p")
    assert {k.lower() for k in server} == {"photos/a.jpg", "photos/b.jpg"}, server


def test_names_smb_forbids_are_reported(lab):
    lab.pair("p", {"ok.txt": "fine", "a:b.txt": "colon", "what?.txt": "q"})
    result = lab.sync("p")
    assert result["unsupported"] == 2, lab.log


def test_trailing_dots_and_spaces_are_backed_up_or_reported(lab):
    lab.pair("p", {"name.": "dot", "space ": "space", "plain": "plain"})
    result = lab.sync("p")
    server = lab.server_tree("p")
    told = result.get("unsupported", 0) + result.get("errors", 0)
    assert len(server) + told >= 3, f"silently lost: {server} {lab.log}"
    for rel, digest in server.items():
        assert rel in ("name.", "space ", "plain"), f"stored under another name: {rel}"


# --------------------------------------------------------------------------- #
# "What's on the NAS is my folder — no less."
# --------------------------------------------------------------------------- #
def test_empty_folders_are_kept(lab):
    lab.pair("p", {"keep.txt": "x"})
    os.makedirs(os.path.join(lab.local("p"), "empty", "nested"))
    lab.sync("p")
    assert {"empty", "empty/nested"} <= lab.server_dirs("p"), lab.server_dirs("p")


def test_a_symlinked_file_is_backed_up_as_its_content(lab):
    lab.pair("p", {"real.txt": "the content"})
    os.symlink("real.txt", os.path.join(lab.local("p"), "link.txt"))
    lab.sync("p")
    assert "link.txt" in lab.server_tree("p")


def test_a_symlinked_folder_is_reported_not_silently_skipped(lab):
    lab.pair("p", {"keep.txt": "x"})
    outside = lab.home / "elsewhere"
    outside.mkdir()
    (outside / "inside.txt").write_text("y")
    os.symlink(str(outside), os.path.join(lab.local("p"), "linked-folder"))
    result = lab.sync("p")
    assert result.get("unsupported", 0) or result.get("errors", 0) or \
        "linked-folder/inside.txt" in lab.server_tree("p"), lab.log


def test_an_unreadable_file_is_reported(lab):
    lab.pair("p", {"ok.txt": "x"})
    path = lab.write("p", "secret.txt", "y")
    os.chmod(path, 0)
    try:
        result = lab.sync("p")
    finally:
        os.chmod(path, 0o600)
    assert result["errors"] >= 1, lab.log


def test_an_unreadable_folder_does_not_delete_its_backup(lab):
    """A folder whose permissions changed is not a folder that was deleted."""
    lab.pair("p", {"ok.txt": "x", "private/a.txt": "1", "private/b.txt": "2"})
    lab.sync("p")
    private = os.path.join(lab.local("p"), "private")
    os.chmod(private, 0)
    try:
        lab.sync("p")
    finally:
        os.chmod(private, 0o700)
    server = lab.server_tree("p")
    assert {"private/a.txt", "private/b.txt"} <= set(server), \
        f"backup of an unreadable folder was deleted: {server} {lab.log}"


# --------------------------------------------------------------------------- #
# "Someone else touching the NAS doesn't make me lose anything."
# --------------------------------------------------------------------------- #
def test_a_file_edited_on_the_nas_is_not_silently_overwritten(lab):
    lab.pair("p", {"shared.txt": "mine"})
    lab.sync("p")
    lab.server_write("p", "shared.txt", "edited on the NAS by the other laptop")
    time.sleep(1.1)
    lab.write("p", "shared.txt", "my newer local edit")
    lab.sync("p")
    contents = set(lab.server_tree("p").values())
    import hashlib
    theirs = hashlib.sha256(b"edited on the NAS by the other laptop").hexdigest()
    assert theirs in contents, "the NAS-side edit was destroyed"


def test_a_file_added_on_the_nas_is_not_deleted(lab):
    lab.pair("p", {"mine.txt": "x"})
    lab.sync("p")
    lab.server_write("p", "dropped-in-by-phone.jpg", "photo")
    lab.sync("p")
    assert "dropped-in-by-phone.jpg" in lab.server_tree("p"), lab.log


def test_a_file_deleted_on_the_nas_is_backed_up_again(lab):
    lab.pair("p", {"a.txt": "x", "b.txt": "y"})
    lab.sync("p")
    lab.server_remove("p", "a.txt")
    lab.sync("p")
    lab.assert_mirrored("p")


def test_a_file_i_deleted_is_deleted_from_the_mirror(lab):
    lab.pair("p", {"a.txt": "x", "b.txt": "y"})
    lab.sync("p")
    os.remove(os.path.join(lab.local("p"), "a.txt"))
    lab.sync("p")
    lab.assert_mirrored("p")


def test_a_one_hour_clock_shift_on_the_nas_does_not_resend_everything(lab):
    """FAT/exFAT volumes report times shifted by an hour across DST."""
    lab.pair("p", {f"f{i}.txt": f"file {i}" for i in range(5)})
    lab.sync("p")
    for i in range(5):
        current = lab.server_mtime("p", f"f{i}.txt")
        lab.server_exec(f"touch -d @{current + HOUR} '{lab.server_path('p', f'f{i}.txt')}'")
    result = lab.sync("p")
    assert result["transferred"] == 0, lab.log
    lab.assert_mirrored("p")


# --------------------------------------------------------------------------- #
# "An interrupted sync never leaves me worse off."
# --------------------------------------------------------------------------- #
def test_a_cancelled_upload_never_leaves_a_truncated_file_in_place(lab):
    import threading
    from nasync.core.sync import Syncer
    lab.pair("p", {"big.bin": b"old" * 10})
    lab.sync("p")
    lab.write("p", "big.bin", os.urandom(8 * 1024 * 1024))
    cancel = threading.Event()

    def stop_halfway(done, total, name, verb):
        if done >= 2 * 1024 * 1024:
            cancel.set()

    Syncer(config=lab.config, server="lab", reporter=lab.log.append,
           byte_progress=stop_halfway, cancel_event=cancel).sync_pair("p")
    server = lab.server_tree("p")
    import hashlib
    old = hashlib.sha256(b"old" * 10).hexdigest()
    assert server.get("big.bin") == old, \
        "a cancelled upload left a partial file where the good copy was"


# --------------------------------------------------------------------------- #
# "Restore gives me back what I had."
# --------------------------------------------------------------------------- #
def test_restore_round_trip_is_byte_identical_with_times(lab):
    files = {"a.txt": "alpha", "deep/er/b.bin": os.urandom(3000), "פ/ש.txt": "heb"}
    lab.pair("p", files)
    times = {}
    for i, rel in enumerate(files):
        path = os.path.join(lab.local("p"), *rel.split("/"))
        os.utime(path, (1_600_000_000 + i, 1_600_000_000 + i))
        times[rel] = 1_600_000_000 + i
    lab.sync("p")
    before = lab.local_tree("p")
    import shutil
    shutil.rmtree(lab.local("p"))
    os.makedirs(lab.local("p"))
    lab.restore("p")
    assert lab.local_tree("p") == before
    for rel, when in times.items():
        path = os.path.join(lab.local("p"), *rel.split("/"))
        assert abs(os.stat(path).st_mtime - when) < 2, rel


def test_restore_does_not_overwrite_newer_local_work(lab):
    lab.pair("p", {"draft.txt": "old text"})
    lab.sync("p")
    time.sleep(2.2)
    lab.write("p", "draft.txt", "new text")             # not synced yet
    lab.restore("p")
    with open(os.path.join(lab.local("p"), "draft.txt")) as handle:
        assert handle.read() == "new text"


def test_restore_keeps_the_executable_bit(lab):
    lab.pair("p", {"run.sh": "#!/bin/sh\necho hi\n"})
    path = os.path.join(lab.local("p"), "run.sh")
    os.chmod(path, 0o755)
    lab.sync("p")
    os.remove(path)
    lab.restore("p")
    assert os.stat(path).st_mode & 0o111, "restored script is no longer executable"
