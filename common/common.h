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
/* One cache line per port: owners of different ports must not share a line. */
struct ingress_frontier {
    _Atomic uint64_t version, enqueued, watermark_ns;
} __attribute__((aligned(64)));
_Static_assert(sizeof(struct ingress_frontier) == 64, "one frontier per cache line");
struct port_resources { uint16_t rx_descriptors, tx_descriptors; bool prepared; };
struct host_context {
    struct Engine *engine;
    struct LineInfo lines[HNG_MAX_LINES];
    struct TxPolicy *policies[HNG_MAX_LINES];
    /* Taken by host_load on the main lcore, before any owner launches. */
    struct Line *line_handle[HNG_MAX_LINES];
    _Atomic bool launched;
    size_t line_count;
    uint16_t ports[RTE_MAX_ETHPORTS], port_count;
    struct ingress_frontier frontier[RTE_MAX_ETHPORTS];
    struct port_resources resources[RTE_MAX_ETHPORTS];
    uint64_t epoch_cycles, cycles_hz;
    _Atomic bool invalid;
    /* The last scheduler to finish releases TX; 0 or 1 means a single scheduler. */
    _Atomic int schedulers_running;
    /* The last RX lcore to finish releases the schedulers; 0 or 1 means a single RX lcore. */
    _Atomic int receivers_running;
};
extern struct host_context host;
uint64_t host_now_ns(void);
bool host_load(const char *path);
/* Per-direction owners need every egress port fed by lines of one ingress port. */
bool host_split_valid(void);
/* Take every line handle; fails and invalidates once owners may run (host_mark_launched). */
bool host_take_line_handles(void);
/* Call on the main lcore immediately before launching pipeline lcores. */
void host_mark_launched(void);
void host_cleanup(void);
void host_invalidate(const char *reason);
void host_publish(uint16_t port, uint64_t enqueued, uint64_t watermark);
bool host_frontier(uint16_t port, uint64_t consumed, uint64_t *watermark);

/* Counter structs merged across lcores are defined from field lists, so the merge
 * (rx_stats_merge, tx_stats_merge) handles every field by name with its declared
 * rule: SUM or MAX. A field outside the list is never merged. */
#define STATS_DECLARE_S(name, rule) uint64_t name;
#define STATS_DECLARE_A(name, count, rule) uint64_t name[count];
#define STATS_SUM(d, s) (d) += (s);
#define STATS_MAX(d, s) if ((s) > (d)) (d) = (s);
#define STATS_MERGE_S(name, rule) STATS_##rule(dst->name, src->name)
#define STATS_MERGE_A(name, count, rule) for (size_t i = 0; i < (count); ++i) STATS_##rule(dst->name[i], src->name[i])
/* ring_full_skips: polls skipped because that port's scheduler ring had no room; indexed like host.ports. */
#define RX_STATS_FIELDS(S, A) \
    S(received, SUM) S(received_bytes, SUM) S(enqueued, SUM) S(rx_ring_drops, SUM) S(rx_ring_drop_bytes, SUM) \
    A(ring_full_skips, 2, SUM) A(burst_hist, BURST_HIST_BUCKETS, SUM)
struct rx_stats { RX_STATS_FIELDS(STATS_DECLARE_S, STATS_DECLARE_A) } __attribute__((aligned(64)));
_Static_assert(_Alignof(struct rx_stats) == 64 && sizeof(struct rx_stats) % 64 == 0,
    "rx_stats owners write different cache lines");
struct worker_stats {
    uint64_t received, received_bytes, sent, sent_bytes, emulator_drops, tx_ring_drops;
    uint64_t invalid_drops, invalid_drop_bytes, storage_drops, storage_drop_bytes;
    uint64_t shutdown_drops, shutdown_drop_bytes, tx_ring_drop_bytes;
    struct HngStats lines[HNG_MAX_LINES];
} __attribute__((aligned(64)));
_Static_assert(_Alignof(struct worker_stats) == 64 && sizeof(struct worker_stats) % 64 == 0,
    "worker_stats owners write different cache lines");
#define TX_STATS_FIELDS(S, A) \
    S(received, SUM) S(received_bytes, SUM) S(transmitted, SUM) S(transmitted_bytes, SUM) \
    S(tx_drops, SUM) S(tx_drop_bytes, SUM) S(retries, SUM) S(pending_peak, MAX) \
    S(trace_outage_drops, SUM) S(trace_outage_bytes, SUM) \
    S(late_submit_packets, SUM) S(late_submit_total_ns, SUM) S(late_submit_max_ns, MAX) \
    A(line_outage_drops, HNG_MAX_LINES, SUM) A(line_invalid_drops, HNG_MAX_LINES, SUM) \
    A(line_transmitted, HNG_MAX_LINES, SUM) A(line_transmitted_bytes, HNG_MAX_LINES, SUM) \
    A(burst_hist, BURST_HIST_BUCKETS, SUM)
struct tx_stats { TX_STATS_FIELDS(STATS_DECLARE_S, STATS_DECLARE_A) } __attribute__((aligned(64)));
_Static_assert(_Alignof(struct tx_stats) == 64 && sizeof(struct tx_stats) % 64 == 0,
    "tx_stats owners write different cache lines");
/* owners[k] holds the counters of the scheduler for host.ports[k] in split layouts;
 * rx_owners[k] and tx_owners[k] those of direction k's RX and TX lcores (7 lcores). */
struct pipeline_stats {
    struct rx_stats rx; struct worker_stats worker; struct tx_stats tx; struct worker_stats owners[2];
    struct rx_stats rx_owners[2]; struct tx_stats tx_owners[2];
};
struct lcore_parameters {
    unsigned int worker_id;
    struct rte_ring *receive_worker_ring, *worker_transmit_ring;
    struct pipeline_stats *stats;
    uint16_t num_ports;
    /* Split layout: one scheduler per ingress port host.ports[owner_index].
     * io_split (7 lcores): RX and TX lcores are per direction owner_index too. */
    bool split, io_split;
    uint16_t owner_index;
    struct rte_ring *owner_rings[2];
};
void signal_handler(int signum);
int host_prepare_port(uint16_t port);
unsigned host_pool_objects(uint16_t ingress_port, unsigned lcores);
/* The production per-ingress-port mbuf pool on the port's NUMA node; NULL on failure. */
/* NIC-side receive loss (imissed, rx_nombuf) is infrastructure loss: invalidate, return false. */
bool host_check_nic_loss(uint16_t port, const struct rte_eth_stats *stats);
struct rte_mempool *host_create_pool(uint16_t ingress_port, unsigned lcores, int *socket);
int initialize_port(uint16_t port, struct rte_mempool *pool);
int close_port(uint16_t port);
#endif
