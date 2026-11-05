"""The second data-safety audit of 0.21.0, as lab scenarios.

Each test asserts what a user would naturally expect; every one failed on the
first cut of 0.21.0 and is kept here as a regression test once fixed.
"""

import hashlib
import os
import signal
import subprocess
import sys
import time
import uuid

import pytest

import nasync
from lab.conftest import USER, PASSWORD, SHARE, IMAGE


def sha(data) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def state_of(lab, pair, server="lab"):
    from nasync.core.state import SyncState
    cfg = lab.config
    landing = cfg.resolve_remote(cfg.get_pair(pair, server)["server"], server)
    st = SyncState(cfg.config_dir, server, SHARE, landing, read_only=True)
    try:
        return st.initialised, st.load()
    finally:
        st.close()


# --------------------------------------------------------------------------- #
# F1: one permanently unreadable folder (lost+found on a mounted disk) keeps a
#     pair in bootstrap forever, so mirror keeps deleting files other people
#     put on the NAS -- the 0.21 "foreign files are never deleted" promise
#     never engages.
# --------------------------------------------------------------------------- #
def test_f1_unreadable_folder_keeps_pair_in_bootstrap_and_foreign_files_die(lab):
    # A pair on a mounted ext4 disk: lost+found is root-only from day one.
    lab.pair("p", {"a.txt": "a", "b.txt": "b", "lost+found/x": "x"})
    locked = os.path.join(lab.local("p"), "lost+found")
    os.chmod(locked, 0)
    try:
        for _ in range(4):
            lab.sync("p")
        initialised, _ = state_of(lab, "p")
        print("initialised after 4 runs:", initialised)
        lab.server_write("p", "from-partner.jpg", "partner photo")
        lab.sync("p")
        print("\n".join(lab.log))
    finally:
        os.chmod(locked, 0o755)
    assert "from-partner.jpg" in lab.server_tree("p"), (
        f"a file someone else put on the NAS was deleted (initialised="
        f"{initialised})")


def test_f1b_one_unreadable_file_also_blocks_initialisation(lab):
    lab.pair("p", {"a.txt": "a", "secret.key": "k"})
    path = os.path.join(lab.local("p"), "secret.key")
    os.chmod(path, 0)
    try:
        lab.sync("p")
        lab.sync("p")
        initialised, _ = state_of(lab, "p")
        lab.server_write("p", "partner.doc", "theirs")
        lab.sync("p")
    finally:
        os.chmod(path, 0o644)
    print("initialised:", initialised, "\n" + "\n".join(lab.log))
    assert "partner.doc" in lab.server_tree("p"), \
        f"foreign file deleted; initialised={initialised}"


# --------------------------------------------------------------------------- #
# F2: renaming the server entry re-keys every record -> bootstrap -> mirror
#     deletes conflict copies (the only copy of a NAS-side edit) and foreign
#     files, without asking (< 50).
# --------------------------------------------------------------------------- #
def test_f2_renaming_the_server_deletes_conflict_copies(lab):
    from nasync.core.sync import Syncer
    lab.pair("p", {"doc.txt": "my version", "other.txt": "o"})
    lab.sync("p")
    lab.server_write("p", "doc.txt", "partner edited this on the NAS, longer")
    lab.sync("p")
    tree = lab.server_tree("p")
    copies = [r for r in tree if "NAS copy" in r]
    assert copies, tree
    print("conflict copy:", copies)
    lab.server_write("p", "partner-added.txt", "theirs")
    lab.sync("p")
    assert "partner-added.txt" in lab.server_tree("p")
    # Rename the server entry, as the app's server editor allows.
    base = lab.server_path("p")
    lab.config.save_server("home-nas", "127.0.0.1", USER, PASSWORD, SHARE,
                           lab.samba["port"], orig_name="lab", root=lab.root)
    log = []
    Syncer(reporter=log.append, config=lab.config, server="home-nas",
           source="background").sync_pair()
    out = lab.server_exec(f"cd '{base}' && find . -type f -exec sha256sum {{}} +")
    after = {line.split(None, 1)[1][2:] for line in out.splitlines()}
    print("\n".join(log))
    print("server now:", sorted(after))
    assert copies[0] in after, "the NAS-side edit (conflict copy) was deleted"
    assert "partner-added.txt" in after, "a foreign file was deleted"


