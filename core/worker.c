#include <string.h>
#include <rte_ether.h>
#include <rte_malloc.h>
#include "worker.h"

void worker_state_init(struct worker_state *s, struct pipeline_stats *stats, uint16_t num_ports)
{
    (void)num_ports;
    memset(s, 0, sizeof(*s));
    s->engine = host.engine;
    s->stats = &stats->worker;
}

static void discard_invalid(struct worker_state *s, struct rte_mbuf *m)
{
    s->stats->invalid_drops++;
    s->stats->invalid_drop_bytes += m->pkt_len;
    rte_pktmbuf_free(m);
}

static int classify(struct rte_mbuf *m)
{
    if (m->nb_segs != 1 || m->pkt_len < 18 || m->pkt_len > 1518 ||
        m->data_len != m->pkt_len || (m->ol_flags & (RTE_MBUF_F_RX_VLAN_STRIPPED | RTE_MBUF_F_RX_QINQ_STRIPPED))) return -1;
    const uint8_t *d = rte_pktmbuf_mtod(m, const uint8_t *);
    if (d[12] != 0x81 || d[13] != 0x00 ||
        (d[16] == 0x81 && d[17] == 0x00) || (d[16] == 0x88 && d[17] == 0xa8)) return -1;
    uint16_t vlan = ((uint16_t)d[14] << 8 | d[15]) & 0xfff;
    struct packet_meta *meta = packet_meta(m);
    meta->vlan_id = vlan;
    for (size_t i = 0; i < host.line_count; ++i) {
        struct LineInfo *line = &host.lines[i];
        uint64_t q = m->pkt_len + 4 < 64 ? 64 : m->pkt_len + 4;
        if (line->ingress_port == meta->ingress_port && line->vlan_id == vlan &&
            q >= line->min_q_bytes && q <= line->max_q_bytes) return (int)i;
    }
    return -1;
}

void worker_end_input(struct worker_state *s)
{
    if (!s->ended) {
        hng_engine_end_input(s->engine);
        s->ended = true;
        s->shutdown_start = host_now_ns();
    }
}

static uint64_t watermark(struct worker_state *s, uint16_t port)
{
    uint64_t w;
    if (s->ended) return UINT64_MAX;
    if (host_frontier(port, s->consumed, &w) && w > s->admitted_frontier[port]) return w;
    return s->admitted_frontier[port];
}

uint16_t worker_step_at(struct worker_state *s, struct rte_mbuf **in_batch,
    uint16_t count, struct rte_mbuf **out_batch, uint64_t now)
{
    for (uint16_t i = 0; i < count; ++i) {
        struct rte_mbuf *m = in_batch[i];
        struct packet_meta *meta = packet_meta(m);
        s->stats->received++;
        s->stats->received_bytes += m->pkt_len;
        if (meta->ingress_port >= RTE_MAX_ETHPORTS || s->ended || s->aborted) {
            host_invalidate("invalid RX metadata or admission after end");
            discard_invalid(s, m);
            continue;
        }
        int line = classify(m);
        s->admitted_frontier[meta->ingress_port] = meta->arrival_ns;
        if (line < 0) {
            discard_invalid(s, m);
            continue;
        }
        meta->line = (uint16_t)line;
        enum PushResult result = hng_engine_offer(s->engine, line, m, m->pkt_len,
            meta->arrival_ns, meta->ingress_seq);
        if (result == Accepted) continue;
        if (result == QDrop || result == TraceLoss || result == TraceOutage) {
            s->stats->emulator_drops++;
        } else if (result == StorageDrop) {
            s->stats->storage_drops++;
            s->stats->storage_drop_bytes += m->pkt_len;
            host_invalidate("core delay storage exhausted");
        } else {
            host_invalidate("core rejected timestamp/sequence/length");
            discard_invalid(s, m);
            continue;
        }
        rte_pktmbuf_free(m);
    }
    s->consumed += count;
    if (s->ended && !s->aborted && !hng_engine_drain_advance(s->engine, now))
        host_invalidate("core service drain requires sealed input");
    uint16_t emitted = 0;
    for (size_t attempt = 0; attempt < MAX_BURST_SIZE * host.line_count; ++attempt) {
        size_t start = s->next_line++ % host.line_count;
        struct HngPacket candidate;
        size_t chosen = host.line_count;
        /* Alternate physical outputs; within an output merge by release, physical seq. */
        uint16_t port = host.lines[start].egress_port;
        for (size_t line = 0; line < host.line_count; ++line) {
            struct HngPacket p;
            if (host.lines[line].egress_port != port || !hng_engine_peek(s->engine, line,
                now, watermark(s, host.lines[line].ingress_port), &p)) continue;
            if (chosen == host.line_count || p.release_ns < candidate.release_ns ||
                (p.release_ns == candidate.release_ns && p.ingress_seq < candidate.ingress_seq)) {
                candidate = p;
                chosen = line;
            }
        }
        if (chosen == host.line_count) continue;
        bool complete = true;
        for (size_t line = 0; line < host.line_count; ++line) {
            if (host.lines[line].egress_port != port) continue;
            uint64_t w = watermark(s, host.lines[line].ingress_port);
            uint64_t min_delay = hng_engine_min_delay(s->engine, line);
            if (w != UINT64_MAX && candidate.release_ns >
                (UINT64_MAX - w < min_delay ? UINT64_MAX : w + min_delay)) complete = false;
        }
        if (!complete) continue;
        if (!hng_engine_pop(s->engine, chosen, now,
            watermark(s, host.lines[chosen].ingress_port), &candidate)) continue;
        struct rte_mbuf *m = candidate.packet_ptr;
        struct packet_meta *meta = packet_meta(m);
        meta->release_ns = candidate.release_ns;
        meta->deadline_ns = candidate.deadline_ns;
        m->port = port;
        out_batch[emitted++] = m;
        s->stats->sent++;
        s->stats->sent_bytes += m->pkt_len;
        if (emitted == MAX_BURST_SIZE) break;
    }
    return emitted;
}

