# Packaging NASync as a Flatpak

The runtime side of sandboxing has been in the code since 0.9.0 — the portal
paths for the background service, notifications and the credential vault, and
the pip→Flatpak config import that runs on first launch. This directory is the
packaging that turns that into an installable app.

## Files

| File | What it is |
|---|---|
| `io.github.YuvalBogo.NASync.yml` | The flatpak-builder manifest |
| `python3-modules.json` | Every runtime dependency, pinned with a checksum. **Generated** — see below |
| `io.github.YuvalBogo.NASync.metainfo.xml` | AppStream data: what software centres show |
| `io.github.YuvalBogo.NASync.desktop` | The launcher inside the sandbox |
| `nasync-gui` | One-line shim so the launcher opens the window rather than printing help |

## Building it

```bash
sudo dnf install flatpak-builder            # or apt install flatpak-builder
flatpak install -y flathub org.gnome.Platform//50 org.gnome.Sdk//50

flatpak-builder --user --install --force-clean build \
    packaging/io.github.YuvalBogo.NASync.yml
flatpak run io.github.YuvalBogo.NASync
```

`runtime-version` in the manifest and the `flatpak install` line above have to
agree. It currently says 50 because that is what is installed here; any recent
GNOME runtime works, and the host's own GNOME version is unrelated.

## Refreshing the pinned dependencies

`python3-modules.json` is generated rather than hand-written, because a Flatpak
build has to be offline and reproducible: every source needs a URL and a
checksum. Regenerate it whenever `pyproject.toml`'s dependencies change.

The three cases it handles, which is the part worth knowing before editing it
by hand:

- **Pure Python** packages take the portable `py3-none-any` wheel.
- **`cryptography`** has no portable wheel and its sdist needs a Rust toolchain
  the GNOME SDK does not carry, so its `abi3` manylinux wheel is used, one per
  architecture. `abi3` is stable across CPython 3.x, so it survives the runtime
  moving to a newer Python.
- **`cffi`** publishes only exact `cp3XX` wheels, so any pin would break the day
  the runtime's Python changed. It is built from its sdist instead, which needs
  only a C compiler and libffi — both in the SDK.

## Before submitting to Flathub

Not done, and **not to be done without Yuval's explicit approval** (see
`CLAUDE.md`). What a reviewer will raise:

- **`--filesystem=home`.** A folder-sync tool cannot know in advance which
  folders it will be asked to sync, so there is no narrower grant that leaves
  the app able to do its job. Worth stating plainly in the submission rather
  than waiting to be asked.
- **Screenshot URLs** in the metainfo point at `main` on GitHub. They must
  resolve at review time.
- The `<releases>` block needs an entry per release; it is not generated.
