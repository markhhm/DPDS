/* SPDX-License-Identifier: Apache-2.0 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <rte_eal.h>
#include "../core/worker.h"
int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0 && host_load("ledger.json"));
    struct rte_mempool *pool = rte_pktmbuf_pool_create("ledger_test", 255, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN),
        RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    struct pipeline_stats stats = {0}; struct worker_state worker;
    worker_state_init(&worker, &stats, 2);
    struct rte_mbuf *m = rte_pktmbuf_alloc(pool), *out[MAX_BURST_SIZE];
    assert(m);
    uint8_t *d = (uint8_t *)rte_pktmbuf_append(m, 76);
    assert(d); memset(d, 0, 76); d[12]=0x81; d[15]=100; d[16]=0x08;
    *packet_meta(m) = (struct packet_meta){.ingress_port=7};
    assert(worker_step_at(&worker, &m, 1, out, 0) == 0);
    assert(stats.worker.emulator_drops == 1 && rte_mempool_avail_count(pool) == 255);
    assert(hng_engine_len(host.engine, 0) == 0 && worker_has_backlog(&worker));
    worker_end_input(&worker);
    assert(worker_step_at(&worker, NULL, 0, out, 99) == 0 && worker_has_backlog(&worker));
    struct HngStats core; assert(hng_engine_stats(host.engine, 0, &core) && core.q_bytes == 80);
    assert(worker_step_at(&worker, NULL, 0, out, 100) == 0 && !worker_has_backlog(&worker));
    assert(hng_engine_stats(host.engine, 0, &core) && core.q_bytes == 0 && core.ledger_watermark_ns == 100);
    worker_state_cleanup(&worker); host_cleanup();
    assert(!host.invalid && rte_mempool_avail_count(pool) == 255);
    rte_mempool_free(pool); assert(rte_eal_cleanup() == 0);
    puts("all-trace-loss service ledger drains at completion despite zero payload custody: PASS");
}
