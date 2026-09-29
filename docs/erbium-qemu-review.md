# Review: Erbium xSPI QEMU Emulation Brief

Sources checked: the brief, https://erbium.readthedocs.io (Summary, Memory Map, XSPI Core, SCCR, SFDP, Bringup, Signals),
upstream QEMU (`hw/block/m25p80*.c`, `hw/ssi/*`, `tests/qtest/`), upstream Linux `drivers/mtd/spi-nor/{core,sfdp}.c`,
and `aifoundry-org/et-platform/sw-sysemu` (+ `erbium-hal`).

## TL;DR

The QEMU-side architecture (SSI peripheral, block-backed 16 MB MRAM, logical-not-physical Octal/DTR, SFDP golden table,
qtests first, milestones) is sound. **But the brief's central premise is wrong for this chip:** Erbium's xSPI target is
an **xSPI Profile 2.0 (HyperRAM-like) device, not a Profile 1.0 SPI-NOR flash**. The documented command set has no RDID,
no WREN, no status register, no erase and no page program. Linux `spi-nor` cannot probe it without an Erbium-specific
driver, so the M2/M3 acceptance criteria ("unmodified mainline Linux via `jedec,spi-nor`") are unachievable unless
the emulator lies about the hardware — which defeats the purpose. Re-scope around the real personality.

## 1. What the Erbium docs actually say (vs. the brief)

| Brief assumes | Erbium TRM (xspi/, sfpd/, sccr/) |
|---|---|
| RDID (9Fh), WREN, RDSR, erase, page program | Command set is only: `5Ah` Read SFDP, `65h` Read Register, `71h` Write Register, `99h` Reset, `0Bh` Read Memory, `02h` Write Memory, `52h` setRate. TRM: "Write Enable: Not Supported", "Page/Sector Erase/Program: Not Supported". |
| NOR flash personality (JESD216 BFPT + xSPI Profile 1.0) | SFDP advertises **Profile 2.0** (`0xFF06`, `xspi_profile_2_support=1`, `wren1/wren2/sren = no`, `program/erase not supported`), plus BFPT (`0xFF00`, 23 DWORDs), 4BAIT (`0xFF84`), SCCR map (`0xFF87`), `0xFF09`, `0xFF0F`. `ID1.dev_type` = "hyperram". |
| 3/4-byte addressing negotiated via SFDP | Memory ops are always 4-byte address (even in 1S); register ops are 3-byte. Non-1S modes are `CMD + Ext, 4B addr, latency, data`. |
| Standard mode-entry (vendor CR write) | Mode change is the **non-standard `setRate 52h`** (3 bytes: cmd/addr/data rate; S1=0, D1=1, S4=4, D4=5, S8=6, D8=7, HB=8) plus `default_mode` pins for power-on rate (HB/Octal/Quad/SPI). |
| Byte-granular I/O | "Register data is 32-bit. Memory data is a multiple of 64-bit. Behaviour otherwise is undefined." Max burst 256. |
| Vendor opcodes for a compute control plane | No vendor opcodes. The control plane is memory-mapped: `system_registers.Mailbox0/1`, `sccr.xspi_status.wip`, `sccr.interrupt_status`, reachable via `0Bh/02h` at `0x40000000+` in the xSPI map, or via `65h/71h`. |
| Dummy cycles from SFDP | Latency comes from `sccr.CFG.InitialLatency` (reset 8), `FixedLatency=1`, HyperRAM style. SFDP dummy fields are still "TBD". |
| — | After any reset the host must clock 8 TCK cycles (or issue a `cmd=0` transaction). Model must accept opcode `0x00` as a NOP. |

Linux consequences (checked against current `spi-nor/core.c`):
* Unknown JEDEC ID + valid SFDP → `spi_nor_generic_flash` fallback exists, so RDID is survivable.
* But `spi_nor_select_erase()` returns `-EINVAL` if BFPT has no usable erase type → **probe fails**. Writes require WREN + RDSR polling, which Erbium doesn't implement.
* `spi-nor` parses Profile 1.0 / SCCR / 4BAIT / SMPT; it does **not** parse Profile 2.0 (`0xFF06`).
* 8D-8D-8D entry goes through `params->set_octal_dtr`, a per-vendor hook (Micron/Macronix/Spansion). Generic code can never issue `setRate 52h`.
* Linux `drivers/mtd/hyperbus` only supports HyperFlash (CFI), not HyperRAM — no help there either.

## 2. Recommended re-scoping

