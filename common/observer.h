/* SPDX-License-Identifier: Apache-2.0 */
#ifndef HNG_OBSERVER_H
#define HNG_OBSERVER_H
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <assert.h>
#include "config.h"
#define OBS_PORTS 2
#define OBS_PRODUCERS 7
#define OBS_QUEUES 2
enum observer_producer { OBS_RX, OBS_WORKER0, OBS_WORKER1, OBS_TX, OBS_CONTROL, OBS_RX1, OBS_TX1 };
enum observer_abort_reason { OBS_ABORT_DRAIN = 1, OBS_ABORT_PRODUCER = 2, OBS_ABORT_RESOURCE = 4, OBS_ABORT_REQUESTED = 8 };
enum observer_identity_policy { OBS_ID_STAGE1, OBS_ID_SOFTWARE_L2 };
enum observer_kind { OBS_PACKET, OBS_OFFER, OBS_TERMINAL, OBS_FRONTIER, OBS_END, OBS_DISPATCH, OBS_ATTEMPT, OBS_RESOURCES };
enum observer_reason { OBS_Q_DROP, OBS_TRACE_LOSS, OBS_TRACE_OUTAGE, OBS_STORAGE, OBS_INVALID_INPUT,
    OBS_HANDOFF, OBS_TERMINAL_TX, OBS_MISSED_OUTAGE, OBS_SHUTDOWN };
struct observer_packet {
    uint64_t identity, ingress_seq, selection_seq, arrival_ns, handoff_seq, gate_ns, rx_flags;
    uint32_t source, destination, length;
    uint16_t source_port, destination_port, ingress_port, line, vlan_id, segments, handoff_queue;
    uint8_t identity_kind, selected_late, input_error;
    bool classified, stripped;
};
struct observer_snapshot {
    uint64_t seen[OBS_PORTS], lower_bound[OBS_PORTS];
    uint16_t ports[OBS_PORTS], count;
    bool ended[OBS_PORTS];
};
struct observer_record {
    uint64_t producer_seq, order, at_ns, gate_ns, submit_before_ns, submit_after_ns;
    uint64_t resources[10];
    uint64_t actual_selection_seq, attempt_id, consumed_dispatch[OBS_QUEUES], first_handoff_seq;
    struct observer_snapshot snapshot;
    enum observer_kind kind;
    enum observer_reason reason;
    uint16_t port, count, accepted_count, handoff_queue;
    struct observer_packet items[MAX_BURST_SIZE];
};
/* Only the declared item prefix is initialized, copied and consumed. */
static inline void observer_record_init(struct observer_record *record, enum observer_kind kind, uint16_t count)
{
    assert(count <= MAX_BURST_SIZE);
    memset(record, 0, offsetof(struct observer_record, items) + count * sizeof(record->items[0]));
    record->kind = kind; record->count = count;
}
struct observer_health {
    uint64_t records_attempted, records_emitted, records_dropped, capture_drops;
    uint64_t queue_high_water, queue_capacity, identity_errors, malformed_records;
};
struct observer_storage_health {
    uint64_t blocks_capacity, blocks_high_water, blocks_pending;
    int cpu;
};
/* Read after close; storage metrics do not change the JSONL evidence contract. */
struct observer_storage_health observer_storage_health(void);
struct rte_mbuf;
/* Setup/teardown run on the control thread; fd must be a pipe or socket. */
bool observer_open(int fd, enum observer_identity_policy policy, unsigned probe_port, unsigned capacity, bool per_direction);
/* Binary capture accepts an empty regular file; the consumer owns all writes. */
bool observer_open_binary(int fd, enum observer_identity_policy policy, unsigned probe_port, unsigned capacity, bool per_direction);
/* Software rate check only: /dev/null is not an evidence capture. */
bool observer_open_binary_discard(int fd, enum observer_identity_policy policy, unsigned probe_port, unsigned capacity, bool per_direction);
bool observer_start(void);
bool observer_close(bool complete);
void observer_abort(enum observer_abort_reason reason);
bool observer_enabled(void);
bool observer_emit(enum observer_producer producer, const struct observer_record *record);
/* Reserve before publishing a handoff; commit after its successful prefix is known. */
struct observer_record *observer_reserve(enum observer_producer producer);
void observer_commit(enum observer_producer producer);
/* One scheduler writer per owner; completed publications use a three-slot SPSC mailbox. */
void observer_publish_snapshot(unsigned owner, const struct observer_snapshot *snapshot);
/* One TX reader per mailbox; bounded reads retain the last completed publication. */
bool observer_load_snapshot(enum observer_producer producer, struct observer_snapshot *snapshot);
struct observer_health observer_health(enum observer_producer producer);
struct observer_packet observer_packet(const struct rte_mbuf *packet);
void observer_input(enum observer_producer producer, struct rte_mbuf *packet);
void observer_terminal(enum observer_producer producer, const struct rte_mbuf *packet,
    enum observer_reason reason, uint64_t at_ns);
void observer_input_end(enum observer_producer producer, uint16_t port, uint64_t next);
/* Control-thread resource snapshots, before launch and after port close. */
void observer_resources(uint16_t port, bool end, uint64_t total, uint64_t free_count,
    const struct rte_eth_stats *nic);
#endif
