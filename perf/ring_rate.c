/* SPDX-License-Identifier: Apache-2.0 */
#define _GNU_SOURCE
/*
 * Maximum host packet rate over net_ring ports. The production RX, scheduler
 * and TX lcore functions run unchanged on companion rings. The main lcore is a
 * closed-loop generator: it recycles every frame that leaves an egress ring
 * into the ingress ring it came from and tops the population up after drops.
 * Each direction has its own pool, so its frames stay in that direction.
 * The frames that can reach one TX handoff ring never exceed it, because the
 * production scheduler invalidates the run instead of waiting for TX handoff
 * space: up to ring-1 in total with one shared TX ring, up to ring-1 per
 * direction with one TX ring per direction (7 lcores).
 * Software rings only: the result is not evidence of NIC or 100G performance.
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <assert.h>
#include <fcntl.h>
#include <sched.h>
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
    "usage: hng-ring-rate EAL_ARGS -- [-c config.json] [-b mbuf_bytes] [-d seconds] [-w warmup_s] [-n population] [-o binary_file] [-r pps_per_direction] [-a observer_cpu] [-g generator_cpu] [-D]\n";

static struct rte_ring *source[2], *sink[2];
static uint64_t pace_start, pace_hz, paced_sent[2];
static unsigned pace_pps;

static void frame(struct rte_mbuf *m, unsigned port, uint16_t bytes, uint64_t n)
{
    uint8_t *d = (uint8_t *)rte_pktmbuf_append(m, bytes);
    if (!d) rte_exit(EXIT_FAILURE, "mbuf too small for %u bytes\n", bytes);
    memset(d, 0, bytes);
    memset(d, 0xff, 6);
    d[6] = 2; d[7] = (uint8_t)port;
    memcpy(d + 18, &n, sizeof(n));
    d[12] = 0x81; d[13] = 0x00; d[15] = (uint8_t)(100 + n % 2); d[16] = 0x88; d[17] = 0xb5;
}

/* Generate up to `free` frames from each ingress port's pool into its ingress ring. */
static uint64_t top_up(struct rte_mempool *pool[2], uint16_t bytes, uint64_t *serial)
{
    uint64_t made = 0;
    for (unsigned p = 0; p < 2; ++p) {
        struct rte_mbuf *m[BURST];
        unsigned n = rte_ring_free_count(source[p]);
        if (n > BURST) n = BURST;
        if (pace_pps) {
            uint64_t elapsed = rte_get_timer_cycles() - pace_start;
            uint64_t allowance = (elapsed / pace_hz) * pace_pps + (elapsed % pace_hz) * pace_pps / pace_hz;
            uint64_t budget = allowance > paced_sent[p] ? allowance - paced_sent[p] : 0;
            /* Preserve the registered packet rate while presenting RX-sized bursts. */
            if (budget < BURST) continue;
            if (n > budget) n = (unsigned)budget;
        }
        if (!n || rte_pktmbuf_alloc_bulk(pool[p], m, n) != 0) continue;
        for (unsigned i = 0; i < n; ++i) frame(m[i], p, bytes, (*serial)++);
        unsigned sent = rte_ring_enqueue_burst(source[p], (void **)m, n, NULL);
        for (unsigned i = sent; i < n; ++i) rte_pktmbuf_free(m[i]);
        made += sent; paced_sent[p] += sent;
    }
    return made;
}

