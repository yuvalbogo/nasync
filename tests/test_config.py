import json
import os
import tempfile
from nasync.core.config import Config


def _cfg(tmp):
    os.environ["XDG_CONFIG_HOME"] = tmp
    return Config()


def _cfg_with_server(tmp, name="home"):
    """A config with one server, which is where sync settings now live.

    Since 0.18.0 the mode and the automatic-sync triggers belong to a server
    rather than to the app, so a config with no servers has nowhere to keep
    them — and says so rather than accepting the write.
    """
    c = _cfg(tmp)
    c.add_or_update_server(name, "10.0.0.5", "user", "secret", "backup", 445)
    return c


def test_server_crud():
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg(tmp)
        c.add_or_update_server("home", "10.0.0.5", "user", "secret", "backup", 445)
        assert c.get_active_server_name() == "home"
        assert [s["name"] for s in c.list_servers()] == ["home"]

        # password lives in .creds (0600), never in config.json
        assert "secret" not in c.config_file.read_text()
        assert oct(c.creds_file.stat().st_mode)[-3:] == "600"

        sc = c.get_server_config()
        assert sc["ip"] == "10.0.0.5" and sc["password"] == "secret" and sc["port"] == 445

        # a second server; active stays until switched
        c.add_or_update_server("work", "10.0.0.9", "u2", "pw2", "share2", 4450)
        assert c.get_active_server_name() == "home"
        assert c.set_active_server("work")
        assert c.get_server_config()["ip"] == "10.0.0.9"

        # removing the active server falls back to another
        c.remove_server("work")
        assert c.get_active_server_name() == "home"
        print("✅ test_server_crud passed")


def test_migration_from_legacy():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        cfgdir = os.path.join(tmp, "nasync")
        os.makedirs(cfgdir)
        json.dump(
            {"server": {"ip": "1.2.3.4", "username": "yuno", "share": "Storage", "port": 445},
             "pairs": [{"name": "Docs", "local": "/x", "server": "/Docs"}]},
            open(os.path.join(cfgdir, "config.json"), "w"),
        )
        open(os.path.join(cfgdir, ".creds"), "w").write("legacypass")

        c = Config()
        assert "server" not in c.data                       # old key gone
        assert c.get_active_server_name() == "Storage"       # named from the share
        sc = c.get_server_config()
        assert sc["ip"] == "1.2.3.4" and sc["password"] == "legacypass"
        assert [p["name"] for p in c.get_pairs()] == ["Docs"]  # pairs preserved
        print("✅ test_migration_from_legacy passed")


def test_pairs():
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg(tmp)
        c.add_or_update_server("home", "10.0.0.5", "u", "p", "backup", 445)
        assert c.add_pair("/home/user/docs", "/Docs", "Documents") == "Documents"
        assert len(c.get_pairs()) == 1
        try:
            c.add_pair("/b", "/b", "Documents")
            raise AssertionError("expected duplicate rejection")
        except ValueError:
            pass
        assert c.add_pair("/home/user/Photos/", "/Photos") == "Photos"

        # edit: rename + change destination
        c.update_pair("Documents", "/home/user/docs", "/DocsV2", "Docs")
        assert c.get_pair("Documents") is None
        # Stored normalised since 0.19.0: a destination is relative to the
        # server's sync root, and a leading slash would suggest otherwise.
        assert c.get_pair("Docs")["server"] == "DocsV2"
        # editing to a name owned by another pair is rejected
        try:
            c.update_pair("Docs", "/x", "/x", "Photos")
            raise AssertionError("expected rename collision to be rejected")
        except ValueError:
            pass

        c.remove_pair("Docs")
        assert [p["name"] for p in c.get_pairs()] == ["Photos"]
        print("✅ test_pairs passed")


