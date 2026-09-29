# Erbium xSPI/QSPI QEMU Emulation – Implementation Brief

## Goal

Implement a **functional QEMU model of the Erbium xSPI target interface** so an unmodified Linux guest can probe and use the device through the standard Linux `spi-mem` / `spi-nor` stack.

The initial objective is **software compatibility**, not cycle-accurate electrical simulation. The model should emulate the host-visible behavior needed to develop and test Linux, bootloader, and control-plane software before silicon is available.

Erbium documentation describes the chip as having:

- an **xSPI target interface**;
- **JESD251C** and **JESD216H** compliance;
- support for `1S-1S-1S`, `4S-4D-4D`, `4D-4D-4D`, `8S-8S-8S`, `8D-8D-8D (Octal)`, and `8D-8D-8D (HyperBus)` modes;
- operation up to 200 MHz;
- 64-bit register and memory access;
- linear burst support;
- 16 MB MRAM exposed through the xSPI-facing memory map.

Reference: <https://erbium.readthedocs.io/en/latest/>

---

## Recommended QEMU architecture

Start from QEMU's existing SPI-NOR model rather than inventing a new SPI infrastructure.

Primary upstream reference:

- `hw/block/m25p80.c`
- QEMU SSI/SPI infrastructure under `hw/ssi/` and `include/hw/ssi/`
- existing qtests such as `tests/qtest/m25p80-test.c`

Upstream `m25p80.c` already provides useful building blocks:

- `SSIPeripheral` integration;
- block-backed flash storage;
- JEDEC ID handling;
- command state machine;
- read/program/erase behavior;
- reset handling;
- SFDP support through `FlashPartInfo::sfdp_read`;
- VM migration state patterns.

Do **not** try to model eight physical IO lines, DDR edges, DQS timing, setup/hold timing, or 200 MHz timing in milestone 1. Treat Octal/DTR as a **logical protocol mode** while bytes continue to move through QEMU's SSI abstraction.

Target architecture:

```text
Linux guest
    |
    |  MTD / spi-nor
    v
  spi-mem
    |
    v
QEMU SPI/OSPI controller model
    |
    v
QEMU SSI bus
    |
    v
+---------------------------+
| erbium-xspi               |
|                           |
| JEDEC ID                  |
| JESD216H SFDP             |
| JESD251C command behavior |
| xSPI configuration regs   |
| 16 MB MRAM backing        |
| optional vendor commands  |
+---------------------------+
    |
    v
 erbium-mram.img
```

---

## Scope

### Milestone 1 – Linux-compatible flash personality

The first milestone is successful generic Linux SPI-NOR discovery and basic MTD operation.

The guest should be able to:

1. assert chip select;
2. issue `RDID` and obtain a stable JEDEC ID;
3. issue `RDSFDP`;
4. parse a JESD216H-compatible SFDP structure;
5. discover the flash geometry and supported read/program modes;
6. configure the Erbium model into the advertised xSPI mode;
7. read MRAM contents;
8. program MRAM contents;
9. erase supported regions if Erbium's target personality exposes erase semantics;
10. access the device as an ordinary Linux MTD device.

The ideal Linux DT binding should remain generic:

```dts
flash@0 {
    compatible = "jedec,spi-nor";
    reg = <0>;

    spi-max-frequency = <200000000>;
    spi-rx-bus-width = <8>;
    spi-tx-bus-width = <8>;
};
```

The important acceptance criterion is that **no Erbium-specific SPI-NOR driver is needed for normal MRAM access**.

---

## Suggested source layout

Prefer a dedicated model instead of heavily extending generic `m25p80.c`:

```text
hw/block/erbium-xspi.c
include/hw/block/erbium-xspi.h
hw/block/Kconfig
hw/block/meson.build
hw/block/trace-events            # if useful

tests/qtest/erbium-xspi-test.c
```

The implementation may borrow/refactor helpers from `m25p80.c` if that produces cleaner upstream-quality code.

A rough first implementation is expected to be on the order of:

```text
erbium-xspi.c              ~800–1500 LOC
erbium-xspi.h              ~100 LOC
qtests                     ~300–600 LOC
build/config glue           small
```

This is only a sizing guideline, not a constraint.

---

## Device state

A useful starting structure is:

