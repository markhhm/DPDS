#include <stdio.h>
#include <inttypes.h>
#include <unistd.h>
#include <signal.h>

#include <rte_ethdev.h>
#include <rte_ether.h>
#include <rte_cycles.h>
#include <string.h>
#include <limits.h>
#include <errno.h>

#include "common.h"
#include "config.h"

// -------------------------------------------
// Global quit signals
// -------------------------------------------
_Atomic uint8_t quit_signal_receive, quit_signal_worker, quit_signal_transmit;
struct host_context host;

uint64_t host_now_ns(void)
{
    return (uint64_t)(((__uint128_t)(rte_get_timer_cycles() - host.epoch_cycles)
        * UINT64_C(1000000000)) / host.cycles_hz);
}

void host_invalidate(const char *reason)
{
    if (!atomic_exchange(&host.invalid, true)) fprintf(stderr, "INVALID RUN: %s\n", reason);
    quit_signal_receive = 1;
}

bool host_load(const char *path)
{
    memset(&host, 0, sizeof(host));
    host.epoch_cycles = rte_get_timer_cycles();
    host.cycles_hz = rte_get_timer_hz();
    host.engine = hng_engine_load(path);
    if (!host.engine) return false;
    host.line_count = hng_engine_line_count(host.engine);
    for (size_t i = 0; i < host.line_count; ++i) {
        if (!hng_engine_line_info(host.engine, i, &host.lines[i])) return false;
        host.policies[i] = hng_tx_policy_new(host.engine, i);
        if (!host.policies[i]) return false;
    }
    return host_take_line_handles();
}

bool host_take_line_handles(void)
{
    /* hng_engine_line borrows the whole Engine; owners may already hold line handles. */
    if (atomic_load(&host.launched)) {
        host_invalidate("line handle requested after owner launch");
        return false;
    }
    for (size_t i = 0; i < host.line_count; ++i)
        if (!(host.line_handle[i] = hng_engine_line(host.engine, i))) return false;
    return true;
}

void host_mark_launched(void) { atomic_store(&host.launched, true); }

bool host_split_valid(void)
{
    for (size_t i = 0; i < host.line_count; ++i)
        for (size_t j = 0; j < host.line_count; ++j)
            if (host.lines[i].egress_port == host.lines[j].egress_port &&
                host.lines[i].ingress_port != host.lines[j].ingress_port) return false;
    return true;
}

bool host_io_split_valid(void)
{
    if (!host_split_valid()) return false;
    for (size_t i = 0; i < host.line_count; ++i)
        for (size_t j = 0; j < host.line_count; ++j)
            if (host.lines[i].ingress_port == host.lines[j].ingress_port &&
                host.lines[i].egress_port != host.lines[j].egress_port) return false;
    return true;
}

void host_cleanup(void)
{
    for (size_t i = 0; i < host.line_count; ++i) hng_tx_policy_destroy(host.policies[i]);
    if (!hng_engine_destroy(host.engine)) {
        host_invalidate("forced service ledger abort at final destroy");
        for (size_t line = 0; line < host.line_count; ++line) {
            struct HngStats stats;
            if (hng_engine_stats(host.engine, line, &stats))
                fprintf(stderr, "abort line=%zu outstanding_q_l2_bytes=%"PRIu64"\n", line, stats.q_bytes);
        }
        if (!hng_engine_abort_destroy(host.engine)) host_invalidate("payload remains at forced destroy");
    }
    host.engine = NULL;
}

/* All fields are atomic: a seqlock alone would leave C data races. */
void host_publish(uint16_t port, uint64_t enqueued, uint64_t watermark)
{
    struct ingress_frontier *f = &host.frontier[port];
    atomic_fetch_add_explicit(&f->version, 1, memory_order_seq_cst);
    atomic_store_explicit(&f->enqueued, enqueued, memory_order_seq_cst);
    atomic_store_explicit(&f->watermark_ns, watermark, memory_order_seq_cst);
    atomic_fetch_add_explicit(&f->version, 1, memory_order_seq_cst);
}

bool host_frontier(uint16_t port, uint64_t consumed, uint64_t *watermark)
{
    struct ingress_frontier *f = &host.frontier[port];
    uint64_t v = atomic_load_explicit(&f->version, memory_order_seq_cst);
    if (v & 1) return false;
    uint64_t enqueued = atomic_load_explicit(&f->enqueued, memory_order_seq_cst);
    uint64_t w = atomic_load_explicit(&f->watermark_ns, memory_order_seq_cst);
    if (v != atomic_load_explicit(&f->version, memory_order_seq_cst) || consumed < enqueued) return false;
    *watermark = w;
    return true;
}

// -------------------------------------------
// Signal handling
// -------------------------------------------
void signal_handler(int signum)
{
    if (signum == SIGINT || signum == SIGTERM)
    {
        quit_signal_receive = 1;
    }
}

int host_prepare_port(uint16_t port)
{
    if (port >= RTE_MAX_ETHPORTS) return -EINVAL;
    struct port_resources *r = &host.resources[port];
    if (r->prepared) return 0;
    uint16_t rx = RX_RING_SIZE, tx = TX_RING_SIZE;
    int result = rte_eth_dev_adjust_nb_rx_tx_desc(port, &rx, &tx);
    if (result != 0) return result;
    if (!rx || !tx) return -EINVAL;
    r->rx_descriptors = rx; r->tx_descriptors = tx; r->prepared = true;
    printf("descriptors port=%u rx=%u tx=%u\n", port, rx, tx);
    return 0;
}