uint16_t worker_step(struct worker_state *s, struct rte_mbuf **in, uint16_t count, struct rte_mbuf **out)
{
    if (s->ended && !s->aborted && host_now_ns() - s->shutdown_start > SHUTDOWN_TIMEOUT_NS) {
        host_invalidate("scheduler shutdown drain timeout");
        s->aborted = true;
        for (size_t line = 0; line < host.line_count; ++line) {
            struct HngPacket p;
            while (hng_engine_abort_pop(s->engine, line, &p)) {
                s->stats->shutdown_drops++;
                s->stats->shutdown_drop_bytes += p.packet_bytes;
                rte_pktmbuf_free(p.packet_ptr);
            }
        }
    }
    return worker_step_at(s, in, count, out, host_now_ns());
}

bool worker_has_backlog(const struct worker_state *s)
{
    return !s->aborted && hng_engine_has_backlog(s->engine);
}

void worker_state_cleanup(struct worker_state *s)
{
    if (s->ended && !s->aborted) hng_engine_drain_advance(s->engine, host_now_ns());
    for (size_t line = 0; line < host.line_count; ++line) {
        struct HngPacket p;
        while (hng_engine_abort_pop(s->engine, line, &p)) {
            s->stats->shutdown_drops++;
            s->stats->shutdown_drop_bytes += p.packet_bytes;
            rte_pktmbuf_free(p.packet_ptr);
            s->aborted = true;
            host_invalidate("forced scheduler cleanup");
        }
        hng_engine_stats(s->engine, line, &s->stats->lines[line]);
    }
}

int lcore_worker(struct lcore_parameters *parameters)
{
    struct worker_state wk;
    struct rte_mbuf *batch[MAX_BURST_SIZE], *output[MAX_BURST_SIZE];
    worker_state_init(&wk, parameters->stats, parameters->num_ports);
    while (!wk.ended || worker_has_backlog(&wk)) {
        uint16_t count = rte_ring_dequeue_burst(parameters->receive_worker_ring,
            (void **)batch, MAX_BURST_SIZE, NULL);
        if (quit_signal_worker && rte_ring_empty(parameters->receive_worker_ring) && !count) worker_end_input(&wk);
        uint16_t out = worker_step(&wk, batch, count, output);
        uint16_t sent = rte_ring_enqueue_burst(parameters->worker_transmit_ring,
            (void **)output, out, NULL);
        if (sent != out) {
            host_invalidate("TX handoff ring exhausted");
            for (uint16_t i = sent; i < out; ++i) {
                wk.stats->tx_ring_drops++;
                wk.stats->tx_ring_drop_bytes += output[i]->pkt_len;
                rte_pktmbuf_free(output[i]);
            }
        }
    }
    worker_state_cleanup(&wk);
    quit_signal_transmit = 1;
    rte_free(parameters);
    return 0;
}
