#ifndef COMMON_H
#define COMMON_H
#include <stdint.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <rte_ring.h>
#include <rte_mempool.h>
#include <rte_mbuf.h>
#include <libdpds_core.h>
#include "config.h"

extern _Atomic uint8_t quit_signal_receive, quit_signal_worker, quit_signal_transmit;
#define HNG_MAX_LINES 4
struct packet_meta {
    uint64_t arrival_ns, ingress_seq, release_ns, deadline_ns, pending_since_ns, retry_since_ns;
    bool retry_active;
    uint16_t ingress_port, vlan_id, line;
};
static inline struct packet_meta *packet_meta(struct rte_mbuf *m) {
    return (struct packet_meta *)rte_mbuf_to_priv(m);
}
struct ingress_frontier {
    _Atomic uint64_t version, enqueued, watermark_ns;
};
struct host_context {
    struct Engine *engine;
    struct LineInfo lines[HNG_MAX_LINES];
    struct TxPolicy *policies[HNG_MAX_LINES];
    size_t line_count;
    uint16_t ports[RTE_MAX_ETHPORTS], port_count;
    struct ingress_frontier frontier[RTE_MAX_ETHPORTS];
    uint64_t epoch_cycles, cycles_hz;
    _Atomic bool invalid;
};
extern struct host_context host;
uint64_t host_now_ns(void);
bool host_load(const char *path);
void host_cleanup(void);
void host_invalidate(const char *reason);
void host_publish(uint16_t port, uint64_t enqueued, uint64_t watermark);
bool host_frontier(uint16_t port, uint64_t consumed, uint64_t *watermark);

struct rx_stats {
    uint64_t received, received_bytes, enqueued, rx_ring_drops, rx_ring_drop_bytes;
    uint64_t burst_hist[BURST_HIST_BUCKETS];
} __attribute__((aligned(64)));
struct worker_stats {
    uint64_t received, received_bytes, sent, sent_bytes, emulator_drops, tx_ring_drops;
    uint64_t invalid_drops, invalid_drop_bytes, storage_drops, storage_drop_bytes;
    uint64_t shutdown_drops, shutdown_drop_bytes, tx_ring_drop_bytes;
    struct HngStats lines[HNG_MAX_LINES];
} __attribute__((aligned(64)));
struct tx_stats {
    uint64_t received, received_bytes, transmitted, transmitted_bytes, tx_drops, tx_drop_bytes;
    uint64_t retries, pending_peak, trace_outage_drops, trace_outage_bytes;
    uint64_t late_submit_packets, late_submit_total_ns, late_submit_max_ns;
    uint64_t line_outage_drops[HNG_MAX_LINES], line_invalid_drops[HNG_MAX_LINES];
    uint64_t line_transmitted[HNG_MAX_LINES], line_transmitted_bytes[HNG_MAX_LINES];
    uint64_t burst_hist[BURST_HIST_BUCKETS];
} __attribute__((aligned(64)));
struct pipeline_stats { struct rx_stats rx; struct worker_stats worker; struct tx_stats tx; };
struct lcore_parameters {
    unsigned int worker_id;
    struct rte_ring *receive_worker_ring, *worker_transmit_ring;
    struct pipeline_stats *stats;
    uint16_t num_ports;
};
void signal_handler(int signum);
int initialize_port(uint16_t port, struct rte_mempool *pool);
int close_port(uint16_t port);
#endif
