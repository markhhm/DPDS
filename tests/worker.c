#include <assert.h>
#include <stdio.h>
#include <rte_eal.h>
#include <rte_mbuf.h>
#include "../core/worker.h"

int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    struct rte_mempool *pool = rte_pktmbuf_pool_create(
        "worker_test", 255, 0, 0, RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    struct pipeline_stats stats = {0};
    struct worker_state worker = {0};
    worker_state_init(&worker, &stats, 2);
    struct rte_mbuf *batch[3], *out[MAX_BURST_SIZE];
    for (unsigned i = 0; i < 3; ++i)
    {
        batch[i] = rte_pktmbuf_alloc(pool);
        assert(batch[i]);
        assert(rte_pktmbuf_append(batch[i], 76));
        batch[i]->port = i == 1 ? 1 : 0;
    }
    assert(worker_step(&worker, batch, 3, out) == 0);
    fprintf(stderr, "directional Q: expected 1 drop, got %lu\n",
            (unsigned long)stats.worker.emulator_drops);
    // Each direction has a 100-wire-byte Q: both first packets must fit.
    assert(stats.worker.emulator_drops == 1);
    worker_state_cleanup(&worker);
    rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
    return 0;
}
