#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <rte_eal.h>
#include "../core/worker.h"
#include "../core/transmit.h"
static unsigned delivered;
static uint64_t ids[2];
static uint16_t transmit(uint16_t port, uint16_t queue, struct rte_mbuf **batch, uint16_t count)
{
    (void)queue; assert(port == 13);
    for (uint16_t i = 0; i < count; ++i) {
        assert(delivered < 2);
        ids[delivered++] = packet_meta(batch[i])->ingress_seq;
        rte_pktmbuf_free(batch[i]);
    }
    return count;
}
static void run(struct rte_mempool *pool, uint64_t tx_first_ns, uint64_t expected_first)
{
    assert(host_load("recovery.json")); delivered = 0;
    struct pipeline_stats stats = {0}; struct worker_state worker; struct tx_state tx;
    worker_state_init(&worker, &stats, 2); tx_state_init(&tx, &stats); tx.burst = transmit;
    struct rte_mbuf *input[2], *output[MAX_BURST_SIZE];
    for (unsigned i = 0; i < 2; ++i) {
        input[i] = rte_pktmbuf_alloc(pool); assert(input[i]);
        uint8_t *d = (uint8_t *)rte_pktmbuf_append(input[i], 76); assert(d);
        memset(d, 0, 76); d[12] = 0x81; d[15] = i ? 100 : 101; d[16] = 0x08;
        *packet_meta(input[i]) = (struct packet_meta){.ingress_port=7, .ingress_seq=i};
    }
    host_publish(7, 2, 1000);
    /* LEO deadline190, cable220; worker runs during outage[200,300). */
    uint16_t count = worker_step_at(&worker, input, 2, output, 250);
    tx_step_at(&tx, output, count, tx_first_ns);
    count = worker_step_at(&worker, NULL, 0, output, 300);
    tx_step_at(&tx, output, count, 300);
    assert(delivered == 2 && ids[0] == expected_first && ids[1] == 1 - expected_first);
    assert(!tx_has_backlog(&tx) && !host.invalid);
    worker_end_input(&worker);
    assert(worker_step_at(&worker, NULL, 0, output, 300) == 0 && !worker_has_backlog(&worker));
    worker_state_cleanup(&worker); tx_state_cleanup(&tx); host_cleanup();
    assert(rte_mempool_avail_count(pool) == 255);
}
int main(int argc, char **argv)
{
    assert(rte_eal_init(argc, argv) >= 0);
    struct rte_mempool *pool = rte_pktmbuf_pool_create("recovery_test", 255, 0,
        RTE_ALIGN_CEIL(sizeof(struct packet_meta), RTE_MBUF_PRIV_ALIGN),
        RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
    assert(pool);
    run(pool, 300, 0); /* Recovery before TX: both heads compete, LEO first. */
    run(pool, 250, 1); /* TX during outage: cable bypasses held LEO. */
    rte_mempool_free(pool); assert(rte_eal_cleanup() == 0);
    puts("worker/TX recovery boundary preserves actual-time port arbitration: PASS");
}
