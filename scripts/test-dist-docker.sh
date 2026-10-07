#!/usr/bin/env bash
# Validate a release in pristine Ubuntu with runtime packages only.
set -euo pipefail
if [ "$#" -ne 1 ] || [ ! -s "$1" ] || [ ! -s "$1.sha256" ]; then
  echo "usage: $0 ARCHIVE.tar.gz (with adjacent .sha256 file)" >&2
  exit 2
fi
ARCHIVE=$(basename "$1")
NAME="erbium-runtime-$$"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker create --name "$NAME" --cpus "${J:-2}" -w /opt/erbium \
  -e ARCHIVE="$ARCHIVE" "${DOCKER_IMAGE:-ubuntu:24.04}" bash -lc '
    set -euo pipefail
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      libglib2.0-0t64 libpixman-1-0 libfdt1 libslirp0 libgcrypt20 libattr1 zlib1g libstdc++6 libgcc-s1
    sha256sum -c "$ARCHIVE.sha256"
    tar xzf "$ARCHIVE"
    timeout 180 dist/scripts/run-e2e.sh
    timeout 120 dist/scripts/run-linux.sh --test > /tmp/guest-stub.log 2>&1
    cat /tmp/guest-stub.log
    grep -q "ERBIUM-TEST-RESULT 0" /tmp/guest-stub.log
  ' >/dev/null
docker cp "$1" "$NAME:/opt/erbium/"
docker cp "$1.sha256" "$NAME:/opt/erbium/"
docker start -a "$NAME"
[ "$(docker inspect -f '{{.State.ExitCode}}' "$NAME")" = 0 ]
echo 'Binary-only container tests PASSED'
