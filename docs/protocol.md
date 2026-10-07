# QEMU <-> erbium_emu control protocol (v1)

Transport: UNIX stream socket. `erbium_emu` listens (`--api-socket PATH`), QEMU connects
(`-chardev socket,id=erb,path=PATH` + `-device erbium-xspi,chardev=erb`).
Exactly one outstanding request at a time; QEMU is always the requester in v1.

This models the **AXI4 initiator port of the xSPI target IP into the Erbium NIC**. Addresses are
**CPU-map addresses** (the QEMU side translates from the xSPI-visible map before sending), e.g.
`system_registers` at `0x02000000`, `SRAM` at `0x0200C000`, `MRAM` at `0x40000000`, `cpu_registers` at `0x80000000`.
MRAM is normally *not* sent over the socket (it is shared via the mmap'd file) but the server must still
serve it correctly if asked (used for tests / fallback).

All integers little-endian. Every message (both directions) starts with a 24-byte header:

```c
struct erb_msg_hdr {
    uint32_t magic;     /* 0x42524555 'UERB' */
    uint16_t version;   /* 1 */
    uint16_t op;        /* see below */
    uint32_t status;    /* request: 0 ; response: ERB_ST_* */
    uint32_t len;       /* payload bytes following the header */
    uint64_t addr;      /* address for READ/WRITE; kind for RESET; 0 otherwise */
};
```

| op | value | request payload | response payload |
|---|---|---|---|
| `ERB_OP_PING`  | 1 | none | `struct erb_info { uint32_t proto_version; uint32_t flags; uint64_t mram_base; uint64_t mram_size; char name[32]; }` |
| `ERB_OP_READ`  | 2 | none; `hdr.addr` = address, `hdr.len` = size (1..4096) | `len` data bytes (on error: `len` = 0) |
| `ERB_OP_WRITE` | 3 | `len` data bytes at `hdr.addr` | none |
| `ERB_OP_RESET` | 4 | none; `hdr.addr` = kind: 0 = POR (whole chip), 1 = xSPI-only (no-op for the server) | none |

Response `op` echoes the request `op` with bit 15 set (`op | 0x8000`).

Status values (map 1:1 onto AXI responses so QEMU can set `sccr.interrupt_status.axi_resp`):

| name | value | meaning |
|---|---|---|
| `ERB_ST_OK`     | 0 | OKAY |
| `ERB_ST_SLVERR` | 2 | target signalled error (e.g. register write rejected, misaligned) |
| `ERB_ST_DECERR` | 3 | no target at that address |
| `ERB_ST_BADREQ` | 0x100 | protocol error (bad magic/version/len) |

Rules:
* Accesses may be any size 1..4096 and need not be aligned; the server splits them as its memory model
  requires. Register regions in sw-sysemu are 32/64-bit oriented; a misaligned/odd-size access into a
  register region returns `SLVERR`.
* The server must service requests while the CPU model is running (it is polled from the emulator's main
  loop, `api_communicate::process()`), and also while all harts are asleep.
* If the client disconnects the server keeps running and accepts a new connection.
* Future (not v1): `ERB_OP_EVENT` (server->client, out-of-band completion line, see TODO), `ERB_OP_STEP`
  (deterministic co-simulation).

## Host-controlled CPU boot (backend reset/start extension)

No new socket opcode is needed. Use ordinary register writes to System
SoftReset (`0x02000028` in this CPU-addressed protocol), MINION_BOOT and the
thread-disable ESRs. `SoftReset=0x6` holds CPU warm reset with MRAM out of reset;
`0x4` releases it using the host-programmed boot PC. Host-facing xSPI SoftReset
is instead `0x40000028`. See `host-elf-loading.md` for the complete sequence.

`--start-held` makes the backend assert CPU warm hold on startup and each
whole-chip POR (including QEMU device reset). The listener remains usable with
no ELF and no running harts. Kind 0 resets boot configuration/reapplies the CLI
startup profile; kind 1 continues to leave CPU/hold/boot configuration unchanged.
A CPU-only warm release does not reapply CLI settings or erase MRAM.