# --------------------------------------------------------------------------- #
# F3: a case-twin created locally swaps which file is backed up and overwrites
#     the existing backup of the file that is still there.
# --------------------------------------------------------------------------- #
def test_f3_case_twin_overwrites_backup_of_existing_file(lab):
    lab.pair("p", {"notes.txt": "years of notes"})
    lab.sync("p")
    lab.write("p", "Notes.txt", "a different, unrelated file")
    lab.sync("p")
    print("\n".join(lab.log))
    tree = lab.server_tree("p")
    print("server:", tree)
    assert sha("years of notes") in tree.values(), \
        "the backup of notes.txt (still on disk locally) was overwritten"


# --------------------------------------------------------------------------- #
# F4: a "yes" never works first time when the held run also did ordinary
#     overwrites/deletes: the fingerprint changes because they were done.
# --------------------------------------------------------------------------- #
def test_f4_fingerprint_yes_is_refused_after_an_ordinary_edit(lab):
    from nasync.core.sync import Syncer
    files = {f"f{i}.txt": f"file {i}" for i in range(60)}
    files["edit.txt"] = "short"
    lab.pair("p", files)
    lab.sync("p")
    for i in range(60):
        os.remove(os.path.join(lab.local("p"), f"f{i}.txt"))
    lab.write("p", "edit.txt", "edited, longer text")
    first = lab.sync("p")
    held = [r for r in first["refused"] if r.get("fingerprint")]
    assert held, lab.log
    again = Syncer(reporter=lab.log.append, config=lab.config, server="lab",
                   source="app").sync_pair(
        "p", allow_mass_delete={"p": held[0]["fingerprint"]})
    print("second run refused:", again["refused"])
    assert not again["refused"], "the confirmed delete was held back again"


# --------------------------------------------------------------------------- #
# F5: long names (fine before 0.21) can no longer be uploaded or restored:
#     the hidden temp name is ~20 bytes longer than the file's own name.
# --------------------------------------------------------------------------- #
def test_f5_long_file_name_cannot_be_uploaded(lab):
    name = "x" * 240 + ".pdf"                     # 244 bytes: legal everywhere
    lab.pair("p", {name: "paper"})
    result = lab.sync("p")
    print(result, "\n".join(lab.log))
    assert name in lab.server_tree("p"), "a legal 244-byte name was not backed up"


def test_f5b_long_hebrew_name_cannot_be_restored(lab):
    name = "ש" * 118 + ".txt"                 # 240 bytes UTF-8
    lab.pair("p")
    # On the NAS already (uploaded by an older version, or by another tool).
    lab.server_write("p", name, "hebrew doc")
    result = lab.restore("p")
    print(result, "\n".join(lab.log))
    assert os.path.exists(os.path.join(lab.local("p"), name)), \
        "a file on the NAS could not be restored"


# --------------------------------------------------------------------------- #
# F6: a NAS-side same-size edit whose time is exactly one hour after the
#     recorded one is read as a FAT DST shift, and then overwritten without a
#     conflict copy.
# --------------------------------------------------------------------------- #
def test_f6_dst_slack_hides_a_nas_edit(lab):
    t = int(time.time()) - 7200
    lab.pair("p")
    lab.write("p", "plan.txt", "AAAA", mtime=t)
    lab.sync("p")
    lab.server_write("p", "plan.txt", "BBBB", mtime=t + 3600)   # partner's edit
    lab.sync("p")
    lab.write("p", "plan.txt", "CCCC", mtime=t + 7000)          # my later edit
    lab.sync("p")
    tree = lab.server_tree("p")
    print("\n".join(lab.log), tree)
    assert sha("BBBB") in tree.values(), "the NAS-side edit was overwritten"


