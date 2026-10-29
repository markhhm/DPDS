#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
set -eu
cd "$(git rev-parse --show-toplevel)"
destination=$(git rev-parse --git-path hooks)/pre-push
if [ -e "$destination" ] && ! cmp -s scripts/git-hooks/pre-push "$destination"; then
    echo "Existing pre-push hook differs; integrate it before installing." >&2
    exit 1
fi
install -m 755 scripts/git-hooks/pre-push "$destination"
echo "Installed $destination"
