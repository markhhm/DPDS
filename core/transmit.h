#ifndef TRANSMIT_H
#define TRANSMIT_H

#include <rte_mbuf.h>
#include <rte_ethdev.h>
#include "../common/common.h"
#include "../common/config.h"

// -------------------------------------------
// TX stage state and step function
// -------------------------------------------

// Per-port software TX accumulation table.
// Packets are appended here and flushed to the NIC in a single
// rte_eth_tx_burst() call, avoiding per-packet function-call overhead.
struct mbuf_table
{
    uint16_t len;
    struct rte_mbuf *m_table[MAX_BURST_SIZE];
};

struct tx_state
{
    struct mbuf_table tx_tables[RTE_MAX_ETHPORTS];
    struct tx_stats *stats;
};

void tx_state_init(struct tx_state *s, struct pipeline_stats *stats);

// Send batch to NICs and handle periodic drain flush.
void tx_step(struct tx_state *s, struct rte_mbuf **batch, uint16_t count);

// Flush remaining TX buffers and free resources.
void tx_state_cleanup(struct tx_state *s);

// Transmit core function (standalone, ring input)
int lcore_transmit(struct lcore_parameters *parameters);

#endif // TRANSMIT_H
