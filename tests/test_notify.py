"""Tests for desktop notifications.

Nothing here talks to a real notification daemon: the machine running the tests
may not have one, and the point of this module is that it degrades quietly when
that's true. So the transports are exercised through a stubbed
``subprocess.run``, or a stubbed portal, instead.
"""

import subprocess

import pytest

from nasync.system import notify, portal


class _Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


@pytest.fixture(autouse=True)
def outside_a_sandbox(monkeypatch):
    """Default every test to the unsandboxed case.

    Otherwise a machine that happens to have FLATPAK_ID set would silently
    exercise a different backend than the test is describing.
    """
    monkeypatch.setattr(portal, "in_flatpak", lambda: False)


def test_gdbus_is_preferred_over_notify_send(monkeypatch):
    """gdbus ships with glib2 and supports replacing a notification in place,
    which is what keeps one sync from leaving two entries in the tray."""
    monkeypatch.setattr(notify.shutil, "which", lambda name: name == "gdbus")
    assert notify.backend() == "gdbus"

    monkeypatch.setattr(notify.shutil, "which",
                        lambda name: name == "notify-send")
    assert notify.backend() == "notify-send"
    print("✅ test_gdbus_is_preferred_over_notify_send passed")


def test_a_machine_without_a_daemon_syncs_silently(monkeypatch):
    """A server or a locked-down sandbox has neither binary. That's not an
    error — it means no notifications, and syncing carries on."""
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)

    assert notify.available() is False
    assert notify.send("Sync complete", "12 transferred") == 0
    print("✅ test_a_machine_without_a_daemon_syncs_silently passed")


def test_the_notification_id_comes_back_for_reuse(monkeypatch):
    """The id is what makes 'started' turn into 'finished' rather than stack
    up beside it."""
    monkeypatch.setattr(notify.shutil, "which", lambda name: name == "gdbus")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Result(stdout="(uint32 27,)\n"))

    assert notify.send("Syncing…", "nas") == 27
    print("✅ test_the_notification_id_comes_back_for_reuse passed")


def test_a_failing_daemon_never_reaches_the_caller(monkeypatch):
    """No session bus, daemon not running, call times out: none of it is worth
    failing a sync over."""
    monkeypatch.setattr(notify.shutil, "which", lambda name: name == "gdbus")

    def explode(*args, **kwargs):
        raise OSError("no session bus")

    monkeypatch.setattr(subprocess, "run", explode)
    assert notify.send("Sync complete") == 0
    print("✅ test_a_failing_daemon_never_reaches_the_caller passed")


def test_critical_notifications_stay_until_dismissed(monkeypatch):
    """A scheduled sync that didn't happen is the one thing worth interrupting
    for, so it must not expire after eight seconds like the rest."""
    calls = []
    monkeypatch.setattr(notify.shutil, "which", lambda name: name == "gdbus")
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or _Result(stdout="(uint32 1,)"))

    notify.send("Scheduled sync didn't run", "Away from home", urgency="critical")
    assert calls[0][-1] == "-1", calls[0]

    notify.send("Sync complete", "all good")
    assert calls[1][-1] == str(notify.DEFAULT_TIMEOUT_MS), calls[1]
    print("✅ test_critical_notifications_stay_until_dismissed passed")


def test_a_run_occupies_one_notification_that_updates(monkeypatch):
    """Otherwise a service syncing all day fills the tray with its own
    history, and a stale 'Syncing…' sits there forever."""
    sent = []

    def fake_send(summary, body="", urgency="normal", replaces=0, **kwargs):
        sent.append((summary, replaces))
        return 42

    notifier = notify.Notifier(sender=fake_send)
    notifier.notify("Syncing…", "nas")
    notifier.notify("Sync complete", "12 transferred")

    assert sent == [("Syncing…", 0), ("Sync complete", 42)]
    print("✅ test_a_run_occupies_one_notification_that_updates passed")


