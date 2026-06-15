#ifndef WORKER_H
#define WORKER_H

#include <stdbool.h>
#include <rte_mbuf.h>
#include <libdpds_core.h>
#include "../common/common.h"
#include "../common/config.h"

// -------------------------------------------
// Worker stage state and step function
// -------------------------------------------
struct worker_state
{
    struct Buffer *buffer;
    struct worker_stats *stats;
    double tsc_ns_per_cycle;
    uint16_t num_ports;
};

void worker_state_init(struct worker_state *s, struct pipeline_stats *stats,
                       uint16_t num_ports);

void worker_state_cleanup(struct worker_state *s);

// Ingest in_count packets from in_batch into the delay buffer, then drain
// ready packets into out_batch. Returns the number of packets in out_batch.
// in_count may be 0 (drain-only iteration).
uint16_t worker_step(struct worker_state *s,
                     struct rte_mbuf **in_batch, uint16_t in_count,
                     struct rte_mbuf **out_batch);

// Returns true if the worker's delay buffer still holds packets.
bool worker_has_backlog(const struct worker_state *s);

// Worker core function (standalone, ring I/O)
int lcore_worker(struct lcore_parameters *parameters);

#endif // WORKER_H
