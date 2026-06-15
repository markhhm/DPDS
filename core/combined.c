#include <rte_mbuf.h>
#include <rte_ring.h>
#include <rte_malloc.h>
#include <rte_branch_prediction.h>

#include "../common/common.h"
#include "../common/config.h"
#include "receive.h"
#include "worker.h"
#include "transmit.h"
#include "combined.h"

// -------------------------------------------
// 3-core mode: Worker + TX on one thread
// -------------------------------------------
// Input comes from receive_worker_ring (RX runs on a separate core).
// Worker output goes directly to tx_step (no ring between them).
int lcore_worker_transmit(struct lcore_parameters *parameters)
{
    struct worker_state wk;
    struct tx_state tx;
    struct rte_mbuf *batch[MAX_BURST_SIZE];
    struct rte_mbuf *send_batch[MAX_BURST_SIZE];
    uint16_t batch_size = 0;

    struct rte_ring *input_ring = parameters->receive_worker_ring;

    worker_state_init(&wk, parameters->stats, parameters->num_ports);
    tx_state_init(&tx, parameters->stats);

    while (!quit_signal_worker || batch_size || worker_has_backlog(&wk))
    {
        batch_size = rte_ring_dequeue_burst(input_ring, (void *)batch,
                                            MAX_BURST_SIZE, NULL);
        wk.stats->received += batch_size;

        uint16_t send_count = worker_step(&wk, batch, batch_size, send_batch);
        wk.stats->sent += send_count;

        // Feed directly into TX (no ring).
        tx_step(&tx, send_batch, send_count);
    }

    tx_state_cleanup(&tx);
    worker_state_cleanup(&wk);
    quit_signal_transmit = 1;

    rte_free(parameters);
    return 0;
}

// -------------------------------------------
// 2-core mode: RX + Worker + TX on one thread
// -------------------------------------------
// No rings at all - all three stages run in a single loop.
int lcore_receive_worker_transmit(struct lcore_parameters *parameters)
{
    struct rx_state rx;
    struct worker_state wk;
    struct tx_state tx;
    struct rte_mbuf *batch[MAX_BURST_SIZE];
    struct rte_mbuf *send_batch[MAX_BURST_SIZE];

    rx_state_init(&rx, parameters->stats);
    worker_state_init(&wk, parameters->stats, parameters->num_ports);
    tx_state_init(&tx, parameters->stats);

    while (!quit_signal_receive || worker_has_backlog(&wk))
    {
        uint16_t rx_count = rx_step(&rx, batch, MAX_BURST_SIZE);

        // In combined mode there is no ring, so no enqueue/drop accounting.
        // rx_stats->received is already updated by rx_step.

        uint16_t send_count = worker_step(&wk, batch, rx_count, send_batch);
        wk.stats->received += rx_count;
        wk.stats->sent += send_count;

        tx_step(&tx, send_batch, send_count);
    }

    tx_state_cleanup(&tx);
    worker_state_cleanup(&wk);
    quit_signal_worker = 1;
    quit_signal_transmit = 1;

    rte_free(parameters);
    return 0;
}
