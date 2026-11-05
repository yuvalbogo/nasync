"""The third data-safety audit (of 0.21.1), as lab scenarios.

Each asserts what a person would naturally expect; each failed when found.
"""
import hashlib
import os
import shutil
import subprocess
import sys

import pytest

import nasync
from lab.conftest import USER, PASSWORD, SHARE

AUDIT = os.path.dirname(os.path.dirname(os.path.abspath(nasync.__file__)))


def sha(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def state_of(lab, pair, server="lab"):
    from nasync.core.state import SyncState
    cfg = lab.config
    landing = cfg.resolve_remote(cfg.get_pair(pair, server)["server"], server)
    st = SyncState(cfg.config_dir, server, SHARE, landing, read_only=True)
    try:
        return st.initialised, st.mirrored, st.load()
    finally:
        st.close()


def bg_sync(lab, pair=None, **kw):
    from nasync.core.sync import Syncer
    lab.log = []
    return Syncer(reporter=lab.log.append, config=lab.config, server="lab",
                  source="background").sync_pair(pair, **kw)


# --------------------------------------------------------------------------
# T1: the "first mirror run" guard is not re-armed by mirror -> update -> mirror
# --------------------------------------------------------------------------
def _update_then_mirror(lab, mirrored_before, n_files, n_deleted, runner):
    files = {f"photo{i:03}.jpg": f"pixels {i}" for i in range(n_files)}
    if not mirrored_before:
        lab.config.set_sync_mode("update", server="lab")
    lab.pair("p", files)
    lab.sync("p")
    print("after first run (initialised, mirrored):", state_of(lab, "p")[:2])
    lab.config.set_sync_mode("update", server="lab")
    for i in range(n_deleted):
        os.remove(os.path.join(lab.local("p"), f"photo{i:03}.jpg"))
    lab.sync("p")
    assert len(lab.server_tree("p")) == n_files
    lab.config.set_sync_mode("mirror", server="lab")
    res = runner(lab, "p")
    print("\n".join(lab.log))
    return res, len(lab.server_tree("p"))


@pytest.mark.parametrize("who", ["person", "service"])
def test_t1_control_never_mirrored_pair_is_held(lab, who):
    runner = (lambda world, p: world.sync(p)) if who == "person" else bg_sync
    res, left = _update_then_mirror(lab, False, 300, 55, runner)
    print(who, "control: server files left", left, "refused", res["refused"])
    assert left == 300


@pytest.mark.parametrize("who", ["person", "service"])
def test_t1_switch_back_to_mirror_deletes_without_asking(lab, who):
    runner = (lambda world, p: world.sync(p)) if who == "person" else bg_sync
    res, left = _update_then_mirror(lab, True, 300, 55, runner)
    print(who, "server files left", left, "refused", res["refused"])
    assert left == 300, (f"switching update -> mirror deleted {300 - left} "
                         f"files with no question (DATA_SAFETY: first mirror run "
                         f"incl. a switch from update waits for a person at 50+)")


# --------------------------------------------------------------------------
# T2: a backslash in a Linux file name is an SMB path separator
# --------------------------------------------------------------------------
def test_t2_backslash_name_replaces_another_files_backup(lab):
    lab.pair("p", {"a/b.txt": "the real a/b.txt, important",
                   "a\\b.txt": "x"})
    r1 = lab.sync("p")
    log1 = list(lab.log)
    t1 = lab.server_tree("p")
    r2 = lab.sync("p")
    log2 = list(lab.log)
    t2 = lab.server_tree("p")
    print("run1", r1, "\n".join(log1))
    print("server after run1:", t1)
    print("run2", r2, "\n".join(log2))
    print("server after run2:", t2)
    assert t2.get("a/b.txt") == sha("the real a/b.txt, important"), \
        "the backup of a/b.txt holds another file's content"


# --------------------------------------------------------------------------
# T3: one file the server can never take keeps the pair in bootstrap for ever,
#     so mirror keeps deleting files other people put on the NAS
# --------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["latin1", "backslash"])
def test_t3_one_unstorable_name_keeps_bootstrap(lab, kind):
    lab.pair("p", {"a.txt": "a", "b.txt": "b"})
    folder = lab.local("p")
    if kind == "latin1":
        # An old archive extracted on Linux: the name is ISO-8859-1, not UTF-8.
        fd = os.open(os.path.join(os.fsencode(folder), b"caf\xe9.txt"),
                     os.O_CREAT | os.O_WRONLY, 0o644)
        os.write(fd, b"menu")
        os.close(fd)
    else:
        lab.write("p", "notes\\2024.txt", "n")
    for _ in range(3):
        res = lab.sync("p")
    print(res, "\n".join(lab.log))
    initialised, mirrored, _ = state_of(lab, "p")
    print("initialised after 3 runs:", initialised)
    lab.server_write("p", "from-partner.jpg", "partner photo")
    lab.sync("p")
    print("\n".join(lab.log))
    assert "from-partner.jpg" in lab.server_tree("p"), (
        f"a file someone else put on the NAS was deleted (initialised={initialised})")


# --------------------------------------------------------------------------
# T5: killed between an upload and the record's commit
# --------------------------------------------------------------------------
KILL_SCRIPT = r"""
import os, signal, sys
sys.path.insert(0, sys.argv[1])
from nasync.core.config import Config
from nasync.core.samba import SAMBAClient
from nasync.core.sync import Syncer
from nasync.system import vault
vault.backend = lambda: ""
real = SAMBAClient._upload_file
count = {"n": 0}
def upload(self, *a, **k):
    out = real(self, *a, **k)
    count["n"] += 1
    if count["n"] == int(sys.argv[2]):
        os.kill(os.getpid(), signal.SIGKILL)
    return out
SAMBAClient._upload_file = upload
Syncer(config=Config(), server="lab", source="cli",
       reporter=lambda m: None).sync_pair("p")
"""


def test_t5_kill_after_upload_before_commit(lab):
    files = {f"doc{i}.txt": f"version one of doc {i}" for i in range(5)}
    lab.pair("p", files)
    lab.sync("p")
    for i in range(5):
        lab.write("p", f"doc{i}.txt", f"version two of doc {i}, a bit longer")
    env = dict(os.environ)
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    proc = subprocess.run([sys.executable, "-I", "-c", KILL_SCRIPT, AUDIT, "3"],
                          env=env, capture_output=True, text=True)
    print("child exit:", proc.returncode, proc.stderr[-500:])
    print("server after kill:", sorted(lab.server_tree("p")))
    res = lab.sync("p")
    print(res, "\n".join(lab.log))
    tree = lab.server_tree("p")
    print("server after next sync:", sorted(tree))
    copies = [r for r in tree if "NAS copy" in r]
    # the user deletes doc0 locally; mirror
    os.remove(os.path.join(lab.local("p"), "doc0.txt"))
    lab.sync("p")
    print("after deleting doc0 locally:", sorted(lab.server_tree("p")))
    assert not copies, f"NASync's own uploads were treated as NAS edits: {copies}"


# --------------------------------------------------------------------------
# T6: a file becomes a folder (and the reverse)
# --------------------------------------------------------------------------
def test_t6a_file_becomes_folder_blocks_the_whole_pair(lab):
    lab.pair("p", {"notes": "a plain notes file", "other.txt": "o"})
    lab.sync("p")
    os.remove(os.path.join(lab.local("p"), "notes"))
    lab.write("p", "notes/today.txt", "today")
    lab.write("p", "new-thesis.tex", "brand new work")
    results = []
    for _ in range(2):
        results.append(lab.sync("p"))
    print(results[-1], "\n".join(lab.log))
    tree = lab.server_tree("p")
    print("server:", sorted(tree))
    assert "new-thesis.tex" in tree, "an unrelated new file was never backed up"


def test_t6b_folder_becomes_file_in_update_mode(lab):
    lab.config.set_sync_mode("update", server="lab")
    lab.pair("p", {"report/part1.txt": "one", "report/part2.txt": "two"})
    lab.sync("p")
    shutil.rmtree(os.path.join(lab.local("p"), "report"))
    lab.write("p", "report", "the report, now one file")
    res = lab.sync("p")
    print(res, "\n".join(lab.log))
    print("server:", lab.server_exec(f"cd '{lab.server_path('p')}' && find . | sort"))
    res = lab.sync("p")
    print(res, "\n".join(lab.log))
    tree = lab.server_tree("p")
    print("server after 2nd:", sorted(tree))
    assert "report/part1.txt" in tree, \
        "update mode: the old folder is no longer where it was on the NAS"


# --------------------------------------------------------------------------
# T7: restore adopts other people's files as NASync's own
# --------------------------------------------------------------------------
def test_t7_restore_then_local_delete_removes_foreign_file(lab):
    lab.pair("p", {"mine.txt": "mine"})
    lab.sync("p")
    lab.server_write("p", "partner.jpg", "partner photo")
    lab.sync("p")
    print("foreign kept:", state_of(lab, "p")[2].get("partner.jpg"))
    lab.restore("p")
    lab.sync("p")
    print("after restore+sync:", state_of(lab, "p")[2].get("partner.jpg"))
    os.remove(os.path.join(lab.local("p"), "partner.jpg"))
    lab.sync("p")
    print("\n".join(lab.log))
    assert "partner.jpg" in lab.server_tree("p")


# --------------------------------------------------------------------------
# T8: names Samba alters
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["trailing.", "trailing ", "CON", "aux.txt"])
def test_t8_names_the_nas_would_change_are_skipped_and_said(lab, name):
    """Samba stored these under an 8.3 alias: re-sent on every run, restored
    under the wrong name. Skipped and reported instead — never mangled."""
    lab.pair("p", {name: "data", "z.txt": "z"})
    r1 = lab.sync("p")
    r2 = lab.sync("p")
    print(repr(name), r1, r2, "\n".join(lab.log))
    on_disk = lab.server_exec(f"cd '{lab.server_path('p')}' && ls -A").split("\n")
    assert r1["unsupported"] == 1 and r2["transferred"] == 0
    assert any(repr(name)[1:-1] in line or name in line for line in lab.log
               if "skipped" in line), lab.log
    assert "z.txt" in on_disk and not any("~" in f for f in on_disk), on_disk


