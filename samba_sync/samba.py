import os
import subprocess
from pathlib import Path
import tempfile


class SAMBAClient:
    def __init__(self, ip: str, username: str, password: str, share: str):
        self.ip = ip
        self.username = username
        self.password = password
        self.share = share
        self.url = f"smb://{ip}/{share}"

    def _run_smbclient(self, commands: list) -> str:
        """Run smbclient with commands"""
        cmd_str = "\n".join(commands)
        env = os.environ.copy()
        env["SMBCLIENT_PASSWORD"] = self.password
        try:
            result = subprocess.run(
                ["smbclient", self.url, "-U", self.username, "-p", "445"],
                input=cmd_str,
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
            if result.returncode != 0 and "NT_STATUS" in result.stderr:
                raise Exception(f"SAMBA error: {result.stderr}")
            return result.stdout
        except FileNotFoundError:
            raise Exception(
                "smbclient not found. Install samba-client: "
                "Ubuntu/Debian: sudo apt install samba-client, "
                "Fedora: sudo dnf install samba-client"
            )
        except Exception as e:
            raise Exception(f"SAMBA command failed: {e}")

    def connect(self):
        try:
            self._run_smbclient(["quit"])
        except Exception as e:
            raise Exception(f"Failed to connect to SAMBA server: {e}")

    def disconnect(self):
        pass

    def upload_file(self, local_path: str, remote_path: str):
        if not os.path.exists(local_path):
            raise Exception(f"Local file not found: {local_path}")

        remote_file = remote_path.lstrip("/").replace("/", "\\")
        commands = [
            f"put \"{local_path}\" \"{remote_file}\"",
            "quit",
        ]
        self._run_smbclient(commands)

    def download_file(self, remote_path: str, local_path: str):
        local_dir = os.path.dirname(local_path)
        if local_dir:
            os.makedirs(local_dir, exist_ok=True)

        remote_file = remote_path.lstrip("/").replace("/", "\\")
        commands = [
            f"get \"{remote_file}\" \"{local_path}\"",
            "quit",
        ]
        self._run_smbclient(commands)

    def mkdir(self, remote_path: str):
        remote_dir = remote_path.lstrip("/").replace("/", "\\")
        commands = [
            f"mkdir \"{remote_dir}\"",
            "quit",
        ]
        try:
            self._run_smbclient(commands)
        except Exception as e:
            if "already exists" not in str(e):
                pass

    def sync_directory(self, local_dir: str, remote_dir: str, direction="push"):
        if direction == "push":
            self._push_directory(local_dir, remote_dir)
        elif direction == "pull":
            self._pull_directory(local_dir, remote_dir)

    def _push_directory(self, local_dir: str, remote_dir: str):
        for item in Path(local_dir).rglob("*"):
            if item.is_file():
                rel_path = item.relative_to(local_dir)
                remote_file = f"{remote_dir}/{rel_path}".replace("\\", "/")
                remote_parent = "/".join(remote_file.split("/")[:-1])
                if remote_parent:
                    self.mkdir(remote_parent)
                try:
                    self.upload_file(str(item), remote_file)
                    print(f"Synced: {item.name}")
                except Exception as e:
                    print(f"Warning: Failed to sync {item.name}: {e}")

    def _pull_directory(self, local_dir: str, remote_dir: str):
        os.makedirs(local_dir, exist_ok=True)
        self._pull_recursive(remote_dir, local_dir)

    def _pull_recursive(self, remote_path: str, local_path: str):
        try:
            remote_dir = remote_path.lstrip("/").replace("/", "\\")
            commands = [
                f"ls \"{remote_dir}/*\"",
                "quit",
            ]
            output = self._run_smbclient(commands)

            for line in output.split("\n"):
                line = line.strip()
                if not line or line.startswith("Domain"):
                    continue

                parts = line.split()
                if len(parts) < 2:
                    continue

                filename = parts[0]
                if filename in [".", ".."]:
                    continue

                remote_file = f"{remote_path}/{filename}".replace("\\", "/")
                local_file = os.path.join(local_path, filename)

                if "D" in line:
                    os.makedirs(local_file, exist_ok=True)
                    self._pull_recursive(remote_file, local_file)
                else:
                    try:
                        self.download_file(remote_file, local_file)
                        print(f"Restored: {filename}")
                    except Exception as e:
                        print(f"Warning: Failed to restore {filename}: {e}")
        except Exception as e:
            print(f"Warning: {e}")