```c
typedef struct ErbiumXSPI {
    SSIPeripheral parent_obj;

    BlockBackend *blk;

    uint8_t *storage;
    uint64_t size;

    /* Flash/xSPI state */
    bool write_enable;
    bool reset_enable;
    bool four_byte_addr;

    bool octal_mode;
    bool dtr_mode;
    bool hyperbus_mode;

    uint8_t status_reg;
    uint8_t cfg_regs[256];

    uint16_t opcode;
    uint64_t addr;
    unsigned addr_bytes;
    unsigned dummy_cycles;

    enum {
        ERB_IDLE,
        ERB_COLLECT_OPCODE,
        ERB_COLLECT_ADDR,
        ERB_DUMMY,
        ERB_READ,
        ERB_PROGRAM,
        ERB_READ_SFDP,
        ERB_READ_REGISTER,
        ERB_WRITE_REGISTER,
        ERB_VENDOR_COMMAND,
    } state;

    /* Optional compute-plane state */
    uint32_t compute_status;
    uint64_t descriptor_addr;

} ErbiumXSPI;
```

The exact fields should follow QEMU coding conventions and actual Erbium register definitions.

---

## Protocol modeling strategy

### Important abstraction

Do not make milestone 1 depend on QEMU understanding physical:

```text
DQ0..DQ7
DQS
rising/falling DDR edges
```

Instead represent the selected transfer protocol as model state:

```c
s->octal_mode = true;
s->dtr_mode = true;
```

The SSI transport still exchanges logical bytes/words.

For example, real hardware may perform:

```text
8D-8D-8D
command over 8 lanes DDR
address over 8 lanes DDR
data over 8 lanes DDR
```

while QEMU internally performs something equivalent to:

```text
transfer(opcode byte)
transfer(address byte ...)
transfer(dummy byte ...)
transfer(data byte ...)
```

The model validates that the command is legal in the current logical xSPI mode and applies the correct command semantics.

This is sufficient for validating:

- Linux driver behavior;
- SFDP probing;
- mode negotiation;
- command sequencing;
- address handling;
- configuration-register behavior;
- flash/MRAM semantics;
- future private control-plane commands.

It intentionally does **not** validate:

- signal integrity;
- DQS phase/alignment;
- setup/hold timing;
- DDR sampling;
- physical 200 MHz operation;
- board-level electrical behavior.

---

## JESD216H / SFDP

SFDP is the most important mechanism for avoiding an Erbium-specific Linux flash driver.

Implement a deterministic SFDP table describing the Erbium xSPI target personality.

QEMU already supports an SFDP callback in `FlashPartInfo`:

```c
uint8_t (*sfdp_read)(uint32_t sfdp_addr);
```

A simple first implementation can expose a static byte array:

```c
static uint8_t erbium_sfdp_read(uint32_t addr)
{
    static const uint8_t sfdp[] = {
        /* SFDP header */
        /* parameter headers */
        /* BFPT */
        /* 4-byte address table if applicable */
        /* xSPI profile tables required by the target personality */
    };

    return addr < sizeof(sfdp) ? sfdp[addr] : 0xff;
}
```

### Requirements for the SFDP data

The coding agent should derive the actual values from:

1. Erbium RTL/register definitions;
2. the Erbium technical reference manual;
3. the licensed JEDEC JESD216H/JESD251C specifications available to the development team.

Do **not** guess standard-defined bitfields or opcode assignments.

The SFDP data should describe at minimum:

- total density / 16 MB MRAM geometry;
- supported addressing width;
- supported read protocols;
- dummy-cycle requirements;
- page/program semantics where applicable;
- xSPI profile information required for Linux to select the intended mode;
- reset behavior;
- mode-entry/mode-exit behavior;
- any required status/configuration-register mechanisms.

Keep the SFDP table isolated in one source section/file so it can be compared directly against RTL and compliance-test expectations.

---

## Initial command set

Implement the smallest subset necessary for Linux probing and MTD I/O.

Suggested functional groups:

### Identification/discovery

- Read JEDEC ID (`RDID`)
- Read SFDP (`RDSFDP`)

### State/control

- Write Enable (`WREN`), if required by Erbium's programming model
- Write Disable, if applicable
- Read Status Register
- Read Configuration Register(s)
- Write Configuration Register(s)
- Reset Enable
- Reset Memory / Reset Device
- protocol-mode entry/exit required for Octal/DTR

### Data path

- basic 1S-1S-1S read
- fast read as required for initial Linux operation
- target Octal read mode
- page/program/write operation
- erase operation(s), only if part of Erbium's actual host-visible MRAM personality
- 3-byte/4-byte addressing handling as required
- linear/burst reads

### xSPI protocols to represent

Erbium documentation advertises:

