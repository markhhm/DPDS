/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#include <rte_eal.h>
#include "../core/worker.h"
#include "../core/transmit.h"

static unsigned calls, delivered;
static uint16_t prefix(uint16_t port, uint16_t queue, struct rte_mbuf **items, uint16_t count)
{
    (void)port; (void)queue;
    if (!calls++) return 0;
    for (unsigned i = 0; i < count; ++i) { delivered++; rte_pktmbuf_free(items[i]); }
    return count;
}
int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0 && host_load("config.json"));
    host.port_count = 2; host.ports[0] = 7; host.ports[1] = 13;
    int fd[2]; assert(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    assert(observer_open(fd[0], OBS_ID_SOFTWARE_L2, 9000, 128) && observer_start());
    struct rte_mempool *pool = rte_pktmbuf_pool_create("observer_split", 63, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    struct pipeline_stats stats = {0}; struct worker_state workers[2]; struct tx_state tx;
    struct rte_mbuf *pending[2];
    for (unsigned k = 0; k < 2; ++k) {
        struct worker_state *w = &workers[k]; uint16_t port = host.ports[k];
        worker_state_init_owner(w, &stats.owners[k], port);
        struct rte_mbuf *in[2], *out[MAX_BURST_SIZE];
        for (unsigned i = 0; i < 2; ++i) {
            in[i] = rte_pktmbuf_alloc(pool); assert(in[i]);
            uint8_t *d = (uint8_t *)rte_pktmbuf_append(in[i], 76); assert(d); memset(d, 0, 76);
            d[12] = 0x81; d[15] = 100; d[16] = 0x88; d[17] = 0xb5; d[18] = 2*k+i;
            *packet_meta(in[i]) = (struct packet_meta){.ingress_port=port, .ingress_seq=i, .arrival_ns=i*1000};
            observer_input(in[i]);
        }
        assert(worker_step_at(w, in, 2, out, 0) == 0); worker_end_input(w);
        uint16_t n = worker_step_at(w, NULL, 0, out, 10000); assert(n == 2);
        char name[32]; snprintf(name, sizeof(name), "observer_handoff_%u", k);
        struct rte_ring *ring = rte_ring_create(name, 2, rte_socket_id(), RING_F_SP_ENQ | RING_F_SC_DEQ); assert(ring);
        struct observer_record *reservations[MAX_BURST_SIZE];
        worker_dispatch_prepare(w, out, n, reservations, 10000);
        uint16_t sent = rte_ring_enqueue_burst(ring, (void **)out, n, NULL); assert(sent == 1);
        worker_dispatch_done(w, reservations, n, sent);
        assert(w->handoff_next == 1);
        observer_terminal(w->producer, out[1], OBS_HANDOFF, 10000); rte_pktmbuf_free(out[1]);
        assert(rte_ring_dequeue_burst(ring, (void **)&pending[k], 1, NULL) == 1);
        assert(packet_meta(pending[k])->observation.handoff_queue == k);
        assert(packet_meta(pending[k])->observation.handoff_seq == 0);
        rte_ring_free(ring);
        observer_input_end(port, 2);
    }
    tx_state_init(&tx, &stats); tx.burst = prefix;
    tx_step_at(&tx, pending, 2, 11000);
    while (tx_has_backlog(&tx)) tx_step_at(&tx, NULL, 0, 12000);
    assert(delivered == 2 && stats.tx.transmitted == 2 && stats.tx.retries == 1);
    assert(tx.consumed_dispatch[0] == 1 && tx.consumed_dispatch[1] == 1);
    for (unsigned k = 0; k < 2; ++k) worker_state_cleanup(&workers[k]);
    host_cleanup(); assert(observer_close(true)); close(fd[0]);
    char raw[32768]; ssize_t n = read(fd[1], raw, sizeof(raw)-1); assert(n > 0); raw[n] = 0; close(fd[1]);
    assert(strstr(raw, "\"consumed_dispatch\":[1,1]"));
    assert(strstr(raw, "\"kind\":2") && strstr(raw, "\"reason\":5"));
    assert(rte_mempool_avail_count(pool) == pool->size); rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
    puts("observer split physical queues, partial dispatch and TX retry prefix: PASS");
}
