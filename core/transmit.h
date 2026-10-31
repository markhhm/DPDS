#ifndef TRANSMIT_H
#define TRANSMIT_H
#include "../common/common.h"
/* Live entries are m_table[head .. head+len). Removal advances head; an append
 * that reaches the end of the 2x buffer compacts once, after at least
 * TX_PENDING_PER_LINE removals, so every operation is amortized O(1). */
struct mbuf_table {
    uint16_t head, len;
    struct rte_mbuf *m_table[2 * TX_PENDING_PER_LINE];
};
static inline struct rte_mbuf **pending_at(struct mbuf_table *t) { return t->m_table + t->head; }
struct tx_state {
    enum observer_producer producer;
    unsigned handoff_mask;
    struct observer_snapshot snapshot;
    uint64_t consumed_dispatch[OBS_QUEUES], attempt_id[RTE_MAX_ETHPORTS];
    struct mbuf_table pending[HNG_MAX_LINES];
    struct tx_stats *stats;
    uint16_t (*burst)(uint16_t, uint16_t, struct rte_mbuf **, uint16_t);
    unsigned next_line;
};
void tx_state_init(struct tx_state *s, struct pipeline_stats *stats);
void tx_step(struct tx_state *s, struct rte_mbuf **batch, uint16_t count);
void tx_step_at(struct tx_state *s, struct rte_mbuf **batch, uint16_t count, uint64_t now);
bool tx_has_backlog(const struct tx_state *s);
void tx_state_cleanup(struct tx_state *s);
/* Add one TX lcore's counters to dst; peaks are maxima. */
void tx_stats_merge(struct tx_stats *dst, const struct tx_stats *src);
int lcore_transmit(struct lcore_parameters *parameters);
#endif
