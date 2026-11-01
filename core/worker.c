#include <string.h>
#include <rte_ether.h>
#include <rte_malloc.h>
#include "worker.h"

void worker_state_init_owner(struct worker_state *s, struct worker_stats *stats, int ingress_port)
{
    memset(s, 0, sizeof(*s));
    s->engine = host.engine;
    s->handoff_queue = ingress_port >= 0 && host.port_count == 2 && ingress_port == host.ports[1] ? 1 : 0;
    s->producer = s->handoff_queue ? OBS_WORKER1 : OBS_WORKER0;
    s->snapshot.count = host.port_count;
    for (unsigned p = 0; p < host.port_count; ++p) s->snapshot.ports[p] = host.ports[p];
    s->stats = stats;
    for (size_t i = 0; i < host.line_count; ++i) {
        if (ingress_port >= 0 && host.lines[i].ingress_port != ingress_port) continue;
        /* Copy only owned handles; the main lcore took them before launch. */
        s->line[i] = host.line_handle[i];
        s->owned[i] = true;
        s->owned_lines[s->owned_count++] = (uint16_t)i;
        s->min_delay[i] = hng_line_min_delay(s->line[i]);
        /* Group owned lines by egress port, in line order. */
        uint16_t g = 0;
        while (g < s->group_count && host.lines[s->group_line[g][0]].egress_port != host.lines[i].egress_port) ++g;
        if (g == s->group_count) s->group_count++;
        s->group_handle[g][s->group_size[g]] = s->line[i];
        s->group_line[g][s->group_size[g]++] = (uint16_t)i;
    }
    /* host_split_valid(): the owner of an egress port must own every line feeding it. */
    for (size_t i = 0; i < host.line_count; ++i)
        for (size_t j = 0; j < host.line_count; ++j)
            if (s->owned[i] && !s->owned[j] && host.lines[i].egress_port == host.lines[j].egress_port)
                host_invalidate("egress port shared between scheduler owners");
}

void worker_state_init(struct worker_state *s, struct pipeline_stats *stats, uint16_t num_ports)
{
    (void)num_ports;
    worker_state_init_owner(s, &stats->worker, -1);
}

void worker_stats_merge(struct worker_stats *dst, const struct worker_stats *src, int ingress_port)
{
    dst->received += src->received; dst->received_bytes += src->received_bytes;
    dst->sent += src->sent; dst->sent_bytes += src->sent_bytes;
    dst->emulator_drops += src->emulator_drops; dst->tx_ring_drops += src->tx_ring_drops;
    dst->invalid_drops += src->invalid_drops; dst->invalid_drop_bytes += src->invalid_drop_bytes;
    dst->storage_drops += src->storage_drops; dst->storage_drop_bytes += src->storage_drop_bytes;
    dst->shutdown_drops += src->shutdown_drops; dst->shutdown_drop_bytes += src->shutdown_drop_bytes;
    dst->tx_ring_drop_bytes += src->tx_ring_drop_bytes;
    for (size_t i = 0; i < host.line_count; ++i)
        if (ingress_port < 0 || host.lines[i].ingress_port == ingress_port) dst->lines[i] = src->lines[i];
}

static void discard_invalid(struct worker_state *s, struct rte_mbuf *m, uint64_t now)
{
    s->stats->invalid_drops++;
    s->stats->invalid_drop_bytes += m->pkt_len;
    observer_terminal(s->producer, m, OBS_INVALID_INPUT, now);
    rte_pktmbuf_free(m);
}

static int classify(const struct worker_state *s, struct rte_mbuf *m)
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
            q >= line->min_q_bytes && q <= line->max_q_bytes) return s->owned[i] ? (int)i : -2;
    }
    return -1;
}

void worker_end_input(struct worker_state *s)
{
    if (!s->ended) {
        for (uint16_t k = 0; k < s->owned_count; ++k) hng_line_end_input(s->line[s->owned_lines[k]]);
        s->ended = true;
        s->shutdown_start = host_now_ns();
    }
}