# --------------------------------------------------------------------------- #
# F7: update mode (or one `--no-delete` run) initialises a pair; files the
#     user had deleted locally become "someone else's" and a later mirror
#     never removes them.
# --------------------------------------------------------------------------- #
def test_f7_update_run_then_mirror_keeps_old_deletions(lab):
    lab.pair("p", {"keep.txt": "k"})
    lab.server_exec(f"mkdir -p '{lab.server_path('p')}' && chmod 777 '{lab.server_path('p')}'")
    # Left on the NAS by an older NASync in update mode; deleted locally long ago.
    lab.server_write("p", "deleted-years-ago.txt", "old")
    lab.sync("p", mirror=False)          # e.g. `nasync sync --no-delete`
    lab.sync("p")                        # mirror
    lab.sync("p")
    print("\n".join(lab.log))
    assert "deleted-years-ago.txt" not in lab.server_tree("p"), \
        "mirror kept a file the user deleted locally (now treated as foreign)"


# --------------------------------------------------------------------------- #
# F8: case-twins on the server (created by a NAS-side process): restore brings
#     both down and records both; the next mirror push drops one as a local
#     case-duplicate and then deletes its backup as 'removed locally'.
# --------------------------------------------------------------------------- #
def test_f8_case_twins_on_server_restore_then_push_deletes_one(lab):
    lab.pair("p", {"z.txt": "z"})
    lab.sync("p")
    lab.server_write("p", "Report.txt", "upper")
    lab.server_write("p", "report.txt", "lower one")
    lab.restore("p")
    print("local after restore:", sorted(os.listdir(lab.local("p"))))
    lab.sync("p")
    print("\n".join(lab.log))
    tree = lab.server_tree("p")
    print("server:", tree)
    assert {"Report.txt", "report.txt"} <= set(tree), \
        "a file present locally lost its backup"


AUDIT = os.path.dirname(os.path.dirname(os.path.abspath(nasync.__file__)))


class Stub:
    enabled = True

    def __init__(self):
        self.sent = []

    def notify(self, summary, body="", urgency="normal"):
        self.sent.append((summary, body, urgency))
        return 1

    def close(self, withdrawer=None):
        pass


# --------------------------------------------------------------------------- #
# G1: live sync + a pair that finishes "with errors" (one unreadable file):
#     sync_now returns True, the pair stays owed, _catch_up_at is reset to None
#     -> catch_up_due() is True on the very next tick: back-to-back full syncs.
# --------------------------------------------------------------------------- #
def test_g1_catch_up_spins_on_a_pair_with_a_permanent_error(lab):
    from nasync.core.automation import Automator
    lab.pair("p", {"a.txt": "a", "root-owned.log": "r"})
    os.chmod(os.path.join(lab.local("p"), "root-owned.log"), 0)
    lines = []
    auto = Automator(reporter=lines.append, config=lab.config, notifier=Stub(),
                     server="lab")
    auto.on_lan = lambda: True
    auto.on_files_changed("p")
    runs = 0
    t0 = time.monotonic()
    for _ in range(5):
        if not auto.catch_up_due():
            break
        auto.catch_up()
        runs += 1
    elapsed = time.monotonic() - t0
    os.chmod(os.path.join(lab.local("p"), "root-owned.log"), 0o644)
    syncs = sum("Auto-sync at" in line for line in lines)
    print(f"{syncs} automatic syncs in {elapsed:.1f}s; still due: "
          f"{auto.catch_up_due()}; _catch_up_at={auto._catch_up_at}")
    assert syncs < 3, "catch-up re-runs a failing pair back-to-back with no pause"


