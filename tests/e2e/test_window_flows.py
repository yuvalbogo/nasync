"""The real window, driven through its main loop against a real server."""

import os
import shutil

import pytest

from e2e.conftest import gtk_or_skip, pump


@pytest.fixture
def window(world, monkeypatch):
    gtk_or_skip()
    from gi.repository import Adw
    from nasync.system import launcher, service
    monkeypatch.setattr(launcher, "repair", lambda: False)
    monkeypatch.setattr(service, "repair", lambda: False)
    world.config.set_active_server("lab")
    from nasync.gui.window import NasyncWindow
    Adw.init()
    app = Adw.Application(application_id="io.github.YuvalBogo.NASync.E2E")
    win = NasyncWindow(app)
    yield win
    win.destroy()


def show(window):
    """What the window's one-second poll does: pick up the changed config."""
    window.config.refresh()
    window.refresh_servers()


def row_status(window, server, pair):
    for row in window._pair_groups[server]._rows:
        info = getattr(row, "_nasync", None)
        if info and info["pair"] == pair:
            return info["status"].get_label()
    return None


def test_the_window_syncs_and_says_so_folder_by_folder(world, window):
    world.pair("Documents", {"cv.pdf": os.urandom(50_000), "notes/a.txt": "a"})
    world.pair("Pictures", {"cat.jpg": os.urandom(80_000)})
    show(window)
    window.start_transfer("push", False)
    assert window._busy and window._progress_title.get_label() == "Syncing…"
    pump(lambda: not window._busy, what="the sync to finish")
    assert window._progress_title.get_label() == "Sync complete", \
        window._progress_detail.get_label()
    assert "3 transferred" in window._progress_detail.get_label()
    assert not window._log_scroll.get_visible(), "an in-progress line was left up"
    world.assert_mirrored("Documents")
    world.assert_mirrored("Pictures")
    assert row_status(window, "lab", "Documents").startswith("Synced")
    assert row_status(window, "lab", "Pictures").startswith("Synced")


def test_a_preview_changes_nothing_and_lists_what_it_would_do(world, window):
    world.pair("Documents", {"cv.pdf": "cv", "letter.odt": "hi"})
    show(window)
    window.start_transfer("push", True)
    pump(lambda: not window._busy, what="the preview")
    assert window._progress_title.get_label() == "Preview ready — nothing was changed"
    assert world.server_tree("Documents") == {}
    assert any("cv.pdf" in line for line in window._preview), window._preview


def test_one_folder_is_restored_from_its_row(world, window):
    world.pair("Documents", {"cv.pdf": "my cv"})
    world.pair("Pictures", {"cat.jpg": "meow"})
    show(window)
    window.start_transfer("push", False)
    pump(lambda: not window._busy, what="the first backup")
    shutil.rmtree(world.local("Documents"))
    shutil.rmtree(world.local("Pictures"))
    os.makedirs(world.local("Documents"))
    os.makedirs(world.local("Pictures"))

    from gi.repository import GLib
    window.start_transfer("pull", False, server="lab", pair="Pictures")
    assert "Restoring 'Pictures'" in window._progress_title.get_label()
    pump(lambda: not window._busy, what="the restore")
    assert window._progress_title.get_label() == "Restore complete"
    assert os.listdir(world.local("Pictures")) == ["cat.jpg"]
    assert os.listdir(world.local("Documents")) == [], "restored a folder not asked for"
    assert GLib  # the row menu's action is covered in tests/test_gui.py


def test_a_sync_from_the_command_line_is_shown_and_can_be_cancelled(world, window):
    world.pair("p")
    world.many_files("p", 4000)
    show(window)
    run = world.start_nasync("sync", "--server", "lab")
    pump(lambda: window._bg_status is not None, timeout=60,
         what="the window to notice the CLI's sync")
    pump(lambda: "command line" in window._progress_title.get_label(), what="its title")
    assert not window._sync_button.get_sensitive(), "Sync offered during another sync"
    assert window._cancel_button.get_sensitive()
    pump(lambda: "Uploading" in window._progress_log.get_label()
         or "Reading" in window._progress_log.get_label(), what="its progress")
    window.cancel_transfer()
    out, _ = run.communicate(timeout=120)
    assert "Cancelled" in out, out
    pump(lambda: window._bg_status is None, what="the window to see it end")
    assert window._progress_title.get_label() == "Sync cancelled", \
        window._progress_title.get_label()
    assert window._sync_button.get_sensitive()


@pytest.mark.parametrize("answer", ["keep", "delete"])
def test_a_held_delete_is_asked_and_the_answer_is_done(world, window, monkeypatch, answer):
    from gi.repository import Adw
    world.pair("p", {f"f{i:02d}.txt": f"file {i}" for i in range(80)})
    show(window)
    window.start_transfer("push", False)
    pump(lambda: not window._busy, what="the first backup")
    for i in range(70):
        os.remove(os.path.join(world.local("p"), f"f{i:02d}.txt"))

    asked = []
    monkeypatch.setattr(Adw.AlertDialog, "present",
                        lambda self, parent: asked.append(self))
    window.start_transfer("push", False)
    pump(lambda: asked, what="the question")
    dialog = asked[0]
    assert dialog.get_heading() == "Delete files on the server?"
    assert "f00.txt" in dialog.get_body(), "the question named no files"
    assert len(world.server_tree("p")) == 80, "deleted before the answer"
    dialog.emit("response", answer)
    pump(lambda: not window._busy, what="the answer to be carried out")
    left = len(world.server_tree("p"))
    assert left == (80 if answer == "keep" else 10), left
