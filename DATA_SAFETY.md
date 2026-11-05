# Data safety

NASync copies, overwrites and deletes files on a NAS that people use as their
backup. This file is the contract: what NASync promises, how each promise is
kept, what it cannot promise, and how to check all of it before a release.

It is also what the data-safety auditor reads first (see
[Auditing](#auditing)), so an accepted limitation listed here is not reported
as a new finding — unless it turns out to be worse than stated.

## The promises

Each line is something a person would naturally assume. Each is checked
against a real Samba server by the lab (`make lab`).

**If I change a file, the NAS gets the change.**
A file has changed when its size or modification time differs at all from
what NASync last uploaded — an *older* version put back counts — unless its
checksum proves the content the same. Without a record (see below), a file
changed when its size differs or the local copy is newer.

**Nothing on the NAS is destroyed without a copy surviving somewhere.**
- A file changed **on the NAS** since NASync uploaded it is never overwritten:
  it is renamed to `name (NAS copy YYYY-MM-DD HHMM).ext` first, and kept.
- A file **someone else put on the NAS** is never deleted. (Before a pair's
  first complete sync under the record, NASync can't tell such files apart
  from ones it uploaded earlier — see [Bootstrap](#bootstrap).) A local file
  that can't be read does not keep a pair in bootstrap.
- **Conflict copies are never deleted** by a mirror, even without a record.
- Uploads are written under a temporary name and renamed into place, so an
  interrupted upload never leaves a truncated file where the good copy was.
  Downloads do the same on the local side. If a replace is interrupted at
  any step — even by Ctrl+C or a killed process — the complete old copy is
  put back, or recovered by the next sync and found by restore.
- A half-written download left by a killed restore is never uploaded.

**A mistake can't wipe the backup.**
- An **empty local folder is never mirrored** over a server folder that has
  files.
- Deleting or overwriting **more than half** of a pair's server files (50+)
  waits for a person. The background service holds back at **a fifth**. A
  held-back run still backs up new files; only the destructive part waits.
- A "yes" covers exactly the deletions and overwrites that were shown (a
  fingerprint of each list); anything found later is asked about again.
- The **first mirror run** of a pair — including a server switched from update
  to mirror — waits for a person before deleting 50 or more files.
- Of two local names differing only in letter case, the one already on the
  NAS is the one backed up; the other is reported, and never replaces it.
- A local folder or file that **can't be read** is protected: nothing under it
  is deleted, and it is reported.
- **Each pair has a server folder of its own.** Pairs whose folders are the
  same or nested — on one server entry, or on two entries for the same NAS —
  are refused when added, and skipped by sync if an older config has them.
- An upgrade never makes a server **more destructive** than it was.

**Restore gives back what I had, and never takes away what I have.**
- It never overwrites a local file that changed since it was last backed up
  (or, without a record, one newer than the server's copy). It never deletes
  anything local.
- It fails loudly if the server folder doesn't exist.
- It restores modification times, and permissions where the record has them.

**My password only goes to my NAS.**
NASync learns the NAS's identity (its SMB server GUID) on first contact and
checks it, before sending any credentials, on every connection after. On
Samba that identity is the server's host name — see the limitations.

**Renaming a server entry changes nothing on the NAS.** The record, the
identity, the pairs and the settings all move with the new name.

**If something isn't backed up, I am told.**
Skipped files are listed with a reason (a name SMB can't store, two names
differing only in case, a symlinked folder, an unreadable file). Failures give
a non-zero exit code. The background service notifies when a run is held back,
when changes have waited over an hour, when a scheduled server hasn't backed up
for three days, and when a folder can't be watched.

**One sync at a time, visible from anywhere.**
The app, the CLI and the background service share a lock and a status file.

## How it is enforced

| Mechanism | Where |
|:--|:--|
| The record of what was uploaded | `nasync/core/state.py` |
| Deciding what a run does (pure, unit-tested) | `SAMBAClient.plan_push` in `nasync/core/samba.py` |
| Holding back mass deletes and overwrites | `SAMBAClient.check_mirror` |
| Atomic uploads and downloads | `_upload_file`, `_swap_into_place`, `_download_file` |
| Unreadable, skipped and colliding names | `SAMBAClient._scan_local` |
| Restore's rules | `SAMBAClient._pull_directory` |
| Server identity | `SAMBAClient.server_identity`, `connect` |
| One server folder per pair | `Config.pair_overlapping`, `overlapping_pairs`, `same_storage` |
| Settings never undone by a stale copy | `_mutator`, `Config.refresh`, atomic `Config.save` |
| Cross-process lock and status | `nasync/core/activity.py` |
| Nothing a watcher saw is dropped | `Automator.note_changes`, `catch_up`, `fallback_due` |

### The record

`sync-state.db` (SQLite, in the config directory) holds, per pair, what NASync
last put on the server: the local size and time it had, the server size and
time it got, its SHA-256 (taken while uploading, so free), and its permissions.
It is keyed by server, share and resolved destination — pointing a pair at a
new destination starts a new record, and the old one is pruned.

### Bootstrap

Until a pair has completed one sync with a record, NASync can't tell a file
someone else put on the NAS from one it uploaded with an older version. In
that state the old rules apply — with one extra guard: deleting 50 or more
files waits for a person, however small a share that is. In update mode the
leftovers are recorded as NASync's, so turning mirror on later removes them
(after the same guard).

## Accepted limitations

- **Restore on a new machine doesn't bring back permissions.** An SMB share
  has nowhere to keep Unix permissions; they live in the local record, which a
  new machine doesn't have. Times and contents are restored.
- **Server identity stops accidents, not attackers — and on Samba it is the
  host name.** Samba's "GUID" is its NetBIOS name padded with zeros (checked
  against real Samba), so two Samba boxes left at the same default name
  ("raspberrypi") look like one machine: a stranger with that name passes the
  check (the password itself is never sent — NTLM proves it without sending
  it). A different name is always caught. For the same reason, two entries
  with the same Samba name count as one storage only when their addresses
  resolve to the same machine; the same NAS reached by two *different*
  addresses (LAN and VPN) is then not detected, and the record's "never
  delete what NASync didn't put there" is what protects it.
- **A DST hour is forgiven only when it moves most files at once.** FAT/exFAT
  volumes report every time an hour off across DST; that is accepted without
  reading the files. A single file an hour off is checked by checksum (when
  the record has one), so a NAS-side edit made an hour after the upload is
  kept as a conflict copy.
- **Folder case renames stay at the old casing on the server.** The files are
  kept and matched; only the folder's letter case lags.
- **Composed and decomposed Unicode names** (NFC/NFD) are distinct on Linux
  and Samba keeps both; a server that normalises names would not, and NASync
  doesn't detect that.
- **Hard links** are uploaded as separate files; **sockets, FIFOs and devices**
  are not data and are skipped without a report.
- **Only one machine per destination.** Two computers mirroring to the same
  server folder each see the other's files as "added on the NAS" (kept) and
  the other's edits as conflicts (copies). Nothing is lost, but it is not a
  two-way sync.

## Checking it

```bash
make test        # unit tests: decisions as plain data, with a fake SMB layer
make lab         # the lab: the real engine against a throwaway Samba container
```

The lab (`tests/lab/`) needs podman or docker. It starts one container on a
random port, gives every test an isolated config directory, hides the session
bus so the real keyring is never touched, and checks outcomes on the server's
own disk. It cannot reach a real NAS.

- `test_edge_cases.py` — one test per promise above, phrased as the user's
  assumption.
- `test_random_history.py` — random sequences of real-world edits (same-size
  edits, older versions put back, folder and case renames, deletes, touches),
  syncing after each and comparing byte for byte, then a full restore.
  Replay a failure with `NASYNC_LAB_SEED=<seed>`; lengthen with
  `NASYNC_LAB_STEPS`.
- `test_audit_findings.py` — every finding of the 0.21.0 audit, reproduced.

Run the lab before every release, and after any change to how files are
compared, copied, renamed or deleted.

## Auditing

`~/.claude/agents/sync-data-safety-auditor.md` is an adversarial auditor for
sync and backup code: it reads this file, walks the code paths that write and
delete, works through a question bank of ways backups go wrong, and proves
what it finds against a throwaway server. Ask Claude Code to run it after any
change to the engine, before a release, or whenever a change makes you ask
"could this lose something?".
