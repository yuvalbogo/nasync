"""How NASync fits the machine it runs on — and the whole sandbox surface.

Everything that talks to the desktop, the session bus, systemd or the keyring
lives here. **This is the code a Flathub reviewer asks about**, because it is
the code that decides which permissions the Flatpak manifest needs, so it is
worth reading as a set rather than one file at a time.

    portal.py    XDG Desktop Portal client — Background, Notification, Secret
    service.py   background syncing: a systemd user unit, or the portal
    notify.py    desktop notifications
    vault.py     encrypts the saved passwords, with a key from portal/keyring
    lan.py       "is the NAS on this network?" — the gate on automatic syncing
    launcher.py  the .desktop entry and icon (`nasync install-desktop`)

Two rules hold across all of them, and both exist to keep the manifest small:

* **Inside a sandbox, prefer a portal to talking to a service directly.**
  Notifications and secrets both go through portals, which need *no* manifest
  permission, where the underlying services would each need a ``--talk-name``
  that a reviewer would reasonably ask us to justify.
* **Everything degrades rather than fails.** No systemd, no portal, no keyring,
  no notification daemon — each of those is a machine NASync still runs on, and
  every module here reports what it can't do instead of pretending otherwise.
"""
