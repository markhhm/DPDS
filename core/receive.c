#include <stdio.h>
#include <rte_mbuf.h>
#include <rte_ring.h>
#include <rte_lcore.h>
#include <rte_malloc.h>
#include <rte_ethdev.h>
#include <rte_branch_prediction.h>

#include "../common/config.h"
#include "receive.h"

// -------------------------------------------
// RX stage: init and step
// -------------------------------------------
void rx_state_init(struct rx_state *s, struct pipeline_stats *stats)
{
    const int socket_id = rte_socket_id();
    uint16_t port_id;

    s->port_id = 0;
    s->num_ports = rte_eth_dev_count_avail();
    s->stats = &stats->rx;

    // NUMA placement warning.
    RTE_ETH_FOREACH_DEV(port_id)
    {
        if (rte_eth_dev_socket_id(port_id) >= 0 &&
            rte_eth_dev_socket_id(port_id) != socket_id)
            printf("WARNING: port %u is on a different NUMA node to the "
                   "receive thread. Please reconfigure for optimal "
                   "performance.\n",
                   port_id);
    }
}

uint16_t rx_step(struct rx_state *s, struct rte_mbuf **batch, uint16_t max_batch)
{
    const uint16_t batch_size = rte_eth_rx_burst(s->port_id, 0, batch, max_batch);

    if (likely(batch_size > 0))
    {
        s->stats->received += batch_size;
        s->stats->burst_hist[31 - __builtin_clz(batch_size)]++;
    }

    // Advance to next port.
    if (++s->port_id == s->num_ports)
        s->port_id = 0;

    return batch_size;
}

// -------------------------------------------
// Receive core (standalone, ring output)
// -------------------------------------------
int lcore_receive(struct lcore_parameters *parameters)
{
    struct rx_state rx;
    struct rte_mbuf *batch[MAX_BURST_SIZE];
    struct rte_ring *output_ring = parameters->receive_worker_ring;

    rx_state_init(&rx, parameters->stats);

    while (!quit_signal_receive)
    {
        const uint16_t batch_size = rx_step(&rx, batch, MAX_BURST_SIZE);

        if (unlikely(batch_size == 0))
            continue;

        // Enqueue the packets into the pipeline.
        uint16_t sent = rte_ring_enqueue_burst(output_ring, (void *)batch, batch_size, NULL);
        rx.stats->enqueued += sent;

        // Free packets that couldn't be enqueued (ring full).
        if (unlikely(sent < batch_size))
        {
            rx.stats->rx_ring_drops += batch_size - sent;
            uint16_t j = sent;
            while (j < batch_size)
                rte_pktmbuf_free(batch[j++]);
        }
    }

    // Once this core has exited, shut down worker threads.
    quit_signal_worker = 1;

    rte_free(parameters);
    return 0;
}
