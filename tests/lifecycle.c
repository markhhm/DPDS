/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <string.h>
#include <stdio.h>
#include <rte_eal.h>
#include "../core/worker.h"
int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    struct rte_mempool *pool = rte_pktmbuf_pool_create("lifecycle_test", 255, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN),
        RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool && host_load("config.json"));
    struct pipeline_stats stats = {0};
    struct worker_state worker;
    worker_state_init(&worker, &stats, 2);
    for (unsigned i = 0; i < 33; ++i) {
        struct rte_mbuf *m = rte_pktmbuf_alloc(pool), *out[MAX_BURST_SIZE];
        assert(m);
        uint8_t *d = (uint8_t *)rte_pktmbuf_append(m, 76);
        assert(d);
        memset(d, 0, 76); d[12] = 0x81; d[15] = 100; d[16] = 0x08;
        *packet_meta(m) = (struct packet_meta){.arrival_ns = i * 100, .ingress_seq = i, .ingress_port = 7};
        assert(worker_step_at(&worker, &m, 1, out, 0) == 0);
    }
    assert(host.invalid && quit_signal_receive);
    assert(stats.worker.storage_drops == 1 && stats.worker.emulator_drops == 0);
    assert(stats.worker.storage_drop_bytes == 76);
    worker_state_cleanup(&worker);
    assert(stats.worker.shutdown_drops == 32 && stats.worker.shutdown_drop_bytes == 2432);
    host_cleanup();
    assert(rte_mempool_avail_count(pool) == 255);
    rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
    puts("resource invalidation and forced ownership drain: PASS");
}
