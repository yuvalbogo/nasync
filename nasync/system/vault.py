"""Credential protection at rest — keeping NAS passwords out of a readable file.

Until 0.11.0 NASync stored every saved server's password as plaintext JSON in
``~/.config/nasync/.creds``, protected by nothing but mode 0600. File
permissions stop another *user* reading it; they stop nothing that already runs
as you — a backup that sweeps up dotfiles, a synced home directory, a stray
`cat`, anything you `pip install`. Encrypting the wire (see the ``encrypt``
setting on a server) while leaving that file readable is protecting the strong
link.

The shape here is: **one random key, three places it can come from, AES-GCM over
the whole credential map.**

  * **portal** — used inside a Flatpak, and only there.
    ``org.freedesktop.portal.Secret`` hands the app a private master secret with
    **no manifest permission at all**, where reaching the keyring directly needs
    ``--talk-name=org.freedesktop.secrets``. Same trade, and the same reasoning,
    as the notification backend in 0.10.0.
  * **keyring** — the Secret Service (GNOME Keyring, KWallet) everywhere else,
    holding a single 32-byte item. Not the passwords themselves — see below.
  * **""** — no secret service reachable at all: a headless box, an SSH session
    with no bus, a desktop with no keyring daemon. The file stays exactly as it
    is today, plaintext at 0600, and :func:`backend` returns "" so the front
    ends can *say* that rather than implying a protection that isn't there.

**Why one key over a file, rather than one keyring item per password** — which
would be the obvious design, and is what most apps do:

  * The Secret portal is not a password store. It gives a sandboxed app one
    master secret and nothing else. Per-password items would mean the sandbox
    and the host storing credentials in structurally different ways, with two
    formats, two migrations and two sets of failure modes.
  * ``Config._import_from_host`` (0.10.0) carries a pip install's settings into
    the Flatpak by *copying files*. Passwords living in keyring items would
    arrive missing — precisely the "imported but can't connect" failure that
    migration was written to prevent.

That second point has a sharp edge worth knowing about: the host's key and the
sandbox's key are **different secrets**, so a `.creds` sealed on the host cannot
be opened inside the Flatpak. The envelope therefore records which backend
sealed it, so that case is reported as what it actually is — "your passwords
were saved by a different install, please re-enter them" — instead of as a
generic decryption failure. See :class:`ForeignSeal`.

Nothing here ever loses data silently. A key that cannot be obtained raises,
and the caller reports it; it never quietly falls back to writing plaintext over
something that used to be encrypted.
"""

import base64
import json
import os
import secrets as _random

from nasync.system import portal

#: Bumped only if the envelope layout changes in a way older code can't read.
ENVELOPE_VERSION = 1

#: The marker that tells a `.creds` file apart from the pre-0.11.0 plaintext
#: map. Kept inside the JSON rather than as a byte prefix so that reading the
#: file is still one `json.loads` regardless of which era wrote it.
ENVELOPE_KEY = "nasync_sealed"

#: What the keyring item is called and how it is found again. The label is what
#: shows up in Seahorse, so it says what it is for rather than naming a file.
KEY_LABEL = "NASync credential key"
KEY_ATTRIBUTES = {"application": "nasync", "purpose": "credential-encryption"}

#: Domain separation: ties a ciphertext to this specific use of the key, so a
#: value from some other context can never be decrypted as a credential map.
AAD = b"nasync.credentials.v1"

KEY_BYTES = 32
NONCE_BYTES = 12

#: How long to wait for a keyring unlock prompt. This is a human deciding, but
#: unlike the background portal there is a sane failure — carry on locked — so
#: it doesn't wait indefinitely.
UNLOCK_TIMEOUT_SECONDS = 60


class VaultError(Exception):
    """Base for every failure to protect or recover credentials."""


class CredentialsLocked(VaultError):
    """The keyring exists but is locked, and nothing may prompt right now.

    Raised rather than returning an empty map because the two are not the same
    thing: no passwords means "connect anonymously and fail confusingly", while
    this means "your passwords are right there, unlock the keyring".
    """


