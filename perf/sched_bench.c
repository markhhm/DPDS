/* SPDX-License-Identifier: Apache-2.0 */
/*
 * Scheduler-owner microbenchmark: time per packet of the production
 * worker_step_at() (classify, admit, merge, dequeue) on one thread, with real
 * mbufs and a fixture clock. Arrivals are back-to-back 1518-byte frames at
 * 100 Gbit/s per direction, alternating cable and LEO VLANs.
 *   single: one owner of all four lines; input alternates ports per burst.
 *   split:  one owner per ingress port; each is timed separately.
 * Owners run one after the other on the same core, so the split numbers
 * exclude cross-core effects such as cache-line sharing on the frontier.
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <rte_cycles.h>
#include <rte_eal.h>
#include "../core/worker.h"

#define FRAME 1518
#define SAMPLE_STEPS 64

static struct rte_mbuf **free_list;
static unsigned free_count;

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

static void recycle(struct rte_mbuf **m, unsigned n)
{
    for (unsigned i = 0; i < n; ++i) free_list[free_count++] = m[i];
}

struct direction { uint64_t seq, arrival_num; };

/* Next burst for one port: exact arrivals at 100 Gbit/s for 1542 wire bytes. */
static unsigned burst(unsigned port, struct direction *d, struct rte_mbuf **m, uint64_t *last)
{
    if (free_count < MAX_BURST_SIZE) rte_exit(EXIT_FAILURE, "mbuf population exhausted\n");
    for (unsigned i = 0; i < MAX_BURST_SIZE; ++i) {
        struct rte_mbuf *b = m[i] = free_list[--free_count];
        uint8_t *data = rte_pktmbuf_mtod(b, uint8_t *);
        data[15] = (uint8_t)(100 + d->seq % 2);
        b->port = (uint16_t)port;
        *last = d->arrival_num++ * 12336 / 100; /* 1542 * 8 bits / 100 Gbit/s = 123.36 ns */
        *packet_meta(b) = (struct packet_meta){.arrival_ns = *last, .ingress_seq = d->seq++, .ingress_port = (uint16_t)port};
    }
    return MAX_BURST_SIZE;
}

struct result { double *samples; size_t count; uint64_t packets, emitted; };

static void run(bool split, uint64_t packets, uint64_t warmup_ns, struct result out[2])
{
    if (!host_load("sched_bench.json")) rte_exit(EXIT_FAILURE, "configuration rejected\n");
    if (split && !host_split_valid()) rte_exit(EXIT_FAILURE, "configuration cannot be split by ingress port\n");
    static struct worker_stats stats[2];
    memset(stats, 0, sizeof(stats));
    struct worker_state w[2];
    unsigned owners = split ? 2 : 1;
    for (unsigned k = 0; k < owners; ++k) worker_state_init_owner(w + k, stats + k, split ? (int)k : -1);
    struct direction dir[2] = {{0, 0}, {0, 0}};
    uint64_t consumed[2] = {0}, last[2] = {0}, cycles[2] = {0}, admitted[2] = {0};
    unsigned steps[2] = {0};
    size_t capacity = packets / MAX_BURST_SIZE / SAMPLE_STEPS + 2;
    for (unsigned k = 0; k < 2; ++k) {
        out[k] = (struct result){calloc(capacity, sizeof(double)), 0, 0, 0};
        if (!out[k].samples) rte_exit(EXIT_FAILURE, "out of memory\n");
    }
    double ns_per_cycle = 1e9 / (double)rte_get_tsc_hz();
    struct rte_mbuf *in[MAX_BURST_SIZE], *emitted[MAX_BURST_SIZE];
    while (dir[0].seq + dir[1].seq < packets) {
        for (unsigned p = 0; p < 2; ++p) {
            unsigned k = split ? p : 0, n = burst(p, dir + p, in, last + p);
            consumed[k] += n;
            for (unsigned q = 0; q < 2; ++q)
                if (!split || q == p) host_publish((uint16_t)q, consumed[k], last[q]);
            uint64_t start = rte_rdtsc_precise();
            unsigned e = worker_step_at(w + k, in, n, emitted, last[p]);
            cycles[k] += rte_rdtsc_precise() - start;
            admitted[k] += n;
            out[k].emitted += e;
            recycle(emitted, e);
            if (++steps[k] % SAMPLE_STEPS == 0) {
                if (last[p] >= warmup_ns && out[k].count < capacity)
                    out[k].samples[out[k].count++] = (double)cycles[k] * ns_per_cycle / (double)admitted[k];
                out[k].packets += admitted[k];
                cycles[k] = admitted[k] = 0;
            }
        }
    }
    for (unsigned k = 0; k < owners; ++k) worker_end_input(w + k);
    for (bool backlog = true; backlog;) {
        backlog = false;
        for (unsigned k = 0; k < owners; ++k) {
            recycle(emitted, worker_step_at(w + k, NULL, 0, emitted, UINT64_MAX / 2));
            backlog |= worker_has_backlog(w + k);
        }
    }
    for (unsigned k = 0; k < owners; ++k) {
        worker_state_cleanup(w + k);
        if (stats[k].invalid_drops || stats[k].storage_drops || stats[k].shutdown_drops)
            rte_exit(EXIT_FAILURE, "benchmark run dropped packets outside the model\n");
    }
    if (host.invalid) rte_exit(EXIT_FAILURE, "benchmark run invalid\n");
    host_cleanup();
}

