# QEMU Versal OSPI path — notes for the Erbium xSPI peripheral author

Tree: `ext/qemu` (Xilinx/AMD fork of QEMU, `VERSION` = 10.2.4, shallow clone).
The analysis below was done against the fork commit `59fb95c` (the pristine
fork). While it was being written the peripheral work landed on top:
`0ed6dbd` (erbium-xspi device + machine change), `f457a13`, `4b67c78`
(applies the §2.7 `faithful-frames` patch to `xlnx-versal-ospi.c`) and
`9699a2f` (`tests/qtest/erbium-xspi-test.c`). **All `hw/ssi/xlnx-versal-ospi.c`
line numbers below refer to 59fb95c**, i.e. before 4b67c78 shifted them
(≈ +60 lines after line 368); `xlnx-versal-virt.c` numbers are given for both
59fb95c and 0ed6dbd where they differ. Everything else is unchanged.

Contents

0. Build status / how to build (+ two fork/WIP bugs you will hit)
1. Machine wiring (`hw/arm/xlnx-versal-virt.c`, `hw/arm/xlnx-versal.c`)
2. The OSPI controller model (`hw/ssi/xlnx-versal-ospi.c`) — exactly what
   goes over `ssi_transfer()`, and a patch to make it emit ext-opcode and
   dummy bytes
3. The SSI peripheral API (`hw/ssi/ssi.c`, `include/hw/ssi/ssi.h`) + meson/Kconfig
4. How `m25p80` is structured (state machine, CS, block backend, VMState),
   and how to use a `memdev` (HostMemoryBackend) link instead
5. qtests: aspeed_smc-test / aspeed-smc-utils, existing Versal qtests, sketch
   of an erbium qtest
6. Xilinx-fork specifics relevant to OSPI/xSPI
7. Guest (Linux `cadence-quadspi`) expectations vs. the model

---

## 0. Build

### 0.1 Configure / build

Build directory: `ext/qemu/build`. Deps installed with apt (minimal set):
`ninja-build meson libglib2.0-dev libpixman-1-dev libfdt-dev libslirp-dev
pkg-config flex bison zlib1g-dev python3-venv python3-pip libgcrypt20-dev`.
apt's meson (1.3.2) is too old for this tree (`pythondeps.toml:22` wants
>= 1.5); configure silently creates `build/pyvenv` with the vendored meson
1.9.0, so that is fine.

Configure line used (from `ext/qemu/build`; **`--enable-gcrypt` is mandatory**, see 0.2):

```
../configure --target-list=aarch64-softmmu --enable-fdt --disable-docs \
  --disable-werror --enable-slirp --enable-gcrypt \
  --disable-gtk --disable-sdl --disable-vnc --disable-spice --disable-opengl \
  --disable-virglrenderer --disable-gnutls --disable-nettle \
  --disable-libssh --disable-curl --disable-libnfs --disable-libiscsi \
  --disable-rbd --disable-glusterfs --disable-smartcard --disable-usb-redir \
  --disable-libusb --disable-xen --disable-bpf --disable-guest-agent \
  --disable-tools --disable-plugins --disable-lzo --disable-snappy \
  --disable-bzip2 --disable-lzfse --disable-zstd --disable-brlapi \
  --disable-curses --disable-alsa --disable-pa --disable-pipewire \
  --disable-oss --disable-jack --disable-sndio --disable-vhost-user \
  --disable-vhost-net --disable-vhost-kernel --disable-vhost-vdpa \
  --disable-libvduse --disable-vduse-blk-export --disable-user \
  --disable-linux-user --disable-bsd-user --disable-tpm --disable-seccomp \
  --disable-cap-ng --disable-attr --disable-linux-aio --disable-linux-io-uring \
  --disable-numa --disable-rdma --disable-vde --disable-netmap \
  --disable-libudev --disable-mpath --disable-l2tpv3 --disable-af-xdp \
  --disable-vte --disable-dbus-display --disable-libdw --disable-selinux \
  --disable-crypto-afalg --disable-auth-pam --disable-coreaudio \
  --disable-hvf --disable-whpx --disable-xkbcommon --extra-cflags=-g1
```

Build: `ninja -C build -j2 qemu-system-aarch64` (~1400 objects, > 1 h on
2 vCPUs; an incremental rebuild after touching `hw/ssi/*.c` is seconds).

qtest binaries: with only `aarch64-softmmu`, **`aspeed_smc-test` is not a
target** — it is in `qtests_arm` (`tests/qtest/meson.build:245`; its
machines are 32-bit). The aarch64 twin using the same `aspeed-smc-utils.c`
is `ast2700-smc-test` (`meson.build:224,375`):

```
ninja -C build tests/qtest/ast2700-smc-test tests/qtest/xlnx-versal-trng-test
(cd build && QTEST_QEMU_BINARY=./qemu-system-aarch64 tests/qtest/ast2700-smc-test)
(cd build && QTEST_QEMU_BINARY=./qemu-system-aarch64 tests/qtest/xlnx-versal-trng-test)
```

### 0.2 Two things that break the build

1. **Fork: libgcrypt is not optional.** `crypto/ecdsa-stub.c:12` does
   `#include <gcrypt.h>` even with `--disable-gcrypt`, and
   `hw/misc/xlnx-versal-ecdsa-rsa.c` / `include/hw/misc/ipcores-rsa5-4k.h`
   (pulled in by `CONFIG_XLNX_ZYNQMP_CSU`, `hw/misc/meson.build:44-50`) call
   `gcry_mpi_*`/`rsa_do_*` and fail to link without it. Install
   `libgcrypt20-dev` and configure with `--enable-gcrypt`.
2. **WIP commit `0ed6dbd`: Kconfig cycle.** Its `hw/ssi/Kconfig` entry

   ```
   config ERBIUM_XSPI
       bool
       default y
       depends on SSI
       select SSI
   ```

   makes `scripts/minikconf.py` abort with
   `KconfigDataError: cycle found including ERBIUM_XSPI` — configure fails
   for the whole tree. Fix (applied in the working tree, **uncommitted**, so
   the author can fold it into their commit): delete the `depends on SSI`
   line. (`default y` + `select SSI` is enough and makes the device present
   in `aarch64-softmmu`.)
3. **WIP commit `0ed6dbd`: two compile errors in the new device** (QEMU 10.x
   header moves): `hw/ssi/erbium-xspi.c:29` includes `exec/memory.h`, which
   is now `system/memory.h`; and `include/hw/ssi/erbium-xspi.h:22` uses `MiB`
   without `#include "qemu/units.h"`. These three one-liners (and the Kconfig
   one above) were applied in the working tree and have since been absorbed
   into commits `f457a13`/`4b67c78`; HEAD `9699a2f` configures and builds
   cleanly.

### 0.3 Results (2026-09-29)

* `build/qemu-system-aarch64` — built (`ninja -C build -j2 qemu-system-aarch64`
  exit 0; 57 MB binary with `-g1`; `build/` is 172 MB).
* `qemu-system-aarch64 -M xlnx-versal-virt -display none -serial null -S -monitor stdio`
  starts, accepts `quit`. (`-nographic -monitor stdio` together fails with
  "cannot use stdio by multiple character devices" — use `-serial null` or
  `-monitor stdio` without `-nographic`.)
* `-device help` lists `erbium-xspi` (bus SSI); `-M xlnx-versal-virt,help`
  lists `ospi-flash=<string>`.
* `-M xlnx-versal-virt,ospi-flash=erbium-xspi -object memory-backend-ram,id=mram,size=16M -global erbium-xspi.memdev=mram`
  boots and `info qtree` shows `bus: spi0 / dev: erbium-xspi` with
  `gpio-in "ssi-gpio-cs" 1` under `/machine/xlnx-versal/ospi`.
* HEAD `9699a2f` also builds (incremental), and the new
  `build/tests/qtest/erbium-xspi-test` passes 7/7
  (`QTEST_QEMU_BINARY=./qemu-system-aarch64 tests/qtest/erbium-xspi-test`).
