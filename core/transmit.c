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
    memmove(table->m_table, table->m_table + 1, table->len * sizeof(table->m_table[0]));
}

static void terminal_drop(struct tx_state *s, unsigned line)
{
    struct mbuf_table *table = &s->pending[line];
    struct rte_mbuf *m = table->m_table[0];
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
        table->m_table[table->len++] = m;
        if (table->len > s->stats->pending_peak) s->stats->pending_peak = table->len;
    }
    bool blocked_port[RTE_MAX_ETHPORTS] = {0};
    for (unsigned attempt = 0; attempt < MAX_BURST_SIZE * HNG_MAX_LINES; ++attempt) {
        if (live_clock) now = host_now_ns();
        unsigned start = s->next_line++ % HNG_MAX_LINES;
        if (!s->pending[start].len) continue;
        uint16_t port = s->pending[start].m_table[0]->port;
        if (blocked_port[port]) continue;
        unsigned chosen = HNG_MAX_LINES;
        for (unsigned line = 0; line < HNG_MAX_LINES; ++line) {
            struct mbuf_table *table = &s->pending[line];
            if (!table->len || table->m_table[0]->port != port) continue;
            struct rte_mbuf *m = table->m_table[0];
            uint32_t gate = host.policies[line] ? hng_tx_policy_gate(host.policies[line], now) : 0;
            if (gate == 1) {
                if (now - packet_meta(m)->pending_since_ns > SHUTDOWN_TIMEOUT_NS) {
                    host_invalidate("TX outage hold exceeded bounded backlog time");
                    terminal_drop(s, line);
                }
                continue;
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
            if (chosen == HNG_MAX_LINES || key_before(m, s->pending[chosen].m_table[0])) chosen = line;
        }
        if (chosen == HNG_MAX_LINES) continue;
        struct mbuf_table *table = &s->pending[chosen];
        uint16_t n = table->len < MAX_BURST_SIZE ? table->len : MAX_BURST_SIZE;
        for (unsigned line = 0; line < HNG_MAX_LINES; ++line) {
            if (line == chosen || !s->pending[line].len ||
                s->pending[line].m_table[0]->port != port) continue;
            uint32_t gate = host.policies[line] ? hng_tx_policy_gate(host.policies[line], now) : 0;
            if (gate != 0) continue;
            for (uint16_t i = 1; i < n; ++i) {
                if (key_before(s->pending[line].m_table[0], table->m_table[i])) { n = i; break; }
            }
        }
        if (host.policies[chosen] && hng_tx_policy_gate(host.policies[chosen], now) != 0) continue;
        uint32_t bytes[MAX_BURST_SIZE];
        uint64_t lateness[MAX_BURST_SIZE];
        for (uint16_t i = 0; i < n; ++i) {
            bytes[i] = table->m_table[i]->pkt_len;
            uint64_t deadline = packet_meta(table->m_table[i])->deadline_ns;
            lateness[i] = now > deadline ? now - deadline : 0;
        }
        uint16_t sent = s->burst(port, 0, table->m_table, n);
        s->stats->burst_hist[31 - __builtin_clz(n)]++;
        if (sent > n) {
            /* The PMD contract cannot transfer more than the offered prefix. */
            host_invalidate("TX driver returned an impossible prefix length");
            sent = n;
        }
        s->stats->transmitted += sent;
        s->stats->line_transmitted[chosen] += sent;
        for (uint16_t i = 0; i < sent; ++i) {
            s->stats->transmitted_bytes += bytes[i];
            s->stats->line_transmitted_bytes[chosen] += bytes[i];
            if (lateness[i]) {
                s->stats->late_submit_packets++;
                s->stats->late_submit_total_ns += lateness[i];
                if (lateness[i] > s->stats->late_submit_max_ns) s->stats->late_submit_max_ns = lateness[i];
            }
        }
        table->len -= sent;
        memmove(table->m_table, table->m_table + sent, table->len * sizeof(table->m_table[0]));
        if (sent < n) {
            for (uint16_t i = 0; i < n - sent; ++i) {
                struct packet_meta *meta = packet_meta(table->m_table[i]);
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

int lcore_transmit(struct lcore_parameters *parameters)
{
    struct tx_state tx;
    struct rte_mbuf *batch[MAX_BURST_SIZE];
    tx_state_init(&tx, parameters->stats);
    while (!quit_signal_transmit || !rte_ring_empty(parameters->worker_transmit_ring) || tx_has_backlog(&tx)) {
        uint16_t count = rte_ring_dequeue_burst(parameters->worker_transmit_ring,
            (void **)batch, MAX_BURST_SIZE, NULL);
        tx_step(&tx, batch, count);
    }
    tx_state_cleanup(&tx);
    rte_free(parameters);
    return 0;
}
