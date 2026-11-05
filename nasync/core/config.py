import functools
import json
import os
import shutil
import socket
from pathlib import Path
import appdirs

from nasync.system import vault
from nasync.system.portal import in_flatpak


# How a push decides what to do with files that exist on the server but not
# locally. "mirror" deletes them so the destination matches the source exactly;
# "update" leaves them alone, so a push only ever adds and overwrites.
SYNC_MODES = ("mirror", "update")
DEFAULT_SYNC_MODE = "mirror"

# When scheduled sync is switched on without a time being chosen. Deliberately
# small hours: the machine is usually idle and the network quiet.
DEFAULT_SYNC_TIME = "02:00"

#: The key a server's sync root is stored under. Every pair on that server
#: syncs *inside* this path, so anything on the share beside it is structurally
#: unreachable — not "no pair happens to point there", but outside the set of
#: paths a pair is able to name at all.
#:
#: This exists because mirror mode deletes, and what used to stand between a
#: mirror sync and an untouched folder was only that no pair pointed at it.
#: That is a promise until someone changes it, and the change that breaks it —
#: adding a pair with the wrong destination — looks entirely ordinary. A root
#: turns "nothing points there" into "nothing can point there".
#:
#: Empty means the whole share, which is what every server configured before
#: 0.19.0 has. Those keep working exactly as they did: the guarantee is opted
#: into, not imposed on a share layout NASync cannot see.
SERVER_ROOT = "root"

#: The settings that belong to a server rather than to the app, with the value
#: an unconfigured server takes. Kept in one place because three things have to
#: agree about them: what a new server defaults to, what the 0.18.0 migration
#: writes, and which keys must survive `add_or_update_server` rebuilding a
#: server entry — a rebuild that would otherwise drop the sync root, which is
#: the one field whose loss is dangerous rather than merely annoying.
#:
#: `mirror` is the default mode for the same reason it always was — it is what
#: a push did before the mode existed — and both triggers default to off,
#: because opting someone into an unattended sync that deletes on the server is
#: not a default.
SERVER_SETTINGS = {
    SERVER_ROOT: "",
    "sync_mode": DEFAULT_SYNC_MODE,
    "live_sync": False,
    "scheduled_sync": False,
    "scheduled_time": DEFAULT_SYNC_TIME,
    # Date ("YYYY-MM-DD") of the last scheduled run that actually ran, so a
    # missed one can catch up. Per server: one NAS being unreachable must not
    # mark another's day as done.
    "last_scheduled_run": None,
    # Pre-0.5.0 "every N minutes" trigger, still honoured by `auto`.
    "interval_minutes": 0,
}


def require_time(value: str) -> str:
    """Validate a user-supplied "HH:MM", raising ValueError if it isn't one."""
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Invalid time '{value}'. Use HH:MM, e.g. 02:00.")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time '{value}'. Use HH:MM, e.g. 02:00.")
    return f"{hour:02d}:{minute:02d}"


def normalize_time(value, fallback: str = DEFAULT_SYNC_TIME) -> str:
    """Like `require_time`, but a bad stored value falls back instead of raising."""
    if value is None:
        return fallback
    try:
        return require_time(value)
    except ValueError:
        return fallback


