#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Install the loopback browser IDE service. Desktop replacement is explicit.
set -euo pipefail
ROOT="$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")"
OWNER="${SUDO_USER:-$(id -un)}"
GROUP="$(id -gn "$OWNER")"
HOME_DIR="$(getent passwd "$OWNER" | cut -d: -f6)"
MODE=start
case "${1:-}" in
  '') ;;
  --install-only) MODE=install ;;
  --replace-desktop) MODE=replace ;;
  *) echo "Usage: $0 [--install-only|--replace-desktop]" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { echo "Too many arguments" >&2; exit 2; }
if [[ "$OWNER" == root ]]; then
  echo 'Run as the VM user (sudo is used only for packages/unit installation).' >&2
  exit 1
fi
for f in dist/bin/erbium_emu build/smallvm/smallvm.elf \
  smallvm/web/server.py smallvm/web/index.html smallvm/web/erbium.js \
  build/smallvm/web/gp_wasm.js build/smallvm/web/gp_wasm.wasm \
  build/smallvm/web/gp_wasm.data build/smallvm/web/provenance.json \
  build/smallvm/web/SHA256SUMS; do
  test -s "$ROOT/$f" || { echo "Missing $ROOT/$f; build firmware/browser assets first." >&2; exit 1; }
done
test -x "$ROOT/dist/bin/erbium_emu" || { echo "Emulator is not executable" >&2; exit 1; }
/usr/bin/python3 - "$ROOT/build/smallvm/web/gp_wasm.wasm" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
if p.stat().st_size < 100000 or p.read_bytes()[:8] != b'\0asm\1\0\0\0':
    raise SystemExit("Missing real GP WASM interpreter; rerun scripts/fetch-smallvm-web.sh")
PY
(cd "$ROOT/build/smallvm/web"; sha256sum --check --quiet SHA256SUMS)

port_busy() { ss -Hltn 'sport = :8000' | grep -q .; }
port_owned_by() {
  local unit="$1" pids pid
  pids="$(sudo ss -Hltnp 'sport = :8000' | grep -oE 'pid=[0-9]+' | cut -d= -f2)"
  [[ -n "$pids" ]] || return 1
  for pid in $pids; do
    grep -Fq "/$unit" "/proc/$pid/cgroup" || return 1
  done
}
if [[ "$MODE" != install ]] && port_busy; then
  if systemctl is-active --quiet smallvm-web.service && port_owned_by smallvm-web.service; then
    : # Restart our own service, not a process belonging to someone else.
  elif [[ "$MODE" == replace ]] && systemctl is-active --quiet smallvm-ide.service \
       && port_owned_by smallvm-ide.service; then
    : # Do not stop it until dependencies and the unit are ready below.
  else
    echo 'Port 8000 is occupied. Refusing to overwrite its owner.' >&2
    echo 'Test the browser IDE on 8001 first; --replace-desktop explicitly migrates smallvm-ide.' >&2
    exit 1
  fi
fi

sudo apt-get update -qq
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3-aiohttp curl iproute2 libstdc++6 libgcc-s1 libc6
/usr/bin/python3 -c 'import aiohttp; from aiohttp import web; assert hasattr(web, "AppKey")'
sudo tee /etc/systemd/system/smallvm-web.service >/dev/null <<EOF
[Unit]
Description=Self-hosted MicroBlocks browser IDE + real Erbium UART
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=10

[Service]
Type=simple
User=$OWNER
Group=$GROUP
Environment=HOME=$HOME_DIR
WorkingDirectory=$ROOT
StateDirectory=smallvm-web
StateDirectoryMode=0700
ExecStart=/usr/bin/python3 $ROOT/smallvm/web/server.py --repo $ROOT --port 8000 --logs /var/lib/smallvm-web/current
Restart=on-failure
RestartSec=3
KillMode=control-group
TimeoutStopSec=20
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
if [[ "$MODE" == install ]]; then
  echo 'Installed smallvm-web.service; not enabled/started. Existing desktop is untouched.'
  echo 'After isolated real-browser tests, run this script with --replace-desktop.'
  exit 0
fi

RESTORE_DESKTOP=false
if [[ "$MODE" == replace ]] && systemctl is-active --quiet smallvm-ide.service; then
  RESTORE_DESKTOP=true
  sudo systemctl stop smallvm-ide.service
fi
rollback() {
  echo 'Browser service failed health checks; see journalctl -u smallvm-web.' >&2
  sudo systemctl disable --now smallvm-web.service || true
  if "$RESTORE_DESKTOP"; then sudo systemctl start smallvm-ide.service; fi
}
if port_busy && ! port_owned_by smallvm-web.service; then rollback; exit 1; fi
if ! sudo systemctl enable smallvm-web.service || ! sudo systemctl restart smallvm-web.service; then
  rollback
  exit 1
fi
READY=false
for _ in {1..100}; do
  if curl --fail --silent --max-time 1 http://127.0.0.1:8000/api/status |
    /usr/bin/python3 -c 'import json,sys; assert json.load(sys.stdin)["emulator_running"]' 2>/dev/null; then
    READY=true
    break
  fi
  sleep .1
done
if ! "$READY" || ! curl --fail --silent --max-time 5 http://127.0.0.1:8000/ >/dev/null \
  || ! curl --fail --silent --max-time 5 --range 0-7 \
    http://127.0.0.1:8000/assets/gp_wasm.wasm >/dev/null; then
  rollback
  exit 1
fi
if "$RESTORE_DESKTOP"; then sudo systemctl disable smallvm-ide.service; fi
echo 'Browser IDE: port 8000, loopback only, behind your PRIVATE authenticated exe.dev proxy.'
echo 'Do not make that proxy public. No VNC or desktop process is used by this service.'
echo 'Service: smallvm-web.service; logs: /var/lib/smallvm-web/current/'