int main(int argc, char **argv)
{
    int eal = rte_eal_init(argc, argv);
    if (eal < 0) rte_exit(EXIT_FAILURE, "Invalid EAL arguments\n");
    argc -= eal; argv += eal;
    uint64_t packets = 4000000, warmup_ns = 5000000;
    unsigned population = 32767, repeat = 3;
    int option;
    optind = 1;
    while ((option = getopt(argc, argv, "n:w:m:r:")) != -1) {
        if (option == 'n') packets = strtoull(optarg, NULL, 10);
        else if (option == 'w') warmup_ns = strtoull(optarg, NULL, 10);
        else if (option == 'm') population = (unsigned)strtoul(optarg, NULL, 10);
        else if (option == 'r') repeat = (unsigned)strtoul(optarg, NULL, 10);
        else rte_exit(EXIT_FAILURE, "usage: hng-sched-bench EAL_ARGS -- [-n packets] [-w warmup_ns] [-m mbufs] [-r repeat]\n");
    }
    struct rte_mempool *pool = rte_pktmbuf_pool_create("bench_pool", population, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    free_list = calloc(population, sizeof(*free_list));
    if (!pool || !free_list) rte_exit(EXIT_FAILURE, "cannot allocate %u mbufs\n", population);
    for (unsigned i = 0; i < population; ++i) {
        struct rte_mbuf *m = rte_pktmbuf_alloc(pool);
        uint8_t *d = m ? (uint8_t *)rte_pktmbuf_append(m, FRAME) : NULL;
        if (!d) rte_exit(EXIT_FAILURE, "cannot build frames\n");
        memset(d, 0, FRAME); d[12] = 0x81; d[16] = 0x08;
        free_list[free_count++] = m;
    }
    char model[128] = "unknown", line[256];
    FILE *cpu = fopen("/proc/cpuinfo", "r");
    while (cpu && fgets(line, sizeof(line), cpu))
        if (!strncmp(line, "model name", 10)) { snprintf(model, sizeof(model), "%s", strchr(line, ':') + 2); model[strcspn(model, "\n")] = 0; break; }
    if (cpu) fclose(cpu);
    printf("cpu_model=\"%s\" timer=rdtsc tsc_hz=%"PRIu64" (DPDK estimate) lcore=%u packets=%"PRIu64" warmup_ns=%"PRIu64"\n",
        model, rte_get_tsc_hz(), rte_lcore_id(), packets, warmup_ns);
    for (unsigned r = 0; r < repeat; ++r) {
        for (int split = 0; split < 2; ++split) {
            struct result res[2];
            run(split, packets, warmup_ns, res);
            for (unsigned k = 0; k < (split ? 2u : 1u); ++k) {
                if (!res[k].count) rte_exit(EXIT_FAILURE, "no samples: raise -n or lower -w\n");
                printf("sched run=%u layout=%s owner=%s samples=%zu packets=%"PRIu64" emitted=%"PRIu64
                    " ns_per_packet_p50=%.2f p99=%.2f\n", r, split ? "per-ingress-port" : "single",
                    split ? (k ? "port1" : "port0") : "all-lines", res[k].count, res[k].packets, res[k].emitted,
                    percentile(res[k].samples, res[k].count, 50), percentile(res[k].samples, res[k].count, 99));
                free(res[k].samples);
            }
            if (!split) free(res[1].samples);
        }
    }
    for (unsigned i = 0; i < free_count; ++i) rte_pktmbuf_free(free_list[i]);
    bool reclaimed = rte_mempool_avail_count(pool) == population;
    rte_mempool_free(pool);
    free(free_list);
    rte_eal_cleanup();
    if (!reclaimed) { puts("sched mbufs_reclaimed=FAIL"); return EXIT_FAILURE; }
    return EXIT_SUCCESS;
}
