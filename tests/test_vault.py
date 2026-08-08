"""Credential sealing: the round trip, and every way it is allowed to fail.

The failures matter more than the happy path here. An encrypted credential
store that silently returns nothing is worse than a plaintext one, because the
error it produces — "wrong password" from the NAS — points at the wrong thing
entirely. Each test below pins one failure to a message that names its own
cause.
"""

import json

import pytest

from nasync.system import vault
from conftest import TEST_KEY
from nasync.core.config import Config


@pytest.fixture
def creds():
    return {"nas": "hunter2", "backup": "s3cr3t"}


# ---------------------------------------------------------------------- #
# The round trip
# ---------------------------------------------------------------------- #
def test_sealing_then_unsealing_returns_the_same_passwords(sealed_vault, creds):
    envelope = vault.seal(creds)
    assert vault.unseal(envelope) == creds


def test_a_sealed_envelope_contains_no_plaintext(sealed_vault, creds):
    envelope = vault.seal(creds)
    blob = json.dumps(envelope)
    assert "hunter2" not in blob
    assert "s3cr3t" not in blob
    # The server names are secrets too: knowing someone has a "backup" share is
    # information the file shouldn't leak just because the password is safe.
    assert "backup" not in blob


def test_sealing_twice_produces_different_ciphertext(sealed_vault, creds):
    """A fresh nonce per write, so identical passwords aren't identifiable."""
    first, second = vault.seal(creds), vault.seal(creds)
    assert first["data"] != second["data"]
    assert first["nonce"] != second["nonce"]


def test_with_no_backend_the_map_passes_through_unsealed(creds):
    """The headless path: no keyring, so the file stays as it always was."""
    assert vault.seal(creds) == creds
    assert not vault.is_envelope(vault.seal(creds))


# ---------------------------------------------------------------------- #
# Failures that must name themselves
# ---------------------------------------------------------------------- #
def test_a_tampered_ciphertext_is_refused_rather_than_returned(sealed_vault, creds):
    envelope = vault.seal(creds)
    envelope["data"] = envelope["data"][:-4] + "AAAA"
    with pytest.raises(vault.VaultError, match="couldn't be decrypted"):
        vault.unseal(envelope)


def test_a_different_key_is_refused(sealed_vault, creds, monkeypatch):
    envelope = vault.seal(creds)
    monkeypatch.setattr(vault, "key",
                        lambda create=True, allow_prompt=True: b"\x22" * vault.KEY_BYTES)
    with pytest.raises(vault.VaultError, match="couldn't be decrypted"):
        vault.unseal(envelope)


def test_a_host_sealed_file_opened_in_a_sandbox_says_so(sealed_vault, creds,
                                                        monkeypatch):
    """The one failure a user is likely to hit: pip install → Flatpak.

    The host's key comes from the keyring and the sandbox's from the portal, so
    the file genuinely cannot be opened. What matters is that it is reported as
    the migration problem it is, not as corruption.
    """
    envelope = vault.seal(creds)
    assert envelope["backend"] == "keyring"

    monkeypatch.setattr(vault, "backend", lambda: "portal")  # the sandbox
    with pytest.raises(vault.ForeignSeal, match="keyring-based install"):
        vault.unseal(envelope)


def test_an_envelope_from_a_newer_nasync_is_refused_by_version(sealed_vault, creds):
    envelope = vault.seal(creds)
    envelope[vault.ENVELOPE_KEY] = vault.ENVELOPE_VERSION + 1
    with pytest.raises(vault.VaultError, match="newer version"):
        vault.unseal(envelope)


def test_a_damaged_envelope_is_refused(sealed_vault, creds):
    envelope = vault.seal(creds)
    del envelope["nonce"]
    with pytest.raises(vault.VaultError, match="damaged"):
        vault.unseal(envelope)


def test_key_without_a_backend_raises_rather_than_inventing_one(monkeypatch):
    monkeypatch.setattr(vault, "backend", lambda: "")
    with pytest.raises(vault.VaultError, match="No keyring or secret portal"):
        vault.key()


def test_describe_names_what_is_actually_true():
    assert "keyring" in vault.describe("keyring")
    assert "portal" in vault.describe("portal")
    assert "not encrypted" in vault.describe("")


# ---------------------------------------------------------------------- #
# Through Config — the surface everything else uses
# ---------------------------------------------------------------------- #
def test_saved_passwords_survive_a_reload(config_home, sealed_vault):
    config = Config()
    config.add_or_update_server("nas", "10.0.0.2", "u", "pw", "share")

    config.reload()
    assert config.get_server("nas")["password"] == "pw"
    assert config.credentials_error is None


def test_the_creds_file_on_disk_is_encrypted(config_home, sealed_vault):
    # Long enough not to appear in base64 by chance: a two-character password
    # collides with the ciphertext often enough to make this test flaky.
    password = "correct-horse-battery-staple"
    config = Config()
    config.add_or_update_server("nas", "10.0.0.2", "u", password, "share")

    raw = config.creds_file.read_text()
    assert password not in raw
    assert vault.is_envelope(json.loads(raw))


def test_the_creds_file_stays_0600_when_encrypted(config_home, sealed_vault):
    config = Config()
    config.add_or_update_server("nas", "10.0.0.2", "u", "pw", "share")
    assert config.creds_file.stat().st_mode & 0o777 == 0o600


