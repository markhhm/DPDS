/* SPDX-License-Identifier: Apache-2.0 */
/*
 * Per-direction owners keep the scheduler semantics: the same input and clock
 * schedule through one owner of all four lines and through one owner per
 * ingress port give identical per-egress output sequences and line counters.
 */
#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <rte_eal.h>
#include "../core/worker.h"

#define PACKETS 6000
struct record { uint64_t seq, release, deadline; uint32_t length; uint16_t vlan, ingress; };
struct output { struct record r[2 * PACKETS]; size_t n; };
static struct rte_mempool *pool;

static uint64_t next(uint64_t *x) { *x ^= *x << 13; *x ^= *x >> 7; *x ^= *x << 17; return *x; }

/* Batch b of port p: deterministic lengths, VLANs and bursty arrivals. */
static unsigned batch(unsigned p, unsigned b, struct rte_mbuf **m, uint64_t *last)
{
    uint64_t x = 0x9e3779b97f4a7c15ull ^ (p * 1000003u + b * 7919u + 1);
    unsigned n = 1 + next(&x) % MAX_BURST_SIZE;
    if (b * MAX_BURST_SIZE >= PACKETS) return 0;
    for (unsigned i = 0; i < n; ++i) {
        uint64_t seq = (uint64_t)b * MAX_BURST_SIZE + i;
        uint16_t len = 76 + next(&x) % 1443, vlan = 100 + next(&x) % 2;
        *last += next(&x) % 4 == 0 ? next(&x) % 3000 : next(&x) % 200;
        m[i] = rte_pktmbuf_alloc(pool);
        assert(m[i]);
        uint8_t *d = (uint8_t *)rte_pktmbuf_append(m[i], len);
        assert(d);
        memset(d, 0, len); d[12] = 0x81; d[14] = vlan >> 8; d[15] = vlan & 0xff; d[16] = 0x08;
        *packet_meta(m[i]) = (struct packet_meta){.arrival_ns = *last, .ingress_seq = seq, .ingress_port = p};
        m[i]->port = p;
    }
    return n;
}

static void collect(struct output *o, struct rte_mbuf **m, unsigned n)
{
    for (unsigned i = 0; i < n; ++i) {
        struct packet_meta *meta = packet_meta(m[i]);
        assert(m[i]->port == 1 - meta->ingress_port);
        struct output *out = o + m[i]->port;
        out->r[out->n++] = (struct record){meta->ingress_seq, meta->release_ns, meta->deadline_ns,
            m[i]->pkt_len, meta->vlan_id, meta->ingress_port};
        rte_pktmbuf_free(m[i]);
    }
}

/* owners == 1: one worker owns all lines. owners == 2: one worker per ingress port. */
static void run(unsigned owners, struct output o[2], struct HngStats lines[HNG_MAX_LINES])
{
    assert(host_load("split.json"));
    struct worker_stats stats[2];
    memset(stats, 0, sizeof(stats));
    struct worker_state w[2];
    for (unsigned k = 0; k < owners; ++k) worker_state_init_owner(w + k, stats + k, owners == 1 ? -1 : (int)k);
    /* Owners hold handles only for their own lines. */
    for (unsigned k = 0; k < owners; ++k)
        for (size_t i = 0; i < host.line_count; ++i) assert((w[k].line[i] != NULL) == w[k].owned[i]);
    uint64_t last[2] = {0}, consumed[2] = {0}, now = 0;
    struct rte_mbuf *in[MAX_BURST_SIZE], *out[MAX_BURST_SIZE];
    for (unsigned b = 0;; ++b) {
        unsigned total = 0;
        for (unsigned p = 0; p < 2; ++p) {
            unsigned n = batch(p, b, in, last + p);
            total += n;
            unsigned k = owners == 1 ? 0 : p;
            consumed[k] += n;
            /* Each port's frontier certifies its newest arrival after the owner consumes the batch. */
            for (unsigned q = 0; q < 2; ++q) if (owners == 2 ? q == p : true)
                host_publish(q, consumed[k], last[q]);
            uint64_t t = (last[p] > now ? last[p] : now) + (b % 5) * 1000;
            now = t;
            collect(o, out, worker_step_at(w + k, in, n, out, now));
            /* Extra steps without input interleave departures with arrivals. */
            for (unsigned j = 0; j < owners; ++j) collect(o, out, worker_step_at(w + j, NULL, 0, out, now));
        }
        if (!total) break;
    }
    for (unsigned k = 0; k < owners; ++k) worker_end_input(w + k);
    for (bool backlog = true; backlog;) {
        backlog = false;
        for (unsigned k = 0; k < owners; ++k) {
            collect(o, out, worker_step_at(w + k, NULL, 0, out, now + 1000000000));
            backlog |= worker_has_backlog(w + k);
        }
    }
    for (unsigned k = 0; k < owners; ++k) {
        worker_state_cleanup(w + k);
        assert(stats[k].shutdown_drops == 0 && stats[k].invalid_drops == 0 && stats[k].storage_drops == 0);
    }
    for (size_t i = 0; i < host.line_count; ++i) {
        unsigned k = owners == 1 ? 0 : host.lines[i].ingress_port;
        lines[i] = stats[k].lines[i];
    }
    assert(!host.invalid);
    host_cleanup();
}

int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    pool = rte_pktmbuf_pool_create("split_test", 4095, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    static struct output single[2], split[2];
    struct HngStats a[HNG_MAX_LINES], b[HNG_MAX_LINES];
    run(1, single, a);
    run(2, split, b);
    uint64_t q_drops = 0, late = 0;
    for (unsigned p = 0; p < 2; ++p) {
        assert(single[p].n == split[p].n && single[p].n > 0);
        assert(!memcmp(single[p].r, split[p].r, single[p].n * sizeof(struct record)));
    }
    for (unsigned i = 0; i < HNG_MAX_LINES; ++i) {
        assert(a[i].accepted == b[i].accepted && a[i].q_drops == b[i].q_drops && a[i].dequeued == b[i].dequeued);
        assert(a[i].selected_late == b[i].selected_late && a[i].q_high_water_bytes == b[i].q_high_water_bytes);
        q_drops += a[i].q_drops; late += a[i].selected_late;
    }
    assert(q_drops > 0 && late > 0);
    /* Handles are taken in host_load before launch; taking one after launch fails the run. */
    assert(host_load("split.json") && !host.invalid);
    host_mark_launched();
    assert(!host_take_line_handles() && host.invalid);
    host_cleanup();
    assert(rte_mempool_avail_count(pool) == 4095);
    rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
    printf("single owner vs per-direction owners: egress0=%zu egress1=%zu q_drops=%"PRIu64" late=%"PRIu64
        " identical_sequences=PASS\n", single[0].n, single[1].n, q_drops, late);
}
