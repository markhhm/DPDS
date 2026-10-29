/* DPDS HNG host integration. Original DPDS authors and Apache-2.0 license retained. */
#include <stdio.h>
#include <stdlib.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <unistd.h>
#include <getopt.h>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_malloc.h>
#include <rte_cycles.h>
#include "common/common.h"
#include "core/receive.h"
#include "core/worker.h"
#include "core/transmit.h"
#include "core/combined.h"

static void report(FILE *out, struct pipeline_stats *s)
{
    uint64_t drops = s->rx.rx_ring_drops + s->worker.emulator_drops + s->worker.invalid_drops +
        s->worker.storage_drops + s->worker.tx_ring_drops + s->worker.shutdown_drops +
        s->tx.tx_drops + s->tx.trace_outage_drops;
    bool conserved = s->rx.received == drops + s->tx.transmitted;
    if (!conserved) host_invalidate("software packet conservation failed");
    uint64_t core_drop_bytes = 0;
    for (size_t i = 0; i < host.line_count; ++i) {
        struct HngStats *h = &s->worker.lines[i];
        core_drop_bytes += h->q_drop_payload_bytes + h->trace_loss_bytes + h->trace_outage_bytes;
        fprintf(out, "line=%zu ingress=%u egress=%u vlan=%u accepted=%"PRIu64" q_drop=%"PRIu64
            " trace_loss=%"PRIu64" trace_outage=%"PRIu64" storage_drop=%"PRIu64
            " q_peak_l2=%"PRIu64" storage_peak=%"PRIu64" missed_deadline_outage_drop=%"PRIu64" tx_invalid_drop=%"PRIu64"\n",
            i, host.lines[i].ingress_port, host.lines[i].egress_port, host.lines[i].vlan_id,
            h->accepted, h->q_drops, h->trace_loss_drops, h->trace_outage_drops,
            h->storage_drops, h->q_high_water_bytes, h->storage_high_water_packets,
            s->tx.line_outage_drops[i], s->tx.line_invalid_drops[i]);
        fprintf(out, "line=%zu c_bps=%"PRIu64" q_l2_bytes=%"PRIu64" seed=%"PRIu64
            " selection_threshold=%"PRIu64" always_late=%u accepted_payload_bytes=%"PRIu64
            " accepted_l2_bytes=%"PRIu64" accepted_wire_bytes=%"PRIu64
            " q_drop_payload_bytes=%"PRIu64" q_drop_l2_bytes=%"PRIu64
            " trace_loss_payload_bytes=%"PRIu64" trace_outage_payload_bytes=%"PRIu64
            " storage_drop_payload_bytes=%"PRIu64" invalid_drop_payload_bytes=%"PRIu64
            " dequeued=%"PRIu64" dequeued_payload_bytes=%"PRIu64
            " admission_watermark_ns=%"PRIu64" q_current_l2_bytes=%"PRIu64
            " storage_current_packets=%"PRIu64" storage_current_bytes=%"PRIu64
            " tx_accepted=%"PRIu64" tx_payload_bytes=%"PRIu64"\n",
            i, host.lines[i].c_bps, host.lines[i].q_bytes, host.lines[i].seed,
            host.lines[i].threshold, host.lines[i].always_late,
            h->accepted_bytes, h->accepted_l2_bytes, h->accepted_wire_bytes,
            h->q_drop_payload_bytes, h->q_drop_bytes, h->trace_loss_bytes, h->trace_outage_bytes,
            h->storage_drop_bytes, h->invalid_drop_bytes, h->dequeued, h->dequeued_bytes,
            h->admission_watermark_ns, h->q_bytes, h->storage_packets, h->storage_bytes,
            s->tx.line_transmitted[i], s->tx.line_transmitted_bytes[i]);
        fprintf(out, "line=%zu ledger_watermark_ns=%"PRIu64" input_complete=%u\n",
            i, h->ledger_watermark_ns, h->input_complete);
    }
    uint64_t drop_bytes = s->rx.rx_ring_drop_bytes + core_drop_bytes + s->worker.invalid_drop_bytes +
        s->worker.storage_drop_bytes + s->worker.tx_ring_drop_bytes + s->worker.shutdown_drop_bytes +
        s->tx.tx_drop_bytes + s->tx.trace_outage_bytes;
    bool bytes_conserved = s->rx.received_bytes == drop_bytes + s->tx.transmitted_bytes;
    if (!bytes_conserved) host_invalidate("software byte conservation failed");
    fprintf(out, "rx_ring_full_skips=%"PRIu64",%"PRIu64"\n", s->rx.ring_full_skips[0], s->rx.ring_full_skips[1]);
    fprintf(out, "submit_late_packets=%"PRIu64" submit_late_total_ns=%"PRIu64" submit_late_max_ns=%"PRIu64"\n",
        s->tx.late_submit_packets, s->tx.late_submit_total_ns, s->tx.late_submit_max_ns);
    fprintf(out, "rx=%"PRIu64" rx_bytes=%"PRIu64" tx_accepted=%"PRIu64" tx_bytes=%"PRIu64
        " software_drops=%"PRIu64" drop_bytes=%"PRIu64" invalid_input=%"PRIu64
        " rx_handoff_drop=%"PRIu64" tx_handoff_drop=%"PRIu64" terminal_tx_drop=%"PRIu64
        " shutdown_drop=%"PRIu64" retries=%"PRIu64" pending_peak=%"PRIu64
        " conservation=%s byte_conservation=%s status=%s\n",
        s->rx.received, s->rx.received_bytes, s->tx.transmitted, s->tx.transmitted_bytes,
        drops, drop_bytes, s->worker.invalid_drops, s->rx.rx_ring_drops, s->worker.tx_ring_drops,
        s->tx.tx_drops, s->worker.shutdown_drops, s->tx.retries, s->tx.pending_peak,
        conserved ? "PASS" : "FAIL", bytes_conserved ? "PASS" : "FAIL", host.invalid ? "INVALID" : "DRAINED");
    for (uint16_t i = 0; i < host.port_count; ++i) {
        uint16_t port = host.ports[i];
        struct rte_eth_stats n;
        if (rte_eth_stats_get(port, &n) == 0)
            fprintf(out, "nic=%u raw_ipackets=%"PRIu64" raw_opackets=%"PRIu64
                " imissed=%"PRIu64" ierrors=%"PRIu64" oerrors=%"PRIu64" rx_nombuf=%"PRIu64"\n",
                port, n.ipackets, n.opackets, n.imissed, n.ierrors, n.oerrors, n.rx_nombuf);
        else fprintf(out, "nic=%u raw_counters=UNAVAILABLE\n", port);
    }
}

