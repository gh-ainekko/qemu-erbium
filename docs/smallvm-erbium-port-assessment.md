# SmallVM on Erbium: port assessment

Study date: 2026-10-07. This is a source review and implementation proposal,
not a working port. The only new executable experiment was a native LP64
object-layout probe; no SmallVM firmware was booted on Erbium.

## Scope and verdict

Running the MicroBlocks VM **on Erbium**, with live editing from its existing
IDE, is feasible. It is not just another board definition: SmallVM assumes a
32-bit pointer machine, whereas the normal Erbium C ABI has 64-bit pointers.
That VM representation work dominates a minimal port. Erbium-specific startup,
time, UART, and persistence are comparatively bounded tasks.

Recommended first target: one hart, bare-metal C, polled Shakti UART carrying
the existing MicroBlocks binary protocol, MRAM-backed program storage, and a
minimal primitive set. No Arduino core, Go runtime, SMP, graphics, networking,
or neural-network acceleration is required to prove the port.

Kotama is a hardware/boot reference, not a dependency into which the C VM
should be embedded. Its experimental Go compiler is not needed for this port.

## Reviewed snapshots and evidence

Repositories cloned directly from the user-supplied upstreams:

| Source | Revision |
| --- | --- |
| Codeberg `MicroBlocks/smallvm` | `49f337529294640def7b27a2ee1c4e7ba88ccdbb` |
| GitHub `usbarmory/kotama` | `9e5db018306f6dfa7905932c29c7e2e988e2d6bf` |
| GitHub `usbarmory/tamago`, pinned by Kotama's go.mod | `9e72834d5757038516dddf4c3e402210130ecc7e` |

Source paths below refer to these snapshots unless explicitly marked local.
The existing local `docs/kotama-integration-proposal.md` documents a previously
booted Kotama baseline and the still-unimplemented host-load/reset work; this
review did not repeat that boot test.

## 1. The main blocker: 32-bit object representation

SmallVM evidence:

- `vm/mem.h`: `OBJ` is `int *`; integer values are tagged pointer-shaped values;
  `HEADER_WORDS` is one; `FIELD()` indexes an `OBJ *`.
- `vm/mem.c:memInit()` explicitly requires `sizeof(int*) == 4` and otherwise
  panics. The Linux build (`linux+pi/buildVMLinux.sh`) deliberately uses `-m32`.
- Allocation advances `int *` addresses in four-byte units but fills through
  `OBJ *`. GC forwarding fields and pointer-reversal marking store addresses in
  32-bit words. This is not merely a compiler-warning issue.
- `vm/interp.c:pushLiteral_op` constructs object references directly into the
  downloaded code/literal store. Primitive-name literals do likewise, and
  `interp.c` also defines a static string object outside the heap.
- `vm/persist.h` describes 32-bit persistent record words and 16-bit bytecode.

A native x86-64 LP64 probe including upstream `mem.h` reported:

```
int=4 pointer=8 float=4 OBJ=8 header=4 FIELD(0) offset=8
```

This is the same relevant C data-model mismatch as LP64 on RV64. Removing the
panic or replacing a few casts with `uintptr_t` does not fix it: the allocator,
fields, strings, and collector disagree about sizes and offsets.

### Recommended approach: keep compact 32-bit VM references

Separate native C pointers from VM values:

- Use an explicit 32-bit tagged value type, with accessors to encode/decode
  references through `uintptr_t`, and explicit 32-bit heap words.
- For an Erbium-first implementation, checked absolute addresses are practical:
  its MRAM is `0x40000000..0x40ffffff`, below 2 GiB. Reserve both object heap and
  code/literal storage there and enforce their bounds in the linker and tests.
- Alternatively use offsets in a defined VM arena. If doing so, the arena must
  cover code literals and static objects as well as heap objects, or explicitly
  distinguish them. Absolute-address handles likewise need checked placement
  for static objects; a host regression harness must arrange low-address storage.
- Audit direct object dereferences, field writes, byte-array access, stack and
  global roots, GC marking/forwarding/compaction, primitive arguments, and
  integer tag conversion (including negative values and shifts).
- Keep actual C instruction pointers, function pointers, and MMIO addresses
  native-width. Do not run device-register addresses through tagged VM values.
- Preserve the current bytecode, literal layout, 31-bit signed integer behavior,
  and IDE wire format. Regression-test existing 32-bit builds too.

A native-width object redesign is another valid route, but also requires
separating the in-memory layout from downloaded literals/persistence. It is
not automatically simpler. An RV64 ILP32 compiler would be a third route only
if a matching, demonstrated toolchain/libc existed; do not assume `-mabi=ilp32`
works with this RV64 target, or assume the core can execute an RV32 build.

Do not initially scale the heap to all 16 MiB: `WORDS()` masks object sizes to
16 bits and upstream's large heap configuration is approximately 262 KiB.
Start within existing limits; enlarging the allocator is a separate change.