# ---- from test_a3b.py -------------------------------------------------
def test_replace_learning(lab):
    from nasync.core import samba
    calls = []
    real_replace = samba.smbclient.replace

    def replace(src, dst, **kw):
        try:
            real_replace(src, dst, **kw)
            calls.append(("replace ok", dst.rsplit("\\", 1)[-1]))
        except Exception as e:
            calls.append(("replace FAIL", dst.rsplit("\\", 1)[-1], str(e)[:80]))
            raise
    samba.smbclient.replace = replace
    try:
        lab.pair("p", {"b_changed.txt": "v1"})
        lab.sync("p")
        lab.write("p", "a_new.txt", "new")
        lab.write("p", "b_changed.txt", "v2 longer")
        res = lab.sync("p")
    finally:
        samba.smbclient.replace = real_replace
    print(calls)
    print(res, "\n".join(lab.log))
    assert lab.server_tree("p").get("b_changed.txt") == sha("v2 longer")


@pytest.mark.parametrize("name", ["aux.c", "trailing.", "con"])
def test_mangled_bootstrap_and_restore(lab, name):
    # An older NASync (or any copy) already put the file on the NAS:
    # the pair starts in bootstrap with it there.
    lab.config.set_sync_mode("update", server="lab")
    lab.pair("p", {name: "important source", "z.txt": "z"})
    lab.sync("p")
    lab.server_write("p", name, "important source")
    # Forget the record (as after an upgrade from <0.21, a server re-add, a root move)
    from nasync.core import state
    state.forget_server(lab.config.config_dir, "lab")
    lab.config.set_sync_mode("mirror", server="lab")
    r = lab.sync("p")
    print("bootstrap mirror run:", r, "\n".join(lab.log))
    after = lab.server_tree("p")
    print("server after bootstrap mirror run:", after)
    lab.sync("p")
    print("next run:", "\n".join(lab.log))
    # restore onto a new machine (no record, empty folder)
    shutil.rmtree(lab.local("p"))
    os.makedirs(lab.local("p"))
    state.forget_server(lab.config.config_dir, "lab")
    lab.restore("p")
    print("restore:", "\n".join(lab.log))
    print("restored locally:", sorted(os.listdir(lab.local("p"))))
    assert name in after, "the file's backup was deleted by the bootstrap mirror run"
    # SMB only ever shows the alias, so the name can't come back — the bytes do.
    assert sha("important source") in lab.local_tree("p").values(), \
        "restore lost the contents"