* qtest infrastructure: `build/tests/qtest/ast2700-smc-test` and
  `build/tests/qtest/xlnx-versal-trng-test` compile.
  `xlnx-versal-trng-test` **passes** (5 ok) with
  `QTEST_QEMU_BINARY=./qemu-system-aarch64` — so qtests against
  `xlnx-versal-virt` work. `ast2700-smc-test` aborts because the fork's
  `ast2700-evb` machine itself is broken (`Property 'arm-gicv3.sysbus-irq[20]'
  not found` even when started by hand) — unrelated to us; the 32-bit
  `aspeed_smc-test` needs `arm-softmmu`, which was deliberately not added.

---

## 1. Machine wiring

### 1.1 Machine types and the `ospi-flash` property

`hw/arm/xlnx-versal-virt.c`

* Two concrete machines share an abstract base
  (`TYPE_XLNX_VERSAL_VIRT_BASE_MACHINE`, `xlnx-versal-virt.c:388-396` @59fb95c /
  `399-407` @0ed6dbd): `amd-versal-virt` with alias **`xlnx-versal-virt`**
  (`367-377` / `378-388`, `VERSAL_VER_VERSAL`) and `amd-versal2-virt`.
* The machine property is **`ospi-flash`** (not "ospi-model"): registered in
  `versal_virt_machine_class_init_common()` (`362-365` / `373-376`) with
  `versal_get_ospi_model()` / `versal_set_ospi_model()` (`195-208` / `196-209`),
  stored in `s->cfg.ospi_model` (line 49/50).
  Usage: `-machine xlnx-versal-virt,ospi-flash=<qom-type>`.
* `XLNX_VERSAL_NUM_OSPI_FLASH` = 4 (line 35/36).

### 1.2 Flash instantiation loop (`versal_virt_init`)

At 59fb95c (lines 293-316):

```c
for (i = 0; i < XLNX_VERSAL_NUM_OSPI_FLASH; i++) {        /* 4 chip selects */
    DriveInfo *dinfo = drive_get(IF_MTD, 0, i);             /* -drive if=mtd,index=i */
    if (s->cfg.ospi_model) {
        flash_klass = object_class_by_name(s->cfg.ospi_model);
        if (!flash_klass || object_class_is_abstract(flash_klass) ||
            !object_class_dynamic_cast(flash_klass, TYPE_M25P80)) {   /* line 303 */
            error_report("'%s' is either abstract or not a subtype of m25p80", ...);
            exit(1);
        }
        mdl = s->cfg.ospi_model;
    } else {
        mdl = "mt35xu01g";                                  /* default, line 310 */
    }
    blk = dinfo ? blk_by_legacy_dinfo(dinfo) : NULL;
    versal_ospi_create_flash(&s->soc, i, mdl, blk);         /* line 314 */
}
```

i.e. the *same* model on all four CS lines, hard-required to be an `m25p80`
subclass; `-drive if=mtd,index=N` is optional.

**Commit 0ed6dbd already relaxes this** (lines 294-327): the check becomes
`TYPE_SSI_PERIPHERAL`, `is_m25p80` is computed with
`object_class_dynamic_cast(flash_klass, TYPE_M25P80)`, a non-m25p80 model is
only instantiated on CS0 (`if (!is_m25p80 && i > 0) continue;`), and the
drive is only passed for m25p80 devices. That is essentially the minimal
patch I would have proposed (my version is kept in §1.4 for reference; the
only difference is that I also guard `qdev_prop_set_drive_err` inside
`versal_ospi_create_flash()` with `object_property_find(OBJECT(flash), "drive")`,
which is now unnecessary because the caller passes `NULL`).

### 1.3 SoC side (`hw/arm/xlnx-versal.c`, unchanged by 0ed6dbd)

* Address map: `struct VersalOspiMap` (184-191). Versal values at 319-324:
  controller regs `0xf1010000`, linear (DAC) window `0xc0000000` size
  `0x20000000`, DMA src `0xf1011000`, DMA dst `0xf1011800`, IRQ 124.
  Versal2 at 438-443 (IRQ 216).
* `versal_create_ospi()` (1468-1535): `qdev_new(TYPE_XILINX_VERSAL_OSPI)`
  as child **`ospi`** of the SoC (QOM path `/machine/xlnx-versal/ospi`);
  two `xlnx.csu_dma` children (`dma-src-dev`, `dma-dst-dev`); realize; map
  MMIO region 0 (regs) at `map->ctrl` and region 1 (DAC) inside a
  `linear-mr` container at `map->dac`; OR the three IRQs.
* 1924-1929: SLCR gpio `ospi-mux-sel` → OSPI gpio `ospi-mux-sel` (drives
  `dac_enable`, §2.6).
* **`versal_ospi_create_flash()` (2009-2030)** — the only attach point:

```c
ospi    = DEVICE(versal_get_child(s, "ospi"));
spi_bus = qdev_get_child_bus(ospi, "spi0");              /* bus name "spi0" (2017) */
flash   = qdev_new(flash_mdl);
if (blk) qdev_prop_set_drive_err(flash, "drive", blk, &error_fatal);  /* 2021-2023 */
qdev_prop_set_uint8(flash, "cs", flash_idx);            /* SSIPeripheral "cs" prop */
qdev_realize_and_unref(flash, spi_bus, &error_fatal);   /* == ssi_realize_and_unref */
cs_line = qdev_get_gpio_in_named(flash, SSI_GPIO_CS, 0);/* the peripheral's CS input */
qdev_connect_gpio_out(ospi, flash_idx, cs_line);        /* OSPI unnamed gpio-out[idx] */
```

  QOM path of the CS0 device: `/machine/xlnx-versal/ospi/spi0/child[0]`.
* OSPI realize (`hw/ssi/xlnx-versal-ospi.c:1775-1788`): `num_cs = 4`,
  `ssi_create_bus(dev, "spi0")`, `qdev_init_gpio_out(dev, s->cs_lines, 4)`
  — 4 unnamed gpio-outs, one per CS, active-low (§2.5).
* **No device-tree node for the OSPI is generated.** `fdt_create()`
  (`xlnx-versal-virt.c:59-90`) only creates `/chosen`, `/aliases`, model and
  compatible; per-device nodes are added in `xlnx-versal.c` via
  `versal_fdt_add_simple_subnode()` (uart 1041, canfd 1086, usb 1128, gem
  1208, dma 1266, sdhci 1304, rtc 1336, …) and there is none for OSPI. A
  Linux guest needs an external `-dtb` or a node added next to
  `versal_create_ospi()` (template in §7).

### 1.4 Minimal machine patch (reference; superseded by 0ed6dbd)

Design constraints that led to it:

* `ssi_auto_connect_slaves()` is declared (`include/hw/ssi/ssi.h:130`) but
  **never defined**, so `-device erbium-xspi,bus=…` would never get its CS
  gpio wired; the machine has to do the attach.
* `ssi_bus_check_address()` (`hw/ssi/ssi.c:45-56`) rejects a second device on
  a used CS index, so the default flash must not be created on that CS.
* A `HostMemoryBackend`/chardev cannot be shared by four instances → CS0 only;
  an empty CS is fine (`ssi_transfer()` over no children returns 0).
* Device-specific properties are set with `-global erbium-xspi.memdev=…`
  (globals are applied at instance init; link and chardev props accept ids).

