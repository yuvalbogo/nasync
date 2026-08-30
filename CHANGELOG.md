# Changelog

All notable changes to this project will be documented in this file.

## [0.18.0] - 2026-08-30

**The sync mode and the automatic-sync triggers now belong to a server rather
than to the app.** A home NAS you mirror on a schedule and a work NAS you only
ever push to by hand were never describable by one setting, and until now the
background service only ever automated whichever server happened to be
*active* — so a second NAS was never automated at all.

### Added
- **Per-server sync mode and triggers.** Each server carries its own
  `sync_mode`, live sync, scheduled sync and time, stored on the server entry
  beside `encrypt`, which was already per-server. They survive editing and
  renaming a server.
- **`--server NAME`** on `nasync mode`, `nasync live-sync`,
  `nasync scheduled-sync` and `nasync auto`, defaulting to the active server
  the same way `nasync add` and `nasync encryption` already did.
- **`nasync auto` drives every automated server**, each with its own mode,
  schedule, catch-up date and network check, from one loop. Syncs stay
  serialised — watchers run per server, but only one sync happens at a time,
  which is the "syncs never overlap" guarantee the automation layer has always
  made. A server automated while the service is running is picked up without a
  restart.
- **`NASYNC_DEBUG=1`** prints a traceback when a background sync fails. The
  message alone had proved not to be enough: an unattended failure is read days
  later in the journal with no way to tell where it came from.

### Changed
- **The desktop app gives each server two tabs**, *Server* (its details and
  sync pairs) and *Settings* (its mode and triggers). Servers moved into the
  header, since they are the outer scope; the tab you are on is remembered when
  you switch between them.
- **The menu's *Settings* is now *App settings…***, and holds only what is
  genuinely app-wide: running in the background, notifications, and how saved
  passwords are protected. Everything per-server left it.
- **`nasync status` reports the mode per server** and lists each automated
  server's triggers, instead of one app-wide block that could only ever have
  been describing one of them.

### Migration
- On first run, the old app-wide mode and triggers move to **the active server
  only**. Every other server starts with automation off. Nothing that was not
  syncing by itself before the upgrade starts doing so because of it —
  inheriting a trigger would have begun unattended syncing on a NAS nobody
  asked to automate, in mirror mode, which deletes.
- `background` and notifications stay app-wide and are untouched.
- **Downgrade note:** an older NASync reading a migrated config finds no
  app-wide automation block and treats everything as off, rather than
  misbehaving.

### Fixed
- **Editing a server wiped its sync settings.** `add_or_update_server` rebuilds
  the entry from its arguments, so changing a port would have silently switched
  that server's automation off. Renaming did the same, via the remove-then-add
  path — the bug the code already guarded against for sync pairs.
- **The mode was read app-wide while the connection came from one server.**
  `Syncer` now takes the server it is syncing, so a push cannot take one
  server's rule about deleting and another's files.
- Writing a per-server setting with no server configured now says so instead of
  silently discarding the write and reporting success.
- The daemon no longer treats its own recording of a scheduled run as a user
  edit, which had it reload the config and reset every interval timer.

## [0.17.0] - 2026-08-30

The desktop app rearranged around what it is actually for. NASync is
configured once and then left alone, which means the settings are not
incidental to the main screen — they are what you open the app to confirm. So
they are now stated on the page, with their current values, instead of living
behind the menu button next to the window controls.

### Changed
- **The sync mode, automatic syncing and notifications are on the main page**,
  in a *How syncing works* group, each row showing its value: the mode reads
  `Mirror` or `Update` with a subtitle saying what that mode does to server
  files, and *Automatic syncing* reads `Live · daily 02:00`, `Paused` or `Off`
  and expands in place to the full set of triggers. The setting that decides
  whether a sync **deletes on the server** was previously two clicks into a
  menu; nothing about an app you check on rather than operate justifies that.
  The group says *"These apply to every server"*, because they do — they are
  app-wide, and a group on a per-server tab would otherwise read as per-server.
- **Sync pairs are visible without being expanded.** They were behind a
  *Show all pairs* disclosure triangle, which put the one thing the screen
  exists for one click further away than the settings above it. The add button
  moved to the group header, where GNOME puts the action that adds to a list.
- **The line above the Sync button says what Sync will do** — `Mirror · sends
  3 folder(s) to 'attic-nas' · deletes server files you removed locally` —
  rather than the bar carrying a button labelled with the mode's name. The
  name never said what the mode meant; this does, next to the button it
  applies to.
- **Storage is two named views, Usage and Duplicates**, switched by a view
  switcher in its header. One list used to become the other when *Find
  duplicates* was pressed, with no labelled way back — leaving *Up one folder*
  standing in for "leave duplicate results", which is not what it says.
  **The duplicate scan is now explicit**: opening the tab reads nothing and
  offers a *Scan for duplicates* button, matching `nasync duplicates` being a
  command you run. Results are retired when you browse to another folder,
  rather than sitting under a path they were not scanned in.
- **The *Quick settings* card group is gone.** Of its three rows, one only
  opened Settings and showed no state, one duplicated Settings, and one was
  broken (below).
- The sync mode is one implementation now, shared by the page and the Settings
  dialog, rather than three separate hand-built widget trees. The two popovers
  that carried the mode and the automation switches are removed with it; their
  hand-drawn rows were reimplementations of `AdwActionRow` and `AdwSwitchRow`.

### Fixed
- **"Sync only on home network" silently turned on live sync.** Its handler
  passed the whole automation dict as `set_automation`'s first positional
  argument — which is `live_sync` — so a non-empty dict became `True` and the
  unattended trigger switched on while the user believed they had adjusted a
  network preference. The switch also always displayed *On*, because it read a
  config key nothing ever wrote, and it claimed to control something that was
  never optional: live sync is gated on being on the server's network
  unconditionally, in `automation.py`. Removed rather than repaired — there
  was no setting there to repair.
- **The status line under the progress bar was invisible.** It was created
  hidden and never shown, so everything written to it — the idle summary, the
  final tally (`12 transferred · 3 unchanged`), the cancellation notice and
  the error text — was discarded. It is now visible and carries all of it.
- The tab badge no longer sets a "needs attention" dot from the same pair
  count that the badge already displays.
- The destination field's **Browse the server** button is now present and
  disabled when the server has no address yet, with a tooltip saying so,
  instead of vanishing and leaving two identical-looking fields where only one
  offers to browse.

## [0.16.2] - 2026-08-08

Documentation and packaging corrections found by reading the whole of README,
QUICKSTART and the build files against what the code actually does.

### Fixed
- **The declared Python floor was wrong, and would have failed to install.**
  `requires-python` said 3.8, but `secretstorage>=3.5` and current
  `smbprotocol` both require **3.10**, so `pip install` on 3.8 or 3.9 could
  never have resolved. The floor, the classifiers, the black target and the
  README/QUICKSTART claims now all say 3.10.
- **CI tested Python 3.8 and 3.9**, which for the same reason could only ever
  have failed at the install step. The matrix is now 3.10–3.12.
- **CI still installed the `smbclient` binary** on both runners, unused since
  0.16.0 removed the last subprocess. Dropped.
- **`pipx install nasync` was documented but NASync isn't on PyPI yet**, so the
  desktop-app setup instructions in both README and QUICKSTART installed
  nothing. They now use the git URL, matching the step above them.

### Changed
- **The feature list was five releases out of date** and undersold the security
  work — "credentials stored locally with restricted permissions" had been
  describing encrypted-at-rest storage since 0.11.0. It now covers browsing the
  server, storage insight, server info, credential encryption and SMB 3
  transfers.