1. **Decide the product contract first (M0 blocker, hardware question):**
   * (a) keep the documented Profile 2.0 personality → ship a small **Erbium Linux `spi-mem` driver** (MTD `MTD_RAM` type, no erase, direct 64-bit-aligned writes; or a plain block device) + a chardev for the control plane; or
   * (b) if generic `jedec,spi-nor` compatibility is a real product requirement, that is an **RTL change** (add 9Fh/06h/05h, erase-as-no-op, Profile 1.0 table). The brief should raise this explicitly rather than assume it.
   Everything below assumes (a); it is also the only option that keeps "model matches the chip".
2. Replace the NOR-isms in the proposed device state (`write_enable`, `status_reg`, `four_byte_addr`, erase) with the real SCCR block: `ID0, ID1, CFG, xspi_status, xspi_control, xspi_rates, interrupt_status` (all 64-bit aligned, 0x38 bytes), a current `{cmd,addr,data}` rate tuple, latency count, deep-power-down state.
3. Command state machine per TRM formats `0.A/0.E/0.F/0.G/0.J/0.K` and `1.A/1.B/1.D/1.G`, address decoded through the **xSPI memory map** (`MRAM@0`, `NIC@0x0E000000`, `sysregs@0x40000000`, `mram_regs`, `i2c`, `qspi`, `uart`, `SRAM@0x40005000`, `cpu_regs@0x80000000`), with strict/lenient handling of non-64-bit data lengths and out-of-map accesses (set `interrupt_status.axi_resp`).
4. Drop "vendor compute opcodes". The M2 "fake compute engine" should react to **Mailbox0 writes** and post to Mailbox1 / `wip` / `interrupt_status`, i.e. the real host↔minion ABI. That way the Linux driver ABI developed against the fake engine carries over unchanged when the real CPU model (sw-sysemu) lands.
5. SFDP: generate the golden table from the same SystemRDL that produced the TRM `sfpd/` page and `erbium-hal/include/hwinc/*.h` (PeakRDL output), not by hand. Add a test that byte-compares QEMU's `5Ah` stream with that artefact. Several fields are still `TBD` in the TRM (numHdr, access_protocol, dummy cycles, ID0.mgf_id) — track them.
6. Keep HyperBus (Profile 2 / `HB` rate, RWDS, the A31:A3 erratum) as a later milestone, as the brief already suggests.

## 3. QEMU-specific corrections

* `tests/qtest/m25p80-test.c` does not exist. m25p80 coverage lives in `tests/qtest/aspeed_smc-test.c` / `ast2700-smc-test.c` (+ `aspeed-smc-utils.c`). Model the Erbium qtests on those, with the chosen controller.
* `FlashPartInfo::sfdp_read` exists (`hw/block/m25p80_sfdp.h`), but that table is m25p80-internal; a dedicated `erbium-xspi` device just implements `5Ah` itself. Sharing helpers with m25p80 is probably not worth it — the state machines barely overlap. `hw/ssi/` or `hw/misc/` is a better home than `hw/block/`.
* **QEMU's SSI bus carries no lane width, no DTR flag and no dummy-cycle count**, and controller models disagree on whether they even emit dummy bytes. `xlnx-versal-ospi.c` (the only octal-capable controller model; Linux driver `cadence-quadspi`, which does support 8D-8D-8D) pushes `opcode + address + data` for indirect reads and **no dummy bytes and no opcode-extension byte**. So the brief's "logical mode" plan needs an explicit contract: the Erbium model computes expected dummy *bytes* from its own `xspi_rates` × `CFG.InitialLatency`, and the controller model must be patched to emit them (strategy 2 in the brief). Expect strategy 3 (a small purpose-built OSPI controller) to be more likely than the brief implies. Aspeed SMC / NPCM FIU / SiFive SPI are quad-max.
* Host machine suggestion: `xlnx-versal-virt` (OSPI already wired, Linux `cadence-quadspi`). If the real host is RISC-V, add the same Cadence OSPI model to `virt` — a ~50-line board patch.
* Device properties: `size`/`octal`/`dtr`/`hyperbus` should mostly disappear; what matters is `default-mode` (the 2-bit pin strap), `drive`, `strict-lengths`, `log-commands`.
* Trace points listed are good; add `rate-change`, `latency`, `axi-error`, and `mailbox`.
* Sizing: the state machine is simpler than m25p80 (7 opcodes) but the address decoder + register block + burst rules add back; ~1000–1500 LOC still reasonable.

## 4. CPU subsystem: `erbium_emu` out-of-process (chosen approach)