```diff
--- a/hw/arm/xlnx-versal-virt.c
+++ b/hw/arm/xlnx-versal-virt.c
@@ -300,13 +300,20 @@ static void versal_virt_init(MachineState *machine)
             if (!flash_klass ||
                 object_class_is_abstract(flash_klass) ||
-                !object_class_dynamic_cast(flash_klass, TYPE_M25P80)) {
+                !object_class_dynamic_cast(flash_klass, TYPE_SSI_PERIPHERAL)) {
                 error_report("'%s' is either abstract or"
-                       " not a subtype of m25p80", s->cfg.ospi_model);
+                       " not an SSI peripheral", s->cfg.ospi_model);
                 exit(1);
             }
+            if (i > 0 &&
+                !object_class_dynamic_cast(flash_klass, TYPE_M25P80)) {
+                continue;   /* exclusive-backend devices: CS0 only */
+            }
             mdl = s->cfg.ospi_model;
--- a/hw/arm/xlnx-versal.c
+++ b/hw/arm/xlnx-versal.c
@@ -2020,7 +2020,7 @@ void versal_ospi_create_flash(Versal *s, int flash_idx, const char *flash_mdl,
-    if (blk) {
+    if (blk && object_property_find(OBJECT(flash), "drive")) {
         qdev_prop_set_drive_err(flash, "drive", blk, &error_fatal);
     }
```

Resulting command line (works with 0ed6dbd as committed):

```
qemu-system-aarch64 -M xlnx-versal-virt,ospi-flash=erbium-xspi \
  -object memory-backend-file,id=mram,size=16M,mem-path=/tmp/mram.bin,share=on \
  -global erbium-xspi.memdev=mram [-global erbium-xspi.chardev=…] …
```

---

## 2. What the OSPI model actually puts on the SSI bus

File: `hw/ssi/xlnx-versal-ospi.c` (1897 lines), header
`include/hw/ssi/xlnx-versal-ospi.h`.

### 2.1 Transport primitives

* `ospi_flush_txfifo()` (632-640): pops **bytes** from the 8-bit `Fifo8
  tx_fifo` and calls `ssi_transfer(s->spi, byte)` once per byte, pushing each
  return value into `rx_fifo`. So although `ssi_transfer()` is
  `uint32_t`→`uint32_t`, **the word width is 8 bits**: `val` is always 0..255
  and the peripheral's return is truncated to 8 bits by `fifo8_push()`.
  It is full duplex: one byte received for each byte sent; rx bytes clocked
  during opcode/address are discarded with `fifo8_reset(&s->rx_fifo)`.
* Address: `ospi_tx_fifo_push_address_raw()` (642-657), MSB first, 1..4 bytes.
  Indirect/DAC ops use `DEV_SIZE_CONFIG_REG.NUM_ADDR_BYTES_FLD + 1`
  (`ospi_get_num_addr_bytes`, 383-388, via 659-665); STIG uses
  `FLASH_CMD_ADDR_REG` and `FLASH_CMD_CTRL_REG.NUM_ADDR_BYTES_FLD + 1`
  (`ospi_stig_addr_len` 335-340, `ospi_tx_fifo_push_stig_addr` 667-673).
* Opcodes: read `DEV_INSTR_RD_CONFIG_REG.RD_OPCODE_NON_XIP_FLD` (377-381);
  write `DEV_INSTR_WR_CONFIG_REG.WR_OPCODE_FLD` (371-375); STIG
  `FLASH_CMD_CTRL_REG.CMD_OPCODE_FLD` (1048); WREN hard-coded `0x06`
  (enum `WREN`, 332; pushed at 890).

### 2.2 Per-transaction byte streams (current model)

Notation: `[CS↓] … [CS↑]`; `A×n` = n address bytes; `D×n` = n data bytes;
`Z×n` = n zero bytes clocked out to receive n bytes.

| Trigger | Code (lines) | Bytes on the bus |
|---|---|---|
| Indirect read (`INDIRECT_READ_XFER_CTRL_REG.START`, reg 0x60) | `ind_rd_xfer_ctrl_reg_post_write` 1357 → `ospi_do_ind_read` 830 → `ospi_ind_read` 736-761 | `[CS↓] RD_OPCODE, A×n(DEV_SIZE), Z×len [CS↑]`. `len` ≤ free space in `rx_sram` (1024 B, `RXFF_SZ` 313); **each chunk is a separate CS-framed command** that re-sends opcode + continued address (`ind_op_next_byte` 427). Data reach the guest via the INDAC trigger window (`ospi_indac_read` 1640, `ospi_rx_sram_read` 1129) or the CSU DMA (`ospi_dma_read` 802). `num_bytes` not a multiple of 4 is logged (`ind_op_setup` 443-453). |
| Indirect write (`INDIRECT_WRITE_XFER_CTRL_REG.START`, reg 0x70) | `ind_wr_xfer_ctrl_reg_post_write` 1294 → `ospi_do_indirect_write` 965 → `ospi_ind_write` 904-939 | Unless `DEV_INSTR_WR_CONFIG_REG.WEL_DIS_FLD`: `[CS↓] 0x06 [CS↑]` (`ospi_transmit_wel` 886-902). Then `[CS↓] WR_OPCODE, A×n(DEV_SIZE), D×len [CS↑]`, `len` ≤ page size (`BYTES_PER_DEVICE_PAGE`, `ospi_get_page_sz` 407) and never crossing a page boundary (972-985). Data come from `tx_sram`, filled by guest writes into the INDAC window (`ospi_indac_write` 1651). |
| STIG (`FLASH_CMD_CTRL_REG.CMD_EXEC`, reg 0x90) | `flash_cmd_ctrl_reg_post_write` 1250 → `ospi_stig_cmd_exec` 1022-1082 | `[CS↓] CMD_OPCODE [, A×(NUM_ADDR_BYTES+1) if ENB_COMD_ADDR] [, D×(NUM_WR_DATA_BYTES+1) if ENB_WRITE_DATA] [, Z×(NUM_RD_DATA_BYTES+1, or membank size) if ENB_READ_DATA] [CS↑]`. Read data → `FLASH_RD_DATA_LOWER/UPPER` (≤ 8 B, `ospi_rx_fifo_pop_stig_rd_data` 718) or `stig_membank[512]` (`ospi_stig_fill_membank` 1005). Write data from `FLASH_WR_DATA_LOWER/UPPER` (≤ 8 B, 688-699). |
| DAC read (guest load in `0xC000_0000` window) | `ospi_dac_read` 1695 → `ospi_do_dac_read` 1160-1194 | `[CS↓] RD_OPCODE, A×n(DEV_SIZE), Z×size [CS↑]`, `size` ∈ {4, 8}: `ospi_dac_ops` (1758-1766) has `impl.min_access_size = 4, max = 8`, so byte/halfword guest accesses arrive as 4-byte transactions. One full command per access. Optional `REMAP_ADDR_REG` offset (1707-1709). |
| DAC write | `ospi_dac_write` 1721 → `ospi_do_dac_write` 1196-1233 | WREN framing as above (unless WEL_DIS), then `[CS↓] WR_OPCODE, A×n, D×size [CS↑]`, size 4 or 8. Write-protect check first (1740-1748). |
| Auto-polling / WIP (`WRITE_COMPLETION_CTRL_REG` 0x38, `POLLING_FLASH_STATUS_REG` 0xb0) | — | **Never transmitted.** Registers exist (134-145, 251-256); no code issues a status poll; writes complete synchronously and `IND_OPS_DONE` is set immediately. |
| XIP (`CONFIG_REG.ENTER_XIP_MODE*`) | — | Not modelled; DAC path is used regardless. |

### 2.3 What is never transmitted / ignored

Every field below has a `FIELD()` definition and **no other reference** in the
file (verified by grep):

* **Opcode extension byte**: `OPCODE_EXT_LOWER_REG` (0xe0;
  `EXT_READ/WRITE/POLL/STIG_OPCODE_FLD`, 298-302) and `OPCODE_EXT_UPPER_REG`
  (0xe4; `WEL_OPCODE_FLD`, `EXT_WEL_OPCODE_FLD`, 303-306) are plain storage
  (1599-1604). `CONFIG_REG.DUAL_BYTE_OPCODE_EN_FLD` (line 40) is never read.
  → **The extension byte is never sent**, and WREN does not honour
  `WEL_OPCODE_FLD` either.
