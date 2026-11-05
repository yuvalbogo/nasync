"""Tests for background syncing, under systemd and under the XDG portal.

The two mechanisms are not interchangeable — systemd can be asked whether the
service is running and the portal cannot be asked anything — so most of what is
checked here is that the difference is reported honestly rather than papered
over.
"""

import pytest

from nasync.system import portal, service
from nasync.core.config import Config


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """A config and a systemd unit directory that are ours alone.

    Carries a server, because since 0.18.0 the automatic-sync triggers live on
    one — the service exists to run them, so there is nothing to test without.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    Config().add_or_update_server("home", "10.0.0.5", "u", "pw", "backup", 445)
    return tmp_path


def _use(monkeypatch, kind):
    monkeypatch.setattr(service, "backend", lambda: kind)


# --------------------------------------------------------------------------- #
# Which backend is in play
# --------------------------------------------------------------------------- #
def test_backend_is_the_portal_inside_a_sandbox(monkeypatch):
    """A Flatpak has no systemd user manager, so the portal is the only option."""
    monkeypatch.setattr(portal, "in_flatpak", lambda: True)
    monkeypatch.setattr(portal, "available", lambda: True)
    monkeypatch.setattr(service, "_systemd_available", lambda: True)

    assert service.backend() == service.PORTAL
    assert service.available() is True


def test_backend_is_nothing_when_a_sandbox_has_no_portal(monkeypatch):
    """No fallback to systemd: there is no systemd to fall back to in there."""
    monkeypatch.setattr(portal, "in_flatpak", lambda: True)
    monkeypatch.setattr(portal, "available", lambda: False)
    monkeypatch.setattr(service, "_systemd_available", lambda: True)

    assert service.backend() == ""
    assert service.available() is False


def test_backend_is_systemd_outside_a_sandbox(monkeypatch):
    monkeypatch.setattr(portal, "in_flatpak", lambda: False)
    monkeypatch.setattr(service, "_systemd_available", lambda: True)

    assert service.backend() == service.SYSTEMD


# --------------------------------------------------------------------------- #
# systemd
# --------------------------------------------------------------------------- #
def test_unit_points_at_the_running_executable(monkeypatch, isolated):
    """Same trap as the desktop launcher: PATH often resolves 'nasync' to a
    different install than the one being configured."""
    monkeypatch.setattr(service, "_nasync_exec", lambda: "/opt/venv/bin/nasync")

    path = service.write_unit()
    text = path.read_text()

    assert path == isolated / "systemd" / "user" / "nasync-auto.service"
    assert "ExecStart=/opt/venv/bin/nasync auto" in text
    assert "WantedBy=default.target" in text          # starts at login
    assert "Restart=on-failure" in text               # survives a NAS reboot


def test_status_says_so_when_nothing_is_available(monkeypatch):
    _use(monkeypatch, "")
    state = service.status()

    assert state["available"] is False
    assert state["backend"] == ""
    assert state["active"] is False
    assert "only runs while NASync is open" in state["detail"]


def test_status_distinguishes_installed_from_running(monkeypatch, isolated):
    _use(monkeypatch, service.SYSTEMD)
    monkeypatch.setattr(service, "is_active", lambda: False)
    # Whether the program the unit points at runs is this machine's business;
    # here it does. The broken case has a test of its own.
    monkeypatch.setattr(service, "exec_problem", lambda path: None)

    assert "Not installed" in service.status()["detail"]

    service.write_unit()
    assert "not running" in service.status()["detail"]

    monkeypatch.setattr(service, "is_active", lambda: True)
    assert "Running in the background" in service.status()["detail"]


def test_apply_installs_when_wanted_and_removes_when_not(monkeypatch, isolated):
    _use(monkeypatch, service.SYSTEMD)
    monkeypatch.setattr(service, "is_active", lambda: True)

    calls = []
    monkeypatch.setattr(service, "_install_systemd", lambda: calls.append("install"))
    monkeypatch.setattr(service, "restart", lambda: calls.append("restart"))
    monkeypatch.setattr(service, "uninstall",
                        lambda config=None: calls.append("uninstall") or True)

    config = Config()
    config.set_automation(live_sync=True)
    service.apply(config)
    assert calls == ["install"], calls

    # Turning the last trigger off must not leave an idle service enabled at
    # login — that's a background process doing nothing forever.
    calls.clear()
    config.set_automation(live_sync=False, scheduled_sync=False, interval_minutes=0)
    service.apply(config)
    assert calls == ["uninstall"], calls


def test_apply_is_a_no_op_without_a_backend(monkeypatch, isolated):
    _use(monkeypatch, "")
    called = []
    monkeypatch.setattr(service, "_install_systemd", lambda: called.append("install"))

    config = Config()
    config.set_automation(live_sync=True)
    result = service.apply(config)

    assert called == [], called
    assert result["available"] is False


def test_install_refuses_clearly_without_a_backend(monkeypatch):
    _use(monkeypatch, "")
    with pytest.raises(RuntimeError, match="can't be set up here"):
        service.install()


def test_linger_hint_is_only_a_systemd_concern(monkeypatch):
    _use(monkeypatch, service.SYSTEMD)
    assert "enable-linger" in service.linger_hint()

    # The desktop owns the autostart entry under the portal; there is no
    # lingering to enable, and a hint about one would just be wrong.
    _use(monkeypatch, service.PORTAL)
    assert service.linger_hint() == ""


# --------------------------------------------------------------------------- #
# portal
# --------------------------------------------------------------------------- #
def test_portal_status_never_claims_to_know_it_is_running(monkeypatch, isolated):
    """The portal has no query method, so "running now" is unknowable.

    Reporting it as running anyway would be the kind of reassuring lie that
    makes a sync tool untrustworthy — the wording has to carry the difference.
    """
    _use(monkeypatch, service.PORTAL)
    config = Config()

    state = service.status(config)
    assert state["available"] is True
    assert state["installed"] is False
    assert "only runs while NASync is open" in state["detail"]

    config.set_portal_grant(autostart=True, background=True)
    state = service.status(config)
    assert state["installed"] is True
    assert state["active"] is False                    # unknowable, not false
    assert "Starts automatically at login" in state["detail"]
    assert "running" not in state["detail"].lower()
    assert "revoke" in state["detail"]                 # says how to undo it

    # And nothing pretends to be able to check.
    assert service.is_active() is False


def test_portal_is_asked_once_not_on_every_settings_change(monkeypatch, isolated):
    """A prompt that reappears on every save is one people stop reading."""
    _use(monkeypatch, service.PORTAL)
    asks = []
    monkeypatch.setattr(portal, "request_background",
                        lambda **kw: asks.append(kw) or
                        {"autostart": True, "background": True})

    config = Config()
    config.set_automation(live_sync=True)
    service.apply(config)
    assert len(asks) == 1, asks

    # Changing an unrelated setting must not re-prompt.
    config.set_automation(scheduled_time="03:30")
    service.apply(config)
    assert len(asks) == 1, asks


def test_portal_refusal_is_recorded_so_it_is_not_asked_again(monkeypatch, isolated):
    _use(monkeypatch, service.PORTAL)
    asks = []

    def refuse(**kw):
        # Withdrawing autostart goes through the same portal method, so only a
        # request that wants something is a prompt the user actually sees.
        if not kw.get("autostart", True):
            return {"autostart": False, "background": False}
        asks.append(kw)
        raise PermissionError("declined")

    monkeypatch.setattr(portal, "request_background", refuse)

    config = Config()
    config.set_automation(live_sync=True)
    with pytest.raises(PermissionError):
        service.apply(config)
    assert len(asks) == 1, asks
    assert config.get_portal_grant() == {"asked": True, "autostart": False,
                                         "background": False}

    # The refusal sticks: reconciling again is silent.
    service.apply(config)
    assert len(asks) == 1, asks

    # Turning background syncing off and on is the deliberate way to re-ask.
    config.set_automation(background=False)
    service.apply(config)
    assert config.get_portal_grant()["asked"] is False
    config.set_automation(background=True)
    with pytest.raises(PermissionError):
        service.apply(config)
    assert len(asks) == 2, asks


def test_portal_uninstall_withdraws_autostart_and_forgets_the_grant(
        monkeypatch, isolated):
    _use(monkeypatch, service.PORTAL)
    requests = []
    monkeypatch.setattr(portal, "request_background",
                        lambda **kw: requests.append(kw) or
                        {"autostart": False, "background": False})

    config = Config()
    config.set_portal_grant(autostart=True, background=True)
    monkeypatch.setattr(service, "Config", lambda: config)

    assert service.uninstall() is True
    assert requests == [{"autostart": False}], requests
    assert config.get_portal_grant()["asked"] is False


def test_portal_uninstall_survives_a_portal_that_refuses_to_talk(
        monkeypatch, isolated):
    """Turning a feature *off* must not be blockable by the thing being told."""
    _use(monkeypatch, service.PORTAL)

    def explode(**kw):
        raise RuntimeError("no portal")

    monkeypatch.setattr(portal, "request_background", explode)
    config = Config()
    config.set_portal_grant(autostart=True, background=True)
    monkeypatch.setattr(service, "Config", lambda: config)

    assert service.uninstall() is True
    assert config.get_portal_grant()["asked"] is False


def test_partial_grant_is_reported_rather_than_treated_as_success(
        monkeypatch, isolated):
    """Background-yes/autostart-no means syncing stops at the next logout."""
    _use(monkeypatch, service.PORTAL)
    monkeypatch.setattr(portal, "request_background",
                        lambda **kw: {"autostart": False, "background": True})

    config = Config()
    with pytest.raises(PermissionError, match="won't resume"):
        service._install_portal(config)
    assert config.get_portal_grant()["autostart"] is False


def test_a_unit_pointing_at_a_moved_install_says_so(monkeypatch, isolated):
    """A checkout moved from ~/Programming to ~/Projects left the service
    failing every 30 seconds, silently, with the status saying only
    "not running"."""
    _use(monkeypatch, service.SYSTEMD)
    monkeypatch.setattr(service, "is_active", lambda: False)
    monkeypatch.setattr(service, "_nasync_exec",
                        lambda: str(isolated / "gone" / "nasync"))
    service.write_unit()

    state = service.status()
    assert "can't start" in state["detail"], state
    assert "no longer exists" in state["problem"], state
    assert service.problem() == state["problem"]


def test_repair_rewrites_a_broken_unit_only_when_one_was_wanted(monkeypatch,
                                                                isolated):
    _use(monkeypatch, service.SYSTEMD)
    monkeypatch.setattr(service, "is_active", lambda: False)
    monkeypatch.setattr(service, "_nasync_exec",
                        lambda: str(isolated / "gone" / "nasync"))
    service.write_unit()
    calls = []
    monkeypatch.setattr(service, "_install_systemd", lambda: calls.append(1))
    real_problem = service.exec_problem
    monkeypatch.setattr(service, "exec_problem",
                        lambda path: None if path == "/working/nasync"
                        else real_problem(path))

    assert service.repair() is False, "nobody asked for background syncing"
    config = Config()
    config.set_automation(scheduled_sync=True, server="home")
    config.set_automation(background=True)
    assert service.repair() is False, "would repoint at another broken one"
    monkeypatch.setattr(service, "_nasync_exec", lambda: "/working/nasync")
    assert service.repair() is True and calls == [1]