def test_mangled_folder(lab):
    lab.pair("p", {"drivers/aux/aux.c": "driver", "drivers/i2c.c": "i2c"})
    lab.sync("p")
    l1 = list(lab.log)
    r2 = lab.sync("p")
    print("\n".join(l1), "\n", "\n".join(lab.log))
    print("server:", lab.server_tree("p"))
    _, _, rec = state_of(lab, "p")
    print("record:", {k: (v.kept) for k, v in rec.items()})
    assert r2["transferred"] == 0


# ---- from test_a3c.py -------------------------------------------------
def test_t4_failed_remove_server_drops_record_but_keeps_server(lab, monkeypatch):
    from nasync.system import vault
    from nasync.core.config import Config
    from nasync.core.sync import Syncer
    real_dir = os.path.expanduser("~/.config/nasync")
    lab.pair("p", {"a.txt": "a", "b.txt": "b"})
    lab.sync("p")
    lab.server_write("p", "from-partner.jpg", "partner photo")
    lab.sync("p")
    assert "from-partner.jpg" in lab.server_tree("p")
    print("before (initialised, mirrored):", state_of(lab, "p")[:2])

    with monkeypatch.context() as m:       # the keyring is locked
        m.setattr(vault, "is_envelope", lambda data: True)

        def locked(*a, **k):
            raise vault.VaultError("the keyring is locked")
        m.setattr(vault, "unseal", locked)
        lab.config._creds_cache = None
        with pytest.raises(vault.VaultError) as caught:
            lab.config.remove_server("lab")
        print("remove_server raised:", str(caught.value)[:100])

    fresh = Config()
    assert str(fresh.config_dir) != real_dir and str(fresh.config_dir).startswith(str(lab.home))
    print("on disk: servers", [s["name"] for s in fresh.list_servers()],
          "pairs", [p["name"] for p in fresh.get_pairs("lab")])
    print("record now (initialised, mirrored):", state_of(lab, "p")[:2])
    log = []
    Syncer(reporter=log.append, config=fresh, server="lab",
           source="background").sync_pair()
    print("\n".join(log))
    tree = lab.server_tree("p")
    print("server:", sorted(tree))
    lab.config.set_notifications(False)     # an unrelated toggle in the same app
    after = Config()
    print("after an unrelated toggle: servers", [s["name"] for s in after.list_servers()],
          "active", after.get_active_server_name(),
          "pairs_by_server", list(after.data["pairs_by_server"]))
    assert "from-partner.jpg" in tree, "foreign file deleted after a failed remove"


