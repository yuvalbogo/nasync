# Command reference

Every command takes `--help` for its full options: `nasync sync --help`.
Commands that act on a server use the active one unless given
`--server NAME`.

## Setup

| Command | Description |
|---------|-------------|
| `nasync setup` | Guided first-time setup: server, share, sync root and pairs |
| `nasync init` | Add a server, step by step |
| `nasync servers` | List saved servers (the active one is marked `*`) |
| `nasync use <name>` | Switch the active server |
| `nasync test-connection` | Check the server is reachable, credentials work, and the share exists |
| `nasync root [PATH]` | Show or set a server's [sync root](syncing.md#the-sync-root) |
| `nasync encryption [on\|off]` | Show or set [SMB 3 encryption](security.md#encrypting-the-connection) |
| `nasync credentials [--reset]` | Show how [saved passwords](security.md#how-your-nas-passwords-are-stored) are protected, or discard them |
| `nasync trust` | [Trust the NAS](security.md#your-password-only-goes-to-your-nas) now at a server's address, after replacing it |

## Folders

| Command | Description |
|---------|-------------|
| `nasync add <local> <server>` | Pair a local folder with a folder on the server |
| `nasync list [--all]` | Show a server's pairs, and where each resolves on the NAS |
| `nasync remove <name>` | Remove a pair (nothing on the server is deleted) |

## Syncing

| Command | Description |
|---------|-------------|
| `nasync sync [--pair NAME] [--dry-run] [--mode mirror\|update] [--allow-mass-delete]` | Send changes to the server — see [How syncing works](syncing.md) |
| `nasync restore [--pair NAME]` | [Restore](syncing.md#restore) from the server (never deletes locally) |
| `nasync mode [mirror\|update]` | Show or set a server's [sync mode](syncing.md#sync-mode) |
| `nasync status` | Settings, where each pair resolves, and any sync in progress |
| `nasync cancel` | Stop the running sync, wherever it was started |

## Automatic syncing

| Command | Description |
|---------|-------------|
| `nasync live-sync [on\|off]` | [Live sync](automation.md#live-sync) — sync moments after a file changes |
| `nasync scheduled-sync [on\|off] [--at HH:MM]` | [Scheduled sync](automation.md#scheduled-sync) — once a day |
| `nasync service [status\|on\|off\|restart]` | Manage the [background service](automation.md#the-background-service) |
| `nasync notifications [on\|off] [--test]` | [Desktop notifications](automation.md#notifications) for automatic syncs |
| `nasync auto [--status]` | Run automatic syncing in the foreground |
| `nasync watch [--pair NAME]` | Sync on changes until Ctrl+C, without saving settings |

## Looking at the server

| Command | Description |
|---------|-------------|
| `nasync ls [PATH]` | [Browse a directory](server-tools.md#browsing-the-server) on the server |
| `nasync server-info` | [What the server is](server-tools.md#what-your-server-is) and what was negotiated |
| `nasync usage [PATH]` | [What's using space](server-tools.md#whats-using-space) |
| `nasync duplicates [PATH] [--verify]` | [Files stored more than once](server-tools.md#duplicates) |

## Desktop

| Command | Description |
|---------|-------------|
| `nasync gui` | Open the [desktop app](desktop-app.md) |
| `nasync install-desktop [--uninstall]` | Add or remove the [app menu launcher](install.md#add-it-to-your-app-menu) |
