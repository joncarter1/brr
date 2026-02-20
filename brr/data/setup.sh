#!/bin/bash
# ============================================================
# brr global setup — edit this file to customize node bootstrap
# Location: ~/.brr/setup.sh
# Bake into AMI: brr bake aws
# ============================================================

set -Eeuo pipefail

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

# Suppress needrestart interactive prompts (runs as dpkg hook under sudo,
# so exported env vars don't reach it)
if [ -d /etc/needrestart/conf.d ]; then
  echo "\$nrconf{restart} = 'a';" | sudo tee /etc/needrestart/conf.d/no-prompt.conf >/dev/null
fi

# Source config if available (copied to remote via file_mounts)
if [ -f "/tmp/brr/config.env" ]; then
  source "/tmp/brr/config.env"
fi

# Defaults (used if config.env not present)
CLUSTER_USER="${CLUSTER_USER:-$USER}"
DOTFILES_REPO="${DOTFILES_REPO:-}"

info() {
  echo "[setup] $*"
}

append_line_once() {
  # Usage: append_line_once "line" "/path/to/file"
  local line="$1"
  local file="$2"
  if [ ! -f "$file" ] || ! grep -Fqx "$line" "$file"; then
    echo "$line" >> "$file"
  fi
}

# --- Base packages ---
if ! dpkg -s nfs-common curl unzip bzip2 make perl >/dev/null 2>&1; then
  info "Installing base packages"
  sudo apt-get update -y
  sudo apt-get install -y nfs-common curl unzip bzip2 make perl
else
  info "Base packages already installed"
fi

# --- GNU Stow (for dotfiles) ---
# Install stow 2.4+ from source (Ubuntu's apt ships 2.3.1 which has a bug
# with --dotfiles on directories)
if ! stow --version 2>&1 | grep -qE '2\.[4-9]|[3-9]\.'; then
  info "Installing GNU Stow 2.4.0 from source"
  _stow_tmp="$(mktemp -d)"
  curl -sSL "https://ftp.gnu.org/gnu/stow/stow-2.4.0.tar.gz" | tar xz -C "$_stow_tmp"
  (cd "$_stow_tmp/stow-2.4.0" && ./configure --quiet && make -s && sudo make -s install)
  rm -rf "$_stow_tmp"
else
  info "stow already at $(stow --version 2>&1 | head -1)"
fi

# --- Filesystem mounts (EFS / virtiofs) ---
if [ "${PROVIDER:-aws}" = "aws" ] && [ -n "${EFS_ID:-}" ]; then
  REGION="${AWS_REGION:-$(curl -s http://169.254.169.254/latest/meta-data/placement/region)}"
  EFS_DNS="${EFS_ID}.efs.${REGION}.amazonaws.com"
  MOUNT_POINT="/efs"

  if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
    info "EFS already mounted at $MOUNT_POINT"
  else
    info "Mounting EFS $EFS_ID at $MOUNT_POINT"
    sudo mkdir -p "$MOUNT_POINT"

    # Retry mount (mount target DNS may take a moment to propagate)
    for i in 1 2 3 4 5; do
      if sudo mount -t nfs4 -o nfsvers=4.1,rsize=1048576,wsize=1048576,soft,timeo=600,retrans=2,noresvport \
        "$EFS_DNS:/" "$MOUNT_POINT" 2>/dev/null; then
        break
      fi
      info "Mount attempt $i failed, retrying in 5s..."
      sleep 5
    done

    if mountpoint -q "$MOUNT_POINT"; then
      sudo chown ubuntu:ubuntu "$MOUNT_POINT"
      info "EFS mounted at $MOUNT_POINT"
    else
      info "WARNING: Failed to mount EFS after 5 attempts"
    fi
  fi

  # Add to fstab for persistence across reboots
  if ! grep -q "$EFS_ID" /etc/fstab 2>/dev/null; then
    echo "$EFS_DNS:/ $MOUNT_POINT nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,soft,timeo=600,retrans=2,noresvport,_netdev 0 0" \
      | sudo tee -a /etc/fstab >/dev/null
  fi

  # Save ray_bootstrap_config.yaml before bind-mount shadows it
  # (Ray writes this during file_mounts, before our setup_commands run)
  if [ -f "$HOME/ray_bootstrap_config.yaml" ] && [ ! -L "$HOME/ray_bootstrap_config.yaml" ]; then
    cp "$HOME/ray_bootstrap_config.yaml" /tmp/ray_bootstrap_config.yaml
  fi

  # Persistent home directory on EFS
  if ! mountpoint -q "$HOME" 2>/dev/null; then
    if [ ! -d "$MOUNT_POINT/home/ubuntu" ]; then
      info "First boot: seeding persistent home on $MOUNT_POINT"
      mkdir -p "$MOUNT_POINT/home/ubuntu"
      rsync -a "$HOME/" "$MOUNT_POINT/home/ubuntu/"
    fi
    sudo mount --bind "$MOUNT_POINT/home/ubuntu" "$HOME"
    info "Home bind-mounted from $MOUNT_POINT"
  fi

  # Persistent code directory
  mkdir -p "$HOME/code"

  # Redirect ray_bootstrap_config.yaml to instance-local storage
  # (Ray hardcodes this to ~/, can't relocate)
  if [ ! -L "$HOME/ray_bootstrap_config.yaml" ]; then
    rm -f "$HOME/ray_bootstrap_config.yaml"
    ln -sfn /tmp/ray_bootstrap_config.yaml "$HOME/ray_bootstrap_config.yaml"
  fi