def test_pairs_belong_to_one_server():
    """Each server owns its pairs. Previously one global list was retargeted at
    whichever server was active — so switching servers silently pointed every
    pair at the new one, and in mirror mode that deletes on the wrong NAS."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg(tmp)
        c.add_or_update_server("home", "10.0.0.5", "u", "p", "backup", 445)
        c.add_or_update_server("work", "10.0.0.9", "u", "p", "share", 445)

        c.set_active_server("home")
        c.add_pair("/home/user/docs", "/Docs", "Documents")
        c.set_active_server("work")
        c.add_pair("/home/user/code", "/Code", "Code")

        assert [p["name"] for p in c.get_pairs()] == ["Code"]
        assert [p["name"] for p in c.get_pairs("home")] == ["Documents"]
        assert c.count_pairs("home") == 1 and c.count_pairs("work") == 1

        # The same pair name on two servers is fine — they're separate lists.
        c.add_pair("/elsewhere", "/Docs", "Documents")
        assert c.count_pairs("work") == 2
        assert c.get_pair("Documents", "home")["local"].endswith("/docs")
        assert c.get_pair("Documents", "work")["local"] == "/elsewhere"

        # Editing one server's pair leaves the other's alone.
        c.update_pair("Documents", "/moved", "/Docs2", "Documents", server="work")
        assert c.get_pair("Documents", "home")["server"] == "Docs"
        print("✅ test_pairs_belong_to_one_server passed")


def test_removing_a_server_takes_its_pairs():
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg(tmp)
        c.add_or_update_server("home", "10.0.0.5", "u", "p", "backup", 445)
        c.add_or_update_server("work", "10.0.0.9", "u", "p", "share", 445)
        c.add_pair("/a", "/A", "a", server="home")
        c.add_pair("/b", "/B", "b", server="work")

        c.remove_server("home")
        assert c.count_pairs("home") == 0
        assert c.count_pairs("work") == 1        # untouched
        assert "home" not in Config().data["pairs_by_server"]
        print("✅ test_removing_a_server_takes_its_pairs passed")


def test_renaming_a_server_keeps_its_pairs():
    """save_server renames in place; the remove-then-add it replaced would have
    dropped the pairs filed under the old name."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg(tmp)
        c.add_or_update_server("home", "10.0.0.5", "u", "secret", "backup", 445)
        c.add_pair("/a", "/A", "a")

        c.save_server("home-nas", "10.0.0.5", "u", "secret", "backup", 445,
                      orig_name="home")

        fresh = Config()
        assert [s["name"] for s in fresh.list_servers()] == ["home-nas"]
        assert [p["name"] for p in fresh.get_pairs("home-nas")] == ["a"]
        assert fresh.get_server("home-nas")["password"] == "secret"
        print("✅ test_renaming_a_server_keeps_its_pairs passed")


def test_global_pairs_migrate_to_the_active_server():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        cfgdir = os.path.join(tmp, "nasync")
        os.makedirs(cfgdir)
        json.dump({
            "servers": [
                {"name": "home", "ip": "10.0.0.5", "username": "u",
                 "share": "backup", "port": 445},
                {"name": "work", "ip": "10.0.0.9", "username": "u",
                 "share": "share", "port": 445},
            ],
            "active_server": "work",
            "pairs": [{"name": "Docs", "local": "/x", "server": "/Docs"}],
        }, open(os.path.join(cfgdir, "config.json"), "w"))

        c = Config()
        assert "pairs" not in c.data                       # old key gone
        assert [p["name"] for p in c.get_pairs("work")] == ["Docs"]
        assert c.get_pairs("home") == []
        assert [p["name"] for p in Config().get_pairs("work")] == ["Docs"]
        print("✅ test_global_pairs_migrate_to_the_active_server passed")


