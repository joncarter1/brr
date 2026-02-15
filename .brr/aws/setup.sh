#!/bin/bash
# Project setup — runs after global setup on every node boot.
set -Eeuo pipefail

# Sync project dependencies (uses locked versions from uv.lock)
if [ -d "$HOME/code/brr" ]; then
  cd "$HOME/code/brr"
  # Pre-fetch the Python version required by the project so uv sync doesn't hang.
  uv python install
  uv sync --group brr
fi

# Add extra project-specific dependencies below:
# uv pip install torch
