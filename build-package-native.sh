#!/bin/bash
#
# Build the wlanpi-menu-tui .deb in a clean Debian container and lint it.
# Output: dist/wlanpi-menu-tui_<version>_<arch>.deb
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
SUITE="${SUITE:-trixie}"
IMAGE="wlanpi-menu-tui-builder-${SUITE}"

ENGINE=""
for e in podman docker; do
    if command -v "$e" >/dev/null 2>&1 && "$e" info >/dev/null 2>&1; then
        ENGINE="$e"; break
    fi
done
if [ -z "$ENGINE" ]; then
    echo "ERROR: neither podman nor docker is usable" >&2
    exit 1
fi

echo "== Building $IMAGE with $ENGINE"
"$ENGINE" build -f Dockerfile.build --build-arg SUITE="$SUITE" -t "$IMAGE" .

mkdir -p dist
echo "== Building package"
# Build from a copy so host artefacts (.venv, egg-info, caches) stay out of
# the build and nothing in the checkout ends up root-owned.
"$ENGINE" run --rm \
    -v "$SCRIPT_DIR":/src:ro \
    -v "$SCRIPT_DIR/dist":/out \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    "$IMAGE" bash -euc '
mkdir -p /build/wlanpi-menu-tui
tar -C /src --exclude=.git --exclude=.venv --exclude=dist --exclude="*.egg-info" \
    --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache -cf - . \
  | tar -C /build/wlanpi-menu-tui -xf -
cd /build/wlanpi-menu-tui
mk-build-deps --install --remove --tool "apt-get -y --no-install-recommends" debian/control >/dev/null 2>&1 || true
dpkg-buildpackage -us -uc -b
cd /build
lintian --info --display-info --pedantic --profile debian wlanpi-menu-tui_*.changes | tee lintian.txt || true
cp -v wlanpi-menu-tui_*.deb wlanpi-menu-tui_*.buildinfo wlanpi-menu-tui_*.changes lintian.txt /out/
chown "$HOST_UID:$HOST_GID" /out/*
'

echo
ls -lh dist/wlanpi-menu-tui_*.deb
echo "Install: sudo apt install ./dist/wlanpi-menu-tui_*.deb"
