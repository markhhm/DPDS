#ifndef WORKER_H
#define WORKER_H
#include "../common/common.h"
struct worker_state {
    struct Engine *engine;
    struct Line *line[HNG_MAX_LINES];
    struct worker_stats *stats;
    uint64_t consumed, admitted_frontier[RTE_MAX_ETHPORTS], shutdown_start;
    bool ended, aborted, owned[HNG_MAX_LINES];
    uint16_t next_line, owned_count, owned_lines[HNG_MAX_LINES];
};
void worker_state_init(struct worker_state *s, struct pipeline_stats *stats, uint16_t num_ports);
/* ingress_port < 0: own every line. Otherwise own only that port's lines. */
void worker_state_init_owner(struct worker_state *s, struct worker_stats *stats, int ingress_port);
/* Add an owner's counters to dst; line counters come from the lines it owned. */
void worker_stats_merge(struct worker_stats *dst, const struct worker_stats *src, int ingress_port);
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
