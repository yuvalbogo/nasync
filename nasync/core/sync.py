from nasync.core.config import Config
from nasync.core.samba import SAMBAClient, SyncCancelled


class Syncer:
    """Orchestrates push/pull across configured pairs.

    Output is routed through an injectable ``reporter`` callback (defaults to
    ``print`` so the CLI is unchanged). Operations *return a summary dict*
    instead of exiting, so any front-end can decide how to react::

        {"ok", "failed", "cancelled",            # per pair
         "transferred", "skipped", "deleted", "errors", "unsupported"}
    """

    def __init__(self, reporter=None, progress=None, byte_progress=None,
                 cancel_event=None, pair_progress=None, config=None,
                 server: str = None):
        self.config = config or Config()
        #: Which server this syncer targets; None means the active one. Named
        #: rather than implied, because the mode, the connection and the pairs
        #: must all come from the *same* server — since 0.18.0 the mode is a
        #: per-server setting, so reading it globally could push one server's
        #: files under another server's rule about deleting.
        self.server = server
        self._report = reporter or print
        self._progress = progress
        # Optional per-byte progress and a threading.Event for cancellation;
        # front-ends that don't supply them behave exactly as before.
        self._byte_progress = byte_progress
        self._cancel = cancel_event
        # Per-*pair* progress: fires as each pair starts, so a front-end can
        # show movement across pairs even for ones that turn out to be
        # unchanged and transfer no files at all.
        self._pair_progress = pair_progress or (
            lambda index, total, name, verb: None)

    def _prepare(self, pair_name):
        """Validate config and return (client, pairs) ready to transfer."""
        server_config = self.config.get_server_config(self.server)
        # Password may legitimately be blank (guest shares); the rest is required.
        if not all(server_config.get(k) for k in ("ip", "username", "share")):
            raise Exception("Server not configured. Run 'nasync setup' first.")

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
            reporter=self._report,
            progress=self._progress,
            byte_progress=self._byte_progress,
            cancel_event=self._cancel,
        )
        return client, pairs

    def sync_pair(self, pair_name: str = None, dry_run: bool = False,
                  mirror: bool = None) -> dict:
        """Push local → server. ``mirror=None`` uses this server's sync mode."""
        if mirror is None:
            mirror = self.config.get_sync_mode(self.server) == "mirror"
        return self._run(pair_name, direction="push", dry_run=dry_run, mirror=mirror)

    def restore_pair(self, pair_name: str = None, dry_run: bool = False) -> dict:
        # Restore deliberately never mirrors: pulling from the server must not
        # delete local files that the server happens not to have. The sync mode
        # governs the push side only.
        return self._run(pair_name, direction="pull", dry_run=dry_run, mirror=False)

    def _run(self, pair_name, direction, dry_run, mirror=True) -> dict:
        client, pairs = self._prepare(pair_name)
        verb, icon = ("Syncing", "📤") if direction == "push" else ("Restoring", "📥")

        if direction == "push":
            self._report(
                "🪞 Mirror mode — the server will match your local folders "
                "exactly; server-only files are deleted."
                if mirror else
                "➕ Update mode — new and changed files are copied; "
                "nothing is deleted on the server."
            )

        ok = 0
        failed = 0
        cancelled = False
        # "unsupported" only comes back from the push side; `.get(key, 0)`
        # below lets the pull side omit it without special-casing.
        totals = {"transferred": 0, "skipped": 0, "deleted": 0, "errors": 0,
                  "unsupported": 0}
        try:
            # A dry run connects too. The comment here used to say a push
            # dry-run "only reads local disk", which was wrong in the case that
            # matters most: working out what a **mirror** would delete requires
            # indexing the destination, and without a session that index comes
            # back empty or errors — so the one command someone runs to ask
            # "what will this delete before I let it?" could not answer.
            client.connect()
            for index, pair in enumerate(pairs, start=1):
                self._pair_progress(index, len(pairs), pair["name"], verb)
                self._report(f"\n{icon} {verb}: {pair['name']}  ({index}/{len(pairs)})")
                landing = self.config.resolve_remote(pair["server"], self.server)
                if direction == "push":
                    self._report(f"  Local:  {pair['local']}")
                    self._report(f"  Server: {landing}")
                else:
                    self._report(f"  From Server: {landing}")
                    self._report(f"  To Local:    {pair['local']}")
                try:
                    # The pair stores its destination relative to the server's
                    # sync root; this is where the two are joined. Doing it here
                    # rather than at `add` time means changing a server's root
                    # moves its pairs with it, and a pair can never name a path
                    # outside the root even if the config file is hand-edited.
                    stats = client.sync_directory(
                        pair["local"],
                        self.config.resolve_remote(pair["server"], self.server),
                        direction=direction, dry_run=dry_run, mirror=mirror,
                    )
                    for key in totals:
                        totals[key] += stats.get(key, 0)
                    self._report(f"✅ {pair['name']} done")
                    ok += 1
                except SyncCancelled:
                    # A cancel is a deliberate user action, not a failure —
                    # stop the whole run rather than moving to the next pair.
                    cancelled = True
                    break
                except Exception as e:
                    self._report(f"❌ Failed on {pair['name']}: {e}")
                    failed += 1
        finally:
            client.release()

        if cancelled:
            self._report("\n⏹️  Cancelled.")
        elif failed:
            self._report(f"\n⚠️  Completed with {failed} error(s)")
        return {"ok": ok, "failed": failed, "cancelled": cancelled, **totals}