* **Dummy cycles**: `DEV_INSTR_RD_CONFIG_REG.DUMMY_RD_CLK_CYCLES_FLD` (65),
  `DEV_INSTR_WR_CONFIG_REG.DUMMY_WR_CLK_CYCLES_FLD` (78),
  `FLASH_CMD_CTRL_REG.NUM_DUMMY_CYCLES_FLD` (241),
  `POLLING_FLASH_STATUS_REG.DEVICE_STATUS_NB_DUMMY` (253) — never read.
  → **Zero dummy bytes are sent**; data immediately follow the address. (The
  fork's `mt35xu01g` copes because its dummy count comes from
  `volatile_cfg_large[1]`, default 0 — §6.)
* **Mode bits**: `MODE_BIT_ENABLE_FLD` (66), `ENB_MODE_BIT_FLD` (237),
  `MODE_BIT_CONFIG_REG` — ignored.
* **Lane width / DTR**: `INSTR_TYPE_FLD` (74), `ADDR_XFER_TYPE_STD_MODE_FLD`
  (71/82), `DATA_XFER_TYPE_EXT_MODE_FLD` (69/80), `DDR_EN_FLD` (73),
  `CONFIG_REG.ENABLE_DTR_PROTOCOL_FLD` (44), `RD_DATA_CAPTURE_REG.DQS_ENABLE /
  DDR_READ_DELAY` (91-98) — ignored.
  → **The peripheral gets no lane-width or DTR information.** There is no
  side channel in the SSI API (`ssi_transfer()` carries one value, nothing
  else). The peripheral must derive its rate from protocol state it owns
  (e.g. Erbium `setRate`/strap), which is what the WIP header does
  (`cmd_rate/addr_rate/data_rate`).
* `DEV_DELAY_REG`, `MSTR_BAUD_DIV`, PHY/DLL registers: timing only.
  `phy_config_reg_postw` (1392-1405) mirrors DLL delays into
  `DLL_OBSERVABLE_UPPER_REG`; reset (1434-1456) sets DLL-lock bits so guest
  PHY-tuning loops terminate.

### 2.4 Xilinx-fork gotcha: `max-tap-dly-suspend`

`ospi_stig_cmd_exec()` 1022-1039: if property `max-tap-dly-suspend` (default
**true**, 1871) and `PHY_CONFIGURATION_REG.PHY_CONFIG_RX_DLL_DELAY_FLD > 64`
(`MAX_RX_DLL_DELAY`, 329), the STIG command is **silently skipped** (only
`-d guest_errors` shows it) and `FLASH_RD_DATA_*` is filled from stale
`rx_fifo` contents. Intended to make Xilinx PHY-tuning software see bad reads
at large tap values. If your device "stops receiving" STIG commands, check
this; disable with `-global xlnx.versal-ospi.max-tap-dly-suspend=false`.

### 2.5 Chip-select handling

* `ospi_update_cs_lines()` 596-612: with `CONFIG_REG.PERIPH_SEL_DEC_FLD`
  (decoder mode) the 4-bit `PERIPH_CS_LINES_FLD` (bits 13:10) is driven as-is
  onto the four gpio-outs (bit = line level; 1 = deasserted); otherwise
  `single_cs()` 575-594 turns it into a one-low pattern (rightmost 0 selects:
  `0b1110`→CS0, `0b1101`→CS1, …, `0b1111`→none).
* `ospi_disable_cs()` 623-630: all four lines → 1.
* `ospi_dac_cs()` 614-621: with `CONFIG_REG.ENABLE_AHB_DECODER_FLD`,
  `ospi_ahb_decoder_enable_cs()` 564-573 picks the CS from the address using
  per-CS sizes `DEV_SIZE_CONFIG_REG.MEM_SIZE_ON_CSn` (`flash_sz` 516-525:
  512 Mbit/1 G/2 G/4 G).
* Lines are **active-low**, so peripherals use `cs_polarity = SSI_CS_LOW`;
  the base `ssi_cs_default()` (`hw/ssi/ssi.c:72-83`) invokes `set_cs()` only
  on a level *change*, so the peripheral sees clean edges even though the
  controller re-drives the same level repeatedly.
* Every transaction in §2.2 is `update_cs_lines → flush → disable_cs`, so
  the peripheral observes `set_cs(select=false /*asserted*/)`, N × `transfer()`,
  `set_cs(select=true /*deasserted*/)` **once per command** and can reset its
  parser on deassert exactly as `m25p80_cs()` does (§4.2). (In
  `ospi_stig_cmd_exec` CS is asserted at 1056 before the data bytes are
  queued, but nothing is flushed until 1074, so the peripheral sees no
  difference.)

### 2.6 DAC enable gating

`ospi_dac_read/write` (1695-1756) require `CONFIG_REG.ENB_SPI_FLD`,
`CONFIG_REG.ENB_DIR_ACC_CTLR_FLD` **and** `s->dac_enable`. `dac_enable` is the
`ospi-mux-sel` gpio input (`ospi_update_dac_status` 1768-1773; declared
1830-1831), driven by PMC IOU SLCR `OSPI_QSPI_IOU_AXI_MUX_SEL` @ `0xF1060504`
bit 1 (`hw/misc/xlnx-versal-pmc-iou-slcr.c:628-629, 882-889`; register
reset value 0x1 → bit 1 = 0). **DAC is disabled at reset**: a guest/qtest must
write bit 1 of `0xF1060504` before linear accesses work; otherwise they log
"OSPI AHB rd while DAC disabled" and return 0. Indirect and STIG paths don't
depend on it. The INDAC trigger window inside the DAC region is defined by
`IND_AHB_ADDR_TRIGGER_REG` (0x1c) / `INDIRECT_TRIGGER_ADDR_RANGE_REG` (0x80)
(1666-1693).

### 2.7 Recommended minimal OSPI-model patch: emit ext-opcode and dummy bytes

> **Status:** applied (near-verbatim) by commit `4b67c78` as the
> `faithful-frames` property of `xlnx.versal-ospi`, plus an extra tweak in
> `ospi_indac_write` (accumulate a full page before framing an indirect
> write). Kept here as the rationale.

Goal: (i) send the opcode extension byte when 2-byte-opcode mode is on, and
(ii) send N dummy *bytes* derived from the dummy clock cycles, so a
byte-stream peripheral sees the real frame.

Rule: `dummy_bytes = cycles × lanes / 8`, ×2 in DTR (each clock edge pair
moves `lanes` bits, twice that in DTR). Lanes come from the `*_XFER_TYPE`
fields (0 = 1 lane, 1 = 2, 2 = 4, 3 = 8; the encoding Linux writes via
`CQSPI_INST_TYPE_*`). The Cadence IP clocks the dummy phase at the data lane
width, so use `DATA_XFER_TYPE_EXT_MODE_FLD` (in 8D-8D-8D all three are 8).

Because the fork's `mt35xu01g` expects *no* dummy bytes (default
`volatile_cfg_large[1] == 0`), keep the new behaviour **opt-in** behind a
device property (`faithful-frames`), enabled for erbium with
`-global xlnx.versal-ospi.faithful-frames=on`, so the default machine is
unchanged.

