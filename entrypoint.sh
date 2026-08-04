#!/bin/sh
set -e

REPO_URL="${GITHUB_REPO_URL:-https://github.com/LemurTech22/guacamole-test-repo.git}"
TARGET_DIR="/home/workshop-repo"

if [ ! -d "$TARGET_DIR" ]; then
    echo "Cloning workshop repo: $REPO_URL"
    git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
    chown -R caiworkshopstest:caiworkshopstest "$TARGET_DIR"
else
    echo "Workshop repo already present at $TARGET_DIR, skipping clone."
fi

mkdir -p /var/run/dbus
dbus-daemon --system --fork
xrdp-sesman
exec xrdp --nodaemon
