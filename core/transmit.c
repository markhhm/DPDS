#include <stddef.h>
#include <string.h>
#include <rte_ethdev.h>
#include <rte_malloc.h>
#include "transmit.h"

void tx_state_init(struct tx_state *s, struct pipeline_stats *stats)
{
    memset(s, 0, sizeof(*s));
    s->stats = &stats->tx;
    s->burst = rte_eth_tx_burst;
}

static void remove_head(struct mbuf_table *table)
{
    --table->len;
    table->head = table->len ? table->head + 1 : 0;
}

static void terminal_drop(struct tx_state *s, unsigned line)
{
    struct mbuf_table *table = &s->pending[line];
    struct rte_mbuf *m = pending_at(table)[0];
    s->stats->tx_drops++;
    s->stats->tx_drop_bytes += m->pkt_len;
    s->stats->line_invalid_drops[line]++;
    rte_pktmbuf_free(m);
    remove_head(table);
}

static bool key_before(const struct rte_mbuf *a, const struct rte_mbuf *b)
{
    const struct packet_meta *x = packet_meta((struct rte_mbuf *)a);
    const struct packet_meta *y = packet_meta((struct rte_mbuf *)b);
    return x->release_ns < y->release_ns ||
        (x->release_ns == y->release_ns && x->ingress_seq < y->ingress_seq);
}

static void tx_step_clock(struct tx_state *s, struct rte_mbuf **batch, uint16_t count, uint64_t now, bool live_clock)
{
    s->stats->received += count;
    for (uint16_t i = 0; i < count; ++i) {
        struct rte_mbuf *m = batch[i];
        unsigned line = packet_meta(m)->line;
        s->stats->received_bytes += m->pkt_len;
        if (line >= HNG_MAX_LINES || m->port >= RTE_MAX_ETHPORTS) {
            host_invalidate("invalid TX metadata");
            s->stats->tx_drops++;
            s->stats->tx_drop_bytes += m->pkt_len;
            rte_pktmbuf_free(m);
            continue;
        }
        struct mbuf_table *table = &s->pending[line];
        if (table->len == TX_PENDING_PER_LINE) {
            host_invalidate("bounded TX backlog exhausted");
            s->stats->tx_drops++;
            s->stats->tx_drop_bytes += m->pkt_len;
            s->stats->line_invalid_drops[line]++;
            rte_pktmbuf_free(m);
            continue;
        }
        packet_meta(m)->pending_since_ns = now;
        packet_meta(m)->retry_active = false;
        if (table->head + table->len == 2 * TX_PENDING_PER_LINE) {
            memmove(table->m_table, pending_at(table), table->len * sizeof(table->m_table[0]));
            table->head = 0;
        }
        table->m_table[table->head + table->len++] = m;
        if (table->len > s->stats->pending_peak) s->stats->pending_peak = table->len;
    }
    bool blocked_port[RTE_MAX_ETHPORTS] = {0};
    for (unsigned attempt = 0; attempt < MAX_BURST_SIZE * HNG_MAX_LINES; ++attempt) {
        if (live_clock) now = host_now_ns();
        unsigned start = s->next_line++ % HNG_MAX_LINES;
        if (!s->pending[start].len) continue;
        uint16_t port = pending_at(&s->pending[start])[0]->port;
        if (blocked_port[port]) continue;
        unsigned chosen = HNG_MAX_LINES;
        for (unsigned line = 0; line < HNG_MAX_LINES; ++line) {
            struct mbuf_table *table = &s->pending[line];
            if (!table->len || pending_at(table)[0]->port != port) continue;
            /* Every replacement head must pass validation before the merge can include it. */
            while (table->len) {
                struct rte_mbuf *m = pending_at(table)[0];
                uint32_t gate = host.policies[line] ? hng_tx_policy_gate(host.policies[line], now) : 0;
                if (gate == 1) {
                    if (now - packet_meta(m)->pending_since_ns > SHUTDOWN_TIMEOUT_NS) {
                        host_invalidate("TX outage hold exceeded bounded backlog time");
                        terminal_drop(s, line);
                        continue;
                    }
                    break;
                }
                if (gate == 2) {
                    host_invalidate("missed-deadline outage drop after model release");
                    s->stats->trace_outage_drops++;
                    s->stats->trace_outage_bytes += m->pkt_len;
                    s->stats->line_outage_drops[line]++;
                    rte_pktmbuf_free(m);
                    remove_head(table);
                    continue;
                }
                if (gate != 0 || (packet_meta(m)->retry_active &&
                    now - packet_meta(m)->retry_since_ns > TERMINAL_TIMEOUT_NS)) {
                    host_invalidate(gate ? "TX trace coverage expired" : "TX retry timeout");
                    terminal_drop(s, line);
                    continue;
                }
                if (chosen == HNG_MAX_LINES || key_before(m, pending_at(&s->pending[chosen])[0])) chosen = line;
                break;
            }
        }
        if (chosen == HNG_MAX_LINES) continue;
        /* Gather one burst for this port: merge the eligible lines' pending frames in
         * global release order. Frames stay in their tables until the PMD accepts them. */
        bool eligible[HNG_MAX_LINES] = {0};
        for (unsigned line = 0; line < HNG_MAX_LINES; ++line) {
            if (!s->pending[line].len || pending_at(&s->pending[line])[0]->port != port) continue;
            eligible[line] = !host.policies[line] || hng_tx_policy_gate(host.policies[line], now) == 0;
        }
        struct rte_mbuf *burst[MAX_BURST_SIZE];
        uint8_t line_of[MAX_BURST_SIZE];
        uint16_t taken[HNG_MAX_LINES] = {0}, n = 0;
        while (n < MAX_BURST_SIZE) {
            unsigned next = HNG_MAX_LINES;
            for (unsigned line = 0; line < HNG_MAX_LINES; ++line) {
                if (!eligible[line] || taken[line] == s->pending[line].len) continue;
                if (next == HNG_MAX_LINES ||
                    key_before(pending_at(&s->pending[line])[taken[line]], pending_at(&s->pending[next])[taken[next]]))
                    next = line;
            }
            if (next == HNG_MAX_LINES) break;
            burst[n] = pending_at(&s->pending[next])[taken[next]++];
            line_of[n++] = (uint8_t)next;
        }
        uint32_t bytes[MAX_BURST_SIZE];
        uint64_t lateness[MAX_BURST_SIZE];
        for (uint16_t i = 0; i < n; ++i) {
            bytes[i] = burst[i]->pkt_len;
            uint64_t deadline = packet_meta(burst[i])->deadline_ns;
            lateness[i] = now > deadline ? now - deadline : 0;
        }
        uint16_t sent = s->burst(port, 0, burst, n);
        s->stats->burst_hist[31 - __builtin_clz(n)]++;
        if (sent > n) {
            /* The PMD contract cannot transfer more than the offered prefix. */
            host_invalidate("TX driver returned an impossible prefix length");
            sent = n;
        }
        s->stats->transmitted += sent;
        uint16_t accepted[HNG_MAX_LINES] = {0};
        for (uint16_t i = 0; i < sent; ++i) {
            unsigned line = line_of[i];
            accepted[line]++;
            s->stats->line_transmitted[line]++;
            s->stats->transmitted_bytes += bytes[i];
            s->stats->line_transmitted_bytes[line] += bytes[i];
            if (lateness[i]) {
                s->stats->late_submit_packets++;
                s->stats->late_submit_total_ns += lateness[i];
                if (lateness[i] > s->stats->late_submit_max_ns) s->stats->late_submit_max_ns = lateness[i];
            }
        }
        /* The accepted prefix of a merge is a prefix of each line, so each line drops
         * its accepted frames from its head; the refused frames stay there in order. */
        for (unsigned line = 0; line < HNG_MAX_LINES; ++line) {
            struct mbuf_table *table = &s->pending[line];
            table->len -= accepted[line];
            table->head = table->len ? table->head + accepted[line] : 0;
        }
        if (sent < n) {
            for (uint16_t i = sent; i < n; ++i) {
                struct packet_meta *meta = packet_meta(burst[i]);
                if (!meta->retry_active) { meta->retry_active = true; meta->retry_since_ns = now; }
            }
            s->stats->retries++;
            blocked_port[port] = true;
        }
    }
}

