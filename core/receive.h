#ifndef RECEIVE_H
#define RECEIVE_H
#include "../common/common.h"
struct rx_state {
    uint16_t next_port, last_port, last_index;
    /* Split layouts hand each port to its own scheduler ring; enqueued counts per ring. */
    bool split;
    uint64_t sequence[RTE_MAX_ETHPORTS], last_arrival[RTE_MAX_ETHPORTS], last_watermark, enqueued[2];
    struct rx_stats *stats;
};
void rx_state_init(struct rx_state *s, struct pipeline_stats *stats);
uint16_t rx_step(struct rx_state *s, struct rte_mbuf **batch, uint16_t max_batch);
void rx_publish(struct rx_state *s, uint16_t enqueued);
/* Poll the next port once. A port whose scheduler ring lacks room for a burst is
 * skipped (counted, left in its NIC queue), so it cannot stall the other port. */
uint16_t rx_iteration(struct rx_state *s, struct lcore_parameters *parameters, struct rte_mbuf **batch);
int lcore_receive(struct lcore_parameters *parameters);
#endif
