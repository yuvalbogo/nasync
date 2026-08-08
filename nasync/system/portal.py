"""Talk to the XDG Desktop Portal — how a sandboxed app asks for what it needs.

Inside a Flatpak there is no systemd user manager to write a unit into and no
``systemctl`` to call it with, so the mechanism NASync uses everywhere else
(:mod:`nasync.service`) is simply not present. ``org.freedesktop.portal.Background``
is the sanctioned replacement: it asks the desktop to start the app at login and
to let it keep running with no window open.

Three properties of the portal shape everything here:

  * **It answers asynchronously, and only after asking the user.**
    ``RequestBackground`` returns a handle immediately; the real answer arrives
    later as a ``Response`` signal on that handle. So a request blocks on a
    nested main loop rather than returning a value, and the wait has to outlast
    a human reading a dialog.
  * **It may grant less than was asked for.** The response says what was
    actually given, which is why :func:`request_background` returns the grant
    instead of a bare success flag.
  * **There is no way to ask what is currently granted.** The interface has no
    query method, and the user can revoke a permission at any time from the
    desktop's own settings. Nothing here can report live state — a caller that
    wants to *display* it has to record what it was last told and be honest
    that that is what it is doing. See :func:`nasync.service.status`.

Everything degrades: outside a sandbox, or on a desktop with no portal, the
functions here report unavailable and `service.py` uses systemd instead.
"""

import os
import secrets
import select
import time

from nasync import APP_ID

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
BACKGROUND_IFACE = "org.freedesktop.portal.Background"
NOTIFICATION_IFACE = "org.freedesktop.portal.Notification"
SECRET_IFACE = "org.freedesktop.portal.Secret"
REQUEST_IFACE = "org.freedesktop.portal.Request"

#: Response codes carried by ``org.freedesktop.portal.Request.Response``.
RESPONSE_OK, RESPONSE_CANCELLED, RESPONSE_FAILED = 0, 1, 2

#: How long to wait for the portal's answer. The user is being shown a dialog
#: during this, so the limit is "they walked away", not "the bus is slow".
RESPONSE_TIMEOUT_SECONDS = 120

#: What the desktop shows the user when it asks. Portals put this in the
#: permission dialog verbatim, so it has to read as a reason, not a feature.
BACKGROUND_REASON = ("Keep your folders in sync with your NAS while NASync "
                     "is closed.")

_available_cache = {}


def in_flatpak() -> bool:
    """True when this process is running inside a Flatpak sandbox.

    ``/.flatpak-info`` is written into every sandbox by flatpak itself and is
    the check the portal libraries use. ``FLATPAK_ID`` is honoured too so the
    path can be exercised without building a sandbox first.
    """
    return os.path.exists("/.flatpak-info") or bool(os.environ.get("FLATPAK_ID"))


def app_id() -> str:
    """The identity the portal knows us by.

    Inside a sandbox flatpak sets ``FLATPAK_ID``; it should equal
    :data:`nasync.APP_ID`, and if the two ever drift the sandbox's value is the
    one the portal will actually use.
    """
    return os.environ.get("FLATPAK_ID") or APP_ID


def autostart_commandline() -> list:
    """The argv the desktop should autostart, as seen *inside* the sandbox.

    The portal turns this into a host autostart entry that re-enters the
    sandbox, so it must name the command as the sandbox sees it — a host path
    would point at nothing. `nasync auto` is the same headless entry point the
    systemd unit runs, so both mechanisms start the identical process.
    """
    return ["nasync", "auto"]


def _gi():
    """``(Gio, GLib)``, or ``None`` where PyGObject isn't importable.

    The portal is D-Bus, and GDBus is the one client already guaranteed to be
    present wherever a portal is — but `nasync auto` is expected to run on
    machines with no PyGObject at all, so this import can never be top-level.
    """
    try:
        import gi
        from gi.repository import GLib, Gio
    except (ImportError, ValueError):
        return None
    del gi
    return Gio, GLib


