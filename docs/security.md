# Security

## Encrypting the connection

By default NASync signs its traffic but does not encrypt it: signing proves
nobody tampered with your files in transit, encryption is what stops anyone
reading them. Turn encryption on per server:

```bash
nasync encryption on              # the active server
nasync encryption on --server nas # a specific one
nasync encryption                 # show the current setting
nasync test-connection            # check it before the next scheduled run
```

There is a switch in the desktop app's server editor too.

| | |
|:--|:--|
| **What it protects** | The contents of your files on the network, with AES-GCM |
| **What it needs** | SMB 3 at both ends — most NAS boxes made since ~2014 |
| **What it costs** | Throughput. On a NAS with no hardware AES it can halve transfer speed |
| **Default** | **Off**, so that upgrading NASync never breaks a working setup |

**If your server can't encrypt, NASync refuses to connect** rather than falling
back to sending your files in the clear. A security setting that silently
degrades reads as protection that isn't there. You'll get a message saying
the server needs SMB 3 and how to carry on without encryption.

Worth turning on if you sync over Wi-Fi, on a network you share with people
you don't administer, or over a VPN link you don't control. Less obviously
worth it on a wired LAN in your own home, where the cost is real and the
threat is not.

## How your NAS passwords are stored

Passwords are never written to `config.json`. They live in `.creds`, which is
**encrypted with AES-GCM** under a key NASync does not store itself:

| Where NASync is installed | Where the key lives |
|:--|:--|
| Flatpak | Held for the app by `org.freedesktop.portal.Secret` |
| pip / pipx / source | One item in your login keyring, shown in Seahorse as *NASync credential key* |
| No keyring or portal at all | **Nowhere — the file stays plaintext at mode 0600** |

That last row is the honest case, not a bug: a headless box or an SSH session
with no session bus has nowhere to keep a key. NASync keeps working there, and
`nasync status` says which of the three you have rather than implying a
protection you don't:

```bash
nasync status | grep "Saved passwords"
#   Saved passwords: encrypted (key held in your keyring)
```

An existing plaintext `.creds` is encrypted in place the next time NASync
starts; no password needs re-entering. `nasync credentials` shows how saved
passwords are protected, and `nasync credentials --reset` discards them.

If your keyring is locked when NASync starts, it says so, instead of
connecting without a password and letting the NAS report a login failure.

## Your password only goes to your NAS

NASync remembers which machine the NAS is the first time it connects, and
won't send credentials to a different one at the same address — on a strange
network that happens to use your home's addresses, say. After replacing the
NAS:

```bash
nasync trust
```

For Samba servers this identity is only the host name; see
[Data safety](data-safety.md#accepted-limitations).

## Scripts and cron

For scripted use, the password can come from `SAMBA_PASSWORD` instead of the
saved one:

```bash
export SAMBA_PASSWORD=$(cat /path/to/secret/file)   # a file only you can read
nasync sync
```

## Where NASync keeps its settings

| File | What it holds |
|:--|:--|
| `~/.config/nasync/config.json` | Servers, sync pairs, modes and triggers — no passwords |
| `~/.config/nasync/.creds` | Saved passwords, encrypted as above (mode 0600) |
| `~/.config/nasync/sync-state.db` | The record of what was uploaded (see [How syncing works](syncing.md#what-a-sync-does)) |
| `~/.local/state/nasync/gui.log` | Output of the last launch from the app menu |

A Flatpak keeps the same files under `~/.var/app/io.github.YuvalBogo.NASync/`.
Change settings through `nasync` commands or the app rather than by editing
`config.json`.
