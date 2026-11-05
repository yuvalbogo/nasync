"""The background automation, running for real against a real server."""

import datetime
import os
import threading

import pytest

from nasync.core.automation import AutomationRunner, Automator


class Notices:
    """Stands in for the desktop: keeps what would have been shown."""
    enabled = True

    def __init__(self):
        self.sent = []

    def notify(self, summary, body="", urgency="normal"):
        self.sent.append((summary, body, urgency))
        return len(self.sent)

    def close(self, withdrawer=None):
        pass


@pytest.fixture
def service(world):
    """`nasync auto` for the lab server, in a thread; stopped afterwards."""
    started = []

    def start():
        notices = Notices()
        log = []
        automator = Automator(reporter=log.append, config=world.config,
                              notifier=notices, server="lab")
        runner = AutomationRunner([automator], config=world.config,
                                  reporter=log.append)

        def run():
            try:
                runner.run()
            except BaseException:            # shown in the test's failure
                import traceback
                log.append(traceback.format_exc())
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        started.append((runner, thread))
        return automator, notices, log

    yield start
    for runner, thread in started:
        runner.stop()
        thread.join(timeout=30)


def test_live_sync_backs_up_an_edit_within_seconds(world, service):
    world.pair("docs", {"a.txt": "first"})
    world.config.set_automation(live_sync=True, server="lab")
    automator, notices, log = service()
    world.wait_for(lambda: "a.txt" in world.server_tree("docs"), timeout=60,
                   what=f"the startup catch-up sync; the service said: {log}")
    world.write("docs", "new/idea.txt", "written while the service runs")
    world.wait_for(lambda: "new/idea.txt" in world.server_tree("docs"), timeout=60,
                   what="live sync to back up the new file")
    world.assert_mirrored("docs")
    assert any("Syncing" in s or "Sync complete" in s for s, _, _ in notices.sent) \
        or any("transferred" in b for _, b, _ in notices.sent), notices.sent


def test_a_missed_scheduled_run_catches_up_and_is_recorded(world, service):
    world.pair("docs", {"report.txt": "due tonight"})
    earlier = (datetime.datetime.now() - datetime.timedelta(minutes=5)).strftime("%H:%M")
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    world.config.set_automation(scheduled_sync=True, scheduled_time=earlier,
                                last_scheduled_run=yesterday, server="lab")
    service()
    world.wait_for(lambda: "report.txt" in world.server_tree("docs"), timeout=60,
                   what="the missed scheduled run")

    def recorded():
        world.config.refresh()
        return world.config.get_automation("lab")["last_scheduled_run"] \
            == datetime.date.today().isoformat()
    world.wait_for(recorded, timeout=30, what="today's run to be recorded")


def test_a_held_run_is_announced_and_then_asked_by_a_person(world, service):
    """The service holds a 30% delete back; it says so; a person's next Sync
    asks before going ahead instead of waving it through."""
    files = {f"f{i:03d}.txt": f"file {i}" for i in range(300)}
    world.pair("p", files)
    world.nasync("sync", "--server", "lab", check=0)
    for i in range(90):
        os.remove(os.path.join(world.local("p"), f"f{i:03d}.txt"))
    earlier = (datetime.datetime.now() - datetime.timedelta(minutes=5)).strftime("%H:%M")
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    world.config.set_automation(scheduled_sync=True, scheduled_time=earlier,
                                last_scheduled_run=yesterday, server="lab")
    automator, notices, log = service()
    world.wait_for(lambda: any("held" in (s + b).lower() for s, b, _ in notices.sent),
                   timeout=60, what="a 'held back' notification")
    assert len(world.server_tree("p")) == 300

    world.nasync("sync", "--server", "lab", check=1)
    assert "would delete 90" in world.last_output, world.last_output
    assert len(world.server_tree("p")) == 300, "a person's Sync waved the hold through"