static uint64_t watermark(struct worker_state *s, uint16_t port)
{
    uint64_t w;
    if (s->ended) return UINT64_MAX;
    if (host_frontier(port, s->consumed, &w) && w > s->admitted_frontier[port])
        s->admitted_frontier[port] = w;
    return s->admitted_frontier[port];
}

uint16_t worker_step_at(struct worker_state *s, struct rte_mbuf **in_batch,
    uint16_t count, struct rte_mbuf **out_batch, uint64_t now)
{
    /* Classify the whole input first, then admit it with one core call; offers stay in input order. */
    struct Line *handles[HNG_MAX_LINES];
    uint16_t line_of[2 * MAX_BURST_SIZE];
    struct rte_mbuf *offer[2 * MAX_BURST_SIZE];
    void *packets[2 * MAX_BURST_SIZE];
    uint32_t bytes[2 * MAX_BURST_SIZE];
    uint64_t arrivals[2 * MAX_BURST_SIZE], seqs[2 * MAX_BURST_SIZE];
    enum PushResult results[2 * MAX_BURST_SIZE];
    struct HngPacket records[2 * MAX_BURST_SIZE];
    for (size_t i = 0; i < host.line_count; ++i) handles[i] = s->line[i];
    for (uint16_t first = 0; first < count; first += 2 * MAX_BURST_SIZE) {
        uint16_t end = count - first > 2 * MAX_BURST_SIZE ? first + 2 * MAX_BURST_SIZE : count, n = 0;
        for (uint16_t i = first; i < end; ++i) {
            struct rte_mbuf *m = in_batch[i];
            struct packet_meta *meta = packet_meta(m);
            if (observer_enabled())
                for (unsigned p = 0; p < s->snapshot.count; ++p)
                    if (s->snapshot.ports[p] == meta->ingress_port) s->snapshot.seen[p] = meta->ingress_seq + 1;
            s->stats->received++;
            s->stats->received_bytes += m->pkt_len;
            if (meta->ingress_port >= RTE_MAX_ETHPORTS || s->ended || s->aborted) {
                host_invalidate("invalid RX metadata or admission after end");
                discard_invalid(s, m, now);
                continue;
            }
            int line = classify(s, m);
            s->admitted_frontier[meta->ingress_port] = meta->arrival_ns;
            if (line == -2) host_invalidate("packet handed to a scheduler that does not own its line");
            if (line < 0) {
                discard_invalid(s, m, now);
                continue;
            }
            meta->line = (uint16_t)line;
            line_of[n] = (uint16_t)line; offer[n] = m; packets[n] = m; bytes[n] = m->pkt_len;
            arrivals[n] = meta->arrival_ns; seqs[n] = meta->ingress_seq;
            if (observer_enabled()) {
                struct observer_record event = {.kind = OBS_OFFER, .count = 1, .at_ns = now,
                    .actual_selection_seq = s->actual_selection[line]++};
                event.items[0] = observer_packet(m); observer_emit(s->producer, &event);
            }
            n++;
        }
        hng_lines_offer_burst(handles, line_of, packets, bytes, arrivals, seqs, n, results, records);
        for (uint16_t k = 0; k < n; ++k) {
            struct rte_mbuf *m = offer[k];
            enum PushResult result = results[k];
            if (result == Accepted) {
                /* Annotate now, while the mbuf is cached; the record never changes after admission. */
                packet_meta(m)->release_ns = records[k].release_ns;
                packet_meta(m)->deadline_ns = records[k].deadline_ns;
                m->port = host.lines[line_of[k]].egress_port;
                continue;
            }
            if (result == QDrop || result == TraceLoss || result == TraceOutage) {
                s->stats->emulator_drops++;
            } else if (result == StorageDrop) {
                s->stats->storage_drops++;
                s->stats->storage_drop_bytes += m->pkt_len;
                host_invalidate("core delay storage exhausted");
            } else {
                host_invalidate("core rejected timestamp/sequence/length");
                discard_invalid(s, m, now);
                continue;
            }
            observer_terminal(s->producer, m, result == QDrop ? OBS_Q_DROP : result == TraceLoss ? OBS_TRACE_LOSS :
                result == TraceOutage ? OBS_TRACE_OUTAGE : OBS_STORAGE, now);
            rte_pktmbuf_free(m);
        }
    }
    s->consumed += count;
    if (s->ended && !s->aborted) {
        bool sealed = true;
        for (uint16_t k = 0; k < s->owned_count; ++k)
            sealed &= hng_line_drain_advance(s->line[s->owned_lines[k]], now);
        if (!sealed) host_invalidate("core service drain requires sealed input");
    }
    /* One frontier snapshot per step. A newer frontier only permits more output,
     * so a snapshot can defer a packet to the next step but never reorders one. */
    uint64_t w[HNG_MAX_LINES][HNG_MAX_LINES], limit[HNG_MAX_LINES];
    bool blocked[HNG_MAX_LINES] = {0};
    for (uint16_t g = 0; g < s->group_count; ++g) {
        /* A head is safe against unseen input only below every same-egress line's W + min delay. */
        limit[g] = UINT64_MAX;
        for (uint16_t k = 0; k < s->group_size[g]; ++k) {
            size_t line = s->group_line[g][k];
            uint64_t x = w[g][k] = watermark(s, host.lines[line].ingress_port);
            uint64_t bound = x == UINT64_MAX || UINT64_MAX - x < s->min_delay[line] ? UINT64_MAX : x + s->min_delay[line];
            if (bound < limit[g]) limit[g] = bound;
        }
    }
    if (observer_enabled()) {
        for (unsigned p = 0; p < s->snapshot.count; ++p) {
            uint16_t port = s->snapshot.ports[p];
            bool owned = false;
            for (unsigned k = 0; k < s->owned_count; ++k) owned |= host.lines[s->owned_lines[k]].ingress_port == port;
            if (owned) {
                s->snapshot.lower_bound[p] = watermark(s, port);
                s->snapshot.ended[p] = s->ended;
            }
        }
    }
    unsigned open = s->group_count;
    uint16_t emitted = 0;
    for (size_t attempt = 0; open && attempt < (size_t)MAX_BURST_SIZE * s->owned_count; ++attempt) {
        /* Alternate physical outputs; within an output merge by release, physical seq. */
        uint16_t g = s->next_line++ % s->group_count;
        if (blocked[g]) continue;
        struct HngPacket candidate;
        if (hng_lines_pop_merged(s->group_handle[g], w[g], s->group_size[g], now, limit[g], &candidate) < 0) {
            /* Nothing on this port changes until the next step: stop retrying it. */
            blocked[g] = true;
            open--;
            continue;
        }
        /* Dequeue does not touch the (usually cold) mbuf; admission annotated it. */
        out_batch[emitted++] = candidate.packet_ptr;
        s->stats->sent++;
        s->stats->sent_bytes += candidate.packet_bytes;
        if (emitted == MAX_BURST_SIZE) break;
    }
    if (count || emitted || s->ended) observer_publish_snapshot(s->handoff_queue, &s->snapshot);
    return emitted;
}