fi

# --- Shared filesystem mount (Nebius virtiofs) ---
if [ "${PROVIDER:-}" = "nebius" ] && [ -n "${NEBIUS_FILESYSTEM_ID:-}" ]; then
  MOUNT_POINT="/shared"

  if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
    info "Filesystem already mounted at $MOUNT_POINT"
  else
    info "Mounting shared filesystem at $MOUNT_POINT"
    sudo mkdir -p "$MOUNT_POINT"
    sudo mount -t virtiofs brr-shared "$MOUNT_POINT"
    sudo chown ubuntu:ubuntu "$MOUNT_POINT"
    info "Filesystem mounted at $MOUNT_POINT"
  fi

  # Add to fstab for persistence across reboots
  if ! grep -q 'brr-shared' /etc/fstab 2>/dev/null; then
    echo "brr-shared $MOUNT_POINT virtiofs defaults 0 0" | sudo tee -a /etc/fstab >/dev/null
  fi

  # Save ray_bootstrap_config.yaml before bind-mount shadows it
  if [ -f "$HOME/ray_bootstrap_config.yaml" ] && [ ! -L "$HOME/ray_bootstrap_config.yaml" ]; then
    cp "$HOME/ray_bootstrap_config.yaml" /tmp/ray_bootstrap_config.yaml
  fi

  # Persistent home directory on shared filesystem
  if ! mountpoint -q "$HOME" 2>/dev/null; then
    if [ ! -d "$MOUNT_POINT/home/ubuntu" ]; then
      info "First boot: seeding persistent home on $MOUNT_POINT"
      mkdir -p "$MOUNT_POINT/home/ubuntu"
      rsync -a "$HOME/" "$MOUNT_POINT/home/ubuntu/"
    fi
    sudo mount --bind "$MOUNT_POINT/home/ubuntu" "$HOME"
    info "Home bind-mounted from $MOUNT_POINT"
  fi

  # Persistent code directory
  mkdir -p "$HOME/code"

  # Redirect ray_bootstrap_config.yaml to instance-local storage
  if [ ! -L "$HOME/ray_bootstrap_config.yaml" ]; then
    rm -f "$HOME/ray_bootstrap_config.yaml"
    ln -sfn /tmp/ray_bootstrap_config.yaml "$HOME/ray_bootstrap_config.yaml"
  fi
fi

# --- AWS CLI ---
if [ "${PROVIDER:-aws}" = "aws" ]; then
  if ! command -v aws >/dev/null 2>&1; then
    info "Installing AWS CLI v2"
    tmpdir="$(mktemp -d)"
    curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "$tmpdir/awscliv2.zip"
    unzip -q "$tmpdir/awscliv2.zip" -d "$tmpdir"
    sudo "$tmpdir/aws/install" --update
    rm -rf "$tmpdir"
  else
    info "AWS CLI already installed: $(aws --version 2>&1 | head -n1)"
  fi