def test_replace_learning_across_pairs(lab):
    """A camera folder gets a new photo each day; Documents gets an edit each day."""
    lab.config.set_sync_mode("update", server="lab")
    lab.pair("a-camera", {"img000.jpg": "photo 0"})
    lab.pair("b-documents", {"thesis.tex": "draft 0"})
    lab.sync()
    for day in range(1, 4):
        lab.write("a-camera", f"img{day:03}.jpg", f"photo {day}")
        lab.write("b-documents", "thesis.tex", f"draft {day} " + "more text " * day)
        res = lab.sync()
        print(f"day {day}:", res["errors"],
              [line for line in lab.log if "!" in line or "Uploaded" in line])
    srv = lab.server_tree("b-documents")
    print("thesis on NAS is draft 3?", srv["thesis.tex"] == sha("draft 3 " + "more text " * 3))
    assert srv["thesis.tex"] == sha("draft 3 " + "more text " * 3), \
        "three days of thesis edits never reached the NAS"


# ---- from test_a3d.py -------------------------------------------------
def test_case_only_server_rename_wipes_record(lab):
    from nasync.core.sync import Syncer
    lab.pair("p", {"a.txt": "a", "b.txt": "b"})
    lab.sync("p")
    lab.server_write("p", "from-partner.jpg", "partner photo")
    lab.sync("p")
    base = lab.server_path("p")
    print("before:", state_of(lab, "p")[:2], len(state_of(lab, "p")[2]), "entries")
    lab.config.save_server("LAB", "127.0.0.1", USER, PASSWORD, SHARE,
                           lab.samba["port"], orig_name="lab", root=lab.root)
    from nasync.core.state import SyncState
    landing = lab.config.resolve_remote("p", "LAB")
    st = SyncState(lab.config.config_dir, "LAB", SHARE, landing, read_only=True)
    print("after renaming 'lab' -> 'LAB': initialised", st.initialised,
          "entries", len(st.load()))
    st.close()
    log = []
    Syncer(reporter=log.append, config=lab.config, server="LAB",
           source="background").sync_pair()
    print("\n".join(log))
    out = lab.server_exec(f"cd '{base}' && ls")
    print("server:", out.split())
    assert "from-partner.jpg" in out.split()