int main(int argc, char **argv)
{
    int eal = rte_eal_init(argc, argv);
    if (eal < 0) rte_exit(EXIT_FAILURE, "Invalid EAL arguments\n");
    argc -= eal; argv += eal;
    const char *config = "ring_rate.json";
    /* 4 lcores: one scheduler. 5: one scheduler per ingress port. 7: RX, scheduler and TX per direction. */
    bool io_split = rte_lcore_count() == 7, split = rte_lcore_count() == 5 || io_split;
    unsigned limit = (io_split ? 2 : 1) * (SCHED_TX_RING_SZ - 1);
    unsigned bytes = 1518, population = limit;
    double seconds = 2.0, warmup = 0.25;
    int option, observer_cpu = -1, generator_cpu = -1;
    const char *observe_path = NULL;
    bool discard = false;
    optind = 1;
    while ((option = getopt(argc, argv, "c:b:d:w:n:o:r:a:g:D")) != -1) {
        if (option == 'D') discard = true;
        else if (option == 'o') observe_path = optarg;
        else if (option == 'r') pace_pps = (unsigned)strtoul(optarg, NULL, 10);
        else if (option == 'a') observer_cpu = (int)strtol(optarg, NULL, 10);
        else if (option == 'g') generator_cpu = (int)strtol(optarg, NULL, 10);
        else if (option == 'c') config = optarg;
        else if (option == 'b') bytes = (unsigned)strtoul(optarg, NULL, 10);
        else if (option == 'd') seconds = strtod(optarg, NULL);
        else if (option == 'w') warmup = strtod(optarg, NULL);
        else if (option == 'n') population = (unsigned)strtoul(optarg, NULL, 10);
        else rte_exit(EXIT_FAILURE, "%s", usage);
    }
    if (observer_cpu >= CPU_SETSIZE || observer_cpu < -1 || generator_cpu >= CPU_SETSIZE || generator_cpu < -1 || pace_pps > 1000000000U)
        rte_exit(EXIT_FAILURE, "%s", usage);
    if (bytes < 60 || bytes > 1518 || seconds <= 0 || warmup < 0 || population < 64 ||
        population > limit)
        rte_exit(EXIT_FAILURE, "%s(population: 64 to %u for this layout)\n", usage, limit);
    if (rte_lcore_count() != 4 && !split) rte_exit(EXIT_FAILURE, "hng-ring-rate needs 4, 5 or 7 lcores\n");
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
    /* Recycling into the source port needs every line to cross: ingress q -> egress 1-q. */
    for (size_t i = 0; i < host.line_count; ++i)
        if (host.lines[i].ingress_port > 1 || host.lines[i].egress_port != 1 - host.lines[i].ingress_port)
            rte_exit(EXIT_FAILURE, "hng-ring-rate needs every line to go from port q to port 1-q\n");
    struct rte_mempool *pool[2];
    unsigned share[2] = {population - population / 2, population / 2};
    for (unsigned p = 0; p < 2; ++p) {
        char name[32];
        snprintf(name, sizeof(name), "rate_pool%u", p);
        pool[p] = rte_pktmbuf_pool_create(name, share[p], 0,
            RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
        if (!pool[p]) rte_exit(EXIT_FAILURE, "cannot allocate %u mbufs: %s\n", share[p], rte_strerror(rte_errno));
    }
    for (uint16_t p = 0; p < 2; ++p)
        if (host_prepare_port(p) != 0 || initialize_port(p, pool[p]) != 0) rte_exit(EXIT_FAILURE, "port %u setup failed\n", p);
    struct pipeline_stats *stats = rte_zmalloc("rate_stats", sizeof(*stats), 64);
    if (split && !host_split_valid()) rte_exit(EXIT_FAILURE, "split needs each egress fed by one ingress port\n");
    struct rte_ring *rx_ring = rte_ring_create("rate_rx", SCHED_RX_RING_SZ, rte_socket_id(), RING_F_SC_DEQ | RING_F_SP_ENQ);
    struct rte_ring *rx_ring1 = rte_ring_create("rate_rx1", SCHED_RX_RING_SZ, rte_socket_id(), RING_F_SC_DEQ | RING_F_SP_ENQ);
    struct rte_ring *tx_ring = rte_ring_create("rate_tx", SCHED_TX_RING_SZ, rte_socket_id(),
        RING_F_SC_DEQ | RING_F_SP_ENQ);
    struct rte_ring *tx_ring1 = rte_ring_create("rate_tx1", SCHED_TX_RING_SZ, rte_socket_id(), RING_F_SC_DEQ | RING_F_SP_ENQ);
    if (!stats || !rx_ring || !rx_ring1 || !tx_ring || !tx_ring1) rte_exit(EXIT_FAILURE, "cannot allocate pipeline state\n");
    lcore_function_t *tx = (lcore_function_t *)lcore_transmit, *wk = (lcore_function_t *)lcore_worker,
        *rx = (lcore_function_t *)lcore_receive;
    lcore_function_t *functions[6] = {tx, wk, split ? wk : rx, rx};
    if (io_split) memcpy(functions, (lcore_function_t *[6]){tx, tx, wk, wk, rx, rx}, sizeof(functions));
    unsigned cores[6], index = 0, core, stages = io_split ? 6 : split ? 4 : 3;
    RTE_LCORE_FOREACH_WORKER(core) cores[index++] = core;
    host.epoch_cycles = rte_get_timer_cycles();
    if (split) host.schedulers_running = 2;
    if (io_split) host.receivers_running = 2;
    int observe_fd = -1;
    if (observe_path) {
        observe_fd = open(observe_path, discard ? O_WRONLY | O_CLOEXEC :
            O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
        if (observe_fd < 0) rte_exit(EXIT_FAILURE, "cannot create binary capture: %s\n", strerror(errno));
    }
    if (observe_fd >= 0) {
        bool prepared = discard ? observer_open_binary_discard(observe_fd, OBS_ID_SOFTWARE_L2, 9000, 65536, io_split) :
            observer_open_binary(observe_fd, OBS_ID_SOFTWARE_L2, 9000, 65536, io_split);
        if (!prepared)
            rte_exit(EXIT_FAILURE, "cannot open binary observer\n");
        close(observe_fd);
        for (unsigned p = 0; p < 2; ++p) {
            struct rte_eth_stats nic;
            if (rte_eth_stats_get(p, &nic)) rte_exit(EXIT_FAILURE, "cannot read NIC stats\n");
            observer_resources(p, false, pool[p]->size, rte_mempool_avail_count(pool[p]), &nic);
        }
        cpu_set_t saved, observer_set;
        if (sched_getaffinity(0, sizeof(saved), &saved)) rte_exit(EXIT_FAILURE, "cannot set/read CPU affinity\n");
        if (observer_cpu >= 0) {
            CPU_ZERO(&observer_set); CPU_SET(observer_cpu, &observer_set);
            if (sched_setaffinity(0, sizeof(observer_set), &observer_set)) rte_exit(EXIT_FAILURE, "cannot set/read CPU affinity\n");
        }
        if (!observer_start()) rte_exit(EXIT_FAILURE, "cannot start observer\n");
        if (sched_setaffinity(0, sizeof(saved), &saved)) rte_exit(EXIT_FAILURE, "cannot set/read CPU affinity\n");
        printf("observer mode=binary capacity=65536 consumer_cpu=%d\n", observer_cpu);
    }
    host_mark_launched();
    for (unsigned i = 0; i < stages; ++i) {
        struct lcore_parameters *parameters = rte_zmalloc(NULL, sizeof(*parameters), 0);
        if (!parameters) rte_exit(EXIT_FAILURE, "cannot allocate lcore parameters\n");
        *parameters = (struct lcore_parameters){.worker_id = i, .receive_worker_ring = rx_ring,
            .worker_transmit_ring = tx_ring, .stats = stats, .num_ports = host.port_count, .split = split,
            .owner_index = (uint16_t)(i == 2), .owner_rings = {rx_ring, rx_ring1}};
        if (split && i == 2) parameters->receive_worker_ring = rx_ring1;
        if (split && !io_split) {
            parameters->tx_owner_rings[0] = tx_ring;
            parameters->tx_owner_rings[1] = tx_ring1;
            if (i == 1 || i == 2) parameters->worker_transmit_ring = i == 1 ? tx_ring : tx_ring1;
        }
        if (io_split) {
            /* Pairs per direction k, as in dpds: TX k, scheduler k, RX k. */
            unsigned k = i % 2;
            parameters->io_split = true;
            parameters->owner_index = (uint16_t)k;
            parameters->receive_worker_ring = k ? rx_ring1 : rx_ring;
            parameters->worker_transmit_ring = k ? tx_ring1 : tx_ring;
        }
        if (rte_eal_remote_launch(functions[i], parameters, cores[i]) != 0) rte_exit(EXIT_FAILURE, "cannot launch lcore\n");
    }
    cpu_set_t main_set, generator_set;
    if (sched_getaffinity(0, sizeof(main_set), &main_set)) rte_exit(EXIT_FAILURE, "cannot set/read CPU affinity\n");
    if (generator_cpu >= 0) {
        CPU_ZERO(&generator_set); CPU_SET(generator_cpu, &generator_set);
        if (sched_setaffinity(0, sizeof(generator_set), &generator_set)) rte_exit(EXIT_FAILURE, "cannot set/read CPU affinity\n");
    }
    uint64_t hz = rte_get_timer_hz(), start = rte_get_timer_cycles(), serial = 0, generated = 0;
    pace_start = start; pace_hz = hz;
    uint64_t window_start = start + (uint64_t)(warmup * hz), window_end = window_start + (uint64_t)(seconds * hz);
    uint64_t sunk[2] = {0}, window[2] = {0}, recycled = 0;
    uint64_t next_health = start + hz;
    for (uint64_t now = start; now < window_end; now = rte_get_timer_cycles()) {
        if (observe_fd >= 0 && now >= next_health) {
            for (unsigned p = 0; p < 4; ++p) {
                struct observer_health h = observer_health(p);
                printf("observer_progress seconds=%.1f producer=%u attempted=%"PRIu64" emitted=%"PRIu64" dropped=%"PRIu64"\n",
                    (double)(now - start) / hz, p, h.records_attempted, h.records_emitted, h.records_dropped);
            }
            next_health += hz;
        }
        generated += top_up(pool, (uint16_t)bytes, &serial);
        for (unsigned p = 0; p < 2; ++p) {
            struct rte_mbuf *m[BURST];
            unsigned n = rte_ring_dequeue_burst(sink[p], (void **)m, BURST, NULL);
            sunk[p] += n;
            if (now >= window_start) window[p] += n;
            if (pace_pps) { rte_pktmbuf_free_bulk(m, n); continue; }
            /* A frame that left egress p came from ingress 1-p and re-enters there; RX rewrites its metadata. */
            for (unsigned j = 0; j < n; ++j) {
                uint64_t identity = serial++;
                memcpy(rte_pktmbuf_mtod(m[j], uint8_t *) + 18, &identity, sizeof(identity));
            }
            unsigned back = rte_ring_enqueue_burst(source[1 - p], (void **)m, n, NULL);
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
    if (sched_setaffinity(0, sizeof(main_set), &main_set)) rte_exit(EXIT_FAILURE, "cannot set/read CPU affinity\n");
    rte_eal_mp_wait_lcore();
    for (unsigned k = 0; split && k < 2; ++k) worker_stats_merge(&stats->worker, &stats->owners[k], (int)k);
    for (unsigned k = 0; io_split && k < 2; ++k) {
        rx_stats_merge(&stats->rx, &stats->rx_owners[k]);
        tx_stats_merge(&stats->tx, &stats->tx_owners[k]);
    }
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
        io_split ? "7-lcore-per-direction-rx-scheduler-tx" : split ? "5-lcore-per-ingress-port-schedulers" : "4-lcore", bytes, population, seconds, window[0] / seconds, window[1] / seconds, pps, pps / 2);
    printf("ring_rate rx=%"PRIu64" tx=%"PRIu64" q_drop=%"PRIu64" other_drops=%"PRIu64
        " retries=%"PRIu64" pending_peak=%"PRIu64" conservation=%s status=%s\n",
        s->rx.received, s->tx.transmitted, s->worker.emulator_drops, drops - s->worker.emulator_drops,
        s->tx.retries, s->tx.pending_peak, conserved ? "PASS" : "FAIL", host.invalid ? "INVALID" : "DRAINED");
    /* Bucket b counts TX calls carrying 2^b to 2^(b+1)-1 frames; whole run, all TX lcores. */
    uint64_t calls = 0;
    printf("ring_rate tx_burst_hist=");
    for (int b = 0; b < BURST_HIST_BUCKETS; ++b) { printf("%s%"PRIu64, b ? "," : "", s->tx.burst_hist[b]); calls += s->tx.burst_hist[b]; }
    printf(" mean_frames_per_call=%.2f\n", calls ? (double)s->tx.transmitted / (double)calls : 0.0);
    bool ok = conserved && !host.invalid;
    if (observe_fd >= 0) {
        for (unsigned p = 0; p < 2; ++p) {
            struct rte_eth_stats nic;
            if (rte_eth_stats_get(p, &nic)) rte_exit(EXIT_FAILURE, "cannot read NIC stats\n");
            observer_resources(p, true, pool[p]->size, rte_mempool_avail_count(pool[p]), &nic);
        }
        bool complete = observer_close(!host.invalid); ok &= complete;
        printf("observer_complete=%u\n", complete);
        struct observer_storage_health storage = observer_storage_health();
        printf("observer_io high_water=%"PRIu64" capacity=%"PRIu64" pending=%"PRIu64" cpu=%d\n",
            storage.blocks_high_water, storage.blocks_capacity, storage.blocks_pending, storage.cpu);
        for (unsigned p = 0; p < OBS_PRODUCERS; ++p) {
            struct observer_health h = observer_health(p);
            assert(h.queue_high_water <= h.queue_capacity);
            assert(h.records_attempted == h.records_emitted + h.records_dropped + h.capture_drops);
            printf("observer producer=%u attempted=%"PRIu64" emitted=%"PRIu64" dropped=%"PRIu64" capture_drops=%"PRIu64" high_water=%"PRIu64" capacity=%"PRIu64" identity_errors=%"PRIu64" malformed=%"PRIu64"\n",
                p,h.records_attempted,h.records_emitted,h.records_dropped,h.capture_drops,h.queue_high_water,h.queue_capacity,h.identity_errors,h.malformed_records);
        }
    }
    host_cleanup();
    for (uint16_t p = 0; p < 2; ++p) close_port(p);
    for (unsigned p = 0; p < 2; ++p) {
        if (rte_mempool_avail_count(pool[p]) != share[p]) { ok = false; puts("ring_rate mbufs_reclaimed=FAIL"); }
        rte_mempool_free(pool[p]);
    }
    for (unsigned p = 0; p < 2; ++p) { rte_ring_free(source[p]); rte_ring_free(sink[p]); }
    rte_ring_free(rx_ring); rte_ring_free(rx_ring1); rte_ring_free(tx_ring); rte_ring_free(tx_ring1); rte_free(stats);
    rte_eal_cleanup();
    return ok ? EXIT_SUCCESS : EXIT_FAILURE;
}
