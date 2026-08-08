import os
import tempfile
from click.testing import CliRunner


class _FakeSMB:
    """Stand-in for SAMBAClient so setup needs no real server."""
    def __init__(self, *a, **k):
        pass

    def list_shares(self):
        return ["backup"]

    def connect(self):
        return None

    def release(self):
        return None

    def disconnect(self):
        return None


def _invoke_setup(tmp, input_text, seed_pairs=None):
    os.environ["XDG_CONFIG_HOME"] = tmp
    import nasync.cli as cli
    from nasync.core.config import Config

    cli.SAMBAClient = _FakeSMB
    if seed_pairs:
        c = Config()
        c.set_server("127.0.0.1", "u", "p", "backup", 445)
        for name in seed_pairs:
            c.add_pair("/tmp/" + name, "/" + name, name)

    result = CliRunner().invoke(cli.setup, input=input_text, catch_exceptions=True)
    return result, Config


# Answers: ip, port(=default), user, password(blank), share(=default 1),
# server-name(=default), keep existing?, local, dest, pair-name(=default), add another?
def test_setup_replaces_existing_pairs():
    """Regression: the 'don't keep' path used to crash because the `list`
    command shadowed the builtin list()."""
    with tempfile.TemporaryDirectory() as tmp:
        result, Config = _invoke_setup(
            tmp,
            "127.0.0.1\n\nu\n\n\n\nn\n/tmp\n/Docs\n\nn\n",
            seed_pairs=["Documents"],
        )
        assert result.exception is None, result.exception
        assert result.exit_code == 0, result.output
        names = [p["name"] for p in Config().get_pairs()]
        assert names == ["tmp"], names
        print("✅ test_setup_replaces_existing_pairs passed")


def test_setup_keeps_existing_pairs():
    with tempfile.TemporaryDirectory() as tmp:
        result, Config = _invoke_setup(
            tmp,
            "127.0.0.1\n\nu\n\n\n\ny\n/tmp\n/More\n\nn\n",
            seed_pairs=["Documents"],
        )
        assert result.exception is None, result.exception
        assert result.exit_code == 0, result.output
        names = sorted(p["name"] for p in Config().get_pairs())
        assert names == ["Documents", "tmp"], names
        print("✅ test_setup_keeps_existing_pairs passed")


def test_list_command_does_not_shadow_builtin():
    import nasync.cli as cli
    import click
    # The CLI command must be exposed as 'list' but the module name must not
    # rebind the builtin.
    assert isinstance(cli.list_pairs, click.Command)
    assert cli.list_pairs.name == "list"
    assert not isinstance(getattr(cli, "list", list), click.Command)
    print("✅ test_list_command_does_not_shadow_builtin passed")


def test_mode_command_shows_and_sets():
    import nasync.cli as cli
    from nasync.core.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        runner = CliRunner()

        result = runner.invoke(cli.mode, [], catch_exceptions=False)
        assert "mirror" in result.output, result.output

        result = runner.invoke(cli.mode, ["update"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert Config().get_sync_mode() == "update"

        # An unknown mode is rejected by click before it reaches the config.
        result = runner.invoke(cli.mode, ["hard"])
        assert result.exit_code != 0
        assert Config().get_sync_mode() == "update"
        print("✅ test_mode_command_shows_and_sets passed")


def test_notifications_command_shows_and_sets(monkeypatch):
    import nasync.cli as cli
    from nasync.system import notify
    from nasync.core.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        monkeypatch.setattr(notify, "available", lambda: True)
        runner = CliRunner()

        # On by default: a background sync nobody is told about is the problem
        # notifications exist to solve.
        result = runner.invoke(cli.notifications, [], catch_exceptions=False)
        assert "on" in result.output, result.output

        result = runner.invoke(cli.notifications, ["off"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert Config().get_notifications() is False

        result = runner.invoke(cli.notifications, ["on"], catch_exceptions=False)
        assert Config().get_notifications() is True
        print("✅ test_notifications_command_shows_and_sets passed")


def test_notifications_command_admits_a_machine_cannot_show_them(monkeypatch):
    """"on" while nothing can ever appear would be a lie, and would send
    someone hunting for a sync bug that isn't there."""
    import nasync.cli as cli
    from nasync.system import notify

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        monkeypatch.setattr(notify, "available", lambda: False)

        result = CliRunner().invoke(cli.notifications, [], catch_exceptions=False)
        assert "no notification daemon" in result.output, result.output

        # --test refuses rather than silently succeeding.
        result = CliRunner().invoke(cli.notifications, ["--test"])
        assert result.exit_code == 1, result.output
        print("✅ test_notifications_command_admits_a_machine_cannot_show_them passed")


class _BrowsableSMB(_FakeSMB):
    """A client whose share has one directory of mixed content in it."""

    listed = []

    def list_directory(self, path=""):
        _BrowsableSMB.listed.append(path)
        if path.strip("/") == "missing":
            raise Exception("'missing' doesn't exist on the server.")
        return [
            {"name": "photos", "path": "photos", "is_dir": True,
             "is_symlink": False, "size": None},
            {"name": "notes.txt", "path": "notes.txt", "is_dir": False,
             "is_symlink": False, "size": 2048},
            {"name": "shortcut", "path": "shortcut", "is_dir": False,
             "is_symlink": True, "size": None},
        ]


def _ls_config(tmp):
    os.environ["XDG_CONFIG_HOME"] = tmp
    import nasync.cli as cli
    from nasync.core.config import Config

    cli.SAMBAClient = _BrowsableSMB
    _BrowsableSMB.listed = []
    config = Config()
    config.set_server("127.0.0.1", "u", "p", "backup", 445)
    return cli, config


def test_ls_lists_a_remote_directory():
    with tempfile.TemporaryDirectory() as tmp:
        cli, _ = _ls_config(tmp)
        result = CliRunner().invoke(cli.ls, [], catch_exceptions=False)

        assert "photos/" in result.output
        assert "notes.txt" in result.output
        assert "2.0 KB" in result.output          # from the shared formatter
        assert "1 folder(s), 2 file(s)" in result.output
        print("✅ test_ls_lists_a_remote_directory passed")


def test_ls_passes_the_path_through_to_the_server():
    with tempfile.TemporaryDirectory() as tmp:
        cli, _ = _ls_config(tmp)
        CliRunner().invoke(cli.ls, ["photos/2024"], catch_exceptions=False)
        assert _BrowsableSMB.listed == ["photos/2024"]
        print("✅ test_ls_passes_the_path_through_to_the_server passed")


def test_ls_reports_a_missing_directory_and_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        cli, _ = _ls_config(tmp)
        result = CliRunner().invoke(cli.ls, ["missing"])
        assert result.exit_code == 1, result.output
        assert "doesn't exist" in result.output
        print("✅ test_ls_reports_a_missing_directory_and_exits_nonzero passed")


def test_ls_without_a_server_says_so_rather_than_crashing():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        import nasync.cli as cli

        result = CliRunner().invoke(cli.ls, [])
        assert result.exit_code == 1, result.output
        assert "No server configured" in result.output
        print("✅ test_ls_without_a_server_says_so_rather_than_crashing passed")


if __name__ == "__main__":
    test_setup_replaces_existing_pairs()
    test_setup_keeps_existing_pairs()
    test_list_command_does_not_shadow_builtin()
    test_mode_command_shows_and_sets()
