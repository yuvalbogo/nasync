# Contributing to NASync

Thanks for helping. Bugs and feature ideas go in
[GitHub Issues](https://github.com/YuvalBogo/nasync/issues) — please include
`nasync --version`, your distribution, and what `nasync status` says.

## Setting up

```bash
git clone https://github.com/YuvalBogo/nasync.git
cd nasync
python -m venv --system-site-packages .venv   # the flag lets the venv see GTK
.venv/bin/pip install -e ".[dev]"
```

The desktop app needs GTK4 and libadwaita 1.5+ from your distribution (see
[Installation](docs/install.md#with-the-desktop-app-linux)). The engine and
CLI don't.

Run your checkout with `.venv/bin/nasync`. If you also use NASync day to day,
install your everyday copy with `make install-user` so the app and
background service don't run whatever branch you have checked out (see
[Installation](docs/install.md#from-a-checkout)).

## Tests

| Command | What it runs | Needs |
|:--|:--|:--|
| `make test` | Unit tests | nothing |
| `make lint` | flake8 | nothing |
| `make lab` | The [data-safety](docs/data-safety.md) lab: the real engine against a throwaway Samba container, checked on the server's own disk | podman or docker |
| `make e2e` | End to end: `nasync` commands as separate processes, the background service left running, the real window driven through its main loop | podman; a desktop session for window tests |
| `make check-all` | All of the above | — |

Run `make check-all` before anything is merged to `main`. Run `make lab`
after any change to how files are compared, copied, renamed or deleted.

Other helpers:

```bash
./scripts/local-test.sh                 # push → verify → restore against a throwaway Samba server
scripts/gui-shots ~/nasync-shots        # render every window state to PNG, in a test profile
.venv/bin/python scripts/screenshots.py # regenerate docs/screenshots/
```

## How the code is organised

See [Architecture](docs/architecture.md). In short: `nasync/core/` is the
engine and knows nothing about front-ends; `cli.py` and `gui/` are two
callers of it; `system/` is how NASync fits the desktop (portals, systemd,
keyring, notifications).

## Making a change

- **Branches:** `main` holds tested releases only. Work happens on a short
  branch (`fix/…`, `feature/…`, `docs/…`) off `dev`, merged back into `dev`.
- **Keep changes focused**, one concern per branch, with one-line commit
  messages.
- **Tests:** a bug fix comes with a test that fails without it.
- **Docs:** update the page in [`docs/`](docs/) that describes the behaviour
  you changed. The README only links to them.
- **Version and changelog:** bump `__version__` in `nasync/__init__.py` (the
  only place it is set) — patch for fixes, minor for new features — and add
  an entry to [CHANGELOG.md](CHANGELOG.md).
- **Pull requests** go against `dev`, and should say how the change was
  tested.