Run `erbium_emu` (sw-sysemu's Erbium build) as a **separate process**. QEMU and `erbium_emu` share the 16 MB MRAM
through one mmap'd file; everything that is not plain memory goes over a small UNIX-socket protocol.

Why this is the right first cut:
* No C++/glog/lz4 inside QEMU, no license entanglement (Apache-2.0 vs GPLv2), no need to librarify or
  refactor `sys_emu::main_internal()` — the existing per-iteration `api_communicate::process()` hook is exactly
  the place to service a socket.
* Free persistence: the shared MRAM file *is* the `erbium-mram.img` the brief wanted.
* Each side keeps its own tooling: sysemu's GDB stub, logs, UART fifo files, `-elf` preloads; QEMU's qtests, tracing, monitor.
* The "fake compute engine" becomes a real minion firmware ELF running on `erbium_emu` — no throw-away C code in QEMU.

### 4.1 Topology

```text
  Linux guest (cadence-quadspi / erbium spi-mem driver)
        |
  QEMU  |  SSI
  +-----v----------------+     mmap (share=on)      +---------------------------+
  | erbium-xspi          |<======================>  | erbium-mram.img (16 MiB)  |
  |  SCCR regs (owned)   |                          +---------------------------+
  |  xSPI->CPU addr xlat |                                ^ mmap
  |  chardev=erb  -------+-- UNIX socket (req/resp) --+   |
  +----------------------+                            |   |
                                                +-----v---+----------------------+
                                                | erbium_emu (sw-sysemu)         |
                                                |  8 minions, sysregs, mailbox,  |
                                                |  PLIC, UART, bootrom, SRAM     |
                                                |  MRAM = MmapRegion(file)       |
                                                |  SocketAgent : api_communicate |
                                                +--------------------------------+
```

* **MRAM**: QEMU `-object memory-backend-file,id=mram,share=on,mem-path=erbium-mram.img,size=16M`; the
  `erbium-xspi` device takes `memdev=mram` (instead of a `BlockBackend`) and reads/writes the host pointer directly.
  `erbium_emu --mram-file erbium-mram.img` maps the same file into a new `MmapRegion` in place of the MRAM `DenseRegion`.
  Real hardware is non-coherent (L1D is explicitly non-coherent), so plain shared memory without locking is
  a faithful model; the mailbox handshake provides ordering, exactly as on silicon.
* **Registers**: the xSPI model translates xSPI-map addresses to CPU-map addresses
  (`MRAM 0x0 -> 0x40000000`, `sysregs 0x40000000 -> 0x02000000`, `SRAM 0x40005000 -> 0x0200C000`,
  `cpu_regs 0x80000000 -> 0x80000000`, …) and forwards non-MRAM accesses over the socket.
  SCCR (the xSPI IP's own registers: ID0/ID1/CFG/xspi_status/xspi_control/xspi_rates/interrupt_status) is **owned by
  QEMU**, since `setRate`, latency, WIP and AXI-error bits are xSPI-side behaviour. Minion-side access to SCCR (rare:
  `interrupt_enable`, `use_xspi_clk`) is proxied in the reverse direction later, or stubbed in sysemu for phase 1.
* **Socket**: QEMU side is a standard `chardev` (`-chardev socket,id=erb,path=erbium.sock` + `-device erbium-xspi,chardev=erb`),
  the same pattern vhost-user/ivshmem use. `erbium_emu --api-socket erbium.sock` listens and services requests
  from `SocketAgent::process()` (implements `api_communicate`: `host_memory_read/write`, `raise_host_interrupt`, `notify_fatal_error`).

### 4.2 Protocol (v1, synchronous, QEMU is the client)

Fixed 24-byte little-endian header + payload; one outstanding request at a time.

| op | payload | semantics |
|---|---|---|
| `READ`  | `addr:u64, size:u32` | returns `data[size]`, `status` (OK / AXI_SLVERR / AXI_DECERR) |
| `WRITE` | `addr:u64, size:u32, data[]` | returns `status` |
| `RESET` | `kind:u32` (POR / xspi-only) | resets the chip model; QEMU resets SCCR itself |
| `PING`  | — | version/handshake, reports MRAM file expectations (size, layout hash) |

`status` feeds `sccr.interrupt_status.axi_resp`, so the guest sees the same error semantics as hardware.
No asynchronous sysemu→QEMU messages are needed in v1: the TRM defines no out-of-band interrupt to the host
(host polls `Mailbox1`/`wip`), which also matches the brief's "start with polling". If a host-facing event line is ever
added in RTL, add an `EVENT` message and wire it to a QEMU GPIO.

Timing: no lockstep. `erbium_emu` free-runs (optionally throttled with a `--realtime`-style sleep per N cycles so it does not
pin a core while idle); QEMU blocks on a socket round-trip only for register accesses. Good enough for software bring-up;
if reproducibility becomes an issue later, add a `STEP n` op and drive sysemu from a `QEMUTimer`.

### 4.3 Changes required in sw-sysemu (small, all additive)

1. `memory/mmap_region.h` — `MemoryRegion` backed by a file mapping; used for MRAM when `--mram-file` is given (fall back to `DenseRegion`).
2. `sw-sysemu/socket_agent.{h,cpp}` — `api_communicate` implementation: non-blocking accept/poll in `process()`,
   uses `chip->memory.read/write` with a `Noagent` (same as `SysEmuImp::host_memory_*` does today). Wire it in `main.cpp` / `sys_emu_parse_args.cpp`.
3. `devices/sccr_er.h` — stub SCCR region at `ERBIUM_TOP_XSPI_REGISTERS_BASE` (0x0200F000) so minion firmware touching it does not
   `memory_error`. Note `hwinc/top.h` sizes it at 0x1C while the TRM's SCCR map is 0x38 with 64-bit stride — reconcile.
4. Keep the main loop as is. **No** `step()` refactor, **no** C façade, **no** QEMU build dependency.

Discrepancy to resolve with the HW team while there: TRM `interconnect/` puts SRAM at `0x0200A000`, `hwinc/top.h`
(and sw-sysemu) at `0x0200C000`; the TRM xSPI map has no SCCR entry at all (it is reached via `65h/71h` 3-byte addressing).

### 4.4 Changes in QEMU

* `erbium-xspi`: `memdev` property (HostMemoryBackend) for MRAM; `chardev` property for the backend; if no chardev
  is attached, non-MRAM accesses hit a **built-in stub** (mailbox/sysregs as plain storage) so qtests and
  the Linux driver run without `erbium_emu`.
* Address translation table (xSPI map -> CPU map) kept in one place, generated from the same RDL as `hwinc/top.h` if possible.
* Trace points: `erbium_xspi_backend_req/resp`, `erbium_xspi_axi_error`.

### 4.5 Launch

```bash
truncate -s 16M erbium-mram.img
erbium_emu -elf minion_fw.elf --mram-file erbium-mram.img --api-socket /tmp/erb.sock &
qemu-system-aarch64 -M xlnx-versal-virt ... \
  -object memory-backend-file,id=mram,share=on,mem-path=erbium-mram.img,size=16M \
  -chardev socket,id=erb,path=/tmp/erb.sock \
  -device erbium-xspi,bus=ospi-bus,cs=0,memdev=mram,chardev=erb
```

(Exact bus/cs syntax depends on the controller; the Versal OSPI model attaches SSI peripherals at board level today,
so the machine will need a small hook or a `-device` path.)

## 5. Milestones (revised)

* **M0 – Recon + contract.** Decide Profile 2.0 + Erbium `spi-mem` driver vs. RTL NOR-compat (§2.1). Resolve SFDP
  `TBD`s, Ext-byte encoding, SRAM/SCCR map discrepancies. Pick controller (Versal OSPI + dummy/ext-byte patch).
  Define the socket protocol and the mailbox/descriptor ABI. Deliverable: implementation note.
* **M1 – `erbium-xspi` device, standalone.** 1S-1S-1S; `00h` NOP, `99h`, `5Ah`, `65h/71h` (SCCR), `0Bh/02h` over the
  xSPI memory map; MRAM via `memdev`; built-in sysregs stub; qtests + SFDP golden compare.
* **M2 – Linux path.** Erbium `spi-mem` driver (MTD RAM + control chardev) on `xlnx-versal-virt`; `setRate` and logical
  4S/4D/8S/8D with the controller patch. Acceptance: `/proc/mtd`, r/w persists across reboot, mode switch traced.
* **M3 – `erbium_emu` backend.** `MmapRegion`, `SocketAgent`, SCCR stub in sw-sysemu; `chardev` backend in QEMU;
  a minimal minion firmware (poll `Mailbox0`, memcpy/CRC in MRAM, write `Mailbox1`) replaces the fake engine.
  Acceptance: Linux test tool submits a job through the driver, minion firmware on `erbium_emu` completes it, result read back through xSPI.
* **M4 – Real firmware.** `device-minion-runtime` / bootloader from `et-platform` booting from shared MRAM; error paths
  (`axi_resp`, underflow/overflow) exercised.
* **M5 – HyperBus profile, wrapped bursts, XIP** (TRM "Phase 2"); optional `STEP`-based deterministic mode; revisit
  in-process linking only if socket latency becomes a real problem.
