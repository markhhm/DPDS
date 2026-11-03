<!-- SPDX-License-Identifier: Apache-2.0 -->
# Quarter-rate A: four mirror sources, one capture port

Use the [quarter-rate matrix](../../examples/m6-x710.json) for independent external packet measurements.
Its [resource estimate](../reports/m6-quarter-rate.md) records scaling, exposure and steady mirror load.
The short full-rate campaign has a different measurement scope and topology.
Complete M5 clock and bypass qualification before A.

## Wiring and private bindings

Keep the generator external to the DUT. Use both generator directions concurrently when the condition requires them.
The single capture port is separate from the generator's transmitting ports.
Connect the four DUT-side mirror sources to one external 10G capture destination:

| Mirror source | Logical stream | Packet direction |
| --- | --- | --- |
| DUT port 0 RX | before, ingress 0 | 0 to 1 |
| DUT port 1 TX | receiver, egress 1 | 0 to 1 |
| DUT port 1 RX | before, ingress 1 | 1 to 0 |
| DUT port 0 TX | receiver, egress 0 | 1 to 0 |

Bind `CAPTURE_HOST` and `CAPTURE_IF` to the external collector and its one physical interface.
Both directional entries in `deployment.capture.interfaces` refer to `CAPTURE_IF`.
Bind `CAPTURE_CLOCK_ID`, `CAPTURE_MAPPING_ID` and `CAPTURE_CLOCK_SOURCE` to its qualified clock mapping.
Register private mirror endpoints, source-label mapping, capture command and restore command before traffic starts.
Do not use the DUT's software clock as the external before/after measurement clock.

## Source classification and identity matching

**The collector must preserve a trustworthy source label for each mirrored packet.**
The label distinguishes DUT port and RX/TX tap before identity matching.
One unlabelled aggregate stream cannot reliably distinguish before from receiver packets, especially when drops or duplicates occur.
The model VLAN identifies a line, not its mirror tap.
Do not guess tap identity from timestamp order or alternate duplicate identities.

The existing [PCAPNG adapter](../PCAPNG.md) consumes separate before and receiver files with interface-to-DUT-port mappings.
The approved collector must demultiplex source labels into those registered streams, preserving timestamps, wire lengths, identities and health evidence.
Source labels can be capture metadata or a qualified mirror encapsulation scheme.
The repository does not supply a switch-specific label generator or aggregate-stream demultiplexer.
If the deployed mirror cannot preserve and validate those labels, the A topology is blocked.

After tap classification, pair by direction, registered line/VLAN, flow identity and dense per-flow sequence.
Each flow starts at zero and increments by one independently.
The two VLAN groups in paired conditions retain distinct flow/line identities.
No pair, repeat, episode or direction shares a sequence namespace implicitly.

For each shared acquisition, retain separate `d0/pair.json` and `d1/pair.json` registrations when both directions are active.
Map the before interfaces to ingress ports and receiver interfaces to egress ports as in the table.
Use one external clock mapping for all taps. Preserve common start/stop and the existing calibration uncertainty.
Bind every pair to the same registered deployment and its own unchanged run ID.

## Capture qualification and limits

Keep snaplen at **128 bytes**. Identity headers and original wire length remain mandatory.
If encapsulation adds headers, qualify their removal and wire-length normalization before the adapter reads a file.
Account for encapsulation bytes on the mirror link even if storage snaplen truncates them.
Preserve final interface statistics and per-source capture-health evidence for each resulting stream.
Never invent zero drops or classify unmatched packets as intended Q drops to make a run pass.

The worst steady estimate is 9.5G, with 0.5G headroom on a 10G destination.
This does not bound instantaneous TX bursts, backlog release or the final drain.
Qualify mirror buffering, source labelling, collector packet rate and zero unexplained capture loss at those transitions.
Failure of these checks blocks external correctness evidence, even when average link load fits.

Judge complete captures with the existing [two-shard procedure](judge-shards.md), then apply K-1 only after its full-evidence checks.
Quarter-rate A supports external packet correctness at its registered rates.
It does not establish a full-rate B-prime PASS or independent full-rate OWD correctness.
