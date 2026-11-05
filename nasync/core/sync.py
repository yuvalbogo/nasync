import threading
import time

from nasync.core import state as sync_state
from nasync.core.activity import Activity
from nasync.core.config import Config
from nasync.core.samba import MirrorRefused, SAMBAClient, SyncCancelled


class Syncer:
    """Orchestrates push/pull across configured pairs.

    Output is routed through an injectable ``reporter`` callback (defaults to
    ``print`` so the CLI is unchanged). Operations *return a summary dict*
    instead of exiting, so any front-end can decide how to react::

        {"ok", "failed", "cancelled",            # per pair
         "transferred", "skipped", "deleted", "moved", "errors", "unsupported",
         "conflicts",     # files changed on the NAS, set aside as copies
         "foreign",       # files on the NAS NASync didn't put there, kept
         "local_errors",  # of "errors": files/folders here that couldn't be
                          # read — reported, but a retry can't fix them
         "kept_local",    # restore: local files kept, changed since backup
         "direction",     # "push" or "pull"
         "dry_run",
         "refused",       # [{pair, message, deletes, total, confirmable,
                          #   fingerprint}] — held back, needing a yes
         "overlapping",   # [pair names skipped for sharing a server folder]
         "pairs"}         # {pair name: "ok" | "held" | "failed" | "skipped"}

    Each pair carries a record of what was last uploaded (see
    :mod:`nasync.core.state`); a dry run reads it but never writes it.

    A real (not dry) run holds the machine-wide sync lock for its length — see
    :mod:`nasync.core.activity` — and raises
    :class:`~nasync.core.activity.SyncBusy` if another process already does.
    ``source`` and ``trigger`` say who is asking, for whoever looks.
    """

    def __init__(self, reporter=None, progress=None, byte_progress=None,
                 cancel_event=None, pair_progress=None, config=None,
                 server: str = None, source: str = "app", trigger: str = "",
                 stage=None):
        self.config = config or Config()
        #: Which server this syncer targets; None means the active one. Named
        #: rather than implied, because the mode, the connection and the pairs
        #: must all come from the *same* server — since 0.18.0 the mode is a
        #: per-server setting, so reading it globally could push one server's
        #: files under another server's rule about deleting.
        self.server = server
        self.source = source
        self.trigger = trigger
        self._report = reporter or print
        self._progress = progress
        # Optional per-byte progress and a threading.Event for cancellation.
        # The event always exists, so another process can cancel this run
        # through the activity files even when the caller supplied none.
        self._byte_progress = byte_progress
        self._cancel = cancel_event or threading.Event()
        # Per-*pair* progress: fires as each pair starts, so a front-end can
        # show movement across pairs even for ones that turn out to be
        # unchanged and transfer no files at all.
        self._pair_progress = pair_progress or (
            lambda index, total, name, verb: None)
        # What the run is doing between files ("Reading the server's copy…"),
        # for the window and for the status file a background run publishes.
        self._stage_callback = stage
        self._last_stage = ("", 0.0)
        self._activity = None

    def _stage(self, text: str) -> None:
        """Pass a stage on, at most a few times a second for the same kind."""
        kind = text.split("…", 1)[0]
        now = time.monotonic()
        if kind == self._last_stage[0] and now - self._last_stage[1] < 0.25:
            return
        self._last_stage = (kind, now)
        if self._stage_callback is not None:
            self._stage_callback(text)
        if self._activity is not None:
            # A new stage is not a file count: drop the last one's, so the
            # app doesn't show "Uploading 40/40" while it deletes.
            self._activity.update(stage=text, files_done=0, files_total=0, file="")

    def _file_progress(self, done, total, name, verb) -> None:
        if self._progress is not None:
            self._progress(done, total, name, verb)
        if self._activity is not None:
            self._activity.update(files_done=done, files_total=total,
                                  file=name, verb=verb)

    def _prepare(self, pair_name):
        """Validate config and return (client, pairs) ready to transfer."""
        server_config = self.config.get_server_config(self.server)
        # Password may legitimately be blank (guest shares); the rest is required.
        if not all(server_config.get(k) for k in ("ip", "username", "share")):
            raise Exception("Server not configured. Run 'nasync setup' first.")
        if self.config.credentials_error:
            # Said as what it is. Connecting anyway, with the empty password a
            # locked keyring reads as, was reported as "Authentication failed.
            # Check username and password" — about a password that is fine.
            raise Exception(f"Saved passwords couldn't be read: "
                            f"{self.config.credentials_error}")

        pairs = self.config.get_pairs(self.server)
        if not pairs:
            raise Exception("No sync pairs configured. Run 'nasync add' first.")

        if pair_name:
            pairs = [p for p in pairs if p["name"] == pair_name]
            if not pairs:
                raise Exception(f"Pair '{pair_name}' not found.")

        client = SAMBAClient(
            server_config["ip"],
            server_config["username"],
            server_config["password"],
            server_config["share"],
            port=server_config.get("port", 445),
            encrypt=server_config.get("encrypt", False),
            identity=server_config.get("identity"),
            reporter=self._report,
            progress=self._file_progress,
            byte_progress=self._byte_progress,
            cancel_event=self._cancel,
            stage=self._stage,
        )
        return client, pairs

    def sync_pair(self, pair_name: str = None, dry_run: bool = False,
                  mirror: bool = None, allow_mass_delete=False) -> dict:
        """Push local → server. ``mirror=None`` uses this server's sync mode.

        ``allow_mass_delete`` is a person's yes to a mirror run the safety
        check held back: True for every pair, or a collection of pair names.
        It never overrides the refusal to mirror an empty folder. A
        ``{pair name: fingerprint}`` dict is a yes to exactly the changes that
        were shown for each pair (see `SAMBAClient.check_mirror`).
        """
        if mirror is None:
            mirror = self.config.get_sync_mode(self.server) == "mirror"
        return self._run(pair_name, direction="push", dry_run=dry_run,
                         mirror=mirror, allow_mass_delete=allow_mass_delete)

    def restore_pair(self, pair_name: str = None, dry_run: bool = False) -> dict:
        # Restore deliberately never mirrors: pulling from the server must not
        # delete local files that the server happens not to have. The sync mode
        # governs the push side only.
        return self._run(pair_name, direction="pull", dry_run=dry_run, mirror=False)

    def _run(self, pair_name, direction, dry_run, mirror=True,
             allow_mass_delete=False) -> dict:
        client, pairs = self._prepare(pair_name)
        server_name = self.server or self.config.get_active_server_name()
        share = self.config.get_server_config(self.server).get("share", "")
        verb, icon = ("Syncing", "📤") if direction == "push" else ("Restoring", "📥")

        # Taken before connecting: if another process is mid-run, this one
        # should not even open a session, let alone start comparing.
        activity = None
        if not dry_run:
            activity = Activity(self.config.config_dir, source=self.source,
                                trigger=self.trigger,
                                server=self.server
                                or self.config.get_active_server_name(),
                                cancel_event=self._cancel)
            activity.acquire()
            activity.update(force=True, direction=direction)
        self._activity = activity

        if direction == "push":
            self._report(
                "🪞 Mirror mode — the server will match your local folders "
                "exactly; server-only files are deleted."
                if mirror else
                "➕ Update mode — new and changed files are copied; "
                "nothing is deleted on the server."
            )

        # Pairs whose server folders overlap delete each other's files, so a
        # push skips them until they are pointed apart. Configs from before
        # 0.20.0 can contain them; new ones can't be created.
        clashes = (self.config.overlapping_pairs(self.server)
                   if direction == "push" else {})

        ok = 0
        failed = 0
        cancelled = False
        refused = []
        overlapping = []
        # "unsupported" only comes back from the push side; `.get(key, 0)`
        # below lets the pull side omit it without special-casing.
        totals = {"transferred": 0, "skipped": 0, "deleted": 0, "moved": 0,
                  "deleted_dirs": 0, "errors": 0, "unsupported": 0,
                  "conflicts": 0, "foreign": 0, "local_errors": 0,
                  "kept_local": 0}
        if not dry_run and not pair_name:
            # Records of destinations no pair points at any more would be
            # trusted wrongly if one were pointed back there later.
            sync_state.prune(self.config.config_dir, server_name, {
                sync_state.pair_key(server_name, share, self.config.resolve_remote(
                    p["server"], self.server))
                for p in self.config.get_pairs(self.server)})
        result = None
        try:
            # A dry run connects too. Working out what a **mirror** would
            # delete requires indexing the destination, and without a session
            # that index comes back empty or errors — so the one command
            # someone runs to ask "what will this delete before I let it?"
            # could not answer.
            self._stage(f"Connecting to '{server_name}'…")
            client.connect()
            if client.identity and not self.config.get_server_config(
                    self.server).get("identity"):
                # First contact: remember which machine this is, so a
                # different one at the same address is never sent the password.
                self.config.set_server_identity(server_name, client.identity)
            unattended = self.source == "background"
            outcome = {}
            for index, pair in enumerate(pairs, start=1):
                self._pair_progress(index, len(pairs), pair["name"], verb)
                if activity is not None:
                    activity.update(force=True, pair=pair["name"],
                                    pair_index=index, pair_total=len(pairs),
                                    files_done=0, files_total=0, file="",
                                    verb=verb)
                self._report(f"\n{icon} {verb}: {pair['name']}  ({index}/{len(pairs)})")
                landing = self.config.resolve_remote(pair["server"], self.server)
                if direction == "push":
                    self._report(f"  Local:  {pair['local']}")
                    self._report(f"  Server: {landing}")
                else:
                    self._report(f"  From Server: {landing}")
                    self._report(f"  To Local:    {pair['local']}")

                if pair["name"] in clashes:
                    others = ", ".join(f"'{n}'" for n in clashes[pair["name"]])
                    self._report(
                        f"❌ Skipped {pair['name']}: its server folder overlaps "
                        f"that of {others}, so in mirror mode each would delete "
                        f"the other's files. Give each pair a folder of its own.")
                    overlapping.append(pair["name"])
                    outcome[pair["name"]] = "skipped"
                    failed += 1
                    continue

                if isinstance(allow_mass_delete, dict):
                    allowed = allow_mass_delete.get(pair["name"], False)
                else:
                    allowed = (allow_mass_delete is True
                               or (bool(allow_mass_delete)
                                   and pair["name"] in allow_mass_delete))
                record = sync_state.SyncState(self.config.config_dir, server_name,
                                              share, landing, read_only=dry_run)
                try:
                    # The pair stores its destination relative to the server's
                    # sync root; this is where the two are joined. Doing it here
                    # rather than at `add` time means changing a server's root
                    # moves its pairs with it, and a pair can never name a path
                    # outside the root even if the config file is hand-edited.
                    record.register()
                    push = {"unattended": unattended,
                            "outstanding": record.held} if direction == "push" else {}
                    stats = client.sync_directory(
                        pair["local"], landing,
                        direction=direction, dry_run=dry_run, mirror=mirror,
                        allow_mass_delete=allowed, state=record, **push)
                    held = stats.get("held")
                    held_deletes = bool(held and held.get("hold_deletes", True))
                    server_errors = stats.get("errors", 0) - stats.get("local_errors", 0)
                    if direction == "push" and not dry_run and not held_deletes:
                        # From now on a server file without a record was put
                        # there by someone else, and is never deleted. Not
                        # while deletions are held back: until someone answers,
                        # the files in question must still count as NASync's.
                        # Errors don't stop it: a file the run missed is one
                        # NASync then *keeps* (as someone else's), never one
                        # it deletes — while staying in bootstrap meant
                        # deleting other people's files on every run, for as
                        # long as one name couldn't be stored.
                        record.mark_initialised()
                        record.mark_mirrored(mirror)
                    if direction == "push" and not dry_run:
                        record.mark_held(held_deletes)
                    for key in totals:
                        totals[key] += stats.get(key, 0)
                    if stats.get("refused"):
                        refused.append({"pair": pair["name"],
                                        "message": stats["refused"],
                                        "dry_run": True})
                    if held:
                        refused.append({"pair": pair["name"], **held})
                        outcome[pair["name"]] = "held"
                        self._report(f"⚠️  {pair['name']} done, part held back")
                    else:
                        outcome[pair["name"]] = "failed" if server_errors else "ok"
                        self._report(f"✅ {pair['name']} done"
                                     + (" — some files here couldn't be read"
                                        if stats.get("local_errors") else ""))
                    ok += 1
                except MirrorRefused as e:
                    self._report(f"🛑 {pair['name']} held back: {e}")
                    refused.append({"pair": pair["name"], "message": str(e),
                                    "deletes": e.deletes, "total": e.total,
                                    "confirmable": e.confirmable,
                                    "fingerprint": e.fingerprint,
                                    "sample": e.sample})
                    outcome[pair["name"]] = "held"
                    failed += 1
                except SyncCancelled:
                    # A cancel is a deliberate user action, not a failure —
                    # stop the whole run rather than moving to the next pair.
                    cancelled = True
                    break
                except Exception as e:
                    self._report(f"❌ Failed on {pair['name']}: {e}")
                    outcome[pair["name"]] = "failed"
                    failed += 1
                finally:
                    record.close()
            result = {"ok": ok, "failed": failed, "cancelled": cancelled,
                      "refused": refused, "overlapping": overlapping,
                      "pairs": outcome, "direction": direction,
                      "dry_run": dry_run, **totals}
        finally:
            client.release()
            self._activity = None
            if activity is not None:
                activity.release(result)

        if cancelled:
            self._report("\n⏹️  Cancelled.")
        elif failed:
            self._report(f"\n⚠️  Completed with {failed} error(s)")
        return result
