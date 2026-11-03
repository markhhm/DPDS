<!-- SPDX-License-Identifier: Apache-2.0 -->
# M6 diagnostic raw retention budget

This budget implements K-1 for the complete 360-condition matrix.
It covers 1,380 acquisitions and 1,960 directional reporter inputs.
It estimates retained raw bytes after complete judgment.
It does not qualify hardware or measure physical disk performance.
The [runner procedure](../M6-RUNNER.md#raw-retention-after-complete-judgment) preserves the complete sealed judgments.

## Formula

TB means 10^12 bytes. The raw limit is 2 TB.
Ten percent remains outside the planned raw allocation:

```text
planned = floor(2,000,000,000,000 * 0.90) = 1,800,000,000,000 bytes
quota = floor(planned / 1,380) = 1,304,347,826 bytes per acquisition
metadata_reserve = 65,536 bytes per acquisition
packet_rate = aggregate_offered_bps / (8 * mean_wire_bytes)
pcap_bytes_per_packet = mean(32 + pad4(min(128, q_bytes - 4)))
rate_native = ceil(packet_rate * 316)
rate_before = rate_receiver = ceil(packet_rate * pcap_bytes_per_packet)
rate_total = rate_native + rate_before + rate_receiver
N_ns = min(capture_duration_ns, floor((quota - metadata_reserve) * 10^9 / rate_total))
estimated_retained = ceil(rate_total * N_ns / 10^9) + metadata_reserve
```

The aggregate offered rate includes all active lines and directions.
The receiver estimate assumes delivery of every offer. Intentional drops cannot increase it.
Two simultaneous directions share one acquisition quota.
The generator requires independent, dense per-flow probe sequences.

The U profile has 1542 wire bytes and 160 bytes per PCAPNG packet at each point.
The X profile has equal packet counts at 84, 532 and 1542 wire bytes.
Its PCAPNG mean is `(92 + 160 + 160) / 3` bytes at each point.
Both external points count in the budget.

The measured native scenarios reached about 278 bytes per offer in the prior offline dry run.
The 316-byte planning scenario allows additional native density.
It is an estimate, not an upper bound for every possible capture.
The destructive stage enforces the actual sum of prefix sizes before any replacement.
An excess causes refusal. The operator must revise the registered budget before the first report or retain full raw elsewhere.

## Complete matrix allocation

Every run contains exact `keep_ns`, decimal `keep_seconds`, estimated bytes and rate components.
The following counts deduplicate shared acquisition IDs.

| Retained seconds N | Acquisitions |
| ---: | ---: |
| 0.635359182 | 162 |
| 0.705954647 | 118 |
| 1.264907727 | 162 |
| 1.270718363 | 220 |
| 1.405453029 | 138 |
| 1.411909294 | 160 |
| 2.529815447 | 220 |
| 2.810906055 | 200 |
| Total | 1,380 |

The estimated total is **1,799,999,999,718 bytes**.
The total integer quotas are **1,799,999,999,880 bytes**.
Both remain below 1.8 TB and leave at least 200 GB below the 2 TB raw limit.
No raw prefix combines evidence from different repeats or episodes.

## Evidence and peak storage

Complete judgment uses full native and both full captures before the cut.
Complete samples, C results, configurations, provenance, reports and seals remain available.
Prefixes support packet and event inspection only.
They cannot recompute a complete run or replace its full identity evidence.
Resume checks receipts and sealed artifacts before it returns the original result.

The quota covers declared raw prefixes only.
It excludes samples, reports, backups, filesystem overhead and full acquisitions that await judgment.
The prior dry run estimated about 122 GB for samples and reports across this matrix.
That estimate does not include external backups.
A cut temporarily needs the original files plus one acquisition's staged prefixes.
An interrupted process can leave staged files. Preserve receipts and inspect those files before manual cleanup.
Acquisition and offline worker rates determine the full-raw backlog and its peak storage.
The 1.8 TB retained allocation does not bound that backlog or total disk use.

## Software verification

Synthetic tests exercise healthy complete sampled reports through the real runner and C checker.
They cover missing seals and hashes, explicit backup confirmation, dry-run, quota refusal and source scope.
They also cover simultaneous directional workers, shared native files, hardlinked backups and interrupted replacement.
Retained native and PCAPNG readers validate event bytes, packet bytes, timestamps and diagnostic health.
Resume preserves complete report, sample, C result and seal bytes.
Changed raw, non-raw inputs, receipts or scientific outputs cause refusal.
No physical capture was acquired for this change.

## Independent short-run inspection

The [prefix reporter](../M6-RUNNER.md#inspect-a-retained-prefix-as-a-short-run) computes a separate diagnostic result from retained raw.
It uses the same reference, calibration and timing statistics as the reporter.
It preserves exact stress and trace arrival state within the retained prefix.
It does not fabricate final counter or drain evidence.
The complete sealed results remain unchanged.

Synthetic cases distinguish pending tail packets from due missing samples.
They also verify independent verdicts, real timing and Q faults, trace clamps and insufficient samples.
An error after the retained interval can fail the complete run while the diagnostic prefix passes its own checks.
The short result always keeps `g2_qualified=false` and declares its reduced coverage.
Its files reside in a new directory outside the complete results and raw captures.
