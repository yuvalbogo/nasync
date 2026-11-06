# Getting started

This assumes NASync is [installed](install.md), and that you know your NAS's
address, a username and password for it, and the share you want to back up
to.

Everything below can also be done in the desktop app (`nasync gui`); this page
uses the commands because they show each step.

## 1. Add your server

```bash
nasync setup
```

One guided command: it asks for the server's address, your username and
password, **tests the connection as you go**, lets you **pick the share from
a list read off the server**, and asks for the [sync root](syncing.md#the-sync-root)
— the one folder on that share NASync may touch (default `nasync`). Then it
offers to add your first folders.

<details>
<summary>Prefer to do it step by step?</summary>

```bash
nasync init
```

You'll be asked for the server address (e.g. `192.168.1.100`), username,
password, share name (default `backup`), SMB port (default `445`), and the
sync root (default `nasync`). Then check it works:

```bash
nasync test-connection
```

This checks the server is reachable, your credentials are accepted, and the
share exists. If the share name is wrong, it lists the shares that *are*
available.

</details>

## 2. Choose folders to back up

Each local folder is paired with a folder on the NAS:

```bash
nasync add ~/Documents /docs
nasync add ~/Pictures /pictures
```

- **The NAS path is inside the sync root.** With the share `backup` and the
  root `nasync`, `/docs` lands at `backup/nasync/docs`. Don't repeat the share
  or root name in it.
- **Each folder needs a NAS folder of its own** — `/docs` and `/pictures`, not
  `/backup` twice, and not `/backup` plus `/backup/photos`. In mirror mode two
  folders sharing a destination would delete each other's files, so NASync
  refuses it.
- **Folders belong to a server.** They're added to the active one; use
  `--server NAME` for another, and `nasync list --all` to see every server's.

Check what you set up, and where each folder resolves to on the NAS:

```bash
nasync list
nasync status
```

To stop backing a folder up: `nasync remove Documents`.

## 3. Preview, then sync

The default [sync mode](syncing.md#sync-mode) is **mirror**: the NAS is made to
match your folders, so files you delete locally are deleted there too. See
what a sync would do before the first one:

```bash
nasync sync --dry-run
```

Then run it:

```bash
nasync sync
nasync sync --pair Documents     # just one folder
```

If you'd rather the NAS only ever accumulates, switch to update mode first:
`nasync mode update`.

## 4. Restore

```bash
nasync restore
nasync restore --pair Documents
```

Restore never deletes anything local, and never overwrites a local file you
changed since it was backed up.

## 5. Make it automatic

```bash
nasync live-sync on                  # sync moments after a file changes
nasync scheduled-sync on --at 02:00  # and/or once a day
nasync service status                # is the background service running?
```

Both are off until you turn them on, and both keep working with the app
closed. See [Automatic syncing](automation.md).

## Next

- [How syncing works](syncing.md) — what a sync will and won't do to your files
- [Desktop app](desktop-app.md)
- [Command reference](cli-reference.md)
- [Troubleshooting](troubleshooting.md)
