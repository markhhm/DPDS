
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#include <unistd.h>
#include <signal.h>
#include <getopt.h>

#include <rte_atomic.h>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_cycles.h>
#include <rte_malloc.h>
#include <rte_debug.h>
#include <rte_prefetch.h>
#include <rte_pause.h>

#include "common/common.h"
#include "common/config.h"
#include "core/receive.h"
#include "core/worker.h"
#include "core/transmit.h"
#include "core/combined.h"

// -------------------------------------------
// Statistics printing (in-place dashboard)
// -------------------------------------------
// Number of fixed output lines: Header(1) + RX(1) + RX hist(1) + Worker(1) + TX(1) + TX hist(1) = 6
// Plus one line per NIC port (dynamic).
#define STATS_FIXED_LINES 6

static int stats_printed_once = 0;

// Print a per-second burst-size histogram derived from the delta of two snapshots.
// Bucket i covers burst sizes [2^i, 2^(i+1)-1]; the last bucket is MAX_BURST_SIZE.
// Also prints the average burst size derived from delta_pkts / total burst count.
static void
print_burst_hist(const char *label,
                 const uint64_t *cur, const uint64_t *prev,
                 uint64_t delta_pkts)
{
    uint64_t total_bursts = 0;
    printf("  %s burst/s:", label);
    for (int i = 0; i < BURST_HIST_BUCKETS; i++)
    {
        uint32_t lo = 1u << i;
        uint32_t hi = (i == BURST_HIST_BUCKETS - 1) ? lo : ((lo << 1) - 1);
        uint64_t delta = cur[i] - prev[i];
        total_bursts += delta;
        if (lo == hi)
            printf(" [%u]:%" PRIu64, lo, delta);
        else
            printf(" [%u-%u]:%" PRIu64, lo, hi, delta);
    }
    if (total_bursts > 0)
        printf(" | avg: %.1f", (double)delta_pkts / (double)total_bursts);
    printf("\033[K\n");
}

static void
print_stats(const struct pipeline_stats *stats,
            const struct pipeline_stats *prev)
{
    const int stats_num_lines = STATS_FIXED_LINES + rte_eth_dev_count_avail();

    // Move cursor up to overwrite previous output (skip on first print).
    if (stats_printed_once)
        printf("\033[%dA", stats_num_lines);
    stats_printed_once = 1;

    // Per-second deltas
    uint64_t rx_pps = stats->rx.received - prev->rx.received;
    uint64_t tx_pps = stats->tx.transmitted - prev->tx.transmitted;

    // \033[K clears to end-of-line so shrinking numbers don't leave artifacts.
    printf("--- Emulator Stats (1s interval) ---\033[K\n");
    printf("RX:     %" PRIu64 " pkt/s | Total: %" PRIu64
           " | Ring drops: %" PRIu64 "\033[K\n",
           rx_pps, stats->rx.received, stats->rx.rx_ring_drops);
    print_burst_hist("RX", stats->rx.burst_hist, prev->rx.burst_hist, rx_pps);
    printf("Worker: recv %" PRIu64 " | sent %" PRIu64
           " | emulator drops: %" PRIu64 " | TX ring drops: %" PRIu64 "\033[K\n",
           stats->worker.received, stats->worker.sent,
           stats->worker.emulator_drops, stats->worker.tx_ring_drops);
    printf("TX:     %" PRIu64 " pkt/s | Total: %" PRIu64
           " | TX drops: %" PRIu64 "\033[K\n",
           tx_pps, stats->tx.transmitted, stats->tx.tx_drops);
    print_burst_hist("TX", stats->tx.burst_hist, prev->tx.burst_hist, tx_pps);

    // NIC hardware stats per port
    uint16_t port_id;
    RTE_ETH_FOREACH_DEV(port_id)
    {
        struct rte_eth_stats eth_stats;
        if (rte_eth_stats_get(port_id, &eth_stats) == 0)
        {
            printf("NIC %u:  imissed=%" PRIu64 " ierr=%" PRIu64
                   " oerr=%" PRIu64 " rx_nombuf=%" PRIu64 "\033[K\n",
                   port_id, eth_stats.imissed, eth_stats.ierrors,
                   eth_stats.oerrors, eth_stats.rx_nombuf);
        }
    }

    fflush(stdout);
}