def available(interface: str = BACKGROUND_IFACE, refresh: bool = False) -> bool:
    """True when `interface` is reachable on the desktop portal.

    Cached per interface: this costs a D-Bus round trip, it is asked on the UI
    thread, and a portal does not appear or vanish part-way through a session
    in any way the settings screen could act on. `refresh=True` re-probes.
    """
    if interface in _available_cache and not refresh:
        return _available_cache[interface]
    _available_cache[interface] = _probe(interface)
    return _available_cache[interface]


def _probe(interface: str = BACKGROUND_IFACE) -> bool:
    modules = _gi()
    if modules is None:
        return False
    Gio, GLib = modules
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # Reading the interface version proves three things at once: the bus is
        # there, the portal owns its name, and it implements this interface.
        connection.call_sync(
            PORTAL_BUS, PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (interface, "version")),
            GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 5000, None)
        return True
    except Exception:
        return False


def request_background(reason: str = BACKGROUND_REASON, commandline=None,
                       autostart: bool = True, parent_window: str = "",
                       timeout: int = RESPONSE_TIMEOUT_SECONDS) -> dict:
    """Ask to run in the background, and to be started at login.

    Returns ``{"background": bool, "autostart": bool}`` — what the desktop
    actually granted, which can be less than was asked for. Raises
    ``PermissionError`` when the user declined and ``RuntimeError`` when the
    portal is unusable or never answers.

    `parent_window` is an exported window handle (``wayland:…`` / ``x11:…``);
    passing "" makes the permission dialog free-floating rather than attached
    to the NASync window, which is the honest default for the CLI and for a
    request made before any window exists.
    """
    modules = _gi()
    if modules is None:
        raise RuntimeError(
            "PyGObject isn't installed, so the desktop portal can't be asked "
            "for permission to run in the background.")
    Gio, GLib = modules

    # A private main context, made thread-default, so this works when it is
    # called from a GUI worker thread while the real main loop already runs on
    # the main thread — two loops on one context would fight.
    context = GLib.MainContext.new()
    context.push_thread_default()
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        token = "nasync_" + secrets.token_hex(8)
        request_path = _request_path(connection.get_unique_name(), token)

        answer = {}
        loop = GLib.MainLoop.new(context, False)

        def on_response(_conn, _sender, _path, _iface, _name, params):
            code, results = params.unpack()
            answer["code"] = code
            answer["results"] = results
            loop.quit()

        # Subscribed *before* the call: the portal is free to answer before
        # RequestBackground has even returned its handle, and a Response that
        # arrives first would otherwise be missed and the wait would hang.
        subscription = connection.signal_subscribe(
            PORTAL_BUS, REQUEST_IFACE, "Response", request_path, None,
            Gio.DBusSignalFlags.NONE, on_response)
        try:
            options = {
                "handle_token": GLib.Variant("s", token),
                "reason": GLib.Variant("s", reason),
                "autostart": GLib.Variant("b", autostart),
                "commandline": GLib.Variant(
                    "as", list(commandline or autostart_commandline())),
                "dbus-activatable": GLib.Variant("b", False),
            }
            connection.call_sync(
                PORTAL_BUS, PORTAL_PATH, BACKGROUND_IFACE, "RequestBackground",
                GLib.Variant("(sa{sv})", (parent_window, options)),
                GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 30000, None)

            expiry = GLib.timeout_source_new_seconds(timeout)
            expiry.set_callback(lambda *_: loop.quit())
            expiry.attach(context)
            loop.run()
            expiry.destroy()
        finally:
            connection.signal_unsubscribe(subscription)
    finally:
        context.pop_thread_default()

    return _interpret(answer)


# ---------------------------------------------------------------------- #
# Notifications
# ---------------------------------------------------------------------- #
#: The portal's word for what freedesktop calls urgency.
PRIORITY = {"low": "low", "normal": "normal", "critical": "urgent"}