fi

# --- GitHub SSH access (copied via file_mounts) ---
if [ -f "/tmp/brr/github_key" ]; then
  info "Configuring GitHub SSH access"
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  cp "/tmp/brr/github_key" "$HOME/.ssh/github_key"
  chmod 600 "$HOME/.ssh/github_key"

  if ! grep -q 'Host github.com' "$HOME/.ssh/config" 2>/dev/null; then
    cat >> "$HOME/.ssh/config" <<'SSHCFG'
Host github.com
  IdentityFile ~/.ssh/github_key
  StrictHostKeyChecking accept-new
SSHCFG
    chmod 600 "$HOME/.ssh/config"
  fi
  info "GitHub SSH access configured"
fi

# --- AI coding tools (configured via `brr configure tools`) ---

_ensure_node() {
  if command -v node >/dev/null 2>&1; then
    return
  fi
  info "Installing Node.js (required for Codex/Gemini CLI)"
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
}

if [ "${INSTALL_CLAUDE_CODE:-}" = "true" ]; then
  if ! command -v claude >/dev/null 2>&1; then
    info "Installing Claude Code"
    curl -fsSL https://claude.ai/install.sh | bash
  else
    info "Claude Code already installed"
  fi
fi


if [ "${INSTALL_CODEX:-}" = "true" ]; then
  _ensure_node
  if ! command -v codex >/dev/null 2>&1; then
    info "Installing Codex"
    npm install -g @openai/codex
  else
    info "Codex already installed"
  fi
fi

if [ "${INSTALL_GEMINI:-}" = "true" ]; then
  _ensure_node
  if ! command -v gemini >/dev/null 2>&1; then
    info "Installing Gemini CLI"
    npm install -g @google/gemini-cli
  else
    info "Gemini CLI already installed"
  fi
fi

# --- Dotfiles ---
if [ -n "$DOTFILES_REPO" ]; then
  DOTFILES_DIR="$HOME/dotfiles"
  if [ -d "$DOTFILES_DIR/.git" ]; then
    info "Updating dotfiles repository"
    git -C "$DOTFILES_DIR" fetch --all --quiet || true
    git -C "$DOTFILES_DIR" pull --ff-only --quiet || true
  else
    info "Cloning dotfiles repository"
    git clone "$DOTFILES_REPO" "$DOTFILES_DIR"
  fi
  if [ -x "$DOTFILES_DIR/install.sh" ]; then
    info "Running dotfiles install script"
    # Run without inheriting set -Eeuo pipefail — dotfiles scripts often have
    # commands that return non-zero for idempotency (apt, git clone, etc.)
    (cd "$DOTFILES_DIR" && set +Eeu && ./install.sh) || info "dotfiles install.sh exited non-zero (continuing)"
  fi
else
  info "Skipping dotfiles (DOTFILES_REPO not set)"
fi

# --- Python environment (uv, venv, Ray) ---
if ! command -v uv >/dev/null 2>&1; then
  info "Installing uv package manager"
  curl -LsSf https://astral.sh/uv/install.sh | sh
else
  info "uv already installed: $(uv --version 2>/dev/null || true)"
fi

# Determine uv binary path for immediate use in this shell
UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ] && [ -x "$HOME/.local/bin/uv" ]; then
  UV_BIN="$HOME/.local/bin/uv"
fi
if [ -z "$UV_BIN" ]; then
  UV_BIN="uv"
fi

# Route uv caches to /tmp so EFS flock issues don't hang uv commands.
# The wrapper (installed later) also sets these, but we need them NOW
# for the uv venv/pip commands below.
export UV_CACHE_DIR="/tmp/uv"
export UV_PYTHON_INSTALL_DIR="/tmp/uv/python"

# Ensure a managed Python is available in the redirected install dir
"$UV_BIN" python install

# Create virtual environment at /tmp/brr/venv if absent (instance-local, not in home)
VENVDIR="/tmp/brr/venv"
_WANT_PY="${PYTHON_VERSION:-3.11}"
if [ ! -d "$VENVDIR" ]; then
  info "Creating Python ${_WANT_PY} virtual environment at $VENVDIR"
  "$UV_BIN" venv --python "$_WANT_PY" "$VENVDIR"
