# Erbium QEMU emulation – TODO / open questions

- [ ] **Explore an out-of-band completion signal from Erbium to the host.** The TRM defines no
      interrupt line toward the host; the documented flow is polling `Mailbox1` / `sccr.xspi_status.wip`
      over xSPI. Candidates: a `GPIO[10:0]` line driven by minion firmware, or the UART. Both need extra
      board wiring (and pin-mux configuration via `system_register.SystemConfig`). Decide with the HW team;
      if adopted, add an `EVENT` message to the QEMU<->erbium_emu socket protocol and wire it to a QEMU GPIO
      the guest driver can use as an IRQ.
