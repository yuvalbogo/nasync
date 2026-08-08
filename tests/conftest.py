"""Fixtures shared across the suite.

The fakes here used to be declared once per test module, and had already
drifted: two copies of the recording notifier disagreed about whether a
*disabled* notifier records anything, so a test passing against one proved
nothing about the other. One copy is the point of this file.
"""

import tempfile

import pytest

from nasync.system import vault
from nasync.core.config import Config

#: A fixed key for the tests that exercise sealing, so a run is reproducible
#: and produces the same failure twice.
TEST_KEY = b"\x11" * vault.KEY_BYTES


@pytest.fixture(autouse=True)
def offline_vault(monkeypatch):
    """Keep the whole suite away from the developer's real keyring.

    Without this, anything that saves a server reaches the Secret Service on
    the machine running the tests: it creates a key item in a real keyring, and
    the suite then passes or fails depending on whether that machine has one
    unlocked. Tests default to the no-backend path — plaintext 0600, exactly
    what a headless install does — and `sealed_vault` opts back in.
    """
    monkeypatch.setattr(vault, "backend", lambda: "")


@pytest.fixture
def sealed_vault(monkeypatch):
    """Exercise the encrypted path with a fixed key and no keyring at all.

    Patches the key *source*, not the crypto: what gets written is a real
    AES-GCM envelope, so the round trip under test is the real one.
    """
    monkeypatch.setattr(vault, "backend", lambda: "keyring")
    monkeypatch.setattr(vault, "key", lambda create=True, allow_prompt=True: TEST_KEY)
    return TEST_KEY


class RecordingNotifier:
    """Stands in for `notify.Notifier`, keeping what it was asked to show.

    Mirrors the real signature, including the disabled short-circuit — a fake
    that always records would let a test claim notifications were suppressed
    when they weren't.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.sent = []

    def notify(self, summary, body="", urgency="normal"):
        if not self.enabled:
            return 0
        self.sent.append((summary, body, urgency))
        return 1


@pytest.fixture
def recording_notifier():
    return RecordingNotifier()


@pytest.fixture
def config_home(monkeypatch):
    """An isolated XDG_CONFIG_HOME, so tests never touch the real config.

    Via `monkeypatch.setenv` rather than a bare assignment, so the variable is
    restored afterwards instead of leaking into every test that follows.
    """
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("XDG_CONFIG_HOME", tmp)
        yield tmp


@pytest.fixture
def configured_config(config_home):
    """A Config with one server and one pair — the usual starting point."""
    config = Config()
    config.add_or_update_server("nas", "192.168.1.50", "u", "p", "share", 445)
    config.set_active_server("nas")
    config.add_pair(config_home, "/dest", "pair")
    return config
