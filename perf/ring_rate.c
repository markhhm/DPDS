/* SPDX-License-Identifier: Apache-2.0 */
/*
 * Maximum host packet rate over net_ring ports. The production RX, scheduler
 * and TX lcore functions run unchanged on companion rings. The main lcore is a
 * closed-loop generator: it recycles every frame that leaves an egress ring
 * into that port's ingress ring and tops the population up after drops.
 * The population never exceeds the TX handoff ring, because the production
 * scheduler invalidates the run instead of waiting for TX handoff space.
 * Software rings only: the result is not evidence of NIC or 100G performance.
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <rte_cycles.h>
#include <rte_eal.h>
#include <rte_eth_ring.h>
#include <rte_launch.h>
#include <rte_malloc.h>
#include "../core/receive.h"
#include "../core/worker.h"
#include "../core/transmit.h"

#define BURST 32

static const char usage[] =
    "usage: hng-ring-rate EAL_ARGS -- [-c config.json] [-b mbuf_bytes] [-d seconds] [-w warmup_s] [-n population]\n";

static struct rte_ring *source[2], *sink[2];

static void frame(struct rte_mbuf *m, unsigned port, uint16_t bytes, uint64_t n)
{
    uint8_t *d = (uint8_t *)rte_pktmbuf_append(m, bytes);
    if (!d) rte_exit(EXIT_FAILURE, "mbuf too small for %u bytes\n", bytes);
    memset(d, 0, bytes);
    memset(d, 0xff, 6);
    d[6] = 2; d[7] = (uint8_t)port;
    d[12] = 0x81; d[13] = 0x00; d[15] = (uint8_t)(100 + n % 2); d[16] = 0x88; d[17] = 0xb5;
}

/* Generate up to `free` frames from the pool into the ingress rings. */
static uint64_t top_up(struct rte_mempool *pool, uint16_t bytes, uint64_t *serial)
{
    uint64_t made = 0;
    for (unsigned p = 0; p < 2; ++p) {
        struct rte_mbuf *m[BURST];
        unsigned n = rte_ring_free_count(source[p]);
        if (n > BURST) n = BURST;
        if (!n || rte_pktmbuf_alloc_bulk(pool, m, n) != 0) continue;
        for (unsigned i = 0; i < n; ++i) frame(m[i], p, bytes, (*serial)++);
        unsigned sent = rte_ring_enqueue_burst(source[p], (void **)m, n, NULL);
        for (unsigned i = sent; i < n; ++i) rte_pktmbuf_free(m[i]);
        made += sent;
    }
    return made;
}

