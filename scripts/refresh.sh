#!/usr/bin/env bash
# refresh.sh — sync local repo and app with GitHub
# Usage: ./scripts/refresh.sh [branch]
# Default branch: main
#
# WARNING: This script discards local unpushed commits. Use with care!

set -euo pipefail

BRANCH="${1:-main}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${REPO_ROOT}/.venv"

echo "🔄 Syncing NASync repo + app with GitHub..."
echo ""

# Step 0: Check for unpushed commits
cd "$REPO_ROOT"
UNPUSHED=$(git log "origin/$BRANCH..HEAD" --oneline 2>/dev/null | wc -l)
if [ "$UNPUSHED" -gt 0 ]; then
    echo "⚠️  WARNING: You have $UNPUSHED unpushed commit(s) on this branch!"
    echo ""
    echo "These will be DISCARDED:"
    git log "origin/$BRANCH..HEAD" --oneline
    echo ""
    read -p "Continue? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "❌ Aborted. Your commits are safe."
        exit 1
    fi
fi

# Step 1: Fetch latest from GitHub
echo "📡 Fetching latest from origin/$BRANCH..."
git fetch origin "$BRANCH"

# Step 2: Reset local branch to match remote
echo "🔀 Resetting local branch to origin/$BRANCH..."
git reset --hard "origin/$BRANCH"

# Step 3: Reinstall app from updated code
if [ -d "$VENV" ]; then
    echo "📦 Reinstalling app from updated code..."
    "$VENV/bin/pip" install -e "$REPO_ROOT" -q
else
    echo "⚠️  No virtualenv found at $VENV"
    echo "   Skipping app reinstall. Create one with: python -m venv $VENV && $VENV/bin/pip install -e ."
fi

# Step 4: Verify version
echo ""
echo "✅ Sync complete. Current version:"
if [ -x "$VENV/bin/nasync" ]; then
    "$VENV/bin/nasync" --version
else
    python3 -c "import sys; sys.path.insert(0, '$REPO_ROOT'); from nasync import __version__; print(f'v{__version__}')"
fi