```text
1S-1S-1S
4S-4D-4D
4D-4D-4D
8S-8S-8S
8D-8D-8D (Octal)
8D-8D-8D (HyperBus)
```

Do not implement all of these on day one unless Linux probing requires them.

Recommended order:

```text
1. 1S-1S-1S
2. SFDP discovery
3. 8S-8S-8S if needed
4. 8D-8D-8D Octal
5. remaining Quad modes
6. HyperBus personality
```

HyperBus should probably be treated as a separate follow-on milestone rather than complicating the initial SPI-NOR path.

---

## MRAM backing store

Erbium exposes 16 MB MRAM.

The model should use a normal QEMU block backend so state can persist across guest boots:

```bash
-blockdev driver=file,node-name=erbiumflash,filename=erbium-mram.img
```

and conceptually:

```bash
-device erbium-xspi,drive=erbiumflash,...
```

Exact command-line attachment depends on the selected QEMU machine/controller.

### Backing semantics

Implement host-facing MRAM behavior according to the actual Erbium RTL rather than blindly copying NOR constraints.

In particular, confirm whether the Erbium xSPI personality intentionally emulates NOR-style:

- page program restrictions;
- write-enable latches;
- sector erase requirements;
- `1 -> 0` programming behavior;

or whether MRAM is writable more directly.

Linux compatibility may justify presenting conventional SPI-NOR semantics even if the underlying storage technology is MRAM, but this must match the intended product interface.

---

## Erbium xSPI memory map

The Erbium documentation gives an xSPI-visible system address map with MRAM starting at address zero.

Relevant documented layout:

```text
0x00000000   MRAM
0x0E000000   NIC configuration space
0x40000000   System registers
0x40001000   MRAM registers
0x40002000   I2C registers
0x40003000   QSPI registers
0x40004000   UART registers
0x40005000   SRAM
0x80000000   CPU registers
```

Reference: <https://erbium.readthedocs.io/en/latest/interconnect/>

For milestone 1, only the MRAM region needs to be implemented unless Linux-visible register access is needed to enter/operate xSPI modes.

Later milestones can map accesses outside MRAM onto additional QEMU objects representing the CPU subsystem and peripherals.

---

## Recommended QEMU properties

Useful device properties could include:

```text
size=16M
jedec-id=<value>
drive=<block backend>
octal=true
dtr=true
hyperbus=false
strict-protocol=true
```

Potential debug/development properties:

```text
allow-legacy-mode=true
log-commands=false
inject-busy-us=0
```

Do not expose properties for behavior that should instead come from Erbium registers/SFDP unless they are genuinely useful for testing.

---

## Command-state machine

Model commands transactionally around chip-select boundaries.

Pseudo-flow:

```text
CS asserted
    |
    v
collect opcode
    |
    +--> determine legal opcode for current protocol
    |
    v
collect required address bytes
    |
    v
consume mode/dummy phase
    |
    +--> read path ------> stream bytes until CS deasserts
    |
    +--> write path -----> collect/write bytes until CS deasserts
    |
    +--> register op ----> fixed/variable register payload
    |
    +--> SFDP -----------> stream SFDP bytes

CS deasserted
    |
    v
commit/finalize command and return to idle
```

Be strict enough to catch bad guest-driver sequencing, but do not reject harmless behavior that real compliant hardware accepts.

Add trace points for at least:

```text
opcode
protocol mode
address
dummy count
read length
write length
mode transition
reset
invalid sequence
```

---

## Compute/control-plane extension – Milestone 2

After generic flash operation works, add Erbium-specific compute commands without changing the standard flash path.

Keep the normal data plane standard:

```text
Linux MTD
    |
 spi-nor
    |
 spi-mem
    |
 Erbium xSPI standard commands
```

Add a private control plane alongside it:

```text
custom Linux Erbium driver
    |
 spi-mem
    |
 vendor/private xSPI commands
```

### Placeholder command model

Do not commit to these opcode numbers until the real Erbium command map is defined, but conceptually provide:

```text
COMPUTE_CAPS
COMPUTE_SUBMIT
COMPUTE_STATUS
COMPUTE_RESULT / result metadata
EVENT_STATUS
EVENT_ACK
```

Prefer command descriptors stored in shared MRAM rather than pushing large payloads through register-like xSPI commands.

Example conceptual submission:

```text
SUBMIT_JOB
    descriptor_address
    descriptor_length
    flags
```

The descriptor and data already live in MRAM, so the host and Erbium compute cores operate on the same storage namespace.

---

