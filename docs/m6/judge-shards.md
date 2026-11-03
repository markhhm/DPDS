<!-- SPDX-License-Identifier: Apache-2.0 -->

# Two judges for M6 captures

Judge completed conditions while the capture machine acquires later conditions.
Use the same frozen source revisions and master matrix on both judges.
This procedure starts no generator, device or capture. It deletes no raw data.
macOS arm64 and Python 3.14 execution require a separate platform check.
The local verification used Linux x86-64, clang 19 and Python 3.13.

## Assignment and cost model

[The full matrix](../../examples/m6-x710.json) contains `plan.judge_shards`.
Shards `a` and `b` each own 180 of the 360 conditions. Their lists are disjoint
and cover the complete inventory. All repeats, episodes and directions of one
condition stay together. A shared native capture therefore has one judge owner.
The [selector](../../tools/g2/judge_shards.py) never splits time or packet ranges.

The model estimates CPU cost from every offered packet, with warmup and
packets that the DUT later drops. It uses the registered mean wire size.
Each directional report currently reads the complete shared bidirectional native
capture. The estimate includes that repeated work, repeat/episode counts and
trace startup for every active profile. It does not divide work by the sample mask.

| Input class | Estimated ns per processed offer |
| --- | ---: |
| Fixed U / X | 6573 / 22680 |
| Trace calibration U / X | 10530 / 25440 |
| Trace stress U / X | 8710 / 25110 |

Fixed startup is 0.26 s for U and 0.34 s for X. Trace startup is 9.17 s per profile.
These are software-only, warm-cache pipeline estimates. Earlier X fixtures used
global-strided IDs. Dense IDs can reduce actual index work. The coefficients do
not qualify either judge's speed, disk bandwidth or concurrent performance.

Sort conditions by integer estimated cost, largest first, then by ID. Assign each
condition to the cheaper shard, with an exact half-count cap. Break ties by
count, then shard name. Input order does not change the assignment.

For the committed quarter-rate A inventory, each shard estimates **58.06 core-hours**.
The estimated difference is **0%**, below the 5% limit relative to mean shard cost.
Equal estimated work does not guarantee equal wall time on different machines.
See the [resource estimate](../reports/m6-quarter-rate.md) for exposure, raw retention and sample-size assumptions.

Regenerate registration before the campaign if the approved model or conditions change:

```sh
cd "$DPDS_SRC"
"$PYTHON" -m tools.g2.expand_m6_x710 --out "$NEW_MATRIX_TEMPLATE"
```

Bind private paths and deployment values into `MASTER_MATRIX`. Freeze its bytes
before dispatch. Never edit a dispatched matrix or reuse another job's output directory.
The selector rejects changed assignments and preserves every registered run.
It converts outer manifest paths to absolute paths relative to the master matrix.
A selected job reports only its selected conditions, not the complete campaign.

## Private values and common paths

| Variables | Requirement |
| --- | --- |
| `DPDS_SRC`, `HOST_REV`, `CORE_REV` | Source checkout and approved exact revisions, including the Core submodule |
| `JUDGE_ROOT` | Approved persistent root with the **same absolute path** on source, judges and return verifier |
| `MASTER_MATRIX`, `SHARD`, `CONDITION_ID` | Bound full master, assigned `a` or `b`, and one fully closed condition from that shard |
| `JUDGE_VENV`, `OPENSSL_PREFIX`, `CLANG`, `RSYNC` | Private virtual environment, OpenSSL installation and executable paths |
| `CAPTURE_ENDPOINT`, `CLOSED_FILES` | Approved rsync endpoint and relative file list for the closed condition's complete source closure |
| `RETURN_ENDPOINT`, `RETURN_STAGE`, `RETURN_ARCHIVE` | Approved result destination, new staging directory and new local archive outside sealed output |
| `MEMORY_LIMIT_BYTES`, `JUDGE_WORKERS` | Approved per-process budget, initially 4294967296 bytes, and reporter process count, initially 1 |

Preserve all referenced native, pcapng, counter, configuration, trace and calibration
files at their registered relative locations. Keep `MASTER_MATRIX` under `JUDGE_ROOT`.
`CLOSED_FILES` includes the frozen master matrix, every repeat, episode, direction
and referenced shared file. The capture owner confirms closure
before transfer. This document adds no readiness service or queue daemon.

Completion receipts contain absolute source paths. Derived bundles can contain
absolute symlinks. Preserve the common namespace and symlinks for return verification
and resume. Do not rewrite receipts, dereference links or rename sealed trees.
If that namespace is unavailable, stop and arrange the operator's path mapping first.
An archive at another prefix is evidence storage, not a validated local resume tree.

## macOS preparation and small software check

