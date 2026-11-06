# Installation

NASync needs **Python 3.10+** and a Samba/SMB share you can read and write.
There is no Samba tooling to install: NASync speaks SMB 2/3 itself, through
[`smbprotocol`](https://github.com/jborean93/smbprotocol).

It is not on PyPI or Flathub yet, so it installs from GitHub.

## Command line only

```bash
pipx install git+https://github.com/YuvalBogo/nasync.git
```

The CLI runs on Linux and macOS.

## With the desktop app (Linux)

The desktop app needs PyGObject, GTK4 and **libadwaita 1.5 or newer**. Those
are system packages, not pip installs.

**Fedora** (40 and newer):

```bash
sudo dnf install python3-gobject gtk4 libadwaita
pipx install --system-site-packages \
  git+https://github.com/YuvalBogo/nasync.git
```

**Ubuntu** (24.04 LTS and newer) **/ Debian 13+**:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 pipx
pipx install --system-site-packages \
  git+https://github.com/YuvalBogo/nasync.git
```

**Why `--system-site-packages`:** GTK can't be installed by pip, so it lives in
the system Python. A normal venv (and a normal `pipx install`) hides it, and
`nasync gui` will stop and tell you so. The flag lets the isolated install see
GTK while keeping NASync's own dependencies separate. The same applies to a
manual venv: `python -m venv --system-site-packages .venv`.

> On Ubuntu, a bare `pip install` is blocked by PEP 668
> ("externally-managed-environment"). `pipx` is the supported route.

| Distro | libadwaita | Desktop app |
|---|---|---|
| Fedora 40+ | 1.5+ | ✅ |
| Fedora 39 | 1.4 | ❌ — use the CLI, or the Flatpak |
| Ubuntu 24.04 LTS+ | 1.5+ | ✅ |
| Ubuntu 22.04 LTS | 1.1 | ❌ — use the CLI, or the Flatpak |
| Debian 13+ | 1.6+ | ✅ |
| Debian 12 | 1.2 | ❌ — use the CLI, or the Flatpak |

On anything older, `nasync gui` reports the version it found and says what to
do about it, rather than failing partway through a widget.

### Add it to your app menu

```bash
nasync install-desktop
```

**NASync** then appears in your app launcher with its icon. This installs a
standard freedesktop `.desktop` entry and an icon under `~/.local/share`
(nothing system-wide, no root needed), both named after the application ID
`io.github.YuvalBogo.NASync` — which is what lets GNOME match the window to
the launcher and show the logo in the dash and alt-tab.

Install the GTK packages first: the command checks for them and refuses
without them, rather than adding a launcher that opens nothing. To remove it:

```bash
nasync install-desktop --uninstall
```

If the launcher stops working later, see
[Troubleshooting](troubleshooting.md#the-app-wont-open).

## From a checkout

To run NASync from the source tree without the app and background service
depending on whatever branch the checkout has open:

```bash
git clone https://github.com/YuvalBogo/nasync.git
cd nasync
make install-user            # or: make install-user REF=v0.21.2
```

This builds a virtualenv under `~/.local/share/nasync` from a clean export of
that ref (`main` by default), then points the app launcher, the background
service and `~/.local/bin/nasync` at it. The checkout can switch branches,
move, or lose its `.venv` without the app or tonight's sync noticing. The
previous install is kept for a rollback. After a distribution upgrade that
changes the Python version, run it again.

For working *on* NASync, see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Flatpak

The Flatpak carries its own GTK4 and libadwaita, so the desktop app runs on
distributions too old to have them (Ubuntu 22.04, Debian 12, Fedora 39). It is
not on Flathub yet; build it locally:

```bash
flatpak install -y flathub org.gnome.Platform//50 org.gnome.Sdk//50
flatpak-builder --user --install --force-clean build \
    packaging/io.github.YuvalBogo.NASync.yml
flatpak run io.github.YuvalBogo.NASync
```

See [`packaging/README.md`](../packaging/README.md) for how the dependency
pinning works.

### Moving from pip to the Flatpak

A Flatpak keeps its settings inside `~/.var/app/`, so it doesn't look where a
pip or pipx install put yours. The first time the Flatpak runs it imports
`~/.config/nasync` if it finds one — servers, folders and pairs — and tells
you it did.

It **copies**, so your pip install keeps working and nothing is lost if you go
back. It only runs when the Flatpak has no settings of its own, so it can't
overwrite anything you set up there.

**Passwords don't come across.** The pip install's key is in your keyring and
the Flatpak's is held by the desktop portal, so a file sealed by one cannot be
opened by the other (see [Security](security.md#how-your-nas-passwords-are-stored)).
NASync tells you this happened; enter each password once in the Flatpak.
