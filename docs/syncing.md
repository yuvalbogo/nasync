# How syncing works

**Local is the source of truth**: a sync makes the NAS follow your local
folders, and a restore brings files back the other way. This page covers what
each one will — and won't — do to your files. The full list of guarantees,
and how each is tested, is in [Data safety](data-safety.md).

## The sync root

**Each server has one folder on the share that NASync is allowed to touch.**
Every synced folder lives inside it, and nothing beside it can be reached — so
a video, a photo library or anything else next to the root is safe whatever
you sync later, and whichever sync mode you pick.

```
Storage:/                        the share
├── nasync/                      ← the sync root: NASync works in here
│   ├── Documents/               a pair's destination
│   └── Pictures/                another
├── Family videos/               ← beside the root: unreachable, always
└── old-backups/                 ← same
```

This exists because [mirror mode](#sync-mode) deletes. Without a root, what a
mirror sync could delete was "wherever the pairs happen to point" — so a folder
was protected only by the absence of a mistake, and the mistake that exposes it
(adding a pair with the wrong destination) looks entirely ordinary while you're
making it. A root turns *nothing points there* into **nothing can point
there**.

```bash
nasync root                    # show this server's root
nasync root media/nasync       # set it
nasync root --server work      # another server's
```

`nasync init` and `nasync setup` ask for it when you add a server, and the
desktop app has the same field in its server editor — and says so when a
server doesn't have one.

### Where a folder lands on the NAS

A pair's destination is **relative to the root**. With the share `backup` and
the root `nasync`:

| Pair destination | Ends up at | |
|:--|:--|:--|
| `/docs` | `backup/nasync/docs` | |
| `/photos/2024` | `backup/nasync/photos/2024` | |
| `/` | `backup/nasync` | the root itself, not the whole share |
| `/nasync/docs` | `backup/nasync/nasync/docs` | ⚠️ root repeated — usually a mistake |
| `../elsewhere` | — | refused when the pair is created |

`nasync list` and `nasync status` show where each pair actually resolves to.

> **Servers made before 0.19.0 have no root** and keep syncing exactly where
> they did, with the whole share in range. Set one with `nasync root
> <folder>` — the pairs' destinations stay as they are and become relative to
> it, so check `nasync list` afterwards.

## Sync mode

A sync always uploads new and changed files. The mode decides what happens to
files that are on the server but *not* in your local folder — typically files
you deleted locally since the last sync:

| Mode | What a sync does | Use it when |
|------|------------------|-------------|
| **Mirror** (default) | The server is made to match your local folders exactly. Server-only files are **deleted**. | The server should reflect your machine — deleting locally means deleting everywhere. |
| **Update** | New and changed files are copied. Server-only files are left alone; **nothing is deleted**. | The server is an archive that should accumulate, or you want a delete-proof backup. |

**The mode belongs to a server**, so a NAS you mirror and one you only ever
add to are two different rules rather than one compromise:

```bash
nasync mode                        # the active server's mode
nasync mode update                 # stop deleting on the active server
nasync mode mirror --server nas    # a different rule for another
```

Override it for a single run without changing the saved setting:

```bash
nasync sync --mode update    # (--no-delete is an alias for this)
nasync sync --mode mirror
```

Check what a switch to mirror would do before making it:

```bash
nasync sync --dry-run --mode mirror    # lists every file it would delete
```

In the desktop app the mode is on that server's **Settings** tab. The line
above the **Sync** button spells out the run itself (*Mirror · sends 3
folder(s) to 'attic-nas' · deletes server files you removed locally*), so a
run that deletes is never a surprise.

## What a sync does

- **Only what changed is sent.** A file has changed when its size differs or
  the local copy is newer than the server's — so an edit that keeps the file
  the same length is caught too. Uploads carry the local modification time
  onto the server.
- **Renames are renames.** A folder you rename is moved on the server rather
  than uploaded again in full and deleted, and a file renamed only in letter
  case (`img.jpg` → `IMG.jpg`) is renamed rather than deleted — SMB shares
  treat those as the same name.
- **Nothing is skipped silently.** Names SMB can't store, two names that
  differ only in letter case (an SMB share can't keep both), symlinked folders
  and unreadable files are listed with the reason. Empty folders are created
  on the server too.
- **A record of what was uploaded** (`sync-state.db`, beside your settings)
  remembers, per pair, sizes, times, a checksum taken while uploading, and
  permissions. It is how NASync catches an *older* version put back, ignores
  a file that was only touched, recognises changes made on the NAS, and tells
  a file you deleted from one someone else added.

## What NASync refuses to do

**Nothing on the NAS is destroyed without a copy surviving:**

- **A file changed on the NAS** since NASync put it there (from another
  computer, say) is never overwritten. It is renamed to
  `name (NAS copy 2026-11-05 1925).ext` and kept, then your copy is uploaded.
- **A file someone else put on the NAS** is never deleted by mirror — mirror
  deletes what *you* deleted. The first sync after upgrading from before 0.21
  can't tell yet, so it asks before deleting 50 or more files.
- **Interrupted uploads** never leave a half-written file where the good copy
  was: files are written under a temporary name and renamed into place.
- **What mirror can delete is bounded by the [sync root](#the-sync-root).**

**Runs that look like a mistake wait for you:**

- **An empty local folder is never mirrored** onto a server folder that has
  files. It almost always means the folder moved, a disk didn't mount, or a
  pair points at the wrong place.
- **Deleting or overwriting more than half of a folder's server files** (50 or
  more) waits for a yes — more than a fifth, when the background service runs
  it with nobody watching. The run still backs up new files; only the
  destructive part waits. `nasync sync` asks; the app shows a dialog whose
  default keeps the files; the background service sends a *Sync held back*
  notification. A yes covers exactly the files that were shown. From a
  script: `nasync sync --allow-mass-delete`.
- **A folder or file that can't be read** is never taken for deleted: its
  backup is left alone, and it is reported.
- **Each pair needs a server folder of its own** — on one server entry, or on
  two entries that reach the same NAS. Overlapping pairs are refused when
  added, skipped by sync if an older config has them, and flagged by
  `nasync list`, `nasync status` and the app.

## Restore

**Restore is never destructive, in either mode.** It downloads files that are
missing locally, a different size, or newer on the server. It never
overwrites a local file you changed since it was last backed up (it says
which ones it kept), and never deletes anything local. It restores
modification times, and permissions on the machine that made the backup. A
server folder that doesn't exist is an error, not "0 files restored".

```bash
nasync restore
nasync restore --pair Documents
```
