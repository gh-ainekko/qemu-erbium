# Erbium QEMU emulation – TODO / open questions

- [ ] **Explore an out-of-band completion signal from Erbium to the host.** The TRM defines no
      interrupt line toward the host; the documented flow is polling `Mailbox1` / `sccr.xspi_status.wip`
      over xSPI. Candidates: a `GPIO[10:0]` line driven by minion firmware, or the UART. Both need extra
      board wiring (and pin-mux configuration via `system_register.SystemConfig`). Decide with the HW team;
      if adopted, add an `EVENT` message to the QEMU<->erbium_emu socket protocol and wire it to a QEMU GPIO
      the guest driver can use as an IRQ.

## Added during implementation (see docs/rtl-xspi-findings.md for details)

- [ ] **Linux side (M2):** write the Erbium `spi-mem` driver (MTD RAM type + control chardev); needs a
      guest kernel, an OSPI DT node for `xlnx-versal-virt` (QEMU generates none; `docs/qemu-ospi-notes.md` §7)
      and `OSPI_QSPI_IOU_AXI_MUX_SEL` bit1 set for DAC mode. Driver must respect: reads limited to one AXI
      burst per CS# (8 B default, 128 B max with CFG.BurstEnable), 8-byte write granularity, 4 KiB write wrap,
      latency = 8 + CFG.InitialLatency cycles, 4-byte addresses, ext byte in 4S/4D/8S/8D.
- [ ] **HW questions from RTL vs TRM:** SRAM at CPU 0x0200C000/xSPI 0x4000C000 (TRM says 0x0200A000/0x40005000);
      SCCR size 0x38 with 8-byte stride (`hwinc/top.h` says 0x1C); max read burst 128 B (TRM says 256);
      `xspi_control.interrupt_enable` unwired; Mailbox1 @0x70; SFDP NPH=6 (should be 5), 0xFF0F pointer 0x1100
      unreachable, DWORDs streamed with 4 zero pad bytes; D1 cmd-rate register ops need 4 address bytes
      (BFM sends 3); `addr_rate` must equal `data_rate`.
- [ ] **Mailbox interrupt:** writing Mailbox0 does not interrupt the minion; host must set
      `SysInterrupt` (0x4000_0020) after `SystemConfig.sys_interrupt_enable`. The worker firmware polls for now.
- [ ] **HyperBus profile (8D-8D-8D HB / CA format, A31:A3 erratum):** not implemented in the QEMU model.
- [ ] **Versal OSPI model fidelity:** `faithful-frames` is opt-in; indirect writes framed per controller page
      (max 4095 B) so the 4 KiB Erbium write wrap can't be exercised through it; consider making the
      erbium machine variant default to faithful-frames and DAC mux enabled.
- [ ] **MRAM beyond 16 MiB / alias window 0x7E00_0000:** model returns DECERR; check what the MRAM slave does.
- [ ] **Deterministic co-simulation:** add `STEP` op if reproducibility is needed (erbium_emu free-runs now).
- [ ] **VMState/migration** for `erbium-xspi` (currently unmigratable).
- [ ] `hwinc/top.h` `XSPI_REGISTERS_SIZE` fix in erbium-hal.
