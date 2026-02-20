#!/bin/bash
# Clone a project repo on the cluster using metadata from repo_info.json.
# Written to /tmp/brr/ by brr's inject_brr_infra, runs as a setup_command.
# No-ops if repo_info.json is absent (non-git projects or re-deploys).

set -euo pipefail

REPO_INFO="/tmp/brr/repo_info.json"
if [ ! -f "$REPO_INFO" ]; then
    exit 0
fi

REMOTE_URL=$(python3 -c "import json; print(json.load(open('$REPO_INFO'))['remote_url'])")
BRANCH=$(python3 -c "import json; print(json.load(open('$REPO_INFO'))['branch'])")
COMMIT=$(python3 -c "import json; print(json.load(open('$REPO_INFO'))['commit'])")
REPO_NAME=$(python3 -c "import json; print(json.load(open('$REPO_INFO'))['repo_name'])")

PROJECT_DIR="$HOME/code/$REPO_NAME"

if [ -d "$PROJECT_DIR/.git" ]; then
    # Already cloned (shared storage or worker joining existing cluster)
    echo "[sync-repo] $PROJECT_DIR already exists, fetching latest"
    cd "$PROJECT_DIR"
    git fetch origin
    git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
    git reset --hard "$COMMIT"
else
    # First deploy — clone
    echo "[sync-repo] Cloning $REMOTE_URL ($BRANCH) → $PROJECT_DIR"
    mkdir -p "$HOME/code"
    git clone --branch "$BRANCH" "$REMOTE_URL" "$PROJECT_DIR"
    cd "$PROJECT_DIR"
    git reset --hard "$COMMIT"
fi

# Sync uv project environment if applicable
if [ -f "$PROJECT_DIR/pyproject.toml" ] && [ -f "$PROJECT_DIR/uv.lock" ]; then
    echo "[sync-repo] Syncing uv project environment (brr group)"
    cd "$PROJECT_DIR"
    uv sync --group brr
fi

echo "[sync-repo] Done: $(git log --oneline -1)"
