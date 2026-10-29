#ifndef CONFIG_H
#define CONFIG_H

#include <rte_ethdev.h>

// -------------------------------------------
// Static settings
// -------------------------------------------
#define RX_RING_SIZE 256
#define TX_RING_SIZE 1024

#define MBUF_CACHE_SIZE 32
#define SCHED_RX_RING_SZ 1024
#define SCHED_TX_RING_SZ 1024
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
// 1024 pending packets per line cover about 126 us at 8.127 Mpps (estimate); see the M4 report.
#define TX_PENDING_PER_LINE 1024
#define TERMINAL_TIMEOUT_NS UINT64_C(1000000000)
#define SHUTDOWN_TIMEOUT_NS UINT64_C(5000000000)
static const struct rte_eth_conf default_port_configuration = {
    .rxmode = { .mq_mode = RTE_ETH_MQ_RX_NONE, .offloads = 0 },
    .txmode = { .mq_mode = RTE_ETH_MQ_TX_NONE, .offloads = 0 },
};

#endif
