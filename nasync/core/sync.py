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
                 cancel_event=None, pair_progress=None, config=None):
        self.config = config or Config()
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
        server_config = self.config.get_server_config()
        # Password may legitimately be blank (guest shares); the rest is required.
        if not all(server_config.get(k) for k in ("ip", "username", "share")):
            raise Exception("Server not configured. Run 'nasync setup' first.")

        pairs = self.config.get_pairs()
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
        """Push local → server. ``mirror=None`` uses the configured sync mode."""
        if mirror is None:
            mirror = self.config.get_sync_mode() == "mirror"
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
            # A push dry-run only reads local disk, so don't require the server.
            if not dry_run:
                client.connect()
            for index, pair in enumerate(pairs, start=1):
                self._pair_progress(index, len(pairs), pair["name"], verb)
                self._report(f"\n{icon} {verb}: {pair['name']}  ({index}/{len(pairs)})")
                if direction == "push":
                    self._report(f"  Local:  {pair['local']}")
                    self._report(f"  Server: {pair['server']}")
                else:
                    self._report(f"  From Server: {pair['server']}")
                    self._report(f"  To Local:    {pair['local']}")
                try:
                    stats = client.sync_directory(
                        pair["local"], pair["server"],
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