- The cross-platform claim is now precise: Linux first, with the CLI also
  running on macOS and the desktop app Linux-only, rather than "works on Linux
  and macOS" implying the GUI does too.
- **Screenshots regenerated.** The old set predated the TUI removal, the
  Storage panel, the Security section and the encryption switch. The generator
  now uses an invented identity throughout and runs with a throwaway `HOME`, so
  nothing about the machine that produced them can appear; the README shows
  three of them.

## [0.16.1] - 2026-08-08

Fixes from a quality review and a code review run over 0.11.0–0.16.0. The first
two are the ones that mattered.

### Fixed
- **Encryption could silently not happen.** `share_details()` — which the
  desktop app's *Test* button and `nasync setup` both call — opened the `IPC$`
  pipe without passing the encryption flag, creating an *unencrypted* pooled
  session. Every later call in that process reuses the pooled session, and
  `smbclient`'s upgrade path sets an attribute `smbprotocol.session.Session`
  does not have, so the upgrade is a silent no-op. The result: turn encryption
  on, press Test, and the sync that follows runs in the clear while every
  surface reports "encrypted".

  The flag now travels with every path-based call, and `connect()` **verifies
  against the live session** that encryption actually happened when it was
  required, refusing rather than continuing. Both halves confirmed against a
  real server.
- **A server that refuses to list its shares was reported as having none.**
  A NAS that restricts SRVSVC to administrators answers with a well-formed,
  empty reply and `ERROR_ACCESS_DENIED`; the decoder read only the container
  and returned an empty list, so three separate surfaces claimed a server full
  of shares had none. The return code is now checked.
- **`scripts/local-test.sh` and `scripts/screenshots.py` were broken by the
  0.15.0 reorganisation** — both still imported `nasync.config`, so the
  end-to-end container test could not run at all.
- **The icon fallback path pointed at a directory that doesn't exist**, also
  from the 0.15.0 move: `launcher.py` resolved assets relative to
  `nasync/system/` instead of the package root.
- **Sizes stopped at GB**, so an 8 TB volume printed as "8192.0 GB" — in the
  panel whose entire job is reporting it. Now up to PB.
- **The GUI's storage summary omitted the quota caveat** that the other three
  surfaces print, making it the one place that promised space a quota won't let
  you write. All four now share `format_volume`.
- **A finishing sync tore down the shared connection.** `Syncer` called
  `disconnect()`, which `smbclient` documents as closing the pooled connection
  and every session on it — so a sync completing while the Storage or Browse
  dialog was scanning aborted that scan with a bogus "server unreachable".
- **Listing a server's pairs created an entry for it.** `_owned_pairs` used
  `setdefault` on a read path, so reading pairs for a name — including one that
  doesn't exist, or was just removed — inserted it, and the next unrelated
  save wrote it to disk.
- **`Syncer` built its own `Config`**, so the background service's careful
  `reload()` bought its syncs nothing: they acted on the settings as they were
  when the process started. It now accepts the caller's instance.
- **Constructing a `Config` reached the keyring.** The credential migration
  probed for a backend before checking whether the file needed migrating at
  all, so every CLI command and the GUI's main-thread window construction paid
  a D-Bus round trip that only an unencrypted file could act on.

## [0.16.0] - 2026-08-08

NASync now talks to the server about the server, and the last thing that
couldn't work inside a Flatpak no longer needs a subprocess.

### Added
- **`nasync server-info`, and a Server info panel in the desktop app.** What
  protocol was negotiated, whether this connection is encrypted and signed,
  how much space you may actually write, and every share the server offers.

  All of it comes from the *live connection* rather than from your settings,
  which is the point: the settings say what was asked for, and only the
  connection knows what the server agreed to. A NAS that quietly negotiated an
  older dialect, or a session that isn't encrypted although the switch is on,
  is exactly what this makes visible.
- **`nasync/core/srvsvc.py` — share enumeration spoken directly.** One DCE/RPC
  bind and one `NetrShareEnum` call on the `\srvsvc` named pipe.
- `SAMBAClient.share_details()` returns each share's name, type and comment;
  `list_shares(include_all=True)` includes administrative shares for the info
  panel, which reports rather than chooses.

### Changed
- **Listing shares no longer runs `smbclient -L`.** That binary is not present
  inside a Flatpak, and bundling the whole of Samba to read a list of names
  would be a strange thing to ask a Flathub reviewer to accept — so this was
  the last piece of NASync that structurally could not work in the sandbox.
  It is now spoken over the protocol: no subprocess, no new dependency, and the
  same answer.

  Verified against a real Samba server to return exactly what `smbclient -L`
  returns, including share types and comments.

### Fixed
- **Protocol failures wrapped in `SMBOSError` now get a readable message.**
  `smbclient`'s filesystem layer reports failures that way rather than raising
  the typed exceptions NASync matched on, so an authentication failure while
  listing shares surfaced as a raw `[NtStatus 0xc000006d]`. Found while moving
  share listing onto the `IPC$` pipe, but it affected any such error.

### Notes
- The info panel is **read-only, and deliberately protocol-based**: it reads
  the same on a Synology, a TrueNAS box and a Raspberry Pi running Samba.
  Changing a server's settings — its IP, its shares, its users — is its own web
  UI's job, and doing it here would mean a different code path per
  manufacturer. See the roadmap's rejected list.

## [0.15.0] - 2026-08-08

The terminal UI is gone, and the package is laid out to be read.

### Removed
- **The Textual terminal UI (`nasync tui`) has been removed**, along with the
  `nasync[tui]` extra and the `--tui` flag on `install-desktop`. It answered a
  narrow niche, and maintaining a third front-end over the same engine was
  slowing down the two that matter.

  **If you used it:** the CLI does everything it did, and the desktop app
  (`nasync gui`) is the graphical answer. On a distribution too old for
  libadwaita 1.5 — Ubuntu 22.04, Debian 12 — the CLI is the interactive option
  until the Flatpak ships, since that carries its own runtime and makes the
  desktop app work regardless of what the system provides.
- `nasync install-desktop` no longer installs a launcher when GTK4 and
  libadwaita aren't available. It used to fall back to opening the terminal UI;
  with nothing to fall back to, an entry in the apps list that opens nothing
  would be worse than no entry, so it refuses and says what to do instead.

### Changed
- **The package is reorganised so it can be read in order.** Everything moved;
  nothing changed behaviour.

  | Was | Is now |
  |:--|:--|
  | `nasync/config.py`, `samba.py`, `sync.py`, `automation.py` | `nasync/core/` |
  | `nasync/portal.py`, `service.py`, `notify.py`, `lan.py`, `vault.py` | `nasync/system/` |
  | `nasync/desktop.py` | `nasync/system/launcher.py` |
  | `nasync/test_*.py`, `conftest.py` | `tests/` |

  `core/` is the engine and depends on nothing above it. `system/` is every
  place NASync touches the desktop, the session bus, systemd or the keyring —
  which is exactly the set of code that decides what a Flatpak manifest has to
  ask permission for, so it is now one directory rather than six files mixed in
  with the engine. Both carry a package docstring explaining what belongs
  there.

  `desktop.py` became `launcher.py` because "desktop" already meant two things
  next to a `gui/` package: the desktop *app* and the desktop *entry*. This is
  only the latter.
- `ADW_MIN` moved to `nasync/__init__.py` beside `APP_ID`. The launcher needs
  the libadwaita floor to decide whether a launcher is installable, and reading
  it from `nasync.gui` meant the system layer imported the GUI package to find
  out whether the GUI could run.

