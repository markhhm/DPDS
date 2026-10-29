/* SPDX-License-Identifier: Apache-2.0 */
/*
 * Scheduler-owner microbenchmark: time per packet of the production
 * worker_step_at() (classify, admit, merge, dequeue) with real mbufs and a
 * fixture clock. Arrivals are back-to-back 1518-byte frames, one per gap
 * (-g, default 123.36 ns = 100 Gbit/s), alternating cable and LEO VLANs.
 *   single: one owner of all four lines on one lcore; input alternates ports.
 *   split:  one owner per ingress port, on two lcores at the same time.
 * Each owner generates its own input untimed and recycles its own mbufs.
 */
#define _GNU_SOURCE
#include <getopt.h>
#include <sched.h>
#include <sys/mman.h>
#include <rte_memory.h>
#include <inttypes.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <rte_cycles.h>
#include <rte_eal.h>
#include <rte_launch.h>
#include "../core/worker.h"

#define FRAME 1518
#define SAMPLE_STEPS 64

static uint64_t packets_per_port = 2000000, warmup_ns = 5000000, gap_ps = 123360;
static _Atomic unsigned ready;

struct owner {
    int port;                 /* -1: all lines (single owner) */
    struct rte_mbuf **free_list;
    unsigned free_count;
    double *samples;
    size_t count, capacity;
    uint64_t emitted, in_flight_peak;
    int cpu;
    struct worker_stats stats;
};