class ForeignSeal(VaultError):
    """The file was sealed by a different install, whose key we cannot have.

    In practice: a pip install's `.creds`, encrypted with the host keyring's
    key, imported into a Flatpak whose key comes from the portal instead.
    """


def _crypto():
    """The bits of `cryptography` used here, or None if it isn't importable.

    It ships as a dependency of `smbprotocol`, so this is all but guaranteed —
    but credential storage failing to *import* would be a much worse failure
    than credential storage being unavailable, so it is treated as optional.
    """
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError:
        return None
    return AESGCM, HKDF, hashes


def _secretstorage():
    """The Secret Service client, or None where it isn't installed."""
    try:
        import secretstorage
    except ImportError:
        return None
    return secretstorage


def backend() -> str:
    """Which key source is usable here: "portal", "keyring", or "".

    Deliberately not cached. Unlike a portal interface, whether the keyring
    answers can change within a session — the daemon starts late, the bus
    appears when a desktop session does — and the cost of asking is one D-Bus
    round trip on a path that already touches disk.
    """
    if _crypto() is None:
        return ""
    if portal.in_flatpak() and portal.available(portal.SECRET_IFACE):
        return "portal"
    if _secretstorage() is not None and _keyring_reachable():
        return "keyring"
    return ""


def _keyring_reachable() -> bool:
    """True when a Secret Service is actually answering on the session bus.

    A *locked* keyring counts as reachable: it can be unlocked, and reporting
    it as absent would send the user to the wrong problem entirely.
    """
    secretstorage = _secretstorage()
    if secretstorage is None:
        return False
    try:
        connection = secretstorage.dbus_init()
    except Exception:
        return False
    try:
        secretstorage.get_default_collection(connection)
        return True
    except Exception:
        return False
    finally:
        connection.close()


# ---------------------------------------------------------------------- #
# The key
# ---------------------------------------------------------------------- #
def _derive(material: bytes) -> bytes:
    """Stretch whatever a backend handed us into exactly one AES-256 key.

    The portal's secret is opaque bytes of unspecified length and the keyring's
    is 32 bytes we generated, so running both through HKDF means the rest of
    this module never has to care which it got.
    """
    AESGCM, HKDF, hashes = _crypto()
    del AESGCM
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None,
                info=AAD).derive(material)


def key(create: bool = True, allow_prompt: bool = True) -> bytes:
    """The encryption key for this install, deriving or creating it as needed.

    `create=False` refuses to mint a new key, which is what reading wants: a
    missing key means the ciphertext is unopenable, and inventing a fresh one
    would turn that into a silent, permanent loss of every saved password.

    `allow_prompt=False` is for unattended callers — `nasync auto` must never
    block a scheduled sync on a dialog nobody is in front of.
    """
    which = backend()
    if which == "portal":
        try:
            return _derive(portal.retrieve_secret())
        except RuntimeError as e:
            raise VaultError(str(e))
    if which == "keyring":
        return _derive(_keyring_key(create=create, allow_prompt=allow_prompt))
    raise VaultError(
        "No keyring or secret portal is available to hold the encryption key.")


def _keyring_key(create: bool = True, allow_prompt: bool = True) -> bytes:
    """Fetch — or first create — the single random item in the Secret Service."""
    secretstorage = _secretstorage()
    if secretstorage is None:
        raise VaultError("The 'secretstorage' package isn't installed.")

    try:
        connection = secretstorage.dbus_init()
    except Exception as e:
        raise VaultError(f"Couldn't reach the session bus: {e}")

    try:
        collection = secretstorage.get_default_collection(connection)
        if collection.is_locked():
            if not allow_prompt:
                raise CredentialsLocked(
                    "The keyring is locked and this process can't ask you to "
                    "unlock it.")
            # unlock() reports whether the *prompt was dismissed*, so True is
            # the failure here, not the success.
            #
            # `timeout` only exists from secretstorage 3.5. Debian 12 and
            # Ubuntu 22.04/24.04 ship 3.3.x, and a pipx install made with
            # --system-site-packages picks up the distro's copy — so calling it
            # unconditionally raises TypeError on exactly the systems the
            # Flatpak floor already excludes, which is the audience we most
            # need this to work for.
            try:
                dismissed = collection.unlock(timeout=UNLOCK_TIMEOUT_SECONDS)
            except TypeError:
                dismissed = collection.unlock()
            if dismissed:
                raise CredentialsLocked("The keyring was not unlocked.")

        for item in collection.search_items(KEY_ATTRIBUTES):
            secret = item.get_secret()
            if secret:
                return secret

        if not create:
            raise VaultError("The credential key is missing from the keyring.")
        secret = _random.token_bytes(KEY_BYTES)
        collection.create_item(KEY_LABEL, KEY_ATTRIBUTES, secret, replace=True)
        return secret
    except VaultError:
        raise
    except Exception as e:
        raise VaultError(f"The keyring refused the request: {e}")
    finally:
        connection.close()