### Migration
- **Nothing to do.** The `nasync` command, its subcommands, the config format
  and the config location are all unchanged. This is an internal reorganisation
  plus the removal of a front-end.
- Anyone importing NASync as a library (not a supported interface, but
  possible) needs the new module paths in the table above.

## [0.14.1] - 2026-08-08

Two long-standing bugs in automatic syncing, both found by the same review as
0.13.1. Neither is new — both predate this week's work — and both affect anyone
running background syncing.

### Fixed
- **Changing a setting could immediately perform a full sync you didn't ask
  for.** The background service synced unconditionally on startup, and it is
  restarted far more often than that suggests: `service.apply()` restarts the
  unit after any automation change, and the systemd unit restarts it on failure
  every 30 seconds. So adjusting the *scheduled time* — a setting whose entire
  purpose is to defer work until 2am — pushed everything to the server
  immediately, in mirror mode, deleting server-side files, with nobody
  watching.

  A startup sync now happens only when **live sync** is on. That is the one
  trigger with a real gap to fill: watchdog only sees changes made while it is
  running, so edits made while the service was down would otherwise wait for
  the next edit. A schedule-only configuration has no such gap — it has a slot,
  and a missed one already catches up. `nasync auto --sync-at-start` forces the
  old behaviour, `--no-sync-at-start` refuses it outright.
- **A scheduled sync in which every folder failed was recorded as done, and
  never retried.** `Syncer` reports per-pair failures in its result rather than
  raising, so a run that transferred nothing at all — share renamed,
  permissions changed, an external disk not mounted — still counted as the
  day's backup. Nothing synced, nothing retried, and nothing said so until the
  backup was needed. This contradicted the promise in `automation.py`'s own
  docstring that "the day's run stays due until it succeeds".

  A run now counts only if **at least one folder synced**. A partial success
  still counts, deliberately: requiring every folder to succeed would leave the
  day's run due forever whenever one pair pointed at an unmounted disk,
  re-syncing every healthy folder on each retry.
- **A failed scheduled run backs off before retrying.** Keeping the day's run
  due is the correct half of the fix above, but the loop ticks once a second —
  so on its own it would have meant a full sync attempt per second against a
  NAS that rejects everything, until midnight. Retries are 15 minutes apart,
  and the failure is announced once a day rather than once per attempt.

### Added
- `nasync auto --sync-at-start / --no-sync-at-start`, for the cases where the
  default rule above isn't what you want.

## [0.14.0] - 2026-08-08

NASync can now tell you what is actually on your NAS, not just put things on it.

### Added
- **"What's using space", with drill-down.** `nasync usage [PATH]`, a Storage
  entry in the desktop app's menu, and `u` in the TUI. Every folder is ranked by
  everything *beneath* it, so a 40 GB video buried three levels down is
  attributed to the folder you would actually delete — which is the question
  people are asking when they ask this one.

  It costs a scan and no more: the report is built from the same index the sync
  engine already computes, which does one round trip per directory and was
  tuned for exactly this in 0.8.0. NASync was throwing that away after every
  comparison.
- **Free space**, where the server will report it. The figure shown is what
  *this user* may still write, not the disk's raw free space: on a NAS with
  per-user quotas the disk's number promises room a sync cannot use. When they
  differ, both are shown.
- **A duplicate finder.** `nasync duplicates [PATH]`, and a *Find duplicates*
  button in the desktop app. Grouping by size is free, because every size is
  already known from the scan — so the default report costs nothing extra.

  `--verify` confirms by content. It is opt-in and states its price first,
  because SMB has no server-side hash: confirming means reading every candidate
  over the network, which on a share of large media files is gigabytes. Files
  that share a size but differ in content are split apart rather than reported
  together — offering them as duplicates would invite someone to delete a file
  that is not a copy of anything.
- `SAMBAClient.volume_info()`, `usage_tree()` and `find_duplicates()`, plus an
  optional per-directory progress hook on the existing index walk. A whole-share
  scan is minutes of silence otherwise, which reads as a hang.

### Changed
- Small files are ignored by the duplicate finder by default (`--min-size`,
  1 MB). Every empty file matches every other one, and a share has thousands of
  identical thumbnails and stub files that bury the answer in noise.
- Nothing here deletes anything, in any front-end, and each says so. A sync
  tool that also removes files on its own initiative is one bad heuristic away
  from being the reason someone lost a photo library.

## [0.13.1] - 2026-08-08

Fixes for defects found by a review of 0.11.0–0.13.0. **Anyone running 0.11.0
or 0.12.0 should update**: the first item below could permanently destroy saved
passwords.

### Fixed
- **Saving one server could delete every other server's saved password.**
  Introduced in 0.11.0. Every write does read-modify-write on the credential
  file, and a read that *failed* returned an empty map — so writing it back
  deleted the passwords it hadn't been able to decrypt. `remove_server` was
  worse: it wrote back an empty map and emptied the file entirely.

  Three real routes reached it: a locked keyring, a key deleted from the
  keyring, and a `.creds` carried into a Flatpak from a pip install (sealed
  with a key the sandbox cannot have). NASync now refuses to write when it
  couldn't read, and says so, leaving the file untouched.
- **A missing keyring could silently rewrite an encrypted file as plaintext.**
  `vault.seal` passes the map through unencrypted when nothing can hold a key,
  which is correct for a machine that never had a keyring and catastrophic for
  one whose keyring merely isn't there this session. Writing plaintext over an
  encrypted file is now refused outright.
- **The "saved passwords couldn't be read" warning never actually appeared.**
  It was checked in `nasync status` and in the GUI's Security section, but the
  flag it read is only ever set by an attempt to read the file — and nothing on
  the way to either screen reads a password. The check is now explicit
  (`Config.check_credentials`), so the one diagnostic that separates a locked
  keyring from a wrong password reaches the person who needs it.
- **Inside a Flatpak, fetching the encryption key could hang the app forever.**
  The portal keeps its own end of the pipe it writes the secret to, so if it
  never answers, the read never returns — outliving the request timeout
  entirely. Confirmed against a real desktop, where `retrieve_secret` blocked
  indefinitely. The read is now bounded and the portal's response is checked
  before we wait on it at all. The same path also leaked a file descriptor on
  every failure.
- **Unlocking a keyring failed outright on Debian 12 and Ubuntu 22.04/24.04.**
  `Collection.unlock(timeout=…)` only exists in `secretstorage` 3.5; those
  distributions ship 3.3, where it raises `TypeError`. That surfaced as an
  unreadable credential file — which, before this release, then triggered the
  data loss above.
- **A `.creds` that cannot be decrypted is no longer a dead end.** Refusing to
  overwrite it would otherwise trap you: the recovery is to enter the passwords
  again, and entering one is a write. `nasync credentials --reset` and a
  *Forget saved passwords* button in the GUI discard them deliberately, leaving
  servers and sync pairs alone.
- **Browsing the server could abort a running sync.** `smbclient` pools one
  connection per server and `delete_session` closes it along with every session
  on it — so the browser, the storage report, and `nasync ls` were tearing down
  a connection a transfer might be using, which surfaced as a bogus "server
  unreachable". Read-only operations now use `SAMBAClient.release()`, which
  leaves the pooled connection alone.
- **Re-running `nasync setup` turned encryption off.** It saved servers without
  the flag, so the default of "off" was written over an existing server's
  setting — silently downgrading a configured NAS to unencrypted transfers.
- `vault.unseal` raised `TypeError` rather than `VaultError` where
  `cryptography` isn't importable, escaping every caller's error handling and
  crashing the app — the exact outcome that treating it as optional exists to
  avoid.

