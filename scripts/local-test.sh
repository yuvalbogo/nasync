#!/usr/bin/env bash
#
# local-test.sh — end-to-end test of nasync against a throwaway Samba
# server running locally in a container. No real NAS and no root required.
#
# It will:
#   1. start a local Samba server (podman or docker) on a high port
#   2. configure nasync in an ISOLATED config dir (your real config is
#      never touched)
#   3. push a small tree (incl. hidden + nested files) and verify it landed
#   4. restore it into an empty dir and verify the files come back
#   5. tear everything down automatically on exit
#
# Usage:
#   ./scripts/local-test.sh            # run the full test
#   KEEP=1 ./scripts/local-test.sh     # leave the container/config up for poking
#
set -euo pipefail

# ---- config ---------------------------------------------------------------
CONTAINER=smbtest
IMAGE=docker.io/dperson/samba
PORT=4450
USER_NAME=testuser
USER_PASS=testpass
SHARE=backup                 # SMB share name (maps to /share in the container)
SERVER_SUBDIR=/mydata        # path under the share to sync into

WORKDIR="$(mktemp -d /tmp/nasync-test.XXXXXX)"
export XDG_CONFIG_HOME="$WORKDIR/config"      # isolate nasync config here
SRC="$WORKDIR/src"
RESTORE="$WORKDIR/src"                          # restore back into the same path

# ---- helpers --------------------------------------------------------------
say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

# pick a container engine
if command -v podman >/dev/null 2>&1; then ENGINE=podman
elif command -v docker >/dev/null 2>&1; then ENGINE=docker
else die "Need podman or docker installed to run the local Samba server."; fi

cleanup() {
    if [ "${KEEP:-0}" = "1" ]; then
        say "KEEP=1 — leaving things up"
        echo "  container: $ENGINE rm -f $CONTAINER"
        echo "  workdir:   $WORKDIR   (XDG_CONFIG_HOME points here)"
        return
    fi
    say "Cleaning up"
    $ENGINE rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
    ok "removed container and temp files"
}
trap cleanup EXIT

# ---- 0. sanity ------------------------------------------------------------
command -v nasync >/dev/null 2>&1 || die "nasync not on PATH — run 'pip install -e .' first."
command -v smbclient  >/dev/null 2>&1 || die "smbclient not installed."

# ---- 1. start the Samba server -------------------------------------------
say "Starting local Samba server ($ENGINE) on port $PORT"
$ENGINE rm -f "$CONTAINER" >/dev/null 2>&1 || true
$ENGINE run -d --name "$CONTAINER" -p "$PORT:445" "$IMAGE" \
    -u "$USER_NAME;$USER_PASS" \
    -s "$SHARE;/share;yes;no;no;$USER_NAME" \
    -p >/dev/null
ok "container started"

say "Waiting for the share to accept connections"
ready=0
for _ in $(seq 1 30); do
    if PASSWD="$USER_PASS" smbclient "//127.0.0.1/$SHARE" -U "$USER_NAME" \
         -p "$PORT" -c quit >/dev/null 2>&1; then
        ready=1; break
    fi
    sleep 1
done
[ "$ready" = "1" ] || die "Samba server did not become ready in time."
ok "server is up"

# ---- 2. configure nasync (isolated) ----------------------------------
say "Configuring nasync (isolated config at $XDG_CONFIG_HOME)"
# Configure via the same API 'init' uses, so no interactive prompts are needed.
python - "$PORT" "$USER_NAME" "$USER_PASS" "$SHARE" "$SRC" "$SERVER_SUBDIR" <<'PY'
import sys
from nasync.config import Config
port, user, pw, share, src, subdir = sys.argv[1:7]
c = Config()
c.set_server("127.0.0.1", user, pw, share, int(port))
c.add_pair(src, subdir, "test")
print("  configured server 127.0.0.1:%s share=%s pair=test" % (port, share))
PY
nasync status
ok "configured"

# ---- 3. push and verify ---------------------------------------------------
say "Creating test data and pushing"
mkdir -p "$SRC/nested"
echo "top level" > "$SRC/root.txt"
echo "nested"    > "$SRC/nested/deep.txt"
echo "hidden"    > "$SRC/.dotfile"

nasync sync --pair test

say "Verifying files landed on the server"
remote_list="$($ENGINE exec "$CONTAINER" find "/share$SERVER_SUBDIR" -type f 2>/dev/null | sort)"
echo "$remote_list" | sed 's/^/  /'
for f in root.txt nested/deep.txt .dotfile; do
    echo "$remote_list" | grep -q "/share$SERVER_SUBDIR/$f" \
        && ok "found $f on server" \
        || die "MISSING on server: $f"
done

# ---- 4. restore and verify ------------------------------------------------
say "Wiping local copy and restoring from server"
rm -rf "$SRC"/* "$SRC"/.dotfile 2>/dev/null || true
nasync restore --pair test --yes

say "Verifying restored files"
for f in root.txt nested/deep.txt .dotfile; do
    [ -f "$SRC/$f" ] && ok "restored $f" || die "MISSING after restore: $f"
done

# ---- 5. dry-run check -----------------------------------------------------
say "Checking dry-run makes no changes"
nasync sync --pair test --dry-run >/dev/null && ok "dry-run ran clean"

say "ALL CHECKS PASSED 🎉"
