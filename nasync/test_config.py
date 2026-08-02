import tempfile
from pathlib import Path
from nasync.config import Config


def _fresh_config(tmpdir):
    config_dir = Path(tmpdir)
    config = Config()
    config.config_dir = config_dir
    config.config_file = config_dir / "config.json"
    config.creds_file = config_dir / ".creds"
    config._load()  # reset in-memory state to the (empty) tmp location
    return config


def test_config_creation():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _fresh_config(tmpdir)

        config.set_server("192.0.2.10", "testuser", "testpass", "backup")
        assert config.data["server"]["ip"] == "192.0.2.10"
        assert config.config_file.exists()
        # Password lives only in the creds file, never in config.json.
        assert "testpass" not in config.config_file.read_text()
        assert oct(config.creds_file.stat().st_mode)[-3:] == "600"

        name = config.add_pair("/home/user/docs", "/server/backups/docs", "Documents")
        assert name == "Documents"
        assert len(config.get_pairs()) == 1

        pair = config.get_pair("Documents")
        assert pair["local"] == "/home/user/docs"
        assert pair["server"] == "/server/backups/docs"

        config.remove_pair("Documents")
        assert len(config.get_pairs()) == 0

        print("✅ test_config_creation passed")


def test_duplicate_pair_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _fresh_config(tmpdir)
        config.add_pair("/a", "/remote/a", "dup")
        try:
            config.add_pair("/b", "/remote/b", "dup")
        except ValueError:
            print("✅ test_duplicate_pair_rejected passed")
            return
        raise AssertionError("Expected ValueError for duplicate pair name")


def test_default_name_from_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _fresh_config(tmpdir)
        name = config.add_pair("/home/user/Photos/", "/remote/photos")
        assert name == "Photos", name
        print("✅ test_default_name_from_path passed")


if __name__ == "__main__":
    test_config_creation()
    test_duplicate_pair_rejected()
    test_default_name_from_path()