### Added
- `nasync credentials [--reset]` — shows how saved passwords are protected and
  provides the deliberate way to discard ones that can't be opened.

## [0.13.0] - 2026-08-08

You can now look at what's on the server instead of typing a path and hoping.

### Added
- **Browse the server when choosing a destination.** The local side of a sync
  pair has always opened a real folder picker; the server side was a text field
  you had to fill in from memory. That asymmetry mattered more than it looked:
  **in mirror mode a destination pointing at the wrong folder deletes what is
  in it**, and a typo is not obviously a typo until after the fact.

  There is now a Browse button beside the destination in the desktop app, a
  *Browse the server…* button in the TUI, and `nasync ls [PATH]` for the
  command line. All three walk the share one directory at a time.
- `SAMBAClient.list_directory(path)` — one directory's entries, folders first
  and each group sorted case-insensitively, which is the order a file manager
  shows rather than the order the server happens to return.

### Changed
- The browser reads **one directory per step**, never the whole share. The
  recursive index that sync uses would be the wrong tool here: it exists to
  compare a tree, and running it to draw the first screen would stall exactly
  the large shares the 0.8.0 indexing work was about.
- A symlink's size is reported as unknown rather than as a number. A directory
  listing describes the *link*, not its target, so the size it carries is not
  the file's — the same trap that was fixed for indexing in 0.8.0, surfacing
  again now that entries are shown to a person.
- Files are listed in the browser but can't be picked. Hiding them would remove
  the only cue that you are looking at the folder you meant; making them
  selectable would let you aim a sync pair at a file.
- `format_bytes` moved to a new `nasync/format.py` shared by all three
  front-ends, rather than being copied a third time. Formatting duplicated per
  front-end is already a known source of drift in this codebase.

### Fixed
- The desktop browser could strand itself on a spinner. Clicking a folder while
  a directory was still loading moved the current path, so the in-flight reply
  was correctly discarded as stale — but an "already loading, skip this one"
  guard meant the new folder was never requested either, and nothing ever
  arrived to replace it. Overlapping fetches are now allowed and correctness
  comes from the staleness check alone.
- Browsing somewhere that doesn't exist says so. Indexing treats a missing path
  as the ordinary first-sync case and returns an empty tree, which would have
  made a mistyped path look like an empty folder.

## [0.12.0] - 2026-08-08

Transfers can now be encrypted on the network. 0.11.0 protected your passwords
where they are stored; this protects your files while they move.

### Added
- **SMB 3 encryption, per server.** A new switch encrypts the *contents* of
  every transfer with AES-GCM, using encryption that has been part of SMB since
  version 3.0 — so this needs nothing on the NAS beyond an SMB 3 server, which
  most boxes made since about 2014 have.

  It is **off by default and deliberately so.** Turning it on for existing
  servers would break every connection to a NAS too old to encrypt, turning a
  security improvement into an outage; and it costs real throughput — on a NAS
  without hardware AES acceleration it can halve transfer speed. That is a
  trade worth making on a shared or untrusted network and not worth making on a
  wired LAN you control, which is a judgement only you can make.
- `nasync encryption [on|off] [--server NAME]` shows or sets it, and `nasync
  init` asks. `nasync servers` marks encrypted servers with a padlock and
  `nasync status` spells it out per server.
- The same switch in the desktop app's server editor and in the TUI, with the
  trade-off written next to it rather than left to be discovered.

### Changed
- Testing a connection from the server editor now uses the encryption switch
  **as it currently stands**, not as it was last saved. The point of Test is to
  find out whether these settings work before committing to them, and
  encryption is the setting most likely not to.
- Signing was already required on every connection and stays that way. Signing
  proves nobody tampered with the traffic; encryption is what stops them
  reading it. They are different guarantees and NASync now offers both.

### Fixed
- `Config.get_server()` returned whatever was on disk, so a server saved before
  this release came back with no `encrypt` key at all — every caller would have
  had to remember the default, and one that forgot would read a missing flag as
  an error or as something truthy. It is normalised in the accessor now.

### Migration
- **Existing servers are unchanged and stay unencrypted.** Nothing about a
  working setup changes until you turn the switch on.
- **When encryption is on, it is a requirement, not a preference.** If the
  server can't encrypt, NASync refuses the connection instead of quietly
  falling back to sending your files in the clear — a security setting that
  silently degrades is worse than none, because it reads as protection that
  isn't there. The refusal names the cause and says how to proceed, rather than
  passing on `smbprotocol`'s "the connection does not support it", which is
  accurate and tells you nothing about which end to change.
- Internally the transport is asked for `encrypt=None` rather than
  `encrypt=False` when the switch is off. Passing `False` actively *disables*
  encryption, which would break connections to a share whose own configuration
  requires it — a server working today would stop working purely because
  NASync started being explicit.

## [0.11.0] - 2026-08-08

Saved NAS passwords are no longer stored in a file anything running as you can
read.

### Added
- **Saved passwords are encrypted at rest.** Until now `~/.config/nasync/.creds`
  held every saved server's password as plaintext JSON, protected by nothing
  but mode 0600. File permissions stop another *user* reading that file; they
  stop nothing that already runs as you — a backup sweeping up dotfiles, a
  synced home directory, anything you `pip install`. NASync now encrypts the
  whole file with AES-GCM, under a key it never stores itself.

  Where the key comes from depends on where NASync is installed, not on a
  setting. Inside a Flatpak it is the master secret
  `org.freedesktop.portal.Secret` holds for us, which needs **no manifest
  permission at all** — the same trade, for the same reason, as the
  notification backend in 0.10.0. Everywhere else it is a single item in the
  Secret Service, so it lives in your login keyring alongside everything else
  of this kind and is visible in Seahorse as "NASync credential key".
- **`nasync status` and the GUI's Settings say which of those is true**, in
  plain words, including the case where neither is available.

### Changed
- Saving a server writes its password *before* touching anything else. Sealing
  can fail — a keyring that disappeared mid-session — and doing it first means
  such a failure leaves the config exactly as it was rather than recording a
  server whose password never got stored.
- A `Config` now caches the decrypted credential map for its lifetime, because
  every read of a sealed file costs a round trip to the keyring or the portal
  and `get_server` is called on the UI thread. `reload()` drops it, so a
  password changed in the desktop app still reaches the background service by
  the same route as every other setting.

### Fixed
- A machine that cannot decrypt its own credentials now *says so*. Previously
  any failure to read the file surfaced as an empty password and, a moment
  later, as the NAS rejecting the login — which sends people to re-check a
  password that was correct all along. Three distinct causes are now reported
  as themselves: a locked keyring, a key that has been removed or changed, and
  a file sealed by a different install.

### Migration
- **An existing plaintext `.creds` is encrypted in place on the next start.**
  Nothing is asked of you and no password needs re-entering.
- **On a machine with no keyring and no portal** — a headless box, an SSH
  session with no session bus — behaviour is unchanged: the file stays
  plaintext at 0600, and both front-ends report that rather than implying a
  protection that isn't there. Encryption is never load-bearing for the app
  starting or syncing.
- **Moving a pip install to the Flatpak no longer carries passwords across.**
  The host's key lives in your keyring and the sandbox's is held by the portal;
  they are different secrets, so a file sealed by one genuinely cannot be
  opened by the other. The config import from 0.10.0 still copies servers,
  folders and pairs; the passwords have to be entered once in the Flatpak. This
  is reported as exactly that — "encrypted by a keyring-based install" — rather
  than as a damaged file, because the two need completely different responses.

## [0.10.0] - 2026-08-07

The rest of the sandbox work: settings that follow you into the Flatpak, and
notifications that arrive once you're in it. Nothing changes outside a sandbox.