def test_the_switch_turns_the_whole_thing_off():
    sent = []
    notifier = notify.Notifier(enabled=False,
                               sender=lambda *a, **k: sent.append(a) or 1)

    assert notifier.notify("Syncing…") == 0
    assert sent == []

    # Flipped back on mid-run — the service does this when the config changes.
    notifier.enabled = True
    notifier.notify("Syncing…")
    assert len(sent) == 1
    print("✅ test_the_switch_turns_the_whole_thing_off passed")


# --------------------------------------------------------------------------- #
# Inside a Flatpak — the portal, which needs no manifest permission at all
# --------------------------------------------------------------------------- #
def test_the_portal_is_used_inside_a_sandbox(monkeypatch):
    """Talking to org.freedesktop.Notifications directly would need a
    --talk-name permission in the manifest; the portal needs nothing."""
    monkeypatch.setattr(portal, "in_flatpak", lambda: True)
    monkeypatch.setattr(portal, "available", lambda iface=None, **kw: True)
    monkeypatch.setattr(notify.shutil, "which", lambda name: name == "gdbus")

    assert notify.backend() == "portal"
    print("✅ test_the_portal_is_used_inside_a_sandbox passed")


def test_the_portal_is_not_used_outside_one(monkeypatch):
    """Outside a sandbox it would work, but it's a needless extra hop through
    a service whose whole job is mediating for sandboxed apps."""
    monkeypatch.setattr(portal, "available", lambda iface=None, **kw: True)
    monkeypatch.setattr(notify.shutil, "which", lambda name: name == "gdbus")

    assert notify.backend() == "gdbus"
    print("✅ test_the_portal_is_not_used_outside_one passed")


def test_a_sandbox_without_the_notification_portal_falls_back(monkeypatch):
    monkeypatch.setattr(portal, "in_flatpak", lambda: True)
    monkeypatch.setattr(portal, "available", lambda iface=None, **kw: False)
    monkeypatch.setattr(notify.shutil, "which", lambda name: name == "gdbus")

    assert notify.backend() == "gdbus"
    print("✅ test_a_sandbox_without_the_notification_portal_falls_back passed")


def test_portal_notifications_replace_by_a_stable_id(monkeypatch):
    """The portal replaces whatever is on screen under the same id, so one sync
    run occupies one slot instead of filling the tray with its history."""
    monkeypatch.setattr(portal, "in_flatpak", lambda: True)
    monkeypatch.setattr(portal, "available", lambda iface=None, **kw: True)
    sent = []
    monkeypatch.setattr(portal, "add_notification",
                        lambda nid, title, body="", urgency="normal":
                        sent.append((nid, title, body, urgency)) or True)

    notifier = notify.Notifier()
    notifier.notify("Syncing…", "documents")
    notifier.notify("Sync complete", "12 transferred")

    assert [s[0] for s in sent] == [notify.PORTAL_NOTIFICATION_ID] * 2, sent
    assert sent[1][1] == "Sync complete"
    print("✅ test_portal_notifications_replace_by_a_stable_id passed")


def test_critical_maps_to_the_portals_urgent_priority(monkeypatch):
    """Failures have to survive Do Not Disturb — a sync that didn't happen is
    the one thing worth interrupting for."""
    monkeypatch.setattr(portal, "in_flatpak", lambda: True)
    monkeypatch.setattr(portal, "available", lambda iface=None, **kw: True)
    seen = []
    monkeypatch.setattr(portal, "add_notification",
                        lambda nid, title, body="", urgency="normal":
                        seen.append(urgency) or True)

    notify.send("Sync failed", "server unreachable", urgency="critical")

    assert seen == ["critical"], seen
    assert portal.PRIORITY["critical"] == "urgent"
    print("✅ test_critical_maps_to_the_portals_urgent_priority passed")


def test_a_portal_that_refuses_is_not_an_error(monkeypatch):
    monkeypatch.setattr(portal, "in_flatpak", lambda: True)
    monkeypatch.setattr(portal, "available", lambda iface=None, **kw: True)
    monkeypatch.setattr(portal, "add_notification",
                        lambda *a, **kw: False)

    assert notify.send("Sync complete") == 0
    print("✅ test_a_portal_that_refuses_is_not_an_error passed")
