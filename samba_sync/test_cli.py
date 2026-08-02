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


def _invoke_setup(tmp, input_text, seed_pairs=None):
    os.environ["XDG_CONFIG_HOME"] = tmp
    import samba_sync.cli as cli
    from samba_sync.config import Config

    cli.SAMBAClient = _FakeSMB
    if seed_pairs:
        c = Config()
        c.set_server("127.0.0.1", "u", "p", "backup", 445)
        for name in seed_pairs:
            c.add_pair("/tmp/" + name, "/" + name, name)

    result = CliRunner().invoke(cli.setup, input=input_text, catch_exceptions=True)
    return result, Config


# Answers: ip, port(=default), user, password(blank), share(=default 1),
# keep existing? , local folder, dest, name(=default), add another? -> n
def test_setup_replaces_existing_pairs():
    """Regression: the 'don't keep' path used to crash because the `list`
    command shadowed the builtin list()."""
    with tempfile.TemporaryDirectory() as tmp:
        result, Config = _invoke_setup(
            tmp,
            "127.0.0.1\n\nu\n\n\nn\n/tmp\n/Docs\n\nn\n",
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
            "127.0.0.1\n\nu\n\n\ny\n/tmp\n/More\n\nn\n",
            seed_pairs=["Documents"],
        )
        assert result.exception is None, result.exception
        assert result.exit_code == 0, result.output
        names = sorted(p["name"] for p in Config().get_pairs())
        assert names == ["Documents", "tmp"], names
        print("✅ test_setup_keeps_existing_pairs passed")


def test_list_command_does_not_shadow_builtin():
    import samba_sync.cli as cli
    import click
    # The CLI command must be exposed as 'list' but the module name must not
    # rebind the builtin.
    assert isinstance(cli.list_pairs, click.Command)
    assert cli.list_pairs.name == "list"
    assert not isinstance(getattr(cli, "list", list), click.Command)
    print("✅ test_list_command_does_not_shadow_builtin passed")


if __name__ == "__main__":
    test_setup_replaces_existing_pairs()
    test_setup_keeps_existing_pairs()
    test_list_command_does_not_shadow_builtin()
