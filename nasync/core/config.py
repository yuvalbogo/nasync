import json
import os
import shutil
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

    def __init__(self):
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
    def _load(self):
        if self.config_file.exists():
            with open(self.config_file) as f:
                self.data = json.load(f)
        else:
            self.data = {}
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

    def save(self):
        with open(self.config_file, "w") as f:
            json.dump(self.data, f, indent=2)

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
    def _read_creds(self, allow_prompt: bool = True) -> dict:
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
    def add_or_update_server(self, name: str, ip: str, username: str,
                             password: str, share: str, port: int = 445,
                             encrypt: bool = False) -> str:
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
        self.data["servers"] = [s for s in self.data["servers"] if s["name"] != name]
        self.data["servers"].append(entry)

        if not self.data.get("active_server"):
            self.data["active_server"] = name
        self.save()
        return name

    # Name-less convenience wrapper, kept for callers that configure a server
    # without choosing a name for it — `scripts/local-test.sh` does this to set
    # up a throwaway container. The name falls out of the share.
    def set_server(self, ip: str, username: str, password: str, share: str,
                   port: int = 445, name: str = None, encrypt: bool = False) -> str:
        name = name or share or ip or "default"
        return self.add_or_update_server(name, ip, username, password, share,
                                         port, encrypt)

    def save_server(self, name: str, ip: str, username: str, password: str,
                    share: str, port: int = 445, orig_name: str = None,
                    encrypt: bool = False) -> str:
        """Add or update a server, renaming in place when the name changed.

        The rename path exists so editing a server's name doesn't lose the
        pairs filed under the old one — remove-then-add would.
        """
        if orig_name and orig_name != name:
            pairs = self.data["pairs_by_server"].pop(orig_name, [])
            self.remove_server(orig_name)
            self.add_or_update_server(name, ip, username, password, share, port,
                                      encrypt)
            if pairs:
                self.data["pairs_by_server"][name] = pairs
                self.save()
        else:
            self.add_or_update_server(name, ip, username, password, share, port,
                                      encrypt)
        return name

    def remove_server(self, name: str):
        self.data["servers"] = [s for s in self.data["servers"] if s["name"] != name]
        creds = self._read_creds()
        creds.pop(name, None)
        self._write_creds(creds)
        # The pairs belonged to this server; nothing else can sync them.
        self.data["pairs_by_server"].pop(name, None)
        if self.data.get("active_server") == name:
            self.data["active_server"] = (
                self.data["servers"][0]["name"] if self.data["servers"] else None
            )
        self.save()

    def list_servers(self) -> list:
        return self.data.get("servers", [])

    def get_active_server_name(self):
        return self.data.get("active_server")

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

    def get_server_config(self) -> dict:
        """The *active* server's config (or empty fields if none configured)."""
        name = self.get_active_server_name()
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
        }

    # ------------------------------------------------------------------ #
    # Sync mode
    # ------------------------------------------------------------------ #
    def get_sync_mode(self) -> str:
        """"mirror" (destination matches the source exactly) or "update".

        Defaults to "mirror", which is what pushes did before the mode was
        configurable — an existing install keeps behaving as it always has.
        """
        mode = self.data.get("sync_mode")
        return mode if mode in SYNC_MODES else DEFAULT_SYNC_MODE

    def set_sync_mode(self, mode: str) -> str:
        if mode not in SYNC_MODES:
            raise ValueError(
                f"Unknown sync mode '{mode}'. Use one of: {', '.join(SYNC_MODES)}."
            )
        self.data["sync_mode"] = mode
        self.save()
        return mode

    # ------------------------------------------------------------------ #
    # Automation: live sync + scheduled sync
    # ------------------------------------------------------------------ #
    def get_automation(self) -> dict:
        """Both automatic modes, plus the legacy interval trigger.

        Live sync and scheduled sync are **off** by default: an automatic sync
        that deletes on the server is not something to opt someone into.
        """
        a = self.data.get("automation", {})
        return {
            # Sync a few seconds after files stop changing, when on the LAN.
            "live_sync": bool(a.get("live_sync", False)),
            # Sync once a day at "HH:MM".
            "scheduled_sync": bool(a.get("scheduled_sync", False)),
            # Master switch for running unattended. Off means the triggers stay
            # configured but nothing runs them, so automatic syncing pauses.
            "background": bool(a.get("background", True)),
            "scheduled_time": normalize_time(a.get("scheduled_time"), DEFAULT_SYNC_TIME),
            # Date ("YYYY-MM-DD") of the last scheduled run that actually ran,
            # so a missed one (asleep, or away from home) can catch up.
            "last_scheduled_run": a.get("last_scheduled_run"),
            # Pre-0.5.0 "every N minutes" trigger, still honoured by `auto`.
            "interval_minutes": int(a.get("interval_minutes", 0)),
        }

    def set_automation(self, live_sync=None, scheduled_sync=None,
                       scheduled_time=None, interval_minutes=None,
                       last_scheduled_run=None, background=None) -> dict:
        a = self.get_automation()
        if live_sync is not None:
            a["live_sync"] = bool(live_sync)
        if scheduled_sync is not None:
            a["scheduled_sync"] = bool(scheduled_sync)
        if background is not None:
            a["background"] = bool(background)
        if scheduled_time is not None:
            a["scheduled_time"] = require_time(scheduled_time)
        if interval_minutes is not None:
            a["interval_minutes"] = int(interval_minutes)
        if last_scheduled_run is not None:
            a["last_scheduled_run"] = last_scheduled_run
        self.data["automation"] = a
        self.save()
        return a

    def record_scheduled_run(self, date_str: str) -> None:
        """Mark today's scheduled sync as done.

        The daemon writes this while the GUI may be writing settings, so the
        file is re-read first — otherwise a stale in-memory copy would undo
        whatever the user just changed.
        """
        self._load()
        automation = self.data.get("automation", {})
        automation["last_scheduled_run"] = date_str
        self.data["automation"] = automation
        self.save()

    def automation_wanted(self) -> bool:
        """True when at least one automatic trigger is configured."""
        a = self.get_automation()
        return a["live_sync"] or a["scheduled_sync"] or a["interval_minutes"] > 0

    def background_service_wanted(self) -> bool:
        """True when a trigger is on *and* the user wants it running unattended."""
        return self.get_automation()["background"] and self.automation_wanted()

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

    def set_portal_grant(self, autostart: bool, background: bool) -> dict:
        self.data["portal"] = {"autostart": bool(autostart),
                               "background": bool(background)}
        self.save()
        return self.get_portal_grant()

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

    def add_pair(self, local_path: str, server_path: str, name: str = None,
                 server: str = None):
        target = server or self.get_active_server_name()
        if not target:
            raise ValueError("No server selected. Add a server first.")

        local_path = os.path.abspath(os.path.expanduser(local_path))
        if name is None:
            name = os.path.basename(local_path.rstrip("/")) or "root"
        if self.get_pair(name, target):
            raise ValueError(
                f"A pair named '{name}' already exists on '{target}'. "
                f"Use --name to choose a different name, or remove it first."
            )
        self._own_pairs(target).append({
            "name": name,
            "local": local_path,
            "server": server_path,
        })
        self.save()
        return name

    def update_pair(self, orig_name: str, local_path: str, server_path: str,
                    name: str, server: str = None):
        local_path = os.path.abspath(os.path.expanduser(local_path))
        if name != orig_name and self.get_pair(name, server):
            raise ValueError(f"A pair named '{name}' already exists.")
        for pair in self._owned_pairs(server):
            if pair["name"] == orig_name:
                pair["name"] = name
                pair["local"] = local_path
                pair["server"] = server_path
                self.save()
                return name
        raise ValueError(f"No pair named '{orig_name}'.")

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

    def get_pair(self, name: str, server: str = None):
        for pair in self._owned_pairs(server):
            if pair["name"] == name:
                return pair
        return None

    def count_pairs(self, server: str) -> int:
        return len(self.data["pairs_by_server"].get(server, []))
