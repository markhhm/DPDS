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
    /* Per-direction RX (7 lcores): each state polls only its own port and feeds only its ring. */
    void *m;
    while (rte_ring_dequeue(rings[1], &m) == 0) rte_pktmbuf_free(m);
    struct pipeline_stats direction = {0};
    struct rx_state own[2];
    for (unsigned k = 0; k < 2; ++k) {
        rx_state_init(own + k, &direction);
        own[k].stats = &direction.rx_owners[k];
        own[k].split = own[k].fixed = true;
        own[k].next_port = (uint16_t)k;
        for (unsigned i = 0; i < 3; ++i) rx_iteration(own + k, &parameters, batch);
    }
    /* Direction 0's ring is full: it skips its own port every time and never polls port 1. */
    assert(direction.rx_owners[0].ring_full_skips[0] == 3 && direction.rx_owners[0].received == 0);
    assert(own[0].sequence[0] == 0 && own[0].sequence[1] == 0);
    assert(direction.rx_owners[1].ring_full_skips[1] == 0 && direction.rx_owners[1].received == 3 * MAX_BURST_SIZE);
    assert(own[1].sequence[0] == 0 && own[1].sequence[1] == 3 * MAX_BURST_SIZE && own[1].enqueued[1] == 3 * MAX_BURST_SIZE);
    assert(rte_ring_count(rings[1]) == 3 * MAX_BURST_SIZE && rte_ring_full(rings[0]));
    rx_stats_merge(&direction.rx, &direction.rx_owners[0]);
    rx_stats_merge(&direction.rx, &direction.rx_owners[1]);
    assert(direction.rx.received == 3 * MAX_BURST_SIZE && direction.rx.ring_full_skips[0] == 3 && !host.invalid);
    /* NIC-side loss after a deferred poll (imissed) or a pool shortage (rx_nombuf) invalidates. */
    struct rte_eth_stats nic = {0};
    assert(host_check_nic_loss(0, &nic) && !host.invalid);
    nic.imissed = 1;
    assert(!host_check_nic_loss(0, &nic) && host.invalid);
    host.invalid = false;
    nic = (struct rte_eth_stats){.rx_nombuf = 2};
    assert(!host_check_nic_loss(1, &nic) && host.invalid);
    host.invalid = false;
    while (rte_ring_dequeue(rings[1], &m) == 0) rte_pktmbuf_free(m);
    for (uint16_t p = 0; p < 2; ++p) assert(close_port(p) == 0);
    assert(rte_mempool_avail_count(pool) == 1023);
    host_cleanup();
    rte_mempool_free(pool);
    for (unsigned k = 0; k < 2; ++k) rte_ring_free(rings[k]);
    assert(rte_eal_cleanup() == 0);
    puts("full owner ring defers only its port; the other direction flows: PASS");
}
