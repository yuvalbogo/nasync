import os
import platform
import tempfile


def test_install_and_uninstall():
    if platform.system() != "Linux":
        print("… skipped (not Linux)")
        return

    tmp = tempfile.mkdtemp()
    os.environ["XDG_DATA_HOME"] = tmp

    from nasync.system import launcher as desktop

    app_id = desktop.APP_ID
    written = desktop.install(gui=True)
    desktop_file = os.path.join(tmp, "applications", f"{app_id}.desktop")
    icon_file = os.path.join(tmp, "icons", "hicolor", "scalable", "apps", f"{app_id}.svg")

    assert os.path.isfile(desktop_file), written
    assert os.path.isfile(icon_file), written

    content = open(desktop_file).read()
    # Exec is the wrapper, which runs the app — or says why it can't.
    script = content.split("Exec=")[1].split()[0]
    assert os.path.isabs(script) and script == str(desktop.wrapper())
    wrapper = open(script).read()
    assert '"$NASYNC" gui' in wrapper                       # opens the desktop app
    assert os.path.isabs(desktop.entry_exec(wrapper))       # absolute path
    assert os.access(script, os.X_OK)
    assert "Terminal=false" in content                      # not a terminal window
    assert "StartupNotify=true" in content
    # Icon name, entry name and app_id must agree or GNOME shows a generic icon.
    assert f"Icon={app_id}" in content
    assert f"StartupWMClass={app_id}" in content

    removed = desktop.uninstall()
    assert not os.path.exists(desktop_file)
    assert not os.path.exists(icon_file)
    assert not os.path.exists(script)
    assert len(removed) == 3
    print("✅ test_install_and_uninstall passed")


def test_installing_without_gtk_is_refused_rather_than_pointing_at_nothing():
    """A launcher in the apps list that opens nothing is worse than no entry —
    so this refuses and names the two ways forward."""
    if platform.system() != "Linux":
        print("… skipped (not Linux)")
        return
    import pytest

    from nasync.system import launcher as desktop

    os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
    with pytest.raises(RuntimeError) as excinfo:
        desktop.install(gui=False)
    assert "libadwaita" in str(excinfo.value)
    assert "Flatpak" in str(excinfo.value)
    print("✅ test_installing_without_gtk_is_refused passed")


def test_install_clears_the_pre_0_3_0_entry():
    """The old bare-'nasync' files would otherwise linger as a second,
    stale entry in the application menu."""
    if platform.system() != "Linux":
        print("… skipped (not Linux)")
        return

    tmp = tempfile.mkdtemp()
    os.environ["XDG_DATA_HOME"] = tmp

    from nasync.system import launcher as desktop

    legacy_desktop = os.path.join(tmp, "applications", "nasync.desktop")
    legacy_icon = os.path.join(tmp, "icons", "hicolor", "scalable", "apps", "nasync.svg")
    os.makedirs(os.path.dirname(legacy_desktop), exist_ok=True)
    os.makedirs(os.path.dirname(legacy_icon), exist_ok=True)
    open(legacy_desktop, "w").write("[Desktop Entry]\n")
    open(legacy_icon, "w").write("<svg/>")

    desktop.install(gui=True)
    assert not os.path.exists(legacy_desktop), "stale launcher left behind"
    assert not os.path.exists(legacy_icon), "stale icon left behind"
    assert os.path.isfile(os.path.join(tmp, "applications", f"{desktop.APP_ID}.desktop"))

    desktop.uninstall()
    print("✅ test_install_clears_the_pre_0_3_0_entry passed")


if __name__ == "__main__":
    test_install_and_uninstall()
    test_install_clears_the_pre_0_3_0_entry()


def test_a_program_that_is_gone_or_will_not_start_is_named():
    import os
    import stat
    import tempfile
    from nasync.system.launcher import entry_exec, exec_problem

    assert entry_exec("[Desktop Entry]\nExec=/a/b/nasync gui\n") == "/a/b/nasync"
    assert entry_exec("[Service]\nExecStart=/x/nasync auto\n") == "/x/nasync"
    assert "no longer exists" in exec_problem("/no/such/nasync")
    with tempfile.TemporaryDirectory() as tmp:
        broken = os.path.join(tmp, "nasync")
        with open(broken, "w") as handle:
            # What a virtualenv's script does after a Python upgrade.
            handle.write("#!/bin/sh\necho \"ModuleNotFoundError: No module "
                         "named 'nasync'\" >&2\nexit 1\n")
        os.chmod(broken, stat.S_IRWXU)
        assert "No module named" in exec_problem(broken)


def test_a_broken_program_behind_the_wrapper_is_still_found(monkeypatch, tmp_path):
    """The launcher runs the wrapper, so checking the launcher has to look at
    the program inside it — or a moved checkout reads as fine."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("NASYNC_EXEC", "/no/such/venv/bin/nasync")
    from nasync.system import launcher as desktop
    monkeypatch.setattr(desktop.os, "access", lambda path, mode: True)
    desktop.install(gui=True)
    assert "/no/such/venv/bin/nasync" in desktop.wrapper().read_text()
    assert "no longer exists" in desktop.problem()
    desktop.wrapper().unlink()
    assert "nasync-gui" in desktop.problem()


def test_a_stable_install_names_its_own_path(monkeypatch, tmp_path):
    """`make install-user` points everything at .../nasync/current, which the
    next install keeps valid — not at the versioned folder it resolves to."""
    stable = tmp_path / "current" / "bin" / "nasync"
    stable.parent.mkdir(parents=True)
    stable.write_text("#!/bin/sh\n")
    stable.chmod(0o755)
    monkeypatch.setenv("NASYNC_EXEC", str(stable))
    from nasync.system.launcher import _nasync_exec
    assert _nasync_exec() == str(stable)
    monkeypatch.setenv("NASYNC_EXEC", "relative/nasync")
    assert _nasync_exec() != "relative/nasync"