```diff
--- a/include/hw/ssi/xlnx-versal-ospi.h
+++ b/include/hw/ssi/xlnx-versal-ospi.h
@@ -97,6 +97,8 @@ struct XlnxVersalOspi {
     bool dac_enable;
     bool src_dma_inprog;
     bool max_tap_dly_suspend;
+    /* Emit opcode-extension and dummy bytes on the SSI bus */
+    bool faithful_frames;
 
     IndOp rd_ind_op[2];
     IndOp wr_ind_op[2];
--- a/hw/ssi/xlnx-versal-ospi.c
+++ b/hw/ssi/xlnx-versal-ospi.c
@@ -368,6 +368,66 @@ static void ospi_update_irq_line(XlnxVersalOspi *s)
 }
 
+/*
+ * Frame helpers.  The SSI bus only carries bytes, so lane width and DTR
+ * are folded into the *number* of bytes clocked during the dummy phase.
+ */
+static bool ospi_dual_byte_opcode(XlnxVersalOspi *s)
+{
+    return s->faithful_frames &&
+           ARRAY_FIELD_EX32(s->regs, CONFIG_REG, DUAL_BYTE_OPCODE_EN_FLD);
+}
+
+static unsigned int ospi_lanes(unsigned int xfer_type)
+{
+    return 1u << (xfer_type & 3);       /* 0:1, 1:2, 2:4, 3:8 lanes */
+}
+
+static unsigned int ospi_dummy_bytes(XlnxVersalOspi *s, unsigned int cycles,
+                                     unsigned int xfer_type)
+{
+    unsigned int bits;
+
+    if (!s->faithful_frames) {
+        return 0;
+    }
+    bits = cycles * ospi_lanes(xfer_type);
+    if (ARRAY_FIELD_EX32(s->regs, CONFIG_REG, ENABLE_DTR_PROTOCOL_FLD)) {
+        bits *= 2;
+    }
+    return bits / 8;
+}
+
+static void ospi_tx_fifo_push_dummy(XlnxVersalOspi *s, unsigned int n)
+{
+    while (n-- && !fifo8_is_full(&s->tx_fifo)) {
+        fifo8_push(&s->tx_fifo, 0);
+    }
+}
+
+static void ospi_tx_fifo_push_opcode(XlnxVersalOspi *s, uint8_t op,
+                                     uint8_t ext)
+{
+    fifo8_push(&s->tx_fifo, op);
+    if (ospi_dual_byte_opcode(s)) {
+        fifo8_push(&s->tx_fifo, ext);
+    }
+}
+
+static unsigned int ospi_rd_dummy_bytes(XlnxVersalOspi *s)
+{
+    return ospi_dummy_bytes(s,
+        ARRAY_FIELD_EX32(s->regs, DEV_INSTR_RD_CONFIG_REG,
+                         DUMMY_RD_CLK_CYCLES_FLD),
+        ARRAY_FIELD_EX32(s->regs, DEV_INSTR_RD_CONFIG_REG,
+                         DATA_XFER_TYPE_EXT_MODE_FLD));
+}
+
+static unsigned int ospi_wr_dummy_bytes(XlnxVersalOspi *s)
+{
+    return ospi_dummy_bytes(s,
+        ARRAY_FIELD_EX32(s->regs, DEV_INSTR_WR_CONFIG_REG,
+                         DUMMY_WR_CLK_CYCLES_FLD),
+        ARRAY_FIELD_EX32(s->regs, DEV_INSTR_WR_CONFIG_REG,
+                         DATA_XFER_TYPE_EXT_MODE_FLD));
+}
+
 static uint8_t ospi_get_wr_opcode(XlnxVersalOspi *s)
@@ -675,13 +735,16 @@ static void ospi_tx_fifo_push_rd_op_addr(XlnxVersalOspi *s, uint32_t flash_addr)
     fifo8_reset(&s->tx_fifo);
 
     /* Push read opcode */
-    fifo8_push(&s->tx_fifo, inst_code);
+    ospi_tx_fifo_push_opcode(s, inst_code,
+        ARRAY_FIELD_EX32(s->regs, OPCODE_EXT_LOWER_REG, EXT_READ_OPCODE_FLD));
 
     /* Push read address */
     ospi_tx_fifo_push_address(s, flash_addr);
+
+    /* Dummy cycles, expressed as bytes */
+    ospi_tx_fifo_push_dummy(s, ospi_rd_dummy_bytes(s));
 }
@@ -886,8 +949,11 @@ static void ospi_transmit_wel(XlnxVersalOspi *s, bool ahb_decoder_cs,
     fifo8_reset(&s->tx_fifo);
-    fifo8_push(&s->tx_fifo, WREN);
+    ospi_tx_fifo_push_opcode(s,
+        s->faithful_frames ? ARRAY_FIELD_EX32(s->regs, OPCODE_EXT_UPPER_REG,
+                                              WEL_OPCODE_FLD) : WREN,
+        ARRAY_FIELD_EX32(s->regs, OPCODE_EXT_UPPER_REG, EXT_WEL_OPCODE_FLD));
@@ -918,10 +984,13 @@ static void ospi_ind_write(XlnxVersalOspi *s, uint32_t flash_addr, uint32_t len)
     inst_code = ospi_get_wr_opcode(s);
-    fifo8_push(&s->tx_fifo, inst_code);
+    ospi_tx_fifo_push_opcode(s, inst_code,
+        ARRAY_FIELD_EX32(s->regs, OPCODE_EXT_LOWER_REG, EXT_WRITE_OPCODE_FLD));
 
     /* Push write address */
     ospi_tx_fifo_push_address(s, flash_addr);
+    ospi_tx_fifo_push_dummy(s, ospi_wr_dummy_bytes(s));
@@ -1046,13 +1115,20 @@ static void ospi_stig_cmd_exec(XlnxVersalOspi *s)
     inst_code = ARRAY_FIELD_EX32(s->regs, FLASH_CMD_CTRL_REG, CMD_OPCODE_FLD);
-    fifo8_push(&s->tx_fifo, inst_code);
+    ospi_tx_fifo_push_opcode(s, inst_code,
+        ARRAY_FIELD_EX32(s->regs, OPCODE_EXT_LOWER_REG, EXT_STIG_OPCODE_FLD));
 
     /* Push address if enabled */
     if (ARRAY_FIELD_EX32(s->regs, FLASH_CMD_CTRL_REG, ENB_COMD_ADDR_FLD)) {
         ospi_tx_fifo_push_stig_addr(s);
     }
+    /* STIG dummy cycles are clocked at the read-instruction data width */
+    if (!ARRAY_FIELD_EX32(s->regs, FLASH_CMD_CTRL_REG, ENB_WRITE_DATA_FLD)) {
+        ospi_tx_fifo_push_dummy(s, ospi_dummy_bytes(s,
+            ARRAY_FIELD_EX32(s->regs, FLASH_CMD_CTRL_REG, NUM_DUMMY_CYCLES_FLD),
+            ARRAY_FIELD_EX32(s->regs, DEV_INSTR_RD_CONFIG_REG,
+                             DATA_XFER_TYPE_EXT_MODE_FLD)));
+    }
@@ -1210,7 +1286,9 @@ static void ospi_do_dac_write(void *opaque,
     inst_code = ospi_get_wr_opcode(s);
-    fifo8_push(&s->tx_fifo, inst_code);
+    ospi_tx_fifo_push_opcode(s, inst_code,
+        ARRAY_FIELD_EX32(s->regs, OPCODE_EXT_LOWER_REG, EXT_WRITE_OPCODE_FLD));
 
     /* Push write address */
     ospi_tx_fifo_push_address(s, addr);
+    ospi_tx_fifo_push_dummy(s, ospi_wr_dummy_bytes(s));
@@ -1868,6 +1946,8 @@ static const Property xlnx_versal_ospi_properties[] = {
     DEFINE_PROP_BOOL("max-tap-dly-suspend", XlnxVersalOspi,
                     max_tap_dly_suspend, true),
+    DEFINE_PROP_BOOL("faithful-frames", XlnxVersalOspi,
+                     faithful_frames, false),
 };
```

Hunk line numbers are approximate (file untouched). `ospi_do_dac_read` needs
no change (it calls `ospi_tx_fifo_push_rd_op_addr`). No VMState bump
(property, not state). If you want it always-on for the erbium machine
instead of via `-global`, set the property in `versal_create_ospi()`.

Sanity check for Erbium: 8D-8D-8D, `DUMMY_RD_CLK_CYCLES = 8` → 8×8×2/8 =
**16 dummy bytes**; 8S: 8 bytes; 1S-1S-1S with 8 cycles: 1 byte (matches the
"8 dummy cycles = 1 byte" convention of `m25p80` SPI parts,
`decode_fast_read_cmd` `m25p80.c:1088-1137`). In 8D mode with a
`DUMMY_RD_CLK_CYCLES` that is odd the truncation `bits/8` drops a nibble —
acceptable, real controllers need even DTR counts anyway.

