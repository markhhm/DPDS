/* SPDX-License-Identifier: Apache-2.0 */
/* A blocked port holds 1024 packets per line before the run is invalidated,
 * and ports request a matching 1024 TX descriptors. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <rte_eal.h>
#include <rte_cycles.h>
#include "../core/transmit.h"

static uint16_t blocked(uint16_t port, uint16_t queue, struct rte_mbuf **packets, uint16_t count)
{
    (void)port; (void)queue; (void)packets; (void)count;
    return 0;
}

int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    struct rte_mempool *pool = rte_pktmbuf_pool_create("tx_pending", 2047, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    host.epoch_cycles = rte_get_timer_cycles();
    host.cycles_hz = rte_get_timer_hz();
    assert(host_prepare_port(0) == 0 && host.resources[0].tx_descriptors == 1024);
    struct pipeline_stats stats = {0};
    struct tx_state tx;
    tx_state_init(&tx, &stats);
    tx.burst = blocked;
    for (unsigned sent = 0; sent < 1024 + MAX_BURST_SIZE; sent += MAX_BURST_SIZE) {
        struct rte_mbuf *m[MAX_BURST_SIZE];
        for (unsigned i = 0; i < MAX_BURST_SIZE; ++i) {
            assert((m[i] = rte_pktmbuf_alloc(pool)) && rte_pktmbuf_append(m[i], 76));
            memset(packet_meta(m[i]), 0, sizeof(struct packet_meta));
            packet_meta(m[i])->ingress_seq = sent + i;
            m[i]->port = 0;
        }
        tx_step_at(&tx, m, MAX_BURST_SIZE, 0);
        if (sent + MAX_BURST_SIZE <= 1024) assert(!host.invalid && stats.tx.tx_drops == 0);
    }
    assert(tx.pending[0].len == 1024 && stats.tx.pending_peak == 1024);
    assert(host.invalid && stats.tx.tx_drops == MAX_BURST_SIZE);
    for (unsigned i = 0; i < tx.pending[0].len; ++i) rte_pktmbuf_free(tx.pending[0].m_table[i]);
    assert(rte_mempool_avail_count(pool) == 2047);
    rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
    puts("TX pending holds 1024 packets per line; 1024 TX descriptors: PASS");
}