The operator runs these commands locally on the approved judge. Required tools:
Xcode Command Line Tools, [uv](https://docs.astral.sh/uv/guides/install-python/),
[OpenSSL 3](https://formulae.brew.sh/formula/openssl@3), and rsync 3 with protected arguments.
Use native arm64 tools. Do not use a different architecture through emulation.

```sh
set -eu
cd "$DPDS_SRC"
test "$(git rev-parse HEAD)" = "$HOST_REV"
git submodule update --init --recursive
test "$(git -C dpds-core rev-parse HEAD)" = "$CORE_REV"
umask 077
mkdir -p "$JUDGE_ROOT/tools"
uv python install 3.14
uv venv --python 3.14 "$JUDGE_VENV"
uv pip install --python "$JUDGE_VENV/bin/python" --only-binary=:all: \
  -r dpds-core/scripts/trace-requirements.txt
PYTHON="$JUDGE_VENV/bin/python"
CLANG="$(xcrun --find clang)"
OPENSSL_PREFIX="$(brew --prefix openssl@3)"
SEQCHECK_BINARY="$JUDGE_ROOT/tools/seqcheck"
export PYTHON SEQCHECK_BINARY
"$PYTHON" -c 'import platform,sys,numpy; assert sys.version_info[:2]==(3,14); assert platform.machine()=="arm64"; print(sys.version,platform.platform(),numpy.__version__)'
"$CLANG" -std=c11 -O3 -Wall -Wextra -Werror -pthread \
  -I"$OPENSSL_PREFIX/include" tools/seqcheck/seqcheck.c \
  -L"$OPENSSL_PREFIX/lib" -Wl,-rpath,"$OPENSSL_PREFIX/lib" -lcrypto -o "$SEQCHECK_BINARY"
export PATH="$JUDGE_ROOT/tools:$PATH"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1
"$PYTHON" -m unittest -v tools.seqcheck.test_seqcheck
"$PYTHON" -m unittest -v tools.g2.tests.test_sampled_binary tools.g2.tests.test_matrix_seqcheck
```

Stop on a compiler, dependency or test error. Record Python, NumPy, compiler,
OpenSSL and source versions privately. Confirm that the small native-binary
sampled tests run, including auto seqcheck and resume. Synthetic tests do not
qualify physical clocks or throughput. Do not run the DPDK host on this judge.

The C source selects Darwin's `stat` nanosecond members and keeps full precision.
See [Apple's stat definitions](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/stat.h).
The stdout failure fixture uses POSIX file-size limits instead of `/dev/full`.

The invalid UTF-8 filename fixture is skipped on macOS because APFS does not
accept those names. Report it as not executed, never PASS. Linux still runs it
on a filesystem that accepts invalid UTF-8 names.

GNU `prlimit` is not a macOS tool. Do not set `RLIMIT_AS` on macOS. Darwin
[aliases it with its resident-set limit](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/resource.h),
and the limit request can fail. Measure each job with `/usr/bin/time -l` and
record its maximum resident set size in bytes. If a single job exceeds
4294967296 bytes (4 GiB), stop and report it.

In a macOS sandbox, `/usr/bin/time -l` can fail with
`time: sysctl kern.clockrate: Operation not permitted` and exit 1 even when
the child succeeds. Keep that failure in the evidence; it is not a successful
RSS measurement. In that environment, replace the `time -l` job command with
the following `os.wait4` wrapper. Run one complete job at a time. Darwin's
`ru_maxrss` is **bytes**, whereas Linux reports KiB. The seqcheck benchmark's
`max_rss_kib` field converts Darwin bytes to KiB.

```sh
"$PYTHON" - "$JOB_MATRIX" "$JOB_OUT" "$JUDGE_WORKERS" <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys
import time

assert sys.platform == 'darwin'
matrix, output, workers = sys.argv[1:]
workers = int(workers)
assert workers > 0
start = time.perf_counter()
process = subprocess.Popen([sys.executable, '-m', 'tools.g2.matrix',
                            matrix, '--out', output])
_, status, usage = os.wait4(process.pid, 0)
process.returncode = os.waitstatus_to_exitcode(status)
measurement = dict(exit=process.returncode, wall_seconds=time.perf_counter() - start,
                   cpu_seconds=usage.ru_utime + usage.ru_stime,
                   peak_rss_bytes=usage.ru_maxrss,
                   rss_interface='Darwin os.wait4 ru_maxrss; bytes', workers=workers)
Path(output + '.rusage.json').write_text(json.dumps(measurement, indent=2) + '\n')
if process.returncode:
    raise SystemExit(process.returncode if process.returncode > 0 else 128 - process.returncode)
if usage.ru_maxrss > 4294967296 or workers * usage.ru_maxrss > 25769803776:
    raise SystemExit('STOP: measured RSS exceeds the judge memory budget')
PY
```

This records the completed job's exit status, wall time, CPU time and peak RSS
beside its output directory. The budget check runs after completion; stop a
running job if observed RSS exceeds 4 GiB. A resumed job that only skips
completed runs does not measure the full job's peak RSS. If `time -l` already
completed the child, preserve its output and measure the next complete job
with the wrapper rather than treating a resume-only measurement as its peak.

Retain the registered seqcheck budget and start with one reporter process.
After platform checks, require `JUDGE_WORKERS` times the largest measured job
peak RSS to be at most 25769803776 bytes (24 GiB). On a 32 GiB judge, reserve
8 GiB for the system and other workloads. Include all child processes in the
memory budget. Do not increase concurrency automatically or select it from
the CPU count alone.

## Transfer and judge one complete condition

Repeat for newly closed conditions on the assigned list while capture continues.
Never transfer an actively written file. The capture owner supplies the complete
relative `CLOSED_FILES` list under the common root. Keep rsync's exit status.
A partial transfer is not a ready job. Raw hashes and capture health remain
subject to the existing reporter's full checks.

```sh
# Judge: COMMON paths and all private values already set.
JOB_DIR="$JUDGE_ROOT/jobs/$CONDITION_ID"
JOB_MATRIX="$JOB_DIR/matrix.json"
JOB_OUT="$JUDGE_ROOT/results/$CONDITION_ID"
mkdir -p "$JOB_DIR" "$JUDGE_ROOT/results"
"$RSYNC" -a --partial --protect-args --files-from="$CLOSED_FILES" \
  "$CAPTURE_ENDPOINT:$JUDGE_ROOT/" "$JUDGE_ROOT/"
cd "$DPDS_SRC"
"$PYTHON" -m tools.g2.judge_shards "$MASTER_MATRIX" --shard "$SHARD" \
  --condition "$CONDITION_ID" --workers "$JUDGE_WORKERS" --out "$JOB_MATRIX"
"$PYTHON" -m tools.g2.matrix "$JOB_MATRIX" --preflight
/usr/bin/time -l "$PYTHON" -m tools.g2.matrix "$JOB_MATRIX" --out "$JOB_OUT"
```

The `time -l` command above is for macOS. On Linux use the same job matrix with
`prlimit --as=4294967296 -- "$PYTHON" -m tools.g2.matrix "$JOB_MATRIX" --out "$JOB_OUT"`.
For a restart, reuse the existing job matrix and output on that judge.
Do not regenerate the exclusive job file. The runner verifies sealed inputs
and outputs, skips valid results and preserves incomplete attempts.

Expect `summary.json`, each run's report/verdict/CSV/text, checker evidence and
`.complete.json`. Verify that all selected IDs appear and every completed run
has its source/output-bound receipt. Preserve FAIL and INCONCLUSIVE decisions.
A CLI status alone is not proof of completion or physical PASS.

Without `--condition`, the selector emits the whole half matrix. Use that form
only when all of its conditions are closed. It is not a live-tail reporter.
To choose another condition after this job, keep its original shard ownership.
Do not alter source paths, clock scope, sampling, C/Q settings or sequence policy.

## Return reports and seals

Stop all writers for the selected result tree. Include the immutable job matrix,
reports, C results, receipts and private version logs. Keep raw outside this archive.
Tar preserves symlinks by default. Do not use its dereference option or rsync `-L`.

```sh
# Judge: new archive path, outside jobs/results; preserve the relative tree.
set -eu
set -C
COPYFILE_DISABLE=1 tar -C "$JUDGE_ROOT" -cf - \
  "jobs/$CONDITION_ID" "results/$CONDITION_ID" > "$RETURN_ARCHIVE"
(
  cd "$(dirname "$RETURN_ARCHIVE")"
  shasum -a 256 "$(basename "$RETURN_ARCHIVE")" > "$(basename "$RETURN_ARCHIVE").sha256"
)
"$RSYNC" -a --partial --protect-args "$RETURN_ARCHIVE" "$RETURN_ARCHIVE.sha256" \
  "$RETURN_ENDPOINT:$RETURN_STAGE/"
```

The receiver checks the archive digest and contents before extraction. Restore
under the same common root, with no existing condition job/result tree.
Use tar's keep-existing-files option. Do not overwrite another judge's results.

All selected condition IDs must occur once across the two shard lists and their
job summaries. Retain each job's source-matrix SHA256 for this coverage audit.
A job PASS does not establish that all 360 conditions finished.

```sh
# Return verifier: digest first, empty destination trees, preserve existing files.
set -eu
cd "$RETURN_STAGE"
ARCHIVE_NAME="$(basename "$RETURN_ARCHIVE")"
shasum -a 256 -c "$ARCHIVE_NAME.sha256"
tar -tf "$ARCHIVE_NAME"
test ! -e "$JUDGE_ROOT/jobs/$CONDITION_ID"
test ! -L "$JUDGE_ROOT/jobs/$CONDITION_ID"
test ! -e "$JUDGE_ROOT/results/$CONDITION_ID"
test ! -L "$JUDGE_ROOT/results/$CONDITION_ID"
tar -C "$JUDGE_ROOT" -xkf "$RETURN_STAGE/$ARCHIVE_NAME"
```

Only after verified return and the separate retention owner's required checks
can that owner authorize raw deletion. This procedure sets no deletion or backup
confirmation flag. Keep raw available while a judge or resume still needs it.