### Added
- **Moving from pip to the Flatpak brings your settings with you.** Under
  Flatpak, `XDG_CONFIG_HOME` points inside `~/.var/app`, so an existing
  install's servers, folders and passwords are simply not where NASync looks —
  the app would open empty and every server would have to be entered again, and
  every folder re-picked. Re-picking a folder in mirror mode is the operation
  with the worst failure mode NASync has, so this is not a papercut. The first
  sandboxed run now adopts `~/.config/nasync` if it finds one.

  It **copies, never moves**: the pip install keeps working, so this is
  reversible by uninstalling the Flatpak. It only runs when there is no
  sandboxed config yet, so it can never overwrite something you set up in the
  Flatpak. And it can't stop the app starting — an unreadable host config just
  means starting empty, which is what would have happened anyway. `nasync
  status` says once that it happened, because copying saved passwords to a new
  location shouldn't be silent even when it's the helpful thing to do.
- **Notifications inside a Flatpak go through the desktop portal.** Verified
  against a real desktop, including the replace-in-place behaviour the sync
  notifier depends on.

### Changed
- The notification backend is chosen per environment: the portal inside a
  sandbox, `gdbus` outside it, `notify-send` as the last resort. The portal is
  deliberately *not* used outside a sandbox, where it would only add a hop
  through a service that exists to mediate for sandboxed apps.

  Choosing the portal over talking to `org.freedesktop.Notifications` directly
  is what keeps `--talk-name=org.freedesktop.Notifications` out of the Flatpak
  manifest — a permission Flathub reviewers reasonably ask you to justify when
  a portal already does the job. The portal also replaces a notification by an
  id *we* choose, which survives a restart of the process, rather than by a
  number the daemon hands back and we have to hold on to.

### Migration
- No config change. The import described above reads a pre-existing
  `~/.config/nasync`; it writes nothing there, and it is skipped entirely
  outside a Flatpak, where the two locations are the same directory.

## [0.9.0] - 2026-08-07

Groundwork for shipping NASync as a Flatpak. Background syncing was the one
feature the sandbox structurally could not support, because it worked by
writing a systemd user unit and calling `systemctl` — neither of which exists
inside a Flatpak. Nothing changes for a pip or pipx install.

### Added
- **Background syncing works inside a Flatpak, via the XDG Background portal.**
  `nasync/portal.py` asks the desktop for permission to run in the background
  and to be started at login; the desktop owns the resulting autostart entry.
  Which mechanism is used is decided by where NASync is installed, not by a
  setting — `service.backend()` returns `"systemd"` outside a sandbox and
  `"portal"` inside one, and the CLI and desktop app go on asking `service` the
  same questions they always did.
- `nasync service status` now names the mechanism it's describing, so it's
  possible to tell which of the two is in play.

### Changed
- **The status line no longer claims to know things it can't.** systemd can be
  asked whether the service is running this second; the Background portal has
  no query method at all, and the permission can be revoked from the desktop's
  settings at any time. Under the portal, NASync reports that it is *set to
  start at login* — and says where to revoke that — rather than reporting it as
  running. Claiming a background process is alive when nothing can see it is
  the kind of reassuring lie that makes a sync tool untrustworthy.
- **The portal is asked once, not on every settings change.** Reconciling the
  background service happens after any automation setting is saved, and
  re-requesting each time would put a permission dialog up repeatedly — which
  is how a prompt becomes something people dismiss without reading. A refusal
  is recorded and honoured; turning background syncing off and on again is the
  deliberate way to be asked afresh.
- A partial grant is reported instead of being rounded up to success. The
  portal may allow background running but decline autostart, which means
  syncing stops at the next logout — worth saying out loud.

### Fixed
- `service.uninstall()` takes the caller's `Config` instead of opening its own.
  `Config` caches config.json in memory, so a second instance clearing the
  portal grant left the caller's copy still believing it was granted, and the
  status line drawn immediately afterwards said so.

### Migration
- Config gains an optional top-level `portal` key recording what the desktop
  last granted. It is written only inside a Flatpak, and its absence — which is
  every config written before 0.9.0 — reads correctly as *"we have never
  asked"*. Nothing needs converting, and an older NASync ignores the key.

## [0.8.0] - 2026-08-07

Comparing is the part of a sync the user actually waits through, and on a large
share it was taking longer than the transfer. This release rewrites both sides
of the comparison. Nothing about *what* gets synced changes.

### Changed
- **Indexing the server costs one round trip per directory instead of one per
  file.** `_remote_file_map` walked the share and then asked the server for
  each file's size individually — even though the size (`end_of_file`) already
  arrives in the directory listing. Every sync paid it, including ones with
  nothing to do, so a share big enough to matter sat at *"comparing files… 0%"*
  long enough to read as a hang rather than as work. Measured against a real
  Samba server with 3,000 files in 121 directories: **3,241 SMB requests down
  to 121**, and 8.34 s down to 0.54 s over loopback — where there is almost no
  latency to save. On a 3 ms LAN the same tree goes from roughly ten seconds to
  under one, and a 20k-file share from about a minute to a couple of seconds.
- **Directories are created once each, not once per file.** A push called
  `makedirs` for every single uploaded file's parent, so a thousand photos in
  one album meant a thousand identical round trips asking for a directory that
  already existed. Uploads now derive the set of parents, and drop the ones
  `makedirs` creates on the way to a deeper one: 1,000 files across 120 nested
  directories went from 1,000 calls to 41.
- **Scanning local folders is 5× faster.** The local index used
  `rglob("*")` with `Path.is_file()` and `Path.stat()`, which is two `stat`
  calls per file; `os.scandir` reports the type from the directory read itself,
  leaving one. Timed over a real 17,080-file tree: 0.650 s → 0.128 s, with a
  byte-identical result.

### Fixed
- **A first sync to a destination that doesn't exist yet no longer depended on
  a happy accident.** The "missing directory reads as an empty server" case
  caught `ObjectNameNotFound` / `ObjectPathNotFound`, but `smbclient` reports a
  missing path as `SMBOSError` — an `OSError` carrying `ENOENT` — so that
  handler never actually fired. It worked only because `smbclient.walk()`
  swallows listing errors silently and yielded nothing. Walking the tree
  directly removes that safety net, so the check now recognises both shapes.
- Symlinks on the server are resolved rather than taken from the listing. A
  reparse point's recorded size describes the link, not its target, so a
  symlinked file would have compared as changed on every single sync; a
  symlinked directory would have been indexed as though it were a file.
  Symlinked directories are still not descended into — following them can loop
  forever on a link that points at its own parent.
- Cancelling now interrupts the local scan too, not just the transfer. On a
  tree large enough for the scan to be worth cancelling, it previously ran to
  completion first.

## [0.7.2] - 2026-08-06

### Fixed
- **Renaming a server in the TUI no longer deletes its sync pairs.** It renamed
  by removing the old server and adding a new one, and removing a server takes
  the pairs it owns with it — so a rename silently discarded every folder that
  server synced. This is the bug fixed in the desktop app and the CLI in 0.6.0;
  it had survived in the terminal UI, which never adopted `Config.save_server`.
  If you renamed a server in `nasync tui` since 0.6.0, its pairs are gone and
  will need adding again — nothing on the NAS itself was touched.
- The desktop app coalesces its within-file progress updates. The callback
  arrives once per megabyte, so a transfer over a fast LAN was posting more
  than a hundred label repaints a second onto the GTK main loop — none of them
  readable, and all of them still queued behind whatever the window was doing.
  Updates are now dropped on the worker thread rather than queued and
  discarded, and the last update of each file always gets through so the bar
  still reaches full.