int main(int argc, char **argv)
{
    int eal = rte_eal_init(argc, argv);
    if (eal < 0) rte_exit(EXIT_FAILURE, "Invalid EAL arguments\n");
    argc -= eal; argv += eal;
    const char *config = "ring_rate.json";
    unsigned bytes = 1518, population = SCHED_TX_RING_SZ - 1;
    double seconds = 2.0, warmup = 0.25;
    int option;
    optind = 1;
    while ((option = getopt(argc, argv, "c:b:d:w:n:")) != -1) {
        if (option == 'c') config = optarg;
        else if (option == 'b') bytes = (unsigned)strtoul(optarg, NULL, 10);
        else if (option == 'd') seconds = strtod(optarg, NULL);
        else if (option == 'w') warmup = strtod(optarg, NULL);
        else if (option == 'n') population = (unsigned)strtoul(optarg, NULL, 10);
        else rte_exit(EXIT_FAILURE, "%s", usage);
    }
    if (bytes < 60 || bytes > 1518 || seconds <= 0 || warmup < 0 || population < 64 ||
        population > SCHED_TX_RING_SZ - 1)
        rte_exit(EXIT_FAILURE, "%s", usage);
    /* 4 lcores: one scheduler. 5 lcores: one scheduler per ingress port, as in dpds. */
    bool split = rte_lcore_count() == 5;
    if (rte_lcore_count() != 4 && !split) rte_exit(EXIT_FAILURE, "hng-ring-rate needs 4 or 5 lcores\n");
    if (!host_load(config)) rte_exit(EXIT_FAILURE, "configuration rejected\n");
    for (unsigned p = 0; p < 2; ++p) {
        char a[32], b[32], name[32];
        snprintf(a, sizeof(a), "rate_src%u", p); snprintf(b, sizeof(b), "rate_sink%u", p);
        source[p] = rte_ring_create(a, 1024, rte_socket_id(), RING_F_SP_ENQ | RING_F_SC_DEQ);
        sink[p] = rte_ring_create(b, 1024, rte_socket_id(), RING_F_SP_ENQ | RING_F_SC_DEQ);
        if (!source[p] || !sink[p]) rte_exit(EXIT_FAILURE, "cannot create rings\n");
        snprintf(name, sizeof(name), "net_ring%u", p);
        if (rte_eth_from_rings(name, &source[p], 1, &sink[p], 1, rte_socket_id()) != (int)p)
            rte_exit(EXIT_FAILURE, "net_ring port %u has an unexpected id\n", p);
        host.ports[host.port_count++] = (uint16_t)p;
    }
    struct rte_mempool *pool = rte_pktmbuf_pool_create("rate_pool", population, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    if (!pool) rte_exit(EXIT_FAILURE, "cannot allocate %u mbufs: %s\n", population, rte_strerror(rte_errno));
    for (uint16_t p = 0; p < 2; ++p)
        if (host_prepare_port(p) != 0 || initialize_port(p, pool) != 0) rte_exit(EXIT_FAILURE, "port %u setup failed\n", p);
    struct pipeline_stats *stats = rte_zmalloc("rate_stats", sizeof(*stats), 64);
    if (split && !host_split_valid()) rte_exit(EXIT_FAILURE, "split needs each egress fed by one ingress port\n");
    struct rte_ring *rx_ring = rte_ring_create("rate_rx", SCHED_RX_RING_SZ, rte_socket_id(), RING_F_SC_DEQ | RING_F_SP_ENQ);
    struct rte_ring *rx_ring1 = rte_ring_create("rate_rx1", SCHED_RX_RING_SZ, rte_socket_id(), RING_F_SC_DEQ | RING_F_SP_ENQ);
    struct rte_ring *tx_ring = rte_ring_create("rate_tx", SCHED_TX_RING_SZ, rte_socket_id(), RING_F_SC_DEQ | (split ? 0 : RING_F_SP_ENQ));
    if (!stats || !rx_ring || !rx_ring1 || !tx_ring) rte_exit(EXIT_FAILURE, "cannot allocate pipeline state\n");
    lcore_function_t *functions[4] = {(lcore_function_t *)lcore_transmit, (lcore_function_t *)lcore_worker,
        split ? (lcore_function_t *)lcore_worker : (lcore_function_t *)lcore_receive, (lcore_function_t *)lcore_receive};
    unsigned cores[4], index = 0, core, stages = split ? 4 : 3;
    RTE_LCORE_FOREACH_WORKER(core) cores[index++] = core;
    host.epoch_cycles = rte_get_timer_cycles();
    if (split) host.schedulers_running = 2;
    for (unsigned i = 0; i < stages; ++i) {
        struct lcore_parameters *parameters = rte_zmalloc(NULL, sizeof(*parameters), 0);
        if (!parameters) rte_exit(EXIT_FAILURE, "cannot allocate lcore parameters\n");
        *parameters = (struct lcore_parameters){.worker_id = i, .receive_worker_ring = rx_ring,
            .worker_transmit_ring = tx_ring, .stats = stats, .num_ports = host.port_count, .split = split,
            .owner_index = (uint16_t)(i == 2), .owner_rings = {rx_ring, rx_ring1}};
        if (split && i == 2) parameters->receive_worker_ring = rx_ring1;
        if (rte_eal_remote_launch(functions[i], parameters, cores[i]) != 0) rte_exit(EXIT_FAILURE, "cannot launch lcore\n");
    }
    uint64_t hz = rte_get_timer_hz(), start = rte_get_timer_cycles(), serial = 0, generated = 0;
    uint64_t window_start = start + (uint64_t)(warmup * hz), window_end = window_start + (uint64_t)(seconds * hz);
    uint64_t sunk[2] = {0}, window[2] = {0}, recycled = 0;
    for (uint64_t now = start; now < window_end; now = rte_get_timer_cycles()) {
        generated += top_up(pool, (uint16_t)bytes, &serial);
        for (unsigned p = 0; p < 2; ++p) {
            struct rte_mbuf *m[BURST];
            unsigned n = rte_ring_dequeue_burst(sink[p], (void **)m, BURST, NULL);
            sunk[p] += n;
            if (now >= window_start) window[p] += n;
            /* A frame that left port p re-enters port p; RX rewrites its metadata. */
            unsigned back = rte_ring_enqueue_burst(source[p], (void **)m, n, NULL);
            recycled += back;
            for (unsigned i = back; i < n; ++i) rte_pktmbuf_free(m[i]);
        }
    }
    quit_signal_receive = 1;
    for (;;) {
        bool running = false;
        RTE_LCORE_FOREACH_WORKER(core) running |= rte_eal_get_lcore_state(core) != WAIT;
        for (unsigned p = 0; p < 2; ++p) {
            struct rte_mbuf *m[BURST];
            unsigned n = rte_ring_dequeue_burst(sink[p], (void **)m, BURST, NULL);
            sunk[p] += n;
            rte_pktmbuf_free_bulk(m, n);
        }
        if (!running && rte_ring_empty(sink[0]) && rte_ring_empty(sink[1])) break;
    }
    rte_eal_mp_wait_lcore();
    for (unsigned k = 0; split && k < 2; ++k) worker_stats_merge(&stats->worker, &stats->owners[k], (int)k);
    uint64_t unread = 0;
    for (unsigned p = 0; p < 2; ++p) {
        struct rte_mbuf *m;
        while (rte_ring_dequeue(source[p], (void **)&m) == 0) { unread++; rte_pktmbuf_free(m); }
    }
    struct pipeline_stats *s = stats;
    uint64_t drops = s->rx.rx_ring_drops + s->worker.emulator_drops + s->worker.invalid_drops +
        s->worker.storage_drops + s->worker.tx_ring_drops + s->worker.shutdown_drops +
        s->tx.tx_drops + s->tx.trace_outage_drops;
    bool conserved = s->rx.received == drops + s->tx.transmitted &&
        s->tx.transmitted == sunk[0] + sunk[1] && generated + recycled == s->rx.received + unread;
    double pps = (double)(window[0] + window[1]) / seconds;
    printf("ring_rate layout=%s mbuf_bytes=%u population=%u window_s=%.3f "
        "egress0_pps=%.0f egress1_pps=%.0f total_pps=%.0f per_direction_mean_pps=%.0f\n",
        split ? "5-lcore-per-ingress-port-schedulers" : "4-lcore", bytes, population, seconds, window[0] / seconds, window[1] / seconds, pps, pps / 2);
    printf("ring_rate rx=%"PRIu64" tx=%"PRIu64" q_drop=%"PRIu64" other_drops=%"PRIu64
        " retries=%"PRIu64" pending_peak=%"PRIu64" conservation=%s status=%s\n",
        s->rx.received, s->tx.transmitted, s->worker.emulator_drops, drops - s->worker.emulator_drops,
        s->tx.retries, s->tx.pending_peak, conserved ? "PASS" : "FAIL", host.invalid ? "INVALID" : "DRAINED");
    bool ok = conserved && !host.invalid;
    host_cleanup();
    for (uint16_t p = 0; p < 2; ++p) close_port(p);
    if (rte_mempool_avail_count(pool) != population) { ok = false; puts("ring_rate mbufs_reclaimed=FAIL"); }
    rte_mempool_free(pool);
    for (unsigned p = 0; p < 2; ++p) { rte_ring_free(source[p]); rte_ring_free(sink[p]); }
    rte_ring_free(rx_ring); rte_ring_free(rx_ring1); rte_ring_free(tx_ring); rte_free(stats);
    rte_eal_cleanup();
    return ok ? EXIT_SUCCESS : EXIT_FAILURE;
}
