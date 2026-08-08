"""Tests for the XDG Desktop Portal client.

The D-Bus round trip itself isn't exercised here — it needs a portal, a session
bus and a human to answer a dialog. What is exercised is everything around it:
sandbox detection, the request path the answer will arrive on, and how a
response is turned into a grant or into a message worth showing someone.
"""

import threading
import time

import pytest

from nasync import APP_ID
from nasync.system import portal


@pytest.fixture(autouse=True)
def no_cached_probe(monkeypatch):
    """`available()` memoises per interface; each test starts from a clean slate."""
    monkeypatch.setattr(portal, "_available_cache", {})


# --------------------------------------------------------------------------- #
# Where are we running?
# --------------------------------------------------------------------------- #
def test_flatpak_detection(monkeypatch, tmp_path):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(portal.os.path, "exists", lambda p: p == "/.flatpak-info")
    assert portal.in_flatpak() is True

    monkeypatch.setattr(portal.os.path, "exists", lambda p: False)
    assert portal.in_flatpak() is False

    # The env var alone is enough, so the sandbox path can be exercised
    # without building a sandbox first.
    monkeypatch.setenv("FLATPAK_ID", APP_ID)
    assert portal.in_flatpak() is True


def test_app_id_prefers_what_the_sandbox_says(monkeypatch):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    assert portal.app_id() == APP_ID

    # If the two ever drift, the portal will use the sandbox's value, so that
    # is the one to report.
    monkeypatch.setenv("FLATPAK_ID", "org.example.Other")
    assert portal.app_id() == "org.example.Other"


def test_autostart_commandline_is_the_headless_entry_point():
    """The portal re-enters the sandbox, so this is argv *inside* it — and it
    has to be the same process the systemd unit runs, or the two mechanisms
    would not be doing the same thing."""
    assert portal.autostart_commandline() == ["nasync", "auto"]


def test_available_is_probed_once_per_interface_and_then_remembered(monkeypatch):
    probes = []
    monkeypatch.setattr(portal, "_probe",
                        lambda interface=portal.BACKGROUND_IFACE:
                        probes.append(interface) or True)

    assert portal.available() is True
    assert portal.available() is True
    assert len(probes) == 1, probes

    # A different interface is a different question, so it is asked once too.
    assert portal.available(portal.NOTIFICATION_IFACE) is True
    assert portal.available(portal.NOTIFICATION_IFACE) is True
    assert probes == [portal.BACKGROUND_IFACE, portal.NOTIFICATION_IFACE], probes

    assert portal.available(refresh=True) is True
    assert len(probes) == 3, probes


def test_available_is_false_without_pygobject(monkeypatch):
    monkeypatch.setattr(portal, "_gi", lambda: None)
    assert portal.available() is False


# --------------------------------------------------------------------------- #
# The request path — subscribed to before the call is even made
# --------------------------------------------------------------------------- #
def test_request_path_matches_what_the_portal_will_use():
    """The portal derives the Request object path from the caller's bus name
    and the token it was handed, which is exactly why the answer can be
    subscribed to before the request is sent."""
    path = portal._request_path(":1.234", "nasync_abc")
    assert path == "/org/freedesktop/portal/desktop/request/1_234/nasync_abc"


# --------------------------------------------------------------------------- #
# Interpreting the answer
# --------------------------------------------------------------------------- #
def test_response_is_reported_as_what_was_granted():
    granted = portal._interpret(
        {"code": portal.RESPONSE_OK,
         "results": {"background": True, "autostart": True}})
    assert granted == {"background": True, "autostart": True}


def test_a_partial_grant_is_not_rounded_up_to_success():
    """The portal is free to give less than was asked for, and a caller that
    assumed otherwise would promise autostart it never got."""
    granted = portal._interpret(
        {"code": portal.RESPONSE_OK, "results": {"background": True}})
    assert granted == {"background": True, "autostart": False}


def test_declining_says_how_to_change_your_mind():
    with pytest.raises(PermissionError) as caught:
        portal._interpret({"code": portal.RESPONSE_CANCELLED})
    assert "Settings" in str(caught.value)


def test_a_portal_that_never_answers_is_not_mistaken_for_a_refusal():
    """A timeout leaves no response at all. Saying "you declined" would be a
    lie, and silently succeeding would be worse."""
    with pytest.raises(RuntimeError, match="didn't answer"):
        portal._interpret({})


def test_other_failures_are_reported_as_refusals_by_the_desktop():
    with pytest.raises(RuntimeError, match="refused"):
        portal._interpret({"code": portal.RESPONSE_FAILED})


def test_reading_a_secret_gives_up_rather_than_blocking_forever():
    """Regression: the portal keeps its own end of the pipe open, so a plain
    read() outlives the request timeout entirely. Observed against a real
    desktop — `retrieve_secret(timeout=5)` never returned."""
    import os

    read_fd, write_fd = os.pipe()          # nothing will ever be written
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="never sent anything"):
            portal._read_with_deadline(read_fd, timeout=1)
        assert time.monotonic() - started < 5
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_reading_a_secret_returns_everything_written_before_eof():
    """Chunked, because a secret may arrive in more than one write."""
    import os

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"hunter")
    os.write(write_fd, b"2")
    os.close(write_fd)                     # EOF is what ends the read
    try:
        assert portal._read_with_deadline(read_fd, timeout=5) == b"hunter2"
    finally:
        os.close(read_fd)