## [0.7.1] - 2026-08-06

Housekeeping after 0.7.0, which added a lot of code quickly. No behavioural
change was intended; the notes below say where one happened anyway.

### Changed
- **The automatic-syncing panel exists once, not twice.** The Settings dialog
  and the main window's popover were two hand-written copies of the same four
  switches — two sets of handlers, two copies of the "what is the background
  service doing?" ladder, and two wordings for every row. They had already
  started to drift. Both now drive one `AutomationSettings` controller and read
  their titles from one table, so a switch cannot be called one thing in the
  popover and another in Settings.
- The three repeated widget recipes in the main window — the destructive
  confirmation dialog (three copies), the flat icon button (four) — are one
  helper each. The confirmation helper always sets Cancel as the default
  response, which is what an accidental Enter selects; it was previously set
  independently in three places.
- `APP_ID` moved to `nasync/__init__.py`. It's the application's identity, not
  the GUI's, and importing the notifier — which exists to work in the headless
  `nasync auto` process — used to pull in the desktop-launcher module to fetch
  it.

### Fixed
- **A pending scheduled sync no longer probes the network every second.**
  Explaining *why* a run couldn't happen costs a blocking TCP connect, and it
  was computed on every tick before the once-a-day guard threw it away — so a
  laptop away from home with a run outstanding attempted roughly 3,600
  connections an hour. The explanation is now produced only when it's about to
  be shown, and shared with the log line that also wants it.
- Opening the automatic popover, or flipping a switch in it, asked systemd for
  its status twice — six `systemctl` processes for one answer. It asks once.
  Reconciling the service also stopped discarding the status it had just
  fetched on the worker thread and re-fetching it on the UI thread.

### Removed
- `Config.hint_seen` / `mark_hint_seen`, and the `hints_seen` key they wrote.
  The banner that used them went away in 0.7.0, and a persisted config key with
  no reader is one more thing every future migration has to carry.
- `Config.rename_server`, superseded by `save_server(orig_name=…)`. It was a
  second implementation of "renaming a server must not drop its pairs" — the
  invariant fixed in 0.6.0, and not one worth maintaining twice.
- Dead parameters that no caller ever passed: `Automator.on_lan(use_cache=)`,
  `Notifier.notify(replace=)`, and the unread `"changed"` key from
  `service.apply()`.

### Internal
- `nasync/conftest.py` holds the fixtures the suite had been re-declaring per
  module. The two copies of the recording notifier disagreed about whether a
  *disabled* notifier records anything, so a test passing against one proved
  nothing about the other.
- Corrected docstrings that described code that no longer exists — most
  importantly `Config`'s, which still said sync pairs were global and targeted
  whichever server was active. They have belonged to a server since 0.6.0, and
  that is precisely the area where getting it wrong deletes on the wrong NAS.

## [0.7.0] - 2026-08-06

### Added
- **Desktop notifications for syncs that happen on their own.** A background
  sync had no way to report itself: the log went to the journal and nobody
  reads the journal, so a service that quietly copied files and one that had
  stopped working looked exactly alike. Automatic syncs now say when they
  start and what they moved, and a scheduled run that *couldn't* happen —
  machine away from the home network, server refusing the connection — says
  so at critical urgency, because a backup that never ran is the one thing
  worth interrupting for. On by default; `nasync notifications off`, or the
  switch in the new automatic popover, turns it off.
  - The "started" notification is replaced in place by the "finished" one, so
    a machine syncing all day occupies one notification that stays current
    instead of filling the tray with its own history.
  - A blocked scheduled run announces itself **once**, not once per tick: the
    day's sync stays due until it succeeds, so the failure path is reached
    every second until then. It speaks up again if the reason changes, or
    after a run gets through.
  - Built on `gdbus`, falling back to `notify-send`. A machine with neither —
    a server, a headless session — syncs exactly as before, silently.
  - The desktop app notifies too, but only for a sync you aren't watching:
    a notification for a transfer whose progress bar is on screen is noise.
- `nasync notifications [on|off] [--test]`, and a line in `nasync status`.
  `--test` sends one immediately, and says which backend carried it.

### Changed
- **Sync mode and automatic syncing are now buttons on the main page**, each
  showing its own state and opening a popover that changes it. They were a
  caption above the progress bar plus switches two clicks deep in the menu —
  readable but not reachable, which is the wrong way round for the mode that
  deletes files on the server. The automatic button distinguishes *off* from
  **Paused**, the state where the triggers are configured but *Run in the
  background* is off; that one used to be invisible and read as "it's
  syncing". The Settings dialog keeps the same settings in full: both surfaces
  reload from the config when opened, so neither can show stale state.
- **The progress bar measures the pair being synced, not the whole run.**
  Spread across every pair, a big folder inched the bar forward by a couple of
  percent and read as stalled. Per pair it fills, resets, and fills again —
  visibly moving — and `Photos (2/4)` in the line below carries how far
  through the run you are.
- **The state — Idle, Syncing…, Complete, Failed — is centred directly on top
  of the progress bar**, with the running detail underneath it. It's the one
  thing to read at a glance and it used to sit in the bottom-left corner,
  below the bar and after the detail line.
- **Cancel moved to the right-hand edge of the action row**, on the same line
  as Sync, Dry run and Restore, and those three are no longer one linked
  strip. They aren't variations on a single action, and the gap before Cancel
  keeps the control that stops work clear of the ones that start it.
- The banner pointing at Settings is gone. It existed because the sync mode
  and the automatic triggers were only reachable through the menu, which is no
  longer true.

### Fixed
- **A running background service now sees settings changed in the app.** It
  watched `config.json`'s mtime but never re-read the file, so it kept acting
  on the settings it started with; only the restart that a settings change
  happens to trigger made it look current. `Config.reload()` re-reads it.

## [0.6.2] - 2026-08-06

### Added
- **Progress is now per pair.** A pair whose folders are all unchanged still
  advances the bar and names itself while it compares — a big unchanged folder
  transfers nothing but takes real time, and the window used to sit at 0%
  looking hung. The detail line reads `Photos (2/3) · Uploading holiday.jpg ·
  3 of 8 files · 0:12 elapsed`, and file and byte progress fill only that
  pair's share of the bar. `Syncer` gained an optional `pair_progress`
  callback; the CLI and TUI don't pass one and are unaffected.

### Changed
- The header keeps the app's identity — **NASync** with *Sync your NAS with
  CLASS* underneath — and the server tabs moved to their own bar below it,
  rather than replacing the title. With one server the tab bar stays hidden.
- The window reads top to bottom as **Server** → server → **Sync pairs** →
  pairs → actions → active status → **Progress** → progress → state and
  Cancel. The server and progress sections gained the headings the pairs
  section already had.
- Sync / Dry run / Restore are one linked group, so they read as a single set
  of actions.
- The server card shows its name again, since the header no longer does.

## [0.6.1] - 2026-08-06

### Added
- **Background syncing is now a switch** — Settings ▸ Automatic ▸ *Run in the
  background*, and `nasync service on|off`. Turning it off keeps live sync and
  the schedule configured but stops anything from running them, so automatic
  syncing pauses instead of being reset. On by default, so nothing changes for
  anyone who hasn't touched it.
- A banner pointing at Settings, since the sync mode and both automatic
  triggers were only reachable through the menu. It retires permanently the
  first time Settings is opened.

### Changed
- The transfer area reads top-down: what's happening above the progress bar,
  and the state (Idle / Complete / Failed) bottom-left with **Cancel**
  bottom-right on the same line. Cancel moved out of the action row, so the
  bottom of the window is state-and-stop rather than four buttons.
