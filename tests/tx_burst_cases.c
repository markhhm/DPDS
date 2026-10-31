/* SPDX-License-Identifier: Apache-2.0 */
/* Merged TX burst edge cases: more than one burst of eligible frames, four lines
 * with tied keys, a retry timeout after a partial accept, and (tx_trace.json)
 * a held line beside a merged one and an outage drop after a merged burst. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <rte_eal.h>
#include <rte_cycles.h>
#include "../core/transmit.h"

static struct rte_mempool *pool;
static unsigned calls, delivered, sizes[16];
static int plan[16];              /* frames to accept per call; < 0 accepts all */
static uint64_t got[64];          /* delivered frame ids, line * 100 + index */

static uint16_t fake_tx(uint16_t port, uint16_t queue, struct rte_mbuf **m, uint16_t n)
{
    (void)port; (void)queue;
    assert(calls < 16);
    sizes[calls] = n;
    int p = plan[calls++];
    uint16_t sent = p < 0 || p > n ? n : (uint16_t)p;
    for (uint16_t i = 0; i < sent; ++i) { got[delivered++] = m[i]->hash.usr; rte_pktmbuf_free(m[i]); }
    return sent;
}

static struct rte_mbuf *frame(unsigned line, unsigned index, uint64_t release, uint64_t seq, uint16_t port)
{
    struct rte_mbuf *m = rte_pktmbuf_alloc(pool);
    assert(m && rte_pktmbuf_append(m, 76));
    *packet_meta(m) = (struct packet_meta){.line = (uint16_t)line, .release_ns = release, .deadline_ns = release,
        .ingress_seq = seq};
    m->port = port;
    m->hash.usr = line * 100 + index;
    return m;
}

static void start(struct tx_state *tx, struct pipeline_stats *stats)
{
    calls = delivered = 0;
    for (unsigned i = 0; i < 16; ++i) plan[i] = -1;
    memset(stats, 0, sizeof(*stats));
    tx_state_init(tx, stats);
    tx->burst = fake_tx;
}

/* Lines a and b, `per` frames each; a releases at even and b at odd ns from 100. */
static unsigned interleave(struct rte_mbuf **m, unsigned a, unsigned b, unsigned per, uint16_t port)
{
    for (unsigned k = 0; k < per; ++k) {
        m[k] = frame(a, k, 100 + 2 * k, 2 * k, port);
        m[per + k] = frame(b, k, 101 + 2 * k, 2 * k + 1, port);
    }
    return 2 * per;
}

static void more_than_one_burst(void)
{
    struct pipeline_stats stats; struct tx_state tx; struct rte_mbuf *m[40];
    start(&tx, &stats);
    tx_step_at(&tx, m, (uint16_t)interleave(m, 0, 1, 20, 7), 1000);
    /* 40 eligible frames: 16 + 16 + 8 in one step, globally ordered. */
    assert(calls == 3 && sizes[0] == 16 && sizes[1] == 16 && sizes[2] == 8 && !tx_has_backlog(&tx));
    for (unsigned i = 0; i < 40; ++i) assert(got[i] == (i % 2) * 100 + i / 2);
    assert(stats.tx.line_transmitted[0] == 20 && stats.tx.line_transmitted[1] == 20);
}

static void four_lines_with_ties(void)
{
    struct pipeline_stats stats; struct tx_state tx; struct rte_mbuf *m[20];
    start(&tx, &stats);
    /* Many-to-one: equal (release, ingress sequence) on every line, handed over in reverse line order. */
    for (unsigned line = 0; line < 4; ++line)
        for (unsigned k = 0; k < 5; ++k) m[(3 - line) * 5 + k] = frame(line, k, 100 + k, k, 7);
    tx_step_at(&tx, m, 20, 1000);
    assert(calls == 2 && sizes[0] == 16 && sizes[1] == 4 && !tx_has_backlog(&tx));
    /* Ties go to the lowest line: for each key, lines 0, 1, 2, 3. */
    for (unsigned i = 0; i < 20; ++i) assert(got[i] == (i % 4) * 100 + i / 4);
}