# ---------------------------------------------------------------------- #
# Sealing
# ---------------------------------------------------------------------- #
def is_envelope(data) -> bool:
    """True when a parsed `.creds` file is sealed rather than plaintext."""
    return isinstance(data, dict) and ENVELOPE_KEY in data


def seal(mapping: dict) -> dict:
    """Encrypt a name→password map into a JSON-serialisable envelope.

    Returns `mapping` untouched when no backend is available, which is what
    keeps a headless install working exactly as it did before. Callers write
    whatever comes back; they don't need to know which happened.
    """
    which = backend()
    if not which:
        return dict(mapping)

    AESGCM, _HKDF, _hashes = _crypto()
    nonce = _random.token_bytes(NONCE_BYTES)
    plaintext = json.dumps(mapping).encode()
    ciphertext = AESGCM(key()).encrypt(nonce, plaintext, AAD)
    return {
        ENVELOPE_KEY: ENVELOPE_VERSION,
        # Which key opens this. Not a security control — it is what turns
        # "decryption failed" into a sentence naming the actual problem.
        "backend": which,
        "nonce": base64.b64encode(nonce).decode(),
        "data": base64.b64encode(ciphertext).decode(),
    }


def unseal(envelope: dict, allow_prompt: bool = True) -> dict:
    """Recover the name→password map from an envelope written by :func:`seal`."""
    if envelope.get(ENVELOPE_KEY) != ENVELOPE_VERSION:
        raise VaultError(
            f"These saved passwords were written by a newer version of NASync "
            f"(format {envelope.get(ENVELOPE_KEY)!r}).")

    # Checked before anything unpacks it: `_crypto()` returns None where
    # `cryptography` isn't importable, and unpacking that raises TypeError —
    # which escapes every caller's `except VaultError` and takes the app down,
    # the exact outcome treating crypto as optional is meant to avoid.
    if _crypto() is None:
        raise VaultError(
            "These saved passwords are encrypted, but the 'cryptography' "
            "package isn't available to decrypt them.")

    sealed_by = envelope.get("backend", "")
    which = backend()
    if sealed_by and which and sealed_by != which:
        raise ForeignSeal(
            f"These saved passwords were encrypted by a {sealed_by}-based "
            f"install and can't be opened by this {which}-based one.")

    AESGCM, _HKDF, _hashes = _crypto()
    try:
        nonce = base64.b64decode(envelope["nonce"])
        ciphertext = base64.b64decode(envelope["data"])
    except (KeyError, ValueError) as e:
        raise VaultError(f"The saved passwords file is damaged: {e}")

    secret = key(create=False, allow_prompt=allow_prompt)
    try:
        plaintext = AESGCM(secret).decrypt(nonce, ciphertext, AAD)
    except Exception:
        raise VaultError(
            "The saved passwords couldn't be decrypted. The encryption key in "
            "your keyring has changed or been removed; re-enter each server's "
            "password to store them again.")
    try:
        data = json.loads(plaintext)
    except ValueError as e:
        raise VaultError(f"The saved passwords file is damaged: {e}")
    return data if isinstance(data, dict) else {}


def describe(which: str = None) -> str:
    """One line for `nasync status` and the GUI, naming what is actually true."""
    which = backend() if which is None else which
    if which == "portal":
        return "encrypted (key held by the desktop portal)"
    if which == "keyring":
        return "encrypted (key held in your keyring)"
    if os.name == "posix":
        return "not encrypted — no keyring available, file permissions only"
    return "not encrypted"
