#include <assert.h>
#include <string.h>
#include <stdio.h>
#include <rte_eal.h>
#include "../core/worker.h"
int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    struct rte_mempool *pool = rte_pktmbuf_pool_create("reorder_test", 255, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN),
        RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool && host_load("reorder.json"));
    struct pipeline_stats stats = {0};
    struct worker_state worker;
    worker_state_init(&worker, &stats, 2);
    struct rte_mbuf *packets[2], *out[MAX_BURST_SIZE];
    for (unsigned i = 0; i < 2; ++i) {
        packets[i] = rte_pktmbuf_alloc(pool);
        assert(packets[i]);
        uint8_t *d = (uint8_t *)rte_pktmbuf_append(packets[i], 76);
        assert(d);
        memset(d, 0, 76); d[12] = 0x81; d[15] = 100; d[16] = 0x08;
        *packet_meta(packets[i]) = (struct packet_meta){.arrival_ns=0, .ingress_seq=i, .ingress_port=7};
    }
    /* Seed3 selects first packet late, second ordinary: deadlines211/210. */
    host_publish(7, 2, 1000);
    assert(worker_step_at(&worker, packets, 1, out, 211) == 0);
    assert(worker_step_at(&worker, packets + 1, 1, out, 211) == 2);
    assert(out[0] == packets[1] && packet_meta(out[0])->deadline_ns == 210);
    assert(out[1] == packets[0] && packet_meta(out[1])->deadline_ns == 211);
    rte_pktmbuf_free(out[0]); rte_pktmbuf_free(out[1]);
    worker_end_input(&worker);
    worker_state_cleanup(&worker);
    host_cleanup();
    assert(rte_mempool_avail_count(pool) == 255);
    rte_mempool_free(pool);
    assert(rte_eal_cleanup() == 0);
    puts("older RX ordinary packet precedes already-due late head: PASS");
}