def test_service_hold_is_waved_through_by_any_manual_sync(lab):
    files = {f"f{i:03}.txt": f"file {i}" for i in range(300)}
    lab.pair("p", files)
    lab.sync("p")
    for i in range(90):                      # 30%: a disk half-unmounted, say
        os.remove(os.path.join(lab.local("p"), f"f{i:03}.txt"))
    r = bg_sync(lab, "p")
    print("service:", r["refused"][0]["message"][:160] if r["refused"] else r)
    # The person opens the app and presses Sync.
    r = lab.sync("p")
    print("manual sync:", "\n".join(lab.log))
    left = len(lab.server_tree("p"))
    assert left == 300, f"{300 - left} deleted with no list and no question"


# ---- from test_a3e.py -------------------------------------------------
@pytest.mark.parametrize("who", ["service", "person"])
def test_guard_diluted_by_files_it_can_never_delete(lab, who):
    lab.pair("photos", {f"mine/{i:03}.jpg": f"my photo {i}" for i in range(100)})
    lab.sync("photos")
    # The partner's 400 photos, added on the NAS (kept as "not from NASync").
    base = lab.server_path("photos")
    lab.server_exec(f"mkdir -p '{base}/partner' && cd '{base}/partner' && "
                    f"for i in $(seq 1 400); do echo partner $i > p$i.jpg; done && "
                    f"chmod -R 777 '{base}/partner'")
    lab.sync("photos")
    print("foreign recorded:", sum(e.kept for e in state_of(lab, "photos")[2].values()))
    # 99 of my 100 photos vanish locally (a wrong rm, a sub-folder not mounted)
    for i in range(1, 100):
        os.remove(os.path.join(lab.local("photos"), "mine", f"{i:03}.jpg"))
    bg_sync(lab, "photos") if who == "service" else lab.sync("photos")
    print("\n".join(lab.log))
    mine_left = [r for r in lab.server_tree("photos") if r.startswith("mine/")]
    print(who, "my photos left on the NAS:", len(mine_left))
    assert len(mine_left) == 100, \
        f"{100 - len(mine_left)} of my 100 photos deleted without a question"
