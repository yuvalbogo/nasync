"""`nasync` for the end-to-end suite: the real CLI, minus the real keyring.

Saved passwords go to the test profile's own file, whatever the session bus
offers — a test must never read or write the person's keyring.
"""
import sys

from nasync.system import vault

vault.backend = lambda: ""

from nasync.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.argv[0] = "nasync"
    main()
