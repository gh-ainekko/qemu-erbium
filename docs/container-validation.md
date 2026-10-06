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
