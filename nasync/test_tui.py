import asyncio
import os
import tempfile


class _FakeSMB:
    def __init__(self, *a, **k):
        pass

    def list_shares(self):
        return ["backup", "media"]

    def connect(self):
        return None


class _FakeSyncer:
    def __init__(self, reporter=None, progress=None):
        self._r = reporter or print
        self._progress = progress

    def sync_pair(self, pair_name=None, dry_run=False):
        if self._progress:
            self._progress(1, 1, "file.txt", "Uploading")
        self._r("  pushing…")
        return {"ok": 1, "failed": 0}

    def restore_pair(self, pair_name=None, dry_run=False):
        self._r("  pulling…")
        return {"ok": 1, "failed": 0}


def test_tui_flow():
    """Drive the TUI headlessly: add a server via the modal, add a pair, sync."""
    tmp = tempfile.mkdtemp()
    os.environ["XDG_CONFIG_HOME"] = tmp

    import nasync.tui as tui
    from nasync.config import Config
    from textual.widgets import Input

    tui.SAMBAClient = _FakeSMB
    tui.Syncer = _FakeSyncer

    async def scenario():
        app = tui.NasyncApp()
        async with app.run_test(size=(160, 50)) as pilot:
            # --- add a server through the modal ---
            await pilot.click("#srv-add")
            await pilot.pause()
            assert type(app.screen).__name__ == "AddServerScreen"
            app.screen.query_one("#sv-name", Input).value = "home"
            app.screen.query_one("#sv-ip", Input).value = "127.0.0.1"
            app.screen.query_one("#sv-user", Input).value = "alice"
            await pilot.click("#sv-test")            # loads shares (fake)
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#sv-ok")
            await pilot.pause()

            assert Config().get_active_server_name() == "home"
            servers = Config().list_servers()
            assert servers[0]["ip"] == "127.0.0.1" and servers[0]["share"] == "backup"

            # --- add a pair through its modal ---
            await pilot.click("#btn-add")
            await pilot.pause()
            app.screen.query_one("#ap-local", Input).value = "/tmp"
            app.screen.query_one("#ap-dest", Input).value = "/Docs"
            await pilot.click("#ap-ok")
            await pilot.pause()
            assert [p["name"] for p in Config().get_pairs()] == ["tmp"]

            # --- edit that pair (change destination) ---
            await pilot.click("#btn-edit")
            await pilot.pause()
            assert type(app.screen).__name__ == "AddPairScreen"
            app.screen.query_one("#ap-dest", Input).value = "/NewDocs"
            await pilot.click("#ap-ok")
            await pilot.pause()
            assert Config().get_pair("tmp")["server"] == "/NewDocs"

            # --- sync (fake) drives progress + finishes cleanly ---
            await pilot.click("#btn-sync")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(scenario())
    print("✅ test_tui_flow passed")


if __name__ == "__main__":
    test_tui_flow()