- The scheduled-sync clock is a compact horizontal field. Its stacked +/-
  buttons made that row twice the height of the switch rows beside it.

## [0.6.0] - 2026-08-06

### Changed
- **Sync pairs now belong to a server.** Each server keeps its own list, so a
  work NAS no longer inherits a home NAS's folders. Previously one global list
  was retargeted at whichever server was active — which meant switching servers
  silently pointed every pair at the new one, and in mirror mode that deletes
  on the wrong NAS. Removing a server removes its pairs; renaming one keeps
  them.
- **The desktop app is a tab per server.** Switching tabs switches the active
  server, and each tab holds that server's connection details and its pairs.
  The tab badge counts them, and a server with none is flagged. With one server
  the switcher stays hidden. A native empty state replaces the old "No servers
  yet" row.
- **Sync / Dry run / Restore moved above the transfer state**, both pinned to
  the bottom of the window: press a button, watch the result directly beneath
  it. Progress can no longer be scrolled off-screen mid-transfer. Sync is now
  the widest button as well as the accented one.
- The idle line names what a sync would do — "Sync sends 3 folder(s) to
  'home-nas'" — instead of "Pick a server, then Sync."
- Sync, Dry run and Restore are insensitive when there's no server to sync to,
  including after a transfer finishes.
- `nasync add` and `nasync remove` take `--server`; `nasync list` takes
  `--server` and `--all`. `nasync status` groups pairs under their server.

### Fixed
- Renaming a server in the desktop app deleted its sync pairs, because saving
  a rename was a remove-then-add. Renames now move the pairs with the server
  (`Config.save_server`).

### Migration
- The pre-0.6.0 global `pairs` list is given to the active server (or the only
  server, if none is active) on first load. Pairs added before any server
  exists are kept as-is and adopted once one is added, rather than dropped.

## [0.5.0] - 2026-08-06

### Added
- **Live sync** (default off) — syncs a few seconds after a file changes, the
  OneDrive-style behaviour. A burst of writes is coalesced into one sync, and
  syncs never overlap.
- **Live sync only runs on the server's network.** The check is the routing
  table's own answer — a NAS reachable without going through a gateway is on
  this machine's LAN — falling back to a fast TCP probe for private addresses
  on another subnet of the same network (VLANs, mesh routers, wired/wireless
  splits). A public address never qualifies, however reachable. Away from home
  the watcher keeps running and doesn't fire, so coming home syncs whatever
  piled up. New `nasync.lan` module.
- **Scheduled sync** (default off) — syncs once a day at a time you pick. A run
  missed because the machine was asleep or away isn't skipped: the day's sync
  stays due until it succeeds, so it catches up on wake or on getting home.
- **A background systemd user service** runs both, so they keep working with
  NASync closed and start again at login. Turning either trigger on installs
  and enables it; turning the last one off removes it, rather than leaving an
  idle service enabled. Settings changes are picked up by a running service
  without a restart.
- Desktop app: menu (⋯) → **Settings** → **Automatic**, with a switch each, an
  HH:MM time picker, and a row reporting the background service's real state.
  Settings is now two pages (*Syncing* and *Automatic*).
- The window's **Transfer** section says whether automatic syncing is on and
  when, next to the sync mode.
- `nasync live-sync [on|off]`, `nasync scheduled-sync [on|off] [--at HH:MM]`,
  and `nasync service [status|install|uninstall|restart]`. `nasync status` and
  `nasync auto --status` report all of it.

### Changed
- `nasync auto` now runs whatever the saved settings ask for (this is what the
  background service runs); its `--on-change` flag is now `--live`, and it
  gained `--schedule` / `--at`. It refuses to run with everything off rather
  than sitting there doing nothing.
- `nasync watch` keeps its foreground, settings-free behaviour but now runs on
  the same engine as live sync, which means it gets the network check too.
- The TUI's session auto-sync toggle also checks the network before syncing.
- Automatic syncing logs arriving and leaving the network once each, instead of
  a line per check — this runs for days at a time as a service.
- The "am I home?" answer is cached for 30s, so a pending scheduled sync
  doesn't re-probe the network every second.

### Removed
- `nasync/watch.py`. Its `Watcher` was a second, slightly different copy of the
  automation loop, and the copy without the network check; `nasync watch` now
  uses `Automator`.

### Migration
- The pre-0.5.0 `automation.enabled` / `automation.on_change` pair folds into
  `live_sync`: anyone who had `nasync auto` watching keeps watching, everyone
  else lands on the new default of off. A saved `interval_minutes` still works.

## [0.4.0] - 2026-08-06

### Added
- **Sync mode — Mirror or Update.** A sync always uploads new and changed
  files; the mode decides what happens to files that are on the server but no
  longer in your local folder. **Mirror** (the default, and what every previous
  version did) deletes them so the server matches your folders exactly.
  **Update** leaves them alone, so a sync only ever adds and overwrites and
  nothing on the server is ever deleted.
- The mode is saved once and honoured by every front-end — `nasync sync`,
  `watch`, `auto`, the TUI, and the desktop app.
- Desktop app: menu (⋯) → **Settings** → *Sync mode*, a preferences dialog with
  the two modes spelled out in full. It applies instantly, per the GNOME HIG.
  <kbd>Ctrl</kbd>+<kbd>,</kbd> opens it.
- The active mode is shown under **Transfer** in the app window and announced
  in the log at the start of every push, so a run that deletes can't be a
  surprise.
- `nasync mode [mirror|update]` shows or sets the saved mode; `nasync status`
  now includes it.
- `nasync sync --mode mirror|update` overrides the saved mode for one run.
  `--no-delete` is kept as an alias for `--mode update`.

### Notes
- **Restore is unaffected and stays non-destructive.** Pulling from the server
  adds and overwrites local files; local files the server doesn't have are
  never deleted, in either mode. The desktop dialog and `nasync mode` both say
  so, since the setting otherwise reads as symmetric.
- Existing installs keep their current behaviour: with no mode saved, and with
  a corrupted or hand-edited value, the mode falls back to mirror.

## [0.3.0] - 2026-08-05

### Added
- **Native desktop app** (`nasync gui`): a GTK4 + libadwaita window that
  follows the system light/dark theme, built on the same `Config`/`Syncer`
  engine as the CLI and TUI. Saved servers as a radio list, sync pairs with a
  real folder picker, add/edit dialogs that can test a connection and load the
  server's share list, and Sync / Dry run / Restore with a cancellable
  transfer. Blocking SMB work runs on worker threads and is marshalled back
  onto the GTK main loop, so the window stays responsive.
- The progress bar accounts for progress *within* the current file, so one
  large file no longer leaves the bar sitting still.
- Files that fail individually are collected and listed behind a **Show
  errors** button instead of vanishing into a summary count.
- `nasync install-desktop --tui` forces the launcher at the terminal UI.
- The desktop entry and icon are installed under the application ID
  (`io.github.YuvalBogo.NASync`) and the app sets a matching program name, so
  GNOME associates the running window with its launcher and shows the NASync
  logo in the dash and alt-tab instead of a generic icon. `StartupWMClass` is
  set for the same reason on X11.
- Upgrading removes the pre-0.3.0 `nasync.desktop`/`nasync.svg`, which would
  otherwise linger as a second, stale entry in the application menu.
- libadwaita 1.5 is now checked before launching. Older runtimes (Ubuntu 22.04
  ships 1.1) get the version they have, the version they need, and a pointer to
  `nasync tui` — rather than an AttributeError from inside a widget.