static int compare(const void *a, const void *b)
{
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

static double percentile(double *v, size_t n, double p)
{
    qsort(v, n, sizeof(*v), compare);
    size_t rank = (size_t)(p / 100.0 * (double)n + 0.999999);
    return v[(rank ? rank : 1) - 1];
}

struct direction { uint64_t seq, last; };

/* Next burst for one port; arrival i is at i * gap. */
static unsigned burst(struct owner *o, unsigned port, struct direction *d, struct rte_mbuf **m)
{
    if (o->free_count < MAX_BURST_SIZE) rte_exit(EXIT_FAILURE, "mbuf population exhausted: raise -m\n");
    for (unsigned i = 0; i < MAX_BURST_SIZE; ++i) {
        struct rte_mbuf *b = m[i] = o->free_list[--o->free_count];
        rte_pktmbuf_mtod(b, uint8_t *)[15] = (uint8_t)(100 + d->seq % 2);
        b->port = (uint16_t)port;
        d->last = (uint64_t)((__uint128_t)d->seq * gap_ps / 1000);
        *packet_meta(b) = (struct packet_meta){.arrival_ns = d->last, .ingress_seq = d->seq++, .ingress_port = (uint16_t)port};
    }
    return MAX_BURST_SIZE;
}

/* Ask for transparent huge pages on the --no-huge EAL memory (a process hint, no system change). */
static int advise_huge(const struct rte_memseg_list *msl, void *arg)
{
    (void)arg;
    if (msl->base_va && msl->len) madvise(msl->base_va, msl->len, MADV_HUGEPAGE);
    return 0;
}

static int owner_main(void *arg)
{
    struct owner *o = arg;
    o->cpu = sched_getcpu();
    struct worker_state w;
    worker_state_init_owner(&w, &o->stats, o->port);
    struct direction dir[2] = {{0, 0}, {0, 0}};
    uint64_t consumed = 0, cycles = 0, admitted = 0, steps = 0;
    double ns_per_cycle = 1e9 / (double)rte_get_tsc_hz();
    unsigned population = o->free_count;
    struct rte_mbuf *in[MAX_BURST_SIZE], *out[MAX_BURST_SIZE];
    atomic_fetch_add(&ready, 1);
    while (atomic_load(&ready) < (o->port < 0 ? 1u : 2u)) rte_pause();
    while (o->port < 0 ? dir[0].seq < packets_per_port || dir[1].seq < packets_per_port
                       : dir[o->port].seq < packets_per_port) {
        for (unsigned p = 0; p < 2; ++p) {
            if (o->port >= 0 && (int)p != o->port) continue;
            unsigned n = burst(o, p, dir + p, in);
            consumed += n;
            for (unsigned q = 0; q < 2; ++q)
                if (o->port < 0 || (int)q == o->port) host_publish((uint16_t)q, consumed, dir[q].last);
            uint64_t start = rte_rdtsc_precise();
            unsigned e = worker_step_at(&w, in, (uint16_t)n, out, dir[p].last);
            cycles += rte_rdtsc_precise() - start;
            admitted += n;
            o->emitted += e;
            for (unsigned i = 0; i < e; ++i) o->free_list[o->free_count++] = out[i];
            if (population - o->free_count > o->in_flight_peak) o->in_flight_peak = population - o->free_count;
            if (++steps % SAMPLE_STEPS == 0) {
                if (dir[p].last >= warmup_ns && o->count < o->capacity)
                    o->samples[o->count++] = (double)cycles * ns_per_cycle / (double)admitted;
                cycles = admitted = 0;
            }
        }
    }
    worker_end_input(&w);
    do {
        unsigned e = worker_step_at(&w, NULL, 0, out, UINT64_MAX / 2);
        for (unsigned i = 0; i < e; ++i) o->free_list[o->free_count++] = out[i];
    } while (worker_has_backlog(&w));
    worker_state_cleanup(&w);
    return 0;
}

int main(int argc, char **argv)
{
    int eal = rte_eal_init(argc, argv);
    if (eal < 0) rte_exit(EXIT_FAILURE, "Invalid EAL arguments\n");
    argc -= eal; argv += eal;
    const char *config = "sched_bench.json", *layouts = "both";
    unsigned population = 32767, repeat = 3;
    int option;
    optind = 1;
    while ((option = getopt(argc, argv, "c:n:w:m:r:g:l:")) != -1) {
        if (option == 'c') config = optarg;
        else if (option == 'n') packets_per_port = strtoull(optarg, NULL, 10);
        else if (option == 'w') warmup_ns = strtoull(optarg, NULL, 10);
        else if (option == 'm') population = (unsigned)strtoul(optarg, NULL, 10);
        else if (option == 'r') repeat = (unsigned)strtoul(optarg, NULL, 10);
        else if (option == 'g') gap_ps = strtoull(optarg, NULL, 10);
        else if (option == 'l') layouts = optarg;
        else rte_exit(EXIT_FAILURE, "usage: hng-sched-bench EAL_ARGS(3 lcores) -- [-c config] [-n packets_per_port]"
            " [-w warmup_ns] [-m mbufs] [-g gap_ps] [-r repeat] [-l single|split|both]\n");
    }
    if (rte_lcore_count() != 3) rte_exit(EXIT_FAILURE, "hng-sched-bench needs 3 lcores: main and two owners\n");
    if (getenv("HNG_BENCH_THP")) rte_memseg_list_walk(advise_huge, NULL);
    if (!gap_ps || population < 4 * MAX_BURST_SIZE) rte_exit(EXIT_FAILURE, "invalid -g or -m\n");
    /* Objects may straddle 4 KiB pages under --no-huge, so the stride matches a hugepage pool. */
    struct rte_mempool *pool = rte_mempool_create_empty("bench_pool", population,
        sizeof(struct rte_mbuf) + RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN) + RTE_MBUF_DEFAULT_BUF_SIZE,
        0, sizeof(struct rte_pktmbuf_pool_private), rte_socket_id(), RTE_MEMPOOL_F_NO_IOVA_CONTIG);
    struct rte_pktmbuf_pool_private priv = {.mbuf_data_room_size = RTE_MBUF_DEFAULT_BUF_SIZE,
        .mbuf_priv_size = RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN)};
    if (!pool || rte_mempool_set_ops_byname(pool, "ring_mp_mc", NULL) != 0)
        rte_exit(EXIT_FAILURE, "cannot create a pool of %u mbufs\n", population);
    rte_pktmbuf_pool_init(pool, &priv);
    if (rte_mempool_populate_default(pool) < 0) rte_exit(EXIT_FAILURE, "cannot populate %u mbufs: raise -m\n", population);
    rte_mempool_obj_iter(pool, rte_pktmbuf_init, NULL);
    struct rte_mbuf **all = calloc(population, sizeof(*all));
    if (!all) rte_exit(EXIT_FAILURE, "out of memory\n");
    for (unsigned i = 0; i < population; ++i) {
        struct rte_mbuf *m = rte_pktmbuf_alloc(pool);
        uint8_t *d = m ? (uint8_t *)rte_pktmbuf_append(m, FRAME) : NULL;
        if (!d) rte_exit(EXIT_FAILURE, "cannot build frames\n");
        memset(d, 0, FRAME); d[12] = 0x81; d[16] = 0x08;
        all[i] = m;
    }
    char model[128] = "unknown", line[256];
    FILE *cpu = fopen("/proc/cpuinfo", "r");
    while (cpu && fgets(line, sizeof(line), cpu))
        if (!strncmp(line, "model name", 10)) { snprintf(model, sizeof(model), "%s", strchr(line, ':') + 2); model[strcspn(model, "\n")] = 0; break; }
    if (cpu) fclose(cpu);
    unsigned cores[2], index = 0, core;
    RTE_LCORE_FOREACH_WORKER(core) cores[index++] = core;
    printf("cpu_model=\"%s\" timer=rdtsc tsc_hz=%"PRIu64" (DPDK estimate) config=%s packets_per_port=%"PRIu64
        " gap_ps=%"PRIu64" warmup_ns=%"PRIu64" mbufs=%u\n", model, rte_get_tsc_hz(), config,
        packets_per_port, gap_ps, warmup_ns, population);
    static struct owner owners[2];
    for (unsigned r = 0; r < repeat; ++r) {
        for (int split = 0; split < 2; ++split) {
            if ((split && !strcmp(layouts, "single")) || (!split && !strcmp(layouts, "split"))) continue;
            if (!host_load(config)) rte_exit(EXIT_FAILURE, "configuration rejected\n");
            if (split && !host_split_valid()) rte_exit(EXIT_FAILURE, "configuration cannot be split\n");
            unsigned n = split ? 2 : 1;
            for (unsigned k = 0; k < n; ++k) {
                struct owner *o = owners + k;
                memset(o, 0, sizeof(*o));
                o->port = split ? (int)k : -1;
                o->free_list = all + k * (population / n);
                o->free_count = population / n;
                o->capacity = 2 * packets_per_port / MAX_BURST_SIZE / SAMPLE_STEPS + 2;
                o->samples = calloc(o->capacity, sizeof(double));
                if (!o->samples) rte_exit(EXIT_FAILURE, "out of memory\n");
            }
            atomic_store(&ready, 0);
            host_mark_launched();
            for (unsigned k = 0; k < n; ++k)
                if (rte_eal_remote_launch(owner_main, owners + k, cores[k]) != 0) rte_exit(EXIT_FAILURE, "launch failed\n");
            rte_eal_mp_wait_lcore();
            for (unsigned k = 0; k < n; ++k) {
                struct owner *o = owners + k;
                if (o->stats.invalid_drops || o->stats.storage_drops || o->stats.shutdown_drops || o->stats.emulator_drops)
                    rte_exit(EXIT_FAILURE, "benchmark run dropped packets\n");
                if (!o->count) rte_exit(EXIT_FAILURE, "no samples: raise -n or lower -w\n");
                printf("sched run=%u layout=%s owner=%s cpu=%d samples=%zu emitted=%"PRIu64" in_flight_peak=%"PRIu64
                    " ns_per_packet_p50=%.2f p99=%.2f\n", r, split ? "per-ingress-port-concurrent" : "single",
                    split ? (k ? "port1" : "port0") : "all-lines", o->cpu, o->count, o->emitted, o->in_flight_peak,
                    percentile(o->samples, o->count, 50), percentile(o->samples, o->count, 99));
                free(o->samples);
            }
            if (host.invalid) rte_exit(EXIT_FAILURE, "benchmark run invalid\n");
            host_cleanup();
        }
    }
    for (unsigned i = 0; i < population; ++i) rte_pktmbuf_free(all[i]);
    bool reclaimed = rte_mempool_avail_count(pool) == population;
    rte_mempool_free(pool);
    free(all);
    rte_eal_cleanup();
    if (!reclaimed) { puts("sched mbufs_reclaimed=FAIL"); return EXIT_FAILURE; }
    return EXIT_SUCCESS;
}
