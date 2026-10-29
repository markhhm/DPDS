/* SPDX-License-Identifier: Apache-2.0 */
/*
 * Pool-size calculator. Without --verify it needs no EAL and prints, per
 * ingress port (one pool on that port's NUMA node), the mbuf objects and bytes
 * the host allocates and the core metadata the engine reserves for the given
 * model. With --verify it also builds that configuration on two net_null
 * ports under the EAL arguments before "--" and compares the prediction with
 * the real allocation.
 */
#define _GNU_SOURCE
#include <getopt.h>
#include <inttypes.h>
#include <malloc.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_malloc.h>
#include <rte_mempool.h>
#include "../common/common.h"

struct params {
    uint64_t c_bps, t_ns, delta_ns, q_ns, min_q, max_q, burst, page;
    unsigned lines, lcores, rx_desc, tx_desc;
    double p[8];
    unsigned p_count;
};

struct plan {
    uint64_t q_bytes, slots_per_line, objects, stride, mempool_bytes, metadata_bytes;
};

static const double GIB = 1024.0 * 1024.0 * 1024.0;

static uint64_t ms_to_ns(const char *s) { return (uint64_t)llround(strtod(s, NULL) * 1e6); }

static size_t mbuf_element_bytes(void)
{
    return sizeof(struct rte_mbuf) + RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN) +
        RTE_MBUF_DEFAULT_BUF_SIZE;
}

/* Mirrors the engine's startup envelope check and host_pool_objects(). */
/* Mempool objects never straddle a page, so each page holds floor(page/stride) objects. */
static uint64_t object_bytes(uint64_t objects, uint64_t stride, uint64_t page)
{
    if (!page || stride > page) return objects * stride;
    uint64_t per_page = page / stride;
    return (objects + per_page - 1) / per_page * page;
}

static struct plan make_plan(const struct params *x)
{
    struct plan r = {0};
    r.q_bytes = (uint64_t)((__uint128_t)x->c_bps * x->q_ns / 8000000000u);
    __uint128_t n = (__uint128_t)x->c_bps * (x->t_ns + x->delta_ns);
    __uint128_t d = (__uint128_t)8000000000u * (x->min_q + 20);
    r.slots_per_line = (uint64_t)(r.q_bytes / x->min_q + x->burst + n / d + (n % d != 0));
    r.objects = x->rx_desc + SCHED_RX_RING_SZ + SCHED_TX_RING_SZ + MBUF_CACHE_SIZE * x->lcores +
        HNG_MAX_LINES * TX_PENDING_PER_LINE + 4 * MAX_BURST_SIZE + x->tx_desc + x->lines * r.slots_per_line;
    struct rte_mempool_objsz size;
    r.stride = rte_mempool_calc_obj_size((uint32_t)mbuf_element_bytes(), 0, &size);
    r.mempool_bytes = object_bytes(r.objects, r.stride, x->page) + rte_ring_get_memsize((unsigned)rte_align64pow2(r.objects + 1)) +
        RTE_MEMPOOL_HEADER_SIZE((struct rte_mempool *)NULL, MBUF_CACHE_SIZE);
    r.metadata_bytes = x->lines * r.slots_per_line * hng_slot_metadata_bytes();
    return r;
}

static void print_plan(const struct params *x, const struct plan *r)
{
    uint64_t wire = x->min_q + 20;
    double pps = (double)x->c_bps / (8.0 * (double)wire);
    printf("inputs c_bps=%"PRIu64" t_ns=%"PRIu64" delta_ns=%"PRIu64" q_ns=%"PRIu64" q_l2_bytes=%"PRIu64
        " min_frame_l2=%"PRIu64" max_frame_l2=%"PRIu64" burst_bound=%"PRIu64" lines_per_port=%u lcores=%u"
        " rx_desc=%u tx_desc=%u page_bytes=%"PRIu64"\n", x->c_bps, x->t_ns, x->delta_ns, x->q_ns, r->q_bytes, x->min_q, x->max_q,
        x->burst, x->lines, x->lcores, x->rx_desc, x->tx_desc, x->page);
    printf("per_packet mbuf_element_bytes=%zu mbuf_stride_bytes=%"PRIu64" core_metadata_bytes_per_slot=%zu"
        " bytes_per_slot=%"PRIu64"\n", mbuf_element_bytes(), r->stride, hng_slot_metadata_bytes(),
        r->stride + hng_slot_metadata_bytes());
    printf("per_numa_node envelope slots_per_line=%"PRIu64" pool_objects=%"PRIu64" mempool_gib=%.3f"
        " core_metadata_gib=%.3f total_gib=%.3f (independent of p: the startup check assumes every packet is late)\n",
        r->slots_per_line, r->objects, r->mempool_bytes / GIB, r->metadata_bytes / GIB,
        (r->mempool_bytes + r->metadata_bytes) / GIB);
    printf("both_nodes envelope total_gib=%.3f\n", 2 * (r->mempool_bytes + r->metadata_bytes) / GIB);
    for (unsigned i = 0; i < x->p_count; ++i) {
        /* Mean occupancy only; not a bound, and the engine does not accept it as an envelope. */
        double mean = x->lines * (pps * ((double)x->t_ns + x->p[i] * (double)x->delta_ns) / 1e9 +
            (double)(r->q_bytes / x->min_q));
        printf("per_numa_node mean_occupancy p=%g packets=%.0f gib=%.3f (estimate, not a sizing bound)\n",
            x->p[i], mean, mean * (double)(r->stride + hng_slot_metadata_bytes()) / GIB);
    }
}

