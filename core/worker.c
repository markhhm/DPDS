#include <stdio.h>
#include <rte_mbuf.h>
#include <rte_ring.h>
#include <rte_lcore.h>
#include <rte_malloc.h>
#include <rte_branch_prediction.h>
#include <rte_mbuf_core.h>
#include <rte_cycles.h>
#include <rte_prefetch.h>

#include "worker.h"

// -------------------------------------------
// TSC-based timing (userspace, no syscalls)
// -------------------------------------------
static inline double get_time_ns(double tsc_ns_per_cycle)
{
    return (double)rte_rdtsc() * tsc_ns_per_cycle;
}

// -------------------------------------------
// Worker stage: init, step, backlog check
// -------------------------------------------
void worker_state_init(struct worker_state *s, struct pipeline_stats *stats,
                       uint16_t num_ports)
{
    struct Config config = read_config("config.json");
    s->buffer = buffer_new(config);
    s->stats = &stats->worker;
    s->tsc_ns_per_cycle = 1000000000.0 / (double)rte_get_tsc_hz();
    s->num_ports = num_ports;
}

void worker_state_cleanup(struct worker_state *s)
{
    if (s->buffer)
    {
        buffer_destroy(s->buffer);
        s->buffer = NULL;
    }
}

uint16_t worker_step(struct worker_state *s,
                     struct rte_mbuf **in_batch, uint16_t in_count,
                     struct rte_mbuf **out_batch)
{
    unsigned int i;

    // Same current time for a batch of packets to improve performance
    double current_time_ns = get_time_ns(s->tsc_ns_per_cycle);

    // Ingest incoming packets into the delay buffer.
    for (i = 0; i < in_count; i++)
    {
        if (likely(i + 3 < in_count))
            rte_prefetch0(in_batch[i + 3]);

        // With two NICs, send through the other port; with one NIC, keep the same port.
        if (s->num_ports > 1)
            in_batch[i]->port ^= 1;
        uint32_t pkt_len = in_batch[i]->pkt_len;

        if (!buffer_push(s->buffer, in_batch[i], pkt_len, current_time_ns))
        {
            s->stats->emulator_drops++;
            rte_pktmbuf_free(in_batch[i]);
        }
    }

    // Drain ready packets from the delay buffer.
    size_t buf_len = buffer_len(s->buffer);
    uint16_t max_packets_to_send = MAX_BURST_SIZE;
    if (max_packets_to_send > buf_len)
        max_packets_to_send = (uint16_t)buf_len;

    if (likely(max_packets_to_send == 0))
        return 0;

    current_time_ns = get_time_ns(s->tsc_ns_per_cycle);

    uint16_t send_count = 0;
    for (i = 0; i < max_packets_to_send; i++)
    {
        if (buffer_front_time(s->buffer) <= current_time_ns)
        {
            void *pkt = buffer_pop(s->buffer);
            out_batch[send_count++] = (struct rte_mbuf *)pkt;
        }
        else
            break;
    }

    return send_count;
}

bool worker_has_backlog(const struct worker_state *s)
{
    return !buffer_is_empty(s->buffer);
}

// -------------------------------------------
// Worker core (standalone, ring I/O)
// -------------------------------------------
int lcore_worker(struct lcore_parameters *parameters)
{
    struct worker_state wk;
    struct rte_mbuf *batch[MAX_BURST_SIZE];
    struct rte_mbuf *send_batch[MAX_BURST_SIZE];
    uint16_t batch_size = 0;

    struct rte_ring *input_ring = parameters->receive_worker_ring;
    struct rte_ring *output_ring = parameters->worker_transmit_ring;

    worker_state_init(&wk, parameters->stats, parameters->num_ports);

    while (!quit_signal_worker || batch_size || worker_has_backlog(&wk))
    {
        batch_size = rte_ring_dequeue_burst(input_ring, (void *)batch,
                                            MAX_BURST_SIZE, NULL);
        wk.stats->received += batch_size;

        uint16_t send_count = worker_step(&wk, batch, batch_size, send_batch);

        if (send_count > 0)
        {
            uint16_t sent = rte_ring_enqueue_burst(output_ring, (void **)send_batch,
                                                   send_count, NULL);
            wk.stats->sent += sent;

            if (unlikely(sent < send_count))
            {
                wk.stats->tx_ring_drops += send_count - sent;
                while (sent < send_count)
                    rte_pktmbuf_free(send_batch[sent++]);
            }
        }
    }

    quit_signal_transmit = 1;

    worker_state_cleanup(&wk);
    rte_free(parameters);
    return 0;
}