## Fake compute engine – first implementation

Do not emulate the full Erbium CPU subsystem initially.

Implement a deterministic fake compute engine in the xSPI model:

```c
static void erbium_submit_job(ErbiumXSPI *s)
{
    /*
     * 1. Read descriptor from modeled MRAM.
     * 2. Validate descriptor.
     * 3. Perform a simple deterministic transformation.
     * 4. Write result back to modeled MRAM.
     * 5. Update completion/event state.
     */
}
```

Good first fake jobs:

- memcpy;
- memset;
- CRC32;
- byte checksum;
- XOR buffer;
- small vector add.

This allows development of:

- descriptor ABI;
- Linux ioctl/sysfs/chardev API;
- synchronization;
- completion handling;
- host/device memory ownership;
- error semantics;

without requiring CPU emulation.

---

## Event/completion mechanism

Start with polling:

```text
SUBMIT_JOB
    |
    v
host polls COMPUTE_STATUS / EVENT_STATUS
    |
    v
COMPLETE
```

Then add an interrupt/GPIO model if the planned silicon exposes an out-of-band event signal.

Do not invent an interrupt interface if the actual Erbium package/RTL does not provide one.

---

## Future Milestone 3 – shared Erbium CPU model

The longer-term model can connect the xSPI-visible storage to an emulated Erbium CPU subsystem:

```text
                 QEMU Erbium

       +-----------------------------+
       |                             |
xSPI --+--> xSPI target              |
       |       |                     |
       |       v                     |
       |  shared MRAM backing <------+-- Erbium CPU(s)
       |       ^                     |
       |       |                     |
       |  registers / SRAM           |
       |                             |
       +-----------------------------+
```

The critical property is that xSPI host accesses and Erbium CPU loads/stores operate on the same modeled MRAM.

Do not make this a dependency for milestones 1 or 2.

---

## Host controller choice

A target peripheral alone is insufficient: the chosen QEMU machine also needs an SPI/OSPI controller model that Linux can drive.

For the first end-to-end demo, select a QEMU-supported machine/controller combination with:

- stable Linux support;
- a usable QEMU SPI controller model;
- ability to attach an `SSIPeripheral`;
- minimal board-specific complexity.

If that controller cannot express the exact Octal/DTR semantics, use one of these strategies, in order:

1. keep lane-width/DTR state logical inside the Erbium model;
2. minimally extend the existing controller model;
3. create a small purpose-built test OSPI controller only if required.

Avoid broad QEMU SPI-framework changes during milestone 1.

---

## Tests

### Unit/qtest coverage

Add qtests covering at least:

```text
reset state
JEDEC ID
SFDP signature/header
SFDP reads across boundaries
basic memory read
write enable behavior
program/write
erase if implemented
mode entry
mode exit/reset
Octal logical-mode read
DTR logical-mode read
invalid opcode handling
invalid command for current mode
block-backend persistence
```

### Linux integration test

Boot a Linux guest and validate:

```bash
cat /proc/mtd
```

or equivalent shows the Erbium device.

Capture:

```bash
dmesg | grep -i -E 'spi|nor|mtd|sfdp'
```

Confirm Linux reports expected:

- density;
- erase/write geometry if applicable;
- selected read protocol;
- address width;
- SFDP revision/parameters where exposed.

Then perform destructive testing on a scratch image:

```text
read known pattern
write pattern
read back
reboot QEMU
read back again
```

Use standard MTD utilities where appropriate.

### SFDP verification

Add a golden test that compares the QEMU SFDP bytes against a checked-in expected binary/table generated from the Erbium specification.

This is important because subtle SFDP errors may cause Linux to choose an unexpected protocol.

---

## Milestones

### M0 – Source reconnaissance

- inspect current upstream `hw/block/m25p80.c`;
- inspect SSI interfaces;
- inspect SPI controller for selected QEMU machine;
- inspect Linux `spi-nor` behavior for JESD216H/xSPI profile parsing;
- obtain exact Erbium JEDEC ID, register map, command set, reset defaults, SFDP contents, and mode-transition rules from RTL/specification.

Deliverable: short implementation note documenting exact QEMU/Linux paths to modify.

### M1 – Generic Erbium flash target

Implement:

```text
QOM device
SSI connection
16 MB block-backed MRAM
CS handling
RDID
basic read
basic write/program semantics
reset
qtests
```

Acceptance: direct QEMU/qtest transactions work.

### M2 – SFDP / generic Linux probe

Implement valid JESD216H SFDP exposure.

