#!/bin/bash
# Project setup — runs after global setup on every node boot.
set -Eeuo pipefail

# Sync project dependencies (uses locked versions from uv.lock)
if [ -d "$HOME/code/brr" ]; then
  cd "$HOME/code/brr"
  uv sync
fi