else
  info "Virtual environment already exists at $VENVDIR"
fi

# Install Ray in the virtual environment if missing
if ! "$VENVDIR/bin/python" -c "import ray" >/dev/null 2>&1; then
  info "Installing Ray into the virtual environment"
  ( . "$VENVDIR/bin/activate" && "$UV_BIN" pip install 'ray[default]' )
else
  info "Ray already installed in the virtual environment"
fi

# Install boto3 in the venv (required by Ray's built-in AWS provider)
if [ "${PROVIDER:-aws}" = "aws" ]; then
  if ! "$VENVDIR/bin/python" -c "import boto3" >/dev/null 2>&1; then
    info "Installing boto3 into the virtual environment"
    ( . "$VENVDIR/bin/activate" && "$UV_BIN" pip install 'boto3>=1.4.8' )
  else
    info "boto3 already installed in the virtual environment"
  fi
fi

# Install pip into the venv (uv venv doesn't include it) and symlink so
# Ray's auto-injected "pip install" commands use the venv pip, not the
# PEP 668-blocked system pip.
if [ ! -x "$VENVDIR/bin/pip" ]; then
  info "Installing pip into the virtual environment"
  ( . "$VENVDIR/bin/activate" && "$UV_BIN" pip install pip )
fi
sudo ln -sf "$VENVDIR/bin/pip" /usr/local/bin/pip
sudo ln -sf "$VENVDIR/bin/pip3" /usr/local/bin/pip3

# --- Nebius provider support ---
if [ "${PROVIDER:-}" = "nebius" ]; then
  # Install Nebius SDK so the node provider can create workers
  if ! "$VENVDIR/bin/python" -c "import nebius" >/dev/null 2>&1; then
    info "Installing Nebius SDK into the virtual environment"
    ( . "$VENVDIR/bin/activate" && "$UV_BIN" pip install nebius )
  else
    info "Nebius SDK already installed in the virtual environment"
  fi

  # Make brr.nebius.node_provider importable via .pth file
  for sp in "$VENVDIR"/lib/python3.*/site-packages; do
    echo "/tmp/brr/provider_lib" > "$sp/brr_provider.pth"
  done

  # Place credentials where the SDK expects them
  if [ -f "/tmp/brr/nebius_credentials.json" ]; then
    mkdir -p "$HOME/.nebius"
    cp "/tmp/brr/nebius_credentials.json" "$HOME/.nebius/credentials.json"
    info "Nebius credentials configured"
  fi
fi

# --- Shell environment ---
# Two parts:
#   1. In-place wrapper at ~/.local/bin/uv (replaces real binary, which is
#      moved to ~/.local/lib/uv). Routes project venvs to /tmp to avoid EFS IO.
#      Works regardless of shell config, PATH order, or dotfiles.
#   2. /etc/profile.d/brr.sh for environment variables (PATH, UV_CACHE_DIR).

UV_DIR="$HOME/.local/bin"
UV_REAL="$HOME/.local/lib/uv"
UV_LINK="$UV_DIR/uv"

# Move real uv binary out of the way (skip if already wrapped)
mkdir -p "$(dirname "$UV_REAL")"
if [ -x "$UV_LINK" ] && [ ! -L "$UV_LINK" ] && ! grep -q 'local/lib/uv' "$UV_LINK" 2>/dev/null; then
  mv "$UV_LINK" "$UV_REAL"
fi
# Migration: move .uv-real to new location
if [ -x "$UV_DIR/.uv-real" ]; then
  mv "$UV_DIR/.uv-real" "$UV_REAL"
fi

# uv wrapper — sets UV_PROJECT_ENVIRONMENT per-project, then execs real uv
cat > "$UV_LINK" <<'UVWRAP'
#!/bin/bash
export UV_CACHE_DIR="/tmp/uv"
export UV_PYTHON_INSTALL_DIR="/tmp/uv/python"
_repo_root=$(timeout 3 git rev-parse --show-toplevel 2>/dev/null)
if [ -n "$_repo_root" ]; then
  export UV_PROJECT_ENVIRONMENT="/tmp/venvs/$(basename "$_repo_root")"
  mkdir -p "$UV_PROJECT_ENVIRONMENT"
