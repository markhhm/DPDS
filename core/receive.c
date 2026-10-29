#include <string.h>
#include <rte_ethdev.h>
#include <rte_malloc.h>
#include <rte_pause.h>
#include "receive.h"

void rx_state_init(struct rx_state *s, struct pipeline_stats *stats)
{
    memset(s, 0, sizeof(*s));
    s->stats = &stats->rx;
}

uint16_t rx_step(struct rx_state *s, struct rte_mbuf **batch, uint16_t max_batch)
{
    uint16_t port = host.ports[s->next_port];
    s->last_port = port;
    s->last_index = s->next_port;
    s->next_port = (s->next_port + 1) % host.port_count;
    uint16_t count = rte_eth_rx_burst(port, 0, batch, max_batch);
    for (uint16_t i = 0; i < count; ++i) {
        struct packet_meta *m = packet_meta(batch[i]);
        memset(m, 0, sizeof(*m));
        m->arrival_ns = host_now_ns();
        m->ingress_seq = s->sequence[port]++;
        m->ingress_port = port;
        batch[i]->port = port;
        if (s->sequence[port] == 0) host_invalidate("physical ingress sequence wrapped");
        s->stats->received_bytes += batch[i]->pkt_len;
    }
    s->last_watermark = host_now_ns();
    s->stats->received += count;
    if (count) s->stats->burst_hist[31 - __builtin_clz(count)]++;
    return count;
}

void rx_publish(struct rx_state *s, uint16_t enqueued)
{
    unsigned ring = s->split ? s->last_index : 0;
    s->enqueued[ring] += enqueued;
    host_publish(s->last_port, s->enqueued[ring], s->last_watermark);
}

int lcore_receive(struct lcore_parameters *parameters)
{
    struct rx_state rx;
    struct rte_mbuf *batch[MAX_BURST_SIZE];
    rx_state_init(&rx, parameters->stats);
    rx.split = parameters->split;
    while (!quit_signal_receive) {
        struct rte_ring *ring = rx.split ? parameters->owner_rings[rx.next_port] : parameters->receive_worker_ring;
        if (rte_ring_free_count(ring) < MAX_BURST_SIZE) {
            rte_pause();
            continue;
        }
        uint16_t count = rx_step(&rx, batch, MAX_BURST_SIZE);
        uint16_t sent = rte_ring_enqueue_burst(ring,
            (void **)batch, count, NULL);
        rx.stats->enqueued += sent;
        if (sent != count) {
            host_invalidate("RX handoff ring exhausted");
            for (uint16_t i = sent; i < count; ++i) {
                rx.stats->rx_ring_drops++;
                rx.stats->rx_ring_drop_bytes += batch[i]->pkt_len;
                rte_pktmbuf_free(batch[i]);
            }
        }
        rx_publish(&rx, sent);
    }
    /* Completion follows the final enqueue and frontier publication. */
    quit_signal_worker = 1;
    rte_free(parameters);
    return 0;
}
