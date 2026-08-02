import os
import platform
import tempfile


def test_install_and_uninstall():
    if platform.system() != "Linux":
        print("… skipped (not Linux)")
        return

    tmp = tempfile.mkdtemp()
    os.environ["XDG_DATA_HOME"] = tmp

    from nasync import desktop

    written = desktop.install()
    desktop_file = os.path.join(tmp, "applications", "nasync.desktop")
    icon_file = os.path.join(tmp, "icons", "hicolor", "scalable", "apps", "nasync.svg")

    assert os.path.isfile(desktop_file), written
    assert os.path.isfile(icon_file), written

    content = open(desktop_file).read()
    assert "Exec=" in content and " tui" in content        # launches the TUI
    assert os.path.isabs(content.split("Exec=")[1].split()[0])  # absolute path
    assert "Terminal=true" in content                      # opens in a terminal
    assert "Icon=nasync" in content

    removed = desktop.uninstall()
    assert not os.path.exists(desktop_file)
    assert not os.path.exists(icon_file)
    assert len(removed) == 2
    print("✅ test_install_and_uninstall passed")


if __name__ == "__main__":
    test_install_and_uninstall()
