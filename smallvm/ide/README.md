# Native MicroBlocks IDE on the VM

**Historical alternative, not the deployed service.** The live port 8000 now
hosts the actual browser IDE described in `../web/README.md`;
`smallvm-ide.service` is stopped and disabled. The following instructions
describe the optional native experiment and its regression tools. Do not run
both services on port 8000.

This is the **real pinned Linux GP MicroBlocks executable**, not the web IDE.
noVNC remotely displays its SDL window. Serial communication happens entirely
on the VM, so browser WebSerial is neither required nor used.

## Install and access

Build the firmware/emulator first, then run:

```sh
scripts/setup-smallvm-ide.sh
```

The enabled system unit is `/etc/systemd/system/smallvm-ide.service`.
Open **https://erbium-qemu.exe.xyz/** through the authenticated exe.dev proxy.
The VM's reflection metadata confirms its default port is 8000. No share or
visibility changes are made.

The setup script installs `xvfb x11vnc novnc websockify xdotool openbox
fonts-dejavu-core x11-utils xauth` with apt. `python3-pil` and `scrot` were also
installed on this VM for shell screenshot validation; they are not required by
the service. The distro noVNC assets are `/usr/share/novnc`.
The native GP runtime's direct Pango/Cairo/ALSA dependencies are installed
explicitly too (`libpangocairo-1.0-0 libcairo2 libasound2t64`).

One supervisor owns one emulator and one desktop session:

- Private X display `:87`, Xauthority cookie, TCP disabled.
- Openbox with a dedicated maximized native IDE window and no runner shortcuts.
- `ext/smallvm/gp/gp-linux64bit`, all `runtime/lib/*.gp`, `loadIDE.gp`, and our
  extra GP files, launched with `ext/smallvm/gp` as cwd.
- Raw VNC listens **only on 127.0.0.1 / ::1 port 5900**.
- HTTP/WebSocket frontend listens **only on 127.0.0.1 port 8000** behind the
  **private authenticated exe.dev proxy**. It rejects cross-origin browser
  WebSockets and adds same-origin framing/content-type security headers.

Official exe.dev documentation:
`https://exe.dev/docs/proxy`, `https://exe.dev/docs/login-with-exe`,
`https://exe.dev/docs/integrations-reflection`.
Do not expose raw VNC, make the exe.dev proxy public, or change the service to
listen on a public interface. Local shell users are trusted and can access
localhost directly for testing. Authentication is the private proxy boundary,
not spoofable client headers.

## Desktop use

The launch opens `smallvm/examples/Erbium Showcase.ubp` when present, connects
to the emulator, and downloads it using the real MicroBlocks compiler/runtime.
Press **Start**, select **Variables**, and click `answer`: the board returns
**41**. The counter/list/timer demo needs no physical peripherals.

Toolbar controls: File, Connect, Start, Stop, Settings, zoom, categories, and
Libraries. File → Open starts in `smallvm/examples`. Connect includes the
explicit stable emulator port and a native port-name prompt.
Open/Save use the **VM's filesystem**, not your laptop's. Save edited projects
before restarting the entire service; that restarts the desktop and reopens
the showcase. Refresh the browser after a full service restart.

The pinned GP binary predates the HTML API primitives used by the pinned IDE
source. `native-compat.gp` restores native toolbar/category/library controls,
native prompts/save dialogs, script-area context menus, and bitmap running
highlights/drag shadows.
It provides inert browser-only notifications and local persisted preferences;
the editor, project parser, compiler, decompiler, bytecode protocol, and serial
primitives remain the real upstream implementation. It also loads current SVG
assets from the pinned source rather than older embedded icons. Browser picker
roots resolve to native filesystem folders, and Computer opens the native root
instead of invoking a nonexistent browser upload primitive. No upstream
files are modified by this service setup. Background upgrade checks are
disabled: the service intentionally uses the pinned source.

`launch.gp` only starts the UI and auto-selects the stable serial alias before
entering the native event loop. The real upstream IDE's `openPortAndSendPing`
fix is carried by the parent's SmallVM patch04, not duplicated in launch glue.
The alias contains `pts`, as accepted by the upstream native `ensurePortOpen`.

## The serial wire and local control

The IDE and emulator open **different raw PTY slaves**. The supervisor holds
both master fds and transparently relays bytes to the *opposite* master. They
must never open the same slave: that is not a UART connection.

