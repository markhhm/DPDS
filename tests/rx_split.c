/* SPDX-License-Identifier: Apache-2.0 */
/* A full scheduler ring defers only its own port: the other direction keeps flowing. */
#include <assert.h>
#include <stdio.h>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include "../core/receive.h"

int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0 && host_load("rings.json"));
    struct rte_mempool *pool = rte_pktmbuf_pool_create("rx_split", 1023, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    for (uint16_t p = 0; p < 2; ++p) {
        host.ports[host.port_count++] = p;
        assert(host_prepare_port(p) == 0 && initialize_port(p, pool) == 0);
    }
    struct rte_ring *rings[2];
    for (unsigned k = 0; k < 2; ++k) {
        char name[16]; snprintf(name, sizeof(name), "owner%u", k);
        rings[k] = rte_ring_create(name, 64, rte_socket_id(), RING_F_SP_ENQ | RING_F_SC_DEQ);
        assert(rings[k]);
    }
    /* Port 0's owner is congested: its ring has no room for a burst. */
    static int dummy;
    while (rte_ring_enqueue(rings[0], &dummy) == 0) {}
    struct pipeline_stats stats = {0};
    struct lcore_parameters parameters = {.stats = &stats, .num_ports = 2, .split = true,
        .owner_rings = {rings[0], rings[1]}};
    struct rx_state rx;
    rx_state_init(&rx, &stats);
    rx.split = true;
    struct rte_mbuf *batch[MAX_BURST_SIZE];
    for (unsigned i = 0; i < 6; ++i) rx_iteration(&rx, &parameters, batch);
    /* Port 1 kept flowing; port 0 was skipped and counted, never spun on. */
    assert(rte_ring_count(rings[1]) >= MAX_BURST_SIZE);
    assert(stats.rx.ring_full_skips[0] == 3 && stats.rx.ring_full_skips[1] == 0);
    assert(rte_ring_full(rings[0]) && stats.rx.rx_ring_drops == 0 && !host.invalid);
    void *m;
    while (rte_ring_dequeue(rings[1], &m) == 0) rte_pktmbuf_free(m);
    for (uint16_t p = 0; p < 2; ++p) assert(close_port(p) == 0);
    assert(rte_mempool_avail_count(pool) == 1023);
    host_cleanup();
    rte_mempool_free(pool);
    for (unsigned k = 0; k < 2; ++k) rte_ring_free(rings[k]);
    assert(rte_eal_cleanup() == 0);
    puts("full owner ring defers only its port; the other direction flows: PASS");
}
