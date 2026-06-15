#ifndef CONFIG_H
#define CONFIG_H

#include <rte_ethdev.h>

// -------------------------------------------
// Static settings
// -------------------------------------------
#define RX_RING_SIZE 65535
#define TX_RING_SIZE 65535
#define NUM_MBUFS ((1024 * 1024) - 1)
#define MBUF_CACHE_SIZE 512 // Maximum cache size
#define SCHED_RX_RING_SZ 65535
#define SCHED_TX_RING_SZ 65535
// Same as in config.json
#define MAX_BURST_SIZE 16
// Number of histogram buckets for burst size tracking.
// Bucket i covers burst sizes [2^i, 2^(i+1)-1]; the last bucket is MAX_BURST_SIZE.
// Requires MAX_BURST_SIZE to be a power of 2.
#define BURST_HIST_BUCKETS (__builtin_ctz(MAX_BURST_SIZE) + 1)

#define ANSI_COLOR_RED "\x1b[31m"
#define ANSI_COLOR_RESET "\x1b[0m"

#define PROJECT_NAME "dpds"

// -------------------------------------------
// Global defaults
// -------------------------------------------
// Mostly copied from distributor example application.
static const struct rte_eth_conf default_port_configuration = {
	.rxmode = {
		.mq_mode = RTE_ETH_MQ_RX_RSS,
	},
	.txmode = {
		.mq_mode = RTE_ETH_MQ_TX_NONE,
	},
	.rx_adv_conf = {.rss_conf = {
						.rss_hf = RTE_ETH_RSS_IP | RTE_ETH_RSS_UDP | RTE_ETH_RSS_TCP | RTE_ETH_RSS_SCTP,
					}},
};

#endif // CONFIG_H