# --------------------------------------------------------------------------- #
# G2: the app/CLI killed mid-restore (window closed, logout, OOM, SIGKILL)
#     leaves .<name>.nasync-part-<pid> in the local folder. Push never filters
#     own temps locally, so it is uploaded -- and re-uploaded on every run.
# --------------------------------------------------------------------------- #
RESTORE_SCRIPT = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from nasync.system import vault
vault.backend = lambda: ""
from nasync.core.config import Config
from nasync.core.sync import Syncer
Syncer(config=Config(), server="lab", source="cli",
       reporter=lambda m: None).restore_pair("p")
"""


def test_g2_killed_restore_leaves_a_partial_that_is_uploaded_every_run(lab):
    big = os.urandom(64 * 1024 * 1024)
    lab.pair("p", {"video.bin": big})
    lab.sync("p")
    os.remove(os.path.join(lab.local("p"), "video.bin"))
    env = dict(os.environ)
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    proc = subprocess.Popen([sys.executable, "-I", "-c", RESTORE_SCRIPT, AUDIT],
                            env=env)
    folder = lab.local("p")
    deadline = time.monotonic() + 60
    partial = None
    while time.monotonic() < deadline and partial is None:
        for name in os.listdir(folder):
            if ".nasync-part-" in name and os.path.getsize(
                    os.path.join(folder, name)) > 4 * 1024 * 1024:
                partial = name
        time.sleep(0.05)
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    print("left locally:", os.listdir(folder))
    assert partial, "no partial download observed"
    first = lab.sync("p")
    second = lab.sync("p")
    print("\n".join(lab.log))
    print("server:", sorted(lab.server_tree("p")))
    print("transferred run1/run2:", first["transferred"], second["transferred"])
    assert second["transferred"] == 0, "the partial download is re-uploaded every run"


# --------------------------------------------------------------------------- #
# G3: Ctrl+C (KeyboardInterrupt) between "old copy moved aside" and "new copy
#     moved in": the old copy is not put back (except Exception), the new temp
#     is removed (except BaseException). The file is missing under its name;
#     a restore silently omits it; the next push deletes the aside copy first.
# --------------------------------------------------------------------------- #
def test_g3_interrupted_swap_loses_the_file_from_restore_and_update_backup(lab):
    import smbclient
    lab.config.set_sync_mode("update", server="lab")
    lab.pair("p", {"thesis.tex": "v1 " * 100})
    lab.sync("p")
    lab.write("p", "thesis.tex", "v2 " * 120)
    real_rename = smbclient.rename
    state = {"aside": False}

    def rename(src, dst, **kw):
        if ".nasync-old-" in dst:
            real_rename(src, dst, **kw)
            state["aside"] = True
            return
        if state["aside"] and ".nasync-part-" in src:
            raise KeyboardInterrupt()          # Ctrl+C lands here
        return real_rename(src, dst, **kw)

    smbclient.rename = rename
    try:
        with pytest.raises(KeyboardInterrupt):
            lab.sync("p")
    finally:
        smbclient.rename = real_rename
    print("server after Ctrl+C:", lab.server_exec(
        f"ls -A '{lab.server_path('p')}'"))
    # The user, in update mode, deletes the local file (it's "backed up").
    os.remove(os.path.join(lab.local("p"), "thesis.tex"))
    res = lab.restore("p")
    print("restore:", res, "\n".join(lab.log))
    restored = os.path.exists(os.path.join(lab.local("p"), "thesis.tex"))
    lab.sync("p")                        # update mode: never deletes, right?
    print("\n".join(lab.log))
    print("server after next sync:", lab.server_exec(
        f"ls -A '{lab.server_path('p')}'"))
    assert restored, "restore silently omitted a file whose backup exists"


# --------------------------------------------------------------------------- #
# G4: Samba's "server GUID" is its NetBIOS name. Two different NASes with the
#     same host name (raspberrypi, openmediavault, ...) have the same identity:
#     the identity check passes for a stranger, and two separate NASes with
#     the same share name are treated as one -> backups to both skipped.
# --------------------------------------------------------------------------- #
def _container(engine, hostname, user, password):
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    name = f"aud2-{uuid.uuid4().hex[:8]}"
    subprocess.run([engine, "run", "-d", "--rm", "--name", name, "--hostname",
                    hostname, "-p", f"{port}:445", IMAGE, "-u",
                    f"{user};{password}", "-s",
                    f"{SHARE};/share;yes;no;no;{user}", "-p"],
                   check=True, capture_output=True)
    import smbclient
    deadline = time.monotonic() + 60
    while True:
        try:
            smbclient.register_session("127.0.0.1", username=user,
                                       password=password, port=port)
            smbclient.listdir(f"\\\\127.0.0.1\\{SHARE}", port=port)
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)
    subprocess.run([engine, "exec", name, "sh", "-c", "chmod 777 /share"])
    return name, port


def test_g4_two_nases_with_the_same_hostname(lab):
    from nasync.core.samba import SAMBAClient
    engine = lab.samba["engine"]
    home, home_port = _container(engine, "raspberrypi", "alice", "homepw")
    away, away_port = _container(engine, "raspberrypi", "mallory", "evilpw")
    try:
        cfg = lab.config
        cfg.add_or_update_server("home", "127.0.0.1", "alice", "homepw", SHARE,
                                 home_port)
        cfg.add_or_update_server("offsite", "127.0.0.1", "alice", "homepw",
                                 SHARE, away_port)
        # Identity check: a different machine, same host name.
        cfg.set_server_identity("home", SAMBAClient(
            "127.0.0.1", "alice", "homepw", SHARE, port=home_port).server_identity())
        stranger = SAMBAClient("127.0.0.1", "alice", "homepw", SHARE,
                               port=away_port,
                               identity=cfg.get_server_config("home")["identity"])
        try:
            stranger.connect()
            outcome = "connected"
        except Exception as e:
            outcome = f"{type(e).__name__}: {e}"
        print("connect to stranger with home's identity:", outcome)
        # Two NASes, same share name, same folder layout (3-2-1 backups).
        folder = os.path.join(str(lab.home), "src", "Photos")
        os.makedirs(folder)
        with open(os.path.join(folder, "img.jpg"), "w") as f:
            f.write("pixels")
        cfg.add_pair(folder, "Photos", "Photos", server="home")
        cfg.set_server_identity("offsite", SAMBAClient(
            "127.0.0.1", "alice", "homepw", SHARE, port=away_port).server_identity())
        try:
            cfg.add_pair(folder, "Photos", "Photos", server="offsite")
            add = "added"
        except ValueError as e:
            add = f"refused: {e}"
        print("adding the offsite pair:", add)
        print("same_storage(home, offsite):", cfg.same_storage("home", "offsite"))
        assert not cfg.same_storage("home", "offsite"), \
            "two different NASes treated as one"
        assert add == "added", add
        # Samba's identity is its host name, so a stranger *with the same
        # name* can't be told apart (documented in DATA_SAFETY.md). One with
        # another name must still be refused before any password is sent.
        assert not outcome.startswith("ServerIdentityChanged"), outcome
        other, other_port = _container(engine, "not-my-nas", "alice", "homepw")
        try:
            imposter = SAMBAClient("127.0.0.1", "alice", "homepw", SHARE,
                                   port=other_port,
                                   identity=cfg.get_server_config("home")["identity"])
            with pytest.raises(Exception) as caught:
                imposter.connect()
            assert type(caught.value).__name__ == "ServerIdentityChanged", caught.value
        finally:
            subprocess.run([engine, "rm", "-f", other], capture_output=True)
    finally:
        subprocess.run([engine, "rm", "-f", home, away], capture_output=True)