def test_pairs_added_before_any_server_are_kept_until_one_exists():
    """`nasync add` before `nasync init` leaves pairs with no owner; they must
    wait rather than being dropped."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        cfgdir = os.path.join(tmp, "nasync")
        os.makedirs(cfgdir)
        json.dump({"pairs": [{"name": "Docs", "local": "/x", "server": "/Docs"}]},
                  open(os.path.join(cfgdir, "config.json"), "w"))

        c = Config()
        assert c.data["pairs"] == [{"name": "Docs", "local": "/x", "server": "/Docs"}]
        c.add_or_update_server("home", "10.0.0.5", "u", "p", "backup", 445)

        migrated = Config()                                # next load adopts them
        assert [p["name"] for p in migrated.get_pairs("home")] == ["Docs"]
        assert "pairs" not in migrated.data
        print("✅ test_pairs_added_before_any_server_are_kept_until_one_exists passed")


def test_sync_mode():
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg_with_server(tmp)
        # Mirror is what pushes did before the mode existed, so an upgraded
        # install must keep behaving that way until told otherwise.
        assert c.get_sync_mode() == "mirror"

        assert c.set_sync_mode("update") == "update"
        assert Config().get_sync_mode() == "update"      # survives a reload

        try:
            c.set_sync_mode("hard")
            raise AssertionError("expected an unknown mode to be rejected")
        except ValueError:
            pass
        assert c.get_sync_mode() == "update"             # rejected, not applied

        # A hand-edited or corrupted value falls back rather than syncing in an
        # undefined mode.
        c._server_entry("home")["sync_mode"] = "nonsense"
        assert c.get_sync_mode() == "mirror"
        print("✅ test_sync_mode passed")


def test_sync_mode_is_per_server():
    """A home NAS you mirror and a work NAS you only add to are not the same
    setting, and one must never be pushed under the other's rule."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg_with_server(tmp, "home")
        c.add_or_update_server("work", "10.0.0.9", "u2", "pw2", "share2", 445)

        c.set_sync_mode("mirror", server="home")
        c.set_sync_mode("update", server="work")

        assert c.get_sync_mode("home") == "mirror"
        assert c.get_sync_mode("work") == "update"

        # No server named means the active one, as everywhere else.
        c.set_active_server("work")
        assert c.get_sync_mode() == "update"
        c.set_active_server("home")
        assert c.get_sync_mode() == "mirror"

        assert Config().get_sync_mode("work") == "update"    # survives a reload
        print("✅ test_sync_mode_is_per_server passed")


def test_settings_survive_editing_and_renaming_a_server():
    """`add_or_update_server` rebuilds the entry from its arguments, so without
    care an edit would silently switch a server's automation off."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg_with_server(tmp)
        c.set_sync_mode("update")
        c.set_automation(live_sync=True, scheduled_sync=True,
                         scheduled_time="03:30")

        # Editing an unrelated field — the port — keeps the settings.
        c.add_or_update_server("home", "10.0.0.5", "user", "secret", "backup", 4450)
        assert c.get_sync_mode() == "update"
        assert c.get_automation()["live_sync"] is True
        assert c.get_automation()["scheduled_time"] == "03:30"

        # So does a rename, which is remove-then-add underneath.
        c.save_server("attic", "10.0.0.5", "user", "secret", "backup", 4450,
                      orig_name="home")
        assert c.get_sync_mode("attic") == "update"
        a = c.get_automation("attic")
        assert a["live_sync"] is True and a["scheduled_time"] == "03:30"
        assert Config().get_sync_mode("attic") == "update"
        print("✅ test_settings_survive_editing_and_renaming_a_server passed")


def test_per_server_settings_need_a_server():
    """With no server there is nowhere to put these, and reporting success for
    a write that went nowhere is worse than refusing it."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg(tmp)
        for call in (lambda: c.set_sync_mode("update"),
                     lambda: c.set_automation(live_sync=True)):
            try:
                call()
                raise AssertionError("expected a refusal with no server")
            except ValueError as exc:
                assert "nasync setup" in str(exc), exc
        print("✅ test_per_server_settings_need_a_server passed")


def test_automation_defaults_to_off():
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg_with_server(tmp)
        a = c.get_automation()
        # Automatic syncing can delete on the server; nobody gets opted in.
        assert a["live_sync"] is False
        assert a["scheduled_sync"] is False
        assert a["scheduled_time"] == "02:00"
        assert c.automation_wanted() is False

        c.set_automation(live_sync=True)
        assert c.automation_wanted() is True
        assert Config().get_automation()["live_sync"] is True
        print("✅ test_automation_defaults_to_off passed")


