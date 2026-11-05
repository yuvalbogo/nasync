"""What a person does at the command line, checked on the server's disk."""

import os
import shutil
import time


def tree_mtimes(world, pair):
    base = world.local(pair)
    return {os.path.relpath(os.path.join(d, f), base): int(os.stat(os.path.join(d, f)).st_mtime)
            for d, _, files in os.walk(base) for f in files}


def test_back_up_change_and_restore_through_the_cli(world):
    """Add a folder, preview, sync, change things, sync, lose the folder,
    restore: the bytes and times come back exactly."""
    folder = world.local("docs")
    world.write("docs", "report.txt", "quarterly numbers")
    world.write("docs", "photos/2024/beach.jpg", os.urandom(300_000))
    world.write("docs", "שלום/ünïcødé name.md", "unicode both ways")
    world.write("docs", "big.iso", os.urandom(5 * 1024 * 1024))
    os.makedirs(os.path.join(folder, "empty folder"))

    world.nasync("add", folder, "docs", "--name", "docs", "--server", "lab", check=0)
    world.nasync("list", "--server", "lab", check=0)
    assert "docs" in world.last_output

    world.nasync("sync", "--server", "lab", "--dry-run", check=0)
    assert "4 to upload" in world.last_output, world.last_output
    assert world.server_tree("docs") == {}, "a dry run changed the server"

    world.nasync("sync", "--server", "lab", check=0)
    world.assert_mirrored("docs")
    assert "empty folder" in world.server_dirs("docs")

    # A same-size edit, a new file, a deletion and a rename, all at once.
    time.sleep(1.1)
    world.write("docs", "report.txt", "QUARTERLY NUMBERS")
    world.write("docs", "notes/new.txt", "fresh")
    os.remove(os.path.join(folder, "photos", "2024", "beach.jpg"))
    os.rename(os.path.join(folder, "big.iso"), os.path.join(folder, "archive.iso"))
    world.nasync("sync", "--server", "lab", check=0)
    world.assert_mirrored("docs")
    assert "moved 1" in world.last_output, "a rename was uploaded again"

    world.nasync("status", check=0)
    assert "lab" in world.last_output

    before, times = world.local_tree("docs"), tree_mtimes(world, "docs")
    shutil.rmtree(folder)
    os.makedirs(folder)
    world.nasync("restore", "--server", "lab", "-y", check=0)
    assert world.local_tree("docs") == before
    assert tree_mtimes(world, "docs") == times, "restore lost modification times"


def test_update_mode_keeps_and_the_switch_back_to_mirror_asks(world):
    files = {f"f{i:02d}.txt": f"file {i}" for i in range(60)}
    world.pair("p", files)
    world.nasync("sync", "--server", "lab", check=0)
    world.nasync("mode", "update", "--server", "lab", check=0)
    for i in range(55):
        os.remove(os.path.join(world.local("p"), f"f{i:02d}.txt"))
    world.nasync("sync", "--server", "lab", check=0)
    assert len(world.server_tree("p")) == 60, "update mode deleted"

    world.nasync("mode", "mirror", "--server", "lab", check=0)
    world.nasync("sync", "--server", "lab", check=1)
    out = world.last_output
    assert "would delete 55" in out and "Not asking without a terminal" in out, out
    assert "✗ f00.txt" in out, "the question gave a count but no names"
    assert len(world.server_tree("p")) == 60, "deleted without a yes"

    world.nasync("sync", "--server", "lab", "--allow-mass-delete", check=0)
    assert sorted(world.server_tree("p")) == [f"f{i:02d}.txt" for i in range(55, 60)]


def test_inspection_commands_answer_from_a_real_server(world):
    world.pair("p", {"a/one.bin": b"x" * 2_000_000, "b/two.bin": b"x" * 2_000_000,
                     "c.txt": "c"})
    world.nasync("sync", "--server", "lab", check=0)
    world.nasync("test-connection", check=0)
    world.nasync("server-info", "--server", "lab", check=0)
    world.nasync("ls", world.root + "/p", "--server", "lab", check=0)
    assert "c.txt" in world.last_output and "a" in world.last_output, world.last_output
    world.nasync("usage", world.root, "--server", "lab", check=0)
    assert "p" in world.last_output
    world.nasync("duplicates", world.root, "--server", "lab", "--verify", check=0)
    assert "one.bin" in world.last_output and "two.bin" in world.last_output, \
        world.last_output


def test_one_sync_at_a_time_and_cancel_reaches_it(world):
    """A second sync is refused while one runs, `status` shows the running one,
    and `nasync cancel` stops it without leaving half a file on the NAS."""
    world.pair("p")
    world.many_files("p", 4000)
    run = world.start_nasync("sync", "--server", "lab")
    world.wait_for(lambda: (world.status() or {}).get("files_total"),
                   what="the sync to start uploading")

    world.nasync("status", check=0)
    assert "Syncing now" in world.last_output and "nasync cancel" in world.last_output, \
        world.last_output
    second = world.nasync("sync", "--server", "lab")
    assert second.returncode != 0, "a second sync ran alongside the first"

    world.nasync("cancel", check=0)
    out, _ = run.communicate(timeout=120)
    assert "Cancelled" in out, out
    leftovers = world.server_exec(f"find '{world.server_path('p')}' -name '*.nasync-*'")
    assert not leftovers.strip(), f"half-written files left: {leftovers}"
    local = world.local_tree("p")
    for rel, digest in world.server_tree("p").items():
        assert local[rel] == digest, f"{rel} on the NAS is not the local file"
    assert world.status() is None, "the run's status outlived it"


def test_restore_keeps_what_changed_here_and_says_so(world):
    world.pair("p", {"draft.txt": "old text", "other.txt": "o"})
    world.nasync("sync", "--server", "lab", check=0)
    time.sleep(1.1)
    world.write("p", "draft.txt", "a much longer new version, not yet backed up")
    os.remove(os.path.join(world.local("p"), "other.txt"))
    world.nasync("restore", "--server", "lab", "-y", check=0)
    out = world.last_output
    assert "kept your local 'draft.txt'" in out, out
    with open(os.path.join(world.local("p"), "draft.txt")) as handle:
        assert handle.read().startswith("a much longer"), "restore overwrote new work"
    assert os.path.exists(os.path.join(world.local("p"), "other.txt"))


def test_a_file_that_did_not_make_it_fails_the_command(world):
    world.pair("p", {"ok.txt": "fine", "locked.txt": "secret"})
    locked = os.path.join(world.local("p"), "locked.txt")
    os.chmod(locked, 0)
    try:
        world.nasync("sync", "--server", "lab", check=1)
    finally:
        os.chmod(locked, 0o644)
    assert "locked.txt" in world.last_output, world.last_output
    assert "ok.txt" in world.server_tree("p")


def test_a_command_for_an_unknown_server_does_nothing(world):
    world.pair("p", {"a.txt": "a"})
    world.nasync("sync", "--server", "nope", check=1)
    assert "No server named 'nope'" in world.last_output
    assert world.server_tree("p") == {}