def test_a_plaintext_creds_file_is_encrypted_on_next_start(config_home, monkeypatch):
    """The 0.11.0 migration, from the shape every existing install has."""
    config = Config()
    config.add_or_update_server("nas", "10.0.0.2", "u", "pw", "share")
    assert json.loads(config.creds_file.read_text()) == {"nas": "pw"}

    # Now the same install, on a machine where a keyring has appeared.
    monkeypatch.setattr(vault, "backend", lambda: "keyring")
    monkeypatch.setattr(vault, "key", lambda create=True, allow_prompt=True: TEST_KEY)

    migrated = Config()
    assert vault.is_envelope(json.loads(migrated.creds_file.read_text()))
    assert migrated.get_server("nas")["password"] == "pw"


def test_a_locked_keyring_is_reported_not_swallowed(config_home, sealed_vault,
                                                    monkeypatch):
    config = Config()
    config.add_or_update_server("nas", "10.0.0.2", "u", "pw", "share")

    def locked(create=True, allow_prompt=True):
        raise vault.CredentialsLocked("The keyring is locked.")

    monkeypatch.setattr(vault, "key", locked)
    reopened = Config()
    # The app still starts and still knows about the server…
    assert reopened.get_server("nas")["ip"] == "10.0.0.2"
    # …but says why the password is missing rather than trying to connect
    # without one and blaming the NAS.
    assert reopened.credentials_error
    assert "locked" in reopened.credentials_error.lower()


# ---------------------------------------------------------------------- #
# Never destroy what we couldn't read (0.13.1)
# ---------------------------------------------------------------------- #
def _two_servers(config_home):
    config = Config()
    config.add_or_update_server("nas-a", "10.0.0.1", "u", "PASSWORD-A", "s")
    config.add_or_update_server("nas-b", "10.0.0.2", "u", "PASSWORD-B", "s")
    return config


def _b_still_recoverable(monkeypatch) -> bool:
    monkeypatch.setattr(vault, "backend", lambda: "keyring")
    monkeypatch.setattr(vault, "key",
                        lambda create=True, allow_prompt=True: TEST_KEY)
    return Config().get_server("nas-b")["password"] == "PASSWORD-B"


def test_saving_one_server_cannot_delete_another_servers_password(
        config_home, sealed_vault, monkeypatch):
    """The read-modify-write trap. A failed read yields {}; writing that back
    deletes every password it couldn't decrypt, permanently."""
    _two_servers(config_home)

    # The file is now sealed by "keyring"; open it as a Flatpak would.
    monkeypatch.setattr(vault, "backend", lambda: "portal")
    monkeypatch.setattr(vault, "key",
                        lambda create=True, allow_prompt=True: b"\x22" * vault.KEY_BYTES)

    reopened = Config()
    assert reopened.check_credentials()          # the read did fail

    with pytest.raises(vault.VaultError, match="would delete them"):
        reopened.add_or_update_server("nas-a", "10.0.0.1", "u", "PASSWORD-A", "s")

    assert _b_still_recoverable(monkeypatch)


def test_removing_a_server_cannot_empty_the_whole_credential_file(
        config_home, sealed_vault, monkeypatch):
    """`remove_server` writes back the map it just read — {} if the read failed."""
    _two_servers(config_home)

    monkeypatch.setattr(vault, "backend", lambda: "portal")
    monkeypatch.setattr(vault, "key",
                        lambda create=True, allow_prompt=True: b"\x22" * vault.KEY_BYTES)

    reopened = Config()
    reopened.check_credentials()
    with pytest.raises(vault.VaultError):
        reopened.remove_server("nas-a")

    assert _b_still_recoverable(monkeypatch)


def test_a_vanished_keyring_never_rewrites_an_encrypted_file_as_plaintext(
        config_home, sealed_vault, monkeypatch):
    """`seal()` passes the map through unsealed with no backend — right for a
    machine that never had a keyring, catastrophic for one that did."""
    _two_servers(config_home)

    monkeypatch.setattr(vault, "backend", lambda: "")      # no keyring today
    reopened = Config()
    reopened.check_credentials()

    with pytest.raises(vault.VaultError, match="in the clear"):
        reopened.add_or_update_server("nas-a", "10.0.0.1", "u", "PASSWORD-A", "s")

    raw = json.loads(reopened.creds_file.read_text())
    assert vault.is_envelope(raw), "an encrypted file was downgraded"
    assert _b_still_recoverable(monkeypatch)


def test_check_credentials_finds_the_problem_status_would_report(
        config_home, sealed_vault, monkeypatch):
    """Regression: `credentials_error` is only set by a read, and nothing on
    the way to a status screen reads a password — so the warning never fired."""
    _two_servers(config_home)
    monkeypatch.setattr(vault, "backend", lambda: "portal")

    fresh = Config()
    assert fresh.credentials_error is None       # nothing has looked yet
    assert "keyring-based install" in fresh.check_credentials()
    assert fresh.credentials_error


def test_a_healthy_config_reports_no_credential_problem(config_home, sealed_vault):
    _two_servers(config_home)
    assert Config().check_credentials() == ""


def test_an_unwritable_key_never_downgrades_an_encrypted_file(config_home,
                                                              sealed_vault,
                                                              monkeypatch):
    """A keyring that fails mid-session must not rewrite the file in plaintext."""
    config = Config()
    config.add_or_update_server("nas", "10.0.0.2", "u", "pw", "share")
    sealed = config.creds_file.read_text()

    def broken(*_a, **_k):
        raise vault.VaultError("keyring went away")

    monkeypatch.setattr(vault, "key", broken)
    with pytest.raises(vault.VaultError):
        config.add_or_update_server("other", "10.0.0.3", "u", "pw2", "share")

    assert config.creds_file.read_text() == sealed