def add_notification(notification_id: str, title: str, body: str = "",
                     urgency: str = "normal") -> bool:
    """Show a notification through the portal. True if it was handed over.

    Far simpler than a background request: there is no Response signal and
    nothing to wait for, so this is one method call and no main loop.

    Replacement is by `notification_id` — sending the same id again *replaces*
    what is on screen. That is a better fit than the ``replaces_id`` dance the
    direct freedesktop interface needs, because the id is ours to choose and
    survives a restart of the process.

    Never raises: a machine with no portal, or a notification the desktop
    declines to show, is not worth failing a sync over.
    """
    modules = _gi()
    if modules is None:
        return False
    Gio, GLib = modules
    try:
        notification = {
            "title": GLib.Variant("s", title),
            "body": GLib.Variant("s", body),
            "priority": GLib.Variant("s", PRIORITY.get(urgency, "normal")),
            # Serialising a themed icon is what makes the shell show the NASync
            # icon rather than a generic bell; the name matches the .desktop
            # file, which is how the desktop ties the two together.
            "icon": Gio.ThemedIcon.new(app_id()).serialize(),
        }
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        connection.call_sync(
            PORTAL_BUS, PORTAL_PATH, NOTIFICATION_IFACE, "AddNotification",
            GLib.Variant("(sa{sv})", (notification_id, notification)),
            None, Gio.DBusCallFlags.NONE, 10000, None)
        return True
    except Exception:
        return False


def remove_notification(notification_id: str) -> bool:
    """Withdraw a notification we posted earlier. Never raises."""
    modules = _gi()
    if modules is None:
        return False
    Gio, GLib = modules
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        connection.call_sync(
            PORTAL_BUS, PORTAL_PATH, NOTIFICATION_IFACE, "RemoveNotification",
            GLib.Variant("(s)", (notification_id,)),
            None, Gio.DBusCallFlags.NONE, 10000, None)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------- #
# Secrets
# ---------------------------------------------------------------------- #
#: How long to wait for the master secret. Unlike a background request there is
#: no dialog and no human in the loop — the portal either answers or it doesn't.
SECRET_TIMEOUT_SECONDS = 15