def test_automation_is_per_server_and_wanted_asks_about_any():
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg_with_server(tmp, "home")
        c.add_or_update_server("work", "10.0.0.9", "u2", "pw2", "share2", 445)

        c.set_automation(live_sync=True, server="home")
        assert c.get_automation("home")["live_sync"] is True
        assert c.get_automation("work")["live_sync"] is False

        # Per server, and app-wide when asked without one — which is what
        # decides whether the background service is needed at all.
        assert c.automation_wanted("home") is True
        assert c.automation_wanted("work") is False
        assert c.automation_wanted() is True
        assert c.automated_servers() == ["home"]

        c.set_automation(scheduled_sync=True, scheduled_time="04:00",
                         server="work")
        assert sorted(c.automated_servers()) == ["home", "work"]
        # Each keeps its own time.
        assert c.get_automation("home")["scheduled_time"] == "02:00"
        assert c.get_automation("work")["scheduled_time"] == "04:00"
        print("✅ test_automation_is_per_server_and_wanted_asks_about_any passed")


def test_scheduled_run_dates_are_tracked_per_server():
    """One NAS being unreachable must not mark another's day as done."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg_with_server(tmp, "home")
        c.add_or_update_server("work", "10.0.0.9", "u2", "pw2", "share2", 445)

        c.record_scheduled_run("2026-08-30", server="home")
        assert c.get_automation("home")["last_scheduled_run"] == "2026-08-30"
        assert c.get_automation("work")["last_scheduled_run"] is None
        print("✅ test_scheduled_run_dates_are_tracked_per_server passed")


def test_background_gates_the_service_without_clearing_triggers():
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg_with_server(tmp)
        # On by default, so turning a trigger on just works.
        assert c.get_automation()["background"] is True
        c.set_automation(live_sync=True)
        assert c.background_service_wanted() is True

        c.set_automation(background=False)
        assert c.background_service_wanted() is False
        assert c.automation_wanted() is True          # the trigger is still set
        assert c.get_automation()["live_sync"] is True
        print("✅ test_background_gates_the_service_without_clearing_triggers passed")


def test_background_stays_global_across_servers():
    """There is one systemd unit, so pausing unattended syncing is one fact
    about the machine, not a per-server choice."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg_with_server(tmp, "home")
        c.add_or_update_server("work", "10.0.0.9", "u2", "pw2", "share2", 445)

        c.set_automation(background=False, server="home")
        assert c.get_automation("work")["background"] is False
        assert c.get_background() is False
        # It is not written onto the server it was set through.
        assert "background" not in c._server_entry("home")
        print("✅ test_background_stays_global_across_servers passed")


def test_scheduled_time_is_validated_and_normalized():
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg_with_server(tmp)
        assert c.set_automation(scheduled_time="7:5")["scheduled_time"] == "07:05"

        for bad in ("25:00", "02:61", "2pm", "0200", ""):
            try:
                c.set_automation(scheduled_time=bad)
                raise AssertionError(f"expected '{bad}' to be rejected")
            except ValueError:
                pass
        assert c.get_automation()["scheduled_time"] == "07:05"

        # A hand-edited value falls back rather than crashing the daemon.
        c._server_entry("home")["scheduled_time"] = "nonsense"
        assert c.get_automation()["scheduled_time"] == "02:00"
        print("✅ test_scheduled_time_is_validated_and_normalized passed")