## 2. Native firmware platform

Add an isolated target, for example `erbium/` containing a build file,
`startup.S`, linker script, platform hooks, and a small `main.c`. Reuse the C
VM core and implement non-Arduino backends rather than compiling all `.cpp`
peripheral files with an invented Arduino compatibility layer.

### Build and C runtime

Start with a conservative RV64 integer/soft-float ABI, e.g. `rv64imc` plus
required CSR/fence extensions and `lp64`, with matching compiler support
libraries and libc. Validate exact flags with the installed toolchain.
Erbium has F but lacks the standard A/D requirements of ordinary general-purpose
RV64 environments; Kotama's README explains why its Go port uses `GOSOFT=1`.
The existing local worker instead uses `rv64imfc/lp64f`, which is also a
possible C route, but requires all objects and libraries to agree on that ABI.
Do not mix the two or silently pull in `rv64gc/lp64d` libraries.

SmallVM uses libc and math helpers despite lacking a user-visible floating
point type. Supply a compatible bare-metal libc/libm/compiler runtime, or a
reviewed minimal subset, plus syscall stubs as required. The tiny local
`-nostdlib` mailbox worker is not evidence that the VM can link unchanged.
Check the final ELF/disassembly for unsupported instructions and relocations.
The independent audit verified the installed GCC's LP64 data model/multilib
selection, but cross-compiling `mem.c` stopped at missing `stdio.h`: the local
compiler installation does not currently provide the required C library headers.
Also audit non-pointer LP64 changes such as the `long`-based integer conversion
in `miscPrims.c`; scalar handles alone do not fix every data-model assumption.

Startup must establish stack/global-pointer state, initialize data/BSS,
install useful fault handling, and keep other harts out of VM initialization.
Use the existing local Erbium firmware startup as an additional reference for
minion-specific CPU initialization rather than assuming generic RISC-V crt0.
Kotama's `soc/aifoundry/erbium/cpuinit.s` uses a custom `AMOADDG.D` encoding and
parks extra harts; it does not establish that normal A-extension atomics work.
For the first boot enable only minion 0/thread 0.

### Memory and loading

TamaGo's `soc/aifoundry/erbium/ram.go` and
`board/aifoundry/erbium_emu/mem.go` define a 16 MiB region at `0x40000000`.
Kotama links text at `0x40010000`; this is a useful starting placement, not a
required C ABI and not necessarily the ELF entry. Build explicit nonoverlapping
regions for image/BSS, stack, libc heap, VM objects, and persistent scripts;
reserve host rings separately if later needed. Persistent storage must not
appear in loadable/zeroed firmware ranges.

First boot through standalone `erbium_emu` ELF loading, using the actual ELF
entry/reset PC. Product boot through the Linux/xSPI host is separate work:
load `PT_LOAD` segments to their mapped addresses, zero segment tails, and use
a tested CPU hold/program-entry/release sequence. Do not copy the whole ELF
as a flat image or use the SPI whole-chip-reset command as a start command.
See local `docs/kotama-integration-proposal.md` for the emulator's current
warm-reset gaps and proposed loader. These are shared infrastructure tasks,
not reasons to block standalone VM bring-up.

### Hooks, timer, and UART

`vm/interp.h` declares platform operations including `hardwareInit`,
`microsecs`, `millisecs`, `recvBytes`, and `sendBytes`. Use `vm/vm.ino:setup`
and `linux+pi/linux.c:main` as initialization references, but initialize enough
UART/fault reporting before `memInit` that an early panic is observable.

TamaGo evidence:

- `soc/aifoundry/erbium/timer.go`: 64-bit `ESR_MTIME` at `0x80f40200`;
  `TimerMultiplier=50000`, consumed as nanoseconds per counter tick by
  `riscv64/timer.go`. That implies 50 microseconds per tick in this profile,
  **not** one tick per 200 MHz CPU clock. Implement integer conversions to
  wrapping 32-bit VM timers; qualify physical hardware timing separately.
  In particular, local `docs/trm/cpu_subsystem.txt` section 2.9 describes a
  prescaled 10 MHz timer. Treat the discrepancy as a configuration/calibration
  question, not evidence that hardware universally has 50-microsecond ticks.
- `soc/aifoundry/erbium/erbium.go`: Shakti UART at `0x02004000`, system
  configuration at `0x02000008`.
- `soc/aifoundry/uart/shakti.go`: enable system-config bit 6; TX/RX/status
  offsets `0x08/0x10/0x18`; poll status bit 1 for TX-full, bit 2 for RX-ready.
  Initialization does not program baud/framing; hardware needs validation.

Respect MMIO width and stride: system/UART registers use 32-bit accesses on
8-byte strides; the timer ESR requires a 64-bit access. Implement nonblocking
RX and partial-progress TX rather than blocking the cooperative VM indefinitely.

