#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# The complete check suite. The pre-push hook and CI both run exactly this script.
set -eu
cd "$(git rev-parse --show-toplevel)"
# The policy scan lives in the pinned core; it checks this repository's tree.
dpds-core/scripts/check-policy.sh
# Pinned core revision: its own policy scan, fmt, clippy --locked and tests.
(cd dpds-core && scripts/check.sh)
# Meson's core build target calls cargo by name.
. dpds-core/scripts/find-cargo.sh
if [ ! -f build/build.ninja ]; then
    meson setup build
fi
meson compile -C build
meson test -C build --print-errorlogs
python3 scripts/check-skips.py build/meson-logs/testlog.json
if [ -n "$(git status --porcelain)" ]; then
    echo "Checks modified the working tree:" >&2
    git status --short >&2
    exit 1
fi
echo "check.sh: all checks passed"
