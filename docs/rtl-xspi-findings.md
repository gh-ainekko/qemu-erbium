# Erbium xSPI target — RTL ground truth (for the QEMU model)

Source: `ext/core-et-erbium` @ afa22ae (2026-06-23). All paths below are relative to that tree.
The xSPI IP is written in Bluespec; `ip/xspi/bsv/*.bsv` is the readable source, `ip/xspi/verilog/*.v` is the
generated netlist that is actually integrated (`ip/xspi/verilog/rtl.f`). Line refs are to the BSV unless noted.
Host-side reference behaviour comes from the vendored BFM `vendor/cocotbext-xspi/src/cocotbext/xspi/*.py`,
which is what all passing tests use.

Legend: **CONFIRMS** doc, **CONTRADICTS** doc/review, **REFINES** (detail not in docs).

---

## 1. Module / ports

* IP top: `mkxspi` — `ip/xspi/bsv/xspi.bsv:42` (`ip/xspi/verilog/mkxspi.v`). Sub-blocks: `ddr_i.v` (input deserialiser,
  `ip/xspi/verilog/ddr_i.v`), `ddr_o.v`, `rwds_o.v`, `mkConfigCSR_SCCR_Reg` (SCCR), `mkConfigCSR_SFDP_Reg` (SFDP ROM).
* Interface `XSPI_Ifc` — `ip/xspi/bsv/xSPITypes.bsv:53-58`:
  * `axi`: AXI4 **master**, 32-bit addr, 64-bit data, ID width 0 (`Ifc_axi4_master#(0,32,64,0)`).
  * `apb`: APB **slave**, 32-bit addr, 64-bit data (`Ifc_apb_slave#(32,64,0)`) — CPU access to SCCR.
  * `xspi`: `dq[7:0]` in, `rwds` in, `csn` in, `dq_out[7:0]`+`dq_out_ena`, `rwds_out`+`rwds_out_ena` (`xSPITypes.bsv:19-35`).
  * `cfg`: `default_mode[1:0]` in; outputs `deep_power_down`, `ultra_deep_power_down`, `drive_strength[2:0]`,
    `use_xspi_clk`, `reset_device`, `interrupt` (`xSPITypes.bsv:36-51`).
* **Clock**: the whole IP (FSM, AXI master, APB slave) is clocked by the host SCK — `erbium_digital/verilog/erbium_digital_et.v:1506` (`.CLK(xspi_clk)`),
  pad `hb_c_clk_jtag_tck` = TCK (`ERBIUM_DIGITAL_TOP/verilog/ERBIUM_DIGITAL_TOP_16MB.sv:100`; `prcm/verilog/prcm_et.v:254 assign xspi_clk = TCK`, shared with JTAG TCK). Reset pad
  `hb_c_resetn_jtag_trstn` = TRSTn feeds POR (`prcm/verilog/prcm_et.v:163`). **REFINES**: nothing (AXI completion, WIP
  clear, CPU→SCCR APB access) progresses unless SCK is running; this is why the docs demand "8 TCK cycles"/`cmd=0` after reset.
* SoC hookup `erbium_digital_et.v:1505-1566`: AXI master → NoC `AXI_SLAVE_S_XSPI`; APB ← `axi2apb_64` ← NoC `AXI_MASTER_M_XSPI`
  (both in the xspi_clk domain); `cfg_default_mode_m(xspi_mode)`, `cfg_reset_device(xspi_rst_req)`, `cfg_interrupt(xspi_interrupt)`.
  `cfg_deep_power_down`/`cfg_ultra_deep_power_down` are **left unconnected** at SoC level (not in the instance port list).

## 2. Command decode

Opcode enum `xSPITypes.bsv:59-81`; **decode tables** `xSPITypes.bsv:106-129`. Only the 7 table entries are decoded.

| Opcode | Name | 1S table (`command_table_s1`, used **only when cmd_rate==S1**) | other table (`command_table_s8`, used for D1/S4/D4/S8/D8) | latency | dir |
|---|---|---|---|---|---|
| 5Ah | ReadSFDP | 3 addr bytes | 4 addr bytes | yes | read |
| 65h | ReadRegister | 3 | 4 | yes | read (32-bit) |
| 71h | WriteRegister | 3 | 4 | no | write (32-bit) |
| 0Bh | ReadMEM | 4 | 4 | yes | read (64-bit words) |
| 02h | WriteMEM | 4 | 4 | no | write (64-bit words) |
| 99h | ResetDevice | none | none | no | — |
| 52h | SetRate | 3 "address" bytes = the 3 rate bytes | 3 | no | — |