Caveat for the peripheral: in DTR the Cadence IP always clocks 4 address
bytes; the model just follows `NUM_ADDR_BYTES_FLD`. Linux programs
`NUM_ADDR_BYTES_FLD = 3` (4 bytes) for 8D ops so this is consistent in
practice.

---

## 3. SSI peripheral API

`include/hw/ssi/ssi.h`

* `TYPE_SSI_PERIPHERAL` (20), `SSIPeripheralClass` (33-58):
  * `realize(SSIPeripheral *, Error **)` — called by the base
    `ssi_peripheral_realize()` (`ssi.c:97-109`) after it caches the class
    pointer and (when `cs_polarity != SSI_CS_NONE` and `transfer_raw` is the
    default) creates the **named gpio-in `"ssi-gpio-cs"`** (`SSI_GPIO_CS`,
    header 24; `ssi.c:104`). Use `k->realize`, not `dc->realize`.
  * `transfer(dev, uint32_t val)` — one call per word, **only while selected**
    per `cs_polarity` (`ssi_transfer_raw_default`, `ssi.c:85-95`); when
    deselected the bus sees 0 without calling you.
  * `set_cs(dev, bool select)` — optional; called from `ssi_cs_default()`
    (`ssi.c:72-83`) on a *level change* only. **`select` is the raw gpio
    level**: for `SSI_CS_LOW` devices `select == true` means *deasserted*.
    `SSIPeripheral.cs` holds the level.
  * `cs_polarity`: `SSI_CS_NONE` (always selected, no gpio created),
    `SSI_CS_LOW`, `SSI_CS_HIGH` (26-30).
  * `transfer_raw` — only for non-standard CS semantics; then no gpio and
    `transfer/set_cs/cs_polarity` are unused.
* `SSIPeripheral` (60-70): `bool cs`, `uint8_t cs_index` (qdev property
  **`cs`**, `ssi.c:111-113`; used by `ssi_get_cs()` 30-43 and the duplicate
  check `ssi_bus_check_address()` 45-56).
* `VMSTATE_SSI_PERIPHERAL(parent_obj, MyState)` (74-80) embeds
  `vmstate_ssi_peripheral` (`ssi.c:170-178`, saves `cs`).
* Creation: `ssi_create_peripheral(bus, type)` (`ssi.c:141-147`) =
  `qdev_new()` + `ssi_realize_and_unref()` (136-139 — just
  `qdev_realize_and_unref(dev, &bus->parent_obj, errp)`). The Versal code
  uses the explicit `qdev_new` / set props / `qdev_realize_and_unref(dev, spi_bus)`
  form (§1.3). Master side: `ssi_create_bus(parent, "spi0")` (149-154).
* `ssi_transfer(bus, val)` (156-168) ORs the `transfer_raw` results of
  **all** children — a deselected device must contribute 0 (the default
  wrapper does that).
* `dc->bus_type = TYPE_SSI_BUS` is set by the base class (`ssi.c:121`);
  `TypeInfo.parent = TYPE_SSI_PERIPHERAL`; class size is inherited.

(The landed `hw/ssi/erbium-xspi.c:719-728` already follows this:
`k->realize/transfer/set_cs`, `cs_polarity = SSI_CS_LOW`.)

### 3.1 meson / Kconfig

`hw/ssi/meson.build`: one `system_ss.add(when: 'CONFIG_X', if_true: files('x.c'))`
per device (OSPI controller: `when: 'CONFIG_XLNX_VERSAL'`, line 12; erbium
added at line 13 by 0ed6dbd). `hw/ssi/Kconfig`: `config NAME` / `bool` /
`select SSI`. Enable it for the target either with `default y` (as 0ed6dbd
does — note the `depends on SSI` cycle, §0.2) or, more conventionally,
`select ERBIUM_XSPI` under `config XLNX_VERSAL` in `hw/arm/Kconfig`, or
`CONFIG_ERBIUM_XSPI=y` in `configs/devices/aarch64-softmmu/default.mak`.
Trace points: `hw/ssi/trace-events` (already wired into `trace.h`; 0ed6dbd
added `erbium_xspi_*` entries). No extra meson deps are needed for
`chardev`/`hostmem` — both are already part of `system_ss` (ivshmem uses both).

---

## 4. How `m25p80` is built (`hw/block/m25p80.c`)

### 4.1 State machine

`enum` 474-482: `STATE_IDLE, STATE_PAGE_PROGRAM, STATE_READ,
STATE_COLLECTING_DATA, STATE_COLLECTING_VAR_LEN_DATA, STATE_READING_DATA,
STATE_READING_SFDP`. `m25p80_transfer8()` (1715-1805) is a `switch (s->state)`:

* `STATE_IDLE` → `decode_new_cmd(s, byte)` (1246-1693): big switch on opcode.
  Commands with an address/arguments set `s->needed_bytes` (address length
  from `get_addr_length()` 742-779 — 3 or 4 depending on opcode, 4-byte mode
  flag, or Micron-octal DDR config — plus vendor-specific dummy **bytes**,
  e.g. `decode_fast_read_cmd()` 1088-1137) and enter `STATE_COLLECTING_DATA`;
  commands that return data fill `s->data[]`, set `s->len`, and enter
  `STATE_READING_DATA`.
* `STATE_COLLECTING_DATA[_VAR_LEN]` (1748-1767): store into `s->data[len++]`
  (bounded by `M25P80_INTERNAL_DATA_BUFFER_SZ`; overrun → log + IDLE); when
  `len == needed_bytes`, `complete_collecting_data()` (781-974) parses the
  address MSB-first from `data[0..n)` into `cur_addr` (masked to size) and
  dispatches on `cmd_in_progress` (→ `STATE_PAGE_PROGRAM`, `STATE_READ`,
  `STATE_READING_DATA`, `STATE_READING_SFDP`, erase, register writes).
  `_VAR_LEN` completes on CS deassert instead.
* `STATE_PAGE_PROGRAM` (1725-1740): each byte → `flash_write8()` (692-740),
  `cur_addr++` wrapping at size.
* `STATE_READ` (1742-1746): return `storage[cur_addr++]`.
* `STATE_READING_DATA` (1769-1790): return `data[pos++]`; back to IDLE at
  `pos == len` unless `data_read_loop` (ID reads wrap).
* `STATE_READING_SFDP` (1791-1797): `pi->sfdp_read(cur_addr++)`.

Return value is 0 unless in a reading state; only the low 8 bits of `tx` are
used.

### 4.2 CS deassert

`m25p80_cs()` (1695-1713): on `select == true` (gpio high = deasserted for
`SSI_CS_LOW`): finish a var-len command if pending, then `len = pos = 0;
state = STATE_IDLE; flash_sync_dirty(s, -1); data_read_loop = false`. On
assertion nothing happens. This makes each command self-contained between CS
edges — mirror it.

### 4.3 Storage and block backend

* Property `DEFINE_PROP_DRIVE("drive", Flash, blk)` (1914), set by the board
  via `qdev_prop_set_drive_err()`.
* `m25p80_realize()` (1824-1879): `size = sector_size * n_sectors`; with a
  drive: `blk_set_perm(blk, CONSISTENT_READ | (writable ? WRITE : 0), BLK_PERM_ALL)`,
  `storage = blk_blockalign(blk, size)`, then the **whole image is read into
  RAM once** with `blk_pread(blk, 0, size, storage, 0)` (1866; the
  `/* Xilinx */` comment marks a fork tweak of that error path). Without a
  drive: `blk_blockalign(NULL, size)` + `memset 0xFF`. Freed in
  `m25p80_exit()` (1807).