// -------------------------------------------
// CSV stats export
// -------------------------------------------
static void
write_stats_csv(const char *filename, const struct pipeline_stats *stats)
{
    FILE *f = fopen(filename, "w");
    if (!f)
    {
        fprintf(stderr, "Failed to open stats file '%s'\n", filename);
        return;
    }

    // Header row
    fprintf(f, "rx_received,rx_ring_drops");
    for (int i = 0; i < BURST_HIST_BUCKETS; i++)
    {
        uint32_t lo = 1u << i;
        uint32_t hi = (i == BURST_HIST_BUCKETS - 1) ? lo : ((lo << 1) - 1);
        if (lo == hi)
            fprintf(f, ",rx_burst_%u", lo);
        else
            fprintf(f, ",rx_burst_%u_%u", lo, hi);
    }
    fprintf(f, ",worker_received,worker_sent,worker_emulator_drops,worker_tx_ring_drops");
    fprintf(f, ",tx_transmitted,tx_drops");
    for (int i = 0; i < BURST_HIST_BUCKETS; i++)
    {
        uint32_t lo = 1u << i;
        uint32_t hi = (i == BURST_HIST_BUCKETS - 1) ? lo : ((lo << 1) - 1);
        if (lo == hi)
            fprintf(f, ",tx_burst_%u", lo);
        else
            fprintf(f, ",tx_burst_%u_%u", lo, hi);
    }
    uint16_t port_id;
    RTE_ETH_FOREACH_DEV(port_id)
    fprintf(f, ",nic_%u_imissed,nic_%u_ierrors,nic_%u_oerrors,nic_%u_rx_nombuf",
            port_id, port_id, port_id, port_id);
    fprintf(f, "\n");

    // Values row
    fprintf(f, "%" PRIu64 ",%" PRIu64, stats->rx.received, stats->rx.rx_ring_drops);
    for (int i = 0; i < BURST_HIST_BUCKETS; i++)
        fprintf(f, ",%" PRIu64, stats->rx.burst_hist[i]);
    fprintf(f, ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64,
            stats->worker.received, stats->worker.sent,
            stats->worker.emulator_drops, stats->worker.tx_ring_drops);
    fprintf(f, ",%" PRIu64 ",%" PRIu64, stats->tx.transmitted, stats->tx.tx_drops);
    for (int i = 0; i < BURST_HIST_BUCKETS; i++)
        fprintf(f, ",%" PRIu64, stats->tx.burst_hist[i]);
    RTE_ETH_FOREACH_DEV(port_id)
    {
        struct rte_eth_stats eth_stats;
        if (rte_eth_stats_get(port_id, &eth_stats) == 0)
            fprintf(f, ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64,
                    eth_stats.imissed, eth_stats.ierrors,
                    eth_stats.oerrors, eth_stats.rx_nombuf);
        else
            fprintf(f, ",0,0,0,0");
    }
    fprintf(f, "\n");

    fclose(f);
    printf("Stats saved to: %s\n", filename);
}