def test_legacy_automation_keys_migrate_to_live_sync():
    """Anyone who ran `nasync auto` before the rename had watching on, and
    keeps it. Everyone else lands on the new default of off."""
    for legacy, expected in (
        ({"enabled": True, "on_change": True, "interval_minutes": 0}, True),
        ({"enabled": False, "on_change": True, "interval_minutes": 0}, False),
        ({"enabled": True, "on_change": False, "interval_minutes": 15}, False),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_CONFIG_HOME"] = tmp
            cfgdir = os.path.join(tmp, "nasync")
            os.makedirs(cfgdir)
            # A config this old is the single-server format, so it has a server
            # for the migrated settings to land on — which is the point: they
            # always described whichever server was in use.
            json.dump({"automation": dict(legacy),
                       "server": {"ip": "10.0.0.5", "username": "u",
                                  "share": "backup", "port": 445}},
                      open(os.path.join(cfgdir, "config.json"), "w"))

            a = Config().get_automation()
            assert a["live_sync"] is expected, (legacy, a)
            assert a["scheduled_sync"] is False
            assert a["interval_minutes"] == legacy["interval_minutes"]
    print("✅ test_legacy_automation_keys_migrate_to_live_sync passed")


def _write_config(tmp, data):
    os.environ["XDG_CONFIG_HOME"] = tmp
    cfgdir = os.path.join(tmp, "nasync")
    os.makedirs(cfgdir, exist_ok=True)
    json.dump(data, open(os.path.join(cfgdir, "config.json"), "w"))


#: A pre-0.18.0 config: one app-wide mode and one app-wide schedule, two
#: servers, and only the active one ever actually automated by them.
_PRE_0_18 = {
    "servers": [
        {"name": "home", "ip": "10.0.0.5", "username": "u", "share": "backup",
         "port": 445},
        {"name": "work", "ip": "10.0.0.9", "username": "u2", "share": "share2",
         "port": 445},
    ],
    "active_server": "home",
    "sync_mode": "update",
    "automation": {"live_sync": True, "scheduled_sync": True,
                   "scheduled_time": "03:30", "last_scheduled_run": "2026-08-01",
                   "background": False, "interval_minutes": 7},
}


def test_migration_gives_the_old_settings_to_the_active_server_only():
    """Nothing that wasn't syncing by itself before the upgrade starts doing so
    because of it — inheriting a trigger would begin unattended mirror syncs on
    a NAS nobody asked to automate."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_config(tmp, dict(_PRE_0_18))
        c = Config()

        home = c.get_automation("home")
        assert c.get_sync_mode("home") == "update"
        assert home["live_sync"] is True and home["scheduled_sync"] is True
        assert home["scheduled_time"] == "03:30"
        assert home["last_scheduled_run"] == "2026-08-01"
        assert home["interval_minutes"] == 7

        work = c.get_automation("work")
        assert work["live_sync"] is False and work["scheduled_sync"] is False
        assert work["scheduled_time"] == "02:00"        # the default, not 03:30
        assert c.get_sync_mode("work") == "mirror"      # the default, not update
        assert c.automated_servers() == ["home"]

        # `background` is app-wide and stays where it was.
        assert c.get_background() is False
        assert home["background"] is False and work["background"] is False

        # One source of truth afterwards: the global copies are gone.
        assert "sync_mode" not in c.data
        assert "live_sync" not in c.data.get("automation", {})
        print("✅ test_migration_gives_the_old_settings_to_the_active_server_only passed")


def test_migration_is_idempotent_and_does_not_undo_later_changes():
    with tempfile.TemporaryDirectory() as tmp:
        _write_config(tmp, dict(_PRE_0_18))
        Config()                                     # migrates

        changed = Config()
        changed.set_sync_mode("mirror", server="home")
        changed.set_automation(live_sync=False, server="home")

        again = Config()                             # a later run must not revert
        assert again.get_sync_mode("home") == "mirror"
        assert again.get_automation("home")["live_sync"] is False
        print("✅ test_migration_is_idempotent_and_does_not_undo_later_changes passed")


def test_migration_with_no_servers_drops_the_orphaned_block():
    """With nothing to describe, the old block is not kept as a second source
    of truth that a later-added server would silently inherit."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_config(tmp, {"sync_mode": "update",
                            "automation": {"live_sync": True}})
        c = Config()
        assert "sync_mode" not in c.data
        assert "live_sync" not in c.data.get("automation", {})

        c.add_or_update_server("new", "10.0.0.1", "u", "p", "s", 445)
        assert c.get_sync_mode("new") == "mirror"
        assert c.get_automation("new")["live_sync"] is False
        print("✅ test_migration_with_no_servers_drops_the_orphaned_block passed")