## 3. IDE connection is a binary transport, not a shell

Keep `vm/runtime.c` framing and existing bytecodes unchanged. Feed it a clean
bidirectional stream via `recvBytes`/`sendBytes`. Do not mix printf/emulator
logs with the IDE bytes. The default runtime's buffers are 1024 bytes, so a
working short Kotama shell command is not an adequate transport test.

For an emulator prototype, expose a raw PTY or bridge that the native IDE can
open. Browser IDE access needs a browser-supported connection/bridge; a PTY on
a remote VM is not automatically visible as Web Serial on the user's laptop.
Discovery and the choice of desktop versus browser frontend must be tested.

The local Kotama proposal identified sysemu UART loss paths: RX drains beyond
its 16-byte FIFO; TX ignores failed/partial writes. Fix buffering/backpressure
before judging live upload reliability. Its proposed UART socket is not an
already-available feature. UART console input cannot be injected by having the
host write the CPU-facing TX register over xSPI.

If the board exposes only xSPI, add host-to-VM and VM-to-host MRAM rings and a
host serial/transport bridge. Specify producer ownership, barriers/coherency,
reset/session behavior and bounds. Adapt only the byte transport initially;
the VM protocol itself need not change. This has higher scope than UART.

## 4. Persistent scripts in MRAM

For the very first executable milestone, the fallback in `vm/persist.c`
already provides a 40 KiB RAM code store with no filesystem dependency.
Use it to prove scripts and IDE communication before adding durability; its
BSS-backed contents must not be advertised as surviving normal firmware boots.

`vm/persist.c` already has a platform porting boundary: `START`, `HALF_SPACE`,
`flashErase`, `flashWriteData`, and `flashWriteWord`. Add an Erbium branch with
two reserved MRAM half-spaces. Erase can fill the expected erased pattern
(`0xff`); writes become properly ordered memory stores, not flash-controller
commands. Preserve record parsing, append, and compaction behavior.

Verify initialization, saved-program readback, automatic start, delete,
compaction, and interrupted updates. Persistent MRAM does not by itself make
the existing multiword update protocol power-fail safe. Retain the MRAM backing
file across emulator restarts; ensure boot/load/reset never clears this region.
Validate stored record lengths before traversal and test live references into
code storage during compaction. Initially stop tasks and clear stale literal
references when moving code; unrestricted live-update correctness needs explicit
tests, not just a successful upload.
Do not confuse script/code persistence with automatically saving all live
heap objects or variables. A filesystem is not required for this milestone.

## 5. Board features and upstream structure

`vm/runtime.c:primsInit` registers many peripheral primitive groups. Add an
explicit minimal profile with core data/misc/variable primitives and supported
platform services; unavailable operations should fail predictably rather than
pretend hardware is present. `vm/boardHooks.*` provides lifecycle/extension
hooks, but is not a complete board abstraction: timer, transport, persistence,
primitive selection and object representation still need independent work.

Report an Erbium board identity. The first build can be installed outside the
IDE; polished support additionally touches board detection, library selection,
firmware packaging and `ide/MicroBlocksFirmwareInstaller.gp`. Existing serial
protocol compatibility does not imply existing installers know how to upload an
Erbium ELF through xSPI.

Keep VM state and GC on one hart. Later accelerator/minion workloads should be
asynchronous native primitives: submit a bounded job, return/yield, and collect
completion from the VM thread. Establish buffer ownership and relocation/pinning
rules; do not let a worker retain pointers into a moving GC heap. The existing
board request queue is not generally multicore-safe outside its ESP32 branch.

## 6. Suggested milestones and effort

These are engineering estimates for one developer familiar with embedded C and
this emulator, not measured implementation durations; phases can overlap.

| Milestone | Acceptance criterion | Rough effort |
| --- | --- | --- |
| 32-bit VM values on an LP64 host | interpreter/literal tests, negative integers, byte arrays, nested/cyclic lists, forced GC and resize, persistence roundtrip; sanitizers clean | 1–2 weeks |
| Erbium firmware bring-up | repeatable cross-build, one-hart boot, faults visible, UART, calibrated timers, representative scripts | 3–5 days |
| Live IDE and MRAM | upload/readback/start/stop, sustained traffic without drops, reconnect, retained scripts and compaction across restart | 3–7 days |
| Product integration | firmware install/host loader, resets, error paths, hardware qualification and regression packaging | 1–3+ weeks |

Budget approximately **2–4 weeks for a credible emulator demonstration** and
**4–8+ weeks for a maintained port**, assuming direct UART access. Toolchain/libc
work, shared host-load/reset fixes, xSPI-only transport, and hardware surprises
can extend this materially. SMP/accelerator integration is additional scope.

The highest-value first experiment is an LP64-host VM regression harness with
compact 32-bit values. It de-risks the hardest change independently of Erbium;
only after that should UART and hardware failures become part of the debug loop.