static void retry_timeout_after_partial(void)
{
    struct pipeline_stats stats; struct tx_state tx; struct rte_mbuf *m[16];
    start(&tx, &stats);
    plan[0] = 5;
    for (unsigned i = 1; i < 16; ++i) plan[i] = 0;
    tx_step_at(&tx, m, (uint16_t)interleave(m, 0, 1, 8, 7), 0);
    assert(calls == 1 && delivered == 5 && stats.tx.retries == 1);
    tx_step_at(&tx, NULL, 0, 1);
    assert(calls == 2 && delivered == 5 && tx.pending[0].len == 5 && tx.pending[1].len == 6 && !host.invalid);
    /* Past the terminal timeout every refused head is dropped, per line, without another send. */
    tx_step_at(&tx, NULL, 0, TERMINAL_TIMEOUT_NS + 2);
    assert(!tx_has_backlog(&tx) && host.invalid && delivered == 5);
    assert(stats.tx.tx_drops == 11 && stats.tx.line_invalid_drops[0] == 5 && stats.tx.line_invalid_drops[1] == 6);
    assert(stats.tx.transmitted == 5 && stats.tx.line_transmitted[0] == 3 && stats.tx.line_transmitted[1] == 2);
    for (unsigned i = 0; i < 5; ++i) assert(got[i] == (i % 2) * 100 + i / 2);
    host.invalid = false; quit_signal_receive = 0;
}

static void expired_retries_beside_fresh_frames(void)
{
    struct pipeline_stats stats; struct tx_state tx;
    struct rte_mbuf *m[] = {frame(0, 0, 100, 0, 7), frame(0, 1, 101, 1, 7)};
    start(&tx, &stats);
    plan[0] = 0;
    tx_step_at(&tx, m, 2, 0);
    assert(calls == 1 && delivered == 0 && tx.pending[0].len == 2);
    m[0] = frame(0, 2, 102, 2, 7);
    m[1] = frame(1, 0, 103, 3, 7);
    /* The PMD now accepts: expired retries must never rejoin the fresh frames. */
    tx_step_at(&tx, m, 2, TERMINAL_TIMEOUT_NS + 1);
    assert(delivered == 2 && got[0] == 2 && got[1] == 100);
    assert(calls == 2 && sizes[1] == 2 && !tx_has_backlog(&tx) && host.invalid);
    assert(stats.tx.tx_drops == 2 && stats.tx.tx_drop_bytes == 152 && stats.tx.transmitted == 2);
    assert(stats.tx.line_invalid_drops[0] == 2 && stats.tx.line_invalid_drops[1] == 0);
    host.invalid = false; quit_signal_receive = 0;
}

/* tx_trace.json, egress 13: line 0 cable, line 1 holds in [200, 2000000300), line 2 drops in [200, 300). */
static void held_line_beside_merge(void)
{
    struct pipeline_stats stats; struct tx_state tx; struct rte_mbuf *m[16];
    start(&tx, &stats);
    tx_step_at(&tx, m, (uint16_t)interleave(m, 0, 1, 8, 13), 250);
    /* The held line's earlier heads neither join nor cut line 0's burst. */
    assert(calls == 1 && sizes[0] == 8 && tx.pending[1].len == 8 && !host.invalid);
    for (unsigned i = 0; i < 8; ++i) assert(got[i] == i);
    tx_step_at(&tx, NULL, 0, 2000000300);
    assert(calls == 2 && sizes[1] == 8 && !tx_has_backlog(&tx) && !host.invalid);
    for (unsigned i = 0; i < 8; ++i) assert(got[8 + i] == 100 + i);
}

static void outage_drop_after_merge(void)
{
    struct pipeline_stats stats; struct tx_state tx; struct rte_mbuf *m[16];
    start(&tx, &stats);
    plan[0] = 5;
    tx_step_at(&tx, m, (uint16_t)interleave(m, 0, 2, 8, 13), 100);
    assert(calls == 1 && sizes[0] == 16 && delivered == 5 && !host.invalid);
    for (unsigned i = 0; i < 5; ++i) assert(got[i] == (i % 2) * 200 + i / 2);
    /* In line 2's drop window its refused frames are outage drops; line 0's rest goes in one call. */
    tx_step_at(&tx, NULL, 0, 250);
    assert(!tx_has_backlog(&tx) && host.invalid && calls == 2 && sizes[1] == 5);
    assert(stats.tx.trace_outage_drops == 6 && stats.tx.line_outage_drops[2] == 6 && stats.tx.tx_drops == 0);
    assert(stats.tx.line_transmitted[0] == 8 && stats.tx.line_transmitted[2] == 2);
    for (unsigned i = 0; i < 5; ++i) assert(got[5 + i] == 3 + i);
    host.invalid = false; quit_signal_receive = 0;
}

int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    pool = rte_pktmbuf_pool_create("tx_burst_cases", 255, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    host.epoch_cycles = rte_get_timer_cycles();
    host.cycles_hz = rte_get_timer_hz();
    more_than_one_burst();
    four_lines_with_ties();
    retry_timeout_after_partial();
    expired_retries_beside_fresh_frames();
    assert(host_load("tx_trace.json"));
    held_line_beside_merge();
    outage_drop_after_merge();
    host_cleanup();
    assert(rte_mempool_avail_count(pool) == 255);
    rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
    puts("TX merge: >16 frames, four tied lines, retry timeout after partial, held line, outage drop after merge: PASS");
}