uint16_t worker_step(struct worker_state *s, struct rte_mbuf **in, uint16_t count, struct rte_mbuf **out)
{
    if (s->ended && !s->aborted && host_now_ns() - s->shutdown_start > SHUTDOWN_TIMEOUT_NS) {
        observer_abort(OBS_ABORT_DRAIN);
        host_invalidate("scheduler shutdown drain timeout");
        s->aborted = true;
        for (uint16_t k = 0; k < s->owned_count; ++k) {
            struct HngPacket p;
            while (hng_line_abort_pop(s->line[s->owned_lines[k]], &p)) {
                s->stats->shutdown_drops++;
                s->stats->shutdown_drop_bytes += p.packet_bytes;
                observer_terminal(s->producer, p.packet_ptr, OBS_SHUTDOWN, host_now_ns());
                rte_pktmbuf_free(p.packet_ptr);
            }
        }
    }
    return worker_step_at(s, in, count, out, host_now_ns());
}

bool worker_has_backlog(const struct worker_state *s)
{
    if (s->aborted) return false;
    for (uint16_t k = 0; k < s->owned_count; ++k)
        if (hng_line_has_backlog(s->line[s->owned_lines[k]])) return true;
    return false;
}

void worker_state_cleanup(struct worker_state *s)
{
    for (uint16_t k = 0; k < s->owned_count; ++k)
        if (s->ended && !s->aborted) hng_line_drain_advance(s->line[s->owned_lines[k]], host_now_ns());
    for (uint16_t k = 0; k < s->owned_count; ++k) {
        size_t line = s->owned_lines[k];
        struct HngPacket p;
        while (hng_line_abort_pop(s->line[line], &p)) {
            s->stats->shutdown_drops++;
            s->stats->shutdown_drop_bytes += p.packet_bytes;
            observer_terminal(s->producer, p.packet_ptr, OBS_SHUTDOWN, host_now_ns());
            rte_pktmbuf_free(p.packet_ptr);
            s->aborted = true;
            observer_abort(OBS_ABORT_DRAIN);
            host_invalidate("forced scheduler cleanup");
        }
        hng_line_stats(s->line[line], &s->stats->lines[line]);
    }
}