static size_t heap_in_use(void) { struct mallinfo2 m = mallinfo2(); return m.uordblks + m.hblkhd; }

static size_t dpdk_heap_bytes(int socket)
{
    struct rte_malloc_socket_stats s;
    return rte_malloc_get_socket_stats(socket, &s) == 0 ? s.heap_allocsz_bytes : 0;
}

static double error(double predicted, double measured) { return fabs(predicted - measured) / measured; }

static int verify(const struct params *given)
{
    struct params copy = *given, *x = &copy;
    struct plan plan = make_plan(x), *r = &plan;
    char path[] = "/tmp/hng-pool-calc-XXXXXX";
    int fd = mkstemp(path);
    if (fd < 0) return perror("mkstemp"), 1;
    FILE *f = fdopen(fd, "w");
    fprintf(f, "{\"schema\":\"hng-v1\",\"lines\":[");
    for (unsigned port = 0, k = 0; port < 2; ++port)
        for (unsigned line = 0; line < x->lines; ++line, ++k)
            fprintf(f, "%s{\"name\":\"l%u\",\"ingress_port\":%u,\"egress_port\":%u,\"vlan_id\":%u,"
                "\"model\":{\"c_bps\":%"PRIu64",\"q_bytes\":%"PRIu64",\"t_ns\":%"PRIu64",\"delta_ns\":%"PRIu64","
                "\"p\":\"0\",\"seed\":1,\"storage_capacity_packets\":%"PRIu64",\"storage_capacity_bytes\":%"PRIu64"},"
                "\"envelope\":{\"min_q_bytes\":%"PRIu64",\"max_q_bytes\":%"PRIu64",\"burst_bound_packets\":%"PRIu64"},"
                "\"trace\":null}", k ? "," : "", k, port, 1 - port, 100 + line, x->c_bps, r->q_bytes, x->t_ns,
                x->delta_ns, r->slots_per_line, r->slots_per_line * (x->max_q - 4), x->min_q, x->max_q, x->burst);
    fprintf(f, "]}\n");
    fclose(f);
    size_t heap_before = heap_in_use();
    bool loaded = host_load(path);
    size_t heap_after = heap_in_use();
    unlink(path);
    if (!loaded) return fputs("verify: engine rejected the generated configuration\n", stderr), 1;
    size_t reserved = hng_engine_metadata_bytes(host.engine);
    uint16_t port;
    RTE_ETH_FOREACH_DEV(port) host.ports[host.port_count++] = port;
    if (host.port_count != 2) return fputs("verify: needs exactly two ports (net_null)\n", stderr), 1;
    int status = 0;
    double predicted_total = 0, measured_total = 0;
    for (uint16_t i = 0; i < 2; ++i) if (host_prepare_port(i) != 0) return 1;
    for (uint16_t i = 0; i < 2; ++i) {
        unsigned objects = host_pool_objects(i, x->lcores);
        int socket = rte_eth_dev_socket_id(i);
        if (socket < 0) socket = rte_socket_id();
        size_t before = dpdk_heap_bytes(socket);
        char name[32];
        snprintf(name, sizeof(name), "verify_pool_%u", i);
        /* Same arguments as the production pool in dpds.c. */
        struct rte_mempool *pool = rte_pktmbuf_pool_create(name, objects, MBUF_CACHE_SIZE,
            RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, socket);
        if (!pool) return fprintf(stderr, "verify: pool allocation failed: %s\n", rte_strerror(rte_errno)), 1;
        size_t measured = dpdk_heap_bytes(socket) - before;
        const struct rte_memseg_list *msl = rte_mem_virt2memseg_list(pool);
        if (!msl) return fputs("verify: cannot find the pool's page size\n", stderr), 1;
        if (msl->page_sz != x->page) {
            /* Predict with the page size this EAL run really uses (4 KiB under --no-huge). */
            x->page = msl->page_sz;
            plan = make_plan(x);
            printf("verify page_bytes=%"PRIu64" (from the EAL memory segment list)\n", x->page);
        }
        printf("verify port=%u objects_calc=%"PRIu64" objects_host=%u stride_calc=%"PRIu64" stride_real=%u"
            " mempool_bytes_calc=%"PRIu64" mempool_bytes_real=%zu error=%.4f\n", i, r->objects, objects,
            r->stride, pool->header_size + pool->elt_size + pool->trailer_size, r->mempool_bytes, measured,
            error((double)r->mempool_bytes, (double)measured));
        if (objects != r->objects || r->stride != pool->header_size + pool->elt_size + pool->trailer_size)
            status = 1;
        predicted_total += (double)r->mempool_bytes;
        measured_total += (double)measured;
        rte_mempool_free(pool);
    }
    predicted_total += 2.0 * (double)r->metadata_bytes;
    measured_total += (double)(heap_after - heap_before);
    printf("verify core_metadata_bytes_calc=%"PRIu64" core_metadata_reserved=%zu process_heap_growth=%zu error=%.4f\n",
        2 * r->metadata_bytes, reserved, heap_after - heap_before,
        error(2.0 * (double)r->metadata_bytes, (double)(heap_after - heap_before)));
    if (reserved != 2 * r->metadata_bytes) status = 1;
    double e = error(predicted_total, measured_total);
    printf("verify total_bytes_calc=%.0f total_bytes_real=%.0f error=%.4f gate_5pct=%s\n",
        predicted_total, measured_total, e, e <= 0.05 && !status ? "PASS" : "FAIL");
    host_cleanup();
    return e <= 0.05 ? status : 1;
}

