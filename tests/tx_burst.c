/* SPDX-License-Identifier: Apache-2.0 */
/* TX gathers one burst per egress port across lines, in global release order.
 * A partial accept leaves each line's refused frames at its head, in order. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <rte_eal.h>
#include <rte_cycles.h>
#include "../core/transmit.h"

#define FRAMES 16
static unsigned calls, delivered, accept_first, sizes[8];
static uint64_t order[FRAMES];
static uint16_t fake_tx(uint16_t port, uint16_t queue, struct rte_mbuf **m, uint16_t n)
{
    (void)queue;
    assert(port == 7 && calls < 8);
    sizes[calls] = n;
    uint16_t sent = calls++ == 0 && accept_first < n ? (uint16_t)accept_first : n;
    for (uint16_t i = 0; i < sent; ++i) {
        order[delivered++] = packet_meta(m[i])->release_ns;
        rte_pktmbuf_free(m[i]);
    }
    return sent;
}

/* Line 0 releases at even, line 1 at odd ns: heads alternate on one port. */
static void run(struct rte_mempool *pool, unsigned accept)
{
    calls = delivered = 0; accept_first = accept;
    memset(sizes, 0, sizeof(sizes));
    struct pipeline_stats stats = {0};
    struct tx_state tx;
    tx_state_init(&tx, &stats);
    tx.burst = fake_tx;
    struct rte_mbuf *m[FRAMES];
    for (unsigned i = 0; i < FRAMES; ++i) {
        m[i] = rte_pktmbuf_alloc(pool);
        assert(m[i] && rte_pktmbuf_append(m[i], 76));
        memset(packet_meta(m[i]), 0, sizeof(struct packet_meta));
        m[i]->port = 7;
        /* Input grouped by line (as a scheduler hands it over), release order interleaved. */
        unsigned line = i / (FRAMES / 2), k = i % (FRAMES / 2);
        packet_meta(m[i])->line = (uint16_t)line;
        packet_meta(m[i])->release_ns = 100 + 2 * k + line;
        packet_meta(m[i])->ingress_seq = 2 * k + line;
    }
    tx_step_at(&tx, m, FRAMES, 1000);
    if (accept >= FRAMES) {
        /* One call carries all 16 interleaved frames. */
        assert(calls == 1 && sizes[0] == FRAMES && !tx_has_backlog(&tx));
    } else {
        assert(calls == 1 && sizes[0] == FRAMES && stats.tx.retries == 1 && tx_has_backlog(&tx));
        /* Refused frames stay at their lines' heads, marked for retry, in FIFO order. */
        for (unsigned line = 0; line < 2; ++line) {
            struct mbuf_table *t = &tx.pending[line];
            assert(t->len == FRAMES / 2 - (accept + 1 - line) / 2);
            for (unsigned i = 0; i < t->len; ++i) {
                assert(packet_meta(pending_at(t)[i])->retry_active);
                if (i) assert(packet_meta(pending_at(t)[i])->release_ns == packet_meta(pending_at(t)[i - 1])->release_ns + 2);
            }
        }
        tx_step_at(&tx, NULL, 0, 1000);
        assert(calls == 2 && sizes[1] == FRAMES - accept && !tx_has_backlog(&tx));
    }
    assert(delivered == FRAMES);
    for (unsigned i = 0; i < FRAMES; ++i) assert(order[i] == 100 + i);
    assert(stats.tx.transmitted == FRAMES && stats.tx.line_transmitted[0] == FRAMES / 2 && stats.tx.line_transmitted[1] == FRAMES / 2);
    assert(stats.tx.tx_drops == 0);
}

int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    struct rte_mempool *pool = rte_pktmbuf_pool_create("tx_burst", 255, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    host.epoch_cycles = rte_get_timer_cycles();
    host.cycles_hz = rte_get_timer_hz();
    run(pool, FRAMES);
    for (unsigned accept = 0; accept < FRAMES; ++accept) run(pool, accept);
    assert(rte_mempool_avail_count(pool) == 255);
    rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
    puts("TX merged two-line burst, global release order, per-line partial accept: PASS");
}
