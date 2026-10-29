/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <stdio.h>
#include <rte_eal.h>
#include <rte_cycles.h>
#include <string.h>
#include "../core/transmit.h"

static unsigned calls, delivered;
static uint64_t ids[8];
static uint16_t short_tx(uint16_t port, uint16_t queue, struct rte_mbuf **packets, uint16_t count)
{
    (void)queue;
    assert(port == 7);
    uint16_t sent = calls++ == 0 ? 0 : (calls == 2 ? 1 : count);
    for (unsigned i = 0; i < sent; ++i) {
        ids[delivered++] = packets[i]->hash.usr;
        rte_pktmbuf_free(packets[i]);
    }
    return sent;
}
int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    struct rte_mempool *pool = rte_pktmbuf_pool_create("tx_test", 255, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN),
        RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    host.epoch_cycles = rte_get_timer_cycles();
    host.cycles_hz = rte_get_timer_hz();
    struct pipeline_stats stats = {0};
    struct tx_state tx;
    tx_state_init(&tx, &stats);
    tx.burst = short_tx;
    struct rte_mbuf *packets[3];
    for (unsigned i = 0; i < 3; ++i) {
        packets[i] = rte_pktmbuf_alloc(pool);
        assert(packets[i]);
        assert(rte_pktmbuf_append(packets[i], 76));
        memset(packet_meta(packets[i]), 0, sizeof(struct packet_meta));
        packets[i]->port = 7;
        packet_meta(packets[i])->ingress_seq = i;
        packets[i]->hash.usr = i;
    }
    tx_step(&tx, packets, 2);
    assert(stats.tx.transmitted == 0 && stats.tx.tx_drops == 0);
    tx_step(&tx, packets + 2, 1);
    assert(stats.tx.transmitted == 1 && stats.tx.retries == 2);
    assert(tx_has_backlog(&tx));
    while (tx_has_backlog(&tx)) tx_step(&tx, NULL, 0);
    assert(delivered == 3 && ids[0] == 0 && ids[1] == 1 && ids[2] == 2);
    assert(stats.tx.tx_drops == 0);
    tx_state_cleanup(&tx);
    assert(rte_mempool_avail_count(pool) == 255);
    rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
    puts("TX zero/partial/full suffix ownership: PASS");
}