def test_request_background_without_pygobject_explains_itself(monkeypatch):
    monkeypatch.setattr(portal, "_gi", lambda: None)
    with pytest.raises(RuntimeError, match="PyGObject"):
        portal.request_background()


# --------------------------------------------------------------------------- #
# The D-Bus round trip, against a stand-in portal on the real session bus.
#
# Everything above tests the code around the call. This tests the call: the
# variant signatures, subscribing to the answer before sending the request, and
# blocking on a nested main loop. Those are exactly the things that cannot fail
# loudly — a wrong signature or a missed signal shows up as a request that
# hangs or silently does nothing, inside a sandbox, on someone else's machine.
# --------------------------------------------------------------------------- #
_BACKGROUND_XML = """
<node>
  <interface name='org.freedesktop.portal.Background'>
    <method name='RequestBackground'>
      <arg type='s' name='parent_window' direction='in'/>
      <arg type='a{sv}' name='options' direction='in'/>
      <arg type='o' name='handle' direction='out'/>
    </method>
  </interface>
</node>
"""

_FAKE_BUS = "io.github.YuvalBogo.NASyncTestPortal"
_FAKE_PATH = "/io/github/YuvalBogo/NASyncTestPortal"


def _has_session_bus() -> bool:
    modules = portal._gi()
    if modules is None:
        return False
    Gio, _ = modules
    try:
        Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return True
    except Exception:
        return False


needs_bus = pytest.mark.skipif(not _has_session_bus(),
                               reason="needs a D-Bus session bus")


class _StandInPortal:
    """Answers RequestBackground the way xdg-desktop-portal does.

    Runs its own main loop on its own thread: the client blocks on a nested
    loop while waiting, so a single-threaded fake would deadlock against it —
    which is itself a useful reminder of why the client needs a private
    context.
    """

    def __init__(self, response=portal.RESPONSE_OK, results=None, answer=True):
        self.response = response
        self.results = results if results is not None else {
            "background": True, "autostart": True}
        self.answer = answer            # False simulates a portal that goes quiet
        self.requests = []
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        assert self._ready.wait(timeout=10), "stand-in portal never came up"

    def stop(self):
        if self._loop is not None:
            self._loop.quit()
        self._thread.join(timeout=5)

    def _run(self):
        Gio, GLib = portal._gi()
        context = GLib.MainContext.new()
        context.push_thread_default()
        self._loop = GLib.MainLoop.new(context, False)
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        node = Gio.DBusNodeInfo.new_for_xml(_BACKGROUND_XML)

        def on_call(conn, sender, path, iface, method, params, invocation):
            parent, options = params.unpack()
            self.requests.append({"parent_window": parent, "options": options})
            token = options.get("handle_token", "unknown")
            handle = portal._request_path(sender, token)
            invocation.return_value(GLib.Variant("(o)", (handle,)))
            if not self.answer:
                return
            conn.emit_signal(
                sender, handle, portal.REQUEST_IFACE, "Response",
                GLib.Variant("(ua{sv})", (
                    self.response,
                    {k: GLib.Variant("b", v) for k, v in self.results.items()},
                )))

        registration = connection.register_object(
            _FAKE_PATH, node.interfaces[0], on_call, None, None)
        owner = Gio.bus_own_name(
            Gio.BusType.SESSION, _FAKE_BUS, Gio.BusNameOwnerFlags.NONE,
            None, lambda *a: self._ready.set(), lambda *a: self._ready.set())
        try:
            self._loop.run()
        finally:
            Gio.bus_unown_name(owner)
            connection.unregister_object(registration)
            context.pop_thread_default()

    _loop = None


@pytest.fixture
def stand_in(monkeypatch):
    """Point the portal client at our stand-in instead of the real desktop."""
    monkeypatch.setattr(portal, "PORTAL_BUS", _FAKE_BUS)
    monkeypatch.setattr(portal, "PORTAL_PATH", _FAKE_PATH)
    created = []

    def make(**kwargs):
        fake = _StandInPortal(**kwargs)
        fake.start()
        created.append(fake)
        return fake

    yield make
    for fake in created:
        fake.stop()


@needs_bus
def test_request_background_completes_a_real_dbus_round_trip(stand_in):
    fake = stand_in()
    granted = portal.request_background()

    assert granted == {"background": True, "autostart": True}
    assert len(fake.requests) == 1, fake.requests
    options = fake.requests[0]["options"]
    assert options["autostart"] is True
    assert options["commandline"] == ["nasync", "auto"]
    # The reason is shown to the user verbatim, so it must actually be sent.
    assert options["reason"] == portal.BACKGROUND_REASON
    assert options["handle_token"].startswith("nasync_")


@needs_bus
def test_a_refusal_over_the_wire_reaches_the_caller_as_one(stand_in):
    stand_in(response=portal.RESPONSE_CANCELLED, results={})
    with pytest.raises(PermissionError):
        portal.request_background()


@needs_bus
def test_withdrawing_autostart_is_sent_as_such(stand_in):
    fake = stand_in(results={"background": False, "autostart": False})
    granted = portal.request_background(autostart=False)

    assert granted == {"background": False, "autostart": False}
    assert fake.requests[0]["options"]["autostart"] is False


@needs_bus
def test_a_portal_that_never_answers_times_out_instead_of_hanging(stand_in):
    """The nested loop must have a way out, or the switch that started it
    freezes for the life of the process."""
    stand_in(answer=False)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="didn't answer"):
        portal.request_background(timeout=1)
    assert time.monotonic() - started < 10
