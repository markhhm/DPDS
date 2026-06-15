#!/bin/sh
set -eu

# Keep Rust reachable even if this runs without shell init files.
for cargo_bin in \
  "/usr/local/bin" \
  "${CARGO_HOME:-$HOME/.cargo}/bin" \
  "/root/.cargo/bin"
do
  if [ -d "${cargo_bin}" ]; then
    PATH="${cargo_bin}:${PATH}"
  fi
done
export PATH

if ! command -v rustc >/dev/null 2>&1; then
  echo "rustc not found in PATH"
  exit 1
fi

rustc --version
pkg-config --modversion libdpdk
