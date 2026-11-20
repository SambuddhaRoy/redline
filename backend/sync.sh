#!/usr/bin/env bash
# Copies the backend from the Windows checkout ($1, a /mnt/c path) to /opt/peregrine/app.
# bootstrap.ps1 runs this straight from /mnt/c: piping scripts from PowerShell 5.1 adds a BOM and CRs.
set -euo pipefail
rm -rf /opt/peregrine/app.new
mkdir -p /opt/peregrine
cp -r "$1" /opt/peregrine/app.new
find /opt/peregrine/app.new -name '*.sh' -exec sed -i 's/\r$//' {} +
find /opt/peregrine/app.new -name __pycache__ -prune -exec rm -rf {} +
chmod +x /opt/peregrine/app.new/*.sh
rm -rf /opt/peregrine/app
mv /opt/peregrine/app.new /opt/peregrine/app
test -f /opt/peregrine/app/setup.sh
