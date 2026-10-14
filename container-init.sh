#!/bin/bash
set -euo pipefail
export HOME=/home/agent USER=agent LOGNAME=agent SHELL=/bin/bash
export XDG_CONFIG_HOME=/home/agent/.config XDG_DATA_HOME=/home/agent/.local/share
export XDG_CACHE_HOME=/home/agent/.cache
export OPENCODE_DISABLE_AUTOUPDATE=true OPENCODE_DISABLE_MODELS_FETCH=true

if [[ -n ${OPENCODE_TEMP_PACKAGES:-} ]]; then
    read -r -a packages <<< "$OPENCODE_TEMP_PACKAGES"
    for package in "${packages[@]}"; do
        [[ $package =~ ^[a-z0-9][a-z0-9+.-]+$ ]] || {
            echo 'Invalid temporary Debian package name.' >&2; exit 2;
        }
    done
    if ! apt-get update || ! DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"; then
        echo 'Temporary package installation failed; OpenCode was not started.' >&2
        exit 1
    fi
    rm -rf /var/lib/apt/lists/*
fi

mkdir -p "$HOME/.config/opencode" "$XDG_DATA_HOME" "$XDG_CACHE_HOME"
# /workspace can be a bind mount. Never chown or chmod a mounted project.
if ! mountpoint -q /workspace; then
    chown 1000:1000 /workspace
fi
python3 /usr/local/lib/opencode-chat/generate-config.py
chown -R 1000:1000 /home/agent
cd /workspace
if [[ ${1:-} == __opencode_chat_exec__ ]]; then
    shift
    exec setpriv --reuid=1000 --regid=1000 --clear-groups --no-new-privs --bounding-set=-all "$@"
fi
exec setpriv --reuid=1000 --regid=1000 --clear-groups --no-new-privs --bounding-set=-all opencode "$@"
