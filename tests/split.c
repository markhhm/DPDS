/* SPDX-License-Identifier: Apache-2.0 */
/*
 * Per-direction owners keep the scheduler semantics. The same input and clock
 * schedule goes through one owner of all four lines and through one owner per
 * ingress port. Schedule: at each tick every owner gets one call with all of
 * its input for that tick, then no-input calls at the same time until it emits
 * nothing, so the 16-packet output cap per call never defers a packet. A
 * packet's departure time is the tick at which an owner emitted it.
 * Per egress, both give identical (sequence, VLAN, length, release, deadline,
 * departure) records; line and worker counters also match.
 *
 * Every owner's output also goes through the production TX step. Layouts 1 and
 * 2 share one TX state; layout 3 (one RX, scheduler and TX lcore per direction)
 * gives each owner its own TX state and counters, merged afterwards. A packet's
 * departure is the tick of the TX burst that sent it; TX must drain every call.
 * TX totals and per-line counts match across all three layouts; burst sizes
 * follow the scheduler's call batching, so layout 3 matches layout 2 on every
 * TX counter. A second pass uses a TX that refuses the first offer of every
 * fifth sequence number: TX retries at the same tick, so records stay the same.
 *
 * The single-owner output must also match a golden digest from the pre-M4
 * scheduler (DPDS d75f4fc). Regenerate it by building this file with
 * -DSPLIT_PRE_M4 against that revision and running it with SPLIT_GOLDEN_WRITE=path.
 */
#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <rte_eal.h>
#include "../core/worker.h"
#ifndef SPLIT_PRE_M4
#include "../core/transmit.h"
#endif

#define PACKETS 6000
#define TICKS (PACKETS / MAX_BURST_SIZE + 64)
struct record { uint64_t seq, release, deadline, departed; uint32_t length; uint16_t vlan, ingress; };
struct output { struct record r[2 * PACKETS]; size_t n; };
static struct rte_mempool *pool;

static uint64_t next(uint64_t *x) { *x ^= *x << 13; *x ^= *x >> 7; *x ^= *x << 17; return *x; }

/* Tick b of port p: deterministic lengths, VLANs and bursty arrivals. */
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

static unsigned collect(struct output *o, struct rte_mbuf **m, unsigned n, uint64_t now)
{
    for (unsigned i = 0; i < n; ++i) {
        struct packet_meta *meta = packet_meta(m[i]);
        assert(m[i]->port == 1 - meta->ingress_port);
        struct output *out = o + m[i]->port;
        out->r[out->n++] = (struct record){meta->ingress_seq, meta->release_ns, meta->deadline_ns, now,
            m[i]->pkt_len, meta->vlan_id, meta->ingress_port};
        rte_pktmbuf_free(m[i]);
    }
    return n;
}

#ifndef SPLIT_PRE_M4
/* The TX burst function records departures at the current tick instead of a NIC. */
static struct output *tx_out;
static uint64_t tx_now;
static bool refuse;
static uint16_t capture(uint16_t port, uint16_t queue, struct rte_mbuf **m, uint16_t n)
{
    (void)queue;
    assert(n && m[0]->port == port);
    uint16_t accept = n;
    /* Refuse from the first frame offered for the first time whose sequence is a multiple of 5. */
    for (uint16_t i = 0; refuse && i < n && accept == n; ++i)
        if (!packet_meta(m[i])->retry_active && packet_meta(m[i])->ingress_seq % 5 == 0) accept = i;
    return (uint16_t)collect(tx_out, m, accept, tx_now);
}
#endif

static uint64_t digest(const struct output *o)
{
    uint64_t h = 0xcbf29ce484222325ull; /* FNV-1a over the record fields, in a fixed order */
    for (size_t i = 0; i < o->n; ++i) {
        const struct record *r = o->r + i;
        uint64_t f[7] = {r->seq, r->release, r->deadline, r->departed, r->length, r->vlan, r->ingress};
        for (unsigned j = 0; j < 7; ++j)
            for (unsigned b = 0; b < 8; ++b) { h ^= (f[j] >> (8 * b)) & 0xff; h *= 0x100000001b3ull; }
    }
    return h;
}

struct totals { uint64_t sent, emulator_drops, tx_ring_drops; };

#ifndef SPLIT_PRE_M4
/* Hand n scheduler outputs to TX at the same tick; returns n so callers loop until idle. */
static unsigned to_tx(struct tx_state *tx, struct rte_mbuf **m, unsigned n, uint64_t now)
{
    tx_now = now;
    tx_step_at(tx, m, (uint16_t)n, now);
    /* A refused frame blocks its port for this step; retry at the same tick. */
    for (unsigned retry = 0; tx_has_backlog(tx); ++retry) { assert(refuse && retry < 64); tx_step_at(tx, NULL, 0, now); }
    return n;
}
#endif

/* layout 1: one worker owns all lines. 2: one worker per ingress port, shared TX.
 * 3: one worker and one TX state per ingress port. tx receives the merged TX counters. */
