#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Machine values come from the operator's deployment environment.
set -eu
exec "${PYTHON:-python3}" "$(dirname "$0")/bringup_check.py" "$@"
