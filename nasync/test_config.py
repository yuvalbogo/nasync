import json
import os
import tempfile
from nasync.config import Config


def _cfg(tmp):
    os.environ["XDG_CONFIG_HOME"] = tmp
    return Config()


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
        assert c.get_pair("Docs")["server"] == "/DocsV2"
        # editing to a name owned by another pair is rejected
        try:
            c.update_pair("Docs", "/x", "/x", "Photos")
            raise AssertionError("expected rename collision to be rejected")
        except ValueError:
            pass

        c.remove_pair("Docs")
        assert [p["name"] for p in c.get_pairs()] == ["Photos"]
        print("✅ test_pairs passed")


if __name__ == "__main__":
    test_server_crud()
    test_migration_from_legacy()
    test_pairs()
