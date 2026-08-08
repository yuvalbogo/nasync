"""Run NASync's automation in the background, whichever way the system allows.

Live sync and scheduled sync are only worth switching on if they keep working
once the window is closed — a 02:00 sync that needs the app to be open at 02:00
is not automation. There are two ways to arrange that, and which one applies is
decided by where NASync is installed, not by anything the user picks:

  * **systemd** — outside a sandbox. A ``systemd --user`` unit runs
    ``nasync auto``, survives quitting the app, and starts again at login.
  * **the XDG Background portal** — inside a Flatpak, where there is no user
    manager to write a unit into and no ``systemctl`` to call. The desktop is
    asked for permission to autostart instead. See :mod:`nasync.portal`.

Both hide behind the same :func:`available` / :func:`install` / :func:`status` /
:func:`apply` surface, so the CLI and the desktop app never branch on which is
in play — they ask what the state is and print the sentence they get back.

The two are not equivalent, and the status text says so rather than pretending:
systemd can be asked whether the service is running *right now*, and the portal
cannot be asked anything at all. See :func:`status`.

Everything degrades rather than fails: with neither mechanism available the
settings still save and still apply while the app or ``nasync auto`` is
running, and :func:`available` tells the UI to say so.

Note on logging out: a systemd user service stops when the last session ends
unless lingering is enabled (``loginctl enable-linger $USER``). That's a
deliberate choice left to the user — see :func:`linger_hint`.
"""

import os
import shutil
import subprocess
from pathlib import Path

from nasync.system import portal
from nasync.core.config import Config
from nasync.system.launcher import _nasync_exec

SERVICE_NAME = "nasync-auto.service"

SYSTEMD, PORTAL = "systemd", "portal"