* Table select: `xspi.bsv:111` `command_table = cmd_rate == S1 ? command_table_s1 : command_table_s8`.
  **CONTRADICTS docs/BFM for D1**: in 1D cmd mode register/SFDP ops need **4** address bytes (RTL) while the BFM
  (`commands.py:56`) sends 3 in D1. D1 cmd mode is never tested (all test tuples use S1 cmd with D1 addr/data). Avoid/flag.
* **Ext byte**: the `extension` flag in the table is never read by the FSM. What actually happens (`xspi.bsv:608-615`, `550-552`):
  * S1, D1: no Ext byte.
  * S4, D4, S8: one extra byte after the opcode, clocked at cmd rate (`get_byte(modifier,…)` `xspi.bsv:613-614`).
  * D8: opcode on rising edge, Ext on falling edge of the same SCK (`get_byte(cmd,modifier)`; `demangle` D8 → `{re,fe}` `xSPITypes.bsv:154`).
  * **Value is ignored** — `modifier` is only used to build the HyperBus CA (`xspi.bsv:109`). BFM sends a repeat of the opcode
    (`master_driver.py:94-95`; the `^0xFF` inversion at `:109-111` is dead code). Model: accept anything.
* Unknown opcodes (00h NOP, 66h, B9h, ABh, 4Bh, 42h are in the enum but **not in the tables**): `fn_decode_cmd` returns Invalid
  (`xspi.bsv:147-153`), `ac_decode_cmd` does nothing (`:547-561`) and **`current_format` keeps the previous transaction's format**
  (it is not cleared in `acInit` `:597-607`). After POR `current_format = 0` (no addr/latency/data) so `cmd=0` is a true NOP
  (`xspi.bsv:82`). If a host sends an unknown opcode *followed by more bytes* after e.g. a WriteMEM, the stale format would treat
  them as address+data. A lone opcode byte followed by CS# high is harmless (FSM aborted on CS# rising edge `xspi.bsv:781-783`).
  **REFINES**: there is no deep-power-down entry/exit command; `CFG.DeepPowerDown/UltraDeepPowerDown` are plain register bits
  with no consumer at SoC level. 66h reset-enable is not required before 99h.
* Rate handling inside a transaction (`ddr_i.v:9-10, 45-69`; `xspi.bsv:528-546, 616-626`): opcode (+Ext) at `cmd_rate`, then
  the deserialiser switches once to `addr_rate` and never switches again → **addr_rate must equal data_rate** (design constraint,
  stated in `ddr_i.v:9`). All tested tuples obey this: (S1,S1,S1) (S1,D1,D1) (S4,D4,D4) (D4,D4,D4) (S8,S8,S8) (D8,D8,D8) (S4,S4,S4).
* Bit/lane mapping: 1S/1D input on DQ0 MSB-first, output on DQ1 (`ddr_i.v:75`, `xspi.bsv:316-324`); x4 on DQ[3:0] high nibble first
  (`ddr_i.v:77`, `xspi.bsv:328-336`); x8 on DQ[7:0]. DDR: rising-edge sample first, then falling (`xSPITypes.bsv:148-159`).
* Byte order on the wire is byte-address order (little-endian 64-bit word: first byte = bits[7:0]) for both reads
  (`xspi.bsv:294-296`, `d[rd_byte_counter]` from 0) and writes (`xspi.bsv:465-478`, `wr_vec[0]` = first byte = wdata[7:0]).

## 3. Latency (dummy cycles)

* `hb_decode_latency(IL) = 8 + IL` (`xspi.bsv:131-133`), IL = `CFG.InitialLatency[7:4]`, reset 8 (`ip/xspi/systemrdl/sccr.rdl:35`)
  → **16 SCK cycles by default, not 8**. **CONTRADICTS the review's reading ("InitialLatency reset 8" ⇒ 8 dummies).**
* Counted in SCK cycles regardless of lane width/DTR (`seqLatency` decrements once per clock `xspi.bsv:673-707`). **Not doubled**
  (`FixedLatency` is a read-only 1 with no logic behind it; RWDS is not used to request extra latency).
