/* SPDX-License-Identifier: Apache-2.0 */
/* Actual owner loops, two stopped TX consumers, END during backpressure. */
#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <rte_eal.h>
#include <rte_malloc.h>
#include "../core/worker.h"
#include "../core/transmit.h"
#define N 2048
static _Atomic uint64_t clock_ns = 500000000;
uint64_t __wrap_host_now_ns(void) { return atomic_load(&clock_ns); }
struct expected { uint64_t seq, release, deadline; };
static struct expected reference[2][N];
static unsigned delivered[2];
static struct rte_mempool *pool;
static bool m7;

static struct rte_mbuf *packet(unsigned owner, unsigned seq)
{
    struct rte_mbuf *m = rte_pktmbuf_alloc(pool);
    assert(m);
    uint8_t *d = (uint8_t *)rte_pktmbuf_append(m, 76);
    assert(d);
    memset(d, 0, 76); d[12] = 0x81; d[15] = 100; d[16] = 0x08;
    *packet_meta(m) = (struct packet_meta){.ingress_port = owner ? 13 : 7,
        .ingress_seq = seq, .arrival_ns = seq * 1000};
    return m;
}

static void baseline(void)
{
    assert(host_load("handoff.json"));
    for (unsigned k = 0; k < 2; ++k) {
        struct worker_stats stats = {0};
        struct worker_state w;
        worker_state_init_owner(&w, &stats, k ? 13 : 7);
        struct rte_mbuf *in[MAX_BURST_SIZE], *out[MAX_BURST_SIZE];
        for (unsigned first = 0; first < N; first += MAX_BURST_SIZE) {
            for (unsigned i = 0; i < MAX_BURST_SIZE; ++i) in[i] = packet(k, first+i);
            assert(worker_step_at(&w, in, MAX_BURST_SIZE, out, 0) == 0);
        }
        worker_end_input(&w);
        unsigned n = 0, count;
        while ((count = worker_step_at(&w, NULL, 0, out, clock_ns))) {
            for (unsigned i = 0; i < count; ++i) {
                struct packet_meta *meta = packet_meta(out[i]);
                reference[k][n++] = (struct expected){meta->ingress_seq, meta->release_ns, meta->deadline_ns};
                rte_pktmbuf_free(out[i]);
            }
        }
        assert(n == N && !worker_has_backlog(&w));
        worker_state_cleanup(&w);
    }
    assert(!host.invalid);
    host_cleanup();
}

static uint16_t capture(uint16_t port, uint16_t queue, struct rte_mbuf **batch, uint16_t count)
{
    (void)queue;
    unsigned k = port == 13 ? 0 : 1;
    for (unsigned i = 0; i < count; ++i) {
        unsigned n = delivered[k]++;
        assert(n < N);
        struct packet_meta *m = packet_meta(batch[i]);
        const struct expected *e = &reference[k][n];
        assert(m->ingress_seq == e->seq && m->release_ns == e->release && m->deadline_ns == e->deadline);
        rte_pktmbuf_free(batch[i]);
    }
    return count;
}

static void *scheduler(void *p) { assert(lcore_worker(p) == 0); return NULL; }

