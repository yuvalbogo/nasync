import asyncio
import os
import tempfile


class _FakeSMB:
    def __init__(self, *a, **k):
        self.reporter = k.get("reporter")

    def list_shares(self):
        return ["backup", "media"]

    def connect(self):
        return None


class _FakeSyncer:
    def __init__(self, reporter=None, progress=None):
        self._r = reporter or print
        self._progress = progress

    def sync_pair(self, pair_name=None, dry_run=False):
        self._r("  pushing…")
        return {"ok": 1, "failed": 0}

    def restore_pair(self, pair_name=None, dry_run=False):
        self._r("  pulling…")
        return {"ok": 1, "failed": 0}


def test_tui_flow():
    """Drive the TUI headlessly: test connection, save server, add a pair, sync."""
    tmp = tempfile.mkdtemp()
    os.environ["XDG_CONFIG_HOME"] = tmp

    import nasync.tui as tui
    from nasync.config import Config
    from textual.widgets import Input, Select

    tui.SAMBAClient = _FakeSMB
    tui.Syncer = _FakeSyncer

    async def scenario():
        app = tui.NasyncApp()
        # Large size so every control is in the viewport for pilot clicks.
        async with app.run_test(size=(140, 50)) as pilot:
            app.query_one("#ip", Input).value = "127.0.0.1"
            app.query_one("#user", Input).value = "alice"

            # Test connection -> shares load into the Select
            await pilot.click("#btn-test")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#share", Select).value == "backup"

            # Save server -> persisted to config
            await pilot.click("#btn-save")
            await pilot.pause()
            sc = Config().get_server_config()
            assert sc["ip"] == "127.0.0.1" and sc["share"] == "backup", sc

            # Add a pair through the modal (its widgets live on the modal screen)
            await pilot.click("#btn-add")
            await pilot.pause()
            assert type(app.screen).__name__ == "AddPairScreen"
            app.screen.query_one("#ap-local", Input).value = "/tmp"
            app.screen.query_one("#ap-dest", Input).value = "/Docs"
            await pilot.click("#ap-ok")
            await pilot.pause()
            assert [p["name"] for p in Config().get_pairs()] == ["tmp"]

            # Sync (fake) -> completes without error
            await pilot.click("#btn-sync")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(scenario())
    print("✅ test_tui_flow passed")


if __name__ == "__main__":
    test_tui_flow()