_UNIT = """[Unit]
Description=NASync automatic syncing (live sync + scheduled sync)
Documentation=https://github.com/YuvalBogo/nasync
After=network-online.target

[Service]
Type=simple
ExecStart={exec} auto
# Transient failures (NAS rebooting, network dropping) shouldn't end the
# service — it comes back and keeps waiting for the next trigger.
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""


# ---------------------------------------------------------------------- #
# Which mechanism is in play
# ---------------------------------------------------------------------- #
def backend() -> str:
    """``"systemd"``, ``"portal"``, or ``""`` when neither is usable.

    Being inside a sandbox settles it: a Flatpak has no systemd user manager to
    talk to, so there is nothing to fall back *to* if the portal is missing —
    which is why the portal branch doesn't try systemd afterwards.
    """
    if portal.in_flatpak():
        return PORTAL if portal.available() else ""
    return SYSTEMD if _systemd_available() else ""


def unit_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    return Path(config_home) / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / SERVICE_NAME


def _systemctl(*args, timeout: int = 30):
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True, timeout=timeout)


def _systemd_available() -> bool:
    """True when a systemd user manager is present and talking to us."""
    if not shutil.which("systemctl"):
        return False
    try:
        # `is-system-running` answers even while degraded; what matters is that
        # the user manager responds at all.
        _systemctl("is-system-running", timeout=10)
        return True
    except Exception:
        return False


def available() -> bool:
    """True when background syncing can be set up at all on this machine."""
    return bool(backend())


# ---------------------------------------------------------------------- #
# systemd
# ---------------------------------------------------------------------- #
def write_unit() -> Path:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_UNIT.format(exec=_nasync_exec()))
    return path


def _install_systemd() -> Path:
    path = write_unit()
    _systemctl("daemon-reload")
    result = _systemctl("enable", "--now", SERVICE_NAME)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()
                           or "systemctl could not enable the service.")
    return path


def _uninstall_systemd() -> bool:
    existed = unit_path().exists()
    _systemctl("disable", "--now", SERVICE_NAME)
    if existed:
        unit_path().unlink()
        _systemctl("daemon-reload")
    return existed


def is_active() -> bool:
    """Whether the background process is running *now*.

    Only systemd can answer this. The portal has no query method at all, so
    inside a Flatpak the honest answer is "unknown", and this reports False
    rather than guessing — :func:`status` is where that distinction is put into
    words the user reads.
    """
    if backend() != SYSTEMD:
        return False
    return _systemctl("is-active", SERVICE_NAME).stdout.strip() == "active"


def restart() -> None:
    """Pick up settings that changed while the service was running.

    A no-op under the portal: the autostarted process isn't ours to restart.
    It doesn't need to be — `nasync auto` re-reads the config when it changes,
    which is what makes a settings change reach it in the first place.
    """
    if backend() == SYSTEMD and unit_path().exists():
        _systemctl("restart", SERVICE_NAME)


# ---------------------------------------------------------------------- #
# portal
# ---------------------------------------------------------------------- #
def _install_portal(config=None):
    """Ask the desktop for permission to autostart, and record the answer.

    The user may be shown a dialog here, and may say no — so this is the one
    install path that can be *declined* rather than failing. A decline is
    reported as it happened; the switch it came from stays on, because the
    triggers still work while the app is open and turning them off would not be
    what was asked for.
    """
    config = config or Config()
    try:
        grant = portal.request_background()
    except PermissionError:
        # Record the refusal, so a later settings change doesn't put the same
        # dialog up again: a permission prompt that reappears every time
        # anything is saved is one people learn to dismiss without reading.
        # Toggling background syncing off and on clears this and asks again,
        # which is the deliberate way back.
        config.set_portal_grant(autostart=False, background=False)
        raise
    config.set_portal_grant(**grant)
    if not grant["autostart"]:
        raise PermissionError(
            "The desktop granted background running but not autostart, so "
            "syncing won't resume by itself after you log in.")
    return grant


def _uninstall_portal(config=None) -> bool:
    """Withdraw the autostart request.

    ``autostart=False`` is how the portal is told to remove the entry it
    created; there is no separate revoke call. The recorded grant is dropped
    either way, because a failed withdrawal must not leave the UI claiming
    background syncing is still arranged.
    """
    config = config or Config()
    had_grant = config.get_portal_grant()["autostart"]
    try:
        portal.request_background(autostart=False)
    except Exception:
        # Nothing to escalate: the user is turning the feature *off*, and the
        # stale autostart entry (if any) starts a `nasync auto` that reads the
        # settings and does nothing.
        pass
    config.clear_portal_grant()
    return had_grant


# ---------------------------------------------------------------------- #
# The common surface
# ---------------------------------------------------------------------- #
def install():
    """Set up background syncing. Returns the unit path, or the portal grant."""
    kind = backend()
    if kind == SYSTEMD:
        return _install_systemd()
    if kind == PORTAL:
        return _install_portal()
    raise RuntimeError(
        "Background syncing can't be set up here — there's no systemd user "
        "service and no desktop portal to ask. Live sync and scheduled sync "
        "will still run while NASync or 'nasync auto' is open."
    )


def uninstall(config=None) -> bool:
    """Turn background syncing off. True if there was something to turn off.

    Takes the caller's `config` rather than opening its own: `Config` caches
    config.json in memory, so a second instance clearing the portal grant would
    leave the caller's copy still believing it was granted — and the status
    line drawn straight afterwards would say so.
    """
    kind = backend()
    if kind == SYSTEMD:
        return _uninstall_systemd()
    if kind == PORTAL:
        return _uninstall_portal(config)
    # No mechanism, but a unit from a previous non-sandboxed install may still
    # be sitting there; leaving it would keep syncing after the switch is off.
    if unit_path().exists():
        unit_path().unlink()
        return True
    return False


def status(config=None) -> dict:
    """Everything the UI needs to describe background syncing, in one line.

    ``active`` means "running right now" and is only ever true under systemd;
    under the portal it stays False and ``detail`` says what *is* known. That
    asymmetry is real and the wording owns it: claiming a portal-autostarted
    process is running when nothing here can see it would be the kind of
    reassuring lie that makes a sync tool untrustworthy.
    """
    kind = backend()
    if kind == SYSTEMD:
        installed = unit_path().exists()
        active = is_active()
        if not installed:
            # Says what *is* true, since the caller may be showing this next to
            # switches that are already on.
            detail = ("Not installed — automatic syncing only runs while NASync "
                      "or 'nasync auto' is open.")
        elif active:
            detail = "Running in the background, even when NASync is closed."
        else:
            detail = "Installed but not running. Check 'systemctl --user status " \
                     f"{SERVICE_NAME}'."
        return {"available": True, "backend": SYSTEMD, "installed": installed,
                "active": active, "detail": detail}

    if kind == PORTAL:
        granted = (config or Config()).get_portal_grant()["autostart"]
        if granted:
            detail = ("Starts automatically at login, so syncing continues "
                      "with NASync closed. Your desktop's Settings can revoke "
                      "this under Applications ▸ NASync.")
        else:
            detail = ("Not enabled — automatic syncing only runs while NASync "
                      "is open.")
        return {"available": True, "backend": PORTAL, "installed": granted,
                # Unknowable through the portal; see the docstring.
                "active": False, "detail": detail}

    return {"available": False, "backend": "", "installed": False,
            "active": False,
            "detail": "Background syncing isn't available here — automatic "
                      "syncing only runs while NASync is open."}


def apply(config) -> dict:
    """Install or remove background syncing to match what the settings ask for.

    Called after any automation setting changes: turning the last trigger off
    removes the service rather than leaving an idle one enabled at login.
    """
    kind = backend()
    if not kind:
        return status(config)
    if config.background_service_wanted():
        if kind == SYSTEMD:
            was_installed = unit_path().exists()
            _install_systemd()
            if was_installed:
                restart()      # already there; make it re-read the settings
        # Only ever ask once. Re-requesting on every settings change would put
        # the permission dialog up again whether it was granted *or* refused,
        # which is how a prompt turns into noise people click through blindly.
        # Turning background syncing off clears the record, so switching it
        # back on asks again.
        elif not config.get_portal_grant()["asked"]:
            _install_portal(config)
        return status(config)
    uninstall(config)
    return status(config)


def linger_hint() -> str:
    """A parting note about staying alive after logout, if the backend needs one.

    Empty under the portal: the desktop owns the autostart entry there, and
    there is no lingering to enable.
    """
    if backend() != SYSTEMD:
        return ""
    return ("Background syncing runs while you're logged in. To keep it "
            "running after logging out, enable lingering:\n"
            "  loginctl enable-linger $USER")