fi
exec "$HOME/.local/lib/uv" "$@"
UVWRAP
chmod +x "$UV_LINK"

# python/python3 wrappers in ~/.local/bin
for cmd in python python3; do
  cat > "$UV_DIR/$cmd" <<'PYWRAP'
#!/bin/bash
exec "$HOME/.local/bin/uv" run CMDNAME "$@"
PYWRAP
  sed -i "s/CMDNAME/$cmd/" "$UV_DIR/$cmd"
  chmod +x "$UV_DIR/$cmd"
done

# Symlink system tools from base venv into ~/.local/bin so they're available
# without putting all of /tmp/brr/venv/bin on PATH (which would shadow our wrappers).
for tool in ray pip; do
  [ -x "$VENVDIR/bin/$tool" ] && ln -sf "$VENVDIR/bin/$tool" "$UV_DIR/$tool"
done
info "Installed uv/python wrappers in $UV_DIR"

# Shell environment — env vars (sourced by login/interactive shells)
sudo tee /etc/profile.d/brr.sh >/dev/null <<'BRRSH'
# brr shell environment
[ -n "$_BRR_ENV_LOADED" ] && return
export _BRR_ENV_LOADED=1

export UV_CACHE_DIR="/tmp/uv"
export UV_PYTHON_INSTALL_DIR="/tmp/uv/python"
BRRSH
# Also source from /etc/bash.bashrc for non-login bash shells (tmux)
if ! grep -q 'profile.d/brr.sh' /etc/bash.bashrc 2>/dev/null; then
  echo '[ -f /etc/profile.d/brr.sh ] && . /etc/profile.d/brr.sh' | sudo tee -a /etc/bash.bashrc >/dev/null
fi
info "Installed /etc/profile.d/brr.sh"

# --- Configure sshd to detect dead connections ---
info "Configuring sshd ClientAlive settings"
sudo tee /etc/ssh/sshd_config.d/brr-keepalive.conf >/dev/null <<'SSHD'
ClientAliveInterval 60
ClientAliveCountMax 3
SSHD
sudo systemctl restart sshd 2>/dev/null || sudo systemctl restart ssh

# --- Idle shutdown daemon ---
if [ "${IDLE_SHUTDOWN_ENABLED}" = "true" ]; then
  info "Installing idle-shutdown daemon"

  sudo cp "/tmp/brr/idle-shutdown.sh" /usr/local/bin/idle-shutdown
  sudo chmod 755 /usr/local/bin/idle-shutdown

  sudo tee /etc/systemd/system/idle-shutdown.service >/dev/null <<UNIT
[Unit]
Description=Idle auto-shutdown daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/idle-shutdown
Environment="IDLE_SHUTDOWN_TIMEOUT_MIN=${IDLE_SHUTDOWN_TIMEOUT_MIN}"
Environment="IDLE_SHUTDOWN_GRACE_MIN=${IDLE_SHUTDOWN_GRACE_MIN}"
Environment="IDLE_SHUTDOWN_CPU_THRESHOLD=${IDLE_SHUTDOWN_CPU_THRESHOLD}"
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=idle-shutdown

[Install]
WantedBy=multi-user.target
UNIT

  sudo systemctl daemon-reload
  sudo systemctl enable idle-shutdown.service
  sudo systemctl restart idle-shutdown.service
  info "idle-shutdown daemon installed and started"
else
  if systemctl is-active idle-shutdown.service >/dev/null 2>&1; then
    info "Disabling idle-shutdown daemon (IDLE_SHUTDOWN_ENABLED=false)"
    sudo systemctl stop idle-shutdown.service
    sudo systemctl disable idle-shutdown.service
  else
    info "Idle-shutdown daemon disabled"
  fi
fi

# GPU detection (informational)
if command -v nvidia-smi >/dev/null 2>&1; then
  info "GPU detected:"
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || true
else
  info "No GPU detected (nvidia-smi not found)"
fi
