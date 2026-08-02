import tempfile
import os
from pathlib import Path
from samba_sync.config import Config


def test_config_creation():
    """Test config file creation and management"""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        config_file = config_dir / "config.json"

        config = Config()
        config.config_dir = config_dir
        config.config_file = config_file
        config.creds_file = config_dir / ".creds"

        config.set_server("10.100.102.200", "testuser", "testpass", "backup")
        assert config.data["server"]["ip"] == "10.100.102.200"
        assert config_file.exists()

        config.add_pair("/home/user/docs", "/server/backups/docs", "Documents")
        assert len(config.get_pairs()) == 1

        pair = config.get_pair("Documents")
        assert pair["local"] == "/home/user/docs"
        assert pair["server"] == "/server/backups/docs"

        config.remove_pair("Documents")
        assert len(config.get_pairs()) == 0

        print("✅ Config tests passed")


if __name__ == "__main__":
    test_config_creation()
