"""The 0.21.0 audit's findings, each proven fixed against a real server.

Every test here failed on 0.20.0. The audit's own reproductions are the
model: the same files, the same sequence, the same expectation.
"""

import hashlib
import os
import threading

import pytest


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_an_interrupted_restore_leaves_the_local_file_and_the_backup_alone(lab):
    """A cancelled restore left 2 MB of an 8 MB file, which the next sync then
    uploaded over the only complete copy."""
    from nasync.core.sync import Syncer
    video = os.urandom(8 * 1024 * 1024)
    lab.pair("p", {"video.bin": video})
    lab.sync("p")
    path = os.path.join(lab.local("p"), "video.bin")
    os.remove(path)
    cancel = threading.Event()

    def stop(done, total, name, verb):
        if done >= 2 * 1024 * 1024:
            cancel.set()

    Syncer(config=lab.config, server="lab", reporter=lab.log.append,
           byte_progress=stop, cancel_event=cancel).restore_pair("p")
    assert not os.path.exists(path), "a partial restore was left in place"
    lab.sync("p")
    assert lab.server_tree("p")["video.bin"] == _digest(video)


def test_restore_keeps_an_unsynced_edit_that_changed_the_size(lab):
    lab.pair("p", {"draft.txt": "old text"})
    lab.sync("p")
    lab.write("p", "draft.txt", "a much longer, newer, unsynced draft")
    result = lab.restore("p")
    with open(os.path.join(lab.local("p"), "draft.txt")) as handle:
        assert handle.read() == "a much longer, newer, unsynced draft"
    assert result["ok"] == 1
    assert any("kept your local" in line for line in lab.log), lab.log


def test_restoring_from_a_folder_that_does_not_exist_is_an_error(lab):
    lab.pair("p", {"a.txt": "x"})
    lab.server_exec(f"rm -rf '{lab.server_path('p')}'")
    result = lab.restore("p")
    assert result["failed"] == 1 and result["ok"] == 0, lab.log
    assert any("doesn't exist" in line for line in lab.log), lab.log


def test_rewriting_every_file_is_held_back(lab):
    """Ransomware or a broken tool rewriting every photo: one run used to
    overwrite every backup."""
    lab.pair("p", {f"photo{i}.jpg": f"real photo {i}" for i in range(60)})
    lab.sync("p")
    for i in range(60):
        lab.write("p", f"photo{i}.jpg", b"")
    result = lab.sync("p")
    held = [r for r in result["refused"] if r.get("overwrites")]
    assert held, lab.log
    server = lab.server_tree("p")
    assert server["photo0.jpg"] == _digest(b"real photo 0"), "backups were overwritten"


def test_the_background_service_holds_back_a_48_percent_delete(lab):
    from nasync.core.sync import Syncer
    lab.pair("p", {**{f"keep{i}": "k" for i in range(60)},
                   **{f"external/f{i}": "e" for i in range(55)}})
    lab.sync("p")
    import shutil
    shutil.rmtree(os.path.join(lab.local("p"), "external"))  # disk unplugged
    result = Syncer(config=lab.config, server="lab", source="background",
                    reporter=lab.log.append).sync_pair("p")
    assert result["refused"], lab.log
    assert len(lab.server_tree("p")) == 115


def test_a_folder_that_can_be_listed_but_not_entered_keeps_its_backup(lab):
    lab.pair("p", {"ok.txt": "x", "locked/a.txt": "1", "locked/b.txt": "2"})
    lab.sync("p")
    locked = os.path.join(lab.local("p"), "locked")
    os.chmod(locked, 0o644)                     # readable names, no entry
    try:
        lab.sync("p")
    finally:
        os.chmod(locked, 0o755)
    assert {"locked/a.txt", "locked/b.txt"} <= set(lab.server_tree("p")), lab.log


def test_a_different_machine_at_the_nas_address_never_gets_the_password(lab):
    from nasync.core.samba import ServerIdentityChanged
    lab.pair("p", {"a.txt": "x"})
    lab.sync("p")
    assert lab.config.get_server_config("lab")["identity"], "identity not learned"
    lab.config.set_server_identity("lab", "00000000-0000-0000-0000-000000000000")
    with pytest.raises(ServerIdentityChanged):
        lab.sync("p")


def test_the_same_nas_under_two_server_entries_cannot_overlap(lab):
    """A LAN address and another name for the same NAS: pairs on the two
    entries deleted each other's files."""
    lab.pair("photos", {"img.jpg": "pixels"})
    lab.sync("photos")
    port = lab.samba["port"]
    lab.config.add_or_update_server("lab-alias", "localhost", "labuser", "labpass",
                                    "backup", port, root=lab.root)
    folder = os.path.join(str(lab.home), "src", "everything")
    os.makedirs(folder)
    # Allowed at this point: nothing yet says "localhost" is the same NAS.
    lab.config.add_pair(folder, "", "everything", server="lab-alias")
    from nasync.core.sync import Syncer
    # First contact teaches the alias its identity, which matches.
    Syncer(config=lab.config, server="lab-alias", reporter=lab.log.append,
           source="cli").sync_pair(dry_run=True)
    lab.config.reload()
    assert lab.config.same_storage("lab", "lab-alias")
    result = Syncer(config=lab.config, server="lab-alias", reporter=lab.log.append,
                    source="cli").sync_pair()
    assert result["overlapping"] == ["everything"], lab.log
    assert "photos/img.jpg" in lab.server_tree("photos") or \
        "img.jpg" in lab.server_tree("photos")
