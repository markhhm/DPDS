#ifndef RECEIVE_H
#define RECEIVE_H

#include <rte_mbuf.h>
#include "../common/common.h"
#include "../common/config.h"

// -------------------------------------------
// RX stage state and step function
// -------------------------------------------
struct rx_state {
    uint16_t port_id;
    uint16_t num_ports;
    struct rx_stats *stats;
};

void rx_state_init(struct rx_state *s, struct pipeline_stats *stats);

// Receive packets from the current port into batch, then advance to the next port.
// Returns the number of packets received (may be 0).
uint16_t rx_step(struct rx_state *s, struct rte_mbuf **batch, uint16_t max_batch);

// Receive core function (standalone RX thread with ring output)
int lcore_receive(struct lcore_parameters *parameters);

#endif // RECEIVE_H