def test_record_scheduled_run_does_not_clobber_concurrent_edits():
    """The daemon writes the run date while the desktop app may be writing
    settings; a stale in-memory copy would silently undo them."""
    with tempfile.TemporaryDirectory() as tmp:
        daemon = _cfg_with_server(tmp)
        daemon.get_automation()                      # daemon's view is now stale

        app = Config()                               # the GUI, another process
        app.set_automation(live_sync=True, scheduled_sync=True)

        daemon.record_scheduled_run("2026-08-06")

        fresh = Config().get_automation()
        assert fresh["last_scheduled_run"] == "2026-08-06"
        assert fresh["live_sync"] is True            # the GUI's change survived
        assert fresh["scheduled_sync"] is True
        print("✅ test_record_scheduled_run_does_not_clobber_concurrent_edits passed")


# --------------------------------------------------------------------------- #
# Importing a pip install's config the first time the Flatpak runs.
#
# Under Flatpak, XDG_CONFIG_HOME points inside ~/.var/app, so without this the
# app opens empty and every server and folder has to be set up again — and
# re-picking a folder in mirror mode is the operation with the worst failure
# mode NASync has.
# --------------------------------------------------------------------------- #
import pytest

import nasync.core.config as config_module


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """A fake sandbox: our config dir, and a separate 'host' one to import."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "sandbox"))
    host = tmp_path / "host" / "nasync"
    host.mkdir(parents=True)
    monkeypatch.setattr(config_module, "host_config_dir", lambda: host)
    monkeypatch.setattr(config_module, "in_flatpak", lambda: True)
    return host


def _seed_host(host, password="hunter2"):
    (host / "config.json").write_text(json.dumps({
        "servers": [{"name": "nas", "ip": "192.168.1.50", "username": "u",
                     "share": "backup", "port": 445}],
        "active_server": "nas",
        "pairs_by_server": {"nas": [{"name": "docs", "local": "/home/u/Documents",
                                     "server": "/docs"}]},
    }))
    (host / ".creds").write_text(json.dumps({"nas": password}))


def test_first_sandboxed_run_adopts_an_existing_install(sandbox):
    _seed_host(sandbox)
    config = Config()

    assert config.imported_from_host is True
    assert [s["name"] for s in config.list_servers()] == ["nas"]
    assert config.get_active_server_name() == "nas"
    assert [p["name"] for p in config.get_pairs()] == ["docs"]
    # Servers arriving without their passwords would look imported but fail to
    # connect, which is worse than not importing at all.
    assert config.get_server("nas")["password"] == "hunter2"
    print("✅ test_first_sandboxed_run_adopts_an_existing_install passed")


def test_import_copies_rather_than_moves(sandbox):
    """The pip install has to keep working — this is reversible."""
    _seed_host(sandbox)
    before = (sandbox / "config.json").read_text()

    Config()

    assert (sandbox / "config.json").read_text() == before
    assert (sandbox / ".creds").is_file()
    print("✅ test_import_copies_rather_than_moves passed")


def test_imported_credentials_are_not_world_readable(sandbox):
    """copyfile doesn't carry permissions, and this file is the whole reason
    the directory is locked down."""
    _seed_host(sandbox)
    config = Config()

    assert oct(config.creds_file.stat().st_mode & 0o777) == "0o600"
    print("✅ test_imported_credentials_are_not_world_readable passed")


def test_import_never_overwrites_what_the_sandboxed_app_already_has(sandbox):
    # The sandboxed app is set up first and has its own config...
    existing = Config()
    existing.add_or_update_server("mine", "10.0.0.1", "me", "pw", "share")

    # ...and a pip install appears afterwards. Adopting it now would replace
    # settings the user made in this app, which is not an import, it's a reset.
    _seed_host(sandbox)
    second = Config()

    assert second.imported_from_host is False
    assert [s["name"] for s in second.list_servers()] == ["mine"]
    print("✅ test_import_never_overwrites_what_the_sandboxed_app_already_has passed")


def test_no_import_outside_a_sandbox(sandbox, monkeypatch):
    """Outside Flatpak the two paths are the same install; there is nothing to
    import and copying would be a self-copy."""
    monkeypatch.setattr(config_module, "in_flatpak", lambda: False)
    _seed_host(sandbox)

    assert Config().imported_from_host is False
    print("✅ test_no_import_outside_a_sandbox passed")


def test_no_import_when_source_and_destination_are_the_same_directory(
        monkeypatch, tmp_path):
    """FLATPAK_ID set by hand on an ordinary machine points both at
    ~/.config/nasync — copying a file onto itself would truncate it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(config_module, "in_flatpak", lambda: True)
    monkeypatch.setattr(config_module, "host_config_dir",
                        lambda: tmp_path / "nasync")

    same = tmp_path / "nasync"
    same.mkdir(parents=True, exist_ok=True)
    (same / "config.json").write_text(json.dumps({"servers": [{"name": "keep"}]}))

    config = Config()
    assert config.imported_from_host is False
    assert json.loads((same / "config.json").read_text())["servers"][0]["name"] == "keep"
    print("✅ test_no_import_when_source_and_destination_are_the_same_directory passed")


