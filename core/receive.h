#ifndef RECEIVE_H
#define RECEIVE_H
#include "../common/common.h"
struct rx_state {
    uint16_t next_port, last_port, last_index;
    /* Split layouts hand each port to its own scheduler ring; enqueued counts per ring. */
    bool split;
    uint64_t sequence[RTE_MAX_ETHPORTS], last_watermark, enqueued[2];
    struct rx_stats *stats;
};
void rx_state_init(struct rx_state *s, struct pipeline_stats *stats);
uint16_t rx_step(struct rx_state *s, struct rte_mbuf **batch, uint16_t max_batch);
void rx_publish(struct rx_state *s, uint16_t enqueued);
int lcore_receive(struct lcore_parameters *parameters);
#endif
