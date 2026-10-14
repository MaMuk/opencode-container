#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v python3 >/dev/null || { echo 'Install Python 3 on the host first.' >&2; exit 1; }
command -v podman >/dev/null || { echo 'Install Podman on the host first.' >&2; exit 1; }
config_dir="$HOME/.config/opencode-chat"
umask 077
mkdir -p "$config_dir" "$HOME/.local/bin"
if [ ! -e "$config_dir/.env" ]; then
    cp "$project_dir/.env.example" "$config_dir/.env"
fi
chmod 600 "$config_dir/.env"
destination="$HOME/.local/bin/opencode-chat"
if [ -e "$destination" ] || [ -L "$destination" ]; then
    if [ "$(readlink -f -- "$destination")" != "$project_dir/opencode-chat" ]; then
        echo "Refusing to replace an unrelated file: $destination" >&2
        exit 1
    fi
else
    ln -s "$project_dir/opencode-chat" "$destination"
fi
"$project_dir/opencode-chat" --build
printf '\nInstalled %s\nConfiguration: %s/.env\n' "$destination" "$config_dir"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) printf 'Add this to your shell configuration: export PATH="$HOME/.local/bin:$PATH"\n' ;;
esac
