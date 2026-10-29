#ifndef TRANSMIT_H
#define TRANSMIT_H
#include "../common/common.h"
struct mbuf_table {
    uint16_t len;
    struct rte_mbuf *m_table[TX_PENDING_PER_LINE];
};
struct tx_state {
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
int lcore_transmit(struct lcore_parameters *parameters);
#endif