static void run(unsigned layout, struct output o[2], struct HngStats lines[HNG_MAX_LINES], struct totals *t,
    struct tx_stats *tx_total)
{
    unsigned owners = layout == 1 ? 1 : 2;
    assert(host_load("split.json"));
#ifdef SPLIT_PRE_M4
    assert(owners == 1);
    static struct pipeline_stats pstats;
    memset(&pstats, 0, sizeof(pstats));
    struct worker_stats *stats = &pstats.worker;
    struct worker_state w[1];
    worker_state_init(w, &pstats, 2);
#else
    struct worker_stats stats[2];
    memset(stats, 0, sizeof(stats));
    struct worker_state w[2];
    for (unsigned k = 0; k < owners; ++k) worker_state_init_owner(w + k, stats + k, owners == 1 ? -1 : (int)k);
    /* Owners hold handles only for their own lines. */
    for (unsigned k = 0; k < owners; ++k)
        for (size_t i = 0; i < host.line_count; ++i) assert((w[k].line[i] != NULL) == w[k].owned[i]);
    static struct pipeline_stats tx_counters[2];
    memset(tx_counters, 0, sizeof(tx_counters));
    struct tx_state tx[2];
    for (unsigned k = 0; k < 2; ++k) { tx_state_init(tx + k, tx_counters + k); tx[k].burst = capture; }
    tx_out = o;
#endif
    uint64_t last[2] = {0}, consumed[2] = {0}, now = 0;
#ifdef SPLIT_PRE_M4
#define emit(wk, in, n, out, now) collect(o, out, worker_step_at(wk, in, n, out, now), now)
#else
    /* Layout 3: owner k's output goes to TX state k; otherwise to TX state 0. */
#define emit(wk, in, n, out, now) to_tx(tx + (layout == 3 ? (unsigned)((wk) - w) : 0), \
    out, worker_step_at(wk, in, n, out, now), now)
#endif
    struct rte_mbuf *in[2][MAX_BURST_SIZE], *out[MAX_BURST_SIZE];
    for (unsigned b = 0; b < TICKS; ++b) {
        unsigned n[2];
        for (unsigned p = 0; p < 2; ++p) n[p] = batch(p, b, in[p], last + p);
        /* The clock trails the newest arrival by 0-4 us, so heads become due mid-input. */
        uint64_t newest = last[0] > last[1] ? last[0] : last[1];
        if (newest + (b % 5) * 1000 > now) now = newest + (b % 5) * 1000;
        for (unsigned k = 0; k < owners; ++k) {
            struct rte_mbuf *input[2 * MAX_BURST_SIZE];
            unsigned count = 0;
            for (unsigned p = 0; p < 2; ++p) {
                if (owners == 2 && p != k) continue;
                memcpy(input + count, in[p], n[p] * sizeof(*input));
                count += n[p];
            }
            consumed[k] += count;
            /* Each port's frontier certifies its newest arrival once its owner consumes the tick. */
            for (unsigned p = 0; p < 2; ++p)
                if (owners == 1 || p == k) host_publish((uint16_t)p, consumed[k], last[p]);
            unsigned e = emit(w + k, input, (uint16_t)count, out, now);
            while (e) e = emit(w + k, NULL, 0, out, now);
        }
    }
    for (unsigned k = 0; k < owners; ++k) worker_end_input(w + k);
    now += 1000000000;
    for (unsigned k = 0; k < owners; ++k) {
        while (emit(w + k, NULL, 0, out, now)) {}
        assert(!worker_has_backlog(w + k));
    }
#undef emit
    *t = (struct totals){0};
#ifndef SPLIT_PRE_M4
    memset(tx_total, 0, sizeof(*tx_total));
    for (unsigned k = 0; k < 2; ++k) { assert(!tx_has_backlog(tx + k)); tx_stats_merge(tx_total, &tx_counters[k].tx); }
#else
    (void)tx_total;
#endif
    for (unsigned k = 0; k < owners; ++k) {
        worker_state_cleanup(w + k);
        assert(stats[k].shutdown_drops == 0 && stats[k].invalid_drops == 0 && stats[k].storage_drops == 0);
        t->sent += stats[k].sent; t->emulator_drops += stats[k].emulator_drops; t->tx_ring_drops += stats[k].tx_ring_drops;
    }
    for (size_t i = 0; i < host.line_count; ++i) lines[i] = stats[owners == 1 ? 0 : host.lines[i].ingress_port].lines[i];
    assert(!host.invalid);
    host_cleanup();
}

