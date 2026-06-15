#ifndef COMMON_H
#define COMMON_H

#include <stdint.h>
#include <rte_ring.h>
#include <rte_mempool.h>
#include "config.h"

// -------------------------------------------
// Global quit signals
// -------------------------------------------
extern volatile uint8_t quit_signal_receive;
extern volatile uint8_t quit_signal_worker;
extern volatile uint8_t quit_signal_transmit;

// -------------------------------------------
// Pipeline statistics
// -------------------------------------------
// Cache-line aligned per-core structs to avoid false sharing.
// Each core writes only to its own struct; the main thread only reads.
struct rx_stats {
    uint64_t received;
    uint64_t enqueued;
    uint64_t rx_ring_drops;
    uint64_t burst_hist[BURST_HIST_BUCKETS];
} __attribute__((aligned(64)));

struct worker_stats {
    uint64_t received;
    uint64_t sent;
    uint64_t emulator_drops;
    uint64_t tx_ring_drops;
} __attribute__((aligned(64)));

struct tx_stats {
    uint64_t received;
    uint64_t transmitted;
    uint64_t tx_drops;
    uint64_t burst_hist[BURST_HIST_BUCKETS];
} __attribute__((aligned(64)));

struct pipeline_stats {
    struct rx_stats rx;
    struct worker_stats worker;
    struct tx_stats tx;
};

// -------------------------------------------
// Shared structures
// -------------------------------------------
// Structure to hold parameters passed to lcore functions
struct lcore_parameters
{
    unsigned int worker_id;
    struct rte_ring *receive_worker_ring;
    struct rte_ring *worker_transmit_ring;
    struct rte_mempool *mbuf_pool;
    struct pipeline_stats *stats;
    uint16_t num_ports;
};

// -------------------------------------------
// Function declarations
// -------------------------------------------
// Signal handler
void signal_handler(int signum);

// Port initialization and cleanup
int initialize_port(uint16_t port, struct rte_mempool *mbuf_pool);
int close_port(uint16_t port);

#endif // COMMON_H
