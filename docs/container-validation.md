# Pristine Ubuntu build validation

Validated on 2026-10-05 using Ubuntu 24.04 x86_64:

```
ubuntu@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
```

The build container received a tracked-files archive (no host toolchains,
external source trees, binaries, or compiler caches). The first attempt exposed
an assumption that `sudo` was available. After fixing bootstrap to install
packages directly when running as root, the complete bootstrap passed in about
19.5 minutes with a two-CPU limit:

- Installed dependencies and fetched/applied all pinned source patch series.
- Built QEMU, qtest, `erbium_emu`, minion firmware, guest Linux with initramfs,
  and static arm64 `erbctl`.
- Passed seven stub qtests, eight backend qtests, and the backend guest test.
- Passed four preflight regression cases and the real Kconfig fresh/stale-path
  regression test.
- Packaged a roughly 13 MiB compressed binary distribution with SHA-256 checksum.

A second, pristine Ubuntu container received only the tarball/checksum and
installed the documented runtime packages (no compilers or source). Checksum
verification, all 15 qtests, the backend guest test, and the stub guest test
passed. Both guest runs printed `ERBIUM-TEST-RESULT 0`.

## Repeat

```
J=2 scripts/test-pristine-docker.sh
```

This tests committed HEAD; commit edits first. It creates fresh build/runtime
containers and saves logs, commit, base-image digest, and archive under
`build/container-test/`. Containers are removed on exit. No host directories or
Docker socket are mounted into the containers.

To test only an existing tarball (with its adjacent `.sha256`):

```
scripts/test-dist-docker.sh out/erbium-emu-dist-VERSION-ubuntu24.04-x86_64.tar.gz
```

Actions invokes the same packaging and runtime-validation scripts before
uploading the artifact. This validates Ubuntu 24.04 x86_64; it is not a claim of
support for other host distributions or architectures.

## Clean rerun after the Linux-only recovery fixes — 2026-10-06

Ran `J=2 scripts/test-pristine-docker.sh` against unmodified commit
`a481de95af4d99b7a5e046761544bb4c37c5e29d`, using the Ubuntu digest above.
No host build trees, compiler caches, or source edits were supplied. The test
copied committed source into a new container and invoked `./bootstrap.sh` with
`J=2`; the complete driver script exited **0**, without manual intervention.

- Full source build: QEMU, backend, firmware, Linux/initramfs, and control tool.
- Source-build execution: 15 qtests and an actual QEMU Linux boot with backend
  mailbox jobs; `ALL TESTS PASSED` and `ERBIUM-TEST-RESULT 0`.
- Regression checks: eight preflight/runtime cases plus the real Kconfig test.
- Binary-only container: checksum verified, 15 qtests passed, actual Linux boots
  against both the backend and stub passed.
- Stub serial log confirmed `Linux version 6.12.48-erbium`, `Run /init as init
  process`, `ERBIUM-TEST-RESULT 0`, and `reboot: Power down`.

No additional code fixes were required. The complete build and serial-output
logs are saved locally as `build/container-test/build.log` and
`build/container-test/runtime.log`; the script regenerates these on each run.

## Host ELF loading / CPU hold-start — 2026-10-07

Ran `J=2 scripts/test-pristine-docker.sh` against immutable source snapshot
`249f5b35e4a0a980b59676e1400879631048b0bf`, with the Ubuntu 24.04 digest above.
The full build and binary-only runtime driver exited **0**.

Both containers booted Linux and passed the mailbox-worker suite and the new
host ELF upload/verify/start/reload suite. The latter starts with empty MRAM,
`--start-held`, and **no backend ELF preload**, then requires a RISC-V mailbox
response after Linux uploads the image via xSPI. The runtime container also
booted/passed the stub guest suite.

The build container additionally passed 154 native ELF/fault tests, 28 source
update tests, eight preflight/runtime checks, real Kconfig refresh/optional-ELF
checks, and the backend reset suite (including pipelined requests, debug hold
exclusion and firmware-triggered debug reset with four harts).

An earlier binary-only attempt exposed a pre-existing mailbox qtest race: it
read the worker's startup marker immediately after asynchronous chip reset.
QEMU patch 0008 now waits with a bounded timeout and checks the exact marker
instead of accepting any nonzero value. The fresh rerun above includes that fix.

Recorded outputs include `ERBIUM-TEST-RESULT 0`,
`ERBIUM-LOAD-TEST-RESULT 0`, and `Binary-only container tests PASSED`.
Logs/artifacts remain under `build/container-test/` until the next run.

## Direct UART wiring — 2026-10-07

`J=2 scripts/test-pristine-docker.sh` passed from immutable source snapshot
`560baae711c542e85a1c30b190f999fe8a2c323f` with the Ubuntu 24.04 digest recorded
above. Fresh source acquisition applied eight QEMU patches and seventeen backend
patches. Both the full build container and the separate binary-only runtime
container exited **0**.

Both containers passed the existing mailbox/ELF-loader tests plus the new real
Linux UART1 test: empty MRAM, CPU held, ELF uploaded through guest xSPI, startup
banner on the independent serial cable, byte-transparent IRQ-driven echo and
repeated load on the same open tty. The runtime container also passed the stub
guest tests. Output included `ERBIUM-UART-TEST-RESULT 0` and
`Binary-only container tests PASSED`.

The source container additionally passed 154 loader, 35 console, 25 guest-UART
client, six runner/lifecycle, 28 source-update and eight preflight tests; real
Kconfig refresh; seven RTL-register sanitizer groups; socket lifecycle sanitizer
checks; endpoint/reset-domain integration; all six existing CPU-reset groups;
and two real-backend IRQ-fixture tests. IRQ fixture rounds each recorded three
UART interrupt claims and four WFI entries, including negative checks with UART
mask or PLIC routing disabled. These unit counters are not a console transport.

Outside the hermetic default image, the previously studied Kotama ELF also
passed both full guest paths with the new backend:

- `scripts/run-uart-test.sh --kotama`: guest xSPI upload/verify/start, UART1 banner,
  command `info`, fresh Erbium/RAM response and returned prompt.
- Interactive `erbctl console /dev/ttyAMA1 --load /firmware/host-payload.elf`:
  same early attachment/loading path, complete `info` response, Ctrl-] exit,
  restored Linux shell and clean shutdown. No development-host UART shortcut.

Implementation-session transcripts: `/tmp/erbium-uart-kotama-test.log` and
`/tmp/erbium-uart-interactive-kotama.log`. These temporary files are not release
artifacts. Pristine logs/archive remain under `build/container-test/` until the
next run. The ordinary release image contains hermetic fixtures, not Kotama.

Existing SmallVM transport regression also passed with the corrected sticky IRQ
acknowledgment: 135176 echoed bytes including a 131072-byte backpressure transfer.
The UART transport remains explicitly functional, not electrical/baud-bit-timed;
see `uart-console.md` for the exact scope and limitations.
