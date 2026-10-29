#include <rte_malloc.h>
#include "receive.h"
#include "worker.h"
#include "transmit.h"
#include "combined.h"

int lcore_worker_transmit(struct lcore_parameters *parameters)
{
    struct worker_state wk;
    struct tx_state tx;
    struct rte_mbuf *batch[MAX_BURST_SIZE], *output[MAX_BURST_SIZE];
    worker_state_init(&wk, parameters->stats, parameters->num_ports);
    tx_state_init(&tx, parameters->stats);
    while (!wk.ended || worker_has_backlog(&wk) || tx_has_backlog(&tx)) {
        uint16_t count = rte_ring_dequeue_burst(parameters->receive_worker_ring,
            (void **)batch, MAX_BURST_SIZE, NULL);
        if (quit_signal_worker && rte_ring_empty(parameters->receive_worker_ring) && !count) worker_end_input(&wk);
        uint16_t out = worker_step(&wk, batch, count, output);
        tx_step(&tx, output, out);
    }
    tx_state_cleanup(&tx);
    worker_state_cleanup(&wk);
    quit_signal_transmit = 1;
    rte_free(parameters);
    return 0;
}

int lcore_receive_worker_transmit(struct lcore_parameters *parameters)
{
    struct rx_state rx;
    struct worker_state wk;
    struct tx_state tx;
    struct rte_mbuf *batch[MAX_BURST_SIZE], *output[MAX_BURST_SIZE];
    rx_state_init(&rx, parameters->stats);
    worker_state_init(&wk, parameters->stats, parameters->num_ports);
    tx_state_init(&tx, parameters->stats);
    while (!wk.ended || worker_has_backlog(&wk) || tx_has_backlog(&tx)) {
        uint16_t count = 0;
        if (!quit_signal_receive) {
            count = rx_step(&rx, batch, MAX_BURST_SIZE);
            rx_publish(&rx, count);
            rx.stats->enqueued += count;
        } else worker_end_input(&wk);
        uint16_t out = worker_step(&wk, batch, count, output);
        tx_step(&tx, output, out);
    }
    tx_state_cleanup(&tx);
    worker_state_cleanup(&wk);
    quit_signal_worker = 1;
    quit_signal_transmit = 1;
    rte_free(parameters);
    return 0;
}