unsigned host_pool_objects(uint16_t port, unsigned lcores)
{
    if (port >= RTE_MAX_ETHPORTS || !host.resources[port].prepared || lcores < 1 || lcores > 7) return 0;
    uint64_t count = host.resources[port].rx_descriptors + SCHED_RX_RING_SZ + SCHED_TX_RING_SZ +
        MBUF_CACHE_SIZE * lcores + HNG_MAX_LINES * TX_PENDING_PER_LINE + 4 * MAX_BURST_SIZE;
    bool reserved_egress[RTE_MAX_ETHPORTS] = {0};
    for (size_t line = 0; line < host.line_count; ++line) {
        if (host.lines[line].ingress_port != port) continue;
        uint16_t egress = host.lines[line].egress_port;
        if (egress >= RTE_MAX_ETHPORTS || !host.resources[egress].prepared) return 0;
        if (!reserved_egress[egress]) {
            if (host.resources[egress].tx_descriptors > UINT_MAX - count) return 0;
            count += host.resources[egress].tx_descriptors;
            reserved_egress[egress] = true;
        }
        if (host.lines[line].storage_capacity_packets > UINT_MAX - count) return 0;
        count += host.lines[line].storage_capacity_packets;
    }
    return (unsigned)count;
}

bool host_check_nic_loss(uint16_t port, const struct rte_eth_stats *n)
{
    if (!n->imissed && !n->rx_nombuf) return true;
    fprintf(stderr, "nic=%u imissed=%" PRIu64 " rx_nombuf=%" PRIu64 "\n", port, n->imissed, n->rx_nombuf);
    host_invalidate("NIC dropped received frames (imissed or rx_nombuf)");
    return false;
}

struct rte_mempool *host_create_pool(uint16_t port, unsigned lcores, int *socket)
{
    unsigned count = host_pool_objects(port, lcores);
    if (!count) return NULL;
    char name[32];
    snprintf(name, sizeof(name), "ingress_pool_%u", port);
    *socket = rte_eth_dev_socket_id(port);
    if (*socket < 0) *socket = rte_socket_id();
    return rte_pktmbuf_pool_create(name, count, MBUF_CACHE_SIZE,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN), RTE_MBUF_DEFAULT_BUF_SIZE, *socket);
}

// -------------------------------------------
// Initialization of ports
// -------------------------------------------
int initialize_port(uint16_t port, struct rte_mempool *mbuf_pool)
{
    // This method builds a port configuration for the given port.
    struct rte_eth_conf port_configuration = default_port_configuration;
    struct rte_eth_dev_info device_info;
    struct rte_eth_txconf tx_configuration;
    struct rte_eth_link link;
    struct rte_ether_addr mac_address;
    int error_code;

    // Always one ring for RX and TX.
    const uint16_t rx_rings = 1;
    const uint16_t tx_rings = 1;

    error_code = host_prepare_port(port);
    if (error_code != 0) return error_code;
    uint16_t num_rx_descriptors = host.resources[port].rx_descriptors;
    uint16_t num_tx_descriptors = host.resources[port].tx_descriptors;

    // First try and get the device info for the port.
    error_code = rte_eth_dev_info_get(port, &device_info);
    if (error_code != 0)
    {
        // It seems the rte method returns an inverted pointer to an error message as error code or sth like that.
        printf("Could not get device info for port %u: %s\n", port, rte_strerror(-error_code));
        return error_code;
    }

    // Now configure the device based on the accumulated config.
    error_code = rte_eth_dev_configure(port, rx_rings, tx_rings, &port_configuration);
    if (error_code != 0)
        return error_code;

    // Now create the two queues with the right size.
    error_code = rte_eth_rx_queue_setup(port, 0, num_rx_descriptors,
                                        rte_eth_dev_socket_id(port),
                                        NULL, mbuf_pool);
    if (error_code != 0)
        return error_code;

    tx_configuration = device_info.default_txconf;
    tx_configuration.offloads = port_configuration.txmode.offloads;
    error_code = rte_eth_tx_queue_setup(port, 0, num_tx_descriptors,
                                        rte_eth_dev_socket_id(port),
                                        &tx_configuration);
    if (error_code != 0)
        return error_code;

    // The port is now fully set up -> start the device and wait for the link to go up.
    error_code = rte_eth_dev_start(port);
    if (error_code != 0)
        return error_code;

    error_code = rte_eth_link_get_nowait(port, &link);
    if (error_code != 0)
    {
        printf("Could not get link status for port %u: %s\n", port, rte_strerror(-error_code));
        return error_code;
    }
    if (!link.link_status)
    {
        printf("Link is down on port %" PRIu16 ", shutting down.\n", port);
        return -1;
    }

    // Get the mac address for logging.
    error_code = rte_eth_macaddr_get(port, &mac_address);
    if (error_code != 0)
    {
        printf("Could not get MAC address for port %u: %s\n", port, rte_strerror(-error_code));
        return error_code;
    }

    // Enable promiscuous mode, which basically tells the port to not filter for MAC address,
    // and instead upstream all received ethernet frames to the business logic to decide.
    error_code = rte_eth_promiscuous_enable(port);
    if (error_code != 0)
        return error_code;

    return 0;
}

// -------------------------------------------
// port cleanup
// -------------------------------------------
int close_port(uint16_t port)
{
    int error_code;

    if (!rte_eth_dev_is_valid_port(port))
        return -1;

    error_code = rte_eth_dev_stop(port);
    if (error_code != 0) return error_code;
    return rte_eth_dev_close(port);
}