void worker_dispatch_prepare(struct worker_state *s, struct rte_mbuf **batch, uint16_t count,
    struct observer_record **reservations, uint64_t at_ns)
{
    if (!observer_enabled()) return;
    for (unsigned i = 0; i < count; ++i) {
        struct packet_meta *meta = packet_meta(batch[i]);
        meta->observation.handoff_seq = s->handoff_next + i;
        meta->observation.handoff_queue = s->handoff_queue;
        struct observer_record *r = reservations[i] = observer_reserve(s->producer);
        if (!r) continue;
        r->kind = OBS_DISPATCH; r->port = batch[i]->port; r->count = 1; r->at_ns = at_ns;
        r->handoff_queue = s->handoff_queue; r->first_handoff_seq = s->handoff_next + i;
        r->snapshot = s->snapshot; r->items[0] = observer_packet(batch[i]);
    }
}
void worker_dispatch_done(struct worker_state *s, struct observer_record **reservations, uint16_t count, uint16_t sent)
{
    if (!observer_enabled()) return;
    for (unsigned i = 0; i < count; ++i) {
        if (!reservations[i]) continue;
        if (i >= sent) reservations[i]->count = 0;
        observer_commit(s->producer);
    }
    s->handoff_next += sent;
}

int lcore_worker(struct lcore_parameters *parameters)
{
    struct worker_state wk;
    struct rte_mbuf *batch[MAX_BURST_SIZE], *output[MAX_BURST_SIZE];
    if (parameters->split)
        worker_state_init_owner(&wk, &parameters->stats->owners[parameters->owner_index],
            host.ports[parameters->owner_index]);
    else worker_state_init(&wk, parameters->stats, parameters->num_ports);
    while (!wk.ended || worker_has_backlog(&wk)) {
        uint16_t count = rte_ring_dequeue_burst(parameters->receive_worker_ring,
            (void **)batch, MAX_BURST_SIZE, NULL);
        if (quit_signal_worker && rte_ring_empty(parameters->receive_worker_ring) && !count) worker_end_input(&wk);
        uint16_t out = worker_step(&wk, batch, count, output);
        struct observer_record *reservations[MAX_BURST_SIZE];
        worker_dispatch_prepare(&wk, output, out, reservations, host_now_ns());
        uint16_t sent = rte_ring_enqueue_burst(parameters->worker_transmit_ring,
            (void **)output, out, NULL);
        worker_dispatch_done(&wk, reservations, out, sent);
        if (sent != out) {
            host_invalidate("TX handoff ring exhausted");
            for (uint16_t i = sent; i < out; ++i) {
                wk.stats->tx_ring_drops++;
                wk.stats->tx_ring_drop_bytes += output[i]->pkt_len;
                observer_terminal(wk.producer, output[i], OBS_HANDOFF, host_now_ns());
                rte_pktmbuf_free(output[i]);
            }
        }
    }
    worker_state_cleanup(&wk);
    if (atomic_fetch_sub(&host.schedulers_running, 1) <= 1) quit_signal_transmit = 1;
    rte_free(parameters);
    return 0;
}