def test_nothing_to_import_is_not_an_error(sandbox):
    assert Config().imported_from_host is False
    print("✅ test_nothing_to_import_is_not_an_error passed")


def test_an_unreadable_host_config_still_starts_the_app(sandbox, monkeypatch):
    """Starting empty is what would have happened anyway; refusing to start is
    not an improvement on it."""
    _seed_host(sandbox)

    def denied(*args, **kwargs):
        raise PermissionError("nope")

    monkeypatch.setattr(config_module.shutil, "copyfile", denied)

    config = Config()
    assert config.imported_from_host is False
    assert config.list_servers() == []
    print("✅ test_an_unreadable_host_config_still_starts_the_app passed")


def test_encryption_defaults_off_and_survives_a_rename(config_home):
    """Off by default, because defaulting to on breaks every NAS too old for
    SMB 3 — a security improvement that turns into an outage."""
    config = Config()
    config.add_or_update_server("nas", "10.0.0.2", "u", "pw", "share")
    assert config.get_server("nas")["encrypt"] is False
    assert config.get_server_config()["encrypt"] is False

    config.save_server("nas", "10.0.0.2", "u", "pw", "share", encrypt=True)
    assert config.get_server("nas")["encrypt"] is True

    # Renaming goes through the remove-and-re-add path; the flag has to make
    # the journey along with the pairs.
    config.add_pair("/tmp", "/dest", "p")
    config.save_server("nas2", "10.0.0.2", "u", "pw", "share", orig_name="nas",
                       encrypt=True)
    assert config.get_server("nas2")["encrypt"] is True
    assert [p["name"] for p in config.get_pairs("nas2")] == ["p"]
    print("✅ test_encryption_defaults_off_and_survives_a_rename passed")


def test_a_server_saved_before_0_12_reads_as_unencrypted(config_home):
    """No `encrypt` key at all is what every existing install has on disk."""
    config = Config()
    config.add_or_update_server("old", "10.0.0.3", "u", "pw", "share")
    del config.data["servers"][0]["encrypt"]
    config.save()

    reopened = Config()
    assert reopened.get_server("old")["encrypt"] is False
    assert reopened.get_server_config()["encrypt"] is False
    print("✅ test_a_server_saved_before_0_12_reads_as_unencrypted passed")


if __name__ == "__main__":
    test_server_crud()
    test_migration_from_legacy()
    test_pairs()
    test_sync_mode()
    test_automation_defaults_to_off()
    test_background_gates_the_service_without_clearing_triggers()
    test_hints_are_remembered()
    test_scheduled_time_is_validated_and_normalized()
    test_legacy_automation_keys_migrate_to_live_sync()
    test_record_scheduled_run_does_not_clobber_concurrent_edits()


# --------------------------------------------------------------------------- #
# The sync root (0.19.0)
# --------------------------------------------------------------------------- #
def test_pairs_land_inside_the_sync_root():
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg(tmp)
        c.add_or_update_server("nas", "10.0.0.5", "u", "p", "Storage", 445,
                               root="sync")
        c.add_pair("/home/user/docs", "/Documents", "docs")
        assert c.resolve_remote(c.get_pair("docs")["server"]) == "sync/Documents"
        print("✅ test_pairs_land_inside_the_sync_root passed")