static void paused_pair(bool terminal)
{
    assert(host_load(m7 ? "handoff-m7.json" : "handoff.json"));
    assert(host_storage_bind_pool(7, pool, rte_socket_id()));
    assert(host_storage_bind_pool(13, pool, rte_socket_id()));
    clock_ns = 500000000;
    memset(delivered, 0, sizeof(delivered));
    host.port_count = 2; host.ports[0] = 7; host.ports[1] = 13;
    atomic_store(&host.schedulers_running, 2);
    quit_signal_receive = quit_signal_worker = quit_signal_transmit = 0;
    struct pipeline_stats stats = {0};
    struct rte_ring *rx[2], *tx[2];
    pthread_t thread[2];
    for (unsigned k = 0; k < 2; ++k) {
        char name[32];
        snprintf(name, sizeof(name), "handoff_rx%u", k);
        rx[k] = rte_ring_create(name, 4096, rte_socket_id(), RING_F_SP_ENQ|RING_F_SC_DEQ);
        snprintf(name, sizeof(name), "handoff_tx%u", k);
        tx[k] = rte_ring_create(name, SCHED_TX_RING_SZ, rte_socket_id(), RING_F_SP_ENQ|RING_F_SC_DEQ);
        assert(rx[k] && tx[k] && rte_ring_get_capacity(tx[k]) == 1023);
        for (unsigned i = 0; i < N; ++i) assert(rte_ring_enqueue(rx[k], packet(k, i)) == 0);
        struct lcore_parameters *p = rte_zmalloc(NULL, sizeof(*p), 0);
        assert(p);
        *p = (struct lcore_parameters){.stats=&stats, .receive_worker_ring=rx[k],
            .worker_transmit_ring=tx[k], .split=true, .io_split=true, .owner_index=k};
        assert(pthread_create(&thread[k], NULL, scheduler, p) == 0);
    }
    /* No TX consumer runs: both real scheduler loops must preserve model ownership. */
    for (unsigned tries = 0; !rte_ring_empty(rx[0]) || !rte_ring_empty(rx[1]); ++tries) {
        assert(tries < 10000); usleep(100);
    }
    usleep(10000);
    assert(!host.invalid); /* RED on the original eager pop + short enqueue. */
    assert(rte_ring_count(tx[0]) == 1023 && rte_ring_count(tx[1]) == 1023);
    clock_ns += 100000; /* Both scheduler owners must observe a real loop gap. */
    usleep(1000);
    quit_signal_worker = 1; /* END while both queues remain full. */
    if (terminal) {
        /* No TX progress for the existing terminal budget must invalidate, not spin forever. */
        clock_ns = 500000000 + (m7 ? 100000 : TERMINAL_TIMEOUT_NS);
        usleep(10000);
        assert(!host.invalid); /* Existing gates use strictly greater than the budget. */
        clock_ns += 1;
        for (unsigned tries = 0; !host.invalid || !quit_signal_receive ||
            !atomic_load(&host.diagnostics.invalid_recorded); ++tries) {
            assert(tries < 1000); usleep(10);
        }
        assert(quit_signal_receive);
        /* Preserve the independent shutdown gate: it aborts model-owned suffixes. */
        for (unsigned tries = 0; atomic_load(&host.schedulers_running); ++tries) {
            assert(tries < 10000);
            clock_ns += SHUTDOWN_TIMEOUT_NS + 1;
            usleep(100);
        }
    }
    struct tx_state state[2];
    struct pipeline_stats txstats[2] = {0};
    for (unsigned k = 0; k < 2; ++k) { tx_state_init(&state[k], &txstats[k]); state[k].burst = capture; }
    for (unsigned tries = 0; !quit_signal_transmit || !rte_ring_empty(tx[0]) || !rte_ring_empty(tx[1]); ++tries) {
        assert(tries < 1000000);
        for (unsigned k = 0; k < 2; ++k) {
            struct rte_mbuf *out[MAX_BURST_SIZE];
            unsigned n = rte_ring_dequeue_burst(tx[k], (void **)out, MAX_BURST_SIZE, NULL);
            tx_step_at(&state[k], out, n, clock_ns);
        }
        if (!(tries % 64)) usleep(10);
    }
    for (unsigned k = 0; k < 2; ++k) {
        assert(pthread_join(thread[k], NULL) == 0);
        tx_state_cleanup(&state[k]);
        assert(delivered[k] == (terminal ? 1023 : N) && stats.owners[k].tx_ring_drops == 0);
        assert(stats.owners[k].sent == delivered[k] && stats.owners[k].shutdown_drops == (terminal ? N-1023 : 0));
        assert(txstats[k].tx.transmitted == delivered[k] && txstats[k].tx.tx_drops == 0);
        assert(txstats[k].tx.late_submit_max_ns > 35000000); /* Original deadlines, honest lateness. */
        assert(atomic_load(&host.diagnostics.scheduler[k].ring_high_water) == 1023);
        assert(atomic_load(&host.diagnostics.scheduler[k].loop_gap_max_ns) >= 100000);
        rte_ring_free(rx[k]); rte_ring_free(tx[k]);
    }
    assert(host.invalid == terminal);
    if (terminal) {
        assert(atomic_load(&host.diagnostics.invalid_recorded));
        assert(strcmp(host.diagnostics.invalid_reason, m7 ? "TX handoff hold exceeded" : "TX handoff terminal timeout") == 0);
        assert(strncmp(host.diagnostics.invalid_owner, "scheduler:", 10) == 0);
        assert(host.diagnostics.invalid_at_ns < clock_ns && host.diagnostics.invalid_mono_ns > 0);
    }
    host_cleanup();
    assert(rte_mempool_avail_count(pool) == pool->size);
}

int main(int argc, char **argv)
{
    int eal = rte_eal_init(argc, argv); assert(eal >= 0);
    m7 = argc-eal > 1;
    pool = rte_pktmbuf_pool_create("handoff_test", 8191, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    baseline(); paused_pair(false); paused_pair(true);
    rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
    puts("two paused TX owners, END, catch-up, exact order/deadlines, zero loss/duplicates, pool reclaim: PASS");
}