### Changed
- **The desktop launcher now opens the desktop app.** `install-desktop` probes
  for GTK4 + libadwaita at install time: with them the app-menu entry launches
  `nasync gui` directly (`Terminal=false`), and without them it falls back to
  the previous behaviour of opening `nasync tui` in a terminal.
- `install-desktop` bakes in the executable that is actually running it rather
  than whatever `PATH` resolves `nasync` to. Those are frequently different
  installs, and only the running one is the interpreter just probed for GTK.
- Docs: the README architecture section and the QUICKSTART install steps still
  described the pre-0.2.x `smbclient`-subprocess backend and told users to
  install `samba-client`. Neither is needed since the move to `smbprotocol`.

### Fixed
- `nasync gui` printed a raw traceback instead of its install instructions when
  PyGObject was missing: `nasync.gui` defers its `gi` import into `run()`, so
  guarding the import in the CLI never caught the real failure.
- `Syncer` dropped the `unsupported` count (files whose names SMB rejects) from
  its summary, so front-ends reporting it always showed zero.
- The GUI counted only whole-pair failures, reporting a run as "Complete" when
  individual files within it had failed.
- `nasync.gui` was missing from the packaged subpackages, so an installed
  nasync could not find the desktop app at all.

## [0.2.0] - 2026-08-03

### Added
- **Incremental mirror sync.** `sync` now uploads only new/changed files
  (unchanged files, matched by size, are skipped) and deletes server files that
  were removed locally, so the server mirrors your folder — much faster on
  repeat runs. `--no-delete` keeps server-only files.
- **Automation layer.** `nasync auto` keeps folders synced automatically —
  on file changes (watch) and/or every N minutes (`--interval`), with settings
  remembered; ideal as a systemd user service. The TUI has an **Auto-sync**
  toggle for a live session watcher.
- **Saved, named servers.** Configure multiple servers, each with a name, and
  switch the active one instead of re-entering IP/user/share every time. New
  CLI commands `servers` (list) and `use <name>` (switch active); `init`/`setup`
  now name the server. Existing single-server configs migrate automatically.

### Changed
- **TUI redesign around saved servers + native progress.** The server section
  is now a compact list of saved servers — pick one to activate; the connection
  fields live in an Add/Edit modal (with a Test button). This frees the left
  column for a larger **Sync pairs** area. Transfers show a native progress bar
  with percentage + ETA, a live `X / N files` object counter, and elapsed time,
  now embedded at the top of the **Activity** (log) card as a styled progress
  box. Sync pairs can be **edited** in place (Edit button / `update_pair`). In
  modals, **Enter** confirms and **Esc** cancels.
- **Project layout:** renamed the Python package `samba_sync` → `nasync` (so
  the package, command, distribution, and config dir all share one name).
  Removed redundant `setup.py` and `requirements.txt` — packaging metadata now
  lives solely in `pyproject.toml`.
- **TUI: live per-file progress.** Push/pull now stream `smbclient` output and
  emit a per-file progress callback, so the TUI shows a dnf-style progress bar
  with an `X/total` counter and the current file name.
- **TUI: refined, symmetric layout.** Two equal columns (Servers + Sync pairs
  on the left; Actions + Progress on the right), section titles embedded in
  rounded borders. The verbose log pane was removed in favour of a centred
  **Progress** hero — colored bar, current file, `X / N files` + elapsed, and a
  final `↑ uploaded · ✓ unchanged · ✗ removed` summary.
- **Branding:** the app is now **NASync — "Sync your NAS with class"** (shown in
  the README and the TUI header). The command stays `nasync`.

### Added
- **Desktop launcher** (`nasync install-desktop`): installs a freedesktop
  `.desktop` entry and an app icon under `~/.local/share` so nasync shows up in
  the application menu and opens the TUI in a terminal. `--uninstall` removes
  it. Linux only. Ships an SVG app icon in the package.
- **Interactive terminal UI** (`nasync tui`, install with `nasync[tui]`): a
  Textual app with a server panel + Test-connection button, auto-populated
  share picker, a sync-pairs table, and Sync/Dry-run/Restore actions that
  stream progress into a log pane. Runs the same engine as the CLI.

### Changed
- **Internal:** `SAMBAClient`/`Syncer` now take an injectable `reporter`
  callback (defaults to `print`) and `Syncer` returns a `{"ok", "failed"}`
  summary instead of raising `SystemExit`. This lets non-CLI front-ends (the
  TUI) drive the same code and capture progress; CLI behaviour is unchanged.
- **Renamed the command from `samba-sync` to `nasync`** (shorter to type). The
  distribution and config directory are now `nasync` too (`~/.config/nasync/`).

### Added
- `setup` command: a guided first-run wizard that collects server details with
  a live connection test, auto-detects and lets you pick a share, and adds sync
  pairs — reusing the same validation as `test-connection`.
- `test-connection` command: verifies the server is reachable, credentials are
  accepted, and the configured share exists — and lists available shares when
  the configured one is wrong.
- Configurable SMB port (prompted during `init`, default 445).
- `restore --yes/-y` to skip the confirmation prompt (for automation).

### Fixed
- **Live progress froze after the first file** on multi-file uploads:
  `smbclient` block-buffers its output through a pipe, so per-file lines arrived
  in a lump. It now runs against a pseudo-terminal (line-buffered) with the
  command script fed on a writer thread, so progress streams smoothly.
- **Performance**: sync/restore now run in a single `smbclient` session per
  directory (native recursion) instead of opening a new SMB connection for
  every file — dramatically faster on large trees.
- **Auth**: pass the password via `PASSWD` (the variable `smbclient` actually
  reads); the previous `SMBCLIENT_PASSWORD` was never picked up, which could
  stall non-interactive syncs.
- **Nested directories**: parent directories are now created in order, so
  deeply nested trees push correctly.
- **Restore reliability**: replaced fragile `ls` output parsing (which
  misdetected any file modified in December as a directory) with `mget`.
- **Watch**: switched to a trailing-edge debounce so the final state after a
  burst of edits is always synced (previously trailing changes could be lost).
- `--dry-run` now actually previews instead of performing the transfer, and a
  push dry-run works without contacting the server.
- Duplicate sync-pair names are rejected; blank-password (guest) shares work.

## [0.1.0] - 2026-08-02

### Added
- Initial release of SAMBA Sync Auto
- `init` command for server configuration setup
- `add` command to create sync pairs with flexible naming
- `list` command to view all configured sync pairs
- `remove` command to delete sync pairs
- `sync` command for manual push (local → server)
- `restore` command for manual pull (server → local)
- `watch` command for automatic file watching and syncing
- `status` command to check configuration and sync pairs
- Support for `SAMBA_PASSWORD` environment variable for scripting
- Cross-platform support (Linux/macOS)
- Secure credential storage with restricted file permissions
- Comprehensive error handling and user-friendly messages
- File system monitoring via watchdog

### Features
- Local machine as source of truth
- Recursive directory synchronization
- Automatic platform-specific configuration paths
- systemd service example for daemon mode
- Support for cron-based backup scheduling
- Detailed logging of sync operations

## Future Roadmap
- **macOS `.app` bundle**: a `nasync install-desktop` equivalent for macOS that
  generates `~/Applications/nasync.app` (Info.plist + `.icns` icon assembled
  from the SVG via `iconutil` + a launcher that opens the TUI in Terminal), so
  Mac users get a clickable app too. Deferred until it can be verified on real
  macOS hardware (Gatekeeper behaviour and the Terminal launch need testing).
- Delta/incremental sync mode (only transfer changed files)
- Conflict resolution strategies
- Performance optimizations for large directories