def retrieve_secret(timeout: int = SECRET_TIMEOUT_SECONDS) -> bytes:
    """Fetch this application's master secret from the portal.

    ``org.freedesktop.portal.Secret`` hands a sandboxed app a stable, private
    key that only it can retrieve — derived and stored by the host keyring, and
    reachable with **no manifest permission at all**. It is the sanctioned way
    for a Flatpak to have something to encrypt its own storage with, and the
    reason NASync doesn't need ``--talk-name=org.freedesktop.secrets``.

    The secret arrives out-of-band, down a pipe rather than in the reply: the
    call takes the write end as a file descriptor and the portal writes to it.
    That means three things must line up — the fd is passed in a
    ``Gio.UnixFDList``, *our* copy of the write end is closed straight after the
    call so the read hits EOF, and the ``Response`` signal is subscribed before
    the call for the same reason it is in :func:`request_background`.

    Raises ``RuntimeError`` if the portal is absent, refuses, or never answers.
    The value is opaque bytes of unspecified length — derive a key from it
    rather than using it as one.
    """
    modules = _gi()
    if modules is None:
        raise RuntimeError(
            "PyGObject isn't installed, so the secret portal can't be reached.")
    Gio, GLib = modules

    context = GLib.MainContext.new()
    context.push_thread_default()
    read_fd, write_fd = os.pipe()
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        token = "nasync_" + secrets.token_hex(8)
        request_path = _request_path(connection.get_unique_name(), token)

        answer = {}
        loop = GLib.MainLoop.new(context, False)

        def on_response(_conn, _sender, _path, _iface, _name, params):
            code, _results = params.unpack()
            answer["code"] = code
            loop.quit()

        subscription = connection.signal_subscribe(
            PORTAL_BUS, REQUEST_IFACE, "Response", request_path, None,
            Gio.DBusSignalFlags.NONE, on_response)
        try:
            fd_list = Gio.UnixFDList.new()
            handle = fd_list.append(write_fd)
            connection.call_with_unix_fd_list_sync(
                PORTAL_BUS, PORTAL_PATH, SECRET_IFACE, "RetrieveSecret",
                GLib.Variant("(ha{sv})",
                             (handle, {"handle_token": GLib.Variant("s", token)})),
                GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 30000,
                fd_list, None)
            # The portal now owns a duplicate of the write end. Dropping ours is
            # what lets the read below terminate instead of blocking forever.
            os.close(write_fd)
            write_fd = -1

            expiry = GLib.timeout_source_new_seconds(timeout)
            expiry.set_callback(lambda *_: loop.quit())
            expiry.attach(context)
            loop.run()
            expiry.destroy()
        finally:
            connection.signal_unsubscribe(subscription)
    except GLib.Error as e:
        # Closes the read end too: this is called on every credential read
        # inside a Flatpak, so a portal that keeps refusing would otherwise
        # walk the process out of file descriptors one refusal at a time.
        os.close(read_fd)
        read_fd = -1
        raise RuntimeError(f"The secret portal refused the request: {e}")
    finally:
        if write_fd != -1:
            os.close(write_fd)
        context.pop_thread_default()

    # The answer is checked *before* reading, and the read is bounded. Both
    # matter: the portal keeps its own dup of the write end, so if it never
    # answers and never closes — which is exactly what happens when it can't
    # identify the caller as a sandboxed app — a plain `read()` blocks forever,
    # outliving the timeout above. This is called on every credential read
    # inside a Flatpak, so a hang here is a frozen app, not a slow one.
    try:
        if "code" not in answer:
            raise RuntimeError(
                "The secret portal didn't answer, so the saved passwords "
                "can't be unlocked.")
        if answer["code"] == RESPONSE_CANCELLED:
            raise RuntimeError("The secret portal request was cancelled.")
        if answer["code"] != RESPONSE_OK:
            raise RuntimeError("The secret portal declined to hand over a secret.")
        secret = _read_with_deadline(read_fd, timeout)
    except Exception:
        os.close(read_fd)
        raise

    os.close(read_fd)
    if not secret:
        raise RuntimeError("The secret portal returned an empty secret.")
    return secret


def _read_with_deadline(fd: int, timeout: int) -> bytes:
    """Drain `fd` until EOF, giving up rather than blocking indefinitely."""
    chunks = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "The secret portal opened a channel but never sent anything.")
        if not select.select([fd], [], [], remaining)[0]:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            return b"".join(chunks)          # EOF: the portal is done
        chunks.append(chunk)


def _request_path(unique_name: str, token: str) -> str:
    """Where the portal will send the answer.

    The Request object's path is derived from the caller's bus name and the
    token it chose, precisely so it can be subscribed to before the call is
    made — see the comment at the subscription.
    """
    sender = unique_name.lstrip(":").replace(".", "_")
    return f"{PORTAL_PATH}/request/{sender}/{token}"


def _interpret(answer: dict) -> dict:
    """Turn a raw portal response into a grant, or into a useful exception."""
    if "code" not in answer:
        raise RuntimeError(
            "The desktop portal didn't answer the background request. "
            "Automatic syncing will still run while NASync is open.")
    code = answer["code"]
    if code == RESPONSE_CANCELLED:
        raise PermissionError(
            "Permission to run in the background was declined. You can grant "
            "it later in your desktop's Settings, under Applications ▸ NASync.")
    if code != RESPONSE_OK:
        raise RuntimeError("The desktop refused the background request.")
    results = answer.get("results") or {}
    return {"background": bool(results.get("background", False)),
            "autostart": bool(results.get("autostart", False))}