void tx_step(struct tx_state *s, struct rte_mbuf **batch, uint16_t count)
{
    tx_step_clock(s, batch, count, host_now_ns(), true);
}

void tx_step_at(struct tx_state *s, struct rte_mbuf **batch, uint16_t count, uint64_t now)
{
    tx_step_clock(s, batch, count, now, false);
}

bool tx_has_backlog(const struct tx_state *s)
{
    for (unsigned line = 0; line < HNG_MAX_LINES; ++line) if (s->pending[line].len) return true;
    return false;
}

void tx_state_cleanup(struct tx_state *s)
{
    while (tx_has_backlog(s)) tx_step(s, NULL, 0);
}

void tx_stats_merge(struct tx_stats *dst, const struct tx_stats *src)
{
    TX_STATS_FIELDS(STATS_MERGE_S, STATS_MERGE_A)
}

int lcore_transmit(struct lcore_parameters *parameters)
{
    struct tx_state tx;
    struct rte_mbuf *batch[MAX_BURST_SIZE];
    tx_state_init(&tx, parameters->stats);
    if (parameters->io_split) tx.stats = &parameters->stats->tx_owners[parameters->owner_index];
    /* quit_signal_transmit follows the last scheduler's final enqueue. */
    while (!quit_signal_transmit || !rte_ring_empty(parameters->worker_transmit_ring) || tx_has_backlog(&tx)) {
        uint16_t count = rte_ring_dequeue_burst(parameters->worker_transmit_ring,
            (void **)batch, MAX_BURST_SIZE, NULL);
        tx_step(&tx, batch, count);
    }
    tx_state_cleanup(&tx);
    rte_free(parameters);
    return 0;
}
