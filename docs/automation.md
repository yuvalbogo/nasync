# Automatic syncing

Two independent triggers, **both off by default**, and **set per server**: a
home NAS can sync itself every night while a work one never does. Both run in
whichever [sync mode](syncing.md#sync-mode) that server uses.

In the desktop app they're on that server's **Settings** tab. The background
service and notifications, which are one fact about the machine rather than
about a server, are in menu (⋯) → **App settings…**.

## Live sync

Syncs a few seconds after a file changes.

```bash
nasync live-sync on
nasync live-sync off
nasync live-sync                    # show the active server's state
nasync live-sync on --server work   # or set it for another
```

A burst of writes (editors save the same file several times) is coalesced into
one sync, and only the folder that changed is synced.

**Live sync only runs while this machine is on the server's network.** Away
from home the watcher keeps running and simply doesn't fire, so nothing is
attempted over a café network or the open internet — and coming home syncs
whatever piled up. "Same network" means the NAS is reachable without going
through a gateway; failing that, a private (RFC 1918) address that answers
immediately also counts, which covers homes that split wired and wireless
across subnets. A public address never qualifies, however reachable it is.

Live sync never drops a change: an edit made while another sync is running,
or while you're away from the NAS's network, is remembered and synced as soon
as it can be. If changes wait for more than an hour, you're told why. A folder
added while the service runs is watched straight away; one that can't be
watched (the system limit on watched folders) is synced every 15 minutes
instead, and you're told.

## Scheduled sync

Syncs once a day at a set time:

```bash
nasync scheduled-sync on --at 02:00
nasync scheduled-sync --at 23:30                   # change the time
nasync scheduled-sync on --at 18:00 --server work  # a second NAS, its own slot
nasync scheduled-sync off
```

A run that couldn't happen — machine asleep, or away from home — isn't lost.
The day's sync stays due until it succeeds, so it catches up on wake or on
getting home rather than waiting another 24 hours. A scheduled server with no
backup for three days sends a notification.

**A scheduled sync counts as done only if at least one folder actually
synced.** If every folder fails — the share was renamed, permissions changed,
an external disk isn't mounted — the day's run stays due and is retried every
15 minutes, and you're told once that it didn't happen. A partial success does
count, so one folder on an unmounted disk can't keep re-syncing all the
healthy ones.

## One sync at a time

The background service, the desktop app and the `nasync` command are separate
programs, but only one of them syncs at a time: a second sync started while
one is running is refused with a message saying who is running it.

Open the app during a background sync and it shows it — *Background sync
running…*, which folder it is on, and a progress bar — with **Cancel**
stopping it. When it finishes, the app says what it did, and the line above
**Sync** keeps saying when the last sync ran and what it moved, whoever ran
it.

```bash
nasync status    # "Syncing now — started by the background service …"
nasync cancel    # stop whichever sync is running
```

## The background service

Turning either trigger on arranges for `nasync auto` to run without the app
open. How depends on how NASync was installed — you don't choose it, and the
same switches drive both:

| Install | Mechanism | What it means |
|:--|:--|:--|
| pip / pipx | a systemd **user** service | Starts at login; NASync can tell you whether it's running right now |
| Flatpak | the desktop's **Background portal** | Your desktop asks you once for permission, then starts NASync at login |

A Flatpak has no systemd user manager inside it, so the portal is the
sandboxed equivalent. The trade-off is that the portal can't be *asked*
anything afterwards: NASync can tell you it's set to start at login, but not
whether it is running this second, and the status text says exactly that. You
can revoke the permission in your desktop's Settings, under **Applications ▸
NASync**.

```bash
nasync service status      # is it installed and running — and can it start?
nasync service off         # pause automatic syncing entirely
nasync service on
nasync service restart
```

**Run in the background** (in App settings) is a switch of its own. Turning it
off keeps live sync and the schedule configured but stops anything from
running them — automatic syncing pauses rather than being reset, and the app
shows that state as **Paused**.

The service is removed when you turn the last trigger off, so nothing idles in
the background. A running service picks up settings changes without a restart.

**When the service starts, it syncs only if live sync is on.** The file
watcher only sees changes made while it's running, so edits made while the
service was down need catching up. A schedule-only setup has no such gap — a
missed slot already catches up on its own — and a setting meant to defer work
until 2am shouldn't cause a full sync at 3pm. Override it either way with
`nasync auto --sync-at-start` / `--no-sync-at-start`.

A user service stops when you log out. To keep syncing across logouts:

```bash
loginctl enable-linger $USER
```

That is a systemd concern only — under a Flatpak the desktop owns the
autostart entry.

With neither mechanism available, the settings still save and still apply
while the app or `nasync auto` is running — and the app says so.

## Notifications

A background sync that says nothing is indistinguishable from one that isn't
running, so automatic syncs announce themselves: when a sync starts, what it
moved when it finished, and — at critical urgency, so it survives Do Not
Disturb — when a scheduled run **couldn't happen at all**. That last one is the
failure worth knowing about; the others are reassurance.

```bash
nasync notifications          # show the current state
nasync notifications off
nasync notifications --test   # send one now, and name the backend used
```

On by default.

- **One notification, not a pile.** A run's "started" notification is
  replaced in place by its "finished" one, and the totals of many runs
  accumulate into a single running summary (*340 transferred · 12 syncs*). A
  gap of a few hours starts the count fresh. A scheduled run that keeps
  failing says so once.
- **A sync that changed nothing doesn't notify** — a daily "0 transferred"
  would only train you to ignore the one message that matters. `nasync status`
  says what automatic syncing is doing whenever you want to check.

Notifications go through the desktop's notification portal inside a Flatpak,
and `gdbus` (part of glib2) or `notify-send` elsewhere. On a machine with none
of them — a headless server, a locked-down container — syncing carries on
silently. If they don't show up, see
[Troubleshooting](troubleshooting.md#notifications-dont-appear).

## Without the service

`nasync auto` runs the same automation in the foreground. `nasync watch`
syncs on changes without saving any settings, until Ctrl+C:

```bash
nasync watch
nasync watch --pair Documents
```

Or call `nasync sync` from cron — see
[Security](security.md#scripts-and-cron) for passing the password:

```cron
0 2 * * * /usr/bin/nasync sync
```
