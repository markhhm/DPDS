#include <stdio.h>
#include <rte_mbuf.h>
#include <rte_ring.h>
#include <rte_lcore.h>
#include <rte_malloc.h>
#include <rte_ethdev.h>
#include <rte_branch_prediction.h>
#include <rte_prefetch.h>
#include <rte_cycles.h>

#include "../common/common.h"
#include "../common/config.h"
#include "transmit.h"

// -------------------------------------------
// Flush one port's accumulated mbufs to the NIC.
// Drops and frees any packets the NIC could not accept.
// -------------------------------------------
static inline void
send_burst(struct tx_state *s, uint16_t port_id)
{
    struct mbuf_table *tbl = &s->tx_tables[port_id];
    uint16_t n = tbl->len;

    s->stats->burst_hist[31 - __builtin_clz(n)]++;
    uint16_t sent = rte_eth_tx_burst(port_id, 0, tbl->m_table, n);
    s->stats->transmitted += sent;

    if (unlikely(sent < n))
    {
        s->stats->tx_drops += n - sent;
        do
        {
            rte_pktmbuf_free(tbl->m_table[sent]);
        } while (++sent < n);
    }

    tbl->len = 0;
}

// -------------------------------------------
// TX stage: init, step, cleanup
// -------------------------------------------
void tx_state_init(struct tx_state *s, struct pipeline_stats *stats)
{
    const int socket_id = rte_socket_id();
    uint16_t port_id;

    s->stats = &stats->tx;
    memset(s->tx_tables, 0, sizeof(s->tx_tables));

    // NUMA placement warning.
    RTE_ETH_FOREACH_DEV(port_id)
    {
        if (rte_eth_dev_socket_id(port_id) >= 0 &&
            rte_eth_dev_socket_id(port_id) != socket_id)
            printf("WARNING: port %u is on a different NUMA node to the "
                   "transmit thread. Please reconfigure for optimal "
                   "performance.\n",
                   port_id);
    }
}

void tx_step(struct tx_state *s, struct rte_mbuf **batch, uint16_t count)
{
    // Flush all non-empty port tables every TX_DRAIN_US.
    uint16_t port_id;
    RTE_ETH_FOREACH_DEV(port_id)
    {
        if (s->tx_tables[port_id].len > 0)
            send_burst(s, port_id);
    }

    if (unlikely(count == 0))
        return;

    s->stats->received += count;

    // Append each packet to its port's accumulation table.
    // Flush immediately when the table reaches MAX_BURST_SIZE.
    for (uint16_t i = 0; i < count; i++)
    {
        if (likely(i + 3 < count))
            rte_prefetch0(batch[i + 3]);

        uint16_t port_id = batch[i]->port;
        struct mbuf_table *tbl = &s->tx_tables[port_id];
        tbl->m_table[tbl->len++] = batch[i];

        if (unlikely(tbl->len == MAX_BURST_SIZE))
            send_burst(s, port_id);
    }
}

void tx_state_cleanup(struct tx_state *s)
{
    uint16_t port_id;
    RTE_ETH_FOREACH_DEV(port_id)
    {
        if (s->tx_tables[port_id].len > 0)
            send_burst(s, port_id);
    }
}

// -------------------------------------------
// Transmit core (standalone, ring input)
// -------------------------------------------
int lcore_transmit(struct lcore_parameters *parameters)
{
    struct tx_state tx;
    struct rte_ring *input_ring = parameters->worker_transmit_ring;
    struct rte_mbuf *batch[MAX_BURST_SIZE];

    tx_state_init(&tx, parameters->stats);

    while (!quit_signal_transmit)
    {
        const uint16_t batch_size = rte_ring_dequeue_burst(
            input_ring, (void *)batch, MAX_BURST_SIZE, NULL);

        tx_step(&tx, batch, batch_size);
    }

    tx_state_cleanup(&tx);

    rte_free(parameters);
    return 0;
}