- IDE: `/run/smallvm-ide/erbium-pts` (stable symlink).
- Emulator: opposite slave, also linked at `/run/smallvm-ide/emulator-serial`.
- Partial writes are retained. Each destination has a 1 MiB bounded queue;
  reads stop when it fills, applying backpressure instead of dropping bytes.
- Slave keeper fds prevent EIO/spin during native IDE reconnects.
- UART captures never contain emulator stdout/stderr.
- ELF64 entry PC comes directly from the current firmware ELF.

The emulator command is:

```text
dist/bin/erbium_emu -elf_load build/smallvm/smallvm.elf \
  -reset_pc <ELF-entry> -minions 1 -single_thread -max_cycles -1 \
  -uart_rx_file <opposite-PTY> -uart_tx_file <opposite-PTY>
```

Local-only commands (no HTTP control endpoints or arbitrary command runner):

```sh
smallvm/ide/control.py status
smallvm/ide/control.py reset
smallvm/ide/control.py disconnect
smallvm/ide/control.py reconnect
sudo systemctl restart smallvm-ide.service
sudo journalctl -u smallvm-ide.service -n 50
```

The 0600 UNIX socket is `/run/smallvm-ide/control.sock`. Reset restarts the
emulator from the current ELF, flushes old UART queues, and asks the native IDE
to reconnect/redownload through a fixed local request file. Disconnect stops
the emulator and closes the IDE port. Reconnect recreates the emulator and
reconnects the IDE. GUI responses are asynchronous; poll `status` for
`ide.connection == "connected"`.

An unexpected emulator exit is restarted without replacing the desktop.
Unexpected desktop/helper exit causes the whole owned session to be restarted
by systemd. `KillMode=control-group` ensures children cannot escape cleanup.

## Logs and validation

Each service session gets `build/smallvm-ide/<UTC-timestamp>/`.
`build/smallvm-ide/current` always points to the live session:

- `uart-ide-to-emulator.bin`, `uart-emulator-to-ide.bin`: exact binary streams.
- `emulator.stdout.log`, `emulator.stderr.log`: separate emulator diagnostics.
- `ide.stdout.log`, `ide.stderr.log`: native GP diagnostics.
- Other helpers have separate stdout/stderr logs; `events.jsonl` records
  lifecycle/control events.
- `/run/smallvm-ide/status.json`: atomically published process/byte/queue stats.
- `/run/smallvm-ide/ide-status.json`: connection, firmware version, project.
- `/var/lib/smallvm-ide/user-prefs.json`: persistent native preferences.

Logs are deliberately not served by the frontend. Session logs are retained
for debugging and are not automatically deleted.

```sh
/usr/bin/python3 smallvm/ide/test_relay.py
python3 -m py_compile smallvm/ide/*.py
bash -n scripts/setup-smallvm-ide.sh
```

The relay test exercises every byte value, simultaneous duplex traffic,
partial writes, both queues reaching their exact capacity, and binary
transcripts. It transfers 4.86 MB with no loss/change, then verifies cleanup.
Parent-owned `smallvm/tests/ide_integration.*` test the real compiler/VM path
separately and do not share this live desktop's PTYs.

Native widget validation exercised File/New accept and cancel, File/Open's
actual picker callback, File/Save cancel and a real `.ubp` disk write, editable
port-name prompts, confirmation cancellation, and native information dialogs.
It used the actual `Prompter`/picker widgets in an isolated X display, not
browser-API no-ops. Logs are `/tmp/smallvm-native-widget-tests/gp.log`.
An isolated real-emulator test also replayed the exact original snapshot's
broken open-port body (under a test-only method name): it cleared `portName`
and opened no port. The actual upstream patch04 then preserved a `pts` alias,
connected through the production paired-PTY relay, and received VM version 416.
Evidence: `/tmp/smallvm-native-port-test/logs/porttest.stdout.log`.

## Reproducible teardown

Disable/remove only this dedicated unit:

```sh
sudo systemctl disable --now smallvm-ide.service
sudo rm /etc/systemd/system/smallvm-ide.service
sudo systemctl daemon-reload
```

Stopping cleans all owned children/PTYs and systemd removes `/run/smallvm-ide`.
Persistent preferences `/var/lib/smallvm-ide` and timestamped build logs remain
for recovery. If wanted, delete those exact directories after inspecting them.
Packages are left installed deliberately; do not remove shared X/noVNC packages
without checking other services. Re-run the setup script to restore the unit.
