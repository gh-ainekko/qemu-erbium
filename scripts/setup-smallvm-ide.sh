#!/usr/bin/env bash
# Install the dedicated native IDE service. No changes to upstream SmallVM.
set -euo pipefail
ROOT="$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")"
OWNER="${SUDO_USER:-$(id -un)}"
GROUP="$(id -gn "$OWNER")"
HOME_DIR="$(getent passwd "$OWNER" | cut -d: -f6)"
if [[ "$OWNER" == root ]]; then
  echo 'Run as the VM user (sudo is used only to install packages/unit).' >&2
  exit 1
fi
for f in ext/smallvm/gp/gp-linux64bit build/smallvm/smallvm.elf dist/bin/erbium_emu; do
  test -f "$ROOT/$f" || { echo "Missing $ROOT/$f; build SmallVM first" >&2; exit 1; }
done
if ! systemctl is-active --quiet smallvm-ide.service; then
  for port in 8000 5900; do
    if ss -Hltn "sport = :$port" | grep -q .; then
      echo "Port $port already belongs to another service; refusing to overwrite it." >&2
      exit 1
    fi
  done
  test ! -S /tmp/.X11-unix/X87 || { echo 'X display :87 is already in use' >&2; exit 1; }
fi
sudo apt-get update -qq
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y \
  xvfb x11vnc novnc websockify xdotool openbox fonts-dejavu-core x11-utils xauth
sudo tee /etc/systemd/system/smallvm-ide.service >/dev/null <<EOF
[Unit]
Description=Native MicroBlocks IDE + Erbium emulator (authenticated noVNC)
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=10

[Service]
Type=simple
User=$OWNER
Group=$GROUP
Environment=HOME=$HOME_DIR
WorkingDirectory=$ROOT
RuntimeDirectory=smallvm-ide
RuntimeDirectoryMode=0700
StateDirectory=smallvm-ide
StateDirectoryMode=0700
ExecStart=/usr/bin/python3 $ROOT/smallvm/ide/supervisor.py --repo $ROOT
Restart=on-failure
RestartSec=3
KillMode=control-group
TimeoutStopSec=25
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now smallvm-ide.service
echo 'Native IDE: https://erbium-qemu.exe.xyz/'
echo "Service: smallvm-ide.service; logs: $ROOT/build/smallvm-ide/current/"
