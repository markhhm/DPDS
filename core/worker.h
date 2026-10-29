#ifndef WORKER_H
#define WORKER_H
#include "../common/common.h"
struct worker_state {
    struct Engine *engine;
    struct worker_stats *stats;
    uint64_t consumed, admitted_frontier[RTE_MAX_ETHPORTS], shutdown_start;
    bool ended, aborted;
    uint16_t next_line;
};
void worker_state_init(struct worker_state *s, struct pipeline_stats *stats, uint16_t num_ports);
void worker_state_cleanup(struct worker_state *s);
void worker_end_input(struct worker_state *s);
uint16_t worker_step(struct worker_state *s, struct rte_mbuf **in_batch,
    uint16_t in_count, struct rte_mbuf **out_batch);
/* Explicit clock is also used by the offline mbuf oracle fixture. */
uint16_t worker_step_at(struct worker_state *s, struct rte_mbuf **in_batch,
    uint16_t in_count, struct rte_mbuf **out_batch, uint64_t now_ns);
bool worker_has_backlog(const struct worker_state *s);
int lcore_worker(struct lcore_parameters *parameters);
#endif