def normalize_remote(path: str) -> str:
    """A share-relative path with no leading/trailing slashes and no "." parts.

    Rejects anything that climbs upwards. A pair whose destination could
    contain ".." would be able to name a folder outside its server's sync
    root, which is the entire thing the root exists to prevent.
    """
    # Whitespace as well as slashes: these arrive from text fields and from
    # people pasting paths, and " /backups/ " must mean the same folder as
    # "backups". Stripped per segment too, but only at the edges — a folder
    # genuinely called "Marrige video" has to survive intact.
    cleaned = (path or "").replace("\\", "/").strip().strip("/")
    parts = [part.strip() for part in cleaned.split("/")]
    parts = [part for part in parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(
            "A server path can't contain '..' — it has to stay inside the "
            "sync root."
        )
    return "/".join(parts)


def _mutator(method):
    """Apply a change to the config *as it is on disk now*, then save it.

    The desktop app holds one `Config` for as long as it is open, and the CLI
    and the background service write the same file meanwhile. Until 0.21.0 a
    change in the app was applied to its in-memory copy from whenever it
    opened and saved whole — so a mode switched to "update" from a terminal,
    a pair removed, a root moved, all came silently back the next time
    anything was toggled in the app, and the service mirrored with them.

    Only the outermost call re-reads: a mutator that calls another must not
    discard the half-made change it is in the middle of.

    A change that fails is undone in memory too: the copy is re-read from
    disk. Until 0.22.1 a removal that failed on a locked keyring left the
    server half-removed in memory, and the next unrelated save wrote it out.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        outermost = not self._mutating and not self._initialising
        if outermost:
            self.refresh()
        self._mutating += 1
        try:
            return method(self, *args, **kwargs)
        except BaseException:
            if outermost:
                self.reload()
            raise
        finally:
            self._mutating -= 1
    return wrapper


def identity_is_weak(identity: str) -> bool:
    """True when a server's identity is only its name, not a unique id.

    Windows gives each server a random GUID. Samba sends its NetBIOS name
    padded with zeros ("raspberrypi\\0\\0…"), so every Samba box left at
    the same default host name looks like the same machine.
    """
    import uuid
    try:
        raw = uuid.UUID(str(identity)).bytes
    except ValueError:
        return True
    text = raw.rstrip(b"\0")
    return not text or (len(text) < len(raw) and all(32 < b < 127 for b in text))


@functools.lru_cache(maxsize=64)
def _addresses(host: str) -> frozenset:
    """Every address a host name stands for ("localhost" is 127.0.0.1)."""
    host = (host or "").strip().casefold()
    try:
        found = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (OSError, UnicodeError):
        found = set()
    return frozenset(found | {host})


def destinations_overlap(first: str, second: str) -> bool:
    """True when one server folder is the other, or lies inside it.

    Two such pairs cannot both be mirrored: each run makes the folder match
    its own local side and deletes the other pair's files as "removed
    locally". On a real NAS that cost 57,000 files a night, re-uploaded and
    deleted again on every run. Compared case-insensitively, because the
    share does: "Pictures" and "pictures" are one folder there.
    """
    a = [part.casefold() for part in normalize_remote(first).split("/") if part]
    b = [part.casefold() for part in normalize_remote(second).split("/") if part]
    depth = min(len(a), len(b))
    return a[:depth] == b[:depth]


def overlap_message(dest: str, other_name: str, other_dest: str) -> str:
    """Why a destination can't be used, said in terms of what would be lost."""
    shown = dest or "/"
    other_shown = other_dest or "/"
    if normalize_remote(dest).casefold() == normalize_remote(other_dest).casefold():
        return (f"'{shown}' is already the server folder of pair "
                f"'{other_name}'. Two pairs in one folder delete each other's "
                f"files in mirror mode — give each pair a folder of its own.")
    if len(normalize_remote(dest)) > len(normalize_remote(other_dest)):
        return (f"'{shown}' is inside the server folder of pair "
                f"'{other_name}' ('{other_shown}'), whose mirror sync would "
                f"delete everything this pair puts there. Choose a folder "
                f"beside it instead.")
    return (f"'{shown}' contains the server folder of pair '{other_name}' "
            f"('{other_shown}'), so this pair's mirror sync would delete "
            f"everything that one puts there. Choose a folder beside it "
            f"instead.")


def join_remote(root: str, path: str) -> str:
    """Join a pair's destination onto its sync root, staying inside it."""
    root = normalize_remote(root)
    rest = normalize_remote(path)
    if not root:
        return rest
    return f"{root}/{rest}" if rest else root


def host_config_dir() -> Path:
    """Where a pip or pipx install keeps its config, as the sandbox sees it.

    Flatpak leaves ``HOME`` pointing at the real home directory but redirects
    ``XDG_CONFIG_HOME`` into ``~/.var/app/<app-id>/config``, so the two installs
    look in completely different places. This is the *other* one — spelled out
    rather than read from the environment, because inside the sandbox the
    environment describes the sandbox.

    A host with a custom ``XDG_CONFIG_HOME`` is not discoverable from in here;
    those configs are simply not found, which is the same as before.
    """
    return Path.home() / ".config" / "nasync"


class Config:
    """Persistent config: multiple *named* servers (one active) + sync pairs.

    Passwords live in a separate ``.creds`` file (mode 0600), never in
    ``config.json``. Since 0.11.0 that file is **encrypted** wherever a keyring
    or the secret portal can hold a key — see :mod:`nasync.vault`, which also
    explains what happens where neither can. The ``SAMBA_PASSWORD`` env var, if
    set, overrides the active server's password.

    Sync pairs belong to a server, not to the config as a whole: each server
    keeps its own list under ``pairs_by_server``, so switching servers can't
    silently retarget folders at a NAS they were never meant for.
    """

    def __init__(self, interactive: bool = True):
        self._mutating = 0
        self._initialising = True
        self._disk_stamp = None
        #: May reading the saved passwords prompt to unlock a keyring? Not in
        #: the background service: a prompt at 02:00 blocks the run for a
        #: minute, and the empty password it fell back to was then reported
        #: as "Authentication failed. Check username and password."
        self.interactive = interactive
        self.config_dir = Path(appdirs.user_config_dir("nasync"))
        self.config_file = self.config_dir / "config.json"
        self.creds_file = self.config_dir / ".creds"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        #: True only on the run that adopted a pip install's config — see
        #: `_import_from_host`. Front-ends mention it once; nothing persists it.
        self.imported_from_host = self._import_from_host()
        #: Why the saved passwords couldn't be read, as a sentence, or None.
        #: Front-ends show this: an empty credential map and an unreadable one
        #: look identical at the point of failure ("wrong password"), and only
        #: one of them is fixable by unlocking a keyring.
        self.credentials_error = None
        self._creds_cache = None
        self._load()
        self._migrate_legacy()
        self._migrate_creds()
        self._initialising = False

    def _import_from_host(self) -> bool:
        """Adopt a pip/pipx install's settings the first time we run sandboxed.

        Under Flatpak, ``XDG_CONFIG_HOME`` points inside ``~/.var/app``, so an
        existing install's servers and pairs are not where we look. Without
        this, someone moving to the Flatpak opens it to an empty app and has to
        re-enter every server and re-pick every folder — and re-picking folders
        in mirror mode is the operation with the worst failure mode we have.

        Three properties make this safe to do automatically:

          * **Copied, never moved.** The pip install keeps working untouched,
            so this is reversible by uninstalling the Flatpak.
          * **Only when we have nothing.** It cannot overwrite anything the
            sandboxed app has saved, because it doesn't run once there is a
            config to overwrite.
          * **Never fatal.** An unreadable host config leaves the app starting
            empty, exactly as it would have before.

        Passwords come too. Servers arriving without them would look imported
        but fail to connect, which is worse than not importing at all.
        """
        if not in_flatpak() or self.config_file.exists():
            return False

        source = host_config_dir()
        try:
            # Outside a real sandbox — or with FLATPAK_ID set by hand to
            # exercise this path — the two can be the same directory, and
            # copying a file onto itself would truncate it.
            if source.resolve() == self.config_dir.resolve():
                return False
            if not (source / "config.json").is_file():
                return False
            shutil.copyfile(source / "config.json", self.config_file)
            if (source / ".creds").is_file():
                shutil.copyfile(source / ".creds", self.creds_file)
                # copyfile doesn't carry permissions, and this file is the
                # reason the directory is 0600 in the first place.
                os.chmod(self.creds_file, 0o600)
        except OSError:
            return False
        return True

    # ------------------------------------------------------------------ #
    # Load / save
    # ------------------------------------------------------------------ #
    def _stamp(self):
        try:
            info = self.config_file.stat()
            return (info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            return None

    def _load(self):
        if self.config_file.exists():
            with open(self.config_file) as f:
                self.data = json.load(f)
        else:
            self.data = {}
        self._disk_stamp = self._stamp()
        self.data.setdefault("servers", [])
        self.data.setdefault("active_server", None)
        # {server name: [pair, ...]}. Pairs belong to one server; see
        # `_migrate_pairs` for how the pre-0.6.0 global list is folded in.
        self.data.setdefault("pairs_by_server", {})

    def reload(self):
        """Re-read config.json, discarding the in-memory copy.

        Long-lived processes (the background service) hold a `Config` for days
        while the desktop app writes to the same file. Without this they would
        keep acting on the settings as they were at start-up.

        Drops the decrypted credential cache too: a password changed in the GUI
        reaches the service the same way every other setting does.
        """
        self._creds_cache = None
        self._load()

    def refresh(self) -> bool:
        """Re-read config.json if another process changed it. True if it did."""
        if self._stamp() == self._disk_stamp:
            return False
        self.reload()
        return True

    def save(self):
        """Write atomically: a crash mid-write must not leave half a config.

        A truncated config.json reads as *no* config — no servers, no pairs —
        and the next save would make that permanent.
        """
        temp = self.config_file.with_name(f".{self.config_file.name}.{os.getpid()}.tmp")
        with open(temp, "w") as f:
            json.dump(self.data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.config_file)
        self._disk_stamp = self._stamp()

    def _migrate_legacy(self):
        """Convert the old single-server format to the named-servers model."""
        self._migrate_automation()

        legacy = self.data.pop("server", None)
        if legacy and legacy.get("ip"):
            password = self._read_legacy_password()
            name = legacy.get("name") or legacy.get("share") or "default"
            self.add_or_update_server(
                name, legacy["ip"], legacy.get("username", ""), password,
                legacy.get("share", ""), legacy.get("port", 445),
            )
            self.data["active_server"] = name
            self.save()

        self._migrate_pairs()
        self._migrate_server_settings()

    def _migrate_server_settings(self):
        """Move the app-wide sync mode and triggers onto the active server.

        Before 0.18.0 there was one mode and one schedule, and whichever server
        was active is the one they actually governed — so that is where they
        belong. Runs after `_migrate_pairs` so a config coming from the
        single-server format has a server to attach them to.

        **Every other server starts with automation off.** Inheriting the old
        triggers would begin unattended syncing on a NAS nobody asked to
        automate, in mirror mode, which deletes. Nothing that was not syncing
        by itself before the upgrade starts doing so because of it.

        `background` stays in the global block: there is one service.
        """
        automation = self.data.get("automation", {})
        movable = {key: automation[key] for key in SERVER_SETTINGS
                   if key in automation}
        if "sync_mode" in self.data:
            movable["sync_mode"] = self.data["sync_mode"]
        if not movable:
            return

        target = self._server_entry()
        if target:
            # setdefault, not update: a server already carrying its own value
            # has been through this once, and the global block is the older
            # fact. Re-running must not overwrite a since-changed setting.
            for key, value in movable.items():
                target.setdefault(key, value)
        # The mode is different from the triggers: it governed *every* server
        # before 0.18.0, so every server keeps it. Giving it to the active one
        # alone turned a colleague's "update" share into a mirror one, whose
        # next sync deleted their files. An upgrade must never make a server
        # more destructive than it was.
        if "sync_mode" in movable:
            for server in self.data.get("servers", []):
                server.setdefault("sync_mode", movable["sync_mode"])

        # Dropped whether or not there was a server to receive them: keeping
        # them would leave two sources of truth, and with no servers there is
        # nothing they could have been describing.
        self.data.pop("sync_mode", None)
        for key in SERVER_SETTINGS:
            automation.pop(key, None)
        if automation:
            self.data["automation"] = automation
        else:
            self.data.pop("automation", None)
        self.save()

    def _migrate_pairs(self):
        """Give the pre-0.6.0 global pair list to the server it was syncing to.

        Runs after the server migration above, so a config coming all the way
        from the single-server format still finds a server to attach to. If
        there is no server yet (pairs added before any `init`), the old list is
        left alone and picked up the next time this runs — by then adding a
        server will have given it somewhere to go.
        """
        legacy = self.data.get("pairs")
        if not legacy:
            self.data.pop("pairs", None)
            return

        servers = self.data.get("servers", [])
        target = self.get_active_server_name() or (servers[0]["name"] if servers else None)
        if not target:
            return

        owned = self.data["pairs_by_server"].setdefault(target, [])
        taken = {pair["name"] for pair in owned}
        owned.extend(pair for pair in legacy if pair["name"] not in taken)
        self.data.pop("pairs", None)
        self.save()

    def _migrate_automation(self):
        """Fold the pre-0.5.0 `enabled`/`on_change` pair into `live_sync`.

        Someone running `nasync auto` before the rename had watching switched
        on; they keep it. Everyone else lands on the new default of off.
        """
        automation = self.data.get("automation")
        if not isinstance(automation, dict) or "enabled" not in automation:
            return
        was_watching = bool(automation.pop("enabled", False)) and \
            bool(automation.pop("on_change", True))
        automation.pop("on_change", None)
        automation.setdefault("live_sync", was_watching)
        self.data["automation"] = automation
        self.save()

    # ------------------------------------------------------------------ #
    # Credentials (name -> password), stored 0600
    # ------------------------------------------------------------------ #
    def _read_creds(self, allow_prompt: bool = None) -> dict:
        """The name→password map, decrypting it if it is sealed.

        Cached for the life of the `Config`, because every read of a sealed file
        costs a round trip to the keyring or the portal and `get_server` is
        called from the UI thread. `reload()` drops the cache, which is what
        lets the background service pick up a password changed in the GUI.

        A failure to decrypt is recorded in `credentials_error` and read as an
        empty map: refusing to start because the keyring is locked would make
        an unrelated problem fatal to a tool that can still list servers, edit
        pairs and show its settings.
        """
        if self._creds_cache is not None:
            return self._creds_cache

        if not self.creds_file.exists():
            return {}
        text = self.creds_file.read_text()
        try:
            data = json.loads(text)
        except ValueError:
            return {}  # legacy plaintext (single password) — handled on migrate
        if not isinstance(data, dict):
            return {}

        if allow_prompt is None:
            allow_prompt = self.interactive
        if vault.is_envelope(data):
            try:
                data = vault.unseal(data, allow_prompt=allow_prompt)
            except vault.VaultError as e:
                self.credentials_error = str(e)
                return {}

        self.credentials_error = None
        self._creds_cache = data
        return data

    def _read_legacy_password(self) -> str:
        if not self.creds_file.exists():
            return ""
        text = self.creds_file.read_text()
        try:
            json.loads(text)
            return ""  # already the new JSON format
        except ValueError:
            return text.strip()  # old plaintext single password

    def _file_is_sealed(self) -> bool:
        """True when the `.creds` on disk is encrypted right now."""
        if not self.creds_file.exists():
            return False
        try:
            return vault.is_envelope(json.loads(self.creds_file.read_text()))
        except ValueError:
            return False

    def _write_creds(self, creds: dict):
        """Seal and write the credential map, leaving the file 0600 either way.

        The permissions matter even when the contents are encrypted: they are
        the only protection left on a machine with no keyring, and they cost
        nothing on one that has.

        Two refusals guard this, because every caller does read-modify-write
        and a bad write here destroys passwords that cannot be recovered:

          * **If the last read failed, refuse.** The map in hand is missing
            every entry we couldn't decrypt, so writing it back deletes them.
            Saving one server would have silently discarded the passwords of
            all the others, and `remove_server` would have emptied the file.
          * **Never write plaintext over an encrypted file.** `vault.seal`
            passes the map through unsealed when no keyring is reachable, which
            is right for a machine that never had one and wrong for a machine
            whose keyring is merely absent this session.
        """
        if self.credentials_error:
            raise vault.VaultError(
                f"Not saving passwords, because the ones already stored can't "
                f"be read ({self.credentials_error}) — writing now would delete "
                f"them. Nothing has been changed. Unlock your keyring and try "
                f"again, or run 'nasync credentials --reset' to discard the "
                f"saved passwords and enter them fresh.")

        try:
            payload = vault.seal(creds)
        except vault.VaultError as e:
            # Refusing to write is the safe failure. Falling back to plaintext
            # here would quietly downgrade a file that was encrypted a moment
            # ago, which is the opposite of what someone saving a password
            # expects to happen.
            self.credentials_error = str(e)
            raise
        if not vault.is_envelope(payload) and self._file_is_sealed():
            raise vault.VaultError(
                "Not saving passwords: these are stored encrypted, and no "
                "keyring is available right now to encrypt them again. Saving "
                "would write them to disk in the clear.")

        with open(self.creds_file, "w") as f:
            json.dump(payload, f)
        os.chmod(self.creds_file, 0o600)
        self._creds_cache = dict(creds)
        self.credentials_error = None

    def _migrate_creds(self):
        """Encrypt a pre-0.11.0 plaintext `.creds` in place, once.

        Only when there is somewhere to keep a key: on a machine without one
        the file stays as it is and `credentials_backend()` reports that
        honestly. Never fatal — an install that can't be upgraded keeps working
        with the file it has.
        """
        if not self.creds_file.exists():
            return
        try:
            data = json.loads(self.creds_file.read_text())
        except ValueError:
            return  # legacy single password; _migrate_legacy rewrites it
        if not isinstance(data, dict) or vault.is_envelope(data) or not data:
            return
        # Asked *last*, deliberately. `vault.backend()` opens a session-bus
        # connection and queries the keyring, and this runs in `Config()` —
        # which the GUI constructs on the main thread and every CLI command
        # constructs at startup. Testing it first meant that cost was paid on
        # every run by everyone, when only an unencrypted file can act on the
        # answer. An already-sealed file now returns above without any D-Bus.
        if not vault.backend():
            return
        try:
            self._write_creds(data)
        except vault.VaultError:
            pass

    def check_credentials(self) -> str:
        """Try to read the saved passwords; return the problem, or "".

        Front-ends call this where they intend to *report* — `credentials_error`
        is only ever set by a read, and nothing on the way to a status screen
        reads a password, so checking the flag without this first would always
        see None and the warning would never appear.

        Deliberately not done in `__init__`. Every `Config()` would then reach
        the keyring, and a locked one would put an unlock prompt in front of
        somebody who only ran `nasync list`. `allow_prompt=False` keeps this
        specific check silent for the same reason.
        """
        self._read_creds(allow_prompt=False)
        return self.credentials_error or ""

    @_mutator
    def reset_credentials(self) -> int:
        """Discard the saved passwords entirely. Returns how many were lost.

        The deliberate way out of an unopenable `.creds`. Without it the
        refusal in `_write_creds` is a trap: the recovery for a file sealed by
        a different install is to re-enter the passwords, and re-entering one
        is a *write*, which the refusal would block forever.

        Only ever called because someone asked for it — the count is returned
        so a front-end can say what it is about to throw away.
        """
        lost = 0
        if self.creds_file.exists():
            try:
                data = json.loads(self.creds_file.read_text())
                # An unopenable envelope hides its own count, which is rather
                # the point of it; servers are the honest upper bound.
                lost = (len(data) if isinstance(data, dict)
                        and not vault.is_envelope(data)
                        else len(self.data.get("servers", [])))
            except ValueError:
                lost = 1
            self.creds_file.unlink()
        self._creds_cache = None
        self.credentials_error = None
        return lost

    def credentials_backend(self) -> str:
        """Where the key lives: "portal", "keyring", or "" for none."""
        return vault.backend()

    def credentials_status(self) -> str:
        """One line describing how the saved passwords are protected."""
        return vault.describe()

    # ------------------------------------------------------------------ #
    # Servers
    # ------------------------------------------------------------------ #
    @_mutator
    def add_or_update_server(self, name: str, ip: str, username: str,
                             password: str, share: str, port: int = 445,
                             encrypt: bool = False, root: str = None) -> str:
        # The password is written first, deliberately. Sealing it can fail — a
        # keyring that went away mid-session — and doing it before anything is
        # mutated means that failure leaves the config exactly as it was,
        # rather than recording a server whose password never got stored.
        creds = self._read_creds()
        creds[name] = password or ""
        self._write_creds(creds)

        entry = {
            "name": name,
            "ip": ip,
            "username": username,
            "share": share,
            "port": int(port),
            # Absent from every server saved before 0.12.0, which reads as off.
            # Defaulting to *on* would break the connection to any NAS too old
            # to encrypt, turning a security improvement into an outage.
            "encrypt": bool(encrypt),
        }
        # This rebuilds the entry from its arguments, so anything stored on the
        # server that isn't one of them — the sync mode and the automatic-sync
        # triggers — has to be carried over explicitly. Without this, editing a
        # server's port in the GUI would silently switch its automation off.
        existing = next((s for s in self.data["servers"] if s["name"] == name), None)
        if existing:
            entry.update({key: existing[key] for key in SERVER_SETTINGS
                          if key in existing})
            if existing.get("identity"):
                entry["identity"] = existing["identity"]
        # Given explicitly, it wins — that is how a server gets its root at
        # creation, and how the editor changes one.
        if root is not None:
            entry[SERVER_ROOT] = normalize_remote(root)

        self.data["servers"] = [s for s in self.data["servers"] if s["name"] != name]
        self.data["servers"].append(entry)

        if not self.data.get("active_server"):
            self.data["active_server"] = name
        self.save()
        return name

    # Name-less convenience wrapper, kept for callers that configure a server
    # without choosing a name for it — `scripts/local-test.sh` does this to set
    # up a throwaway container. The name falls out of the share.
    @_mutator
    def set_server(self, ip: str, username: str, password: str, share: str,
                   port: int = 445, name: str = None, encrypt: bool = False) -> str:
        name = name or share or ip or "default"
        return self.add_or_update_server(name, ip, username, password, share,
                                         port, encrypt)

    @_mutator
    def save_server(self, name: str, ip: str, username: str, password: str,
                    share: str, port: int = 445, orig_name: str = None,
                    encrypt: bool = False, root: str = None) -> str:
        """Add or update a server, renaming in place when the name changed.

        The rename path exists so editing a server's name doesn't lose the
        pairs filed under the old one — remove-then-add would.
        """
        if orig_name and orig_name != name:
            pairs = self.data["pairs_by_server"].pop(orig_name, [])
            # Same reasoning as the pairs above: remove-then-add would drop the
            # server's mode and triggers, so a rename would quietly stop it
            # syncing by itself.
            previous = self.get_server(orig_name, with_password=False) or {}
            settings = {key: previous[key] for key in SERVER_SETTINGS
                        if key in previous}
            old_entry = self._server_entry(orig_name)
            if old_entry.get("identity"):
                settings["identity"] = old_entry["identity"]
            old_share = old_entry.get("share") or ""

            self.remove_server(orig_name, _keep_record=True)
            self.add_or_update_server(name, ip, username, password, share, port,
                                      encrypt, root=root)
            if pairs:
                self.data["pairs_by_server"][name] = pairs
            if settings:
                self._server_entry(name).update(settings)
            if pairs or settings:
                self.save()
            # Last, once nothing above can fail and leave it half done. The
            # record is filed under the entry's name; without this a rename
            # sent every pair back to bootstrap, where mirror deletes others'
            # files. Another share is other files: its record isn't carried.
            from nasync.core import state
            if old_share.casefold() == (share or "").casefold():
                state.rename_server(self.config_dir, orig_name, name, old_share)
            else:
                state.forget_server(self.config_dir, orig_name)
        else:
            self.add_or_update_server(name, ip, username, password, share, port,
                                      encrypt, root=root)
        return name

    @_mutator
    def remove_server(self, name: str, _keep_record: bool = False):
        # The saved passwords first: reading them is what fails (a locked
        # keyring), and nothing is changed before it does.
        creds = self._read_creds()
        creds.pop(name, None)
        self._write_creds(creds)
        self.data["servers"] = [s for s in self.data["servers"] if s["name"] != name]
        # The pairs belonged to this server; nothing else can sync them.
        self.data["pairs_by_server"].pop(name, None)
        if self.data.get("active_server") == name:
            self.data["active_server"] = (
                self.data["servers"][0]["name"] if self.data["servers"] else None
            )
        self.save()
        if not _keep_record:
            # After the save: a record dropped for a server that stays would
            # send its pairs back to bootstrap (third audit, #6).
            from nasync.core import state
            state.forget_server(self.config_dir, name)

    def list_servers(self) -> list:
        return self.data.get("servers", [])

    def get_active_server_name(self):
        return self.data.get("active_server")

    @_mutator
    def set_active_server(self, name: str) -> bool:
        if any(s["name"] == name for s in self.data["servers"]):
            self.data["active_server"] = name
            self.save()
            return True
        return False

    def get_server(self, name: str, with_password: bool = True):
        for s in self.data["servers"]:
            if s["name"] == name:
                result = dict(s)
                # Normalised here rather than at each call site: every server
                # saved before 0.12.0 lacks the key entirely, and a caller that
                # forgot the default would read a missing flag as a KeyError or,
                # worse, as something truthy.
                result["encrypt"] = bool(s.get("encrypt", False))
                if with_password:
                    result["password"] = (
                        os.environ.get("SAMBA_PASSWORD")
                        or self._read_creds().get(name, "")
                    )
                return result
        return None

    def get_server_config(self, server: str = None) -> dict:
        """One server's connection details, defaulting to the active one.

        Empty fields when there is no such server, so a caller can check what
        it needs rather than handling a None.
        """
        name = server or self.get_active_server_name()
        server = self.get_server(name) if name else None
        if not server:
            return {"ip": None, "username": None, "password": "",
                    "share": None, "port": 445, "encrypt": False}
        return {
            "ip": server.get("ip"),
            "username": server.get("username"),
            "password": server.get("password", ""),
            "share": server.get("share"),
            "port": server.get("port", 445),
            "encrypt": bool(server.get("encrypt", False)),
            "identity": server.get("identity"),
        }

    @_mutator
    def set_server_identity(self, name: str, identity) -> None:
        """Remember (or, with None, forget) which machine a server is.

        Learned on first contact and checked on every one after it, before any
        password is sent — see `SAMBAClient.connect`. Forgetting it is how a
        replaced NAS is trusted: the next connection learns the new one.
        """
        entry = self._server_entry(name)
        if not entry:
            return
        if identity:
            entry["identity"] = identity
        else:
            entry.pop("identity", None)
        self.save()

    # ------------------------------------------------------------------ #
    # Sync mode
    # ------------------------------------------------------------------ #
    def _server_entry(self, name: str = None) -> dict:
        """The stored dict for a server, by name or the active one.

        The live dict, not a copy — callers write settings through it. Returns
        an empty throwaway when there is no such server, so reading a setting
        with nothing configured yields the defaults instead of raising.
        """
        name = name or self.get_active_server_name()
        for server in self.data.get("servers", []):
            if server["name"] == name:
                return server
        return {}

    def server_setting(self, key: str, server: str = None):
        """One per-server setting, falling back to its default."""
        return self._server_entry(server).get(key, SERVER_SETTINGS[key])

    def _write_server_settings(self, server: str = None, **values) -> None:
        """Attach settings to a server, or say why they can't be.

        Refuses rather than discarding: these settings live on the server, so
        with none configured there is nowhere to put them, and silently
        accepting the write would report success for a change that did not
        happen.
        """
        entry = self._server_entry(server)
        if not entry:
            named = f" '{server}'" if server else ""
            raise ValueError(
                f"No server{named} to apply this to. Add one with "
                f"'nasync setup' first."
            )
        entry.update(values)
        self.save()

    # ------------------------------------------------------------------ #
    # The sync root — the boundary every pair lives inside
    # ------------------------------------------------------------------ #
    def get_root(self, server: str = None) -> str:
        """This server's sync root, as a clean relative path ("" = whole share)."""
        return normalize_remote(self._server_entry(server).get(SERVER_ROOT, ""))

    @_mutator
    def set_root(self, path: str, server: str = None) -> str:
        """Set the sync root. Everything on the share beside it becomes
        unreachable to every pair, now and later."""
        root = normalize_remote(path)
        self._write_server_settings(server, **{SERVER_ROOT: root})
        return root

    def resolve_remote(self, server_path: str, server: str = None) -> str:
        """Where a pair's destination actually lands on the share.

        A pair's stored path is relative to its server's sync root, so this is
        the one place the two are joined — and the one place an attempt to
        climb out of the root is refused.
        """
        return join_remote(self.get_root(server), server_path)

    # ------------------------------------------------------------------ #
    # Sync mode — per server since 0.18.0
    # ------------------------------------------------------------------ #
    def get_sync_mode(self, server: str = None) -> str:
        """"mirror" (destination matches the source exactly) or "update".

        Per server: a home NAS you mirror and a work NAS you only ever add to
        are not describable by one setting. Defaults to "mirror", which is what
        pushes did before the mode was configurable — an existing install keeps
        behaving as it always has.
        """
        mode = self.server_setting("sync_mode", server)
        return mode if mode in SYNC_MODES else DEFAULT_SYNC_MODE

    @_mutator
    def set_sync_mode(self, mode: str, server: str = None) -> str:
        if mode not in SYNC_MODES:
            raise ValueError(
                f"Unknown sync mode '{mode}'. Use one of: {', '.join(SYNC_MODES)}."
            )
        before = self.get_sync_mode(server)
        self._write_server_settings(server, sync_mode=mode)
        if mode == "update" and before != "update":
            # Whatever is deleted locally from now on stays on the NAS; the
            # next mirror run deletes all of it at once, so it is a first
            # mirror run again and asks at 50+ (third audit, #2).
            from nasync.core import state
            state.reset_mirrored(self.config_dir,
                                 server or self.get_active_server_name())
        return mode

    # ------------------------------------------------------------------ #
    # Automation: live sync + scheduled sync
    # ------------------------------------------------------------------ #
    def get_background(self) -> bool:
        """The master switch for running unattended.

        App-wide, not per server: there is one systemd unit (or one portal
        autostart entry), so "run in the background" is a single fact about the
        machine. Off means the triggers stay configured but nothing runs them,
        which is the *Paused* state the front-ends report.
        """
        return bool(self.data.get("automation", {}).get("background", True))

    @_mutator
    def set_background(self, enabled: bool) -> bool:
        automation = self.data.setdefault("automation", {})
        automation["background"] = bool(enabled)
        self.save()
        return automation["background"]

    def get_automation(self, server: str = None) -> dict:
        """One server's automatic-sync settings, plus the global master switch.

        Per server since 0.18.0 — the triggers, the time and the catch-up date
        all belong to the NAS they sync. `background` is the exception and is
        app-wide; it is returned here because every caller that asks about the
        triggers also has to know whether anything is allowed to run them.

        Live sync and scheduled sync are **off** by default: an automatic sync
        that deletes on the server is not something to opt someone into.
        """
        entry = self._server_entry(server)
        return {
            # Sync a few seconds after files stop changing, when on the LAN.
            "live_sync": bool(entry.get("live_sync", False)),
            # Sync once a day at "HH:MM".
            "scheduled_sync": bool(entry.get("scheduled_sync", False)),
            "background": self.get_background(),
            "scheduled_time": normalize_time(entry.get("scheduled_time"),
                                             DEFAULT_SYNC_TIME),
            "last_scheduled_run": entry.get("last_scheduled_run"),
            "interval_minutes": int(entry.get("interval_minutes", 0) or 0),
        }

    @_mutator
    def set_automation(self, live_sync=None, scheduled_sync=None,
                       scheduled_time=None, interval_minutes=None,
                       last_scheduled_run=None, background=None,
                       server: str = None) -> dict:
        """Write any subset of one server's triggers. `background` is global."""
        if background is not None:
            self.set_background(background)

        values = {}
        if live_sync is not None:
            values["live_sync"] = bool(live_sync)
        if scheduled_sync is not None:
            values["scheduled_sync"] = bool(scheduled_sync)
            if scheduled_sync and not self.get_automation(server)["scheduled_sync"] \
                    and last_scheduled_run is None:
                # Switching the schedule on starts its clock: a missed *day*
                # runs at the next chance (see `scheduled_run_is_due`), and a
                # schedule off for months must not count as months missed —
                # that would start a mirror sync the moment it is switched on.
                import datetime
                yesterday = (datetime.date.today()
                             - datetime.timedelta(days=1)).isoformat()
                previous = self.get_automation(server)["last_scheduled_run"]
                if not previous or previous < yesterday:
                    values["last_scheduled_run"] = yesterday
        if scheduled_time is not None:
            values["scheduled_time"] = require_time(scheduled_time)
        if interval_minutes is not None:
            values["interval_minutes"] = int(interval_minutes)
        if last_scheduled_run is not None:
            values["last_scheduled_run"] = last_scheduled_run
        if values:
            self._write_server_settings(server, **values)
        return self.get_automation(server)

    @_mutator
    def record_scheduled_run(self, date_str: str, server: str = None) -> None:
        """Mark today's scheduled sync as done, for one server.

        The daemon writes this while the GUI may be writing settings, so the
        file is re-read first — otherwise a stale in-memory copy would undo
        whatever the user just changed.
        """
        name = server or self.get_active_server_name()
        self._load()
        self._write_server_settings(name, last_scheduled_run=date_str)

    def automated_servers(self) -> list:
        """Names of the servers with at least one trigger switched on.

        What the daemon iterates. A server with nothing on is not merely idle —
        it is not watched, not scheduled, and costs nothing.
        """
        return [server["name"] for server in self.data.get("servers", [])
                if self.automation_wanted(server["name"])]

    def automation_wanted(self, server: str = None) -> bool:
        """True when a trigger is configured — for one server, or for any.

        Passing no server asks the app-wide question ("is anything automated at
        all?"), which is what decides whether the background service is needed.
        """
        if server is None:
            return any(self.automation_wanted(s["name"])
                       for s in self.data.get("servers", []))
        a = self.get_automation(server)
        return a["live_sync"] or a["scheduled_sync"] or a["interval_minutes"] > 0

    def background_service_wanted(self) -> bool:
        """True when some server has a trigger on *and* unattended is allowed."""
        return self.get_background() and self.automation_wanted()

    # ------------------------------------------------------------------ #
    # Portal grant — recorded, not chosen
    # ------------------------------------------------------------------ #
    def get_portal_grant(self) -> dict:
        """What the desktop portal last told us it granted.

        This is deliberately *not* part of `get_automation()`, which is the set
        of switches the user operates. This is a fact about the environment: in
        a Flatpak there is no way to ask the portal what is currently permitted
        — the interface has no query method — so the only thing that can be
        shown is what it last said, and that has to be stored somewhere.

        ``asked`` is whether the question has been put to the user at all, and
        is what stops a refusal being re-asked on every settings change. It's
        the presence of the record rather than a stored flag, so clearing the
        record is a complete reset.

        Absent from every config written before 0.9.0, which reads correctly as
        "we have never asked": outside a sandbox nothing ever does.
        """
        grant = self.data.get("portal")
        return {"asked": grant is not None,
                "autostart": bool((grant or {}).get("autostart", False)),
                "background": bool((grant or {}).get("background", False))}

    @_mutator
    def set_portal_grant(self, autostart: bool, background: bool) -> dict:
        self.data["portal"] = {"autostart": bool(autostart),
                               "background": bool(background)}
        self.save()
        return self.get_portal_grant()

    @_mutator
    def clear_portal_grant(self) -> None:
        self.data.pop("portal", None)
        self.save()

    # ------------------------------------------------------------------ #
    # Notifications
    # ------------------------------------------------------------------ #
    def get_notifications(self) -> bool:
        """Whether syncs announce themselves on the desktop. On by default.

        Unlike the automatic triggers, this defaults to *on*: a background
        sync you can't see is the thing notifications exist to fix, and one
        that never says anything is indistinguishable from one that isn't
        running.
        """
        return bool(self.data.get("notifications", True))

    @_mutator
    def set_notifications(self, enabled: bool) -> bool:
        self.data["notifications"] = bool(enabled)
        self.save()
        return self.data["notifications"]

    # ------------------------------------------------------------------ #
    # Sync pairs (owned by a server — see the class docstring)
    # ------------------------------------------------------------------ #
    def _owned_pairs(self, server: str = None) -> list:
        """The mutable pair list of `server`, defaulting to the active one."""
        name = server or self.get_active_server_name()
        if not name:
            return []
        # `.get`, not `.setdefault`: this is a read path, and inserting here
        # meant merely *listing* a server's pairs created an entry for it —
        # including for names that don't exist, or one just removed, which the
        # next unrelated save() then wrote to disk. Writers use `_own_pairs`.
        return self.data["pairs_by_server"].get(name, [])

    def _own_pairs(self, server: str = None) -> list:
        """The mutable pair list of `server`, creating it if needed."""
        name = server or self.get_active_server_name()
        return self.data["pairs_by_server"].setdefault(name, [])

    @_mutator
    def add_pair(self, local_path: str, server_path: str, name: str = None,
                 server: str = None):
        target = server or self.get_active_server_name()
        if not target:
            raise ValueError("No server selected. Add a server first.")

        # Normalised here rather than at sync time, so a destination that
        # cannot stay inside the sync root is refused while someone is looking
        # at the error, not discovered by a mirror run at 2am.
        server_path = normalize_remote(server_path)

        local_path = os.path.abspath(os.path.expanduser(local_path))
        if name is None:
            name = os.path.basename(local_path.rstrip("/")) or "root"
        if self.get_pair(name, target):
            raise ValueError(
                f"A pair named '{name}' already exists on '{target}'. "
                f"Use --name to choose a different name, or remove it first."
            )
        self._refuse_overlap(server_path, target)
        self._own_pairs(target).append({
            "name": name,
            "local": local_path,
            "server": server_path,
        })
        self.save()
        return name

    @_mutator
    def update_pair(self, orig_name: str, local_path: str, server_path: str,
                    name: str, server: str = None):
        # Same reasoning as `add_pair`: refuse a destination that can't stay
        # inside the sync root here, while someone is looking at the error.
        server_path = normalize_remote(server_path)
        local_path = os.path.abspath(os.path.expanduser(local_path))
        if name != orig_name and self.get_pair(name, server):
            raise ValueError(f"A pair named '{name}' already exists.")
        self._refuse_overlap(server_path, server, ignore=orig_name)
        for pair in self._owned_pairs(server):
            if pair["name"] == orig_name:
                pair["name"] = name
                pair["local"] = local_path
                pair["server"] = server_path
                self.save()
                return name
        raise ValueError(f"No pair named '{orig_name}'.")

    @_mutator
    def remove_pair(self, name: str, server: str = None):
        target = server or self.get_active_server_name()
        if not target:
            return
        self.data["pairs_by_server"][target] = [
            p for p in self._owned_pairs(target) if p["name"] != name
        ]
        self.save()

    def get_pairs(self, server: str = None):
        """Pairs belonging to `server`, or to the active server by default."""
        return self._owned_pairs(server)

    def same_storage(self, first: str, second: str) -> bool:
        """Do two server entries reach the same share on the same NAS?

        One NAS is easily added twice — its LAN address and a VPN name, say —
        and pairs on the two entries can then overlap without either entry
        knowing. The NAS's identity (learned on first contact) settles it once
        both entries have one; until then, the same address and port do.

        Samba's identity is only its host name (see `identity_is_weak`), and
        two NASes left at a default like "raspberrypi" share it. A name can
        prove two servers *different*, not the same, so for those the address
        decides too — until 0.21.1 the second NAS of a 3-2-1 setup was refused
        as "the same storage" as the first.
        """
        if first == second:
            return True
        a, b = self._server_entry(first), self._server_entry(second)
        if not a or not b:
            return False
        if (a.get("share") or "").casefold() != (b.get("share") or "").casefold():
            return False
        if a.get("identity") and b.get("identity"):
            if a["identity"] != b["identity"]:
                return False
            if not identity_is_weak(a["identity"]):
                return True
        if int(a.get("port") or 445) != int(b.get("port") or 445):
            return False
        return bool(_addresses(a.get("ip")) & _addresses(b.get("ip")))

    def _neighbours(self, server: str):
        """(server name, pair) for every pair stored on the same storage."""
        name = server or self.get_active_server_name()
        for other in self.data.get("servers", []):
            if self.same_storage(name, other["name"]):
                for pair in self.data["pairs_by_server"].get(other["name"], []):
                    yield other["name"], pair

    def pair_overlapping(self, server_path: str, server: str = None,
                         ignore: str = None):
        """The first pair, on this server or the same NAS under another entry,
        whose folder overlaps `server_path` — as ``(label, its resolved path)``.
        """
        name = server or self.get_active_server_name()
        mine = self.resolve_remote(server_path, name)
        for owner, pair in self._neighbours(name):
            if owner == name and pair["name"] == ignore:
                continue
            theirs = self.resolve_remote(pair["server"], owner)
            if destinations_overlap(mine, theirs):
                label = pair["name"] if owner == name else f"{pair['name']}' on '{owner}"
                return label, theirs
        return None

    def _refuse_overlap(self, server_path: str, server: str = None,
                        ignore: str = None) -> None:
        """Refuse a destination another pair already covers, while someone is
        looking at the error — not at 19:25 when the mirror run deletes."""
        clash = self.pair_overlapping(server_path, server, ignore)
        if clash:
            label, theirs = clash
            name = server or self.get_active_server_name()
            raise ValueError(overlap_message(
                self.resolve_remote(server_path, name), label, theirs))

    def overlapping_pairs(self, server: str = None) -> dict:
        """``{pair_name: [what its folder overlaps]}`` for one server's pairs.

        Configs written before 0.20.0 can already contain overlaps, which no
        longer get in at the door. Sync skips these pairs, and both front-ends
        flag them, so the existing ones stop deleting and get noticed. Pairs of
        another entry for the same NAS count too (see `same_storage`).
        """
        name = server or self.get_active_server_name()
        neighbours = [(owner, pair, self.resolve_remote(pair["server"], owner))
                      for owner, pair in self._neighbours(name)]
        clashes = {}
        for pair in self._owned_pairs(name):
            mine = self.resolve_remote(pair["server"], name)
            for owner, other, theirs in neighbours:
                if owner == name and other["name"] == pair["name"]:
                    continue
                if destinations_overlap(mine, theirs):
                    label = (other["name"] if owner == name
                             else f"{other['name']} (on {owner})")
                    clashes.setdefault(pair["name"], []).append(label)
        return clashes

    def get_pair(self, name: str, server: str = None):
        for pair in self._owned_pairs(server):
            if pair["name"] == name:
                return pair
        return None

    def count_pairs(self, server: str) -> int:
        return len(self.data["pairs_by_server"].get(server, []))