def test_a_pair_cannot_name_a_path_outside_the_root():
    """The whole point: not "no pair happens to point at the wedding video",
    but "no pair is able to name it"."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg(tmp)
        c.add_or_update_server("nas", "10.0.0.5", "u", "p", "Storage", 445,
                               root="sync")
        for escape in ("../Marrige video", "/../etc", "a/../../b", "..\\up"):
            try:
                c.add_pair("/home/user/x", escape, f"p{len(escape)}")
                raise AssertionError(f"escaped the root with {escape!r}")
            except ValueError:
                pass
        print("✅ test_a_pair_cannot_name_a_path_outside_the_root passed")


def test_a_root_of_its_own_is_not_escapable_by_the_share_root_trick():
    """A destination of "/" used to mean the whole share, which made mirror
    delete everything on it. With a root it means the root itself."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg(tmp)
        c.add_or_update_server("nas", "10.0.0.5", "u", "p", "Storage", 445,
                               root="sync")
        c.add_pair("/home/user/everything", "/", "all")
        assert c.resolve_remote(c.get_pair("all")["server"]) == "sync"
        print("✅ test_a_root_of_its_own_is_not_escapable_by_the_share_root_trick passed")


def test_an_existing_server_without_a_root_is_unchanged():
    """Servers configured before 0.19.0 keep syncing exactly where they did;
    the guarantee is opted into, not imposed on a layout NASync can't see."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg(tmp)
        c.add_or_update_server("old", "10.0.0.5", "u", "p", "Storage", 445)
        c.add_pair("/home/user/docs", "/Documents", "docs")
        assert c.get_root("old") == ""
        assert c.resolve_remote(c.get_pair("docs")["server"]) == "Documents"
        print("✅ test_an_existing_server_without_a_root_is_unchanged passed")


def test_the_root_survives_editing_and_renaming_a_server():
    """`add_or_update_server` rebuilds the entry from its arguments, so the one
    field whose loss is dangerous has to be carried across explicitly."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg(tmp)
        c.add_or_update_server("nas", "10.0.0.5", "u", "p", "Storage", 445,
                               root="sync")
        c.add_or_update_server("nas", "10.0.0.5", "u", "p", "Storage", 4450)
        assert c.get_root("nas") == "sync"
        c.save_server("attic", "10.0.0.5", "u", "p", "Storage", 4450,
                      orig_name="nas")
        assert c.get_root("attic") == "sync"
        assert Config().get_root("attic") == "sync"
        print("✅ test_the_root_survives_editing_and_renaming_a_server passed")


def test_changing_the_root_moves_every_pair_with_it():
    """Pairs store their destination relative to the root, so the root is the
    single place that decides where a server's syncing happens."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _cfg(tmp)
        c.add_or_update_server("nas", "10.0.0.5", "u", "p", "Storage", 445,
                               root="sync")
        c.add_pair("/home/user/docs", "/Documents", "docs")
        assert c.resolve_remote("Documents") == "sync/Documents"
        c.set_root("backups/laptop", server="nas")
        assert c.resolve_remote("Documents") == "backups/laptop/Documents"
        print("✅ test_changing_the_root_moves_every_pair_with_it passed")


def test_remote_paths_are_cleaned_the_way_people_type_them():
    """These come from text fields and pasted paths. " /backups/ " has to mean
    the same folder as "backups" — while a folder genuinely called
    "Marrige video" keeps its space."""
    from nasync.core.config import normalize_remote
    assert normalize_remote("  /backups/laptop/ ") == "backups/laptop"
    assert normalize_remote("Marrige video") == "Marrige video"
    assert normalize_remote(" Marrige video / clips ") == "Marrige video/clips"
    assert normalize_remote("a//b") == "a/b"
    assert normalize_remote("/") == ""
    assert normalize_remote("   ") == ""
    assert normalize_remote(None) == ""
    # Backslashes too: someone reading a path off a Windows machine.
    assert normalize_remote(r"backups\\laptop") == "backups/laptop"
    print("✅ test_remote_paths_are_cleaned_the_way_people_type_them passed")