* Applies to every read (5Ah, 65h, 0Bh) in every mode including 1S (`command_table_s1` latency:True; docs: "we will not implement
  read zero latency"). Writes (71h, 02h) and 52h/99h have none.
* Host contract (BFM `master_driver.py:282-288`, `commands.py:81-94`): after the last address bit, idle exactly `8+IL` SCK
  cycles, then sample data. Tests assert `dut.latency_count == 8+IL` when idle (`ip/xspi/tb/cocotb/test_default.py:75`).
  Tested range IL=0..8 (latency 8..16) and 17 in `ai/` tests.
* HyperBus: reads and **memory** writes take the same `8+IL`; HB register writes have zero latency (`xspi.bsv:757`).

## 4. setRate (52h)

* Payload: 3 bytes after opcode(+Ext) = cmd_rate, addr_rate, data_rate (`xspi.bsv:214-224`; D8 packs them into 2 DDR clocks
  `{r0,r1},{r2,x}` `:226-242`). Bytes are clocked at the *current* `addr_rate` (fn_modifier `xspi.bsv:535-538`).
* Encoding `toRate(b)=b[3:0]` (`xSPITypes.bsv:130`), enum `S1=0,D1=1,S2=2,D2=3,S4=4,D4=5,S8=6,D8=7,HB=8` (`xSPITypes.bsv:94`). **CONFIRMS**.
  S2/D2 have no datapath (`decode_rate` no case; `demangle` default) and 9..15 are undefined → model as error/ignore.
* Effect: `xspi_rates` is written on the 3rd byte; `cmd_rate/addr_rate/data_rate` wires follow the register immediately
  (`xspi.bsv:208-213`) but `current_rate`/`ddr_i` are re-armed only while CS# is high (`acInit` `:603`, `ddr_i.v:35`)
  → **new rate applies from the next CS# assertion**. `sccr.xspi_rates` reads back `{data,addr,cmd}` bytes 2,1,0 (`sccr.rdl:54-58`).
* In HB mode 52h does not exist; BFM writes `xspi_rates` (0x28) with an HB register write (`commands.py:102-103`).
* Default-mode pins `xspi_mode[1:0]` (`erbium_digital_et.v:17`, pad `ERBIUM_DIGITAL_TOP_16MB.sv:24`): enum `HB_DEFAULT=0,
  OSPI_DEFAULT=1, QSPI_DEFAULT=2, SPI_DEFAULT=3` (`xSPITypes.bsv:12-17`), applied one cycle after reset by `handlePostReset`
  (`xspi.bsv:791-823`): 0→HB,HB,HB; 1→D8,D8,D8; 2→S4,S4,S4; 3→S1,S1,S1. **CONFIRMS** `doc/xspi.md:77-82`;
  **CONTRADICTS** `ip/xspi/doc/research.md:40-44` (stale). SoC TB drives 3 (`tb/env.py:96`).

## 5. SCCR register block

`ip/xspi/systemrdl/sccr.rdl` (regwidth 64, alignment 64). Decoder compares the 6-bit offset exactly (`SCCR_Reg_csr.bsv:16-22, 48-66`):

| off | reg | reset (RTL) | notes |
|---|---|---|---|
| 0x00 | ID0 | 0x0000_0002 | `mgf_id[3:0]=2`, `devid0[15:4]=0`; both **sw=rw (writable!)**, "TODO" in rdl:11-15 |
| 0x08 | ID1 | 0x0 | `dev_type[3:0]=0` ("hyperram"), `devid1[15:4]=0`, writable |
| 0x10 | CFG | 0x0000_318A | BurstLength[1:0]=2 rw, HybridBurstEnable[2]=0 r, FixedLatency[3]=1 r, InitialLatency[7:4]=8 rw, Reserved[11:8]=1 r, DriveStrength[14:12]=3 r, DeepPowerDown[15] rw, BurstEnable[16]=0 rw, UltraDeepPowerDown[17] rw (`SCCR_Reg_reg.bsv:126-178`) |
| 0x18 | xspi_status | 0 | wip[0] r |
| 0x20 | xspi_control | 0 | use_xspi_clk[0] rw, interrupt_enable[1] rw |
| 0x28 | xspi_rates | pins | cmd_rate[7:0], addr_rate[15:8], data_rate[23:16] rw |
| 0x30 | interrupt_status | 0 | axi_resp[1:0], read_underflow[2], write_overflow[3]; r, **rclr** |

* Size **0x38 with 8-byte stride** (`ip/xspi/doc/registers.md:12-24`). **erbium-hal `hwinc/top.h` `XSPI_REGISTERS_SIZE 0x1C` is wrong**
  (it was generated as if 4-byte stride); offsets 0x04,0x0C,… read 0 / write nothing.
* Non-matching offsets read as 0 and ignore writes (`SCCR_Reg_csr.bsv:57-68`). Upper 32 bits always 0.
* Host access paths:
  1. 65h/71h: SCCR offset = **address[5:0]**, all higher address bits ignored (`xspi.bsv:259, 439, 475`). 3-byte address in 1S,
     4-byte otherwise. 71h always writes the full 32 bits (`wstrb 0x0f`, `xspi.bsv:512`; RWDS byte-mask ignored for registers).
  2. HB register access (CA bit46) → same `addr[5:0]` decode (`xspi.bsv:112-113`).
  3. Memory ops 0Bh/02h at xSPI address **0x4000F000+off** → NoC `M_XSPI` → `axi2apb_64` → APB (`ip/erbium_noc/rtl/erbium_noc_top.sv:641-644,663`;
     `erbium_digital_et.v:1452-1503`). Not in the TRM xSPI map but physically routed. APB decode is also `paddr[5:0]` (`xspi.bsv:499-523`).
  4. CPU: 0x0200F000 (`regblocks/systemrdl/top_cpu_mm.rdl:22`, NoC rule `erbium_noc_top.sv:663`). Requires SCK running (xspi clock domain).
* 65h read semantics: while the data phase lasts, `r_readReg` re-reads the same register every cycle into a 2-deep FIFO
  (`xspi.bsv:258-261`); every 4 bytes the host gets another (re-)read of the same address — no auto-increment. `interrupt_status`
  is cleared by the first read (rclr `SCCR_Reg_signal.bsv:2237-2247`; clear beats a same-cycle hw set `:2201-2211`).
* 71h with >4 bytes: each 4-byte group is written again to the same offset; <4 trailing bytes dropped.

## 6. Read/Write Register & Read SFDP addressing

* Register data is 32 bits (`mode32bit`, set at end of address phase only for 65h/71h `xspi.bsv:619-626,634-645,788-790`).
* 5Ah: address = `{address[2],address[1],address[0]}`, only bits [11:0] used (`xspi.bsv:580-581`); the 4th address byte (non-1S) is ignored.
  Reads are DWORD-exact-match (`SFDP_Reg_csr.bsv:452-542`): an unaligned or unpopulated address returns 0. Address auto-increments by 4
  (`:583`), wrapping at 4 KB. **RTL quirk**: SFDP DWORDs are enqueued as `zeroExtend` to 64 bits and streamed with the 8-byte
  (`!mode32bit`) rule (`xspi.bsv:582, 353-356`) ⇒ on the wire each DWORD is followed by **4 zero bytes**
  (byte stream: `53 46 44 50 00 00 00 00 0C 01 06 FA 00 00 00 00 …`). Tests only check bytes[0:4] / [0:16]
  (`ip/xspi/tb/cocotb/ai/test_sfdp.py:52,93`). The emulator should replicate this unless the RTL is fixed.
* 65h/71h never reach SFDP; SFDP is not visible from the CPU/APB.

## 7. SFDP ROM contents

Extracted from `ip/xspi/bsv/SFDP_Reg_reg.bsv` reset values + `SFDP_Reg_csr.bsv:95-180` offsets (verified against
`ip/xspi/verilog/mkConfigCSR_SFDP_Reg.v:20123,20387`). Full image (one DWORD per line, offsets 0x000–0xB38, unlisted = 0):
**`docs/sfdp-rtl.hex`**. Template source `ip/xspi/systemrdl/sfdp.rdl` (Perl-templated: `sfdp.rdl:2-9` header params).

Header @0x000: `50444653` "SFDP"; `FA06010C` → minor 0x0C, major 1, **numHdr=6** (`sfdp.rdl:23` `$#params+1`; JESD216 NPH is
count−1 ⇒ should be 5 — TBD/bug), access_protocol 0xFA (TBD, rdl:22 comment says "8D profile 2, will not work for qspi").

| # | @ | ID | rev | len | PTP | body regs |
|---|---|---|---|---|---|---|
| 1 | 0x008 | 0xFF00 BFPT | 1.9 | 23 | 0x400 | `reg_6_4_4..reg_6_4_26` @0x400–0x458 |
| 2 | 0x010 | 0xFF84 4BAIT | 1.1 | 2 | 0x700 | `reg_6_7_3/4` @0x700 |
| 3 | 0x018 | 0xFF06 xSPI Profile 2.0 | 1.0 | 3 | 0x900 | `reg_6_9_3..5` @0x900 |
| 4 | 0x020 | 0xFF87 SCCR map | 1.1 | 28 | 0xA00 | `reg_6_10_3..29` @0xA00–0xA68 (+`reg_6_11_3/4` spill into 0xA6C/0xA70, all 0) |
| 5 | 0x028 | 0xFF09 (SCCR map for Profile 2.0) | 1.0 | 13 | 0xB00 | `reg_6_11_5..15` @0xB00–0xB28, `reg_6_17_3/4` land at 0xB2C/0xB30 |
| 6 | 0x030 | 0xFF0F ("GRAM", rdl:364) | 1.1 | 10 | **0x1100** | **unreachable**: address is 12-bit ⇒ aliases to 0x100 = zeros. `reg_6_17_3..6` actually sit at 0xB2C–0xB38 |

Key body values: BFPT DW1 `7F8CFFFB` (4-byte-only addressing, DTR, no erase), DW2 `00FFFFFF` (16 Mbit), DW17 `D6D76200`
(deep-PD supported, enter AD/exit AE — not implemented in RTL), DW20 `00000200`, DW21 `40000002`.
Profile-2.0 DW1 `AA8F8020` (profile2=1, linear rd/wr reg+mem, no WREN1/2, no SREN, deep_pd=1, enter_spi=1), DW2 `00000108`,
DW3 `10842108` (dummy fields = 2, "TBD"). SCCR-map DW3 `F3C00000` (num_addr_bytes=3, dummy cycles 0 — inconsistent with §3),
DW16/18/20/22/24/25 `90000000`, DW26 `D0000000`. 0xFF09 DW1 `C0000000` (WIP "not supported", contradicts xspi_status.wip), DW9 `C0000000`.
0xB2C `00000410`, 0xB30/0xB34 `00086571` (dummy 8, read op 65, write op 71), 0xB38 `05002101`.
TBDs flagged in rdl: numHdr, access_protocol, all dummy-cycle fields, volatile reg offsets, drive-strength bit pattern, 0xFF0F table.

## 8. Address decode of memory ops

* The xSPI IP puts the raw 32-bit address on AXI (`xspi.bsv:266-268, 378-381`). Translation is in the **NoC**
  (`ip/erbium_noc/rtl/erbium_noc_top.sv:641-644`, `ni700_ErbiumET.sv` is a generated wrapper around `erbium_noc_top`):
  `a<0x4000_0000 → a+0x4000_0000 (MRAM)`, `0x4000_0000≤a<0x8000_0000 → a−0x3E00_0000`, else unchanged. Then the CPU-map
  rules (`erbium_noc_top.sv:656-665`); anything else → **DECERR** (`docs/ARCHITECTURE.md:96`).
* Resulting CPU map (RTL, `erbium_noc_top.sv:656-665`) and xSPI view:

| target | CPU | xSPI | size |
|---|---|---|---|
| M_SYSTEM_REG | 0x0200_0000 | 0x4000_0000 | 4 KB (regs span 0xC4) |
| M_MRAM_REG | 0x0200_1000 | 0x4000_1000 | 4 KB |
| M_I2C_REG (APB) | 0x0200_2000 | 0x4000_2000 | 4 KB |
| M_SPI_REG (qspi) | 0x0200_3000 | 0x4000_3000 | 4 KB |
| M_UART_REG | 0x0200_4000 | 0x4000_4000 | 4 KB |
| M_SRAM (romram) | 0x0200_8000–0x0200_CFFF | 0x4000_8000–0x4000_CFFF | 20 KB window |
| M_XSPI (SCCR via APB) | 0x0200_F000 | 0x4000_F000 | 4 KB |
| M_MRAM | 0x4000_0000–0x7FFF_FFFF | 0x0000_0000–0x3FFF_FFFF | 1 GB window, 16 MB device (`regblocks/systemrdl/mram.rdl` 0x40000×512b) |
| M_CPU_REG | 0x8000_0000–0xBFFF_FFFF | same | 1 GB |
| GPV cfg | 0xFE00_0000–0xFE01_5FFF | same | |

* Inside the romram window (`romram/bsv/RomRam.bsv:33-38`, decoded on dword address bits): **bootrom 0x0200_8000–0x0200_9FFF (8 KB, r/o,
  write→SLVERR)**, **SRAM 0x0200_C000–0x0200_CFFF (4 KB)**, gap 0x0200_A000–0x0200_BFFF → DECERR. RDL agrees
  (`regblocks/systemrdl/romram.rdl:62-79`, `top_cpu_mm.rdl:21`, `top_xspi_mm.rdl:19`), SoC test agrees (`tb/test_memory_map.py:22-24,39-53`).
  **erbium-hal top.h (bootrom 0x02008000..0x0200A000, SRAM 0x0200C000..0x0200D000) is right; TRM interconnect page
  (SRAM 0x0200A000 / xSPI 0x40005000) and the review are stale.** xSPI 0x4000_5000 → CPU 0x0200_5000 → DECERR.
* The xSPI map has no `plic`-at-0xA000_0000 entry in the NoC (it is inside M_CPU_REG 0x8000_0000–0xBFFF_FFFF).
  Alias: xSPI 0x7E00_0000–0x7FFF_FFFF → 0x4000_0000.. = MRAM again (harmless).

## 9. Data-size and burst rules (what RTL really does)

* Memory write (`xspi.bsv:462-492` SDR/1D/4D, `403-458` D8/HB): every 8 received bytes ⇒ one **single-beat** AXI write
  (`awlen=0, awsize=3`, `xspi.bsv:376-391`), `wstrb` = per-byte `!RWDS` sampled with the byte (RWDS high = mask, `:466,410-429`).
  Address then advances by 8 **within bits [11:0] only** (`xspi.bsv:392`) ⇒ a write crossing a 4 KB boundary wraps to the page start
  (tests deliberately avoid crossing, `test_default.py:24-36`). Unlimited length; trailing <8 bytes are silently dropped
  (counter reset in `acInit :604`). If the 2-deep AXI W FIFO is full ⇒ `write_overflow` and the word is lost (`:452,487`).
  Unaligned addresses are forwarded as-is (slave-dependent; treat as undefined).
* Memory read: exactly **one** AXI INCR burst per CS# (`r_read` set once, `xspi.bsv:682-686, 263-279`), `arlen = BurstEnable ?
  {BurstLength 0:15, 1:7, 2:1, 3:3} : 0` (`fn_hb_decode_len` `:116-129`) ⇒ **8 bytes when BurstEnable=0 (reset), else
  128/64/16/32 bytes for BurstLength 0/1/2/3**. Reading past that ⇒ `read_underflow=1` and zeros on DQ (`:367`, `dw_out_data_re` DWire 0).
  **CONTRADICTS "max burst 256"**: max is 128 bytes; default is 8. Wrapped bursts: `arburst` is hard-coded INCR (`:134-136`);
  `HybridBurstEnable` is read-only 0. Host may stop early (CS# high) — remaining AXI data is drained/cleared in idle (`:606`).
* Register ops: 4 bytes per access as in §5. Registers are never burst (address not incremented).
* AXI response handling: read `rresp!=0` and write `bresp∉{OKAY,EXOKAY}` latch into `interrupt_status.axi_resp` (`:180-186, 282-287`);
  SLVERR=2, DECERR=3 (tests: `env.py:71-84`).

## 10. Errors, status, interrupt

* `interrupt_status`: `axi_resp[1:0]` last error code; `read_underflow[2]`; `write_overflow[3]`; all read-to-clear
  (`sccr.rdl:59-63`). Tests: `ai/test_error_handling.py:37-97`.
* `xspi_status.wip`: set every cycle of a **memory** write data phase, cleared when an AXI B response arrives (`setWIP` `xspi.bsv:176-179`).
  Not set for register writes. Host flow: poll 0x18 until 0, then read 0x30 (`env.py:65-84`, `tb/env.py:148-168`).
* `cfg.interrupt = interrupt_status != 0` (`xspi.bsv:874-876`, `mkxspi.v:2057`) → `plic_irq[4]` = **PLIC id 5**
  (`erbium_digital_et.v:776-784`; bit0=MRAM id1, QSPI 2, UART 3, SysReg 4, xSPI 5, GPIO 6; `tb/test_interrupt.py:47-52`).
  **CONTRADICTS docs**: `xspi_control.interrupt_enable` is **not** wired to anything (only the register exists) — the CPU IRQ
  is level = status≠0 regardless. Level clears when the host (or CPU) reads 0x30.

## 11. Reset

* 99h: `cfg.reset_device = (cmd==0x99) && current_rate!=HB` (`xspi.bsv:871-873`) — a level that stays high while the `cmd`
  register holds 0x99. It is `xspi_rst_req` → `prcm_et.xspi_soft_reset` → `soft_rst` of the POR extender
  (`prcm/verilog/prcm_et.v:165-169`) ⇒ **whole-chip soft reset** (system, MRAM, periph, CPU and xSPI domains; same path as
  `SoftReset.soft_reset`/watchdog). **REFINES docs** ("reset device and enter default mode"): SCCR, xspi_rates, CPU, everything resets;
  default mode re-applied from pins by `handlePostReset`. In HB mode 99h is not a command (CA byte) — no in-band reset.
* Reset release for the xSPI domain is clocked by SCK (`prcm_et.v:213-222`, `power_aware_reset_ctrl` RESET_DURATION 5) — hence
  "clock 8 TCK cycles / issue cmd=0 after any reset" (`doc/xspi.md:137-139`). BFM boot: 10 SCK, `Reset()`, 10 SCK (`env.py:54-63`);
  BFM assumes latency 16 and default-pin mode after 99h (`commands.py:67-81`).
* B9h/ABh (deep power down) are **not decoded**; `CFG.DeepPowerDown/UltraDeepPowerDown` are inert bits. `PowerDomainReq.xspi_pd`
  gated by CS# high is the only xSPI power control (`erbium_digital_et.v:143`).

## 12. Host↔CPU signalling

* `system_registers` (`regblocks/systemrdl/system.rdl`, decode `regblocks/verilog/System_Reg.sv:272-283`): `SysInterrupt` @0x20,
  `SpinLock` @0x58, `ChipMode` @0x60, **`Mailbox0` @0x68, `Mailbox1` @0x70** (8-byte aligned; review's "0x6C" is wrong),
  `GPIO_OE` @0x78. From xSPI: 0x4000_0068 / 0x4000_0070 via 0Bh/02h (8-byte accesses; the upper 4 bytes hit the alignment gap).
* "Register interrupt" (PLIC id 4) = `SystemConfig.sys_interrupt_enable && SysInterrupt.interrupt`
  (`erbium_digital_et.v:778-779`); `SysInterrupt.interrupt[0]` is sw=rw, rclr (`system.rdl:47`). **Writing Mailbox0 does not
  raise it** — the host must write `SysInterrupt=1` (0x4000_0020) after `SystemConfig.sys_interrupt_enable` (bit0 of 0x08) is set.
  Test: `tb/test_interrupt.py:63-69`.
* Host-facing lines: none from the xSPI IP (`cfg.interrupt` goes to the PLIC only). Out-of-band candidates: `gpio_out[10:0]`
  (`ERBIUM_DIGITAL_TOP_16MB.sv:26-28`), driven by `GPIO_O/GPIO_OE`; pins are muxed: gpio0 = OSC_CLK_OUT when
  `osc_out_enable`/TestMode, gpio1-2 I2C, 3-6 SPI, 7-8 QSPI, 9 UART_TX, 10 UART_RX when the respective `SystemConfig.*_enable`
  is set (`erbium_digital_et.v:592-676`). UART/I2C/QSPI have no dedicated pads on the 16 MB top.

## 13. HyperBus profile (later milestone)

* Entered only via pins=0 or `xspi_rates=8,8,8`. CA = 3 DDR clocks = 48 bits `{cmd,modifier,addr3,addr2,addr1,addr0}` decoded as
  `HB_Cmd_st` (`xSPITypes.bsv:97-105`): bit47 R/W# (1=read), bit46 reg/mem (1=register), bit45 linear (ignored, always INCR),
  bits[44:16] = **A31:A3 (29 bits, uses the 5 bits JESD251 marks reserved — the erratum in `doc/xspi.md:169`)**, [15:3] reserved,
  [2:0] = A2:0. AXI address = `{uca,lca}` (`xspi.bsv:109-110`) — i.e. a plain **byte** address, not a HyperRAM halfword address.
  BFM: `hb_commands.py:54-69`. Reads / mem writes: `8+IL` latency, no RWDS-based 2×; reg writes: 0 latency (`xspi.bsv:753-759`).
  Register access → SCCR `addr[5:0]`. Write address update for HB is marked TODO (`xspi.bsv:393-399`). 99h not available in HB.

## 14. Testbenches (reference host sequences)

IP-level: `ip/xspi/tb/cocotb/` (`tb.v` wraps `mkxspi` with an AXI RAM model, `env.py` = BFM + AxiRam 4 GB).
* `test_default.py::default_test` — parametrised over modes ×latency 8..15 × burstlen 0..3 × default pin 1..3: boot → `setLatency` →
  `setBurst` → `setRate(mode)` → backdoor write / front-door `read_Mem` of 128/64/16/32 bytes.
* `test_xspi_burst_wr.py::xspi_burst_wr_test` — same matrix, front-door `write_Mem` + backdoor + front-door read.
* `test_wip.py`, `test_default_mode.py` — SFDP sig, reg r/w to ID0, mem r/w, error check.
* `ai/test_sfdp.py` (CP-03), `ai/test_mode_switch.py` (CP-04/08/10 incl. `setRate(HB,HB,HB)` register check), `ai/test_octal_mode.py`
  (CP-05/06/07), `ai/test_quad_mode.py` (CP-09), `ai/test_spi_mode.py` (CP-01/02), `ai/test_error_handling.py` (CP-13/14 rclr, RWDS masks,
  interrupt_enable r/w), `ai/test_rwds.py` (OE/RWDS mask matrix), `ai/test_timing.py` (tCS_high 20 ns), `ai/test_regression.py`.
SoC-level: `tb/test_xspi.py` (SFDP, `write_Mem(0x40000060,'hello world!!!!!')`/`read_Mem` → 8 bytes, random setRate + probe rates),
`tb/test_memory_map.py` (ROM/SRAM ranges), `tb/test_interrupt.py`.

Byte sequences produced by the BFM (`master_driver.py:69-164`, `commands.py`), MSB-first addresses:
* 1S setLatency(17) then read: `65 00 00 10` +16 dummy → 4 bytes; `71 00 00 10 <LE32 (old&0xFFFFFF0F)|((17-8)<<4)>`; thereafter 17 dummies.
* 1S setBurst(0): `65 00 00 10`+dummies → `71 00 00 10 <LE32 (old&0xFFFC)|0|(1<<16)>` (BurstEnable=1, BurstLength=0 → 128-byte reads).
* 1S → 8D: `52 07 07 07` (CS# high) then every command is DDR: e.g. read 128 B at 0x1000: `0B 0B 00 00 10 00` (opcode+Ext in one SCK,
  4 address bytes in 2 SCK), `8+IL` SCK dummies (RWDS/DQ driven by target), then 128 data bytes 2 per SCK; write: `02 02 <addr4> <data…>`
  with host RWDS = byte mask.
* switching while already in a 4S/4D/S8 cmd mode: `52 <Ext=52> 04 05 05` (Ext present because the *current* cmd rate needs it); afterwards `0B <Ext> <4 addr bytes at D4> …`.
* SFDP in 1S: `5A 00 00 00` + `8+IL` dummies → `53 46 44 50 00 00 00 00 0C 01 06 FA 00 00 00 00 …` (see §6 quirk). Non-1S: `5A <Ext> 00 00 00 00`.
* Reset: `99` (1S) / `99 99` (D8, same SCK) / `99 <Ext>` (S4/D4/S8) → chip reset; then ≥10 SCK with CS# high.
* HB read reg 0x28: CA bytes `E0 00 00 05 00 00` (bit47 R=1, bit46 reg=1, bit45 linear=1 → 0xE0; addr 0x28 → uca=5 in bits[44:16], lca=0), 8+IL dummies, 4 bytes.

## Summary of contradictions vs docs/review

1. Default dummy cycles = **8 + InitialLatency = 16**, in SCK cycles, all modes, all reads (not 8).
2. SRAM is at CPU 0x0200_C000 / xSPI 0x4000_C000, 4 KB; bootrom 0x0200_8000 8 KB. TRM interconnect page & review are stale.
3. SCCR is 0x38 bytes / 8-byte stride; erbium-hal `XSPI_REGISTERS_SIZE 0x1C` is wrong. Reachable by memory ops at xSPI 0x4000_F000.
4. Max read per CS# is one AXI burst: 8 B by default (BurstEnable=0), max 128 B — not 256.
5. `xspi_control.interrupt_enable` is not implemented; PLIC id 5 is level `interrupt_status!=0`.
6. 99h is a **whole-chip** soft reset. B9h/ABh/66h/00h are not decoded (unknown opcodes reuse the previous format — model as NOP but log).
7. Ext byte value is ignored; D8 packs opcode+Ext in one SCK; D1 has no Ext but uses 4-byte register addresses (untested, BFM disagrees).
8. Mailbox1 is @0x70 (not 0x6C). Mailbox writes do not interrupt; `SysInterrupt.interrupt` + `SystemConfig.sys_interrupt_enable` do.
9. SFDP: NPH field = 6 (should be 5), 0xFF0F table pointer 0x1100 unreachable, each DWORD is streamed with 4 trailing zero bytes.
10. Write address auto-increment wraps inside 4 KB; register ops don't increment; addr_rate must equal data_rate.