int main(int argc, char **argv)
{
    int eal_args = rte_eal_init(argc, argv);
    if (eal_args < 0) rte_exit(EXIT_FAILURE, "Invalid EAL arguments\n");
    argc -= eal_args; argv += eal_args;
    const char *config = "config.json", *stats_path = NULL;
    optind = 1;
    int option;
    while ((option = getopt(argc, argv, "c:o:")) != -1) {
        if (option == 'c') config = optarg;
        else if (option == 'o') stats_path = optarg;
        else rte_exit(EXIT_FAILURE, "Usage: ... -- [-c config.json] [-o stats.txt]\n");
    }
    if (rte_lcore_count() < 2 || rte_lcore_count() > 5)
        rte_exit(EXIT_FAILURE, "HNG requires 2, 3, 4 or 5 lcores\n");
    if (!host_load(config)) rte_exit(EXIT_FAILURE, "HNG configuration rejected before RX\n");
    uint16_t port;
    RTE_ETH_FOREACH_DEV(port) host.ports[host.port_count++] = port;
    if (host.port_count < 1 || host.port_count > 2)
        rte_exit(EXIT_FAILURE, "Enable one or two software/physical ports\n");
    for (size_t i = 0; i < host.line_count; ++i) {
        if (!rte_eth_dev_is_valid_port(host.lines[i].ingress_port) ||
            !rte_eth_dev_is_valid_port(host.lines[i].egress_port))
            rte_exit(EXIT_FAILURE, "Line %zu references an unavailable port\n", i);
    }
    for (uint16_t i = 0; i < host.port_count; ++i)
        if (host_prepare_port(host.ports[i]) != 0)
            rte_exit(EXIT_FAILURE, "Port %u descriptor preflight failed\n", host.ports[i]);
    struct rte_mempool *pools[RTE_MAX_ETHPORTS] = {0};
    for (uint16_t i = 0; i < host.port_count; ++i) {
        port = host.ports[i];
        uint64_t count = host_pool_objects(port, rte_lcore_count());
        if (!count) rte_exit(EXIT_FAILURE, "Port %u has an invalid pool envelope\n", port);
        char name[32];
        snprintf(name, sizeof(name), "ingress_pool_%u", port);
        int socket = rte_eth_dev_socket_id(port);
        if (socket < 0) socket = rte_socket_id();
        pools[port] = rte_pktmbuf_pool_create(name, count, MBUF_CACHE_SIZE,
            RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN),
            RTE_MBUF_DEFAULT_BUF_SIZE, socket);
        if (!pools[port]) rte_exit(EXIT_FAILURE, "Cannot allocate port %u pool: %s\n", port, rte_strerror(rte_errno));
        struct rte_mempool_objsz size;
        rte_mempool_calc_obj_size(pools[port]->elt_size, pools[port]->flags, &size);
        printf("pool port=%u numa=%d objects=%"PRIu64" object_bytes=%u total_object_bytes=%"PRIu64"\n",
            port, socket, count, size.total_size, count * size.total_size);
    }
    for (uint16_t i = 0; i < host.port_count; ++i) {
        port = host.ports[i];
        if (initialize_port(port, pools[port]) != 0) rte_exit(EXIT_FAILURE, "Port %u initialization failed\n", port);
        printf("nic=%u reset=%s\n", port, rte_eth_stats_reset(port) == 0 ? "OK" : "UNAVAILABLE");
    }
    printf("clock=software-rx-relative-ns queues=1 offloads=none VLAN=retained FCS=stripped wire_overhead=20\n");
    struct pipeline_stats *stats = rte_zmalloc("stats", sizeof(*stats), 64);
    if (!stats) rte_exit(EXIT_FAILURE, "Cannot allocate counters\n");
    unsigned workers = rte_lcore_count() - 1;
    bool split = workers == 4;
    if (split && (host.port_count != 2 || !host_split_valid()))
        rte_exit(EXIT_FAILURE, "5 lcores need two ports, each egress fed by one ingress port\n");
    struct rte_ring *rx_ring = NULL, *tx_ring = NULL, *owner_rings[2] = {0};
    if (workers >= 2 && !split) rx_ring = rte_ring_create("rx_ring", SCHED_RX_RING_SZ, rte_socket_id(), RING_F_SC_DEQ | RING_F_SP_ENQ);
    for (unsigned k = 0; split && k < 2; ++k) {
        char name[32];
        snprintf(name, sizeof(name), "rx_ring%u", k);
        owner_rings[k] = rte_ring_create(name, SCHED_RX_RING_SZ, rte_socket_id(), RING_F_SC_DEQ | RING_F_SP_ENQ);
        if (!owner_rings[k]) rte_exit(EXIT_FAILURE, "Cannot allocate handoff ring\n");
    }
    /* Two schedulers share the TX handoff ring as producers. */
    if (workers >= 3) tx_ring = rte_ring_create("tx_ring", SCHED_TX_RING_SZ, rte_socket_id(), RING_F_SC_DEQ | (split ? 0 : RING_F_SP_ENQ));
    if ((workers >= 2 && !split && !rx_ring) || (workers >= 3 && !tx_ring)) rte_exit(EXIT_FAILURE, "Cannot allocate handoff ring\n");
    signal(SIGINT, signal_handler); signal(SIGTERM, signal_handler);
    unsigned cores[4], index = 0, core;
    RTE_LCORE_FOREACH_WORKER(core) cores[index++] = core;
    lcore_function_t *functions[4];
    if (workers == 1) functions[0] = (lcore_function_t *)lcore_receive_worker_transmit;
    else if (workers == 2) {
        functions[0] = (lcore_function_t *)lcore_worker_transmit;
        functions[1] = (lcore_function_t *)lcore_receive;
    } else if (workers == 3) {
        functions[0] = (lcore_function_t *)lcore_transmit;
        functions[1] = (lcore_function_t *)lcore_worker;
        functions[2] = (lcore_function_t *)lcore_receive;
    } else {
        functions[0] = (lcore_function_t *)lcore_transmit;
        functions[1] = functions[2] = (lcore_function_t *)lcore_worker;
        functions[3] = (lcore_function_t *)lcore_receive;
        host.schedulers_running = 2;
    }
    host.epoch_cycles = rte_get_timer_cycles();
    host_mark_launched();
    printf("layout=%u-lcore%s\n", workers + 1, split ? " schedulers=per-ingress-port" : "");
    for (unsigned i = 0; i < workers; ++i) {
        struct lcore_parameters *parameters = rte_zmalloc(NULL, sizeof(*parameters), 0);
        if (!parameters) rte_exit(EXIT_FAILURE, "Cannot allocate core parameters\n");
        *parameters = (struct lcore_parameters){.worker_id = i, .receive_worker_ring = rx_ring,
            .worker_transmit_ring = tx_ring, .stats = stats, .num_ports = host.port_count};
        if (split) {
            parameters->split = true;
            parameters->owner_rings[0] = owner_rings[0];
            parameters->owner_rings[1] = owner_rings[1];
            parameters->owner_index = (uint16_t)(i - 1);
            if (i == 1 || i == 2) parameters->receive_worker_ring = owner_rings[i - 1];
        }
        if (rte_eal_remote_launch(functions[i], parameters, cores[i]) != 0)
            rte_exit(EXIT_FAILURE, "Cannot launch lcore\n");
    }
    RTE_LCORE_FOREACH_WORKER(core) {
        if (rte_eal_wait_lcore(core) < 0) host_invalidate("lcore failed");
    }
    for (unsigned k = 0; split && k < 2; ++k) worker_stats_merge(&stats->worker, &stats->owners[k], host.ports[k]);
    report(stdout, stats);
    if (stats_path) {
        FILE *out = fopen(stats_path, "wx");
        if (!out) host_invalidate("cannot create statistics file");
        else { report(out, stats); fclose(out); }
    }
    for (uint16_t i = 0; i < host.port_count; ++i) {
        port = host.ports[i];
        if (close_port(port) != 0) host_invalidate("port cleanup failed");
        unsigned free_count = rte_mempool_avail_count(pools[port]);
        printf("pool port=%u free=%u total=%u owned=%u\n", port, free_count,
            pools[port]->size, pools[port]->size - free_count);
        if (free_count != pools[port]->size) host_invalidate("mbuf ownership not fully reclaimed");
        rte_mempool_free(pools[port]);
    }
    host_cleanup();
    rte_ring_free(rx_ring); rte_ring_free(tx_ring); rte_free(stats);
    rte_ring_free(owner_rings[0]); rte_ring_free(owner_rings[1]);
    bool invalid = host.invalid;
    rte_eal_cleanup();
    return invalid ? EXIT_FAILURE : EXIT_SUCCESS;
}