int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    pool = rte_pktmbuf_pool_create("split_test", 4095, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    static struct output single[2];
    struct HngStats a[HNG_MAX_LINES];
    struct totals ta;
    static struct tx_stats txa;
    run(1, single, a, &ta, &txa);
    const char *write = getenv("SPLIT_GOLDEN_WRITE");
    if (write) {
        FILE *f = fopen(write, "w");
        assert(f);
        fprintf(f, "egress,count,fnv1a64\n");
        for (unsigned p = 0; p < 2; ++p) fprintf(f, "%u,%zu,%016" PRIx64 "\n", p, single[p].n, digest(single + p));
        fclose(f);
    }
#ifndef SPLIT_PRE_M4
    /* Golden from the pre-M4 single-owner scheduler on the same input and schedule. */
    FILE *golden = fopen("split-golden.csv", "r");
    assert(golden);
    char header[64];
    assert(fgets(header, sizeof(header), golden));
    for (unsigned p = 0; p < 2; ++p) {
        unsigned egress; size_t count; uint64_t hash;
        assert(fscanf(golden, "%u,%zu,%" SCNx64 "\n", &egress, &count, &hash) == 3 && egress == p);
        assert(single[p].n == count && digest(single + p) == hash);
    }
    fclose(golden);
    uint64_t q_drops = 0, late = 0;
    for (unsigned i = 0; i < HNG_MAX_LINES; ++i) { q_drops += a[i].q_drops; late += a[i].selected_late; }
    assert(q_drops > 0 && late > 0 && ta.emulator_drops == q_drops && ta.sent > 0);
    assert(txa.transmitted == ta.sent && txa.tx_drops == 0 && txa.retries == 0);
    /* Layout 2: scheduler per ingress port. Layout 3: also RX and TX per direction.
     * Pass 0: TX accepts every frame. Pass 1: TX refuses some first offers. */
    uint64_t retries = 0;
    for (unsigned pass = 0; pass < 2; ++pass) {
        refuse = pass == 1;
        static struct tx_stats txs;
        for (unsigned layout = pass ? 1 : 2; layout <= 3; ++layout) {
            static struct output split[2];
            struct HngStats b[HNG_MAX_LINES];
            struct totals tb;
            static struct tx_stats txb;
            memset(split, 0, sizeof(split));
            run(layout, split, b, &tb, &txb);
            for (unsigned p = 0; p < 2; ++p) {
                assert(single[p].n == split[p].n && single[p].n > 0);
                assert(!memcmp(single[p].r, split[p].r, single[p].n * sizeof(struct record)));
            }
            assert(ta.sent == tb.sent && ta.emulator_drops == tb.emulator_drops && ta.tx_ring_drops == tb.tx_ring_drops);
            for (unsigned i = 0; i < HNG_MAX_LINES; ++i) {
                assert(a[i].accepted == b[i].accepted && a[i].q_drops == b[i].q_drops && a[i].dequeued == b[i].dequeued);
                assert(a[i].selected_late == b[i].selected_late && a[i].q_high_water_bytes == b[i].q_high_water_bytes);
            }
            /* TX counters: totals, lateness and per-line counts match the single owner without
             * refusals. Burst sizes, pending_peak and retries depend on how the scheduler
             * batches its output, so layout 3 must match layout 2 of the same pass on every field. */
            assert(txa.received == txb.received && txa.received_bytes == txb.received_bytes);
            assert(txa.transmitted == txb.transmitted && txa.transmitted_bytes == txb.transmitted_bytes);
            assert(txa.tx_drops == txb.tx_drops && txa.trace_outage_drops == txb.trace_outage_drops);
            assert(txa.late_submit_packets == txb.late_submit_packets && txa.late_submit_total_ns == txb.late_submit_total_ns);
            assert(txa.late_submit_max_ns == txb.late_submit_max_ns);
            assert(!memcmp(txa.line_transmitted, txb.line_transmitted, sizeof(txa.line_transmitted)));
            assert(!memcmp(txa.line_transmitted_bytes, txb.line_transmitted_bytes, sizeof(txa.line_transmitted_bytes)));
            assert(pass ? txb.retries > 0 : txb.retries == 0);
            if (pass) retries += txb.retries;
            if (layout == 2) txs = txb;
            else if (layout == 3) assert(!memcmp(&txs, &txb, sizeof(txs)));
        }
    }
    refuse = false;
    /* Handles are taken in host_load before launch; taking one after launch fails the run. */
    assert(host_load("split.json") && !host.invalid);
    host_mark_launched();
    assert(!host_take_line_handles() && host.invalid);
    host_cleanup();
    printf("single owner vs per-direction owners vs per-direction RX/scheduler/TX: egress0=%zu egress1=%zu"
        " sent=%" PRIu64 " q_drops=%" PRIu64 " late=%" PRIu64 " refused_retries=%" PRIu64 " late_submit_max_ns=%" PRIu64 " digest0=%016" PRIx64 " digest1=%016" PRIx64
        " identical_records_departures_counters=PASS pre_m4_golden=PASS\n",
        single[0].n, single[1].n, ta.sent, q_drops, late, retries, txa.late_submit_max_ns, digest(single), digest(single + 1));
#endif
    assert(rte_mempool_avail_count(pool) == 4095);
    rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
}
