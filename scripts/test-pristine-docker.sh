#!/usr/bin/env bash
# Test the committed checkout without host tools, sources, build trees or caches.
# Requires Docker and git on the host. Logs/artifacts go in build/container-test/.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${DOCKER_IMAGE:-ubuntu:24.04}
J=${J:-2}
OUT="$R/build/container-test"
NAME="erbium-pristine-$$"
mkdir -p "$OUT"
cleanup() { docker rm -f "$NAME-build" >/dev/null 2>&1 || true; }
trap cleanup EXIT
command -v docker >/dev/null || { echo 'Docker is required' >&2; exit 1; }
docker pull "$IMAGE"
git -C "$R" rev-parse HEAD | tee "$OUT/commit.txt"
docker image inspect "$IMAGE" --format '{{json .RepoDigests}}' | tee "$OUT/image.txt"
docker create --name "$NAME-build" --cpus "$J" -e J="$J" \
  -e SOURCE_DATE_EPOCH="$(git -C "$R" log -1 --format=%ct)" \
  -w /work/qemu-erbium "$IMAGE" bash -lc '
    set -euo pipefail
    ./bootstrap.sh
    python3 tests/test_preflight.py
    python3 -m unittest discover -s tests -p "test_erbctl_loader.py"
    python3 -m unittest discover -s tests -p "test_fetch_sources.py"
    python3 et-platform/sw-sysemu/tests/erbium/host/test_cpu_reset.py --emu build/sw-sysemu/erbium_emu
    python3 tests/test_linux_config.py
    scripts/package-dist.sh pristine
  ' >/dev/null
git -C "$R" archive HEAD | docker cp - "$NAME-build:/work/qemu-erbium"
docker start -a "$NAME-build" 2>&1 | tee "$OUT/build.log"
[ "$(docker inspect -f '{{.State.ExitCode}}' "$NAME-build")" = 0 ]
docker cp "$NAME-build:/work/qemu-erbium/out/." "$OUT/"

# Only the release archive enters the runtime container: no compilers or source.
"$R/scripts/test-dist-docker.sh" "$OUT/erbium-emu-dist-pristine-ubuntu24.04-x86_64.tar.gz" \
  2>&1 | tee "$OUT/runtime.log"
echo "Pristine build and binary-only runtime tests PASSED. Logs/artifacts: $OUT"
