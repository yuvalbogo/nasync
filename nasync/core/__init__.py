"""The engine: what NASync does, independent of any desktop or front-end.

Nothing here imports GTK, talks to a portal, or knows which interface is
driving it. That is the point — the CLI and the desktop app are two callers of
the same four modules, and a feature that only works in one of them is a bug.

    config.py      config.json and the encrypted .creds under ~/.config/nasync
    samba.py       the SMB 2/3 transport: indexing, transfers, browsing, usage
    sync.py        Syncer — compares two trees and moves what differs
    automation.py  the live and scheduled triggers, and the loop that runs them

The dependency runs one way: ``automation`` uses ``sync`` uses ``samba``, and
all three read ``config``. Nothing in here imports from :mod:`nasync.gui`, and
the only thing it borrows from :mod:`nasync.system` is credential storage and
the LAN check.
"""