// -------------------------------------------
// main method: startup code
// -------------------------------------------
int main(int argc, char **argv)
{
    // EAL managed infrastructure.
    struct rte_mempool *mbuf_pool;

    // Ring-buffers for communication across threads.
    // The naming scheme for these datastructures in DPDK is producer_consumer_type.
    struct rte_ring *receive_worker_ring;
    struct rte_ring *worker_transmit_ring;

    // Local variables.
    int num_eal_arguments;
    int num_ports;
    uint16_t port_id;
    unsigned int lcore_id;
    unsigned int worker_id = 0;

    rte_log_set_level(RTE_LOGTYPE_EAL, RTE_LOG_ERR);

    // Start by initializing the DPDK environment abstraction layer (EAL).
    // This will process arguments until it encounters the first argument it does not know
    // -> The rest may be used as application specific arguments
    num_eal_arguments = rte_eal_init(argc, argv);
    if (num_eal_arguments < 0)
        rte_exit(EXIT_FAILURE, "Invalid EAL arguments\n");

    // Shift the argument vector to the application specific arguments.
    argc -= num_eal_arguments;
    argv += num_eal_arguments;

    // Parse application-level arguments.
    const char *stats_file = NULL;
    optind = 1; // reset after EAL's getopt
    int opt;
    while ((opt = getopt(argc, argv, "o:")) != -1)
    {
        if (opt == 'o')
            stats_file = optarg;
        else
            rte_exit(EXIT_FAILURE, "Usage: ... -- [-o <stats.csv>]\n");
    }

    // Initialize the signal handlers for graceful shutdown
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    // This application requires a minimum of 2 cores (1 main/monitor + 1 worker).
    // Supported configurations:
    //   2 cores: RX+Worker+TX combined on 1 core, monitor on main
    //   3 cores: RX on 1 core, Worker+TX combined on 1 core, monitor on main
    //   4 cores: RX, Worker, TX each on separate cores, monitor on main
    if (rte_lcore_count() < 2)
        rte_exit(EXIT_FAILURE, "This application needs at least 2 lcores.\n");
    if (rte_lcore_count() > 4)
        rte_exit(EXIT_FAILURE, "This application supports at most 4 lcores.\n");

    const unsigned int num_worker_cores = rte_lcore_count() - 1;

    // One port (single-NIC loopback) or two ports (separate RX/TX NICs).
    num_ports = rte_eth_dev_count_avail();
    if (num_ports < 1 || num_ports > 2)
        rte_exit(EXIT_FAILURE, "This application requires one or two enabled ports. "
                               "Please pass one or two PCI addresses via -a.\n");

    // Create a shared memory pool to store packet buffers in.
    mbuf_pool = rte_pktmbuf_pool_create("mbuf_pool",
                                        NUM_MBUFS * num_ports, MBUF_CACHE_SIZE, 0,
                                        RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    if (mbuf_pool == NULL)
        rte_exit(EXIT_FAILURE, "Failed to initialize mbuf pool\n");

    // Initialize each port.
    RTE_ETH_FOREACH_DEV(port_id)
    {
        if (initialize_port(port_id, mbuf_pool) != 0)
            rte_exit(EXIT_FAILURE, "Failed to initialize port %u\n", port_id);
    }

    // Allocate shared pipeline statistics (zeroed, cache-line aligned).
    struct pipeline_stats *stats = rte_zmalloc("stats",
                                               sizeof(struct pipeline_stats), 64);
    if (!stats)
        rte_panic("Failed to allocate memory for pipeline stats.");

    // Create rings only when stages run on separate cores.
    // 2-core mode: no rings needed (all stages in one thread).
    // 3-core mode: RX→Worker ring only (Worker+TX are combined).
    // 4-core mode: both rings.
    receive_worker_ring = NULL;
    worker_transmit_ring = NULL;

    if (num_worker_cores >= 2)
    {
        receive_worker_ring = rte_ring_create("rx_ring", SCHED_RX_RING_SZ,
                                              rte_socket_id(), RING_F_SC_DEQ | RING_F_SP_ENQ);
        if (receive_worker_ring == NULL)
            rte_exit(EXIT_FAILURE, "Failed to create input ring\n");
    }
    if (num_worker_cores >= 3)
    {
        worker_transmit_ring = rte_ring_create("tx_ring", SCHED_TX_RING_SZ,
                                               rte_socket_id(), RING_F_SC_DEQ | RING_F_SP_ENQ);
        if (worker_transmit_ring == NULL)
            rte_exit(EXIT_FAILURE, "Failed to create output ring\n");
    }

    // Collect worker lcore IDs.
    int core_ids[3] = {-1, -1, -1};
    unsigned int core_idx = 0;
    RTE_LCORE_FOREACH_WORKER(lcore_id)
    {
        if (core_idx < 3)
            core_ids[core_idx++] = lcore_id;
    }

// Helper to allocate and populate lcore_parameters.
#define ALLOC_PARAMS(name)                                                 \
    struct lcore_parameters *name = rte_malloc(NULL, sizeof(*name), 0);    \
    if (!name)                                                             \
        rte_panic("Failed to allocate lcore parameters.");                 \
    *name = (struct lcore_parameters)                                      \
    {                                                                      \
        worker_id++, receive_worker_ring, worker_transmit_ring, mbuf_pool, \
            stats, (uint16_t)num_ports                                     \
    }

    // Wire and launch pipeline based on available cores.
    if (num_worker_cores == 1)
    {
        // 2-core mode: RX + Worker + TX all on one thread.
        printf("Starting in 2-core mode: RX+Worker+TX combined on core %d\n",
               core_ids[0]);
        ALLOC_PARAMS(params);
        rte_eal_remote_launch((lcore_function_t *)lcore_receive_worker_transmit,
                              params, core_ids[0]);
    }
    else if (num_worker_cores == 2)
    {
        // 3-core mode: RX on one core, Worker+TX combined on another.
        printf("Starting in 3-core mode: Worker+TX on core %d, RX on core %d\n",
               core_ids[0], core_ids[1]);
        ALLOC_PARAMS(wt_params);
        rte_eal_remote_launch((lcore_function_t *)lcore_worker_transmit,
                              wt_params, core_ids[0]);

        ALLOC_PARAMS(rx_params);
        rte_eal_remote_launch((lcore_function_t *)lcore_receive,
                              rx_params, core_ids[1]);
    }
    else
    {
        // 4-core mode: each stage on its own core (original behavior).
        printf("Starting in 4-core mode: TX on core %d, Worker on core %d, "
               "RX on core %d\n",
               core_ids[0], core_ids[1], core_ids[2]);
        ALLOC_PARAMS(tx_params);
        rte_eal_remote_launch((lcore_function_t *)lcore_transmit,
                              tx_params, core_ids[0]);

        ALLOC_PARAMS(wk_params);
        rte_eal_remote_launch((lcore_function_t *)lcore_worker,
                              wk_params, core_ids[1]);

        ALLOC_PARAMS(rx_params);
        rte_eal_remote_launch((lcore_function_t *)lcore_receive,
                              rx_params, core_ids[2]);
    }

#undef ALLOC_PARAMS

    // -------------------------------------------
    // Monitoring loop: print stats every second
    // -------------------------------------------
    struct pipeline_stats prev_stats;
    memset(&prev_stats, 0, sizeof(prev_stats));

    while (!quit_signal_transmit)
    {
        sleep(1);
        print_stats(stats, &prev_stats);
        prev_stats = *stats;
    }

    // Wait for all cores to finish.
    RTE_LCORE_FOREACH_WORKER(lcore_id)
    {
        if (rte_eal_wait_lcore(lcore_id) < 0)
            return -1;
    }

    // Print final stats (don't overwrite — print fresh).
    stats_printed_once = 0;
    printf("\n=== Final Stats ===\n");
    print_stats(stats, &prev_stats);

    // Print cumulative burst histograms over the entire run.
    static const uint64_t zero_hist[BURST_HIST_BUCKETS] = {0};
    printf("=== Cumulative Burst Histograms ===\n");
    print_burst_hist("RX", stats->rx.burst_hist, zero_hist, stats->rx.received);
    print_burst_hist("TX", stats->tx.burst_hist, zero_hist, stats->tx.transmitted);

    if (stats_file)
        write_stats_csv(stats_file, stats);

    // Close ports before cleanup.
    RTE_ETH_FOREACH_DEV(port_id)
    {
        if (close_port(port_id) != 0)
            rte_exit(EXIT_FAILURE, "Failed to close port %u\n", port_id);
    }

    // Finally cleanup the EAL on application shutdown.
    rte_free(stats);
    rte_eal_cleanup();

    return 0;
}
