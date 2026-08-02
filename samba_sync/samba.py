import os
from pathlib import Path
from smb.SMBConnection import SMBConnection
from smb.smb_structs import FILE_OVERWRITE


class SAMBAClient:
    def __init__(self, ip: str, username: str, password: str, share: str):
        self.ip = ip
        self.username = username
        self.password = password
        self.share = share
        self.conn = None

    def connect(self):
        try:
            self.conn = SMBConnection(
                self.username,
                self.password,
                "SyncClient",
                self.ip,
                use_ntlm_v2=True,
            )
            self.conn.connect((self.ip, 445))
            return True
        except Exception as e:
            raise Exception(f"Failed to connect to SAMBA server: {e}")

    def disconnect(self):
        if self.conn:
            self.conn.close()

    def upload_file(self, local_path: str, remote_path: str):
        try:
            with open(local_path, "rb") as f:
                self.conn.storeFile(self.share, remote_path, f)
        except Exception as e:
            raise Exception(f"Failed to upload {local_path}: {e}")

    def download_file(self, remote_path: str, local_path: str):
        try:
            local_dir = os.path.dirname(local_path)
            if local_dir:
                os.makedirs(local_dir, exist_ok=True)
            with open(local_path, "wb") as f:
                self.conn.retrieveFile(self.share, remote_path, f)
        except Exception as e:
            raise Exception(f"Failed to download {remote_path}: {e}")

    def list_files(self, remote_path: str):
        try:
            return self.conn.listPath(self.share, remote_path)
        except Exception as e:
            raise Exception(f"Failed to list files in {remote_path}: {e}")

    def mkdir(self, remote_path: str):
        try:
            self.conn.createDirectory(self.share, remote_path)
        except Exception as e:
            if "already exists" not in str(e):
                raise Exception(f"Failed to create directory {remote_path}: {e}")

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
                self.upload_file(str(item), remote_file)
                print(f"Synced: {item.name}")

    def _pull_directory(self, local_dir: str, remote_dir: str):
        os.makedirs(local_dir, exist_ok=True)
        self._pull_recursive(remote_dir, local_dir)

    def _pull_recursive(self, remote_path: str, local_path: str):
        try:
            items = self.list_files(remote_path)
            for item in items:
                if item.filename in [".", ".."]:
                    continue
                remote_file = f"{remote_path}/{item.filename}".replace("\\", "/")
                if item.isDirectory:
                    local_subdir = os.path.join(local_path, item.filename)
                    os.makedirs(local_subdir, exist_ok=True)
                    self._pull_recursive(remote_file, local_subdir)
                else:
                    local_file = os.path.join(local_path, item.filename)
                    self.download_file(remote_file, local_file)
                    print(f"Restored: {item.filename}")
        except Exception as e:
            print(f"Warning: {e}")