Acceptance:

```text
Linux spi-nor probes device
MTD device appears
no Erbium-specific spi-nor patch required
```

### M3 – Octal xSPI logical mode

Implement the Erbium mode transition and logical `8S-8S-8S` / `8D-8D-8D` behavior required by Linux.

Acceptance:

```text
Linux negotiates intended xSPI protocol
read/write continues to work
reset returns device to defined power-on mode
```

### M4 – Erbium private compute plane

Implement:

```text
capability query
job descriptor submission
fake deterministic compute engine
status/completion
result storage
```

Acceptance: a small Linux test driver/tool can submit a job against data in MRAM and retrieve the result.

### M5 – Event mechanism

Add modeled interrupt/event notification corresponding to the planned hardware interface.

### M6 – Optional CPU/peripheral integration

Connect the xSPI model to a fuller Erbium SoC model/shared memory map if needed.

---

## Acceptance criteria for the initial project

The initial implementation should be considered successful when all of the following are true:

- [ ] QEMU instantiates an `erbium-xspi` target device.
- [ ] The device uses a persistent 16 MB backing image.
- [ ] JEDEC ID reads work.
- [ ] SFDP reads work.
- [ ] SFDP data accurately represents the intended Erbium host-visible profile.
- [ ] A mainline Linux guest probes it through generic `spi-nor`/`spi-mem`.
- [ ] An MTD device appears without an Erbium-specific SPI-NOR driver.
- [ ] Reads work.
- [ ] Writes/programming work according to the Erbium contract.
- [ ] Erase behavior works if the contract exposes erase commands.
- [ ] Reset/mode-transition semantics work.
- [ ] Octal/DTR operation is represented functionally even if the QEMU SSI bus remains byte-oriented.
- [ ] qtests cover identification, SFDP, storage, reset, and mode switching.
- [ ] Trace output is sufficient to diagnose guest-driver command sequences.

---

## Non-goals for the first version

Do not spend milestone-1 effort on:

- physical IO-lane simulation;
- DDR edge timing;
- DQS timing/alignment;
- signal integrity;
- physical 200 MHz timing;
- power modeling;
- full 8-core Erbium CPU emulation;
- every peripheral in the Erbium memory map;
- HyperBus unless immediately required;
- performance modeling.

These can be added later without changing the host-visible software contract.

---

## Important implementation rule

**Treat the Erbium RTL + licensed JESD251C/JESD216H specifications as authoritative.**

The coding agent should not infer exact JEDEC-standard bitfields, SFDP words, opcodes, mode-transition sequences, dummy-cycle encodings, or register definitions from this document. This brief defines the QEMU architecture and development sequence; standards-level constants must come from authoritative project sources.

---

## Useful upstream references

### Erbium

- Technical reference manual: <https://erbium.readthedocs.io/en/latest/>
- xSPI-visible memory map: <https://erbium.readthedocs.io/en/latest/interconnect/>

### QEMU

- SPI-NOR model: <https://gitlab.com/qemu-project/qemu/-/blob/master/hw/block/m25p80.c>
- QEMU internal/SSI documentation: <https://qemu.readthedocs.io/en/master/devel/index-internals.html>

Key current upstream observations:

- `m25p80.c` models SPI flash as an `SSIPeripheral`.
- it already implements command/state handling, storage backing, JEDEC ID, reset and SFDP support;
- `FlashPartInfo` includes an `sfdp_read()` callback;
- its current internal protocol model includes standard/dual/quad modes, making Erbium Octal support a natural dedicated extension rather than something to assume already exists generically.

---

## Coding-agent starting prompt

Use the following as the immediate implementation directive:

> Implement a new QEMU `erbium-xspi` SSI peripheral that functionally emulates the Erbium xSPI flash-replacement interface. Start from the architecture and milestones in this document. Reuse or refactor upstream `hw/block/m25p80.c` behavior where practical, but keep Erbium-specific JESD216H/JESD251C state and future compute commands isolated. First target is a persistent 16 MB MRAM-backed device supporting reset, JEDEC ID, baseline read/write semantics, and SFDP. Then make an unmodified mainline Linux guest probe it through generic `spi-nor`/`spi-mem`. Model Octal/DTR as logical protocol state initially; do not attempt physical lane/edge/DQS simulation. Add qtests before adding the compute-control plane. Do not invent standards-defined constants: obtain exact SFDP data, command encodings, configuration registers, mode transitions, and reset defaults from the Erbium RTL/TRM and licensed JEDEC specifications.

