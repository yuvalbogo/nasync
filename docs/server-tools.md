# Server tools

Read-only commands for looking at the NAS. **None of them change or delete
anything.**

## Browsing the server

Rather than typing a destination path from memory, you can look:

```bash
nasync ls                 # the root of the share
nasync ls photos          # one directory down
nasync ls photos/2024
```

```
📂 backup:/photos
  📁 2024/
  📁 2023/
     holiday.jpg      2.4 MB
     notes.txt        120 B

   2 folder(s), 2 file(s)
```

In the desktop app there's a **Browse** button beside the destination field
when you add or edit a sync pair. It navigates one folder at a time and
selects the folder you're looking at, the way a normal folder picker does.

This matters more than a convenience: **in mirror mode, a destination pointing
at the wrong folder deletes what is in it.** Seeing the folder before you
choose it is the cheapest protection against that.

- **It never indexes the whole share.** Each step reads one directory, so it
  stays fast on a NAS with a hundred thousand files.
- **Files are shown but can't be picked.** They're there so you can tell
  you're in the right folder; a pair's destination is always a folder.

## What your server is

```bash
nasync server-info
```

```
🖥️  nas  (192.168.1.50:445, user alex)

  Connection
    Protocol:    SMB 3.1.1
    Encrypted:   no  (this server supports it — 'nasync encryption on')
    Signed:      yes
    Max read:    8.0 MB

  Storage
    Free:        21.9 GB of 91.2 GB (76% used)

  Shares
    ● backup           Disk
      media            Disk
      IPC$             IPC       (administrative)
```

In the desktop app it's **Server info…** in the menu.

Everything here comes from the **live connection**, not from your settings —
the settings say what was asked for, only the connection knows what the server
agreed to. A NAS that quietly negotiated an older protocol, or a session that
isn't encrypted although you turned encryption on, shows up here.

It speaks only standard SMB, so it reads the same on a Synology, a TrueNAS box
or a Raspberry Pi running Samba. **NASync deliberately doesn't change server
settings** — your NAS's own web interface does that better.

## What's using space

```bash
nasync usage              # the whole share, biggest folders first
nasync usage movies       # drill into one
```

```
📊 backup:/ — 22.8 MB in 7 file(s)
   Disk: 22.1 GB free of 91.2 GB (76% used)

   ██████████   11.4 MB  movies/
   ████████░░    8.6 MB  backups/
   █░░░░░░░░░    1.4 MB  photos/
```

Each folder is ranked by everything **beneath** it, so a big file three levels
down is attributed to the folder you'd actually delete. In the desktop app
it's **Storage** in the menu, which opens on its **Usage** view.

**Free space** is shown as what *you* may still write, not the disk's raw free
space: on a NAS with per-user quotas those differ. When they do, both are
shown.

## Duplicates

Grouping by size is free, because the scan already knows every size.
Confirming by content is not — SMB has no server-side hash, so verifying means
reading every candidate over the network:

```bash
nasync duplicates                    # by size; costs nothing extra
nasync duplicates --verify           # confirms by content, tells you the cost first
nasync duplicates --min-size 100000  # default floor is 1 MB
```

Files that share a size but differ in content are split apart by `--verify`
rather than reported together — listing them as duplicates would invite you to
delete a file that isn't a copy of anything.

In the desktop app this is the **Duplicates** view in **Storage**. Opening it
reads nothing; it waits for **Scan for duplicates**. Browsing to another
folder clears the results, which belong to the folder they were scanned in.

Removing copies is your call, made with your own tools.
