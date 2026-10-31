/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <string.h>
#include <stdio.h>
#include <rte_eal.h>
#include "../core/transmit.h"
static uint64_t ids[16];
static unsigned delivered;
static bool blocked;
static unsigned clock_reads;
static uint64_t clock_before, clock_after;
uint64_t __wrap_host_now_ns(void)
{
    /* Advance at the final pre-submit read, after the initial candidate selection. */
    return ++clock_reads < 4 ? clock_before : clock_after;
}
static uint16_t transmit(uint16_t port, uint16_t queue, struct rte_mbuf **batch, uint16_t count)
{
    (void)queue;
    assert(port == 7 || port == 13);
    if (blocked && port == 13) return 0;
    for (unsigned i = 0; i < count; ++i) {
        ids[delivered++] = batch[i]->hash.usr;
        rte_pktmbuf_free(batch[i]);
    }
    return count;
}
static struct rte_mbuf *packet(struct rte_mempool *pool, unsigned line, uint64_t id, uint64_t deadline)
{
    struct rte_mbuf *m = rte_pktmbuf_alloc(pool);
    assert(m && rte_pktmbuf_append(m, 76));
    *packet_meta(m) = (struct packet_meta){.line=line, .ingress_seq=id,
        .release_ns=deadline, .deadline_ns=deadline};
    m->port = 13; m->hash.usr = id;
    return m;
}
static void recovery_at_final_clock_read(struct rte_mempool *pool)
{
    struct pipeline_stats stats = {0};
    struct tx_state tx;
    tx_state_init(&tx, &stats); tx.burst = transmit;
    struct rte_mbuf *batch[] = {packet(pool, 0, 200, 200), packet(pool, 1, 100, 100)};
    blocked = false; delivered = clock_reads = 0;
    clock_before = 2000000299; clock_after = 2000000300;
    tx_step(&tx, batch, 2);
    assert(delivered == 2 && ids[0] == 100 && ids[1] == 200);
    assert(!tx_has_backlog(&tx) && stats.tx.transmitted == 2 && !host.invalid);
}
int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    assert(host_load("tx_trace.json"));
    struct rte_mempool *pool = rte_pktmbuf_pool_create("tx_trace_test", 255, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN),
        RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    struct pipeline_stats stats = {0};
    struct tx_state tx;
    tx_state_init(&tx, &stats); tx.burst = transmit;
    struct rte_mbuf *batch[3] = {packet(pool, 1, 0, 190), packet(pool, 0, 1, 220), packet(pool, 1, 2, 195)};
    tx_step_at(&tx, batch, 3, 250);
    assert(delivered == 1 && ids[0] == 1 && tx_has_backlog(&tx) && !host.invalid);
    tx_step_at(&tx, NULL, 0, 299);
    assert(delivered == 1);
    tx_step_at(&tx, NULL, 0, 2000000300);
    assert(delivered == 3 && ids[1] == 0 && ids[2] == 2 && !tx_has_backlog(&tx));
    assert(stats.tx.late_submit_packets == 3 && stats.tx.late_submit_max_ns == 2000000110);
    batch[0] = packet(pool, 2, 3, 190);
    tx_step_at(&tx, batch, 1, 250);
    assert(host.invalid && quit_signal_receive && stats.tx.trace_outage_drops == 1);
    assert(stats.tx.trace_outage_bytes == 76 && delivered == 3 && !tx_has_backlog(&tx));
    /* A blocked physical port cannot prevent the other output from progress. */
    blocked = true;
    batch[0] = packet(pool, 0, 4, 0);
    batch[1] = packet(pool, 1, 5, 0); batch[1]->port = 7;
    tx_step_at(&tx, batch, 2, 0);
    assert(delivered == 4 && ids[3] == 5 && tx_has_backlog(&tx));
    tx_step_at(&tx, NULL, 0, TERMINAL_TIMEOUT_NS + 1);
    assert(!tx_has_backlog(&tx) && stats.tx.tx_drops == 1 && stats.tx.tx_drop_bytes == 76);
    tx_state_cleanup(&tx);
    host.invalid = false; quit_signal_receive = 0;
    recovery_at_final_clock_read(pool);
    host_cleanup();
    assert(rte_mempool_avail_count(pool) == 255);
    rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
    puts("actual-time gate, held LEO bypass, own-line order, missed outage and bounded retry: PASS");
}