int main(int argc, char **argv)
{
    struct params x = {100000000000ull, 142000000, 320000000, 50000000, 1522, 1522, 1024,
        2097152, 1, 4, RX_RING_SIZE, TX_RING_SIZE, {0.01, 0.10}, 2};
    bool do_verify = false, p_given = false;
    static const struct option options[] = {
        {"c-bps", 1, 0, 'c'}, {"t-ms", 1, 0, 't'}, {"delta-ms", 1, 0, 'd'}, {"q-ms", 1, 0, 'q'},
        {"p", 1, 0, 'p'}, {"min-frame", 1, 0, 'm'}, {"max-frame", 1, 0, 'M'}, {"burst", 1, 0, 'b'},
        {"lines-per-port", 1, 0, 'k'}, {"lcores", 1, 0, 'l'}, {"rx-desc", 1, 0, 'r'},
        {"tx-desc", 1, 0, 'x'}, {"page-bytes", 1, 0, 'g'}, {"verify", 0, 0, 'v'}, {"help", 0, 0, 'h'}, {0, 0, 0, 0}};
    /* EAL arguments, if any, come first and end with "--". */
    int split = 0;
    for (int i = 1; i < argc; ++i) if (!strcmp(argv[i], "--")) { split = i; break; }
    int own_argc = split ? argc - split : argc;
    char **own_argv = split ? argv + split : argv;
    int option;
    while ((option = getopt_long(own_argc, own_argv, "", options, NULL)) != -1) {
        switch (option) {
        case 'c': x.c_bps = strtoull(optarg, NULL, 10); break;
        case 't': x.t_ns = ms_to_ns(optarg); break;
        case 'd': x.delta_ns = ms_to_ns(optarg); break;
        case 'q': x.q_ns = ms_to_ns(optarg); break;
        case 'p':
            if (!p_given) x.p_count = 0;
            p_given = true;
            if (x.p_count < 8) x.p[x.p_count++] = strtod(optarg, NULL);
            break;
        case 'm': x.min_q = strtoull(optarg, NULL, 10); break;
        case 'M': x.max_q = strtoull(optarg, NULL, 10); break;
        case 'b': x.burst = strtoull(optarg, NULL, 10); break;
        case 'k': x.lines = (unsigned)strtoul(optarg, NULL, 10); break;
        case 'l': x.lcores = (unsigned)strtoul(optarg, NULL, 10); break;
        case 'r': x.rx_desc = (unsigned)strtoul(optarg, NULL, 10); break;
        case 'x': x.tx_desc = (unsigned)strtoul(optarg, NULL, 10); break;
        case 'g': x.page = strtoull(optarg, NULL, 10); break;
        case 'v': do_verify = true; break;
        default:
            fprintf(stderr, "usage: hng-pool-calc [EAL_ARGS --] [--c-bps C] [--t-ms T] [--delta-ms D]"
                " [--q-ms Q] [--p P]... [--min-frame L2] [--max-frame L2] [--burst N]"
                " [--lines-per-port 1|2] [--lcores N] [--rx-desc N] [--tx-desc N] [--page-bytes B] [--verify]\n");
            return 2;
        }
    }
    if (!x.c_bps || x.min_q < 64 || x.max_q > 1522 || x.min_q > x.max_q || !x.burst || x.lines < 1 ||
        x.lines > 2 || x.lcores < 1 || x.lcores > 5) {
        fputs("invalid parameters: need C > 0, 64 <= min <= max <= 1522, burst > 0, 1-2 lines, 1-5 lcores\n", stderr);
        return 2;
    }
    struct plan r = make_plan(&x);
    print_plan(&x, &r);
    if (!do_verify) return 0;
    if (!split) return fputs("--verify needs EAL arguments before \"--\"\n", stderr), 2;
    if (rte_eal_init(split, argv) < 0) return fputs("EAL initialization failed\n", stderr), 1;
    int status = verify(&x);
    rte_eal_cleanup();
    return status;
}
