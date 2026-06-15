#include <stdio.h>
#include <inttypes.h>
#include <unistd.h>
#include <signal.h>

#include <rte_ethdev.h>
#include <rte_ether.h>

#include "common.h"
#include "config.h"

// -------------------------------------------
// Global quit signals
// -------------------------------------------
volatile uint8_t quit_signal_receive;
volatile uint8_t quit_signal_worker;
volatile uint8_t quit_signal_transmit;

// -------------------------------------------
// Signal handling
// -------------------------------------------
void signal_handler(int signum)
{
    if (signum == SIGINT || signum == SIGTERM)
    {
        // The shutdown process needs to follow the pipeline order:
        // Receive > Worker > Transmit > main thread
        // -> Start with the receive core.
        quit_signal_receive = 1;
    }
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

    // Ring sizes need to be copied here, because they might be adjusted to capabilities later.
    uint16_t num_rx_descriptors = RX_RING_SIZE;
    uint16_t num_tx_descriptors = TX_RING_SIZE;

    if (!rte_eth_dev_is_valid_port(port))
        return -1;

    // First try and get the device info for the port.
    error_code = rte_eth_dev_info_get(port, &device_info);
    if (error_code != 0)
    {
        // It seems the rte method returns an inverted pointer to an error message as error code or sth like that.
        printf("Could not get device info for port %u: %s\n", port, rte_strerror(-error_code));
        return error_code;
    }

    // If the port has TX offload capabilities, we want to use them.
    if (device_info.tx_offload_capa & RTE_ETH_TX_OFFLOAD_MBUF_FAST_FREE)
        port_configuration.txmode.offloads |= RTE_ETH_TX_OFFLOAD_MBUF_FAST_FREE;

    // Store information about RSS hashing capabilities.
    port_configuration.rx_adv_conf.rss_conf.rss_hf &= device_info.flow_type_rss_offloads;

    // Now configure the device based on the accumulated config.
    error_code = rte_eth_dev_configure(port, rx_rings, tx_rings, &port_configuration);
    if (error_code != 0)
        return error_code;

    // After setup, determine the ring sizes. The method might adjust ring sizes to capabilities.
    error_code = rte_eth_dev_adjust_nb_rx_tx_desc(port, &num_rx_descriptors, &num_tx_descriptors);
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
    if (error_code != 0)
        return error_code;

    return 0;
}
