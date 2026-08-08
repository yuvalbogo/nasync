"""Desktop notifications, for the syncs nobody is watching.

Automatic syncing is only trustworthy if it tells you what it did: a service
that quietly copies files — or quietly *doesn't* — is indistinguishable from
one that isn't running. So every background run announces itself, and so does
a scheduled run that couldn't happen.

Deliberately not built on ``Gio.Notification``: the notifier has to work from
``nasync auto``, a headless process with no GTK and no GApplication. Every
backend here speaks a notification protocol directly.

  * **portal** — used inside a Flatpak, and only there.
    ``org.freedesktop.portal.Notification`` is reachable from a sandbox with no
    extra manifest permission at all, where talking to
    ``org.freedesktop.Notifications`` directly needs ``--talk-name`` — a
    permission Flathub reviewers reasonably ask you to justify when a portal
    already does the job. It also replaces by an id *we* choose, which survives
    a restart of the process, rather than by a number the daemon hands back.
  * **gdbus** — preferred everywhere else. It ships with glib2, which is
    installed anywhere a notification daemon exists, and exposes
    ``replaces_id``, so a run's "started" notification is *replaced* by its
    "finished" one instead of stacking up a pair per sync.
  * **notify-send** — the fallback, for the rare desktop with libnotify but no
    gdbus. No replacement, so notifications accumulate.

Nothing here raises. A machine with no notification daemon (a server, a
headless session, a locked-down sandbox) syncs exactly as before, silently.
"""

import re
import shutil
import subprocess

from nasync import APP_ID
from nasync.system import portal

APP_NAME = "NASync"

#: The portal replaces a notification when the same id is sent again, so this
#: is the slot a NASync sync run occupies. One slot that stays current beats a
#: tray filling up with the history of a service that syncs all day.
PORTAL_NOTIFICATION_ID = "nasync-sync"

#: freedesktop urgency levels. Failures are critical so they survive Do Not
#: Disturb — a sync that didn't happen is the one thing worth interrupting for.
URGENCY = {"low": 0, "normal": 1, "critical": 2}

#: How long an ordinary notification stays on screen.
DEFAULT_TIMEOUT_MS = 8000

_ID_PATTERN = re.compile(r"uint32 (\d+)")


def backend() -> str:
    """Which transport is usable here: "portal", "gdbus", "notify-send", or ""."""
    # The portal is checked first but only inside a sandbox: outside one it
    # would work too, and would be a needless extra hop through a service that
    # exists to mediate for sandboxed apps.
    if portal.in_flatpak() and portal.available(portal.NOTIFICATION_IFACE):
        return "portal"
    for name in ("gdbus", "notify-send"):
        if shutil.which(name):
            return name
    return ""


def available() -> bool:
    return bool(backend())


def send(summary: str, body: str = "", urgency: str = "normal",
         replaces: int = 0, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
    """Show a notification. Returns its id, or 0 if nothing was shown.

    A non-zero id can be passed back as `replaces` to update the notification
    in place rather than posting a second one.
    """
    level = URGENCY.get(urgency, URGENCY["normal"])
    if urgency == "critical":
        timeout_ms = -1                 # stays until dismissed

    name = backend()
    try:
        if name == "portal":
            return _send_portal(summary, body, urgency)
        if name == "gdbus":
            return _send_gdbus(summary, body, level, replaces, timeout_ms)
        if name == "notify-send":
            return _send_notify_send(summary, body, urgency, timeout_ms)
    except Exception:
        # A missing session bus, a daemon that isn't running, a timeout: none
        # of it is worth failing a sync over.
        return 0
    return 0


def _send_portal(summary, body, urgency) -> int:
    """Post through the portal, which replaces by id rather than by number.

    `replaces` is not passed on and doesn't need to be: sending the same id
    again *is* the replacement. The non-zero return keeps `Notifier`'s
    bookkeeping honest — it means "there is something on screen to replace" —
    without inventing a daemon-assigned id that the portal never gives out.

    The portal has no expiry parameter, so an ordinary notification is left to
    the desktop's own timing; `urgency="critical"` still maps to the portal's
    "urgent" priority, which is what survives Do Not Disturb.
    """
    return 1 if portal.add_notification(
        PORTAL_NOTIFICATION_ID, summary, body, urgency) else 0


def _send_gdbus(summary, body, level, replaces, timeout_ms) -> int:
    # The hints tell the shell which app this is, so the notification carries
    # the NASync name and icon instead of a generic bell.
    hints = (f"{{'desktop-entry': <'{APP_ID}'>, "
             f"'urgency': <byte {level}>}}")
    result = subprocess.run(
        ["gdbus", "call", "--session",
         "--dest", "org.freedesktop.Notifications",
         "--object-path", "/org/freedesktop/Notifications",
         "--method", "org.freedesktop.Notifications.Notify",
         APP_NAME, str(int(replaces)), APP_ID, summary, body,
         "[]", hints, str(int(timeout_ms))],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return 0
    # "(uint32 27,)" — the id to hand back as replaces_id next time.
    match = _ID_PATTERN.search(result.stdout or "")
    return int(match.group(1)) if match else 0


def _send_notify_send(summary, body, urgency, timeout_ms) -> int:
    command = ["notify-send", "--app-name", APP_NAME, "--icon", APP_ID,
               "--urgency", urgency if urgency in URGENCY else "normal",
               f"--hint=string:desktop-entry:{APP_ID}"]
    if timeout_ms and timeout_ms > 0:
        command += ["--expire-time", str(int(timeout_ms))]
    command += [summary, body]
    subprocess.run(command, capture_output=True, text=True, timeout=10)
    return 0        # no id to reuse, so every notification is a new one


class Notifier:
    """One sync run's notifications, collapsed into a single message.

    A sync says "started" and later "finished"; posting both as separate
    notifications would leave a stale "Syncing…" sitting in the shell's tray
    forever. Holding the id and reusing it means the running sync occupies one
    slot that updates in place.

    `enabled=False` turns the whole thing into a no-op, which is what the
    notifications setting switches off.
    """

    def __init__(self, enabled: bool = True, sender=send):
        self.enabled = enabled
        self._send = sender
        self._id = 0

    def notify(self, summary: str, body: str = "", urgency: str = "normal") -> int:
        """Post a notification, replacing this notifier's previous one.

        Replacement holds for the life of the process, not just one run, so a
        service syncing all day occupies one notification that stays current
        rather than filling the tray with a history of it.
        """
        if not self.enabled:
            return 0
        new_id = self._send(summary, body, urgency, self._id)
        if new_id:
            self._id = new_id
        return new_id