* Write-back: `flash_write8()` writes into `storage` (AND for NOR semantics;
  plain store for EEPROM parts) then `flash_sync_dirty(s, page)` (685-690):
  if a *different* page was dirty it is written back by `flash_sync_page()`
  (602-616) with `blk_aio_pwritev()` on a `QEMUIOVector` pointing straight
  into `storage`; completion `blk_sync_complete()` (589-600) just frees the
  iov. At most one dirty page is pending; it is flushed on page change, on CS
  deassert, and in `m25p80_pre_save()` (1896-1901). Erases use
  `flash_sync_area()` (618-631). All sync paths are no-ops when
  `!blk || !blk_is_writable(blk)`.

### 4.4 VMState

`vmstate_m25p80` (2048-2085): `.version_id = 0`, `.pre_save` (flush dirty
page), `.pre_load` (1917); fields `state`, `data[]`, `len`, `pos`,
`needed_bytes`, `cmd_in_progress`, `cur_addr`, flags and config registers;
feature subsections with `.needed` predicates (1947-2046). `storage` is
**not** migrated (it lives in the block backend). m25p80 does not embed
`VMSTATE_SSI_PERIPHERAL`; a new device should put
`VMSTATE_SSI_PERIPHERAL(parent_obj, ErbiumXSPI)` first. (0ed6dbd currently
sets `dc->vmsd = NULL`, i.e. no migration — fine for now, but note that
`memory-backend-file,share=on` data survives anyway.)

### 4.5 m25p80 and HostMemoryBackend

m25p80 never references `TYPE_MEMORY_BACKEND` / `host_memory_backend_*` /
`memory_region_get_ram_ptr` (grep confirms) — it is malloc + BlockBackend.

Pattern for a `memdev` link elsewhere in this tree (and used by 0ed6dbd,
`erbium-xspi.c:670-676, 705-706`):

* `hw/misc/ivshmem-pci.c:1039-1040`:
  `DEFINE_PROP_LINK("memdev", IVShmemState, hostmem, TYPE_MEMORY_BACKEND, HostMemoryBackend *)`
  (`#include "system/hostmem.h"` — 10.x path, not `sysemu/`).
  `hw/mem/pc-dimm.c:158-159` does the same. Older explicit form:
  `object_property_add_link(obj, "memdev", TYPE_MEMORY_BACKEND, (Object **)&s->hostmem, object_property_allow_set_link, OBJ_PROP_LINK_STRONG)`
  (compare `dma-src` in `xlnx-versal-ospi.c:1834-1837`).
* Realize (`ivshmem-pci.c:869-873`, `nvdimm.c:130-135`):
  `mr = host_memory_backend_get_memory(s->hostmem);`
  `host_memory_backend_set_mapped(s->hostmem, true);`
  `p = memory_region_get_ram_ptr(mr);` + `memory_region_size(mr)` for the
  size check; `host_memory_backend_is_mapped()` guard first
  (`ivshmem-pci.c:1050`, `pc-dimm.c:207`); unmap in unrealize
  (`ivshmem-pci.c:955`).
* Migration: `vmstate_register_ram(mr, DEVICE(s))` (`ivshmem-pci.c:922`) so
  the backend RAM is a migratable RAM block. With
  `memory-backend-file,share=on` the content is simply the file, which is
  the mode that makes the 16 MiB MRAM inspectable from the host with no
  `blk_*`/dirty-page code at all.
* CLI: `-object memory-backend-file,id=mram,size=16M,mem-path=…,share=on`
  (or `memory-backend-ram`) + `memdev=mram` on the device.

---

## 5. qtests

### 5.1 aspeed_smc-test / aspeed-smc-utils

`tests/qtest/aspeed_smc-test.c` (231 lines) + `aspeed-smc-utils.{c,h}`:

* Each board function (e.g. `test_palmetto_bmc`, 32-76) creates a temp file
  (`g_file_open_tmp` + `ftruncate` to the flash size), starts QEMU with
  `qtest_initf("-m 256 -machine palmetto-bmc -drive file=%s,format=raw,if=mtd", path)`
  and fills an `AspeedSMCTestData` (`aspeed-smc-utils.h:71-80`): controller
  reg base `spi_base`, memory-mapped flash window `flash_base`, expected
  JEDEC id, `cs`, the flash **QOM path** (`"/machine/soc/fmc/ssi.0/child[0]"`)
  and a page address. Tests are registered with
  `qtest_add_data_func("/ast2400/smc/read_jedec", data, aspeed_smc_test_read_jedec)`;
  `main()` (207-230) runs `g_test_run()`, then `qtest_quit()` + `unlink`.
* The utils drive the controller only through MMIO (`qtest_writel/readl`,
  `aspeed-smc-utils.c:41-74`). They put the Aspeed controller into "user
  mode" (`spi_ctrl_start_user` 110-120, asserts CS); then each
  `flash_writeb(data, 0, byte)` into the flash window shifts one byte onto
  the SSI bus and each `flash_readb` shifts one byte in; `spi_ctrl_stop_user`
  (122-129) deasserts CS. E.g. `aspeed_smc_test_read_jedec` (209-226): start
  user, write 0x9F, read 3 bytes, stop user, compare. `read_page()` (158-174)
  sends `EN_4BYTE_ADDR, READ, addr(be32)` and reads 256 bytes;
  write/erase tests send `WREN, PP/ERASE, addr, data` (228-328).
  `read_page_mem()` (176-187) switches the controller to hardware read mode
  and reads through the memory window — the analogue of the OSPI DAC path.
* QOM/gpio poking: `qtest_qom_get_bool(s, node, "write-enable")` (459-480)
  and `qtest_set_irq_in(s, node, "WP#", 0, level)` (515, 538).
* The aarch64 twin `ast2700-smc-test.c` (registered under `qtests_aspeed64`,
  `meson.build:224`) is the one that builds with an aarch64-only tree.

### 5.2 Existing Versal qtests — no OSPI one

`grep -il ospi tests/qtest/*` is empty: **no OSPI/xSPI qtest** exists.
Versal qtests present: `xlnx-versal-trng-test.c`
(`qtest_start("-machine xlnx-versal-virt")` line 97; regs `0xf1230000`;
QOM path `/machine/xlnx-versal/trng` line 107) and `xlnx-canfd-test.c`
(`qtest_init("-machine xlnx-versal-virt -machine canbus0=canbus …")`, 290-293),
both in `qtests_aarch64` (`meson.build:262`). Good boilerplate templates.

### 5.3 Sketch of `tests/qtest/erbium-xspi-test.c`

Register in `meson.build` under `qtests_aarch64`:
`(config_all_devices.has_key('CONFIG_ERBIUM_XSPI') ? ['erbium-xspi-test'] : [])`.

```c
#define OSPI_BASE   0xf1010000ULL
#define OSPI_DAC    0xc0000000ULL
#define SLCR_MUX    0xf1060504ULL        /* OSPI_QSPI_IOU_AXI_MUX_SEL, bit1 = OSPI_MUX_SEL */

qts = qtest_initf("-machine xlnx-versal-virt,ospi-flash=erbium-xspi "
                  "-object memory-backend-file,id=mram,size=16M,mem-path=%s,share=on "
                  "-global erbium-xspi.memdev=mram", path);

/* CONFIG_REG (0x00): ENB_SPI(bit0) | ENB_DIR_ACC(bit7) | PERIPH_CS_LINES=0b1110 (bits13:10) */
qtest_writel(qts, OSPI_BASE + 0x00, BIT(0) | BIT(7) | (0xe << 10));
/* DEV_SIZE_CONFIG (0x14): NUM_ADDR_BYTES=3 (=4 bytes), page=256 (bits15:4), subsector=2^16 */
qtest_writel(qts, OSPI_BASE + 0x14, 3 | (256 << 4) | (16 << 16));

/* STIG (0x90): opcode<<24 | ENB_READ(23) | NUM_RD-1 (22:20) | ENB_ADDR(19) | NUM_ADDR-1 (17:16) | EXEC(0) */
qtest_writel(qts, OSPI_BASE + 0x94, reg_addr);                       /* FLASH_CMD_ADDR */
qtest_writel(qts, OSPI_BASE + 0x90, (0x65u << 24) | BIT(23) | (7 << 20) | BIT(19) | (3 << 16) | 1);
lo = qtest_readl(qts, OSPI_BASE + 0xa0); hi = qtest_readl(qts, OSPI_BASE + 0xa4);

/* DAC: DEV_INSTR_RD_CONFIG (0x04): opcode | dummy<<24; then enable mux and just load */
qtest_writel(qts, OSPI_BASE + 0x04, 0x0B | (dummy_cycles << 24));
qtest_writel(qts, SLCR_MUX, 0x3);
v = qtest_readl(qts, OSPI_DAC + off);

/* Indirect read: 0x68 start addr, 0x6c num bytes, 0x1c trigger addr, 0x60 START(bit0);
 * then read data from OSPI_DAC + (trigger - 0xc0000000). */
```

