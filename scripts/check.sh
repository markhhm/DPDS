#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# The complete check suite. The pre-push hook and CI both run exactly this script.
set -eu
cd "$(git rev-parse --show-toplevel)"
# The policy scan lives in the pinned core; it checks this repository's tree.
dpds-core/scripts/check-policy.sh
# Pinned core revision: its own policy scan, fmt, clippy --locked and tests.
# A pre-push hook in a worktree exports GIT_DIR, which would make the core's git commands see this repository.
(cd dpds-core && env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE scripts/check.sh)
# Meson's core build target calls cargo by name.
. dpds-core/scripts/find-cargo.sh
if [ ! -f build/build.ninja ]; then
    meson setup build
fi
meson compile -C build
# Without --in-memory DPDK leaves 3-20 MB of EAL runtime files per run; they filled a tmpfs /tmp once.
dpdk_run="${XDG_RUNTIME_DIR:-/tmp}/dpdk"
ls "$dpdk_run" 2>/dev/null | grep '^hng-' | sort > build/dpdk-before.txt || true
meson test -C build --print-errorlogs
ls "$dpdk_run" 2>/dev/null | grep '^hng-' | sort > build/dpdk-after.txt || true
# EAL still creates an empty dir per run under --in-memory; rmdir fails if a run left files.
for d in $(comm -13 build/dpdk-before.txt build/dpdk-after.txt); do
    if ! rmdir "$dpdk_run/$d"; then
        echo "A test left files in $dpdk_run/$d (missing --in-memory?)" >&2
        exit 1
    fi
done
python3 scripts/check-skips.py build/meson-logs/testlog.json
if [ -n "$(git status --porcelain)" ]; then
    echo "Checks modified the working tree:" >&2
    git status --short >&2
    exit 1
fi
echo "check.sh: all checks passed"