Bit positions from the `FIELD()` definitions in `xlnx-versal-ospi.c` 38-58
(CONFIG), 100-108 (DEV_SIZE), 232-245 (FLASH_CMD_CTRL). Until §2.7 is
applied, use `dummy_cycles = 0` and `-global erbium-xspi.latency-bytes=0`
(property exists in 0ed6dbd) or the data will be shifted.

---

## 6. Xilinx-fork specifics relevant to OSPI / xSPI

Files mentioning `ospi`/`xspi` (`grep -rli`): `hw/ssi/xlnx-versal-ospi.c`,
`hw/arm/xlnx-versal{,-virt}.c`, `hw/misc/xlnx-versal-pmc-iou-slcr*.c`,
`hw/misc/xlnx-versal-pmx-*.c` (Versal2 SLCR/CRP: mux-sel and reset bits),
`hw/misc/xlnx-versal-pmc-clk-rst*.c` (clock enables), plus the new
`hw/ssi/erbium-xspi*.c` / `include/hw/ssi/erbium-xspi.h`. No other
controller in `hw/ssi/` implements octal/DTR (`xilinx_spips.c` = ZynqMP GQSPI
up to quad; `xlnx-axiqspi.c` = AXI Quad SPI; "octal"/"dtr" only appear in
comments/register names there).

Fork additions in `xlnx-versal-ospi.c` vs. upstream (no upstream history in
the shallow clone; from knowledge of upstream 10.x):

* `max-tap-dly-suspend` + `MAX_RX_DLL_DELAY` STIG suppression (§2.4).
* `phy_config_reg_postw()` / `dll_obs_upper_reg_post_read()` DLL-observable
  emulation (1392-1432) and DLL-lock bits at reset (1452-1455).
* `stig_membank[512]` + `FLASH_COMMAND_CTRL_MEM_REG` (STIG reads > 8 bytes;
  390-405, 1005-1020, 1235-1248).
* Two-deep indirect op queues (`rd_ind_op[2]`, `wr_ind_op[2]`, `IND_OPS_DONE_MAX`).
* `dac-with-indac` / `indac-write-disabled` properties, named `ospi-mux-sel` gpio.

Fork additions in `hw/block/m25p80.c`: `MAN_MICRON_OCTAL` (492) with 256-byte
volatile/non-volatile config arrays (`MICRON_OCTAL_CFG_SIZE` 502;
`nv-cfg-large-stage` property 1911), parts `mt35xu01g`, `mt35xu01gbba`,
`mt35xu02gbba` with SFDP tables (269-283), `VCFG_IO_MODE_OCTAL_DDR[_DQS]`
(156-157) forcing 4-byte addresses in `get_addr_length()` (772-777),
`OOR4_MT35X` (411), and — key for §2.3 — the fast-read dummy byte count for
these parts is `volatile_cfg_large[1]`, **defaulting to 0** (1848-1849): the
fork keeps controller and flash consistent by telling the *flash* to expect
no dummy bytes rather than making the controller send them.

`versal_ospi_create_flash()`, the `ospi-flash` machine property and the 4-CS
loop are fork-only too (upstream hard-codes one `mt35xu01g` on CS0).

---

## 7. Linux `cadence-quadspi` (spi-mem) expectations vs. this model

From knowledge of `drivers/spi/spi-cadence-quadspi.c` (6.x); verify against
the guest kernel you actually use.

* **DT node needed** (QEMU generates none, §1.3). Template for an external
  DTB, or to add in `versal_create_ospi()` with
  `versal_fdt_add_simple_subnode()` like `sdhci` (`xlnx-versal.c:1304-1312`):

  ```
  ospi: spi@f1010000 {
      compatible = "cdns,qspi-nor";          /* generic; see below re. xlnx,versal-ospi-1.0 */
      reg = <0 0xf1010000 0 0x10000>, <0 0xc0000000 0 0x20000000>;
      interrupts = <GIC_SPI 124 IRQ_TYPE_LEVEL_HIGH>;
      clocks = <&clk125>;
      cdns,fifo-depth = <256>; cdns,fifo-width = <4>;
      cdns,trigger-address = <0xc0000000>;
      #address-cells = <1>; #size-cells = <0>;
      flash@0 { compatible = "jedec,spi-nor"; reg = <0>;
                spi-max-frequency = <20000000>;
                spi-tx-bus-width = <8>; spi-rx-bus-width = <8>; };
  };
  ```

  `compatible = "xlnx,versal-ospi-1.0"` enables `CQSPI_HAS_DMA` +
  `versal_ospi_reset` / `zynqmp_pm_ospi_mux_select()` which go through the
  Xilinx PM firmware (ATF SMC); without ATF the probe fails. With the generic
  compatible the driver uses indirect mode and, if the second `reg` window is
  present (and the ddata lacks `CQSPI_DISABLE_DAC_MODE`), **direct (DAC) mode
  for reads/writes ≥ 4 bytes** — which in QEMU needs `OSPI_MUX_SEL = 1`
  (§2.6). Nothing in a generic boot writes `0xF1060504`, so either: drop the
  second `reg` entry (forces indirect mode), have the machine start with
  `dac_enable = true`, or have firmware set it.
* Op → controller path: `cqspi_exec_mem_op()` → data ops with an address go
  to `cqspi_read()`/`cqspi_write()` (program `DEV_INSTR_RD/WR_CONFIG`
  incl. opcode, `INSTR_TYPE`, `ADDR/DATA_XFER_TYPE`, `DUMMY_*_CLK_CYCLES`,
  `DDR_EN`; `cqspi_setup_opcode_ext()` writes `OPCODE_EXT_LOWER` when
  `op->cmd.nbytes == 2`; `NUM_ADDR_BYTES`; then indirect or direct transfer).
  Ops without data or with ≤ 8 data bytes and no address
  (`CQSPI_STIG_DATA_LEN_MAX`) go to `cqspi_command_read/write()` (STIG),
  dummy → `FLASH_CMD_CTRL_REG.NUM_DUMMY_CYCLES` via `cqspi_calc_dummy()`
  (`dummy.nbytes*8/buswidth`, halved for DTR). `cqspi_enable_dtr()` sets
  `CONFIG_REG.ENABLE_DTR_PROTOCOL` and `DUAL_BYTE_OPCODE_EN`.
  **All of these are ignored by the model today** (§2.3); with §2.7 the byte
  stream matches what the erbium device expects.
* `spi-nor` will first issue `RDID 0x9F`, `RDSFDP 0x5A` (3-byte addr + 8
  dummy cycles), `RDSR 0x05`, … A non-JEDEC device must answer those
  plausibly (the WIP header has `ERB_OP_READ_SFDP 0x5A` and an SFDP table in
  `erbium-xspi-sfdp.c`), or a custom spi-mem client driver is needed instead
  of `spi-nor`.
* `supports_op` limits in the driver: `addr.nbytes ≤ 4`, no mixed data
  directions; the model's STIG path caps write data at 8 bytes and read data
  at 8 (or 512 with the fork's membank).
